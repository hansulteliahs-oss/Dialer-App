# No Brakes

A cold-call dialer that removes the click.

Airtable holds 5,850 leads with a phone number on every one. Total dispositions
ever logged: 11. The bottleneck was never the list or the talk track — it was the
moment before pressing dial, and that moment is won by not calling.

Set a target, hit Start once, sit through a five-minute lock-in, and the machine
dials down the list on its own — through the breather, into the next number,
never handing him a moment where stopping is easier than continuing.

---

## Run it

```bash
open -a "No Brakes"          # the installed app
./run.sh                     # dev server only, no kiosk window
```

Dry run — real queue, simulated ring→outcome, zero Twilio spend, zero writes:

```bash
DIALER_DRY_RUN=1 DIALER_ARM_WRITE=0 DIALER_WARMUP_SECONDS=2 ./run.sh
./.venv/bin/python3 packaging/dryrun_check.py   # 79-check verification harness
```

The harness sits through the warmup rather than bypassing it, so it needs the
short one; it refuses to run against a warmup longer than 10s, and aborts
outright if it finds the server armed.

Install / reinstall the app bundle:

```bash
bash packaging/install.sh
```

## The warmup

START does not dial. It drops into a **5:00 lock-in** and the first call goes out
when that hits zero, on its own.

**No key shortens it** — not even `ENTER`, which is "go now" in every other state.
That is the point: `ENTER` is the key his hands already know, so a skip would get
pressed reflexively on exactly the mornings the warmup exists for. The only way
out is the same as everywhere else: hold `ESC` and type the sentence, which ends
the session rather than the wait.

It is not a screen that happens to count down. The deadline is an absolute
timestamp in the session file and `/api/dial` returns `425` until it passes, so:

- force-quitting through the warmup **does not skip it** — relaunching returns to
  the time that is actually left *(verified: 60s → killed at 40s left → dead 15s →
  relaunched at 24s)*
- and **does not reset it** — the clock runs while the app is dead, so if it
  expires during the force-quit, relaunching comes back ready to dial
- a hand-rolled `POST /api/dial` gets the same refusal the window does

Five minutes of a bare countdown is five minutes to talk yourself out of it, so
the screen carries the **top three leads** — company, name, industry, attempt
number, mystery-shop cue, and a CSLB owner-name lookup on the rows with no name.
Read the list, then dial it warm. Cues only, never a script (`cold_calling_
conversational`).

Starting late enough that the warmup would push the first dial past 9:00pm is
refused at START, because a timer with no stop must never run toward a wall.

Length is `DIALER_WARMUP_SECONDS` (default 300). `0` disables it.

## Keys

| Key | What it does |
|---|---|
| `SPACE` | voicemail or dead end → log No Answer, 15s breather |
| `ENTER` | real conversation → 2:00 breather; during a breather, go now |
| `1`–`5` | set the disposition (live during *every* breather) |
| `D` | do-not-call — honored on the spot, no rebuttal |
| `P` | pause. One per session, 10:00 ceiling, `ENTER` resumes early |

**Keys that do not exist:** back, add-time, skip-lead, quit, skip-warmup.

During the warmup **no key does anything at all**, including `P` — pausing the
lock-in is pausing a pause, and a refused `P` does not burn the session's one
real pause.

A call that never connects auto-logs No Answer and advances with **zero keys**.

## No way out — what's real and what's theater

Cmd-Q, Force Quit and the power button always work. Any design claiming otherwise
is a lie, and a lock defeated in three seconds teaches you the lock is fake. The
goal is only that **quitting costs more than continuing, and quitting never
actually ends the session.**

1. **Kiosk fullscreen.** No close button, no tabs, no URL bar.
2. **The session lives on the server, not in the window.** Closing the window does
   not end it. Relaunching drops you back into the breather at `8 of 20`, counting
   down to the next dial — no Start button, no fresh slate. An outcome parked
   mid-breather is committed on the next boot, so a force-quit loses nothing.
   *(Verified: 2 of 20 → `kill -9` → relaunched at 3 of 20, parked outcome written.)*
   The same is true of the 5:00 warmup: its deadline is a timestamp, not a timer,
   so quitting through it neither skips it nor restarts it.
3. **Typing is the only graceful exit.** Hold `ESC` for 2s, then type
   `I am quitting at 8 of 20 with 12 calls left` verbatim. Paste is blocked.
4. **Abandonment is recorded** to `state/abandons.jsonl` and shown on the start
   screen until a full session completes.

## Two hard refusals, coded in

