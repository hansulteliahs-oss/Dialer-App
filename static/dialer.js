/* No Brakes - session state machine + Twilio Device client.
 *
 *   IDLE -> WARMUP -> DIALING -> BREATHER -> (target reached?) -> TALLY
 *                        ^                |
 *                        +----------------+
 *
 * Keys that exist:        SPACE  ENTER  1-5  D  P
 * Keys that do not exist: back, add-time, skip-lead, quit
 * Keys during WARMUP:     none. Held ESC still reaches the abandon panel.
 *
 * The session lives on the server. Closing this window does not end it, and the
 * warmup deadline is the server's wall clock - not a timer in this window.
 */
(() => {
'use strict';

const $ = id => document.getElementById(id);
const RING_CIRCUMFERENCE = 2 * Math.PI * 52;

const S = {
  cfg: null,
  session: null,
  device: null,
  call: null,
  parentSid: null,
  connectedAt: null,
  dialStartedAt: null,
  pollTimer: null,
  tickTimer: null,
  breatherEndsAt: null,
  breatherTotal: 15,
  warmupEndsAt: null,
  warmupTotal: 300,
  warmupEnding: false,
  pauseEndsAt: null,
  audioUnlocked: false,
  busy: false,
  escHeldTimer: null,
  typingHoldTimer: null,
  deadlineTimer: null,
  dateTouched: false,
  ending: false,
};

/* Fire exactly at a deadline instead of waiting for the next rAF or the 1s
   watchdog to notice it passed - that slop was a measured second between the
   warmup expiring and the first dial going out. The deadline is read fresh at
   fire time because typing extends the breather: a timer armed at breather
   start would otherwise fire under his fingers and commit early. Re-arms
   itself at the new deadline instead. The rAF ticks and the watchdog stay -
   this is the precise path, they are the backstop. */
function armDeadline(getEndsAt, fire) {
  clearTimeout(S.deadlineTimer);
  const at = getEndsAt();
  if (!at) return;
  S.deadlineTimer = setTimeout(() => {
    const fresh = getEndsAt();
    if (!fresh) return;                      // state moved on; nothing to fire
    if (Date.now() >= fresh - 20) fire();
    else armDeadline(getEndsAt, fire);       // typing pushed the deadline out
  }, Math.max(0, at - Date.now()) + 5);
}

// --- transport ---------------------------------------------------------------

/* Every request gets a deadline, and no request ever throws.

   Neither was true before. fetch() has no default timeout, so a request that
   went out and never came back left the loop parked forever on an await, with
   a countdown that had already stopped - indistinguishable, on screen, from
   the machine waiting for him. And a rejected fetch (wifi dropped) propagated
   out of callers like commitAndDial() that have no catch, killing the loop as
   an unhandled rejection.

   So: abort at the deadline, and hand every failure back as data using the
   same __httpError shape callers already understand. A failed request becomes
   a retry, never a stall. */
const API_TIMEOUT_MS = 20000;

async function api(path, opts = {}) {
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), opts.timeoutMs || API_TIMEOUT_MS);
  try {
    const r = await fetch(path, {
      headers: {'Content-Type': 'application/json'},
      ...opts,
      signal: ctl.signal,
      body: opts.body ? JSON.stringify(opts.body) : undefined,
      method: opts.method || (opts.body ? 'POST' : 'GET'),
    });
    let data = {};
    try { data = await r.json(); } catch (_) {}
    if (!r.ok) data.__httpError = r.status;
    return data;
  } catch (e) {
    const why = e && e.name === 'AbortError' ? 'timed out' : String(e && e.message || e);
    clientLog('warn', 'request failed', {path, why});
    return {__httpError: 0, __netError: why, error: `the server did not answer (${why})`};
  } finally {
    clearTimeout(timer);
  }
}

/* Ship a diagnostic line to the server so it lands in state/server.log. The
   browser is where every failure actually happens and the window takes the
   evidence with it when it closes - see the 2026-08-05 silent call.

   Fire-and-forget on purpose: not awaited, errors swallowed, keepalive set so a
   line written as the page tears down still leaves. Logging may never cost a
   dial, and it may never call banner() - that would recurse. */
function clientLog(level, msg, detail) {
  try {
    fetch('/api/client-log', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({level, msg: String(msg), detail: detail || null}),
      keepalive: true,
    }).catch(() => {});
  } catch (_) { /* never */ }
}

function banner(msg, kind = '') {
  const b = $('banner');
  if (!msg) { b.hidden = true; return; }
  b.textContent = msg;
  b.className = 'banner ' + kind;
  b.hidden = false;
  // One banner element means the second message silently overwrites the first,
  // which is exactly how "it showed a couple of things" became unanswerable.
  // Every message that ever reaches the screen gets a line, in order.
  clientLog(kind === 'warn' ? 'warn' : 'error', 'banner: ' + msg);
}

function writeStatus(msg, kind = '') {
  const el = $('write-status');
  el.textContent = msg || '';
  el.className = 'write-status ' + kind;
}

// --- screens -----------------------------------------------------------------

function show(name) {
  for (const s of ['idle', 'warmup', 'live', 'paused', 'tally']) {
    $('screen-' + s).hidden = (s !== name);
  }
}

function fmtClock(sec) {
  sec = Math.max(0, Math.ceil(sec));
  return `${Math.floor(sec / 60)}:${String(sec % 60).padStart(2, '0')}`;
}

// --- boot --------------------------------------------------------------------

