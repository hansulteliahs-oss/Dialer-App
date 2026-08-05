#!/usr/bin/env python3
"""
Live-call verification - plan verification step 2 + step 4.

Seeds Eliahs's own cell as the #1 lead, waits for him to place one real call
through the app, then verifies the Airtable write AND that the AIOS
accountability stack still sees the dial.

    ./.venv/bin/python3 packaging/live_check.py seed      # before the call
    ./.venv/bin/python3 packaging/live_check.py verify    # after the call
    ./.venv/bin/python3 packaging/live_check.py cleanup   # delete the test row

The seeded row carries Lead Type=Mystery Shopped + Shop Result=No Response so it
sorts to the top of tier 1, and a Date Added far in the past so it beats the
other 89. Always deleted by `cleanup`.
"""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dialer import load_env  # noqa: E402

load_env(ROOT / ".env")

from dialer.airtable import AirtableClient  # noqa: E402

MARKER = "NO BRAKES LIVE TEST"
AIOS = Path.home() / "AI Operating System"


def find_test_rows(air: AirtableClient) -> list[dict]:
    import urllib.parse
    f = f"FIND('{MARKER}', {{Company}})>0"
    qs = urllib.parse.urlencode({"filterByFormula": f, "pageSize": "10"})
    return air._request("GET", f"/{air.base_id}/{air.table_id}?{qs}").get("records", [])


def seed(air: AirtableClient, target: str | None = None) -> int:
    """Seed the test row. Defaults to TWILIO_CALLER_ID, but pass a number to dial
    a different handset - calling your own line from your own caller ID is an edge
    case (many carriers route it straight to voicemail) and it cannot verify that
    the caller ID renders correctly on the receiving screen."""
    import os
    from dialer.airtable import normalize_phone

    if target:
        cell = normalize_phone(target)
        if not cell:
            print(f"{target!r} is not a dialable US number"); return 1
    else:
        cell = os.environ.get("TWILIO_CALLER_ID", "")
    if not cell:
        print("TWILIO_CALLER_ID not set"); return 1
    if cell == os.environ.get("TWILIO_CALLER_ID"):
        print("NOTE: dialing your own line. Carriers often send this straight to\n"
              "      voicemail, and it cannot prove the caller ID renders. Pass a\n"
              "      different number to test properly:  live_check.py seed 7025551234\n")

    for r in find_test_rows(air):
        air._request("DELETE", f"/{air.base_id}/{air.table_id}/{r['id']}")
        print(f"removed a stale test row {r['id']}")

    rec = air._request("POST", f"/{air.base_id}/{air.table_id}", {
        "fields": {
            "Company": f"{MARKER} - delete me",
            "First Name": "Eliahs",
            "Phone": cell,
            "Industry": "HVAC",
            "Status": "Call Today",
            "Lead Type": "Mystery Shopped",
            "Shop Result": "No Response",
            "Date Added": "2020-01-01",
            "Context Cue": "This is the live-call verification row. Answer it, "
                           "confirm the caller ID shows your cell, say something so "
                           "both directions of audio are proven, then hang up.",
            "Attempts": 0,
        }
    })
    print(f"seeded {rec['id']} -> {cell}")

    q = air.fetch_queue(limit=3)
    print("\nqueue now reads:")
    for i, l in enumerate(q, 1):
        print(f"  {i}. {l['company'][:44]:<44} {l['phone']}")
    if not q or MARKER not in q[0]["company"]:
        print("\nWARNING: the test row is not #1 — check the tier ordering")
        return 1

    print(f"""
Ready. Now:

  1. Make sure the server is running ARMED and NOT in dry run:
       cd {ROOT}
       DIALER_ARM_WRITE=1 DIALER_DRY_RUN=0 ./run.sh
     (or just: open -a "No Brakes"  — after setting DIALER_ARM_WRITE=1 in .env)

  2. Set the target to 1, hit START.
  3. Your phone rings. Confirm the caller ID shows {cell}.
  4. Answer it. Talk out loud both ways. Hang up (or press ENTER).
  5. Then run:  ./.venv/bin/python3 packaging/live_check.py verify
""")
    return 0


def verify(air: AirtableClient) -> int:
    rows = find_test_rows(air)
    if not rows:
        print("no test row found - run `seed` first"); return 1
    f = rows[0]["fields"]
    today = date.today().isoformat()

    print("=== Airtable write ===")
    fails = []
    checks = [
        ("Disposition set", bool(f.get("Disposition")), True),
        ("Last Call Date is today", f.get("Last Call Date"), today),
        ("Attempts incremented", (f.get("Attempts") or 0) >= 1, True),
        ("Status written", bool(f.get("Status")), True),
    ]
    for label, got, want in checks:
        ok = got == want
        print(f"  {'ok  ' if ok else 'FAIL'}  {label}: {got!r}")
        if not ok:
            fails.append(label)
    print(f"  ---  Disposition={f.get('Disposition')!r} Status={f.get('Status')!r} "
          f"Next Action={f.get('Next Action')!r}")
    if f.get("Notes"):
        print(f"  ---  Notes: {f['Notes'][:160]}")

    print("\n=== AIOS accountability stack still sees the dial ===")
    gate = AIOS / "tools" / "tune_me_out_gate.py"
    if gate.exists():
        out = subprocess.run(["python3", str(gate), "--status"],
                             capture_output=True, text=True, cwd=AIOS)
        line = out.stdout.strip() or out.stderr.strip()
        print(f"  tune_me_out_gate --status: {line}")
        try:
            data = json.loads(line)
            ok = data.get("dials", 0) >= 1
            print(f"  {'ok  ' if ok else 'FAIL'}  dials today = {data.get('dials')}")
            if not ok:
                fails.append("gate dial count did not increment")
        except json.JSONDecodeError:
            print("  (could not parse gate output)")
    else:
        print(f"  skip - {gate} not found")

    print()
    if fails:
        print(f"FAILED: {', '.join(fails)}")
        return 1
    print("LIVE CALL VERIFIED. Run `cleanup` to delete the test row.")
    return 0


def cleanup(air: AirtableClient) -> int:
    rows = find_test_rows(air)
    if not rows:
        print("nothing to clean up"); return 0
    for r in rows:
        air._request("DELETE", f"/{air.base_id}/{air.table_id}/{r['id']}")
        print(f"deleted {r['id']}")
    return 0


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd not in ("seed", "verify", "cleanup"):
        print(__doc__); return 2
    # seed/cleanup mutate the test row regardless of DIALER_ARM_WRITE — that flag
    # gates the dialer's own writes, not this harness's scaffolding.
    air = AirtableClient(arm_write=True)
    if cmd == "seed":
        return seed(air, sys.argv[2] if len(sys.argv) > 2 else None)
    return {"verify": verify, "cleanup": cleanup}[cmd](air)


if __name__ == "__main__":
    sys.exit(main())
