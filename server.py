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
from datetime import date, datetime
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
STATIC_DIR = ROOT / "static"

# Load .env before importing anything that reads os.environ at construction time.
from dialer import load_env                       # noqa: E402
load_env(ROOT / ".env")

from dialer import outcomes                      # noqa: E402
from dialer.airtable import DEFAULT_PILE, PILES, TIER_LABELS, AirtableClient  # noqa: E402
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
    if _session is None:
        return
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = session_path(_session["date"]).with_suffix(".tmp")
    tmp.write_text(json.dumps(_session, indent=2, default=str))
    tmp.replace(session_path(_session["date"]))


def load_session() -> dict | None:
    p = session_path()
    if not p.exists():
        return None
    try:
        s = json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    if s.get("abandoned") or s.get("state") == "DONE":
        return None
    return s


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
        "pending_outcome": s.get("pending_outcome"),
        "abandon_sentence": abandon_sentence(),
        "dry_run": DRY_RUN,
        "armed": ARM_WRITE,
        "max_dials": MAX_DIALS_PER_SESSION,
    }


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
    limit = min(int(request.args.get("limit", 25)), 200)
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
        target = max(1, min(int(body.get("target", 20)), MAX_DIALS_PER_SESSION))
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
        if _session["state"] == "DIALING":
            return jsonify({"error": "a call is already in progress"}), 409
        if _session["dials"] >= MAX_DIALS_PER_SESSION:
            return jsonify({"error": "session dial cap reached"}), 429
        lead = _session["queue"][_session["cursor"]]

    # Hard refusal #1, second half: the queue filter can go stale between pull and
    # dial. He runs parallel Claude sessions and the SDR agent writes these rows.
    try:
        phone = _air.assert_dialable(lead["id"])
    except Exception as e:  # noqa: BLE001
        with _lock:
            # A refused row costs zero seconds. Straight to the next number.
            _advance(breather_seconds=0, skip=True)
            save_session()
        return jsonify({"error": str(e), "skipped": True,
                        "session": public_session()}), 200

    with _lock:
        _session["state"] = "DIALING"
        _session["dials"] += 1
        _session["current_call"] = {
            "lead_id": lead["id"], "phone": phone,
            "started_at": time.time(), "parent_sid": None,
        }
        save_session()
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
        secs = BREATHER_REAL if int(body.get("breather", 0)) >= BREATHER_REAL \
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
        for k in ("note", "next_action", "next_note"):
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

    result = _air.log_outcome(
        p["record_id"], p["disposition"], note=p.get("note"),
        next_action=p.get("next_action"), next_note=p.get("next_note"),
        dnc=p.get("dnc", False),
    )

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
        return
    if s["cursor"] >= len(s["queue"]):
        try:
            more = _air.fetch_queue(QUEUE_PREFETCH, s["filters"].get("industry"),
                                    s["filters"].get("pile"))
            seen = {l["id"] for l in s["queue"]}
            s["queue"].extend([l for l in more if l["id"] not in seen])
        except Exception as e:  # noqa: BLE001
            print(f"[queue refill failed] {e}")
    if s["cursor"] >= len(s["queue"]):
        s["state"] = "TALLY"
        return
    s["state"] = "BREATHER"
    s["breather_seconds"] = breather_seconds
    s["breather_until"] = time.time() + breather_seconds


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
        if _session["state"] == "DIALING":
            # Nothing to pause on a live call. Take effect after the hangup.
            _session["pause_requested"] = True
            save_session()
            return jsonify({"ok": True, "deferred": True,
                            "message": "pause starts after this call"})
        if _session["pause_used"]:
            return jsonify({"ok": False, "message": "no pauses left"}), 200
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
        _session["target"] += max(1, min(int(body.get("count", 10)),
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
        # Abandoning the session must not also lose the last call's outcome.
        _commit_pending()
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        with (STATE_DIR / "abandons.jsonl").open("a") as fh:
            fh.write(json.dumps({
                "ts": datetime.now().isoformat(timespec="seconds"),
                "session_id": _session["id"],
                "completed": _session["completed"],
                "target": _session["target"],
                "dials": _session["dials"],
            }) + "\n")
        _session["abandoned"] = True
        _session["state"] = "DONE"
        save_session()
        _session = None
        return jsonify({"ok": True})


@app.get("/api/pending")
def pending():
    return jsonify(_air.flush_pending())


# --- startup -----------------------------------------------------------------

def bootstrap() -> None:
    global _air, _twilio, _session
    STATE_DIR.mkdir(parents=True, exist_ok=True)

    _air = AirtableClient()
    _air.start_retry_thread()

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
        info = _twilio.preflight()
        print(f"twilio ok: {info['account_type']} account, caller id {info['caller_id']}, "
              f"balance ${info['balance']}")

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
    except TwilioConfigError as e:
        print(f"\n{e}\n", file=sys.stderr)
        sys.exit(1)
    app.run(host=BIND, port=PORT, threaded=True, debug=False, use_reloader=False)