1. **Never dial a row with `DNC = true`.** Enforced in the Airtable query *and*
   re-checked immediately before `device.connect()` — the queue can go stale
   between pull and dial. Mirrors `tools/sdr_write.py`'s absolute-refusal posture.
2. **Never allow `858-356-4281` as caller ID.** That is the mystery-shop persona
   Google Voice line, and `connections.md` says it must never be shared — half the
   best part of this queue is people who were shopped with it. Rejected at startup
   in `server.py`, again in `provision_twilio.py`, and again inside the Twilio
   Function.

## Not an autodialer, by design

Post-*Facebook v. Duguid*, an ATDS requires a random or sequential number
generator. Three constraints keep this outside that definition, and they are
requirements rather than defaults:

- **one call at a time, never predictive**
- **no prerecorded or synthetic voice, ever**
- **numbers only from the curated Airtable list, never generated**

There is also a hard 8am–9pm local refusal. That outer bound is a legal line, not
a preference, and it never speaks between 8 and 9.

**No call recording in v1.** California is all-party consent (CIPA §632, §632.7),
$5,000 per violation, no proof of harm required.

---

## Setup

### 1. Twilio (~30 min, one time)

1. **Fund the account.** Not optional — a trial account can only *call* verified
   numbers and cannot use a verified number as the outbound caller ID. Funding is
   what unlocks the whole design.
2. **Verify the personal cell as an outgoing caller ID.** Prospects see the real
   number; callbacks ring the pocket.
3. **Buy a US local number** (~$1.15/mo). Possibly not required — outbound works
   with a verified caller ID alone — but a TwiML App *is* required regardless, and
   it is a cheap hedge plus the fallback if attestation hurts connect rate.
4. **Create an API Key + Secret.**
5. **Deploy the Function and TwiML App:**

   ```bash
   ./.venv/bin/python3 packaging/provision_twilio.py
   ```

   This is idempotent and sets Function visibility to **Protected** explicitly,
   then verifies it came back Protected. Do not deploy with the Serverless
   Toolkit: the Console defaults new Functions to Protected but the toolkit
   defaults to **Public** unless the file is named `*.protected.js`, and Public
   here means a stranger with the URL can place calls billed to this account.
   The caller ID is stored as a Function **env var**, so changing it needs no
   redeploy.

Functions cost effectively $0 — 10,000 free invocations/month against ~20/day.
Running cost at 20 dials/day ≈ **$7/mo**.

### 2. `.env`

Copy `.env.example` to `.env` and fill it in. `.env` is gitignored, `chmod 600`,
and a pre-commit hook refuses any commit containing a live `AC`/`SK` Twilio SID or
a `pat`/`key` Airtable token.

| Var | Notes |
|---|---|
| `TWILIO_ACCOUNT_SID` | starts `AC`. **Not** the API key — an `SK` value here is refused at startup |
| `TWILIO_API_KEY` | starts `SK` |
| `TWILIO_API_SECRET` | |
| `TWILIO_TWIML_APP_SID` | starts `AP`, printed by `provision_twilio.py` |
| `TWILIO_CALLER_ID` | E.164. Must be verified on a **Full** account |
| `AIRTABLE_API_KEY` | starts `pat` |
| `DIALER_ARM_WRITE` | `1` to actually write. Matches `CAL_ARM_WRITE` / `SDR_ARM_WRITE` |
| `DIALER_DRY_RUN` | `1` to simulate calls |

### 3. Hardware

A wired USB boom-mic headset (Logitech H390, ~$30). Both AirPods and the XM5s
drop to a degraded mono codec the moment the mic goes live, so you sound thin on
the exact call where sounding like a real person is the pitch. Not a blocker —
AirPods are fine for session one.

---

## Architecture

```
browser ──device.connect({To})──> Twilio ──> its own hosted Function (Protected)
                                                    │
                                                    ▼
                              <Dial callerId answerOnBridge timeout=22>
                                                    │
   local Flask <──poll calls.list(parent_call_sid)──┘
```

No tunnel, no ngrok, no inbound webhook, no URL that changes every launch. The
Function is Twilio-hosted with a permanent URL; the local server polls the REST
API for the child leg's `status` + `duration`. Polling at ~1s on a single active
call is nowhere near Twilio's rate ceilings.

`answerOnBridge="true"` means you hear real ringback and the child leg is not
marked answered until the lead picks up — which is what makes "completed with
nonzero duration" a trustworthy connect signal.

