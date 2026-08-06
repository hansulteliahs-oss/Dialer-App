"""
Twilio Voice: access-token minting and child-call status lookup.

No tunnel, no inbound webhook. The dial path is:

  1. browser calls device.connect({params: {To: phone}})
  2. Twilio invokes its own hosted Function (visibility Protected, permanent URL)
  3. the Function returns
     <Dial callerId={cell} answerOnBridge="true" timeout="22"><Number>{To}</Number></Dial>
  4. this module POLLS the REST API for the child call's status + duration

Polling at 1-2s on a single active call is nowhere near Twilio's rate ceilings -
verified in drafts/research/2026-08-04-twilio-call-lifecycle.md.

Hard refusal #2 lives here: 858-356-4281 is the mystery-shop persona Google Voice
line. connections.md says it must never be shared, and half the best part of the
queue is people he shopped using it. Rejected loudly at startup.

Self-test (no calls placed):
    ./.venv/bin/python3 -m dialer.twilio_voice --selftest
"""
from __future__ import annotations

import os
import re
import threading

from twilio.http.http_client import TwilioHttpClient
from twilio.jwt.access_token import AccessToken
from twilio.jwt.access_token.grants import VoiceGrant
from twilio.rest import Client

# The mystery-shop persona line. Never a caller ID, under any configuration.
PERSONA_LINE_DIGITS = "8583564281"

# Twilio call states, grouped by what a hangup has to send.
# Sending the wrong one for the current state is a documented failure.
PRE_ANSWER_STATES = {"queued", "ringing", "initiated"}
LIVE_STATES = {"in-progress"}
# Terminal states, and whether they mean a human (or machine) actually picked up.
TERMINAL_STATES = {"completed", "busy", "no-answer", "failed", "canceled"}

# Access tokens have no default TTL and max out at 24h. Set it explicitly.
TOKEN_TTL_SECONDS = 3600

# TwilioHttpClient defaults to timeout=None, which requests/urllib3 read as
# "wait forever". A silent black-hole connection - captive-portal wifi, a
# half-open VPN - therefore hangs preflight() before app.run() ever executes,
# and the port never opens: launched from run.sh there is no watchdog at all,
# so the process just sits there. airtable.py already learned this lesson on
# the dial path ("30s each meant a flaky patch could hold a dial hostage for a
# minute and a half"); the same discipline belongs here.
HTTP_TIMEOUT_SECONDS = 10


class TwilioConfigError(RuntimeError):
    pass


def _digits(s: str | None) -> str:
    return re.sub(r"\D", "", s or "")


