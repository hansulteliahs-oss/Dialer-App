"""
Airtable read/write for the No Brakes dialer. Live base, both directions.

A private store was rejected on purpose: it would re-create the 2026-08-01
callback-burial bug at 20x volume. The queue and the outcomes both live in the
same rows the rest of the AIOS reads.

Two hard rules enforced here:
  1. Never surface or dial a row with DNC = true. Filtered in the query AND
     re-checked immediately before device.connect(). Mirrors the absolute-refusal
     posture of tools/sdr_write.py.
  2. Writes only happen when DIALER_ARM_WRITE=1. Otherwise every PATCH is logged
     and dropped, same house pattern as CAL_ARM_WRITE / SDR_ARM_WRITE.

Failure posture: a failed write NEVER stops the session. It goes to
state/pending-writes.jsonl and a background thread retries it. The machine never
hands him a moment where stopping is easier than continuing.

Self-test (read-only, hits the live base):
    ./.venv/bin/python3 -m dialer.airtable --selftest
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.parse
import uuid
from pathlib import Path

import requests

from . import load_env, outcomes

ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = ROOT / "state"
PENDING_FILE = STATE_DIR / "pending-writes.jsonl"
# Where a queued write goes when the retry budget is spent. Nothing is ever
# deleted from the queue silently - a dropped outcome could be a Meeting Booked.
DROPPED_FILE = STATE_DIR / "dropped-writes.jsonl"

API_ROOT = "https://api.airtable.com/v0"

# Airtable evaluates TODAY() and NOW() in UTC - measured against the live base
# 2026-08-05: DATETIME_DIFF(TODAY(), NOW(), 'hours') came back -19 at 12:32 PT,
# i.e. TODAY() is anchored at UTC midnight, not his.
#
# That is a silent, time-of-day-dependent bug in every clause below. From 5:00pm
# PT (00:00 UTC) until the 9:00pm cutoff - four hours of a legal dialing window -
# TODAY() is already tomorrow, and all three date clauses invert:
#
#   * a callback due TOMORROW satisfies NOT(IS_AFTER({Next Action}, TODAY()))
#     and gets dialed a day early. Dialing a promise early is the exact mirror
#     image of the callback-burial bug this tier was built to fix.
#   * a row dialed at 4pm no longer IS_SAME as TODAY(), so a queue refill can
#     hand him a number he already called an hour ago.
#   * a Snoozed row whose promise is not yet due becomes dialable.
#
# So anchor every date comparison to his wall clock instead. Verified live:
# this expression equals the local date, IS_AFTER(tomorrow) is true, and
# IS_AFTER(today) is false.
PT_TODAY = ("DATETIME_PARSE(DATETIME_FORMAT("
            "SET_TIMEZONE(NOW(), 'America/Los_Angeles'), 'YYYY-MM-DD'), 'YYYY-MM-DD')")

# Fields the dialer reads. Everything the live screen and breather card need.
QUEUE_FIELDS = [
    "Company", "First Name", "Full Name", "Industry", "Phone", "Website",
    "Status", "Disposition", "Last Call Date", "Attempts", "Notes", "DNC",
    "Context Cue", "Lead Type", "Shop Result", "Leak Signal",
    "Next Action", "Next Action Note", "Date Added",
]

# Applied to every tier. Order matters only for readability.
#   - DNC rows never appear, at any tier, under any picker
#   - retired / closed rows (Status=Done) stay closed
#   - a row already dialed today never comes back around in the same session
#   - a Snoozed row with a future Next Action is a promise not yet due; dialing it
#     early is the mirror image of the callback-burial bug, so it waits its turn
#   - no phone number means nothing to dial
BASE_EXCLUSIONS = (
    "NOT({DNC})",
    "{Status}!='Done'",
    "{Phone}!=''",
    "NOT(IS_SAME({Last Call Date}, %s, 'day'))" % PT_TODAY,
    "NOT(AND({Status}='Snoozed', IS_AFTER({Next Action}, %s)))" % PT_TODAY,
)

# The ranked queue, hottest first. Defaults are already the best order, so "just
# hit Start" always works; the picker chooses which pile, it does not reorder.
TIERS = (
    # A callback whose date has arrived. Ranked above everything because it is
    # the only tier where he made a promise to a human.
    #
    # THIS TIER IS WHY THE COLD PILE IS NOT ONE-SHOT. Every non-terminal
    # disposition writes Status=Snoozed with a future Next Action, and nothing
    # anywhere flips Snoozed back when that date arrives - cold_call_log.py and
    # sdr_write.py both decide Call-Today-vs-Snoozed at write time and neither
    # owns the roll-forward. Tiers below match on Status, so before this tier
    # existed a dialed cold row matched nothing on its callback date and was
    # never dialed again: one attempt each, forever, and the 4-attempt
    # retirement could never fire. Keying tier 0 on the DATE rather than on
    # Status is what closes that, and it matches how the rest of the AIOS
    # queues (tools/pull_sdr_book.py reads Next Action, not Status).
    ("callback_due",
     "AND({Next Action}!='', NOT(IS_AFTER({Next Action}, %s)))" % PT_TODAY),
    # 89 rows he mystery-shopped that never replied to their own quote form.
    # The strongest opener in the playbook.
    ("mystery_no_reply",
     "AND({Lead Type}='Mystery Shopped', {Shop Result}='No Response')"),
    ("hiring_signal", "{Lead Type}='Hiring Signal'"),
    ("call_today", "{Status}='Call Today'"),
    ("queued", "{Status}='Queued'"),
)

TIER_LABELS = {
    "callback_due": "callback due",
    "mystery_no_reply": "shopped, never replied",
    "hiring_signal": "hiring signal",
    "call_today": "call today",
    "queued": "queued",
}

# The two piles the picker offers. Ordered tuples - the tier ranking survives
# inside each pile, so a due callback still outranks a shopped row.
#
# `priority` deliberately falls through to `queued` at the end rather than
# stopping when the warm tiers run dry. A 20-dial session that exhausts the warm
# rows must not hit the tally early and hand him a finished screen at 11 of 20;
# it keeps dialing. That is what makes it "prioritize" and not "only".
# `callback_due` is in BOTH piles on purpose. The point of the tier is that a
# promise made to a human is never buried, and that guarantee is worth more
# unconditional than it is tidy: picking the cold grind must not be a way to
# silently skip the callbacks the last cold grind created.
PILES = {
    "priority": ("callback_due", "mystery_no_reply", "hiring_signal",
                 "call_today", "queued"),
    "cold": ("callback_due", "queued"),
}
DEFAULT_PILE = "priority"

# --- the trade narrow (2026-08-16, made unconditional 2026-08-17) --------------
# The ICP went from five trades to three: HVAC, plumbing, electrical. Roofers and
# remodelers are out (remodelers run long sales cycles where the missed-call wedge
# barely lands; roofing is storm-lumpy).
#
# The Call List does NOT reflect that and deliberately never will: maps-lead-engine
# keeps sourcing all five because remodelers are its supply floor, and a cheap unsold
# row costs nothing. So 43.6% of the table (2,548 of 5,850 rows) is off-ICP and the
# narrow has to be enforced HERE, at the only place that spends his time.
#
# It applies to EVERY tier, with no exemptions (Eliahs, 2026-08-17). It shipped a day
# earlier with two, and both were measured and killed:
#
#   - `Lead Type='Mystery Shopped'` used to be exempt at any tier, on the grounds that
#     the shop was a real form against a real business and a ghosted roofer is the
#     strongest opener in the playbook. But the shop is sunk whether or not he dials,
#     and dialing does not recover it - it only spends the one thing the narrow exists
#     to protect. Measured on the default `priority` pile: 35 exempt roofing/remodeler
#     rows sat in the HOTTEST tiers, so they front-loaded to 19 of the first 60 leads.
#     A third of his dial day was going to trades he had decided not to sell to.
#   - `callback_due` used to be exempt, on the grounds that a promise to a human
#     outranks a segmentation decision made afterwards. That principle is right and it
#     simply did not apply: all 5 off-ICP rows in that tier were `Disposition='No
#     Answer'` auto-snooze rollforwards. Nobody had promised him anything. The tier is
#     keyed on the `Next Action` DATE, and every non-terminal disposition writes one,
#     so "has a callback date" never meant "a human asked for a callback".
#
# If a real promise to an off-ICP shop ever does get made, honor it by hand. Do not
# reinstate a blanket exemption - that is what put roofers back on top of the queue.
ICP_INDUSTRIES = ("HVAC", "Plumbing", "Electrical")
TRADE_NARROW = "OR(" + ", ".join(
    "{Industry}='%s'" % i for i in ICP_INDUSTRIES) + ")"

# Inside a tier, rows that carry an owner name go first.
#
# Calling a business with no human name to ask for is the hardest version of the
# call - harder than the fear of the call itself - and it is not a rare case:
# 3,067 of the 5,831 dialable rows have no First Name, so the old Date-Added-only
# sort was handing him a coin flip on every dial. `Full Name` does NOT rescue it;
# that column is a duplicate of `Company` on 99% of rows and never held a person.
#
# Ordering is the fix rather than a skip key. A skip key would be an escape hatch
# over half the pile, reachable at exactly the moment the fear is loudest, and
# "no key that ends a dial early" is the premise the whole app rests on.
#
# Unnamed rows still FOLLOW rather than being filtered out, for the same reason
# `priority` falls through to `queued`: the queue must never run dry mid-session.
# 2,764 named rows against a 40-dial day is ~69 days of runway, so in practice
# the second pass is insurance, not a path he walks.
#
# This is a sub-tier, not a global re-sort. Tier order stays absolute - a due
# callback still outranks a named cold row - and the name only breaks ties inside
# a single tier. That keeps the promise-to-a-human guarantee above comfort.
NAME_PASSES = ("{First Name}!=''", "{First Name}=''")


def normalize_phone(raw: str | None) -> str | None:
    """Airtable phoneNumber -> strict E.164 +1XXXXXXXXXX, or None if unusable.

    Twilio needs E.164 and the Function refuses anything else, so a row we cannot
    normalize must be dropped from the queue rather than dialed and failed.
    """
    if not raw:
        return None
    # [^0-9], not \D: Python's \D is Unicode-aware and does NOT strip fullwidth
    # or Arabic-Indic digits, so a Phone of "７６０-846-4537" survived as ten
    # "digits" and returned "+1７６０8464537" - not E.164, and dialed anyway
    # instead of being dropped from the queue the way the docstring promises.
    digits = re.sub(r"[^0-9]", "", str(raw))
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) != 10:
        return None
    if digits[0] in "01" or digits[3] in "01":  # invalid NANP area/exchange code
        return None
    return "+1" + digits


class AirtableError(RuntimeError):
    pass


class DialRefused(AirtableError):
    """The ROW must not be dialed - DNC, or no usable number.

    Deliberately distinct from "Airtable could not be reached to find out".
    Both used to raise AirtableError, and /api/dial treated them the same way:
    skip the lead, zero breather, next number. So with the wifi down the app
    burned through all 80 prefetched leads in seconds - none of them actually
    screened, none of them called - and landed on the tally screen looking like
    a finished session. A refusal consumes a lead; an outage must not.
    """


class AirtableClient:
    def __init__(self, api_key=None, base_id=None, table_id=None, arm_write=None):
        self.api_key = api_key or os.environ.get("AIRTABLE_API_KEY", "")
        self.base_id = base_id or os.environ.get("AIRTABLE_BASE_ID", "appEJYWOrT5NAbxOM")
        self.table_id = table_id or os.environ.get("AIRTABLE_TABLE_ID", "tblURF0GnyhgKIzJj")
        if arm_write is None:
            arm_write = os.environ.get("DIALER_ARM_WRITE", "0") == "1"
        self.arm_write = bool(arm_write)
        if not self.api_key:
            raise AirtableError("AIRTABLE_API_KEY is not set")
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        self._retry_thread = None
        self._stop = threading.Event()
        # Serialises flush_pending(). See the comment there: overlapping replays
        # double-count Attempts on a single dial.
        self._flush_lock = threading.Lock()
        # One keep-alive session for every call. urllib paid a fresh TCP + TLS
        # handshake per request, and the DNC re-check sits on the dial path -
        # most of its measured cost was the handshake, not Airtable. requests
        # re-establishes transparently if Airtable closes the idle connection;
        # the warm() pings from server.py keep that from happening mid-session.
        self._http = requests.Session()
        self._http.headers.update({
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "User-Agent": "no-brakes-dialer/1.0",
        })

    # --- transport -----------------------------------------------------------

    def _request(self, method: str, path: str, body: dict | None = None,
                 timeout: int = 30) -> dict:
        url = f"{API_ROOT}{path}"
        last = ""
        # `timeout` bounds one response, not the whole call. A 429 arrives
        # promptly as a real HTTP response, so it never touches the timeout
        # branch - it lands on the backoff below, which used to sleep for
        # whatever Retry-After said (Airtable's lockout is 30s) regardless of
        # the caller's budget. assert_dialable passes timeout=6 and documents a
        # "worst case under twenty seconds"; two 429s made it 60s+. He runs
        # parallel Claude sessions against this same base, so 429 is the normal
        # failure here, not the exotic one. Give the whole call a deadline and
        # never sleep past it.
        deadline = time.monotonic() + (timeout * 3)
        for attempt in range(3):
            try:
                r = self._http.request(method, url, json=body, timeout=timeout)
            except requests.RequestException as e:
                last = str(e)
                if attempt < 2 and self._nap(2 ** attempt, deadline):
                    continue
                raise AirtableError(f"{method} {path} -> {last}") from e
            if r.status_code == 429 and attempt < 2:
                if self._nap(int(r.headers.get("Retry-After", "5")), deadline):
                    continue
                raise AirtableError(f"{method} {path} -> 429: retry budget spent")
            if 500 <= r.status_code < 600 and attempt < 2:
                if self._nap(2 ** attempt, deadline):
                    continue
                raise AirtableError(f"{method} {path} -> {r.status_code}: retry budget spent")
            if r.status_code >= 400:
                last = r.text[:400]
                raise AirtableError(f"{method} {path} -> {r.status_code}: {last}")
            try:
                return r.json()
            except ValueError as e:
                # A 2xx with a body json can't parse (proxy hiccup, truncated
                # response) used to raise a bare JSONDecodeError from OUTSIDE
                # every except in this file. It escaped log_outcome's
                # `except AirtableError`, escaped _commit_pending, and 500'd
                # /api/outcome - losing the outcome without even queueing it.
                # Every failure out of this method is an AirtableError so the
                # callers' one catch is actually total.
                raise AirtableError(
                    f"{method} {path} -> {r.status_code} with an unreadable body"
                ) from e
        raise AirtableError(f"{method} {path}: retries exhausted ({last})")

    @staticmethod
    def _nap(seconds: float, deadline: float) -> bool:
        """Sleep out a backoff, but never past the call's deadline.

        Returns False when the budget is spent, which means "stop retrying"
        rather than "sleep anyway". Failing fast beats holding a dial hostage.
        """
        left = deadline - time.monotonic()
        if left <= 0:
            return False
        time.sleep(min(seconds, left))
        return True

    def warm(self) -> None:
        """Keep the TLS connection alive between dials. A tiny GET, called from
        a background thread while a session is active - never on the dial path,
        and a failure means nothing (the next real call just pays the handshake).
        """
        try:
            qs = urllib.parse.urlencode([("maxRecords", "1"), ("fields[]", "Phone")])
            self._request("GET", f"/{self.base_id}/{self.table_id}?{qs}", timeout=5)
        except Exception:  # noqa: BLE001
            pass

    # --- queue ---------------------------------------------------------------

    @staticmethod
    def _formula(tier_clause: str, industry: str | None) -> str:
        # The narrow is unconditional and has no opt-out parameter on purpose. It was
        # a per-tier flag for one day and the flag is precisely how 19 of the first 60
        # leads came back off-ICP. A filter that protects his dial time has to be the
        # one thing a caller cannot forget to pass.
        clauses = [tier_clause, *BASE_EXCLUSIONS, TRADE_NARROW]
        if industry:
            # Airtable escapes a quote inside a string literal by DOUBLING it.
            # Stripping it instead meant an Industry containing an apostrophe
            # could never match its own rows - a silently empty queue, reported
            # to him as "no dialable leads match those filters". Doubling is
            # also what keeps this from being an injection point: the only
            # character that could close the literal is now escaped, not deleted.
            clauses.append("{Industry}='%s'" % industry.replace("'", "''"))
        return "AND(" + ", ".join(clauses) + ")"

    def fetch_tier(self, tier_clause: str, limit: int, industry=None,
                   timeout: int = 30) -> list[dict]:
        params = [
            ("filterByFormula", self._formula(tier_clause, industry)),
            ("pageSize", str(min(limit, 100))),
            ("maxRecords", str(limit)),
            # oldest first: the list has been sitting since 2026-05, and the rows
            # at the bottom are the ones that have never once been tried
            ("sort[0][field]", "Date Added"),
            ("sort[0][direction]", "asc"),
        ]
        for f in QUEUE_FIELDS:
            params.append(("fields[]", f))
        qs = urllib.parse.urlencode(params)
        resp = self._request("GET", f"/{self.base_id}/{self.table_id}?{qs}",
                             timeout=timeout)
        return resp.get("records", [])

    def fetch_queue(self, limit: int = 25, industry: str | None = None,
                    pile: str | None = None, timeout: int = 30) -> list[dict]:
        """The ranked queue for one pile, tier by tier, deduped, capped at `limit`.

        An unknown pile falls back to `priority` rather than raising. A bad value
        must never be able to empty the queue - a session that cannot build a
        list is a session that stops, which is the one outcome this app exists to
        prevent.
        """
        wanted = PILES.get(pile or DEFAULT_PILE, PILES[DEFAULT_PILE])
        seen: set[str] = set()
        out: list[dict] = []
        for tier_name, clause in TIERS:
            if tier_name not in wanted:
                continue
            if len(out) >= limit:
                break
            # Named rows of THIS tier, then unnamed rows of this tier, then on to
            # the next tier. Two cheap filtered reads beat one wide read plus a
            # local sort: a tier can be 5,710 rows deep and only `limit` of them
            # are ever wanted, so paging the whole tier back to sort it would
            # trade a fear problem for a latency problem on the dial path.
            for name_clause in NAME_PASSES:
                if len(out) >= limit:
                    break
                scoped = "AND(%s, %s)" % (clause, name_clause)
                for rec in self.fetch_tier(scoped, limit - len(out), industry,
                                           timeout):
                    if rec["id"] in seen:
                        continue
                    f = rec.get("fields", {}) or {}
                    # Belt and braces: the formula already excludes these, but a DNC
                    # row must never reach the UI even if a formula is edited badly.
                    if f.get("DNC"):
                        continue
                    # Same reasoning for the trade narrow. This is the check that is
                    # true independent of whether TRADE_NARROW composed correctly into
                    # the server-side formula, and it is the one that actually holds
                    # the guarantee "he never dials a trade he does not sell to".
                    if (f.get("Industry") or "") not in ICP_INDUSTRIES:
                        continue
                    phone = normalize_phone(f.get("Phone"))
                    if not phone:
                        continue
                    seen.add(rec["id"])
                    out.append({
                        "id": rec["id"],
                        "tier": tier_name,
                        "tier_label": TIER_LABELS[tier_name],
                        "company": f.get("Company") or "(no company)",
                        "first_name": (f.get("First Name") or "").strip(),
                        "full_name": (f.get("Full Name") or "").strip(),
                        "industry": f.get("Industry") or "",
                        "phone": phone,
                        "phone_display": f.get("Phone") or phone,
                        "website": f.get("Website") or "",
                        "context_cue": f.get("Context Cue") or "",
                        "leak_signal": f.get("Leak Signal") or "",
                        "lead_type": f.get("Lead Type") or "",
                        "shop_result": f.get("Shop Result") or "",
                        "attempts": f.get("Attempts") or 0,
                        "notes": f.get("Notes") or "",
                        "next_action": f.get("Next Action") or "",
                        "next_action_note": f.get("Next Action Note") or "",
                        "status": f.get("Status") or "",
                        "disposition": f.get("Disposition") or "",
                    })
                    if len(out) >= limit:
                        break
        return out

    def get_record(self, record_id: str, timeout: int = 30) -> dict:
        return self._request("GET", f"/{self.base_id}/{self.table_id}/{record_id}",
                             timeout=timeout)

    def assert_dialable(self, record_id: str) -> str:
        """Re-check DNC immediately before connect(). Returns the E.164 number.

        The second half of hard refusal #1. The queue filter can go stale between
        pull and dial - he runs parallel Claude sessions and the SDR agent writes
        to these same rows.

        timeout=6, not the default 30: this is the one Airtable call on the dial
        path, and the retry loop can stack three timeouts plus backoff. 30s each
        meant a flaky patch could hold a dial hostage for a minute and a half;
        6s keeps the worst case under twenty seconds, and the check still fails
        CLOSED - no answer from Airtable means no dial, never a blind one.
        """
        rec = self.get_record(record_id, timeout=6)
        f = rec.get("fields", {}) or {}
        if f.get("DNC"):
            raise DialRefused(
                f"REFUSED: {f.get('Company', record_id)} is marked DNC. Never dialing this row."
            )
        phone = normalize_phone(f.get("Phone"))
        if not phone:
            raise DialRefused(f"REFUSED: {record_id} has no dialable phone number")
        return phone

    # --- writes --------------------------------------------------------------

    def log_outcome(self, record_id: str, disposition: str, note=None,
                    next_action=None, next_note=None, dnc=False) -> dict:
        """Write one call's outcome. Never raises - a failed write is queued.

        Returns {ok, armed, queued, payload} so the UI can show what happened
        without ever blocking on it.
        """
        try:
            rec = self.get_record(record_id)
        except Exception as e:  # noqa: BLE001 - the docstring promises no raise
            # Fall back to a bare record so the outcome is not lost. Attempts will
            # be recomputed on replay from the live row.
            rec = {"id": record_id, "fields": {}}
            self._queue_pending(record_id, disposition, note, next_action, next_note, dnc,
                                reason=f"read failed: {e}")
            return {"ok": False, "armed": self.arm_write, "queued": True,
                    "error": str(e), "payload": None}

        try:
            payload = outcomes.build_payload(
                rec, disposition, note=note, next_action=next_action,
                next_note=next_note, dnc=dnc,
            )
        except ValueError as e:
            # The docstring above promises this never raises, and it did: an
            # unparseable follow-up date reached resolve_next_action() and the
            # ValueError escaped all the way out to a 500 on /api/outcome,
            # losing the call. The date is the least important of the seven
            # fields - Disposition, Last Call Date and Attempts are what the
            # accountability stack counts - so drop the date, keep the dial,
            # and say so in the Notes rather than throwing the call away.
            print(f"[bad follow-up date, logging the dial without it] {e}", flush=True)
            salvage = f"{note.strip()} " if note and note.strip() else ""
            payload = outcomes.build_payload(
                rec, disposition, note=f"{salvage}(follow-up date rejected: {e})",
                next_action=None, next_note=next_note, dnc=dnc,
            )

        if not self.arm_write:
            print(f"[DIALER_ARM_WRITE=0] would PATCH {record_id}: "
                  f"{json.dumps(payload, default=str)}")
            return {"ok": True, "armed": False, "queued": False, "payload": payload}

        try:
            self._request("PATCH", f"/{self.base_id}/{self.table_id}/{record_id}",
                          {"fields": payload})
            return {"ok": True, "armed": True, "queued": False, "payload": payload}
        except Exception as e:  # noqa: BLE001 - see the docstring: this NEVER raises
            self._queue_pending(record_id, disposition, note, next_action, next_note, dnc,
                                reason=str(e))
            return {"ok": False, "armed": True, "queued": True,
                    "error": str(e), "payload": payload}

    def _queue_pending(self, record_id, disposition, note, next_action, next_note,
                       dnc, reason) -> None:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        entry = {
            # Identity, so a replay running concurrently with this append can
            # tell "already handled" from "arrived while I was working".
            "uid": uuid.uuid4().hex,
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "record_id": record_id,
            "disposition": disposition,
            "note": note,
            "next_action": next_action,
            "next_note": next_note,
            "dnc": dnc,
            "reason": reason,
            "attempts": 0,
        }
        try:
            with PENDING_FILE.open("a") as fh:
                fh.write(json.dumps(entry) + "\n")
            print(f"[queued for retry] {record_id}: {reason}")
        except Exception as e:  # noqa: BLE001
            # This is the last net under a failed write. If it throws too (disk
            # full, state/ not writable) it must still not take the session
            # down - the outcome is already lost at that point and stalling the
            # machine on top of it helps nobody.
            print(f"[LOST OUTCOME] {record_id} {disposition}: {reason} "
                  f"(and the retry queue is unwritable: {e})", flush=True)

    def flush_pending(self) -> dict:
        """Replay queued writes. Rebuilds each payload from the live row so the
        attempt count stays correct even if the row changed meanwhile."""
        if not PENDING_FILE.exists():
            return {"replayed": 0, "remaining": 0}
        if not self.arm_write:
            return {"replayed": 0, "remaining": sum(1 for _ in PENDING_FILE.open())}

        # Only one replay at a time. The retry thread fires every 45s, bootstrap
        # calls this, and GET /api/pending calls it from a request thread - all
        # three could run together, and each rebuilds Attempts from the live row
        # before PATCHing. Two overlapping replays of the same entry therefore
        # incremented Attempts TWICE for one physical dial and appended the note
        # twice, which can also trip the 4-attempt retirement a call early.
        if not self._flush_lock.acquire(blocking=False):
            return {"replayed": 0, "remaining": -1, "busy": True}
        try:
            lines = [l for l in PENDING_FILE.read_text().splitlines() if l.strip()]
            still: list[str] = []
            done: set[str] = set()
            replayed = 0
            for line in lines:
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                done.add(e.get("uid") or line)
                try:
                    rec = self.get_record(e["record_id"])
                    payload = outcomes.build_payload(
                        rec, e["disposition"], note=e.get("note"),
                        next_action=e.get("next_action"), next_note=e.get("next_note"),
                        dnc=e.get("dnc", False),
                    )
                    self._request("PATCH",
                                  f"/{self.base_id}/{self.table_id}/{e['record_id']}",
                                  {"fields": payload})
                    replayed += 1
                except Exception as err:  # noqa: BLE001 - never let a replay kill the session
                    e["attempts"] = e.get("attempts", 0) + 1
                    e["reason"] = str(err)
                    if e["attempts"] < 20:
                        still.append(json.dumps(e))
                    else:
                        # 20 failures is ~15 minutes of outage. Deleting the line
                        # here threw the outcome away in silence - it could be a
                        # Meeting Booked. Park it where a human can find it and
                        # say so loudly instead.
                        self._drop(e)

            # A commit that failed WHILE this replay was running appended to the
            # file we are about to rewrite. Re-read and carry those forward, or
            # write_text() would silently erase them.
            fresh = []
            for line in PENDING_FILE.read_text().splitlines():
                if not line.strip():
                    continue
                try:
                    uid = json.loads(line).get("uid") or line
                except json.JSONDecodeError:
                    continue
                if uid not in done:
                    fresh.append(line)

            out = still + fresh
            tmp = PENDING_FILE.with_suffix(".tmp")
            tmp.write_text("\n".join(out) + ("\n" if out else ""))
            tmp.replace(PENDING_FILE)
            return {"replayed": replayed, "remaining": len(out)}
        finally:
            self._flush_lock.release()

    def _drop(self, entry: dict) -> None:
        """Give up on a queued write - visibly. Never silently."""
        try:
            with DROPPED_FILE.open("a") as fh:
                fh.write(json.dumps(entry) + "\n")
        except Exception:  # noqa: BLE001
            pass
        print(f"[GAVE UP after {entry.get('attempts')} tries] "
              f"{entry.get('record_id')} {entry.get('disposition')} -> "
              f"{DROPPED_FILE.name}: {entry.get('reason')}", flush=True)

    def dropped_count(self) -> int:
        """How many outcomes were abandoned. Surfaced in the window so a lost
        write is visible rather than invisible."""
        try:
            return sum(1 for l in DROPPED_FILE.read_text().splitlines() if l.strip())
        except OSError:
            return 0

    def start_retry_thread(self, interval: int = 45) -> None:
        if self._retry_thread:
            return

        def loop():
            while not self._stop.wait(interval):
                try:
                    r = self.flush_pending()
                    if r["replayed"]:
                        print(f"[retry] replayed {r['replayed']}, "
                              f"{r['remaining']} remaining")
                except Exception as e:  # noqa: BLE001
                    print(f"[retry] error: {e}")

        self._retry_thread = threading.Thread(target=loop, daemon=True)
        self._retry_thread.start()

    def stop(self) -> None:
        self._stop.set()


def _selftest() -> int:
    from . import load_env
    load_env(ROOT / ".env")

    fails = []
    # phone normalization
    cases = [
        ("(760) 846-4537", "+17608464537"),
        ("+1 760-846-4537", "+17608464537"),
        ("7608464537", "+17608464537"),
        ("17608464537", "+17608464537"),
        ("760-846-453", None),        # too short
        ("", None),
        (None, None),
        ("(060) 846-4537", None),     # invalid area code
    ]
    for raw, want in cases:
        got = normalize_phone(raw)
        if got != want:
            fails.append(f"normalize_phone({raw!r}) -> {got!r}, want {want!r}")

    c = AirtableClient(arm_write=False)
    print(f"base={c.base_id} table={c.table_id} arm_write={c.arm_write}")

    # every tier's formula must parse server-side and exclude DNC
    total = 0
    for name, clause in TIERS:
        recs = c.fetch_tier(clause, 3)
        dnc = [r for r in recs if (r.get("fields") or {}).get("DNC")]
        if dnc:
            fails.append(f"tier {name} returned {len(dnc)} DNC rows")
        print(f"  tier {name:<20} sample={len(recs)}")
        total += len(recs)

    q = c.fetch_queue(limit=12)
    print(f"\nranked queue, first {len(q)}:")
    for i, l in enumerate(q, 1):
        nm = l["first_name"] or "(no name)"
        print(f"  {i:>2}. [{l['tier_label']:<21}] {l['company'][:34]:<34} "
              f"{nm:<14} {l['phone']}  att={l['attempts']}")

    if any(l["phone"] is None for l in q):
        fails.append("queue contains an unnormalizable phone")
    if len({l["id"] for l in q}) != len(q):
        fails.append("queue contains duplicate record ids")

    print("\npiles:")
    for name in PILES:
        p = c.fetch_queue(limit=8, pile=name)
        tiers = [l["tier"] for l in p]
        print(f"  {name:<9} {len(p):>2} leads, tiers={sorted(set(tiers))}")
        stray = set(tiers) - set(PILES[name])
        if stray:
            fails.append(f"pile {name} returned out-of-pile tiers {stray}")
        if any(t == "queued" for t in tiers[:-1]) and name == "priority":
            # queued is the fallthrough - it must never outrank a warm tier
            first_queued = tiers.index("queued")
            if any(t != "queued" for t in tiers[first_queued:]):
                fails.append("priority put a warm tier after the cold fallthrough")

    # An unknown pile must degrade to priority, never to an empty queue.
    if not c.fetch_queue(limit=3, pile="nonsense"):
        fails.append("an unknown pile emptied the queue instead of falling back")

    # The promise guarantee: a due callback is reachable from EVERY pile.
    for name in PILES:
        if "callback_due" not in PILES[name]:
            fails.append(f"pile {name} cannot reach a due callback")

    if fails:
        print(f"\nFAIL ({len(fails)})")
        for f in fails:
            print("  - " + f)
        return 1
    print("\nairtable.py selftest: all checks passed")
    return 0


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    print(__doc__)