async function boot() {
  S.cfg = await api('/api/config');

  if (S.cfg.dry_run) banner('DRY RUN — simulated calls, no Twilio spend', 'warn');
  else if (!S.cfg.armed) banner('DIALER_ARM_WRITE=0 — nothing will be written to Airtable', 'warn');

  // A write the retry queue gave up on means a call he actually made is not in
  // Airtable. That has to be on the screen, not only in a log line.
  if (S.cfg.dropped_writes) {
    banner(`${S.cfg.dropped_writes} call outcome(s) never reached Airtable — `
         + 'see state/dropped-writes.jsonl', 'warn');
  }

  if (S.cfg.last_abandon) {
    const a = S.cfg.last_abandon;
    const el = $('abandon-banner');
    el.textContent = `last session: abandoned at ${a.completed} of ${a.target}`;
    el.hidden = false;
  }

  buildDispositionRow();
  buildPilePicker();
  buildPlaybook();
  wireKeys();
  // requestAnimationFrame throttles hard if the window ever loses foreground.
  // The countdown must fire regardless — a stalled timer is a brake.
  setInterval(() => {
    if (S.breatherEndsAt && Date.now() >= S.breatherEndsAt) commitAndDial();
    if (S.pauseEndsAt && Date.now() >= S.pauseEndsAt) resumePause();
    if (S.warmupEndsAt && Date.now() >= S.warmupEndsAt) endWarmup();
  }, 1000);
  wireIdle();
  wireBreather();
  wireAbandon();
  wireTally();

  window.addEventListener('beforeunload', e => {
    if (S.session && S.session.active) { e.preventDefault(); e.returnValue = ''; }
  });

  // Anything that escapes a handler kills the loop silently - the countdown just
  // stops and the window looks like it is waiting on him.
  window.addEventListener('error', ev => clientLog('error', 'uncaught', {
    message: ev.message, at: `${ev.filename}:${ev.lineno}`,
  }));
  window.addEventListener('unhandledrejection', ev => clientLog('error', 'unhandled rejection', {
    reason: String(ev.reason && (ev.reason.message || ev.reason)),
  }));
  // Chrome throttles timers hard once this window is hidden, and the session
  // stalls mid-breather. Timestamped either side, the log shows that directly.
  document.addEventListener('visibilitychange',
    () => clientLog('info', 'window ' + document.visibilityState));

  S.session = await api('/api/session');
  if (S.session.active) {
    // Resume. No Start button, no fresh slate. The browser still needs one
    // gesture before it will open the mic, which is the resume gate.
    show('live');
    $('resume-done').textContent = S.session.completed;
    $('resume-target').textContent = S.session.target;
    $('resume-gate').hidden = false;
    render();
  } else {
    show('idle');
    $('idle-status').textContent = S.cfg.in_call_window
      ? '' : `outside the ${S.cfg.call_window[0]}:00–${S.cfg.call_window[1]}:00 call window`;
    $('start').disabled = !S.cfg.in_call_window;
  }
}

// --- device ------------------------------------------------------------------

async function initDevice() {
  if (S.device || S.cfg.dry_run) return;
  // Timed locally, not via the dial marks: this function is meant to run during
  // the warmup once pre-registration lands, and its cost matters wherever it is.
  const t0 = performance.now();
  const t = await api('/api/token');
  const tokenMs = Math.round(performance.now() - t0);
  if (t.error) { banner('Twilio token failed: ' + t.error); throw new Error(t.error); }

  S.device = new Twilio.Device(t.token, {
    codecPreferences: ['opus', 'pcmu'],
    disableAudioContextSounds: false,
    logLevel: 'error',
    // Pinned, not 'roaming': the default runs a GeoDNS resolution before the
    // TLS + WebSocket handshake, on the dial path. This machine dials from one
    // desk in Southern California; umatilla is the US-West edge.
    edge: 'umatilla',
  });
  S.device.on('error', e => {
    console.error('[device]', e);
    clientLog('error', 'twilio device error', {
      code: e.code, message: e.message, explanation: e.explanation,
      causes: e.causes, solutions: e.solutions,
    });
    // Loud, but the queue keeps advancing. No technical problem may produce a
    // moment where stopping is easier than continuing.
    banner('Twilio: ' + (e.message || e.code));
  });
  S.device.on('tokenWillExpire', async () => {
    const fresh = await api('/api/token');
    if (fresh.token) S.device.updateToken(fresh.token);
  });
  await S.device.register();

  // Which speaker the SDK actually chose. Chrome snapshots its device list at
  // launch, so headphones connected afterwards can leave "default" pointing at
  // hardware that is gone - silent ringback, and nothing on screen says so.
  try {
    const a = S.device.audio;
    const labels = c => [...(c?.values?.() || c || [])].map(d => d.label || d.deviceId);
    clientLog('info', 'device registered', {
      tokenMs, registerMs: Math.round(performance.now() - t0) - tokenMs,
      inputs: labels(a?.availableInputDevices),
      outputs: labels(a?.availableOutputDevices),
      speaker: labels(a?.speakerDevices?.get()),
    });
  } catch (_) { /* diagnostics never block a dial */ }
}

/* Pre-pay the dial's setup while the clock is running on something else.
   Measured 2026-08-05: token + Device + register() cost ~3-4s and ran lazily
   inside the first dial - after 300 seconds of warmup in which nothing
   happened. Called (never awaited) from the warmup, every breather, and the
   resume gate. Failure is safe by design: dialNext() still awaits initDevice()
   itself, so a dead prewarm costs exactly what today costs, and nothing here
   may surface an error that reads like a reason to stop. */
function prewarmDevice(where) {
  if (S.cfg.dry_run) return;
  const p = (S.device && S.device.state === 'unregistered')
    ? S.device.register()   // socket dropped during a long breather; re-arm it
    : initDevice();
  Promise.resolve(p).catch(e => {
    clientLog('warn', 'prewarm failed - the dial will pay setup itself', {
      where, message: e && e.message,
    });
  });
}

async function unlockAudio() {
  if (S.audioUnlocked) return;
  try {
    const stream = await navigator.mediaDevices.getUserMedia({audio: true});
    // Read the label before stopping the track - a stopped track reports none.
    clientLog('info', 'mic opened', {device: stream.getAudioTracks()[0]?.label || '?'});
    stream.getTracks().forEach(t => t.stop());
    S.audioUnlocked = true;
  } catch (e) {
    // NotAllowedError is a permission problem, NotFoundError is no input device
    // at all, OverconstrainedError is a device that vanished. Three different
    // fixes, and the banner cannot tell him which one he has.
    clientLog('error', 'getUserMedia failed', {name: e.name, message: e.message});
    banner('Microphone blocked — the queue keeps moving but he cannot be heard');
  }
}

// --- local ringback ----------------------------------------------------------

/* The carrier takes ~3s to return real ringback after the leg is placed, and
   nothing on this end can shorten it - measured 2026-08-05 on a fully warm
   dial. This fills that silence with a locally generated US ringback (440+480Hz
   sine pair, 2s on / 4s off) the instant the dial commits, and gets out of the
   way the moment real audio arrives (early media, or the bridge on accept).
   Slightly quieter than carrier ringback so the handoff reads as the real
   thing arriving, not a glitch.

   Plays through the system default output, not the SDK's selected speaker -
   the same headphones in every real session. Every entry point is wrapped:
   a missing tone must never cost a dial. */
const RINGBACK_GAIN = 0.12;
const RB = {ctx: null, nodes: null};