Without AMD, `completed` + nonzero duration covers both human and machine, which
is fine: **you are the classifier** (`SPACE` vs `ENTER`). AMD was rejected because
it costs 1–3s of dead air on human pickup — the exact robocall tell. Auto-advance
only fires on calls that genuinely never connected.

### Implementation notes that each cost an hour to learn

- SDK is **`@twilio/voice-sdk` v2.18.3**, vendored in `static/vendor/` and
  sha512-verified against the npm registry. The older `twilio-client` is EOL since
  Sept 2025 — `Connection` → `Call`, `Device.Status` → `Device.State`.
- Read the CallSid as `call.parameters.CallSid` **inside the `accept` handler**,
  not after `connect()` resolves.
- Access tokens have **no default TTL** and max at 24h. Set it explicitly and
  handle `tokenWillExpire` (fires 10s out) with `device.updateToken()`.
- **Hangup must branch on current status:** `update(status='canceled')` while
  queued/ringing, `update(status='completed')` while in-progress. The wrong one
  for the state is a documented failure, so `hangup()` fetches first.
- Bind `127.0.0.1`, but browse to **`localhost`**. There is a live dispute over
  whether `127.0.0.1` always counts as a secure context for `getUserMedia`, while
  `localhost` definitively does. Same destination, no ambiguity about the mic.

### Kiosk + mic

Verified 2026-08-04, because no Twilio, Chrome, or community source documents it:
**`--kiosk --app` composes fine, `getUserMedia` works, and the grant persists**
across relaunches with a dedicated `--user-data-dir`. Second launch reported
`permission_state_before: "granted"` and resolved in 1.4s with no prompt.

**Known limitation:** the window runs under the Chrome process, so the Dock shows
Chrome's icon while it is open. `/Applications/No Brakes.app` carries the real
icon and is what you launch. For a custom icon on the window itself, Chrome must
"Install" the page as a PWA — `manifest.json` ships for that; it is optional.

### Known tradeoff: STIR/SHAKEN attestation

A personal cell as caller ID gets **B-level** attestation; only a Twilio-owned
number can reach A. There is no way to lift it while keeping the real number, so
this is a live tradeoff rather than a bug. **Trigger to revisit:** if connect rate
looks bad after ~200 dials, switch to a Twilio-owned SoCal number with A
attestation. The dial layer is one module specifically so that is a config change.

**Verified 2026-08-04 — number delivery works.** A live test call reached a second
handset which resolved `+17608464537` against its address book and displayed the
saved contact name. The number arrives intact.

**Still open — spam labeling on cold numbers.** That test proves delivery and
nothing more: iOS and Android suppress spam warnings entirely for known contacts,
so the receiving device never ran the check. A cold prospect has no contact entry,
sees the raw number, and may get a carrier "Spam" / "Scam Likely" label off
B-attestation plus reputation heuristics. Only real dials to strangers can answer
it — which is what the ~200-dial connect-rate trigger above is for.

A2P 10DLC is **SMS-only** — no registration needed for outbound voice.

---

## The Airtable contract

Base `appEJYWOrT5NAbxOM`, table `tblURF0GnyhgKIzJj` (`Call List`).
**Re-read the live schema before changing any of this** — Eliahs runs parallel
Claude sessions and cached field lists go stale within minutes.

### The two piles

The list picker offers exactly two choices. Both are ranked internally — the
picker chooses which pile, it never reorders one.

| Pile | Tiers, in order |
|---|---|
| **priority** (default) | callback due → shopped, never replied → hiring signal → call today → *queued* |
| **cold pile** | callback due → queued |

`priority` deliberately falls through to `queued` at the end instead of stopping
when the warm tiers run dry. A 20-dial session that exhausts them must not hit
the tally at 11 of 20 and hand you a finished screen — it keeps dialing. That is
what makes it *prioritize* rather than *only*.

**`callback due` is in both piles on purpose.** Choosing the cold grind must not
be a way to silently skip the callbacks the last cold grind created. A promise
made to a human is never buried, whichever pile you pick.

The industry picker still narrows within whichever pile is selected.

### Why `callback due` exists — the bug it fixes

Every non-terminal disposition writes `Status = Snoozed` with a future
`Next Action`:

```
Busy, Call Back    -> Status=Snoozed  Next Action=+3d
Conversation       -> Status=Snoozed  Next Action=+7d
No Answer          -> Status=Snoozed  Next Action=+2d
```

**Nothing anywhere flips `Snoozed` back when that date arrives.**
`cold_call_log.py` and `tools/sdr_write.py` both decide Call-Today-vs-Snoozed at
*write* time from the date they are writing; neither owns the roll-forward.

