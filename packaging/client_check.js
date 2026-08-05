#!/usr/bin/env node
/*
 * Client-side regression harness - the loop's timing races.
 *
 *     node packaging/client_check.js
 *
 * dryrun_check.py drives the server. Nothing drove the browser, which is where
 * the state machine actually lives, and on 2026-08-05 that gap cost a live call:
 * a receptionist at KCB Plumbing picked up and the dialer hung up on her ~6
 * seconds in, mid-sentence.
 *
 * What happened, from state/server.log:
 *
 *   11:12:58  he commits the previous breather, the next number goes out
 *   11:12:58  a /api/breather/hold from his last keystroke is still in flight
 *   11:12:58  ...it lands AFTER the commit and sets breatherEndsAt = now + 10s
 *   11:13:08  the receptionist answers
 *   11:13:09  the 1s watchdog sees that resurrected deadline expire -> dials
 *   11:13:09  device.connect() throws InvalidStateError: A Call is already active
 *   11:13:14  the catch's 5s timer runs endCall('error') -> disconnects HER call
 *
 * TYPING_RESUME_AFTER is 10 and the gap from commit to spurious dial was 11s.
 * That is the whole bug: a dead countdown came back to life and took the next
 * answered call with it.
 *
 * These tests load the real static/dialer.js in a vm with a DOM stub and a fake
 * clock, then reproduce both halves. They must fail against the code as it was.
 */
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');
const assert = require('assert');

const ROOT = path.resolve(__dirname, '..');
const SRC = fs.readFileSync(path.join(ROOT, 'static', 'dialer.js'), 'utf8');

let FAILS = 0, CHECKS = 0;
function check(label, fn) {
  CHECKS++;
  try { fn(); console.log('  ok    ' + label); }
  catch (e) { FAILS++; console.log('  FAIL  ' + label + '\n          ' + e.message); }
}

// --- DOM stub ----------------------------------------------------------------

function makeEl(id) {
  const handlers = {};
  const el = {
    id, textContent: '', innerHTML: '', value: '', hidden: false, className: '',
    href: '', target: '', rel: '', dataset: {}, style: {}, children: [],
    classList: {
      _s: new Set(),
      add(c) { this._s.add(c); }, remove(c) { this._s.delete(c); },
      contains(c) { return this._s.has(c); },
      toggle(c, on) { on === undefined ? (this._s.has(c) ? this._s.delete(c) : this._s.add(c))
                                       : (on ? this._s.add(c) : this._s.delete(c)); },
    },
    focus() {}, blur() {}, preventDefault() {},
    addEventListener(ev, fn) { (handlers[ev] = handlers[ev] || []).push(fn); },
    removeEventListener() {},
    fire(ev, arg) { (handlers[ev] || []).forEach(f => f(arg)); },
    append(...n) { el.children.push(...n); }, appendChild(n) { el.children.push(n); },
    querySelector() { return makeEl('q'); }, querySelectorAll() { return []; },
    _handlers: handlers,
  };
  return el;
}

// --- the sandbox -------------------------------------------------------------