function startLocalRingback() {
  if (S.cfg.dry_run) return;
  try {
    stopLocalRingback();
    if (!RB.ctx) RB.ctx = new (window.AudioContext || window.webkitAudioContext)();
    if (RB.ctx.state === 'suspended') RB.ctx.resume();
    const ctx = RB.ctx;
    const gain = ctx.createGain();
    gain.gain.value = 0;
    gain.connect(ctx.destination);
    const oscs = [440, 480].map(hz => {
      const o = ctx.createOscillator();
      o.type = 'sine';
      o.frequency.value = hz;
      o.connect(gain);
      o.start();
      return o;
    });
    // First burst begins ~50ms out - the point is sound at zero, so the cadence
    // starts on the ring, not the silence. Ten cycles outlast the 22s dial
    // timeout with room to spare; stop() is what actually ends it.
    const t0 = ctx.currentTime + 0.05;
    for (let i = 0; i < 10; i++) {
      const on = t0 + i * 6;
      gain.gain.setValueAtTime(0, on);
      gain.gain.linearRampToValueAtTime(RINGBACK_GAIN, on + 0.02);
      gain.gain.setValueAtTime(RINGBACK_GAIN, on + 2);
      gain.gain.linearRampToValueAtTime(0, on + 2.02);
    }
    RB.nodes = {gain, oscs};
  } catch (e) {
    clientLog('warn', 'local ringback failed to start', {message: e && e.message});
  }
}

function stopLocalRingback() {
  if (!RB.nodes) return;
  try {
    const {gain, oscs} = RB.nodes;
    const now = RB.ctx.currentTime;
    // 50ms fade instead of a hard cut - a click at the handoff would announce
    // the seam the tone exists to hide.
    gain.gain.cancelScheduledValues(now);
    gain.gain.setValueAtTime(gain.gain.value, now);
    gain.gain.linearRampToValueAtTime(0, now + 0.05);
    oscs.forEach(o => { try { o.stop(now + 0.08); } catch (_) { /* already stopped */ } });
  } catch (_) { /* teardown never throws */ }
  RB.nodes = null;
}

// --- phase timing ------------------------------------------------------------

/* One dial, one line. The server log's second-granularity timestamps are what
   forced the 8s-vs-4s breakdown to be reconstructed by hand; these marks make
   the go→audible spread readable off a single 'dial timing' entry. Milliseconds
   from the moment dialNext() commits to a dial. Flushed at the first audible
   moment (early media, or accept when the carrier sent none), and at call end
   as the fallback so a failed dial still reports what it paid. */
const T = {marks: null, flushed: false};

function timingStart() {
  T.marks = {t0: performance.now()};
  T.flushed = false;
}

function timingMark(name) {
  if (T.marks && T.marks[name] === undefined) {
    T.marks[name] = Math.round(performance.now() - T.marks.t0);
  }
}

function timingFlush(trigger) {
  if (!T.marks || T.flushed) return;
  T.flushed = true;
  const {t0, ...phases} = T.marks;
  clientLog('info', 'dial timing', {trigger, ...phases});
}

// --- the loop ----------------------------------------------------------------

async function dialNext() {
  if (S.busy) return;
  // A call in progress outranks anything the loop wants. S.busy only covers the
  // moment of dialling - it clears as soon as connect() resolves, so it is false
  // for the entire conversation. The SDK would refuse the second connect anyway,
  // but only after this function had spent a dial and overwritten parentSid and
  // connectedAt, which is what filed an answered call as a No Answer.
  if (S.call) { clientLog('warn', 'dial suppressed - a call is already live'); return; }
  S.busy = true;
  // Reopen the teardown latch here, not after /api/dial answers. A dial that
  // throws still has to be able to end - otherwise a network blip on the request
  // leaves the latch shut from the previous call and the loop never breathes
  // again. The guard above already proved no call is live.
  S.ending = false;
  timingStart();
  try {
    stopBreather();
    const r = await api('/api/dial', {body: {}});
    timingMark('api_dial');
    if (r.error && !r.skipped) {
      banner(r.error);
      // 9pm is a legal wall, not a transient refusal - it never clears today,
      // so retrying every 5s forever just flashes a banner at him until he
      // hold-ESCs out. Close the session out properly instead.
      if (r.error === 'outside_call_window' || r.__httpError === 403) {
        stopBreather();
        S.busy = false;
        banner('past the 9:00pm cutoff — stopping here', 'warn');
        showTally();
        return;
      }
      // A refusal is not a stop. Wait a beat and take the next number.
      setTimeout(() => { S.busy = false; dialNext(); }, 5000);
      return;
    }
    if (r.skipped) {
      // DNC or unusable number. Zero seconds, straight to the next one.
      S.session = r.session;
      writeStatus('skipped — ' + (r.error || 'refused'), 'warn');
      S.busy = false;
      return dialNext();
    }

    S.session = r.session;
    S.connectedAt = null;
    S.parentSid = null;
    S.dialStartedAt = Date.now();
    render();
    setDialState('ringing…', 'ringing');
    startCallTimer();
    timingMark('timer');
    // Sound in his ear the moment the timer starts. The screen and the
    // headphones said different things for ~6 measured seconds; now the label
    // above is true the instant it renders.
    startLocalRingback();

    if (S.cfg.dry_run) {
      simulateCall();
    } else {
      // The SDK is a vendored file loaded by a plain <script> tag with no
      // onerror. If it 404s or fails to execute, `Twilio` is simply undefined
      // and `new Twilio.Device()` throws a bare ReferenceError - which used to
      // land in the catch below and be filed as a No Answer. Name it here so
      // the failure reads as what it is.
      if (typeof Twilio === 'undefined' || !Twilio.Device) {
        throw new Error('the Twilio voice SDK did not load — no call can be placed');
      }
      await initDevice();
      timingMark('device');
      await unlockAudio();
      timingMark('audio');
      S.call = await S.device.connect({params: {To: r.phone}});
      timingMark('connect');
      wireCall(S.call);
    }
  } catch (e) {
    console.error(e);
    clientLog('error', 'dial threw', {name: e.name, code: e.code, message: e.message});
    // The tone must not ring for the 5s the teardown below waits - a dial that
    // failed has nothing to sound like it is doing.
    stopLocalRingback();
    // If a call is up, the attempt is the thing that was wrong - not the call.
    // Tearing down here is what hung up on a receptionist mid-sentence: the
    // teardown fires 5s later and disconnects whatever is on the line.
    if (S.call) { S.busy = false; return; }

    /* Reaching here with no S.call means device.connect() never returned a
       call: nothing was dialled, nobody's phone rang. This used to run
       endCall('error'), which derives its disposition from connectedSecs
       alone - zero - and so wrote "No Answer", Attempts +1, Last Call Date =
       today to a lead that was never called. With a broken SDK that repeats
       for every lead in the queue, and at four of them outcomes.py retires the
       row permanently. A whole morning's list closed out by a missing file.

       So: no outcome, no advance, no attempt spent. Hold this lead, say so,
       and come back to the same number. */
    S.setupFails = (S.setupFails || 0) + 1;
    setDialState('could not place the call', 'failed');
    banner(S.setupFails >= 3
      ? 'Cannot place calls: ' + (e.message || e) + ' — nothing is being written to Airtable'
      : 'Could not place the call: ' + (e.message || e) + ' — retrying this number');
    const wait = S.setupFails >= 3 ? 30000 : 8000;
    setTimeout(() => { S.busy = false; dialNext(); }, wait);
    return;
  }
  S.setupFails = 0;
  S.busy = false;
}

