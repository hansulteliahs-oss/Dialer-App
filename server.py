#!/usr/bin/env python3
"""
No Brakes dialer - local Flask server.

Binds 127.0.0.1 but is browsed as http://localhost:8787. Never 0.0.0.0: this
process can place billable calls. The split is deliberate - there is a live
dispute over whether 127.0.0.1 always counts as a secure context for
getUserMedia, while localhost definitively does. Same destination, no ambiguity
about the mic.

THE SESSION LIVES HERE, NOT IN THE WINDOW. That is the whole point. Closing the
tab, force-quitting Chrome, or killing the app does not end a session; relaunching
drops him back into the breather at "8 of 20" with no Start button and no fresh
slate. Quitting is always possible - Cmd-Q, Force Quit and the power button always
work, and any design claiming otherwise is a lie. The goal is only that quitting
costs more than continuing, and that it never actually ends anything.

The same applies to the WARMUP that START drops into: its deadline is an absolute
timestamp in the session file, so the five minutes elapse whether or not the app
is running. Quitting through the lock-in does not skip it and does not reset it.
"""
from __future__ import annotations

import json
import os
import re
import signal
import sys
import threading
import time
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

from flask import Flask, jsonify, request, send_from_directory

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
STATIC_DIR = ROOT / "static"

# Load .env before importing anything that reads os.environ at construction time.
from dialer import load_env                       # noqa: E402
load_env(ROOT / ".env")

from dialer import outcomes                      # noqa: E402
from dialer.airtable import (  # noqa: E402
    DEFAULT_PILE, PILES, TIER_LABELS, AirtableClient, AirtableError, DialRefused,
)
from dialer.twilio_voice import TwilioConfigError, TwilioVoice  # noqa: E402

PORT = int(os.environ.get("DIALER_PORT", "8787"))
BIND = "127.0.0.1"
DRY_RUN = os.environ.get("DIALER_DRY_RUN", "0") == "1"
ARM_WRITE = os.environ.get("DIALER_ARM_WRITE", "0") == "1"

# Cost guard against a runaway loop. Not a brake - he will never reach it.
MAX_DIALS_PER_SESSION = 60
# The outer bound is a legal line, not a preference. It never speaks between
# 8am and 9pm, which is every hour he would actually dial.
CALL_WINDOW = (8, 21)

BREATHER_DEAD_END = 15    # ring-out, voicemail, dead end
BREATHER_REAL = 120       # an actual human conversation
TYPING_RESUME_AFTER = 10  # timer resumes this long after the last keystroke
PAUSE_MAX_SECONDS = 600   # one per session, 10:00 ceiling
QUEUE_PREFETCH = 80

# The lock-in window between START and the first dial. Same no-stopping rules as
# the rest of the session: no key shortens it, and the clock is wall-clock and
# server-side, so force-quitting through it neither escapes it nor resets it.
# Env-overridable because the dry-run harness cannot wait five minutes.
WARMUP_SECONDS = max(0, int(os.environ.get("DIALER_WARMUP_SECONDS", "300")))
# How many leads off the top of the queue the warmup screen shows him. Enough to
# read the cues and look up a missing owner name; not so many it becomes browsing.
WARMUP_PREP_LEADS = 3

app = Flask(__name__, static_folder=None)

_lock = threading.RLock()
_air: AirtableClient | None = None
_twilio: TwilioVoice | None = None
_session: dict | None = None


# --- session persistence -----------------------------------------------------

def session_path(d: str | None = None) -> Path:
    return STATE_DIR / f"session-{d or date.today().isoformat()}.json"


def save_session() -> None:
    """Persist the session. Never raises.

    Called from every mutating route AND from bootstrap(), which runs before
    app.run() and is guarded only by `except TwilioConfigError`. A full disk or
    an unwritable state/ therefore used to crash the launch outright with a raw
    traceback, or 500 a route mid-session. Losing the on-disk copy is bad;
    refusing to dial because of it is worse.
    """
    if _session is None:
        return
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = session_path(_session["date"]).with_suffix(".tmp")
        tmp.write_text(json.dumps(_session, indent=2, default=str))
        tmp.replace(session_path(_session["date"]))
    except Exception as e:  # noqa: BLE001
        print(f"[SESSION NOT SAVED - resume will be stale] {e}", flush=True)


def _read_session(p: Path) -> dict | None:
    try:
        s = json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    if s.get("abandoned") or s.get("state") == "DONE":
        return None
    return s


def load_session() -> dict | None:
    """The session to resume, if there is one.

    Looks for today's file first, then falls back to the newest unfinished one.
    Keying only on today's date orphaned any session that outlived local
    midnight - the file kept yesterday's name, nothing ever looked for it
    again, and a parked outcome inside it was never committed. That directly
    contradicts the promise the whole design rests on: relaunching always drops
    him back where he was, and no outcome is ever lost.
    """
    p = session_path()
    if p.exists():
        s = _read_session(p)
        if s:
            return s
    # Yesterday only. An unbounded fallback would resurrect a session a hard
    # crash abandoned weeks ago - dropping him into a stale breather against a
    # queue whose rows have all since been dialed - which is a worse failure
    # than the orphaned-at-midnight one it is here to fix.
    yesterday = session_path((date.today() - timedelta(days=1)).isoformat())
    if yesterday.exists():
        s = _read_session(yesterday)
        if s:
            print(f"resuming a session that outlived its day: {yesterday.name}", flush=True)
            return s
    return None


