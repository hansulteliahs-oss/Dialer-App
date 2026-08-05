#!/usr/bin/env python3
"""
One-time Twilio provisioning for the No Brakes dialer.

Deploys twilio-function/dial.js as a Twilio Function with visibility PROTECTED
and points a TwiML App at it. Idempotent - safe to re-run; it reuses anything
that already exists and only redeploys the function body.

Why the API and not the Console: the Console UI defaults new Functions to
Protected but the Serverless Toolkit defaults to Public unless the file is named
*.protected.js. Doing it here means visibility is set explicitly and verified,
so neither default can bite us. Public would let a stranger with the URL place
calls billed to this account.

Usage:
    TWILIO_ACCOUNT_SID=AC... TWILIO_API_KEY=SK... TWILIO_API_SECRET=... \
    TWILIO_CALLER_ID=+1... ./.venv/bin/python3 packaging/provision_twilio.py
"""
import json
import os
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
FUNCTION_SRC = ROOT / "twilio-function" / "dial.js"

SERVICE_NAME = "no-brakes-dialer"
ENV_SUFFIX = "prod"
FUNCTION_PATH = "/dial"
APP_NAME = "No Brakes dialer"
PERSONA_LINE_DIGITS = "8583564281"

SERVERLESS = "https://serverless.twilio.com/v1"
UPLOAD = "https://serverless-upload.twilio.com/v1"
API2010 = "https://api.twilio.com/2010-04-01"


def die(msg):
    print(f"FAIL: {msg}", file=sys.stderr)
    sys.exit(1)


class Twilio:
    def __init__(self, account_sid, key, secret):
        self.account_sid = account_sid
        self.auth = (key, secret)

    def _req(self, method, url, **kw):
        r = requests.request(method, url, auth=self.auth, timeout=45, **kw)
        if r.status_code >= 400:
            die(f"{method} {url} -> {r.status_code} {r.text[:400]}")
        return r.json() if r.content else {}

    def get(self, url, **kw):
        return self._req("GET", url, **kw)

    def post(self, url, data=None, **kw):
        return self._req("POST", url, data=data, **kw)


def ensure_service(tw):
    for s in tw.get(f"{SERVERLESS}/Services?PageSize=50").get("services", []):
        if s["unique_name"] == SERVICE_NAME:
            print(f"  service exists: {s['sid']}")
            return s["sid"]
    s = tw.post(f"{SERVERLESS}/Services", {
        "UniqueName": SERVICE_NAME,
        "FriendlyName": APP_NAME,
        "IncludeCredentials": "true",
    })
    print(f"  service created: {s['sid']}")
    return s["sid"]


def ensure_environment(tw, svc):
    for e in tw.get(f"{SERVERLESS}/Services/{svc}/Environments").get("environments", []):
        if e["domain_suffix"] == ENV_SUFFIX:
            print(f"  environment exists: {e['sid']} ({e['domain_name']})")
            return e["sid"], e["domain_name"]
    e = tw.post(f"{SERVERLESS}/Services/{svc}/Environments", {
        "UniqueName": ENV_SUFFIX,
        "DomainSuffix": ENV_SUFFIX,
    })
    print(f"  environment created: {e['sid']} ({e['domain_name']})")
    return e["sid"], e["domain_name"]


def ensure_function(tw, svc):
    for f in tw.get(f"{SERVERLESS}/Services/{svc}/Functions").get("functions", []):
        if f["friendly_name"] == "dial":
            print(f"  function exists: {f['sid']}")
            return f["sid"]
    f = tw.post(f"{SERVERLESS}/Services/{svc}/Functions", {"FriendlyName": "dial"})
    print(f"  function created: {f['sid']}")
    return f["sid"]


def upload_version(tw, svc, fn):
    """Upload dial.js as a new Function Version. Visibility is set explicitly."""
    body = FUNCTION_SRC.read_text()
    files = {
        "Path": (None, FUNCTION_PATH),
        "Visibility": (None, "protected"),
        "Content": ("dial.js", body, "application/javascript"),
    }
    r = requests.post(
        f"{UPLOAD}/Services/{svc}/Functions/{fn}/Versions",
        auth=tw.auth, files=files, timeout=60,
    )
    if r.status_code >= 400:
        die(f"version upload -> {r.status_code} {r.text[:400]}")
    v = r.json()
    if v.get("visibility") != "protected":
        die(f"visibility came back {v.get('visibility')!r}, refusing to continue")
    print(f"  version uploaded: {v['sid']} visibility={v['visibility']}")
    return v["sid"]


def set_caller_id_var(tw, svc, env, caller_id):
    url = f"{SERVERLESS}/Services/{svc}/Environments/{env}/Variables"
    for v in tw.get(url).get("variables", []):
        if v["key"] == "CALLER_ID":
            tw.post(f"{url}/{v['sid']}", {"Value": caller_id})
            print(f"  CALLER_ID updated -> {caller_id}")
            return
    tw.post(url, {"Key": "CALLER_ID", "Value": caller_id})
    print(f"  CALLER_ID set -> {caller_id}")