function wireCall(call) {
  call.on('accept', () => {
    // CallSid must be read INSIDE the accept handler, not after connect()
    // resolves. answerOnBridge means this fires when the leg is bridged.
    S.parentSid = call.parameters.CallSid;
    clientLog('info', 'call accepted', {sid: S.parentSid});
    // Bridged means audio for certain, even when the carrier never sent early
    // media - so this is the flush of last resort for the audible mark, and
    // the local tone must be gone before a human hears it.
    stopLocalRingback();
    timingMark('accept');
    timingFlush('accept');
    api('/api/call-sid', {body: {call_sid: S.parentSid}});
    startPolling();
  });
  // The one event that proves media reached this end. answerOnBridge sends real
  // ringback down the same path as speech, so no 'ringing' line means he heard
  // silence - which is a media failure, not a lead who did not pick up.
  call.on('ringing', hasEarlyMedia => {
    clientLog('info', 'ringback', {hasEarlyMedia});
    timingMark(hasEarlyMedia ? 'audible' : 'ring_silent');
    // Real carrier ringback has arrived; the local tone hands off and is gone.
    if (hasEarlyMedia) { stopLocalRingback(); timingFlush('early-media'); }
  });
  call.on('disconnect', () => { clientLog('info', 'call disconnect'); endCall('remote'); });
  call.on('cancel', () => { clientLog('info', 'call cancel'); endCall('remote'); });
  call.on('error', e => {
    console.error('[call]', e);
    clientLog('error', 'twilio call error', {
      code: e.code, message: e.message, explanation: e.explanation, causes: e.causes,
    });
    endCall('error');
  });
}

function startPolling() {
  stopPolling();
  S.polling = false;
  S.pollTimer = setInterval(async () => {
    if (!S.parentSid) return;
    // setInterval does not wait for the previous tick. A slow /api/call-status
    // meant a new overlapping request every 1.2s for as long as the stall
    // lasted, each one a fresh Flask thread reaching for the same Twilio
    // client the SDK does not promise is thread-safe.
    if (S.polling) return;
    S.polling = true;
    let st;
    try {
      st = await api('/api/call-status?parent_sid=' + encodeURIComponent(S.parentSid));
    } finally {
      S.polling = false;
    }
    // A failed poll is not a finished call. Saying otherwise would auto-advance
    // off a call that is still up.
    if (!st || st.__netError || st.__httpError) return;
    if (st.connected && !S.connectedAt) {
      S.connectedAt = Date.now();
      setDialState('connected', 'connected');
      $('call-timer').classList.add('live');
    }
    if (st.finished) {
      // Only auto-advance on calls that genuinely never connected. A connected
      // call waits for SPACE or ENTER — he is the classifier, not AMD.
      if (!S.connectedAt) endCall('no-answer');
      else endCall('remote');
    }
  }, 1200);
}
function stopPolling() { if (S.pollTimer) clearInterval(S.pollTimer); S.pollTimer = null; }

/* Dry run: exercise every branch without Twilio. Deterministic-ish mix so a
   verification pass sees ring-outs, short connects and long connects. */
function simulateCall() {
  const roll = Math.random();
  const ringMs = 3000 + Math.random() * 5000;
  setTimeout(() => {
    if (!S.session || S.session.state !== 'DIALING') return;
    if (roll < 0.55) { endCall('no-answer'); return; }          // ring-out
    S.connectedAt = Date.now();
    setDialState('connected (simulated)', 'connected');
    $('call-timer').classList.add('live');
    if (roll < 0.8) setTimeout(() => endCall('remote'), 4000);  // short, machine-ish
    // else: stays connected until he presses SPACE or ENTER
  }, ringMs);
}

async function endCall(reason) {
  // One call ends once. Twilio fires 'error' and 'disconnect' for the same call
  // and both used to run a full teardown - two /api/breather/start requests, two
  // countdown loops racing one deadline, and four commits stacked on the next
  // expiry. Cleared when the next dial goes out.
  if (S.ending) return;
  S.ending = true;
  stopLocalRingback();
  // A dial that never reached audio still reports what it paid and where.
  timingFlush('end:' + reason);
  stopPolling();
  stopCallTimer();
  const connectedSecs = S.connectedAt
    ? Math.max(0, (Date.now() - S.connectedAt - (S.sleepGapMs || 0)) / 1000) : 0;
  // Who ended it, and how far into the dial. A short ring that reads 'error' or
  // 'remote' rather than a keypress is this end hanging up on itself.
  clientLog('info', 'call ended', {
    reason,
    connectedSecs: Math.round(connectedSecs),
    ringSecs: S.dialStartedAt ? Math.round((Date.now() - S.dialStartedAt) / 1000) : null,
  });

  if (S.call && !S.cfg.dry_run) {
    try { S.call.disconnect(); } catch (_) {}
    if (S.parentSid) api('/api/hangup', {body: {call_sid: S.parentSid}});
  }
  S.call = null;

  let disposition, breather;
  if (reason === 'space') {
    disposition = 'No Answer'; breather = S.cfg.breather.dead_end;
  } else if (reason === 'enter') {
    disposition = 'Conversation'; breather = S.cfg.breather.real;
  } else if (connectedSecs <= 0) {
    disposition = 'No Answer'; breather = S.cfg.breather.dead_end;   // zero keys
  } else if (connectedSecs >= 15) {
    disposition = 'Conversation'; breather = S.cfg.breather.real;
  } else {
    disposition = 'No Answer'; breather = S.cfg.breather.dead_end;
  }

  await openBreather(disposition, connectedSecs > 0, breather);
}

/* Parking the outcome is the one request the loop cannot skip - it is what
   makes the next breather exist. If it failed, S.session was overwritten with
   the error object, render() bailed on the missing `active`, and startBreather
   armed a deadline off `undefined`: the countdown never ran, commitAndDial
   refused a session that was not in BREATHER, and the loop simply stopped with
   a screen that looked like it was waiting for him. Keep the old session and
   keep asking instead. */