def new_session(target: int, filters: dict, queue: list) -> dict:
    now = time.time()
    return {
        "id": uuid.uuid4().hex[:12],
        "date": date.today().isoformat(),
        "target": target,
        "filters": filters,
        "queue": queue,
        "cursor": 0,
        "completed": 0,
        "dials": 0,
        "connects": 0,
        "conversations": 0,
        "booked": 0,
        # Never IDLE once started - there is no going back. WARMUP is the lock-in
        # window; the first dial fires when it expires, on its own.
        "state": "WARMUP" if WARMUP_SECONDS > 0 else "BREATHER",
        "warmup_seconds": WARMUP_SECONDS,
        "warmup_until": now + WARMUP_SECONDS,
        "breather_until": now,           # first dial fires the moment warmup ends
        "breather_seconds": 0,
        "pause_used": False,
        "pause_until": None,
        "current_call": None,
        # The outcome of the call that just ended, held while the breather runs so
        # 1-5 / D can still amend it. Committed when the breather expires. Held
        # server-side so a force-quit mid-breather cannot lose it: bootstrap
        # commits any orphan it finds.
        "pending_outcome": None,
        "started_at": time.time(),
        "abandoned": False,
        "outcomes": [],
    }


def public_session() -> dict:
    """What the browser is allowed to see. Includes only the current and next lead,
    except during the warmup, where the point is to arrive at the first dial having
    already read the top of the list."""
    if _session is None:
        return {"active": False}
    s = _session
    q, c = s["queue"], s["cursor"]
    now = time.time()
    remaining = max(0, s["target"] - s["completed"])
    return {
        "active": True,
        "id": s["id"],
        "state": s["state"],
        "target": s["target"],
        "completed": s["completed"],
        "remaining": remaining,
        "dials": s["dials"],
        "connects": s["connects"],
        "conversations": s["conversations"],
        "booked": s["booked"],
        "lead": q[c] if c < len(q) else None,
        "next_lead": q[c + 1] if c + 1 < len(q) else None,
        "warmup_leads": q[c:c + WARMUP_PREP_LEADS] if s["state"] == "WARMUP" else None,
        "queue_depth": len(q) - c,
        "warmup_remaining": max(0.0, (s.get("warmup_until") or 0) - now),
        "warmup_seconds": s.get("warmup_seconds", 0),
        "breather_remaining": max(0.0, (s["breather_until"] or 0) - now),
        "breather_seconds": s["breather_seconds"],
        "pause_used": s["pause_used"],
        "pause_remaining": max(0.0, (s["pause_until"] or 0) - now) if s["pause_until"] else 0,
        "current_call": s["current_call"],
        # Why the tally screen is showing. "Done, 20 of 20" and "the list ran
        # dry at 11 of 20" are different mornings and used to look identical.
        "queue_exhausted": bool(s.get("queue_exhausted")),
        "past_cutoff": bool(s.get("past_cutoff")),
        "pending_outcome": s.get("pending_outcome"),
        "abandon_sentence": abandon_sentence(),
        "dry_run": DRY_RUN,
        "armed": ARM_WRITE,
        "max_dials": MAX_DIALS_PER_SESSION,
    }


def _as_int(value, default: int) -> int:
    """int() on a JSON body field, without letting a null or a string 500 it.

    `body.get("target", 20)` only defaults when the key is ABSENT - an explicit
    {"target": null} reached int(None) and raised inside the request handler.
    """
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def abandon_sentence() -> str:
    s = _session or {"completed": 0, "target": 0}
    remaining = max(0, s["target"] - s["completed"])
    return (f"I am quitting at {s['completed']} of {s['target']} "
            f"with {remaining} calls left")


def last_abandon() -> dict | None:
    p = STATE_DIR / "abandons.jsonl"
    if not p.exists():
        return None
    lines = [l for l in p.read_text().splitlines() if l.strip()]
    if not lines:
        return None
    try:
        return json.loads(lines[-1])
    except json.JSONDecodeError:
        return None


# --- guards ------------------------------------------------------------------

def warmup_remaining() -> float:
    """Seconds left in the lock-in, 0 if it is over or was never running.
    Wall-clock on purpose: time spent force-quit still counts down, so quitting
    through the warmup neither escapes it nor restarts it. Caller holds the lock."""
    if _session is None or _session["state"] != "WARMUP":
        return 0.0
    return max(0.0, (_session.get("warmup_until") or 0) - time.time())


def in_call_window(ts: float | None = None) -> bool:
    when = datetime.fromtimestamp(ts) if ts is not None else datetime.now()
    return CALL_WINDOW[0] <= when.hour < CALL_WINDOW[1]


def window_error() -> dict:
    return {
        "error": "outside_call_window",
        "detail": (f"It is {datetime.now().strftime('%-I:%M %p')}. Calls are only "
                   f"placed between {CALL_WINDOW[0]}:00 and {CALL_WINDOW[1]}:00 local. "
                   "This is a legal line, not a preference."),
    }


def warmup_window_error() -> dict:
    """Starting late enough that the warmup would land the first dial outside the
    window. Refused at START rather than at the first dial - the warmup has no
    stop, so it must never run toward a wall."""
    ends = datetime.fromtimestamp(time.time() + WARMUP_SECONDS)
    return {
        "error": "warmup_ends_outside_call_window",
        "detail": (f"The {WARMUP_SECONDS // 60}-minute warmup would put the first "
                   f"dial at {ends.strftime('%-I:%M %p')}, past the "
                   f"{CALL_WINDOW[1]}:00 legal cutoff. Too late to start."),
    }