function boot(opts = {}) {
  const els = {};
  const $ = id => (els[id] = els[id] || makeEl(id));

  let CLOCK = 1_700_000_000_000;
  const timers = [];       // {id, fn, at, every}
  let nextTimerId = 1;

  // Deferred routes: a path here returns a promise the test resolves by hand,
  // which is the only way to hold a response in flight across a state change.
  const deferred = {};
  const calls = [];        // every request the client made, in order

  const routes = Object.assign({
    '/api/config': () => CFG,
    '/api/session': () => ({active: false}),
    '/api/outcome': () => ({write: null, session: opts.sessionAfterOutcome || SESSION('BREATHER')}),
    '/api/dial': () => ({phone: '+15550000001', lead: LEAD, session: SESSION('DIALING')}),
    '/api/call-sid': () => ({ok: true}),
    '/api/breather/start': () => SESSION('BREATHER'),
    '/api/breather/update': () => ({pending: PENDING}),
    '/api/breather/hold': () => ({ok: true, breather_remaining: 10}),
    '/api/hangup': () => ({ok: true}),
    '/api/client-log': () => ({ok: true}),
  }, opts.routes || {});

  function fetchStub(url, init) {
    const p = String(url).split('?')[0];
    calls.push({path: p, body: init && init.body ? JSON.parse(init.body) : null, at: CLOCK});
    const respond = data => ({ok: true, status: 200, json: async () => data});
    if (deferred[p]) {
      return new Promise(res => { deferred[p].queue.push(v => res(respond(v))); });
    }
    const r = routes[p];
    return Promise.resolve(respond(r ? r() : {}));
  }

  const sandbox = {
    console: {log() {}, warn() {}, error() {}},
    fetch: fetchStub,
    JSON, Math, String, Number, Object, Array, Promise, Set, Map, Error, isNaN,
    parseInt, parseFloat, encodeURIComponent,
  };
  sandbox.window = sandbox;
  sandbox.globalThis = sandbox;
  sandbox.__NOBRAKES_TEST__ = true;

  // Fake clock. dialer.js only ever reads Date.now().
  const RealDate = Date;
  function FakeDate(...a) { return new RealDate(...a); }
  FakeDate.now = () => CLOCK;
  FakeDate.prototype = RealDate.prototype;
  sandbox.Date = FakeDate;

  sandbox.setTimeout = (fn, ms) => {
    const t = {id: nextTimerId++, fn, at: CLOCK + (ms || 0), every: null};
    timers.push(t); return t.id;
  };
  sandbox.setInterval = (fn, ms) => {
    const t = {id: nextTimerId++, fn, at: CLOCK + (ms || 0), every: ms || 1};
    timers.push(t); return t.id;
  };
  sandbox.clearTimeout = sandbox.clearInterval = id => {
    const i = timers.findIndex(t => t.id === id);
    if (i >= 0) timers.splice(i, 1);
  };
  // rAF must NOT self-drive here or tickBreather spins forever. The tests step
  // time explicitly; the 1s watchdog is the path that matters anyway, because it
  // is the one that survives a throttled background tab.
  sandbox.requestAnimationFrame = () => 0;

  sandbox.document = {
    getElementById: $,
    createElement: () => makeEl('new'),
    addEventListener(ev, fn) { (this._h[ev] = this._h[ev] || []).push(fn); },
    querySelectorAll: () => [],
    activeElement: null,
    _h: {},
  };
  sandbox.Twilio = {Device: function () { throw new Error('unused'); }};

  vm.createContext(sandbox);
  vm.runInContext(SRC, sandbox, {filename: 'dialer.js'});

  return {
    sandbox, els, calls, timers,
    nb: () => sandbox.__nb,
    now: () => CLOCK,
    defer(p) { deferred[p] = {queue: []}; },
    release(p, value) {
      const d = deferred[p];
      delete deferred[p];
      (d ? d.queue : []).forEach(fn => fn(value));
    },
    /* Step time forward, firing timers whose deadline passes. Each step yields to
       the microtask queue so awaits inside a handler settle before the next. */
    async advance(ms, stepMs = 250) {
      for (let done = 0; done < ms; done += stepMs) {
        CLOCK += Math.min(stepMs, ms - done);
        const due = timers.filter(t => t.at <= CLOCK).sort((a, b) => a.at - b.at);
        for (const t of due) {
          if (t.every) t.at = CLOCK + t.every;
          else timers.splice(timers.indexOf(t), 1);
          try { t.fn(); } catch (_) {}
          await flush();
        }
        await flush();
      }
    },
  };
}

const flush = () => new Promise(r => setImmediate(r));

// --- fixtures ----------------------------------------------------------------

const CFG = {
  dry_run: false, armed: true, in_call_window: true, call_window: [8, 17],
  warmup: 300, breather: {dead_end: 15, real: 45, typing_resume_after: 10},
  dispositions: {1: 'No Answer', 2: 'Conversation'},
  piles: [{value: 'priority', label: 'priority', tiers: ['callback_due']}],
  default_pile: 'priority', last_abandon: null,
};
const LEAD = {
  id: 'rec1', company: 'KCB Plumbing', first_name: 'Kevin', industry: 'Plumbing',
  phone: '+17148946520', phone_display: '(714) 894-6520', attempts: 1,
  tier: 'queued', tier_label: 'cold', context_cue: '', notes: '', next_action_note: '',
};
const PENDING = {disposition: 'No Answer', dnc: false, next_action: null};
const SESSION = state => ({
  active: true, state, target: 20, completed: 1, dials: 2, connects: 0,
  conversations: 0, booked: 0, lead: LEAD, next_lead: LEAD,
  pending_outcome: PENDING, breather_seconds: 15, breather_remaining: 15,
  abandon_sentence: 'i am stopping at 1 of 20',
});