def build_and_deploy(tw, svc, env, version_sid):
    b = tw.post(f"{SERVERLESS}/Services/{svc}/Builds", {
        "FunctionVersions": version_sid,
        "Dependencies": json.dumps([]),
    })
    build_sid = b["sid"]
    print(f"  build created: {build_sid} ({b['status']})")

    for _ in range(60):
        st = tw.get(f"{SERVERLESS}/Services/{svc}/Builds/{build_sid}/Status")
        if st["status"] == "completed":
            print("  build completed")
            break
        if st["status"] == "failed":
            die(f"build failed: {json.dumps(st)[:400]}")
        time.sleep(2)
    else:
        die("build did not complete within 120s")

    d = tw.post(f"{SERVERLESS}/Services/{svc}/Environments/{env}/Deployments",
                {"BuildSid": build_sid})
    print(f"  deployed: {d['sid']}")


def ensure_twiml_app(tw, voice_url):
    url = f"{API2010}/Accounts/{tw.account_sid}/Applications.json"
    for a in tw.get(f"{url}?PageSize=50").get("applications", []):
        if a["friendly_name"] == APP_NAME:
            tw.post(f"{API2010}/Accounts/{tw.account_sid}/Applications/{a['sid']}.json",
                    {"VoiceUrl": voice_url, "VoiceMethod": "POST"})
            print(f"  twiml app exists, voice url refreshed: {a['sid']}")
            return a["sid"]
    a = tw.post(url, {
        "FriendlyName": APP_NAME,
        "VoiceUrl": voice_url,
        "VoiceMethod": "POST",
    })
    print(f"  twiml app created: {a['sid']}")
    return a["sid"]


def main():
    account_sid = os.environ.get("TWILIO_ACCOUNT_SID", "")
    key = os.environ.get("TWILIO_API_KEY", "")
    secret = os.environ.get("TWILIO_API_SECRET", "")
    caller_id = os.environ.get("TWILIO_CALLER_ID", "").strip()

    if not account_sid.startswith("AC"):
        die(f"TWILIO_ACCOUNT_SID must start with AC, got {account_sid[:2]!r}. "
            "An SK value is an API Key SID, not the Account SID.")
    if not key.startswith("SK"):
        die("TWILIO_API_KEY must start with SK")
    if not secret:
        die("TWILIO_API_SECRET is empty")
    # Hard refusal, same rule as server.py and dial.js.
    if "".join(c for c in caller_id if c.isdigit()).endswith(PERSONA_LINE_DIGITS):
        die("TWILIO_CALLER_ID is the mystery-shop persona line. It must never be "
            "shared with a prospect. Refusing to provision.")
    if not caller_id.startswith("+1") or len(caller_id) != 12:
        die(f"TWILIO_CALLER_ID must be E.164 like +15551234567, got {caller_id!r}")
    if not FUNCTION_SRC.exists():
        die(f"missing {FUNCTION_SRC}")

    tw = Twilio(account_sid, key, secret)

    acct = tw.get(f"{API2010}/Accounts/{account_sid}.json")
    if acct.get("type") != "Full":
        die(f"account type is {acct.get('type')!r}, not 'Full'. A trial account "
            "cannot use a verified number as outbound caller ID and can only "
            "dial pre-verified numbers. Upgrade first.")
    print(f"account: {acct['friendly_name']} [{acct['type']}]")

    verified = [c["phone_number"] for c in
                tw.get(f"{API2010}/Accounts/{account_sid}/OutgoingCallerIds.json")
                  .get("outgoing_caller_ids", [])]
    owned = [n["phone_number"] for n in
             tw.get(f"{API2010}/Accounts/{account_sid}/IncomingPhoneNumbers.json")
               .get("incoming_phone_numbers", [])]
    if caller_id not in verified and caller_id not in owned:
        die(f"{caller_id} is neither a verified caller ID {verified} nor an owned "
            f"number {owned}. Verify it in the Console first.")
    print(f"caller id: {caller_id} (verified)")

    print("serverless:")
    svc = ensure_service(tw)
    env, domain = ensure_environment(tw, svc)
    fn = ensure_function(tw, svc)
    version = upload_version(tw, svc, fn)
    set_caller_id_var(tw, svc, env, caller_id)
    build_and_deploy(tw, svc, env, version)

    voice_url = f"https://{domain}{FUNCTION_PATH}"
    print("twiml app:")
    app_sid = ensure_twiml_app(tw, voice_url)

    print("\n" + "=" * 62)
    print("PROVISIONED")
    print(f"  function url         {voice_url}")
    print(f"  visibility           protected")
    print(f"  TWILIO_TWIML_APP_SID {app_sid}")
    print("=" * 62)
    print("\nAdd to .env:")
    print(f"TWILIO_TWIML_APP_SID={app_sid}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
