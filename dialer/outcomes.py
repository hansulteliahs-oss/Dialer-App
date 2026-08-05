"""
Disposition -> Airtable field mapping, next-action planning, and the retirement rule.

Pure functions, no network. This is the module that has to stay in lockstep with
scripts/cold_call_log.py in the AIOS repo, because both write the same rows and
the accountability stack (tools/tune_me_out_gate.py count_today,
tools/pull_activity_scoreboard.py, the call-nudge cron) reads what they write.

Two deliberate divergences from the CLI, both documented in the build plan:

1. The CLI exits 2 rather than log "Busy, Call Back" or "Conversation" without a
   date. A dialer cannot refuse - refusing means stalling, and stalling is a
   brake. So the dialer PRE-FILLS (+3d callback, +7d conversation), editable
   during the breather. The promise still lands in Next Action where the
   follow-up queue reads it, which is what the 2026-08-01 fix was for.

2. The retirement rule: Attempts >= 4 with a latest Disposition of "No Answer"
   closes the row. The CLI has no such cap.

Self-test:  ./.venv/bin/python3 -m dialer.outcomes --selftest
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta

# --- the live schema, re-read from Airtable 2026-08-04 -----------------------
# Writing a value not in this set would let a future typecast=true silently add a
# sixth junk option, which is how "Call back" and "Hung up on me" got into the
# schema before. Note "Busy, Call Back" is ONE option whose name contains a comma.
LIVE_DISPOSITIONS = (
    "No Answer",
    "Busy, Call Back",
    "Not interested",
    "Conversation",
    "Meeting Booked",
)
LIVE_STATUSES = ("Queued", "Call Today", "Done", "Snoozed")

# Someone committed to a future touch, or talked without naming a next step.
PROMISE_DISPOSITIONS = {"Busy, Call Back", "Conversation"}
# Nothing is owed. Status -> Done, Next Action cleared.
TERMINAL_DISPOSITIONS = {"Not interested"}
BOOKED = "Meeting Booked"

# Nobody promised anything but the row is not finished. Matches the CLI.
RETRY_DEFAULT_DAYS = {"No Answer": 2}
# The divergence: pre-filled instead of refused. Editable during the breather.
PREFILL_DAYS = {"Busy, Call Back": 3, "Conversation": 7}

# His rule: 4 dials with no pickup and they are done.
RETIREMENT_ATTEMPTS = 4
RETIREMENT_NOTE = "retired - no contact in 4 attempts"

# Keyboard -> disposition. Ordered by escalating engagement (didn't reach them ->
# reached them -> done with them), not by the schema's arbitrary option order.
# The selftest asserts every live option is reachable by exactly one key.
KEY_TO_DISPOSITION = {
    "1": "No Answer",
    "2": "Busy, Call Back",
    "3": "Conversation",
    "4": "Not interested",
    "5": "Meeting Booked",
}

# A row that ever had real human contact must never be closed by the attempt cap.
# There is no "ever talked" field and the plan forbids adding one, so the durable
# signal is the Notes history: every promise-class outcome this dialer writes
# leaves a "[date] <Disposition> - ..." line behind. Rows last touched by the CLI
# only carry a marker if a note was passed, so cap-retirement also checks the
# row's current Disposition as a second, shallower signal.
_REAL_CONTACT_RE = re.compile(
    r"\[\d{4}-\d{2}-\d{2}\]\s*(Conversation|Busy, Call Back|Meeting Booked)\b"
)


def today_iso() -> str:
    return date.today().isoformat()


def resolve_next_action(token: str | None) -> str | None:
    """YYYY-MM-DD | today | tomorrow | +Nd -> ISO date. None when not supplied.

    Raises ValueError on garbage. Same grammar as the CLI's --next-action.
    """
    if not token:
        return None
    t = str(token).strip().lower()
    if t == "today":
        return today_iso()
    if t == "tomorrow":
        return (date.today() + timedelta(days=1)).isoformat()
    if t.startswith("+") and t.endswith("d") and t[1:-1].isdigit():
        return (date.today() + timedelta(days=int(t[1:-1]))).isoformat()
    try:
        return datetime.strptime(str(token).strip(), "%Y-%m-%d").date().isoformat()
    except ValueError as e:
        raise ValueError(
            f"bad next action {token!r}: use YYYY-MM-DD, today, tomorrow, or +Nd"
        ) from e


def default_next_action(disposition: str) -> str | None:
    """The date the breather panel opens pre-filled with. None when terminal."""
    if disposition in TERMINAL_DISPOSITIONS or disposition == BOOKED:
        return None
    days = PREFILL_DAYS.get(disposition, RETRY_DEFAULT_DAYS.get(disposition))
    if days is None:
        return today_iso()
    return (date.today() + timedelta(days=days)).isoformat()


def plan_followup(disposition: str, next_action: str | None) -> tuple[str, str | None]:
    """(status, next_action_date) for a disposition.

    Mirrors cold_call_log.plan_followup() except that a promise with no date gets
    pre-filled here instead of refused.

    next_action_date is None - never "" - when the row is terminal. Airtable 422s
    on an empty string for a date column.
    """
    if disposition in TERMINAL_DISPOSITIONS or disposition == BOOKED:
        return "Done", None
    when = next_action or default_next_action(disposition)
    if when is None:
        # Unknown disposition, no date. Keep it visible rather than silently
        # closing it - failing loud beats losing the lead.
        return "Call Today", today_iso()
    status = "Call Today" if when <= today_iso() else "Snoozed"
    return status, when


def had_real_contact(fields: dict) -> bool:
    """True if this row ever reached a human, so the attempt cap must not close it."""
    if _REAL_CONTACT_RE.search(fields.get("Notes") or ""):
        return True
    prior = fields.get("Disposition")
    return prior in PROMISE_DISPOSITIONS or prior == BOOKED


def should_retire(fields: dict, disposition: str, new_attempts: int) -> bool:
    """The 4-attempt cap: 4 dials, still no pickup, close the row.

    Only "No Answer" can trigger it. Every other disposition either terminates on
    its own or represents contact worth keeping alive.
    """
    if disposition != "No Answer":
        return False
    if new_attempts < RETIREMENT_ATTEMPTS:
        return False
    return not had_real_contact(fields)


def build_payload(
    record: dict,
    disposition: str,
    note: str | None = None,
    next_action: str | None = None,
    next_note: str | None = None,
    dnc: bool = False,
) -> dict:
    """The exact Airtable PATCH body for one logged call.

    Writes Disposition, Last Call Date, Attempts+1, Notes (date-stamped append),
    Status, Next Action, Next Action Note - the same seven fields the CLI writes,
    so tune_me_out_gate.count_today and pull_activity_scoreboard keep working
    with zero changes.
    """
    if disposition not in LIVE_DISPOSITIONS:
        raise ValueError(
            f"{disposition!r} is not a live Disposition option. "
            f"Live: {', '.join(LIVE_DISPOSITIONS)}"
        )

    fields = record.get("fields", {}) or {}
    today = today_iso()
    prior_attempts = fields.get("Attempts") or 0
    new_attempts = prior_attempts + 1

    resolved = resolve_next_action(next_action) if next_action else None
    status, when = plan_followup(disposition, resolved)

    note_lines: list[str] = []
    if note and note.strip():
        # Promise-class outcomes get the disposition stamped into the line so
        # had_real_contact() can find it years later.
        if disposition in PROMISE_DISPOSITIONS or disposition == BOOKED:
            note_lines.append(f"[{today}] {disposition} - {note.strip()}")
        else:
            note_lines.append(f"[{today}] {note.strip()}")
    elif disposition in PROMISE_DISPOSITIONS or disposition == BOOKED:
        # No typed note, but this row reached a human. Leave the marker anyway.
        note_lines.append(f"[{today}] {disposition}")

    retiring = should_retire(fields, disposition, new_attempts)
    if retiring:
        status, when = "Done", None
        note_lines.append(f"[{today}] {RETIREMENT_NOTE}")

    payload: dict = {
        "Disposition": disposition,
        "Last Call Date": today,
        "Attempts": new_attempts,
        "Status": status,
        "Next Action": when,  # None, never "" - Airtable 422s on empty date
    }
    payload["Next Action Note"] = (
        (next_note or note or f"{disposition} - follow up") if when else ""
    )

    if note_lines:
        prior_notes = fields.get("Notes") or ""
        appended = "\n".join(note_lines)
        payload["Notes"] = f"{prior_notes}\n{appended}" if prior_notes else appended

    if dnc:
        payload["DNC"] = True

    return payload


# --- self-test ---------------------------------------------------------------

def _selftest() -> int:
    today = today_iso()
    plus = lambda n: (date.today() + timedelta(days=n)).isoformat()
    fails: list[str] = []

    def check(label, got, want):
        if got != want:
            fails.append(f"{label}\n     got  {got!r}\n     want {want!r}")

    # terminal clears the date as None, never ""
    check("not-interested status", plan_followup("Not interested", None), ("Done", None))
    check("booked status", plan_followup("Meeting Booked", None), ("Done", None))

    # no answer auto-schedules +2d and snoozes
    check("no-answer default", plan_followup("No Answer", None), ("Snoozed", plus(2)))

    # the divergence: promises pre-fill instead of refusing
    check("callback prefill", plan_followup("Busy, Call Back", None), ("Snoozed", plus(3)))
    check("conversation prefill", plan_followup("Conversation", None), ("Snoozed", plus(7)))

    # an explicit date wins, and today's date means Call Today not Snoozed
    check("explicit today", plan_followup("Conversation", today), ("Call Today", today))

    # terminal payload must send None for the date column and "" for the text one
    p = build_payload({"fields": {"Attempts": 1}}, "Not interested")
    check("terminal next action", p["Next Action"], None)
    check("terminal next note", p["Next Action Note"], "")
    check("attempts increment", p["Attempts"], 2)
    check("last call date", p["Last Call Date"], today)

    # retirement at 4 no-answers on a row that never reached a human
    p = build_payload({"fields": {"Attempts": 3}}, "No Answer")
    check("retire status", p["Status"], "Done")
    check("retire clears date", p["Next Action"], None)
    if RETIREMENT_NOTE not in p.get("Notes", ""):
        fails.append(f"retire note missing from {p.get('Notes')!r}")

    # 3 attempts is not yet the cap
    p = build_payload({"fields": {"Attempts": 2}}, "No Answer")
    check("attempt 3 not retired", p["Status"], "Snoozed")

    # a row that reached a human is never closed by the cap
    talked = {"fields": {"Attempts": 3, "Notes": "[2026-07-01] Conversation - wants a call back"}}
    p = build_payload(talked, "No Answer")
    check("real contact survives cap", p["Status"], "Snoozed")
    if RETIREMENT_NOTE in p.get("Notes", ""):
        fails.append("row with real contact was retired by the cap")

    # ...including via the shallower prior-disposition signal
    p = build_payload({"fields": {"Attempts": 3, "Disposition": "Conversation"}}, "No Answer")
    check("prior-disposition survives cap", p["Status"], "Snoozed")

    # promise outcomes always leave a findable marker, even with no typed note
    p = build_payload({"fields": {"Attempts": 0}}, "Conversation")
    if not _REAL_CONTACT_RE.search(p.get("Notes", "")):
        fails.append(f"conversation left no real-contact marker: {p.get('Notes')!r}")

    # unknown dispositions are refused, never typecast into the schema
    try:
        build_payload({"fields": {}}, "Hung up on me")
        fails.append("unknown disposition was accepted")
    except ValueError:
        pass

    # date grammar
    check("+5d", resolve_next_action("+5d"), plus(5))
    check("tomorrow", resolve_next_action("tomorrow"), plus(1))
    check("iso passthrough", resolve_next_action("2026-09-01"), "2026-09-01")
    try:
        resolve_next_action("next tuesday")
        fails.append("garbage date was accepted")
    except ValueError:
        pass

    # every live option is reachable by exactly one key (order is a UX choice)
    check("key coverage", sorted(KEY_TO_DISPOSITION.values()), sorted(LIVE_DISPOSITIONS))
    check("keys are unique", len(set(KEY_TO_DISPOSITION.values())), len(KEY_TO_DISPOSITION))

    if fails:
        print(f"FAIL ({len(fails)})")
        for f in fails:
            print("  - " + f)
        return 1
    print("outcomes.py selftest: all checks passed")
    return 0


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    print(__doc__)