async function openBreather(disposition, connected, breather, tries = 0) {
  const b = await api('/api/breather/start', {body: {disposition, connected, breather}});
  if (b && b.active) {
    S.session = b;
    S.dateTouched = false;
    startBreather();
    render();
    return;
  }
  clientLog('error', 'breather/start failed', {
    status: b && b.__httpError, why: b && b.__netError, tries,
  });
  banner('lost the server for a moment — retrying', 'warn');
  setTimeout(() => openBreather(disposition, connected, breather, tries + 1),
             Math.min(2000 * (tries + 1), 10000));
}

// --- warmup ------------------------------------------------------------------

/* The lock-in between START and the first dial. No key shortens it — the server
   refuses /api/dial until its own clock says the window is over, so this screen
   is a display of that fact, not the thing enforcing it.

   Five minutes of a bare countdown is five minutes to talk yourself out of it, so
   the screen carries the top of the queue: read the cues, look up the owner names
   that are missing, and arrive at the first dial already warm. */
function startWarmup() {
  show('warmup');
  // Five minutes of free time starts now; the first dial's setup happens in it.
  prewarmDevice('warmup');
  S.warmupTotal = S.session.warmup_seconds || S.cfg.warmup || 300;
  S.warmupEndsAt = Date.now() + (S.session.warmup_remaining || 0) * 1000;
  S.warmupEnding = false;
  $('warmup-target').textContent = S.session.target;
  renderWarmupLeads();
  armDeadline(() => S.warmupEndsAt, endWarmup);
  tickWarmup();
}

function tickWarmup() {
  if (!S.warmupEndsAt || !S.session || S.session.state !== 'WARMUP') return;
  const left = (S.warmupEndsAt - Date.now()) / 1000;
  const el = $('warmup-clock');
  el.textContent = fmtClock(left);
  el.classList.toggle('warn', left <= 30);
  if (left <= 0) { endWarmup(); return; }
  requestAnimationFrame(tickWarmup);
}

async function endWarmup() {
  if (S.warmupEnding) return;
  S.warmupEnding = true;
  const r = await api('/api/warmup/done', {body: {}});
  if (r.__httpError === 425) {
    // This window's clock ran ahead of the server's. Re-sync and keep counting;
    // the server is the authority on when the lock-in is over.
    S.warmupEndsAt = Date.now() + ((r.session && r.session.warmup_remaining) || 1) * 1000;
    S.warmupEnding = false;
    armDeadline(() => S.warmupEndsAt, endWarmup);
    tickWarmup();
    return;
  }
  S.warmupEndsAt = null;
  S.session = r.active ? r : S.session;
  show('live');
  render();
  dialNext();
}

function renderWarmupLeads() {
  const wrap = $('warmup-leads');
  wrap.innerHTML = '';
  for (const lead of (S.session.warmup_leads || [])) {
    const card = document.createElement('div');
    card.className = 'warm-card';

    const head = document.createElement('div');
    head.className = 'warm-head';
    head.innerHTML = `<span class="warm-company"></span>`
                   + `<span class="warm-tier">${lead.tier_label || ''}</span>`;
    head.querySelector('.warm-company').textContent = lead.company || '—';

    const meta = document.createElement('div');
    meta.className = 'warm-meta';
    meta.textContent = [
      lead.first_name || 'no name on file',
      lead.industry,
      lead.phone_display || lead.phone,
      `attempt ${(lead.attempts || 0) + 1} of 4`,
    ].filter(Boolean).join('  ·  ');

    card.append(head, meta);

    const cueText = [lead.context_cue, lead.leak_signal && `leak: ${lead.leak_signal}`]
      .filter(Boolean).join('\n');
    if (cueText) {
      const cue = document.createElement('div');
      cue.className = 'warm-cue';
      cue.textContent = cueText;
      card.append(cue);
    }

    // Same rule as the breather card: CSLB only pays when the row has no name.
    if (!lead.first_name && lead.company) {
      const a = document.createElement('a');
      a.className = 'cslb';
      a.target = '_blank';
      a.rel = 'noopener';
      a.href = 'https://www.cslb.ca.gov/OnlineServices/CheckLicenseII/NameRequest.aspx'
             + '?BusName=' + encodeURIComponent(lead.company);
      a.textContent = "look up the owner's name on CSLB →";
      card.append(a);
    }
    wrap.append(card);
  }
}

// --- breather ----------------------------------------------------------------

function startBreather() {
  // Keep the device warm between calls - a 120s conversation breather is long
  // enough for a dropped socket to put setup back on the next dial.
  prewarmDevice('breather');
  S.breatherTotal = S.session.breather_seconds || S.cfg.breather.dead_end;
  S.breatherEndsAt = Date.now() + (S.session.breather_remaining || 0) * 1000;
  $('breather').hidden = false;
  // Full angles only on the long breather. On a 15-second dead end there is no
  // objection to review - nobody picked up - and eight paragraphs on a screen he
  // has no time to read is just noise between him and the next dial.
  $('angles-card').hidden = S.breatherTotal < S.cfg.breather.real;
  const note = $('note');
  note.value = '';
  note.focus();
  armDeadline(() => S.breatherEndsAt, commitAndDial);
  tickBreather();
}

function stopBreather() {
  $('breather').hidden = true;
  S.breatherEndsAt = null;
  clearTimeout(S.deadlineTimer);
}

function tickBreather() {
  if (!S.breatherEndsAt || !S.session || S.session.state !== 'BREATHER') return;
  const left = (S.breatherEndsAt - Date.now()) / 1000;
  $('ring-label').textContent = Math.max(0, Math.ceil(left));
  const frac = Math.max(0, Math.min(1, left / Math.max(1, S.breatherTotal)));
  $('ring-fg').style.strokeDashoffset = String(RING_CIRCUMFERENCE * (1 - frac));
  if (left <= 0) { commitAndDial(); return; }
  requestAnimationFrame(tickBreather);
}

async function commitAndDial() {
  if (S.busy) return;
  // Every timer path into the loop funnels through here - the 1s watchdog, the
  // rAF tick, ENTER, the pause resume. Only a live breather may commit, so a
  // stale deadline can never become a dial while he is on the phone.
  if (!S.session || S.session.state !== 'BREATHER') return;
  S.breatherEndsAt = null;
  const r = await api('/api/outcome', {body: {}});

  /* A failed commit must never become a dial. The server still has this lead
     as current and the cursor un-advanced, so falling through to dialNext()
     here called the person he had just finished talking to a second time,
     seconds later, with the real outcome of that call lost. Retry the commit;
     do not move on until it lands. */
  if (r.__httpError || r.__netError) {
    clientLog('error', 'commit failed', {status: r.__httpError, why: r.__netError});
    writeStatus('could not save that outcome — retrying', 'warn');
    S.breatherEndsAt = Date.now() + 4000;
    armDeadline(() => S.breatherEndsAt, commitAndDial);
    return;
  }

  if (r.write) {
    if (r.write.queued) writeStatus('write queued for retry', 'warn');
    else if (!r.write.armed) writeStatus('not armed — nothing written');
    else writeStatus('saved');
  }
  S.session = r.session || S.session;
  render();
  if (S.session.state === 'TALLY') { showTally(); return; }
  // A pause asked for mid-call lands here, once the call it interrupted has
  // been logged. The server owns the transition; this just follows it.
  if (S.session.state === 'PAUSED') {
    stopBreather();
    S.pauseEndsAt = Date.now() + (S.session.pause_remaining || 0) * 1000;
    show('paused');
    tickPause();
    return;
  }
  dialNext();
}