# --- routes ------------------------------------------------------------------

@app.after_request
def no_store(resp):
    resp.headers["Cache-Control"] = "no-store"
    return resp


# Content types a cross-origin <form> can send without triggering a preflight.
# application/json is not one of them, which is what makes requiring it a real
# gate rather than a decoration.
_JSON = "application/json"


@app.before_request
def same_origin_only():
    """Binding 127.0.0.1 keeps the network out. It does not keep websites out.

    Every browser on this Mac can reach localhost, and a cross-origin form POST
    is a "simple request": it is sent, and only the *response* is hidden by the
    same-origin policy. The side effects land. So any page he has open in
    another tab could POST /api/session and start a session, POST /api/dial and
    spend attempts on real prospects at $0.013 a minute, or POST /api/abandon
    and end a block mid-call. Nothing here authenticated anything.

    Two gates, both of which the real window already passes:
      1. state-changing /api calls must be application/json - a content type a
         cross-origin form cannot produce without a preflight it would fail.
      2. when the browser volunteers where the request came from, believe it.
    """
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return None
    if not request.path.startswith("/api/"):
        return None

    site = request.headers.get("Sec-Fetch-Site")
    if site and site not in ("same-origin", "none"):
        return jsonify({"error": "cross-site request refused"}), 403

    origin = request.headers.get("Origin")
    if origin and urlparse(origin).hostname not in ("localhost", "127.0.0.1"):
        return jsonify({"error": "cross-origin request refused"}), 403

    ctype = (request.headers.get("Content-Type") or "").split(";")[0].strip().lower()
    if ctype != _JSON:
        return jsonify({"error": f"expected {_JSON}"}), 415
    return None


@app.get("/")
def index():
    return send_from_directory(STATIC_DIR, "index.html")


@app.get("/<path:filename>")
def static_files(filename):
    return send_from_directory(STATIC_DIR, filename)


@app.get("/health")
def health():
    return jsonify({
        "ok": True,
        "dry_run": DRY_RUN,
        "armed": ARM_WRITE,
        "session_active": _session is not None,
    })


@app.post("/api/client-log")
def client_log():
    """Everything that goes wrong goes wrong in the browser, where it dies with
    the window. On 2026-08-05 a call produced no ringback and hung up at 13s of a
    22s timeout, and the only way to see it was reconstructing the media failure
    from Twilio timestamps - the actual error text was gone. This is the line it
    should have been read off instead.

    Fire-and-forget by contract: never raises, always 200. Nothing here may cost
    a dial. A diagnostic that can stop the machine is worse than no diagnostic.
    """
    try:
        body = request.get_json(silent=True) or {}
        level = str(body.get("level") or "info")[:12]
        msg = str(body.get("msg") or "")[:400]
        line = f"[client {level}] {msg}"
        detail = body.get("detail")
        if detail:
            line += f" | {json.dumps(detail, default=str)[:600]}"
        print(f"{datetime.now():%H:%M:%S} {line}", flush=True)
    except Exception:  # noqa: BLE001
        pass
    return jsonify({"ok": True})


@app.get("/api/config")
def config():
    return jsonify({
        "dry_run": DRY_RUN,
        "armed": ARM_WRITE,
        "caller_id": _twilio.caller_id if _twilio else None,
        "in_call_window": in_call_window(),
        "call_window": CALL_WINDOW,
        "dispositions": outcomes.KEY_TO_DISPOSITION,
        "breather": {"dead_end": BREATHER_DEAD_END, "real": BREATHER_REAL,
                     "typing_resume_after": TYPING_RESUME_AFTER},
        "warmup": WARMUP_SECONDS,
        # The picker is built from this, never hard-coded in the window - same
        # rule as the disposition keys. Two piles: work the warm rows, or grind
        # the cold list. Both reach a due callback.
        "piles": [
            {"value": "priority", "label": "priority — callbacks, shopped, hiring",
             "tiers": [TIER_LABELS[t] for t in PILES["priority"]]},
            {"value": "cold", "label": "cold pile — never dialed",
             "tiers": [TIER_LABELS[t] for t in PILES["cold"]]},
        ],
        "default_pile": DEFAULT_PILE,
        "pause_max": PAUSE_MAX_SECONDS,
        "last_abandon": last_abandon(),
        # Outcomes the retry queue eventually gave up on. Non-zero means a call
        # he made is not in Airtable, so it has to be visible in the window
        # rather than buried in a log line he will never read.
        "dropped_writes": _air.dropped_count() if _air else 0,
    })


@app.get("/api/token")
def token():
    if DRY_RUN:
        return jsonify({"dry_run": True, "token": None})
    if _twilio is None:
        return jsonify({"error": "twilio not configured"}), 503
    try:
        return jsonify(_twilio.mint_token())
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": str(e)}), 500


@app.get("/api/queue")
def queue():
    limit = max(1, min(_as_int(request.args.get("limit", 25), 25), 200))
    industry = request.args.get("industry") or None
    pile = request.args.get("pile") or None
    try:
        return jsonify({"leads": _air.fetch_queue(limit, industry, pile)})
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": str(e)}), 502


@app.get("/api/session")
def get_session():
    with _lock:
        return jsonify(public_session())