Every other tier matches on `Status`. So before this tier existed, a dialed cold
row matched nothing on its callback date and was **never dialed again** — one
attempt each, forever, and the 4-attempt retirement could never fire on a cold
row. It was invisible only because the 11 dispositions logged to date all landed
on Mystery Shopped rows, and that tier matches on `Lead Type` with no `Status`
clause, so those recycled correctly.

`callback_due` keys on the **date**, not on `Status`, which is how the rest of
the AIOS queues — `tools/pull_sdr_book.py` reads `Next Action` too. Pinned by a
regression check in `dryrun_check.py`.

### Queue ranking (tiers, hottest first)

| # | Tier | Matches on | Dialable rows |
|---|---|---|---|
| 0 | `callback due` — `Next Action` today or earlier | **date** | 4 |
| 1 | `Lead Type = Mystery Shopped` **and** `Shop Result = No Response` | lead type | 89 |
| 2 | `Lead Type = Hiring Signal` | lead type | 28 |
| 3 | remaining `Status = Call Today` | status | 8 |
| 4 | `Status = Queued`, oldest `Date Added` first | status | 5,710 |

Rows land in the **first** tier they match and are deduped from there, which is
why tier 3 reads 8 and not the 127 rows actually carrying `Status = Call Today` —
the other 119 are already claimed by tiers 1 and 2.

Tier 0 is the only tier where a promise was made to a human, so it outranks
everything. Tier 1 is the 89 rows whose own quote form he submitted and who never
replied — the strongest opener in the playbook. 72 carry a real first name.

**Always excluded:** `DNC = true`; `Status = Done` (retired); `Last Call Date` =
today; blank/unnormalizable phone; and a `Snoozed` row whose `Next Action` is
still in the future — dialing a promise early is the mirror image of the
callback-burial bug.

### Written after every call

`Disposition`, `Last Call Date`, `Attempts +1`, `Notes` (date-stamped append),
`Status`, `Next Action`, `Next Action Note` — the same seven fields
`scripts/cold_call_log.py` writes.

Status derivation matches that script's `plan_followup()`: terminal → `Done` with
`Next Action` cleared (must be `None`, **never `""`** — Airtable 422s on an empty
string for a date column); future next-action → `Snoozed`; today → `Call Today`.

### Retirement

When `Attempts` reaches **4** and the latest `Disposition` is `No Answer`:
`Status = Done`, `Next Action` cleared, and `[date] retired - no contact in 4
attempts` appended. `Attempts` keeps its existing cumulative meaning — no new
field.

A row that ever reached a human is **never** closed by the cap. There is no
"ever talked" field and the plan forbids adding one, so the durable signal is the
Notes history: every promise-class outcome leaves a `[date] <Disposition> - ...`
line behind, and `had_real_contact()` looks for it (plus the row's current
Disposition as a shallower second signal). Rows last touched by the old CLI only
carry that marker if a note was passed — a known gap in historical coverage, not
in anything this dialer writes.

`Not interested` and `Meeting Booked` already terminate immediately.

### Deliberate divergence from the CLI

`cold_call_log.py` exits 2 rather than log `Busy, Call Back` or `Conversation`
without a date. **A dialer cannot refuse** — refusing means stalling, and stalling
is a brake. So the dialer **pre-fills** (`+3d` callback, `+7d` conversation),
editable during the breather. The promise still lands in `Next Action` where the
follow-up queue reads it, preserving the 2026-08-01 fix.

### Why the accountability stack still works

`tools/tune_me_out_gate.py` (`count_today`) derives dials from
`Last Call Date = today` and conversations from `Disposition`. Writing those two
fields the same way keeps the soft gate, the call-nudge cron, and
`pull_activity_scoreboard.py` working with **zero changes**.

### Drift risk

`cold_call_log.py` and `dialer/outcomes.py` now encode this contract
independently. **The dialer is the primary writer; the CLI is the fallback.**
The contract is documented in both repos. `outcomes.py --selftest` pins the
behaviour.

`static/playbook.js` is the second copy of the same kind of problem: every line
in it is a compression of `references/cold-call-playbook.md` in the AIOS repo.
Change that file's four beats or its objections and this one has to move with
it, by hand, in the other repo.