/* Typing holds the timer. It only extends while words are being produced, so it
   cannot be used to stall. */
async function holdForTyping() {
  if (!S.session || S.session.state !== 'BREATHER') return;
  $('ring-wrap-holder').classList.add('held');
  clearTimeout(S.typingHoldTimer);
  S.typingHoldTimer = setTimeout(() => {
    $('ring-wrap-holder').classList.remove('held');
  }, S.cfg.breather.typing_resume_after * 1000);

  const r = await api('/api/breather/hold', {body: {}});
  // The check at the top of this function is worthless by the time the response
  // lands: he commits, the next number goes out, and this resolves onto a
  // session that is already ringing. So re-check here - and treat a null
  // deadline as "the breather is over", not as zero. `target > null` is true for
  // every timestamp, which is exactly how a dead countdown came back to life and
  // dialed on top of a live call on 2026-08-05.
  if (!r.ok || !S.session || S.session.state !== 'BREATHER' || !S.breatherEndsAt) return;
  const target = Date.now() + r.breather_remaining * 1000;
  if (target > S.breatherEndsAt) {
    S.breatherEndsAt = target;
    S.breatherTotal = Math.max(S.breatherTotal, r.breather_remaining);
  }
}

function buildDispositionRow() {
  const row = $('dispositions');
  row.innerHTML = '';
  // Keys come from the server, which derives them from the live Airtable schema.
  // Never hard-code the list here — that is how the UI and the schema drift.
  for (const [key, label] of Object.entries(S.cfg.dispositions)) {
    const b = document.createElement('button');
    b.className = 'disp';
    b.dataset.disposition = label;
    b.innerHTML = `<kbd>${key}</kbd>${label}`;
    b.addEventListener('click', () => setDisposition(label));
    row.appendChild(b);
  }
  const d = document.createElement('button');
  d.className = 'disp dnc';
  d.dataset.dnc = '1';
  d.innerHTML = `<kbd>D</kbd>do not call`;
  d.addEventListener('click', toggleDnc);
  row.appendChild(d);
}

/* Built from the server, which owns the pile definitions. Never hard-code the
   options here — that is how the picker and the queue drift apart. */
function buildPilePicker() {
  const sel = $('pile');
  sel.innerHTML = '';
  for (const p of (S.cfg.piles || [])) {
    const o = document.createElement('option');
    o.value = p.value;
    o.textContent = p.label;
    o.selected = p.value === S.cfg.default_pile;
    sel.appendChild(o);
  }
  // Spell out the actual tier chain. The option label has to stay short enough
  // not to truncate, and "which list am I about to call" is worth being explicit
  // about before committing to 20 of them with no way to stop.
  const hint = () => {
    const p = (S.cfg.piles || []).find(x => x.value === sel.value);
    $('pile-hint').textContent = p ? p.tiers.join('  →  ') : '';
  };
  sel.addEventListener('change', hint);
  hint();
}

/* --- talk track ---------------------------------------------------------- */

/* All three panels are static content, so they are built once at boot rather
   than on every render(). They must never throw: playbook.js failing to load
   has to cost him three panels, not the dialer. */
function buildPlaybook() {
  const P = window.PLAYBOOK;
  if (!P) { console.warn('[playbook] not loaded — talk track panels are off'); return; }

  const beats = $('warmup-beats');
  for (const [name, body] of (P.beats || [])) {
    const li = document.createElement('li');
    li.innerHTML = '<b></b> ';
    li.querySelector('b').textContent = name;
    li.append(document.createTextNode(body));
    beats.append(li);
  }
  const rules = $('warmup-rules');
  for (const r of (P.rules || [])) {
    const li = document.createElement('li');
    li.textContent = r;
    rules.append(li);
  }

  const rail = $('rail-list');
  for (const [trigger, angle] of (P.rail || [])) {
    const li = document.createElement('li');
    const t = document.createElement('span');
    t.className = 'rail-trigger';
    t.textContent = trigger;
    const a = document.createElement('span');
    a.className = 'rail-angle';
    a.textContent = angle;
    li.append(t, a);
    rail.append(li);
  }

  const angles = $('angles-list');
  for (const [q, a] of (P.angles || [])) {
    const wrap = document.createElement('div');
    const qq = document.createElement('div');
    qq.className = 'angles-q';
    qq.textContent = q;
    const aa = document.createElement('div');
    aa.className = 'angles-a';
    aa.textContent = a;
    wrap.append(qq, aa);
    angles.append(wrap);
  }
}

/* Loose match for "these two say the same thing". The promise note is usually a
   hand-retyped restatement of the log line it came from, so exact comparison
   never fires: "call back monday 8/3" vs "call back on monday (8/3)". Punctuation
   and a few connectives are the entire difference, so both go. */
const NOISE_WORDS = /^(on|at|in|of|to|for|the|a|an|and|his|her|their|they)$/;
function normalizeNote(s) {
  return (s || '').toLowerCase().replace(/[^a-z0-9\s]/g, ' ')
    .split(/\s+/).filter(w => w && !NOISE_WORDS.test(w)).join(' ');
}

/* Callback rows only. `next_action_note` is the promise he made; `notes` is the
   running log the promise came out of. Both already ride in the queue payload,
   so this costs no extra fetch - they were being served and thrown away. */
function renderCallbackNotes(lead) {
  const box = $('callback-notes');
  const isCallback = lead.tier === 'callback_due';
  const promise = (lead.next_action_note || '').trim();
  const log = (lead.notes || '').trim();

  if (!isCallback || (!promise && !log)) { box.hidden = true; return; }

  // Falls back to the log when the promise line is empty, so the panel is never
  // an empty amber box on a row that plainly has history.
  $('callback-promise').textContent = promise || log;

  // The log earns its space only by saying something the promise line does not.
  // Directional on purpose: a log that CONTAINS the promise is longer and richer,
  // so it stays; only a log that is a restatement or a subset is dropped.
  const nLog = normalizeNote(log);
  const nPromise = normalizeNote(promise ? promise : log);
  const redundant = !nLog || nLog === nPromise || nPromise.includes(nLog);
  $('callback-log').textContent = redundant ? '' : log;

  box.hidden = false;
}