@app.post("/api/session")
def start_session():
    """The only click in the session. Also unlocks browser audio, client-side."""
    global _session
    body = request.get_json(silent=True) or {}
    with _lock:
        if _session is not None:
            # Never hand back a fresh slate. Resume is the only option.
            return jsonify(public_session())
        if not in_call_window():
            return jsonify(window_error()), 403
        if not in_call_window(time.time() + WARMUP_SECONDS):
            return jsonify(warmup_window_error()), 403
        target = max(1, min(_as_int(body.get("target", 20), 20), MAX_DIALS_PER_SESSION))
        filters = {
            "industry": body.get("industry") or None,
            "pile": body.get("pile") or DEFAULT_PILE,
        }
        try:
            q = _air.fetch_queue(QUEUE_PREFETCH, filters["industry"], filters["pile"])
        except Exception as e:  # noqa: BLE001
            return jsonify({"error": f"could not build the queue: {e}"}), 502
        if not q:
            return jsonify({"error": "no dialable leads match those filters"}), 404
        _session = new_session(target, filters, q)
        save_session()
        return jsonify(public_session())


@app.post("/api/warmup/done")
def warmup_done():
    """The lock-in ran out. The window asks, the server decides.

    There is no early-exit counterpart to this. ENTER shortens the breather and
    the pause because going faster is always allowed, but the warmup is the one
    timer whose whole job is to be sat through - a skip key would be pressed
    reflexively on exactly the mornings it exists for.
    """
    with _lock:
        if _session is None:
            return jsonify({"error": "no session"}), 409
        if _session["state"] != "WARMUP":
            return jsonify(public_session())
        left = warmup_remaining()
        if left > 0:
            return jsonify({"error": "warmup", "warmup_remaining": left,
                            "session": public_session()}), 425
        _session["state"] = "BREATHER"
        _session["breather_seconds"] = 0
        _session["breather_until"] = time.time()
        save_session()
        return jsonify(public_session())


@app.post("/api/dial")
def dial():
    """Called just before device.connect(). Re-checks DNC on the live row."""
    with _lock:
        if _session is None:
            return jsonify({"error": "no session"}), 409
        if not in_call_window():
            # 9pm is a wall that does not reopen today, so end the session HERE
            # rather than leaving it mid-breather. The window only showed a
            # tally screen; the server still thought a session was running, so
            # [DONE] answered "the session is not finished" and did nothing.
            if _session["state"] != "TALLY":
                _session["state"] = "TALLY"
                _session["breather_until"] = None
                _session["breather_seconds"] = 0
                _session["past_cutoff"] = True
                save_session()
            return jsonify(window_error()), 403
        # The warmup is enforced here, not only by the countdown in the window.
        # A hand-rolled POST must not be able to jump the lock-in either.
        left = warmup_remaining()
        if left > 0:
            return jsonify({"error": "warmup", "warmup_remaining": left,
                            "detail": f"{int(left)}s of warmup left"}), 425
        # The browser must not be the only thing standing between a stale timer
        # and a second call placed on top of a live one. On 2026-08-05 a
        # resurrected countdown fired mid-conversation and this endpoint took
        # the dial: it spent an attempt, reset current_call, and the client then
        # tore down the call that was actually up. The state machine lives here,
        # so the refusal belongs here too - the client guards are the courtesy,
        # this is the guarantee.
        #
        # Only a live BREATHER may become a dial. The original guard named only
        # DIALING, which let a dial through from the TALLY screen - target met,
        # machine already finished - and spent a real attempt on a real prospect.
        # PAUSED and DONE were the same hole.
        if _session["state"] != "BREATHER":
            return jsonify({
                "error": f"a call cannot start from {_session['state']}",
            }), 409
        if _session["dials"] >= MAX_DIALS_PER_SESSION:
            return jsonify({"error": "session dial cap reached"}), 429
        # The cursor can sit past the end after a refill found nothing; indexing
        # it raised IndexError -> 500, and a 500 here is a window with no way
        # forward and no way to stop.
        if _session["cursor"] >= len(_session["queue"]):
            return jsonify({"error": "the queue is exhausted"}), 409
        lead = _session["queue"][_session["cursor"]]
        # Claim the slot HERE, inside the same critical section that checked it.
        # The DNC re-check below runs with the lock released, and the state was
        # only set to DIALING after it came back - so two requests arriving
        # together both saw BREATHER, both passed the guard, and both placed a
        # call. One call at a time is not a preference: it is one of the three
        # constraints keeping this outside the ATDS definition.
        # (Reproduced 2026-08-05: two concurrent POSTs, two numbers, two dials.)
        _session["state"] = "DIALING"

    # Hard refusal #1, second half: the queue filter can go stale between pull and
    # dial. He runs parallel Claude sessions and the SDR agent writes these rows.
    t0 = time.monotonic()
    try:
        phone = _air.assert_dialable(lead["id"])
    except Exception as e:  # noqa: BLE001
        if not isinstance(e, DialRefused):
            # We could not reach Airtable to ask - which is not the same as the
            # row being refused, and must not cost him the lead. Hold the
            # cursor, hand the client a retryable error, and let it come back
            # to this same number in five seconds. (Treating these alike burned
            # the whole 80-lead prefetch in seconds on a dead wifi and left the
            # tally screen looking like a completed session.)
            with _lock:
                if _session is not None and _session["state"] == "DIALING":
                    _session["state"] = "BREATHER"
                    _session["breather_seconds"] = 0
                    _session["breather_until"] = time.time()
                save_session()
            return jsonify({
                "error": f"Airtable is unreachable, holding this number: {e}",
                "session": public_session(),
            }), 503
        with _lock:
            try:
                # A refused row costs zero seconds. Straight to the next number.
                _advance(breather_seconds=0, skip=True)
            finally:
                # The DIALING claim above must come off no matter what _advance
                # did. Leaving it set would 409 every subsequent dial for the
                # rest of the session - a session with no way forward, which is
                # the one state this whole machine exists to prevent.
                if _session is not None and _session["state"] == "DIALING":
                    _session["state"] = "BREATHER"
                    _session["breather_seconds"] = 0
                    _session["breather_until"] = time.time()
                save_session()
        return jsonify({"error": str(e), "skipped": True,
                        "session": public_session()}), 200
    dnc_ms = int((time.monotonic() - t0) * 1000)

    with _lock:
        # state is already DIALING - claimed above, before the lock was released.
        _session["dials"] += 1
        _session["current_call"] = {
            "lead_id": lead["id"], "phone": phone,
            "started_at": time.time(), "parent_sid": None,
        }
        t1 = time.monotonic()
        save_session()
        # The two serial costs standing between "go" and the browser holding a
        # phone number. Everything after this line is Twilio's side of the gap.
        print(f"{datetime.now():%H:%M:%S} [dial] dnc_check={dnc_ms}ms "
              f"save_session={int((time.monotonic() - t1) * 1000)}ms", flush=True)
        return jsonify({"phone": phone, "lead": lead, "session": public_session()})


