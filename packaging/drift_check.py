#!/usr/bin/env python3
"""Cross-repo drift check. Nothing used to check any of this.

    ./.venv/bin/python3 packaging/drift_check.py

Three contracts in this repo are second copies of something that lives in the
AIOS repo (~/AI Operating System), and the README says so in plain text while
naming no mechanism that enforces it:

  1. dialer/outcomes.py  vs  scripts/cold_call_log.py
     The same seven Airtable fields, encoded independently. The dialer is the
     primary writer and the CLI is the fallback; if they disagree about what a
     disposition means, the same call logged through two paths produces two
     different rows, and tune_me_out_gate.py / the call-nudge cron / the
     activity scoreboard all read the result.

  2. dialer/outcomes.py  vs  the LIVE Airtable schema
     Writing a select value that is not an option is a 422 mid-session. Worse,
     a future typecast=true would silently invent the option instead - which is
     how "Call back" and "Hung up on me" got into the schema before.

  3. static/playbook.js  vs  references/cold-call-playbook.md
     Every line of the talk track on screen is a hand-compression of the
     markdown. Change the objections there and this repo has to move with it,
     by hand, in the other repo.

Two deliberate divergences are encoded here as EXPECTED, not failures - both
documented in outcomes.py's own module docstring:
  * a promise with no date: the CLI exits 2 and refuses; the dialer pre-fills,
    because a dialer that refuses is a dialer that stalls, and stalling is a
    brake.
  * the 4-attempt retirement rule, which the CLI does not have at all.

Exits non-zero on real drift. Skips (loudly, exit 0) if the AIOS repo is not
on this machine, so a clone somewhere else still runs its own tests.
"""
from __future__ import annotations

import ast
import importlib.util
import os
import re
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

AIOS = Path(os.environ.get("AIOS_ROOT", Path.home() / "AI Operating System"))
CLI = AIOS / "scripts" / "cold_call_log.py"
PLAYBOOK_MD = AIOS / "references" / "cold-call-playbook.md"

FAILS: list[str] = []
CHECKS = 0


def check(label, got, want):
    global CHECKS
    CHECKS += 1
    if got != want:
        FAILS.append(f"{label}\n     dialer: {got!r}\n     AIOS:   {want!r}")
        print(f"  DRIFT {label}\n          dialer: {got!r}\n          AIOS:   {want!r}")
    else:
        print(f"  ok    {label}")


def literals(path: Path, names: set[str]) -> dict:
    """Module-level constants, read without importing (no env, no side effects)."""
    tree = ast.parse(path.read_text())
    out = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id in names:
                    try:
                        out[t.id] = ast.literal_eval(node.value)
                    except ValueError:
                        pass
    return out


def load_cli():
    spec = importlib.util.spec_from_file_location("aios_cold_call_log", CLI)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)          # main() is behind __main__, safe to import
    return mod