async function setDisposition(label) {
  if (!S.session || !S.session.pending_outcome) return;
  const r = await api('/api/breather/update', {body: {disposition: label}});
  if (r.pending) {
    S.session.pending_outcome = r.pending;
    S.dateTouched = false;
    renderPending();
  }
}

async function toggleDnc() {
  if (!S.session || !S.session.pending_outcome) return;
  const now = !S.session.pending_outcome.dnc;
  const r = await api('/api/breather/update', {body: {dnc: now}});
  if (r.pending) { S.session.pending_outcome = r.pending; renderPending(); }
}

function renderPending() {
  const p = S.session && S.session.pending_outcome;
  document.querySelectorAll('.disp').forEach(el => {
    if (el.dataset.dnc) el.classList.toggle('active', !!(p && p.dnc));
    else el.classList.toggle('active', !!(p && el.dataset.disposition === p.disposition));
  });
  const wrap = $('next-action-wrap');
  if (p && p.next_action) {
    wrap.hidden = false;
    if (!S.dateTouched) $('next-action').value = p.next_action;
  } else {
    wrap.hidden = true;
  }
}

// --- render ------------------------------------------------------------------

function setDialState(text, cls = '') {
  const el = $('dial-state');
  el.textContent = text;
  el.className = 'dial-state ' + cls;
}

function startCallTimer() {
  stopCallTimer();
  $('call-timer').classList.remove('live');
  S.sleepGapMs = 0;
  S.lastTick = Date.now();
  S.tickTimer = setInterval(() => {
    // The Mac sleeping mid-call (lid closed, display sleep escalating) used to
    // count as talk time: connectedSecs is raw wall clock, and >= 15s files the
    // call as a Conversation with a 7-day follow-up. Forty minutes asleep after
    // a two-second hello became a conversation that never happened, written to
    // a real lead's row. A 250ms interval that skipped seconds is a gap, not
    // talking, so measure it and take it back out.
    const now = Date.now();
    const drift = now - S.lastTick;
    if (drift > 5000) S.sleepGapMs += drift;
    S.lastTick = now;
    const s = (now - S.dialStartedAt - S.sleepGapMs) / 1000;
    $('call-timer').textContent = fmtClock(s);
  }, 250);
}
function stopCallTimer() { if (S.tickTimer) clearInterval(S.tickTimer); S.tickTimer = null; }

function render() {
  const s = S.session;
  if (!s || !s.active) return;
  $('count-done').textContent = s.completed;
  $('count-target').textContent = s.target;

  const lead = s.lead;
  if (lead) {
    $('lead-company').textContent = lead.company;
    $('lead-name').textContent = lead.first_name || '';
    $('lead-industry').textContent = lead.industry || '';
    $('lead-phone').textContent = lead.phone_display || lead.phone;
    $('tier-badge').textContent = lead.tier_label || '';
    const cue = $('lead-cue');
    const cueText = [lead.context_cue, lead.leak_signal && `leak: ${lead.leak_signal}`]
      .filter(Boolean).join('\n');
    cue.textContent = cueText;
    cue.hidden = !cueText;

    // The playbook says no voicemail on attempts 1-2 and a brief one on attempt 3.
    // Unfollowable if he cannot see where he is. Also shows one dial from retirement.
    const att = (lead.attempts || 0) + 1;
    const badge = $('attempt-badge');
    badge.textContent = `attempt ${att} of 4`;
    badge.classList.toggle('last', att >= 4);

    renderCallbackNotes(lead);
  }

  // The rail is for the seconds he is actually on the line. During the breather
  // this column belongs to the up-next card, so they are strict opposites.
  const dialing = s.state === 'DIALING';
  $('objection-rail').hidden = !dialing;
  $('dial-row').classList.toggle('norail', !dialing);

  const nxt = s.next_lead;
  if (nxt) {
    $('next-card').hidden = false;
    $('next-company').textContent = nxt.company;
    $('next-meta').textContent =
      [nxt.first_name || 'no name', nxt.industry, nxt.phone_display || nxt.phone]
        .filter(Boolean).join('  ·  ');
    $('next-cue').textContent = nxt.context_cue || '';
    // CSLB is the playbook's highest-yield name source for CA trades. Only
    // surface it when the row has no first name — that is when it pays.
    const link = $('cslb-link');
    if (!nxt.first_name && nxt.company) {
      link.href = 'https://www.cslb.ca.gov/OnlineServices/CheckLicenseII/NameRequest.aspx'
                + '?BusName=' + encodeURIComponent(nxt.company);
      link.hidden = false;
    } else {
      link.hidden = true;
    }
  } else {
    $('next-card').hidden = true;
  }

  renderPending();
}

function showTally() {
  stopBreather();
  const s = S.session;
  // Why it ended. "20 of 20" and "the list ran out at 11 of 20" were the same
  // screen, and only one of them means he is finished.
  if (s.queue_exhausted && s.completed < s.target) {
    banner(`the list ran dry at ${s.completed} of ${s.target} — widen the pile or the industry filter`, 'warn');
  } else if (s.past_cutoff) {
    banner('stopped at the 9:00pm legal cutoff', 'warn');
  }
  $('t-dials').textContent = s.dials;
  $('t-connects').textContent = s.connects;
  $('t-convos').textContent = s.conversations;
  $('t-booked').textContent = s.booked;
  show('tally');
}

// --- pause -------------------------------------------------------------------

async function requestPause() {
  const r = await api('/api/pause', {body: {}});
  if (r.deferred) { writeStatus('pause starts after this call', 'warn'); return; }
  if (!r.ok) { writeStatus(r.message || 'no pauses left', 'warn'); return; }
  S.session = r.session;
  S.pauseEndsAt = Date.now() + S.session.pause_remaining * 1000;
  stopBreather();
  show('paused');
  tickPause();
}

function tickPause() {
  if (!S.pauseEndsAt) return;
  const left = (S.pauseEndsAt - Date.now()) / 1000;
  const el = $('pause-clock');
  el.textContent = fmtClock(left);
  el.classList.toggle('warn', left <= 10);
  if (left <= 0) { resumePause(); return; }
  requestAnimationFrame(tickPause);
}

async function resumePause() {
  S.pauseEndsAt = null;
  const r = await api('/api/pause/resume', {body: {}});
  S.session = r.session || S.session;
  show('live');
  render();
  commitAndDial();
}

// --- keys --------------------------------------------------------------------