@app.post("/api/call-sid")
def call_sid():
    """The browser reports its parent CallSid, read inside the accept handler."""
    sid = (request.get_json(silent=True) or {}).get("call_sid")
    with _lock:
        if _session and _session.get("current_call"):
            _session["current_call"]["parent_sid"] = sid
            save_session()
    return jsonify({"ok": True})


@app.get("/api/call-status")
def call_status():
    sid = request.args.get("parent_sid")
    if not sid:
        return jsonify({"error": "parent_sid required"}), 400
    if DRY_RUN or _twilio is None:
        return jsonify({"dry_run": True})
    try:
        return jsonify(_twilio.call_status(sid))
    except Exception as e:  # noqa: BLE001
        # A polling failure must never stop the machine.
        return jsonify({"status": "unknown", "connected": False,
                        "finished": False, "error": str(e)})


@app.post("/api/hangup")
def hangup():
    sid = (request.get_json(silent=True) or {}).get("call_sid")
    if not sid or DRY_RUN or _twilio is None:
        return jsonify({"ok": True, "dry_run": True})
    return jsonify(_twilio.hangup(sid))


@app.post("/api/breather/start")
def breather_start():
    """The call ended. Park the outcome and start the breather.

    Nothing is written to Airtable yet - the disposition panel is live during
    every breather, so SPACE is always overridable. The key only sets breather
    length, never the record.
    """
    body = request.get_json(silent=True) or {}
    disposition = body.get("disposition", "No Answer")
    if disposition not in outcomes.LIVE_DISPOSITIONS:
        return jsonify({"error": f"unknown disposition {disposition!r}"}), 400
    with _lock:
        if _session is None:
            return jsonify({"error": "no session"}), 409
        # Same unguarded index as /api/dial had. A call that ends after the
        # cursor ran off the end raised IndexError -> 500, and the window's
        # endCall() has no handler for that: the loop simply stops.
        if _session["cursor"] >= len(_session["queue"]):
            return jsonify({"error": "the queue is exhausted"}), 409
        lead = _session["queue"][_session["cursor"]]
        _session["pending_outcome"] = {
            "record_id": lead["id"],
            "company": lead.get("company"),
            "disposition": disposition,
            "connected": bool(body.get("connected")),
            "note": None,
            "next_action": outcomes.default_next_action(disposition),
            "next_note": None,
            "dnc": False,
        }
        secs = BREATHER_REAL if _as_int(body.get("breather", 0), 0) >= BREATHER_REAL \
            else BREATHER_DEAD_END
        _session["state"] = "BREATHER"
        _session["breather_seconds"] = secs
        _session["breather_until"] = time.time() + secs
        _session["current_call"] = None
        save_session()
        return jsonify(public_session())


@app.post("/api/breather/update")
def breather_update():
    """Amend the parked outcome mid-breather. 1-5, D, the note, the date."""
    body = request.get_json(silent=True) or {}
    with _lock:
        if _session is None or not _session.get("pending_outcome"):
            return jsonify({"ok": False}), 409
        p = _session["pending_outcome"]
        if "disposition" in body:
            d = body["disposition"]
            if d not in outcomes.LIVE_DISPOSITIONS:
                return jsonify({"error": f"unknown disposition {d!r}"}), 400
            if d != p["disposition"]:
                # A changed disposition re-derives the pre-filled follow-up date
                # unless he has already typed one himself.
                p["next_action"] = outcomes.default_next_action(d)
            p["disposition"] = d
        if "next_action" in body:
            # Validate HERE, not at commit. This field is free text from the
            # breather panel, and resolve_next_action() raises on garbage. Left
            # unchecked it raised inside build_payload() at commit time - after
            # pending_outcome had already been cleared - which 500'd /api/outcome
            # and destroyed a real call's outcome: the disposition was gone, the
            # cursor never advanced, and the client re-dialed the same lead.
            # Refusing the keystroke costs him a retype; refusing at commit cost
            # him the call. (Reproduced 2026-08-05 with "next tuesday".)
            raw = body["next_action"] or None
            if raw:
                try:
                    raw = outcomes.resolve_next_action(raw)
                except ValueError as e:
                    return jsonify({"error": str(e)}), 400
            p["next_action"] = raw
        for k in ("note", "next_note"):
            if k in body:
                p[k] = body[k] or None
        if "dnc" in body:
            p["dnc"] = bool(body["dnc"])
            if p["dnc"]:
                # DNC files as Not interested and flips the checkbox, matching
                # cold_call_log.py. Honored on the spot, no rebuttal.
                p["disposition"] = "Not interested"
                p["next_action"] = None
        save_session()
        return jsonify({"ok": True, "pending": p})