/* Put the app in the state it is in the instant before the race: a live call is
   up, exactly as it was when the receptionist answered. */
function liveCall(h) {
  const S = h.nb().S;
  const call = {
    disconnected: false, handlers: {},
    on(ev, fn) { this.handlers[ev] = fn; },
    disconnect() { this.disconnected = true; },
    parameters: {CallSid: 'CAlive'},
  };
  S.cfg = CFG;
  S.audioUnlocked = true;
  S.device = {connect: async () => call, register: async () => {}, on() {}};
  S.session = SESSION('DIALING');
  S.call = call;
  S.parentSid = 'CAlive';
  S.connectedAt = h.now() - 6000;   // she has been talking for 6 seconds
  S.dialStartedAt = h.now() - 20000;
  S.busy = false;                    // connect() resolved at dial time, long ago
  return call;
}

// --- tests -------------------------------------------------------------------

async function main() {
  console.log('=== 1. a hold that lands after the commit must not resurrect the breather ===');
  {
    const h = boot();
    await flush(); await flush();
    const S = h.nb().S;
    S.cfg = CFG;
    S.audioUnlocked = true;
    S.device = {connect: async () => ({on() {}, disconnect() {}, parameters: {}}), on() {}};
    S.session = SESSION('BREATHER');
    S.breatherEndsAt = h.now() + 4000;
    S.breatherTotal = 15;

    h.defer('/api/breather/hold');
    h.nb().holdForTyping();            // his last keystroke, response held open
    await flush();

    await h.nb().commitAndDial();      // ENTER: commit, then the next number
    await flush();

    check('the commit placed exactly one dial', () =>
      assert.strictEqual(h.calls.filter(c => c.path === '/api/dial').length, 1));

    // The keystroke's response finally lands - after the session left BREATHER.
    h.release('/api/breather/hold', {ok: true, breather_remaining: 10});
    await flush(); await flush();

    check('a late hold does not revive a dead countdown', () =>
      assert.strictEqual(S.breatherEndsAt, null,
        `breatherEndsAt was resurrected to ${S.breatherEndsAt} (null expected)`));
  }

  console.log('\n=== 2. the watchdog must not dial on top of a live call ===');
  {
    const h = boot();
    await flush(); await flush();
    const call = liveCall(h);
    const S = h.nb().S;
    // Exactly what the landed hold did on 8/05: a deadline 10s out, while DIALING.
    S.breatherEndsAt = h.now() + 10_000;
    const before = h.calls.length;

    await h.advance(20_000);           // walk past it, firing the 1s watchdog

    const dials = h.calls.slice(before).filter(c => c.path === '/api/dial');
    check('no dial fires while a call is up', () =>
      assert.strictEqual(dials.length, 0, `${dials.length} dial(s) fired mid-call`));
    check('the live call was never disconnected', () =>
      assert.strictEqual(call.disconnected, false,
        'the dialer hung up on a live call'));
    check('the live call is still tracked', () =>
      assert.strictEqual(S.call, call, 'S.call was cleared out from under the call'));
  }

  console.log('\n=== 3. a dial that throws mid-call must abandon itself, not the call ===');
  {
    // The last line of defence: something calls dialNext() anyway. It must not
    // spend a dial, must not wipe the live call's tracking, must not hang up.
    const h = boot({routes: {'/api/dial': () => ({phone: '+15550000002', lead: LEAD,
                                                  session: SESSION('DIALING')})}});
    await flush(); await flush();
    const call = liveCall(h);
    const S = h.nb().S;
    S.device.connect = async () => {
      const e = new Error('A Call is already active');
      e.name = 'InvalidStateError';
      throw e;
    };
    const before = h.calls.length;

    await h.nb().dialNext();
    await h.advance(8000);             // past the catch's 5s teardown timer

    check('the spurious attempt spends no dial', () =>
      assert.strictEqual(h.calls.slice(before).filter(c => c.path === '/api/dial').length, 0,
        'a phantom dial was counted against the session'));
    check('it does not hang up the live call', () =>
      assert.strictEqual(call.disconnected, false,
        'the catch tore down the live call'));
    check('it does not wipe the live call sid', () =>
      assert.strictEqual(S.parentSid, 'CAlive',
        'parentSid was cleared - the poller goes deaf and the call files as No Answer'));
    check('it does not wipe the connected clock', () =>
      assert.ok(S.connectedAt, 'connectedAt was cleared - an answered call files as No Answer'));
  }

  console.log('\n=== 4. the ordinary loop still advances ===');
  {
    // The guards must not brick the machine. No call up, breather expired: dial.
    const h = boot();
    await flush(); await flush();
    const S = h.nb().S;
    S.cfg = CFG;
    S.audioUnlocked = true;
    S.device = {connect: async () => ({on() {}, disconnect() {}, parameters: {}}), on() {}};
    S.session = SESSION('BREATHER');
    S.call = null;
    S.breatherEndsAt = h.now() + 2000;
    const before = h.calls.length;

    await h.advance(6000);

    check('an expired breather with no live call still dials', () =>
      assert.strictEqual(h.calls.slice(before).filter(c => c.path === '/api/dial').length, 1,
        'the watchdog stopped advancing the queue - the loop is bricked'));
  }

  console.log('\n=== 5. one call ends once ===');
  {
    // Twilio fired 'error' then 'disconnect' for the same leg at 11:12:28. Two
    // teardowns meant two breathers counting down on one deadline, and four
    // stacked commits when it expired.
    const h = boot();
    await flush(); await flush();
    liveCall(h);
    const before = h.calls.length;

    await Promise.all([h.nb().endCall('error'), h.nb().endCall('remote')]);
    await flush(); await flush();

    const starts = h.calls.slice(before).filter(c => c.path === '/api/breather/start');
    check('a double teardown starts exactly one breather', () =>
      assert.strictEqual(starts.length, 1, `${starts.length} breathers started for one call`));
  }

  console.log('\n=== 6. a failed dial still recovers into a breather ===');
  {
    // The teardown latch must not stay shut from the previous call. If it does,
    // one failed /api/dial ends the session silently: no breather, no countdown,
    // no next number, and a window that just sits there looking like it is
    // waiting on him.
    const h = boot({routes: {'/api/dial': () => { throw new Error('network down'); }}});
    await flush(); await flush();
    const call = liveCall(h);
    const S = h.nb().S;

    await h.nb().endCall('remote');     // previous call ends, latch shuts
    await flush();
    assert.strictEqual(S.call, null, 'precondition: the call is down');
    S.session = SESSION('BREATHER');
    const before = h.calls.length;

    await h.nb().dialNext();            // this throws inside
    await h.advance(8000);              // past the catch's teardown timer

    const starts = h.calls.slice(before).filter(c => c.path === '/api/breather/start');
    check('a dial that throws still starts the next breather', () =>
      assert.strictEqual(starts.length, 1,
        'the loop stalled - no breather after a failed dial'));
  }

  console.log('\n=== 7. one ENTER is one commit ===');
  {
    // The note's keydown handler commits, and the document-level handler commits
    // too without checking whether he is typing. One keypress was producing two
    // /api/outcome requests - visible in the browser trace on 2026-08-05, where
    // four stacked up at 11:12:58.
    const h = boot();
    await flush(); await flush();
    const S = h.nb().S;
    S.cfg = CFG;
    S.audioUnlocked = true;
    S.device = {connect: async () => ({on() {}, disconnect() {}, parameters: {}}), on() {}};
    S.session = SESSION('BREATHER');
    S.call = null;
    S.breatherEndsAt = h.now() + 10_000;
    // getElementById auto-creates in the stub; boot() only touches these on a
    // resumed session, so reach them through the document.
    h.sandbox.document.getElementById('resume-gate').hidden = true;
    h.sandbox.document.getElementById('abandon-panel').hidden = true;
    const before = h.calls.length;

    // Model real bubbling: the note's handler runs, then the document's - unless
    // the first one stopped it.
    let stopped = false;
    const ev = {key: 'Enter', shiftKey: false,
                preventDefault() {}, stopPropagation() { stopped = true; }};
    (h.els.note._handlers.keydown || []).forEach(fn => fn(ev));
    if (!stopped) (h.sandbox.document._h.keydown || []).forEach(fn => fn(ev));
    await flush(); await flush();

    const commits = h.calls.slice(before).filter(c => c.path === '/api/outcome');
    check('one ENTER produces exactly one commit', () =>
      assert.strictEqual(commits.length, 1, `${commits.length} commits from one keypress`));
  }

  console.log('\n' + '='.repeat(64));
  if (FAILS) { console.log(`FAILED ${FAILS} of ${CHECKS} checks`); return 1; }
  console.log(`ALL ${CHECKS} CHECKS PASSED`);
  return 0;
}

main().then(c => process.exit(c)).catch(e => { console.error(e); process.exit(2); });