function wireKeys() {
  document.addEventListener('keydown', async e => {
    // The resume gate: any key is the gesture the browser needs.
    if (!$('resume-gate').hidden) {
      $('resume-gate').hidden = true;
      await unlockAudio();
      prewarmDevice('resume');   // covers every resume path in one line
      if (S.session.state === 'PAUSED') {
        S.pauseEndsAt = Date.now() + S.session.pause_remaining * 1000;
        show('paused'); tickPause();
      } else if (S.session.state === 'WARMUP') {
        // Back into whatever is left of the lock-in, never a fresh five minutes.
        startWarmup();
      } else if (S.session.state === 'TALLY') {
        showTally();
      } else {
        startBreather();
      }
      e.preventDefault();
      return;
    }

    if (!$('abandon-panel').hidden) {
      if (e.key === 'Escape') closeAbandon();
      return;   // the panel owns every other key
    }

    // Held ESC (2s) opens the abandon panel. Slow and deliberate on purpose —
    // impossible to do reflexively.
    if (e.key === 'Escape' && !e.repeat && S.session && S.session.active) {
      S.escHeldTimer = setTimeout(openAbandon, 2000);
      return;
    }

    if (!S.session || !S.session.active) return;
    const st = S.session.state;
    const typing = document.activeElement === $('note');

    if (st === 'WARMUP') {
      // No keys exist here. Not even ENTER — it is the "go now" key everywhere
      // else, which is exactly why it would get pressed reflexively and quietly
      // delete the one part of the session that is for getting his head right.
      // Held ESC is handled above and still reaches the abandon panel.
      if (e.key !== 'Escape') e.preventDefault();
      return;
    }

    if (st === 'PAUSED') {
      if (e.key === 'Enter') { e.preventDefault(); resumePause(); }
      return;
    }

    if (e.key === ' ' && !typing) {
      e.preventDefault();
      if (st === 'DIALING') endCall('space');
      return;
    }
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      if (st === 'DIALING') endCall('enter');
      else if (st === 'BREATHER') commitAndDial();
      return;
    }
    /* startBreather() focuses the note, and nothing ever blurs it - so for the
       whole of every breather `typing` was true and these three branches never
       ran. 1-5, D and P were documented as live during every breather and were
       in fact dead on the keyboard: pressing 2 typed "2" into the note, D typed
       "d", P typed "p". Only the mouse worked, in a keyboard-only app.

       Gating on an EMPTY note instead restores all three without ever eating a
       character out of a real note: the moment he has actually written
       something, the note owns its own keys again. */
    const noteEmpty = !$('note').value;
    const keysLive = !typing || noteEmpty;

    if (st === 'BREATHER' && keysLive) {
      if (S.cfg.dispositions[e.key]) {
        e.preventDefault();
        setDisposition(S.cfg.dispositions[e.key]);
        return;
      }
      if (e.key.toLowerCase() === 'd') { e.preventDefault(); toggleDnc(); return; }
    }
    if (e.key.toLowerCase() === 'p' && keysLive) {
      e.preventDefault();
      requestPause();
    }
  });

  document.addEventListener('keyup', e => {
    if (e.key === 'Escape') { clearTimeout(S.escHeldTimer); S.escHeldTimer = null; }
  });
}

// --- wiring ------------------------------------------------------------------

function wireIdle() {
  $('start').addEventListener('click', async () => {
    $('start').disabled = true;
    $('idle-status').textContent = 'building the queue…';
    await unlockAudio();                 // the one click also unlocks audio
    const r = await api('/api/session', {body: {
      target: parseInt($('target').value, 10) || 20,
      industry: $('industry').value || null,
      pile: $('pile').value || null,
    }});
    if (r.error) {
      $('idle-status').textContent = r.error;
      $('start').disabled = false;
      return;
    }
    S.session = r;
    if (S.session.state === 'WARMUP') { startWarmup(); return; }
    show('live');
    render();
    dialNext();
  });
}

function wireBreather() {
  const note = $('note');
  note.addEventListener('input', () => {
    holdForTyping();
    clearTimeout(note._save);
    note._save = setTimeout(() => {
      api('/api/breather/update', {body: {note: note.value}});
    }, 400);
  });
  note.addEventListener('keydown', e => {
    // ENTER commits from inside the note too; SHIFT+ENTER makes a newline.
    // stopPropagation because the document-level handler ALSO commits on ENTER
    // and does not check whether he is typing - one keypress was producing two
    // commits and two /api/outcome requests.
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault(); e.stopPropagation(); commitAndDial();
    }
  });

  $('next-action').addEventListener('change', e => {
    S.dateTouched = true;
    api('/api/breather/update', {body: {next_action: e.target.value}});
  });
}

function wireTally() {
  $('another').addEventListener('click', async () => {
    S.session = await api('/api/another', {body: {count: 10}});
    show('live');
    render();
    dialNext();
  });
  $('done').addEventListener('click', async () => {
    await api('/api/done', {body: {}});
    S.session = null;
    location.reload();
  });
}

// --- abandon -----------------------------------------------------------------

function openAbandon() {
  const panel = $('abandon-panel');
  if (!panel.hidden || !S.session || !S.session.active) return;
  $('abandon-target').textContent = S.session.abandon_sentence;
  const inp = $('abandon-input');
  inp.value = '';
  $('abandon-confirm').disabled = true;
  panel.hidden = false;
  inp.focus();
}

function closeAbandon() {
  $('abandon-panel').hidden = true;
  clearTimeout(S.escHeldTimer);
  if (S.session && S.session.state === 'BREATHER') $('note').focus();
}

function wireAbandon() {
  const inp = $('abandon-input');
  // Paste is blocked. It has to be typed out.
  for (const ev of ['paste', 'drop']) inp.addEventListener(ev, e => e.preventDefault());
  inp.addEventListener('contextmenu', e => e.preventDefault());
  inp.addEventListener('input', () => {
    const match = inp.value.trim() === (S.session && S.session.abandon_sentence);
    $('abandon-confirm').disabled = !match;
    inp.classList.toggle('match', match);
  });
  $('abandon-cancel').addEventListener('click', closeAbandon);
  $('abandon-confirm').addEventListener('click', async () => {
    const r = await api('/api/abandon', {body: {sentence: inp.value.trim()}});
    if (r.ok) { S.session = null; location.reload(); }
  });
}

/* Test seam. packaging/client_check.js drives these real functions through the
   timing races that killed a live call on 2026-08-05. A test that re-implements
   the guards instead of calling them is a test that passes while the bug ships.
   Absent in the browser - nothing sets this flag but the harness. */
if (typeof window !== 'undefined' && window.__NOBRAKES_TEST__) {
  window.__nb = {S, boot, holdForTyping, commitAndDial, dialNext, endCall, startBreather};
}

boot().catch(e => { console.error(e); banner('Startup failed: ' + e.message); });
})();
