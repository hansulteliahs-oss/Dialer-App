/* No Brakes - session state machine + Twilio Device client.
 *
 *   IDLE -> DIALING -> BREATHER -> (target reached?) -> TALLY
 *                         ^                |
 *                         +----------------+
 *
 * Keys that exist:        SPACE  ENTER  1-5  D  P
 * Keys that do not exist: back, add-time, skip-lead, quit
 *
 * The session lives on the server. Closing this window does not end it.
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
  pauseEndsAt: null,
  audioUnlocked: false,
  busy: false,
  escHeldTimer: null,
  typingHoldTimer: null,
  dateTouched: false,
};

// --- transport ---------------------------------------------------------------

async function api(path, opts = {}) {
  const r = await fetch(path, {
    headers: {'Content-Type': 'application/json'},
    ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
    method: opts.method || (opts.body ? 'POST' : 'GET'),
  });
  let data = {};
  try { data = await r.json(); } catch (_) {}
  if (!r.ok) data.__httpError = r.status;
  return data;
}

function banner(msg, kind = '') {
  const b = $('banner');
  if (!msg) { b.hidden = true; return; }
  b.textContent = msg;
  b.className = 'banner ' + kind;
  b.hidden = false;
}

function writeStatus(msg, kind = '') {
  const el = $('write-status');
  el.textContent = msg || '';
  el.className = 'write-status ' + kind;
}

// --- screens -----------------------------------------------------------------

function show(name) {
  for (const s of ['idle', 'live', 'paused', 'tally']) {
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

  if (S.cfg.last_abandon) {
    const a = S.cfg.last_abandon;
    const el = $('abandon-banner');
    el.textContent = `last session: abandoned at ${a.completed} of ${a.target}`;
    el.hidden = false;
  }

  buildDispositionRow();
  wireKeys();
  // requestAnimationFrame throttles hard if the window ever loses foreground.
  // The countdown must fire regardless — a stalled timer is a brake.
  setInterval(() => {
    if (S.breatherEndsAt && Date.now() >= S.breatherEndsAt) commitAndDial();
    if (S.pauseEndsAt && Date.now() >= S.pauseEndsAt) resumePause();
  }, 1000);
  wireIdle();
  wireBreather();
  wireAbandon();
  wireTally();

  window.addEventListener('beforeunload', e => {
    if (S.session && S.session.active) { e.preventDefault(); e.returnValue = ''; }
  });

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
  const t = await api('/api/token');
  if (t.error) { banner('Twilio token failed: ' + t.error); throw new Error(t.error); }

  S.device = new Twilio.Device(t.token, {
    codecPreferences: ['opus', 'pcmu'],
    disableAudioContextSounds: false,
    logLevel: 'error',
  });
  S.device.on('error', e => {
    console.error('[device]', e);
    // Loud, but the queue keeps advancing. No technical problem may produce a
    // moment where stopping is easier than continuing.
    banner('Twilio: ' + (e.message || e.code));
  });
  S.device.on('tokenWillExpire', async () => {
    const fresh = await api('/api/token');
    if (fresh.token) S.device.updateToken(fresh.token);
  });
  await S.device.register();
}

async function unlockAudio() {
  if (S.audioUnlocked) return;
  try {
    const stream = await navigator.mediaDevices.getUserMedia({audio: true});
    stream.getTracks().forEach(t => t.stop());
    S.audioUnlocked = true;
  } catch (e) {
    banner('Microphone blocked — the queue keeps moving but he cannot be heard');
  }
}

// --- the loop ----------------------------------------------------------------

async function dialNext() {
  if (S.busy) return;
  S.busy = true;
  try {
    stopBreather();
    const r = await api('/api/dial', {body: {}});
    if (r.error && !r.skipped) {
      banner(r.error);
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

    if (S.cfg.dry_run) {
      simulateCall();
    } else {
      await initDevice();
      await unlockAudio();
      S.call = await S.device.connect({params: {To: r.phone}});
      wireCall(S.call);
    }
  } catch (e) {
    console.error(e);
    setDialState('dial failed', 'failed');
    banner('Dial error: ' + (e.message || e));
    // Attempts is deliberately NOT incremented on a dial error.
    setTimeout(() => { S.busy = false; endCall('error'); }, 5000);
    return;
  }
  S.busy = false;
}

function wireCall(call) {
  call.on('accept', () => {
    // CallSid must be read INSIDE the accept handler, not after connect()
    // resolves. answerOnBridge means this fires when the leg is bridged.
    S.parentSid = call.parameters.CallSid;
    api('/api/call-sid', {body: {call_sid: S.parentSid}});
    startPolling();
  });
  call.on('disconnect', () => endCall('remote'));
  call.on('cancel', () => endCall('remote'));
  call.on('error', e => { console.error('[call]', e); endCall('error'); });
}

function startPolling() {
  stopPolling();
  S.pollTimer = setInterval(async () => {
    if (!S.parentSid) return;
    const st = await api('/api/call-status?parent_sid=' + encodeURIComponent(S.parentSid));
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
  stopPolling();
  stopCallTimer();
  const connectedSecs = S.connectedAt ? (Date.now() - S.connectedAt) / 1000 : 0;

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

  S.session = await api('/api/breather/start', {
    body: {disposition, connected: connectedSecs > 0, breather},
  });
  S.dateTouched = false;
  startBreather();
  render();
}

// --- breather ----------------------------------------------------------------

function startBreather() {
  S.breatherTotal = S.session.breather_seconds || S.cfg.breather.dead_end;
  S.breatherEndsAt = Date.now() + (S.session.breather_remaining || 0) * 1000;
  $('breather').hidden = false;
  const note = $('note');
  note.value = '';
  note.focus();
  tickBreather();
}

function stopBreather() {
  $('breather').hidden = true;
  S.breatherEndsAt = null;
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
  S.breatherEndsAt = null;
  const r = await api('/api/outcome', {body: {}});
  if (r.write) {
    if (r.write.queued) writeStatus('write queued for retry', 'warn');
    else if (!r.write.armed) writeStatus('not armed — nothing written');
    else writeStatus('saved');
  }
  S.session = r.session || S.session;
  render();
  if (S.session.state === 'TALLY') { showTally(); return; }
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
  if (r.ok) {
    const target = Date.now() + r.breather_remaining * 1000;
    if (target > S.breatherEndsAt) {
      S.breatherEndsAt = target;
      S.breatherTotal = Math.max(S.breatherTotal, r.breather_remaining);
    }
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
  S.tickTimer = setInterval(() => {
    const s = (Date.now() - S.dialStartedAt) / 1000;
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
  }

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
      if (S.session.state === 'PAUSED') {
        S.pauseEndsAt = Date.now() + S.session.pause_remaining * 1000;
        show('paused'); tickPause();
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
    if (st === 'BREATHER' && !typing) {
      if (S.cfg.dispositions[e.key]) {
        e.preventDefault();
        setDisposition(S.cfg.dispositions[e.key]);
        return;
      }
      if (e.key.toLowerCase() === 'd') { e.preventDefault(); toggleDnc(); return; }
    }
    if (e.key.toLowerCase() === 'p' && !typing) {
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
      lead_type: $('lead-type').value || null,
    }});
    if (r.error) {
      $('idle-status').textContent = r.error;
      $('start').disabled = false;
      return;
    }
    S.session = r;
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
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); commitAndDial(); }
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

boot().catch(e => { console.error(e); banner('Startup failed: ' + e.message); });
})();