class TwilioVoice:
    def __init__(self, account_sid=None, api_key=None, api_secret=None,
                 twiml_app_sid=None, caller_id=None):
        self.account_sid = account_sid or os.environ.get("TWILIO_ACCOUNT_SID", "")
        self.api_key = api_key or os.environ.get("TWILIO_API_KEY", "")
        self.api_secret = api_secret or os.environ.get("TWILIO_API_SECRET", "")
        self.twiml_app_sid = twiml_app_sid or os.environ.get("TWILIO_TWIML_APP_SID", "")
        self.caller_id = (caller_id or os.environ.get("TWILIO_CALLER_ID", "")).strip()
        self.validate()
        self._client = self._new_client()
        # call_status() and hangup() are hit from concurrent Flask request
        # threads (the client polls every 1.2s). preflight() already notes that
        # the SDK's underlying requests.Session "is not promised thread-safe"
        # and hands each worker its own Client - the shared one used for the
        # rest of the session's life needs the same protection, so serialise it.
        self._client_lock = threading.Lock()

    def _new_client(self) -> Client:
        return Client(self.api_key, self.api_secret, self.account_sid,
                      http_client=TwilioHttpClient(timeout=HTTP_TIMEOUT_SECONDS))

    # --- startup guards ------------------------------------------------------

    def validate(self) -> None:
        """Refuse to start on a misconfiguration that could place a wrong call."""
        # Hard refusal #2, checked before anything else can go wrong.
        if _digits(self.caller_id).endswith(PERSONA_LINE_DIGITS):
            raise TwilioConfigError(
                "REFUSED TO START: TWILIO_CALLER_ID is 858-356-4281, the mystery-shop "
                "persona line. It must never be shown to a prospect - half this queue "
                "is people who were shopped with it. Set it to the personal cell."
            )
        if not self.account_sid.startswith("AC"):
            raise TwilioConfigError(
                f"TWILIO_ACCOUNT_SID must start with AC, got {self.account_sid[:2]!r}. "
                "An SK value is an API Key SID, not the Account SID."
            )
        if not self.api_key.startswith("SK"):
            raise TwilioConfigError("TWILIO_API_KEY must start with SK")
        if not self.api_secret:
            raise TwilioConfigError("TWILIO_API_SECRET is not set")
        if not self.twiml_app_sid.startswith("AP"):
            raise TwilioConfigError(
                f"TWILIO_TWIML_APP_SID must start with AP, got {self.twiml_app_sid[:2]!r}"
            )
        if not re.fullmatch(r"\+1\d{10}", self.caller_id):
            raise TwilioConfigError(
                f"TWILIO_CALLER_ID must be E.164 like +17605551234, got {self.caller_id!r}"
            )

    def preflight(self) -> dict:
        """Live checks against the account. Called once at server startup.

        Five REST round-trips, fetched concurrently: run serially they cost
        5x one Twilio RTT before the port even opens, which is most of what the
        launcher's readiness poll used to sit through. Every check still runs
        and every refusal still fires - only the waiting overlaps. Each worker
        gets its own Client because the underlying requests.Session is not
        promised thread-safe.
        """
        from concurrent.futures import ThreadPoolExecutor

        fresh = self._new_client

        try:
            with ThreadPoolExecutor(max_workers=5) as pool:
                f_acct = pool.submit(lambda: fresh().api.accounts(self.account_sid).fetch())
                f_verified = pool.submit(lambda: [c.phone_number
                                                  for c in fresh().outgoing_caller_ids.list()])
                f_owned = pool.submit(lambda: [n.phone_number
                                               for n in fresh().incoming_phone_numbers.list()])
                f_app = pool.submit(lambda: fresh().applications(self.twiml_app_sid).fetch())
                f_balance = pool.submit(lambda: fresh().balance.fetch().balance)
                acct = f_acct.result()
                verified = f_verified.result()
                owned = f_owned.result()
                app = f_app.result()
                balance = f_balance.result()
        except TwilioConfigError:
            raise
        except Exception as e:  # noqa: BLE001
            # Only TwilioConfigError is caught at the top-level startup guard,
            # so a transient Twilio 500 or an unreachable API used to crash the
            # launch with a raw traceback - on a morning he is trying to dial,
            # reading as "what did I break" rather than "try again in a minute".
            raise TwilioConfigError(
                f"Could not reach Twilio to verify the account ({e}). "
                "This is almost always a network blip - try launching again."
            ) from e

        if acct.type != "Full":
            raise TwilioConfigError(
                f"Twilio account type is {acct.type!r}, not 'Full'. A trial account "
                "can only dial pre-verified numbers and cannot use a verified number "
                "as caller ID. Upgrade before dialing."
            )
        if self.caller_id not in verified and self.caller_id not in owned:
            raise TwilioConfigError(
                f"caller ID {self.caller_id} is neither verified {verified} nor owned "
                f"{owned}. Verify it in the Twilio Console first."
            )
        return {
            "account_type": acct.type,
            "caller_id": self.caller_id,
            "twiml_app": app.friendly_name,
            "voice_url": app.voice_url,
            "balance": balance,
        }

    # --- tokens --------------------------------------------------------------

    def mint_token(self, identity: str = "eliahs") -> dict:
        token = AccessToken(
            self.account_sid, self.api_key, self.api_secret,
            identity=identity, ttl=TOKEN_TTL_SECONDS,
        )
        token.add_grant(VoiceGrant(
            outgoing_application_sid=self.twiml_app_sid,
            incoming_allow=False,  # this device never receives; callbacks go to the cell
        ))
        jwt = token.to_jwt()
        return {
            "token": jwt if isinstance(jwt, str) else jwt.decode(),
            "identity": identity,
            "ttl": TOKEN_TTL_SECONDS,
        }

    # --- call lifecycle ------------------------------------------------------

    def child_call(self, parent_call_sid: str) -> dict | None:
        """The leg out to the lead. None until Twilio has created it.

        The browser's own call is the PARENT; <Dial> creates a CHILD leg to the
        prospect. Status and duration that matter are the child's.
        """
        with self._client_lock:
            kids = self._client.calls.list(parent_call_sid=parent_call_sid, limit=1)
        if not kids:
            return None
        c = kids[0]
        return {
            "sid": c.sid,
            "status": c.status,
            "duration": int(c.duration or 0),
            "to": c.to,
            "answered_by": getattr(c, "answered_by", None),
            "start_time": c.start_time.isoformat() if c.start_time else None,
            "end_time": c.end_time.isoformat() if c.end_time else None,
        }

    def call_status(self, parent_call_sid: str) -> dict:
        """What the UI polls. Collapses parent+child into one verdict.

        connected  - the lead's leg actually answered (human or machine; without
                     AMD we do not care, because Eliahs is the classifier)
        finished   - nothing more will happen on this call
        """
        child = self.child_call(parent_call_sid)
        if child is None:
            try:
                with self._client_lock:
                    parent = self._client.calls(parent_call_sid).fetch()
                pstatus = parent.status
            except Exception:  # noqa: BLE001
                pstatus = "unknown"
            return {
                "child": None, "status": pstatus, "duration": 0,
                "connected": False,
                "finished": pstatus in TERMINAL_STATES,
            }
        return {
            "child": child["sid"],
            "status": child["status"],
            "duration": child["duration"],
            "connected": child["status"] == "in-progress" or child["duration"] > 0,
            "finished": child["status"] in TERMINAL_STATES,
            # Carried through purely so the server can time the carrier segment.
            # child_call() already pays for this field on every poll; dropping it
            # here is what left connect -> audible a black box. Costs nothing.
            "child_start_time": child["start_time"],
        }

    def hangup(self, call_sid: str) -> dict:
        """End a call, branching on its CURRENT status.

        update(status='canceled') only works while queued/ringing;
        update(status='completed') only works while in-progress. Sending the
        wrong one for the state is a documented Twilio failure, so fetch first.
        """
        try:
            with self._client_lock:
                call = self._client.calls(call_sid).fetch()
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"fetch failed: {e}"}

        if call.status in TERMINAL_STATES:
            return {"ok": True, "status": call.status, "action": "already-ended"}
        target = "canceled" if call.status in PRE_ANSWER_STATES else "completed"
        try:
            with self._client_lock:
                updated = self._client.calls(call_sid).update(status=target)
            return {"ok": True, "status": updated.status, "action": target}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e), "attempted": target}