def main() -> int:
    from dialer import outcomes

    if not AIOS.is_dir():
        print(f"SKIP: no AIOS repo at {AIOS} - nothing to compare against.")
        print("      (set AIOS_ROOT to point at it)")
        return 0

    # --- 1. the write contract, dialer vs CLI --------------------------------
    print("=== 1. dialer/outcomes.py vs scripts/cold_call_log.py ===")
    if not CLI.exists():
        FAILS.append(f"{CLI} is missing - the fallback writer is gone")
        print(f"  DRIFT {CLI} does not exist")
    else:
        want = literals(CLI, {"LIVE_DISPOSITIONS", "PROMISE_DISPOSITIONS",
                              "TERMINAL_DISPOSITIONS", "RETRY_DEFAULT_DAYS"})
        check("the five live Disposition options match",
              set(outcomes.LIVE_DISPOSITIONS), set(want["LIVE_DISPOSITIONS"]))
        check("promise dispositions match",
              outcomes.PROMISE_DISPOSITIONS, want["PROMISE_DISPOSITIONS"])
        check("terminal dispositions match",
              outcomes.TERMINAL_DISPOSITIONS, want["TERMINAL_DISPOSITIONS"])
        check("the no-answer retry offset matches (README says +1d; both say +2d)",
              outcomes.RETRY_DEFAULT_DAYS, want["RETRY_DEFAULT_DAYS"])

        cli = load_cli()
        plus = lambda n: (date.today() + timedelta(days=n)).isoformat()
        today = date.today().isoformat()
        # Same input -> same (status, date), except where the divergence is
        # documented. A promise WITH a date must agree exactly.
        matrix = [
            ("No Answer", None),
            ("Not interested", None),
            ("Meeting Booked", None),
            ("Conversation", plus(3)),
            ("Busy, Call Back", plus(10)),
            ("Conversation", today),
            ("No Answer", plus(1)),
        ]
        for disp, when in matrix:
            mine = outcomes.plan_followup(disp, when)
            theirs = cli.plan_followup(disp, when)[:2]
            check(f"plan_followup({disp!r}, {when!r})", mine, theirs)

        # ...and the two documented divergences are still exactly that.
        for disp in sorted(outcomes.PROMISE_DISPOSITIONS):
            needs_date = cli.plan_followup(disp, None)[2]
            status, when = outcomes.plan_followup(disp, None)
            check(f"documented divergence: CLI refuses {disp!r} with no date", needs_date, True)
            check(f"documented divergence: the dialer pre-fills {disp!r} instead",
                  (status, bool(when)), ("Snoozed", True))

    # --- 2. the write contract vs the live schema ----------------------------
    print("\n=== 2. dialer/outcomes.py vs the LIVE Airtable schema ===")
    try:
        import requests
        from dialer import load_env
        load_env(ROOT / ".env")
        key = os.environ.get("AIRTABLE_API_KEY")
        base = os.environ.get("AIRTABLE_BASE_ID", "appEJYWOrT5NAbxOM")
        table = os.environ.get("AIRTABLE_TABLE_ID", "tblURF0GnyhgKIzJj")
        if not key:
            print("  SKIP  no AIRTABLE_API_KEY")
        else:
            r = requests.get(f"https://api.airtable.com/v0/meta/bases/{base}/tables",
                             headers={"Authorization": f"Bearer {key}"}, timeout=25)
            r.raise_for_status()
            t = next(x for x in r.json()["tables"] if x["id"] == table)
            fields = {f["name"]: f for f in t["fields"]}
            opts = lambda n: [o["name"]
                              for o in fields[n].get("options", {}).get("choices", [])]
            check("Disposition options", sorted(outcomes.LIVE_DISPOSITIONS),
                  sorted(opts("Disposition")))
            check("Status options", sorted(outcomes.LIVE_STATUSES), sorted(opts("Status")))
            for name in ("Disposition", "Last Call Date", "Attempts", "Notes",
                         "Status", "Next Action", "Next Action Note", "DNC", "Phone"):
                check(f"the contract still has a {name!r} field", name in fields, True)
    except Exception as e:  # noqa: BLE001
        print(f"  SKIP  could not reach Airtable ({e})")

    # --- 3. the talk track ---------------------------------------------------
    print("\n=== 3. static/playbook.js vs references/cold-call-playbook.md ===")
    if not PLAYBOOK_MD.exists():
        FAILS.append(f"{PLAYBOOK_MD} is missing - the talk track's source of truth is gone")
        print(f"  DRIFT {PLAYBOOK_MD} does not exist")
    else:
        md = PLAYBOOK_MD.read_text()
        js = (ROOT / "static" / "playbook.js").read_text()
        # The markdown's own objection headings, normalised to a few keywords.
        heads = re.findall(r"^### \"(.+?)\"\s*$", md, re.M)
        def keywords(s):
            s = s.lower().replace("’", "'")
            return {w for w in re.findall(r"[a-z]+", s) if len(w) > 3}
        jsl = js.lower().replace("’", "'")
        missing = []
        for h in heads:
            kw = keywords(h)
            # An objection is "carried" if most of its distinctive words appear.
            hits = sum(1 for w in kw if w in jsl)
            if not kw or hits < max(1, len(kw) // 2):
                missing.append(h)
        check(f"all {len(heads)} playbook objections are carried on screen", missing, [])
        # The DNC objection lives under the compliance section, not the objection
        # list, but the screen must still carry it - it is the one with a legal edge.
        check("the take-me-off-your-list response is on screen",
              "take me off" in jsl, True)
        check("the screen still says press D for DNC", "press d" in jsl, True)

    print("\n" + "=" * 64)
    if FAILS:
        print(f"DRIFT DETECTED - {len(FAILS)} of {CHECKS} checks")
        for f in FAILS:
            print("  - " + f)
        print("\nThese two repos encode the same contract independently.")
        print("Fix BOTH sides, or the same call logged two ways writes two rows.")
        return 1
    print(f"no drift - all {CHECKS} checks agree across both repos")
    return 0


if __name__ == "__main__":
    sys.exit(main())