def _commit_pending() -> dict | None:
    """Write the parked outcome to Airtable and advance. Caller holds the lock."""
    s = _session
    p = s.get("pending_outcome")
    if not p:
        return None
    s["pending_outcome"] = None

    # log_outcome() is contracted never to raise - a failed write queues itself.
    # This catch is for the contract being broken anyway, which it was: a bad
    # follow-up date escaped build_payload() as a ValueError and took the whole
    # commit with it. The dial is the thing that must survive. Counting it and
    # moving on is strictly better than 500-ing and stranding the session, so
    # nothing below this line may re-raise.
    try:
        result = _air.log_outcome(
            p["record_id"], p["disposition"], note=p.get("note"),
            next_action=p.get("next_action"), next_note=p.get("next_note"),
            dnc=p.get("dnc", False),
        )
    except Exception as e:  # noqa: BLE001
        print(f"[commit failed, dial still counted] {p.get('company')}: {e}", flush=True)
        result = {"ok": False, "armed": ARM_WRITE, "queued": False, "error": str(e)}

    # Persist the CLEARED pending outcome the instant the write lands, before
    # touching any counter. Every caller used to save well after this point -
    # outcome() after _advance(), bootstrap() sixty lines later, abandon() after
    # the jsonl append - so a kill anywhere in that window left a file still
    # holding the outcome we just PATCHed. The next boot's orphan-commit saw it,
    # wrote it AGAIN, and build_payload() re-read the live row: Attempts +2 for
    # one dial, the Notes marker twice, and the 4-attempt retirement able to
    # close a row after two real calls.
    save_session()

    s["completed"] += 1
    if p["disposition"] in ("Conversation", "Busy, Call Back", "Meeting Booked"):
        s["conversations"] += 1
    if p["disposition"] == "Meeting Booked":
        s["booked"] += 1
    if p.get("connected"):
        s["connects"] += 1
    s["outcomes"].append({
        "record_id": p["record_id"], "company": p.get("company"),
        "disposition": p["disposition"], "ts": time.time(),
        "write_ok": result.get("ok"), "queued": result.get("queued"),
    })
    return result


@app.post("/api/outcome")
def outcome():
    """Breather expired (or ENTER). Commit and move to the next number."""
    with _lock:
        if _session is None:
            return jsonify({"error": "no session"}), 409
        result = _commit_pending()
        if result is None:
            return jsonify({"write": None, "session": public_session()})
        _advance(breather_seconds=0)
        save_session()
        return jsonify({"write": result, "session": public_session()})


def _advance(breather_seconds: int = BREATHER_DEAD_END, skip: bool = False) -> None:
    """Move to the next lead. Caller holds the lock."""
    s = _session
    s["cursor"] += 1
    s["current_call"] = None
    if not skip and s["completed"] >= s["target"]:
        s["state"] = "TALLY"
        s["breather_until"] = None
        s["breather_seconds"] = 0
        # Drop it here too, or a pause asked for during the last call of a
        # session survives the tally and fires unrequested on the first dial
        # after [ANOTHER 10].
        s.pop("pause_requested", None)
        return
    if s["cursor"] >= len(s["queue"]):
        try:
            # timeout=5, not the default 30: this runs INSIDE the global lock,
            # so every other endpoint - the countdown, a keypress, the abandon
            # panel - is blocked for exactly as long as it takes. At the default
            # a flaky refill froze the whole app for ~90s across the retries.
            more = _air.fetch_queue(QUEUE_PREFETCH, s["filters"].get("industry"),
                                    s["filters"].get("pile"), timeout=5)
            seen = {l["id"] for l in s["queue"]}
            s["queue"].extend([l for l in more if l["id"] not in seen])
        except Exception as e:  # noqa: BLE001
            print(f"[queue refill failed] {e}")
    if s["cursor"] >= len(s["queue"]):
        s["state"] = "TALLY"
        s["queue_exhausted"] = True   # tally must say "list ran dry", not "done"
        s.pop("pause_requested", None)
        return
    s["state"] = "BREATHER"
    s["breather_seconds"] = breather_seconds
    s["breather_until"] = time.time() + breather_seconds

    # Cash in a pause that was asked for mid-call. /api/pause answers "pause
    # starts after this call" and set this flag - and nothing anywhere read it,
    # so the pause never came. A machine built on being trusted to keep going
    # cannot also quietly break the one promise it makes about stopping.
    if s.pop("pause_requested", False) and not s["pause_used"]:
        s["pause_used"] = True
        s["pause_until"] = time.time() + PAUSE_MAX_SECONDS
        s["state"] = "PAUSED"


@app.post("/api/breather/hold")
def breather_hold():
    """Typing holds the timer. It only ever extends while words are being
    produced, so it cannot be used to stall."""
    with _lock:
        if _session is None or _session["state"] != "BREATHER":
            return jsonify({"ok": False})
        target = time.time() + TYPING_RESUME_AFTER
        if target > (_session["breather_until"] or 0):
            _session["breather_until"] = target
        return jsonify({"ok": True,
                        "breather_remaining": _session["breather_until"] - time.time()})