**Something checks now.** `packaging/drift_check.py` reads both repos and
fails on divergence — the five Disposition options, the promise/terminal sets,
the retry offsets, `plan_followup()` over a matrix of inputs, the two
*deliberate* divergences still being exactly those two, the live Airtable
schema still matching what `outcomes.py` will write, and every objection in the
playbook markdown still being carried on screen:

```bash
./.venv/bin/python3 packaging/drift_check.py     # 29 checks, no server needed
```

It skips with exit 0 if the AIOS repo isn't on the machine, so a clone
elsewhere still runs its own tests.

---

## The talk track on screen

Three panels, and **which screen each lives on is the whole design**. He is an
introvert running an exposure ladder; the failure mode is freezing, and the
second failure mode is reading, which sounds exactly like reading. So the amount
of text on a screen is inversely proportional to how likely he is to be talking
to a human while looking at it.

| Panel | Screen | Why there |
|---|---|---|
| **The four beats** + non-negotiables | warmup | Five idle minutes and nothing else to do with his eyes. The only dose of talk track that costs nothing. |
| **Objection rail** — 8 trigger → angle lines | live card, `DIALING` only | Glanceable, never readable-aloud. Twelve words is the ceiling. |
| **Objection angles** — same 8 at full length | breather, **long one only** | After a real conversation he has two minutes and may have just fumbled one. Absent from the 15s dead end, where nobody picked up and it would be noise. |

The rail and the up-next card share the same 330px column and are strict
opposites — rail while dialing, up-next while breathing. Same slot in both
states so the eye never has to re-find it between calls.

**A separate tab was considered and is functionally broken here**, not merely
worse: the key handler is bound to `document`, so `SPACE`, `ENTER`, `1`–`5` and
`D` all go to whatever tab has focus. Reading the playbook in another tab means
being unable to end the call or log the disposition. A second physical screen
(iPad) does not have that problem and is fine as a reference shelf for the
between-call material — voicemail policy, gatekeeper nav, the DNC wording.

### Callback notes

Rows on tier `callback_due` get an amber panel carrying `Next Action Note` (the
promise) and `Notes` (the log it came from). Both were already in the queue
payload and were being served and thrown away — `render()` simply never drew
them, so the four calls he owes people opened with no memory of what he'd said.

The log is dropped when it only restates the promise. Comparison is normalized
(punctuation and connectives stripped) because the promise note is usually a
hand-retyped version of the log line: *"call back monday 8/3"* vs *"call back on
monday (8/3)"* is the real pair off the Morrison row, and exact comparison never
fires on it. Directional on purpose — a log that *contains* the promise is richer
and stays.

---

## Failure modes — the machine never stops

| Failure | Behavior |
|---|---|
| Airtable write fails | queued to `state/pending-writes.jsonl`, retried by a background thread, session continues |
| Twilio dial errors | 5s pause, `Attempts` **not** incremented, advance |
| Mic permission lost | loud banner, queue keeps advancing |
| Server/tab crash | `state/session-{date}.json` resumes mid-list; parked outcome committed on boot |
| Runaway loop | hard cap of 60 dials per session (cost guard) |
| Outside 8am–9pm | dialing refused (legal line) |
| Warmup would end after 9pm | START refused — the lock-in never runs toward a wall |
| Force-quit during the warmup | resumes into the time that is left; expired-while-dead comes back dialing |

**Rule: no technical problem may ever produce a moment where stopping is easier
than continuing.**

---

## Layout

```
server.py                 Flask app; owns the session
dialer/airtable.py        queue pull, outcome PATCH, retry queue, DNC refusal
dialer/outcomes.py        disposition -> fields, next-action, retirement  (--selftest)
dialer/twilio_voice.py    token minting, child-call lookup, caller-ID guards (--selftest)
static/                   index.html, dialer.js, dialer.css, manifest.json, vendor/
static/playbook.js        the on-screen talk track (beats, rail, angles) — content only
twilio-function/dial.js   deployed once to Twilio Functions, Protected
packaging/provision_twilio.py   idempotent Twilio setup
packaging/dryrun_check.py       79-check verification harness
packaging/install.sh            builds the icon, installs /Applications/No Brakes.app
state/                    gitignored: session, pending writes, abandons, logs
```

Every module self-tests standalone:

```bash
./.venv/bin/python3 -m dialer.outcomes --selftest
./.venv/bin/python3 -m dialer.airtable --selftest
./.venv/bin/python3 -m dialer.twilio_voice --selftest
```

---

Separate from the AIOS on purpose: no Claude, no hooks, no skill, no lockfile.
The AIOS side carries a `connections.md` entry, `references/twilio-api.md`, and
the decisions log.
