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
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import load_env, outcomes

ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = ROOT / "state"
PENDING_FILE = STATE_DIR / "pending-writes.jsonl"

API_ROOT = "https://api.airtable.com/v0"

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
    "NOT(IS_SAME({Last Call Date}, TODAY(), 'day'))",
    "NOT(AND({Status}='Snoozed', IS_AFTER({Next Action}, TODAY())))",
)

# The ranked queue. Defaults are already the best order, so "just hit Start"
# always works; the pickers narrow it, they do not reorder it.
TIERS = (
    # 89 rows he mystery-shopped that never replied to their own quote form.
    # The strongest opener in the playbook.
    ("mystery_no_reply",
     "AND({Lead Type}='Mystery Shopped', {Shop Result}='No Response')"),
    ("hiring_signal", "{Lead Type}='Hiring Signal'"),
    ("call_today", "{Status}='Call Today'"),
    ("queued", "{Status}='Queued'"),
)

TIER_LABELS = {
    "mystery_no_reply": "shopped, never replied",
    "hiring_signal": "hiring signal",
    "call_today": "call today",
    "queued": "queued",
}


def normalize_phone(raw: str | None) -> str | None:
    """Airtable phoneNumber -> strict E.164 +1XXXXXXXXXX, or None if unusable.

    Twilio needs E.164 and the Function refuses anything else, so a row we cannot
    normalize must be dropped from the queue rather than dialed and failed.
    """
    if not raw:
        return None
    digits = re.sub(r"\D", "", str(raw))
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) != 10:
        return None
    if digits[0] in "01" or digits[3] in "01":  # invalid NANP area/exchange code
        return None
    return "+1" + digits


class AirtableError(RuntimeError):
    pass


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

    # --- transport -----------------------------------------------------------

    def _request(self, method: str, path: str, body: dict | None = None,
                 timeout: int = 30) -> dict:
        url = f"{API_ROOT}{path}"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "User-Agent": "no-brakes-dialer/1.0",
        }
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        last = ""
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    return json.loads(r.read().decode())
            except urllib.error.HTTPError as e:
                last = e.read().decode("utf-8", errors="replace")[:400]
                if e.code == 429 and attempt < 2:
                    time.sleep(int(e.headers.get("Retry-After", "5")))
                    continue
                if 500 <= e.code < 600 and attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise AirtableError(f"{method} {path} -> {e.code}: {last}") from e
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                last = str(e)
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise AirtableError(f"{method} {path} -> {last}") from e
        raise AirtableError(f"{method} {path}: retries exhausted ({last})")

    # --- queue ---------------------------------------------------------------

    @staticmethod
    def _formula(tier_clause: str, industry: str | None, lead_type: str | None) -> str:
        clauses = [tier_clause, *BASE_EXCLUSIONS]
        if industry:
            clauses.append("{Industry}='%s'" % industry.replace("'", ""))
        if lead_type:
            clauses.append("{Lead Type}='%s'" % lead_type.replace("'", ""))
        return "AND(" + ", ".join(clauses) + ")"

    def fetch_tier(self, tier_clause: str, limit: int,
                   industry=None, lead_type=None) -> list[dict]:
        params = [
            ("filterByFormula", self._formula(tier_clause, industry, lead_type)),
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
        resp = self._request("GET", f"/{self.base_id}/{self.table_id}?{qs}")
        return resp.get("records", [])

    def fetch_queue(self, limit: int = 25, industry: str | None = None,
                    lead_type: str | None = None) -> list[dict]:
        """The ranked queue, tier by tier, deduped, capped at `limit`."""
        seen: set[str] = set()
        out: list[dict] = []
        for tier_name, clause in TIERS:
            if len(out) >= limit:
                break
            for rec in self.fetch_tier(clause, limit - len(out), industry, lead_type):
                if rec["id"] in seen:
                    continue
                f = rec.get("fields", {}) or {}
                # Belt and braces: the formula already excludes these, but a DNC
                # row must never reach the UI even if a formula is edited badly.
                if f.get("DNC"):
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

    def get_record(self, record_id: str) -> dict:
        return self._request("GET", f"/{self.base_id}/{self.table_id}/{record_id}")

    def assert_dialable(self, record_id: str) -> str:
        """Re-check DNC immediately before connect(). Returns the E.164 number.

        The second half of hard refusal #1. The queue filter can go stale between
        pull and dial - he runs parallel Claude sessions and the SDR agent writes
        to these same rows.
        """
        rec = self.get_record(record_id)
        f = rec.get("fields", {}) or {}
        if f.get("DNC"):
            raise AirtableError(
                f"REFUSED: {f.get('Company', record_id)} is marked DNC. Never dialing this row."
            )
        phone = normalize_phone(f.get("Phone"))
        if not phone:
            raise AirtableError(f"REFUSED: {record_id} has no dialable phone number")
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
        except AirtableError as e:
            # Fall back to a bare record so the outcome is not lost. Attempts will
            # be recomputed on replay from the live row.
            rec = {"id": record_id, "fields": {}}
            self._queue_pending(record_id, disposition, note, next_action, next_note, dnc,
                                reason=f"read failed: {e}")
            return {"ok": False, "armed": self.arm_write, "queued": True,
                    "error": str(e), "payload": None}

        payload = outcomes.build_payload(
            rec, disposition, note=note, next_action=next_action,
            next_note=next_note, dnc=dnc,
        )

        if not self.arm_write:
            print(f"[DIALER_ARM_WRITE=0] would PATCH {record_id}: "
                  f"{json.dumps(payload, default=str)}")
            return {"ok": True, "armed": False, "queued": False, "payload": payload}

        try:
            self._request("PATCH", f"/{self.base_id}/{self.table_id}/{record_id}",
                          {"fields": payload})
            return {"ok": True, "armed": True, "queued": False, "payload": payload}
        except AirtableError as e:
            self._queue_pending(record_id, disposition, note, next_action, next_note, dnc,
                                reason=str(e))
            return {"ok": False, "armed": True, "queued": True,
                    "error": str(e), "payload": payload}

    def _queue_pending(self, record_id, disposition, note, next_action, next_note,
                       dnc, reason) -> None:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        entry = {
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
        with PENDING_FILE.open("a") as fh:
            fh.write(json.dumps(entry) + "\n")
        print(f"[queued for retry] {record_id}: {reason}")

    def flush_pending(self) -> dict:
        """Replay queued writes. Rebuilds each payload from the live row so the
        attempt count stays correct even if the row changed meanwhile."""
        if not PENDING_FILE.exists():
            return {"replayed": 0, "remaining": 0}
        if not self.arm_write:
            return {"replayed": 0, "remaining": sum(1 for _ in PENDING_FILE.open())}

        lines = [l for l in PENDING_FILE.read_text().splitlines() if l.strip()]
        still: list[str] = []
        replayed = 0
        for line in lines:
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
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
        PENDING_FILE.write_text("\n".join(still) + ("\n" if still else ""))
        return {"replayed": replayed, "remaining": len(still)}

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