@app.post("/api/breather/now")
def breather_now():
    """ENTER - go now."""
    with _lock:
        if _session is None:
            return jsonify({"ok": False})
        _session["breather_until"] = time.time()
        save_session()
        return jsonify({"ok": True, "session": public_session()})


@app.post("/api/pause")
def pause():
    """One per session, up to 10:00. A real interruption needs minutes; escaping
    dread needs indefinite. The ceiling serves the first, not the second."""
    with _lock:
        if _session is None:
            return jsonify({"error": "no session"}), 409
        if _session["state"] == "WARMUP":
            # Pausing the lock-in is pausing a pause, and it would burn the one
            # real pause he gets for the calls themselves.
            return jsonify({"ok": False,
                            "message": "the warmup is already the break"}), 200
        if _session["pause_used"]:
            # Checked BEFORE the deferred branch. The other order answered
            # "pause starts after this call" on a session whose one pause was
            # already spent, and then no pause came - the machine breaking the
            # only promise it makes about stopping.
            return jsonify({"ok": False, "message": "no pauses left"}), 200
        if _session["state"] == "DIALING":
            # Nothing to pause on a live call. Take effect after the hangup.
            _session["pause_requested"] = True
            save_session()
            return jsonify({"ok": True, "deferred": True,
                            "message": "pause starts after this call"})
        if _session["state"] not in ("BREATHER", "PAUSED"):
            return jsonify({"ok": False,
                            "message": f"nothing to pause from {_session['state']}"}), 200
        _session["pause_used"] = True
        _session["pause_until"] = time.time() + PAUSE_MAX_SECONDS
        _session["state"] = "PAUSED"
        save_session()
        return jsonify({"ok": True, "session": public_session()})


@app.post("/api/pause/resume")
def pause_resume():
    """ENTER resumes early - a faster-control, so it is allowed."""
    with _lock:
        if _session is None:
            return jsonify({"error": "no session"}), 409
        # Only a pause can be resumed. This transitioned unconditionally, so an
        # ENTER that arrived while a call was live dropped the session out of
        # DIALING into a zero-length breather - which is a dial placed on top of
        # the call that was already up.
        if _session["state"] != "PAUSED":
            return jsonify({"ok": False, "session": public_session()})
        _session["pause_until"] = None
        _session["state"] = "BREATHER"
        _session["breather_seconds"] = 0
        _session["breather_until"] = time.time()
        save_session()
        return jsonify({"ok": True, "session": public_session()})


@app.post("/api/another")
def another():
    """[ANOTHER 10] from the tally screen."""
    body = request.get_json(silent=True) or {}
    with _lock:
        if _session is None:
            return jsonify({"error": "no session"}), 409
        if _session["state"] != "TALLY":
            # It is a tally-screen button. Reachable mid-call it raised the
            # target and forced a BREATHER on top of a live call.
            return jsonify({"error": "the session is not finished"}), 403
        _session["target"] += max(1, min(_as_int(body.get("count", 10), 10),
                                         MAX_DIALS_PER_SESSION))
        _session["state"] = "BREATHER"
        _session["breather_seconds"] = 0
        _session["breather_until"] = time.time()
        save_session()
        return jsonify(public_session())


@app.post("/api/done")
def done():
    """[DONE] - only reachable from the tally screen, target already met."""
    global _session
    with _lock:
        if _session is None:
            return jsonify({"ok": True})
        if _session["state"] != "TALLY":
            return jsonify({"error": "the session is not finished"}), 403
        _session["state"] = "DONE"
        _session["finished_at"] = time.time()
        save_session()
        summary = public_session()
        _session = None
        return jsonify({"ok": True, "summary": summary})


@app.post("/api/abandon")
def abandon():
    """The only graceful exit: type the sentence, verbatim. Paste is blocked
    client-side. Recorded, and surfaced on the start screen until a full session
    completes."""
    global _session
    typed = (request.get_json(silent=True) or {}).get("sentence", "")
    with _lock:
        if _session is None:
            return jsonify({"error": "no session"}), 409
        expected = abandon_sentence()
        if typed.strip() != expected:
            return jsonify({"ok": False, "expected": expected}), 400
        # Abandoning the session must not also lose the last call's outcome -
        # and it must not be BLOCKED by one either. The typed sentence is the
        # only graceful exit in the whole app; if Airtable is down, "I am
        # quitting at 8 of 20" cannot be the thing that 500s. Commit if we can,
        # leave and record either way.
        try:
            _commit_pending()
        except Exception as e:  # noqa: BLE001
            print(f"[abandon: commit failed, leaving anyway] {e}", flush=True)
        try:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            with (STATE_DIR / "abandons.jsonl").open("a") as fh:
                fh.write(json.dumps({
                    "ts": datetime.now().isoformat(timespec="seconds"),
                    "session_id": _session["id"],
                    "completed": _session["completed"],
                    "target": _session["target"],
                    "dials": _session["dials"],
                }) + "\n")
        except Exception as e:  # noqa: BLE001
            # Recording the abandon must never be able to block the abandon.
            print(f"[abandon not recorded] {e}", flush=True)
        _session["abandoned"] = True
        _session["state"] = "DONE"
        save_session()
        _session = None
        return jsonify({"ok": True})


@app.get("/api/pending")
def pending():
    return jsonify(_air.flush_pending())


# --- startup -----------------------------------------------------------------