def _selftest() -> int:
    from . import ROOT, load_env
    load_env(ROOT / ".env")

    fails = []

    # hard refusal #2 must fire on every spelling of the persona line
    for bad in ("+18583564281", "858-356-4281", "8583564281", "+1 (858) 356-4281"):
        try:
            TwilioVoice(account_sid="AC" + "0" * 32, api_key="SK" + "0" * 32,
                        api_secret="x", twiml_app_sid="AP" + "0" * 32, caller_id=bad)
            fails.append(f"persona line {bad!r} was ACCEPTED as caller ID")
        except TwilioConfigError as e:
            if "persona" not in str(e):
                fails.append(f"{bad!r} rejected for the wrong reason: {e}")

    # an SK in the Account SID slot is the exact mistake the AIOS .env makes
    try:
        TwilioVoice(account_sid="SK" + "0" * 32, api_key="SK" + "0" * 32,
                    api_secret="x", twiml_app_sid="AP" + "0" * 32,
                    caller_id="+17605551234")
        fails.append("an SK value was accepted as TWILIO_ACCOUNT_SID")
    except TwilioConfigError:
        pass

    tv = TwilioVoice()
    print("config valid")
    pre = tv.preflight()
    for k, v in pre.items():
        print(f"  {k:<14} {v}")
    if pre["account_type"] != "Full":
        fails.append("account is not Full")

    t = tv.mint_token()
    print(f"  token          {t['token'][:28]}... (ttl {t['ttl']}s, identity {t['identity']})")
    if t["token"].count(".") != 2:
        fails.append("minted token is not a well-formed JWT")

    if fails:
        print(f"\nFAIL ({len(fails)})")
        for f in fails:
            print("  - " + f)
        return 1
    print("\ntwilio_voice.py selftest: all checks passed")
    return 0


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    print(__doc__)
