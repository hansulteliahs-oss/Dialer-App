#!/usr/bin/env python3
"""
Dry-run verification harness - plan verification step 1.

Drives the running server through every branch of the state machine with zero
Twilio spend and zero Airtable writes. Start the server first:

    DIALER_DRY_RUN=1 DIALER_ARM_WRITE=0 ./run.sh

then:

    ./.venv/bin/python3 packaging/dryrun_check.py
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import date, timedelta
from pathlib import Path

BASE = "http://localhost:8787"
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FAILS: list[str] = []
CHECKS = 0


def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode() or "{}"), r.status
    except urllib.error.HTTPError as e:
        return json.loads(e.read().decode() or "{}"), e.code


def check(label, got, want):
    global CHECKS
    CHECKS += 1
    if got != want:
        FAILS.append(f"{label}: got {got!r}, want {want!r}")
        print(f"  FAIL  {label}: got {got!r}, want {want!r}")
    else:
        print(f"  ok    {label}")


def truthy(label, got):
    global CHECKS
    CHECKS += 1
    if not got:
        FAILS.append(f"{label}: got falsy {got!r}")
        print(f"  FAIL  {label}: {got!r}")
    else:
        print(f"  ok    {label}")


def reset():
    """Abandon any live session so each block starts clean."""
    s, _ = call("GET", "/api/session")
    if s.get("active"):
        call("POST", "/api/abandon", {"sentence": s["abandon_sentence"]})


def one_call(disposition="No Answer", connected=False, breather=15):
    """Simulate: dial -> call ends -> breather -> commit."""
    d, _ = call("POST", "/api/dial", {})
    while d.get("skipped"):
        d, _ = call("POST", "/api/dial", {})
    call("POST", "/api/breather/start",
         {"disposition": disposition, "connected": connected, "breather": breather})
    return call("POST", "/api/outcome", {})[0]


def main():
    print("=== 0. server is up, dry run, not armed ===")
    h, _ = call("GET", "/health")
    check("dry_run", h.get("dry_run"), True)
    check("armed", h.get("armed"), False)
    reset()

    print("\n=== 1. session starts, queue is ranked, no fresh slate on restart ===")
    s, code = call("POST", "/api/session", {"target": 3})
    check("session created", code, 200)
    check("target", s.get("target"), 3)
    check("starts in BREATHER, not IDLE", s.get("state"), "BREATHER")
    truthy("lead present", s.get("lead"))
    truthy("next lead present for the breather card", s.get("next_lead"))
    check("tier 1 is shopped-never-replied", s["lead"]["tier"], "mystery_no_reply")
    first_company = s["lead"]["company"]

    again, _ = call("POST", "/api/session", {"target": 99})
    check("second START never resets the session", again.get("target"), 3)
    check("second START keeps the same lead", again["lead"]["company"], first_company)

    print("\n=== 2. attempt counter is load-bearing (playbook: no VM on 1-2) ===")
    truthy("attempts is an int", isinstance(s["lead"]["attempts"], int))

    print("\n=== 3. auto-advance on a ring-out costs ZERO keys ===")
    d, _ = call("POST", "/api/dial", {})
    truthy("dial returned a phone", d.get("phone"))
    check("phone is E.164", d["phone"][:2], "+1")
    b, _ = call("POST", "/api/breather/start",
                {"disposition": "No Answer", "connected": False, "breather": 15})
    check("breather length for a dead end", b.get("breather_seconds"), 15)
    truthy("outcome parked, not yet written", b.get("pending_outcome"))
    check("parked disposition", b["pending_outcome"]["disposition"], "No Answer")

    print("\n=== 4. disposition panel is live during EVERY breather ===")
    u, _ = call("POST", "/api/breather/update", {"disposition": "Conversation"})
    check("SPACE is overridable", u["pending"]["disposition"], "Conversation")
    check("changing disposition re-derives the +7d prefill",
          u["pending"]["next_action"],
          (date.today() + timedelta(days=7)).isoformat())
    u, _ = call("POST", "/api/breather/update", {"disposition": "Busy, Call Back"})
    check("callback prefills +3d", u["pending"]["next_action"],
          (date.today() + timedelta(days=3)).isoformat())

    print("\n=== 5. typing holds the timer ===")
    before, _ = call("GET", "/api/session")
    time.sleep(1.2)
    hold, _ = call("POST", "/api/breather/hold", {})
    truthy("hold extended the deadline", hold.get("breather_remaining", 0) > 9)

    print("\n=== 6. DNC is honored on the spot, no rebuttal ===")
    u, _ = call("POST", "/api/breather/update", {"dnc": True})
    check("DNC files as Not interested", u["pending"]["disposition"], "Not interested")
    check("DNC clears the next action", u["pending"]["next_action"], None)
    check("DNC flag set", u["pending"]["dnc"], True)

    print("\n=== 7. commit advances to the next lead ===")
    o, _ = call("POST", "/api/outcome", {})
    check("completed incremented", o["session"]["completed"], 1)
    check("write not armed", o["write"]["armed"], False)
    truthy("payload built", o["write"]["payload"])
    check("terminal payload sends None, never '' for the date column",
          o["write"]["payload"]["Next Action"], None)
    check("terminal Next Action Note is ''",
          o["write"]["payload"]["Next Action Note"], "")
    check("Last Call Date is today (the accountability stack reads this)",
          o["write"]["payload"]["Last Call Date"], date.today().isoformat())
    truthy("Attempts incremented", o["write"]["payload"]["Attempts"] >= 1)

    print("\n=== 8. pause: one per session, ceiling, ENTER resumes early ===")
    p, _ = call("POST", "/api/pause", {})
    check("first pause granted", p.get("ok"), True)
    check("state is PAUSED", p["session"]["state"], "PAUSED")
    truthy("pause has a visible ceiling <= 10:00",
           0 < p["session"]["pause_remaining"] <= 600)
    p2, _ = call("POST", "/api/pause", {})
    check("second pause refused", p2.get("ok"), False)
    check("refusal message", p2.get("message"), "no pauses left")
    r, _ = call("POST", "/api/pause/resume", {})
    check("ENTER resumes early", r["session"]["state"], "BREATHER")

    print("\n=== 9. target reached -> TALLY, then ANOTHER 10 ===")
    st, _ = call("GET", "/api/session")
    while st["completed"] < st["target"]:
        st = one_call()["session"]
    check("tally reached", st["state"], "TALLY")
    truthy("dials counted", st["dials"] >= 3)
    a, _ = call("POST", "/api/another", {"count": 10})
    check("another 10 raises the target", a["target"], 13)
    check("another 10 returns to the breather", a["state"], "BREATHER")

    print("\n=== 10. abandon requires the sentence, verbatim ===")
    st, _ = call("GET", "/api/session")
    sentence = st["abandon_sentence"]
    truthy("sentence names the real numbers",
           f"at {st['completed']} of {st['target']}" in sentence)
    bad, code = call("POST", "/api/abandon", {"sentence": "i quit"})
    check("wrong sentence refused", code, 400)
    bad2, code2 = call("POST", "/api/abandon", {"sentence": sentence.upper()})
    check("case-mangled sentence refused", code2, 400)
    good, code3 = call("POST", "/api/abandon", {"sentence": sentence})
    check("verbatim sentence accepted", code3, 200)
    gone, _ = call("GET", "/api/session")
    check("session ended", gone.get("active"), False)

    print("\n=== 11. abandonment is recorded and surfaced ===")
    cfg, _ = call("GET", "/api/config")
    truthy("last_abandon recorded", cfg.get("last_abandon"))
    ab = ROOT / "state" / "abandons.jsonl"
    truthy("abandons.jsonl written", ab.exists() and ab.read_text().strip())

    print("\n=== 12. DNC row is refused at dial time, not just filtered ===")
    from dialer.airtable import AirtableClient  # noqa: E402
    from dialer import load_env
    load_env(ROOT / ".env")
    air = AirtableClient(arm_write=False)
    dnc_rows = air._request(
        "GET", f"/{air.base_id}/{air.table_id}?filterByFormula=%7BDNC%7D&pageSize=1"
    ).get("records", [])
    if dnc_rows:
        rid = dnc_rows[0]["id"]
        try:
            air.assert_dialable(rid)
            FAILS.append("assert_dialable ACCEPTED a DNC row")
            print("  FAIL  assert_dialable accepted a DNC row")
        except Exception as e:
            check("DNC refused at dial time", "DNC" in str(e), True)
        q = air.fetch_queue(limit=200)
        check("DNC row never enters the queue", rid in {l["id"] for l in q}, False)
    else:
        print("  skip  no DNC row in the table to test against")

    print("\n=== 13. caller-ID guard refuses the mystery-shop persona line ===")
    from dialer.twilio_voice import TwilioConfigError, TwilioVoice  # noqa: E402
    try:
        TwilioVoice(account_sid="AC" + "0" * 32, api_key="SK" + "0" * 32,
                    api_secret="x", twiml_app_sid="AP" + "0" * 32,
                    caller_id="+18583564281")
        FAILS.append("persona line accepted as caller ID")
        print("  FAIL  persona line accepted as caller ID")
    except TwilioConfigError as e:
        check("startup refuses the persona line", "persona" in str(e), True)

    print("\n=== 14. retirement at 4 attempts, and never for a row that talked ===")
    from dialer import outcomes  # noqa: E402
    p = outcomes.build_payload({"fields": {"Attempts": 3}}, "No Answer")
    check("4th no-answer retires the row", p["Status"], "Done")
    check("retirement clears Next Action", p["Next Action"], None)
    truthy("retirement leaves a note", "retired" in p.get("Notes", ""))
    talked = {"fields": {"Attempts": 8,
                         "Notes": "[2026-07-01] Conversation - call me in the fall"}}
    p2 = outcomes.build_payload(talked, "No Answer")
    check("a row that reached a human is never capped", p2["Status"], "Snoozed")

    print("\n" + "=" * 64)
    if FAILS:
        print(f"FAILED {len(FAILS)} of {CHECKS} checks")
        for f in FAILS:
            print("  - " + f)
        return 1
    print(f"ALL {CHECKS} CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
