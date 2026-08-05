#!/usr/bin/env python3
"""
Dry-run verification harness - plan verification step 1.

Drives the running server through every branch of the state machine with zero
Twilio spend and zero Airtable writes. Start the server first:

    DIALER_DRY_RUN=1 DIALER_ARM_WRITE=0 DIALER_WARMUP_SECONDS=2 ./run.sh

then:

    ./.venv/bin/python3 packaging/dryrun_check.py

The short warmup is required, not a convenience: the harness waits the lock-in
out rather than bypassing it, because there is no bypass to test against.
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

# Overridable so this can be pointed at a throwaway instance on another port
# without going anywhere near the live server on 8787.
BASE = os.environ.get("DIALER_CHECK_BASE", "http://localhost:8787")
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


MAX_TEST_WARMUP = 10   # the harness waits it out, so it has to be short


def main():
    print("=== 0. server is up, dry run, not armed ===")
    h, _ = call("GET", "/health")
    check("dry_run", h.get("dry_run"), True)
    check("armed", h.get("armed"), False)
    if not h.get("dry_run") or h.get("armed"):
        # Every block below this dials. Against an armed live server that is real
        # money and real prospects, so this aborts rather than reporting a fail
        # and carrying on into the queue.
        print("\nABORT: this harness dials. The server on 8787 is live "
              f"(dry_run={h.get('dry_run')}, armed={h.get('armed')}). "
              "Stop it and restart with DIALER_DRY_RUN=1 DIALER_ARM_WRITE=0.\n")
        return 2

    cfg0, _ = call("GET", "/api/config")
    warmup = cfg0.get("warmup")
    truthy("config exposes the warmup length", isinstance(warmup, int))
    if not isinstance(warmup, int) or warmup > MAX_TEST_WARMUP:
        print(f"\nwarmup is {warmup}s. This harness sits through the real lock-in "
              f"rather than bypassing it, so restart the server with:\n\n"
              f"    DIALER_DRY_RUN=1 DIALER_ARM_WRITE=0 DIALER_WARMUP_SECONDS=2 ./run.sh\n")
        return 2
    reset()

    print("\n=== 1. session starts, queue is ranked, no fresh slate on restart ===")
    s, code = call("POST", "/api/session", {"target": 3})
    check("session created", code, 200)
    check("target", s.get("target"), 3)
    check("starts in WARMUP, not IDLE and not dialing", s.get("state"), "WARMUP")
    truthy("lead present", s.get("lead"))
    truthy("next lead present for the breather card", s.get("next_lead"))
    # Not pinned to a tier name: whether a callback is due today depends on the
    # live table, so the invariant is the ordering, not the winner.
    from dialer.airtable import PILES, TIERS  # noqa: E402
    rank = [t for t, _ in TIERS]
    qq, _ = call("GET", "/api/queue?limit=25&pile=priority")
    ranks = [rank.index(l["tier"]) for l in qq["leads"]]
    check("the queue never puts a colder tier ahead of a hotter one",
          ranks, sorted(ranks))
    check("the session's first lead is the hottest tier that has rows",
          s["lead"]["tier"], qq["leads"][0]["tier"])
    first_company = s["lead"]["company"]

    again, _ = call("POST", "/api/session", {"target": 99})
    check("second START never resets the session", again.get("target"), 3)
    check("second START keeps the same lead", again["lead"]["company"], first_company)
    truthy("second START does not restart the warmup clock",
           again.get("warmup_remaining", 0) <= s.get("warmup_remaining", 0))

    print("\n=== 1b. the warmup is a real gate, not a screen ===")
    truthy("warmup counting down", 0 < s.get("warmup_remaining", 0) <= warmup)
    truthy("warmup screen gets the top of the queue", s.get("warmup_leads"))
    check("prep list is capped", len(s.get("warmup_leads") or []) <= 3, True)
    d, code = call("POST", "/api/dial", {})
    check("dial refused during the warmup", code, 425)
    check("refusal names the warmup", d.get("error"), "warmup")
    early, code = call("POST", "/api/warmup/done", {})
    check("warmup cannot be ended early", code, 425)
    p, _ = call("POST", "/api/pause", {})
    check("pause refused during the warmup", p.get("ok"), False)
    st, _ = call("GET", "/api/session")
    check("a refused pause is not consumed", st.get("pause_used"), False)

    time.sleep(s["warmup_remaining"] + 0.4)
    w, code = call("POST", "/api/warmup/done", {})
    check("warmup ends on its own clock", code, 200)
    check("warmup hands off to the breather", w.get("state"), "BREATHER")
    check("prep list is warmup-only", w.get("warmup_leads"), None)

    print("\n=== 1c. the two piles, and the callback that used to get buried ===")
    truthy("config exposes the piles", cfg0.get("piles"))
    check("two of them", len(cfg0.get("piles") or []), 2)
    check("priority is the default", cfg0.get("default_pile"), "priority")

    check("callback_due outranks every other tier", rank[0], "callback_due")
    for name, tiers in PILES.items():
        check(f"pile {name!r} can always reach a due callback",
              "callback_due" in tiers, True)
    check("cold pile does not include the warm tiers",
          set(PILES["cold"]) & {"mystery_no_reply", "hiring_signal", "call_today"},
          set())
    check("priority falls through to cold rather than dead-ending",
          PILES["priority"][-1], "queued")

    # THE REGRESSION THIS EXISTS FOR. Every non-terminal disposition writes
    # Status=Snoozed with a future Next Action, and nothing flips it back when
    # the date lands. Before callback_due, a dialed cold row matched no tier on
    # its callback date and was never dialed again - one attempt each, forever.
    from dialer import outcomes as _o  # noqa: E402
    cold_row = {"fields": {"Attempts": 1, "Lead Type": "Cold",
                           "Status": "Queued", "Notes": ""}}
    after = _o.build_payload(cold_row, "No Answer")
    check("a dialed cold row leaves the Status tiers", after["Status"], "Snoozed")
    truthy("...carrying a callback date", after.get("Next Action"))
    check("...and only the date-keyed tier can find it again",
          [t for t, c in TIERS if "Next Action" in c and "Status" not in c],
          ["callback_due"])

    print("\n=== 2. attempt counter is load-bearing (playbook: no VM on 1-2) ===")
    truthy("attempts is an int", isinstance(s["lead"]["attempts"], int))

    print("\n=== 3. auto-advance on a ring-out costs ZERO keys ===")
    d, _ = call("POST", "/api/dial", {})
    truthy("dial returned a phone", d.get("phone"))
    check("phone is E.164", d["phone"][:2], "+1")

    # THE 2026-08-05 REGRESSION, server half. A countdown that came back to life
    # in the browser fired a second dial while a call was live, and this endpoint
    # took it: spent an attempt, reset current_call, and the client then tore
    # down the call that was actually up. The browser has its own guards now, but
    # this is the one that cannot be raced by a late response or a stale timer.
    st_d, _ = call("GET", "/api/session")
    _, code = call("POST", "/api/dial", {})
    check("a second dial is refused while a call is in progress", code, 409)
    st_after, _ = call("GET", "/api/session")
    check("...and the refused dial is not counted", st_after["dials"], st_d["dials"])
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

    print("\n=== 15. client diagnostics reach the server log ===")
    log = ROOT / "state" / "server.log"
    before = log.stat().st_size if log.exists() else 0
    marker = f"dryrun-probe-{int(time.time())}"
    _, status = call("POST", "/api/client-log",
                     {"level": "error", "msg": marker, "detail": {"probe": True}})
    check("client-log accepts a line", status, 200)
    # Junk must not be able to stop the dialer - the endpoint swallows it and
    # still answers 200, because a logging failure mid-call is not worth a dial.
    _, junk_status = call("POST", "/api/client-log", {"level": None, "msg": None})
    check("client-log survives a malformed line", junk_status, 200)
    time.sleep(0.3)
    tail = log.read_text(errors="replace")[before:] if log.exists() else ""
    truthy("the line lands in state/server.log", marker in tail)

    print("\n" + "=" * 64)
    if FAILS:
        print(f"FAILED {len(FAILS)} of {CHECKS} checks")
        for f in FAILS:
            print("  - " + f)
        return 1
    print(f"ALL {CHECKS} CHECKS PASSED")
    return 0


if __name__ == "__main__":
    # This harness abandons sessions on purpose (block 10). Those abandons are
    # not real, and the START screen banners the most recent one until a full
    # session completes - so leaving them behind puts a lie on the one surface
    # whose whole job is to tell the truth about quitting.
    _ab = ROOT / "state" / "abandons.jsonl"
    _before = _ab.read_bytes() if _ab.exists() else None
    try:
        _code = main()
    finally:
        if _before is None:
            _ab.unlink(missing_ok=True)
        else:
            _ab.write_bytes(_before)
    sys.exit(_code)