def _refuse_second_instance() -> None:
    """Exit if another server already owns this port.

    bootstrap() below auto-commits any `pending_outcome` it finds on disk,
    treating it as an orphan left by a dead process. But the RUNNING process
    parks an outcome there for the whole of every breather - that is the point
    of holding it server-side. So a second `./run.sh` (a documented entry point,
    and he runs parallel Claude sessions) would read the live session file,
    PATCH that outcome to Airtable itself, and only THEN die on the port bind.
    The first process commits the same call again when its breather ends:
    Attempts +2 for one dial, a duplicated Notes line, and the 4-attempt
    retirement able to fire a call early - permanent wrong writes to the CRM
    every other AIOS tool trusts.

    Only the packaged launcher checked for this, via its own curl; run.sh and a
    bare `python3 server.py` went straight through. The guard belongs here, so
    it covers every entry point.
    """
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.4)
        if s.connect_ex((BIND, PORT)) == 0:
            print(f"\nA No Brakes server is already running on {BIND}:{PORT}.\n"
                  "Not starting a second one - it would re-commit the running "
                  "session's parked outcome to Airtable.\n"
                  "Use the window that is already open, or quit it first.\n",
                  file=sys.stderr)
            sys.exit(1)


def bootstrap() -> None:
    global _air, _twilio, _session
    _refuse_second_instance()
    STATE_DIR.mkdir(parents=True, exist_ok=True)

    _air = AirtableClient()
    _air.start_retry_thread()

    # Keep the Airtable TLS connection warm while a session is running, so the
    # per-dial DNC re-check rides an existing socket instead of paying a fresh
    # handshake. Off the dial path by construction; a failed ping costs nothing.
    def _keep_airtable_warm():
        while True:
            time.sleep(45)
            try:
                # Read the global ONCE. `_session is not None` and
                # `_session.get(...)` were two separate global lookups, and
                # /api/done and /api/abandon reassign it to None - land between
                # them and this raised AttributeError, which ended the thread
                # for the rest of the process's life. Silently: the only trace
                # was a traceback in the log, and every later DNC re-check paid
                # a cold TLS handshake on the dial path forever after.
                s = _session
                if s is not None and s.get("state") != "TALLY":
                    _air.warm()
            except Exception as e:  # noqa: BLE001
                print(f"[warm thread] {e}", flush=True)
    threading.Thread(target=_keep_airtable_warm, daemon=True).start()

    if DRY_RUN:
        print("DIALER_DRY_RUN=1 - simulating calls, zero Twilio spend")
        # Still construct Twilio so the caller-ID guards are exercised, but
        # tolerate a missing config in dry run.
        try:
            _twilio = TwilioVoice()
        except TwilioConfigError as e:
            if "persona" in str(e):
                raise
            print(f"  (twilio not configured: {e})")
            _twilio = None
    else:
        _twilio = TwilioVoice()   # raises on the persona line, before anything starts
        t0 = time.monotonic()
        info = _twilio.preflight()
        print(f"twilio ok: {info['account_type']} account, caller id {info['caller_id']}, "
              f"balance ${info['balance']} "
              f"(preflight {int((time.monotonic() - t0) * 1000)}ms)")

    _session = load_session()
    if _session:
        # An outcome parked when the process died is committed before anything
        # else. This is why the pending outcome lives on the server and not in
        # the window: a force-quit mid-breather loses nothing.
        if _session.get("pending_outcome"):
            p = _session["pending_outcome"]
            print(f"committing orphaned outcome: {p.get('company')} -> {p['disposition']}")
            with _lock:
                _commit_pending()
                _advance(breather_seconds=BREATHER_DEAD_END)

        # Resume. No Start button, no fresh slate.
        if _session.get("pause_until") and _session["pause_until"] > time.time():
            _session["state"] = "PAUSED"
        elif _session["state"] == "WARMUP":
            # Wall-clock: the seconds spent force-quit already counted. If it ran
            # out while the app was dead, come back straight into the dial.
            if time.time() >= (_session.get("warmup_until") or 0):
                _session["state"] = "BREATHER"
                _session["breather_until"] = time.time()
                _session["breather_seconds"] = 0
        elif _session["state"] in ("DIALING", "PAUSED"):
            # A call cannot survive a restart; drop into the breather.
            _session["state"] = "BREATHER"
            _session["breather_until"] = time.time() + BREATHER_DEAD_END
            _session["breather_seconds"] = BREATHER_DEAD_END
            _session["current_call"] = None
        print(f"resuming session {_session['id']}: "
              f"{_session['completed']} of {_session['target']}")
        save_session()

    r = _air.flush_pending()
    if r["replayed"] or r["remaining"]:
        print(f"pending writes: replayed {r['replayed']}, {r['remaining']} remaining")

    print(f"armed={ARM_WRITE} dry_run={DRY_RUN}  ->  http://localhost:{PORT}")
    if not in_call_window():
        print(f"NOTE: outside the {CALL_WINDOW[0]}:00-{CALL_WINDOW[1]}:00 call window; "
              "dialing is refused until then")


def _shutdown(signum, frame):
    if _air:
        _air.stop()
    with _lock:
        if _session:
            save_session()
    sys.exit(0)


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)
    try:
        bootstrap()
    except (TwilioConfigError, AirtableError) as e:
        # AirtableError was not caught here, so a missing .env or a blank
        # AIRTABLE_API_KEY crashed with a raw traceback instead of the same
        # clean one-line explanation every Twilio misconfiguration gets.
        print(f"\n{e}\n", file=sys.stderr)
        sys.exit(1)
    app.run(host=BIND, port=PORT, threaded=True, debug=False, use_reloader=False)
