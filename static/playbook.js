/* No Brakes - the on-screen talk track.
 *
 * SOURCE OF TRUTH IS NOT THIS FILE. Every line below is a compression of
 * `references/cold-call-playbook.md` in the AIOS repo (~/AI Operating System).
 * That file owns the channel; this one owns how much of it fits on a screen
 * while a human is on the phone. Same two-writers-one-contract problem as
 * cold_call_log.py / dialer/outcomes.py: change the playbook's objection list
 * or its four beats and this file has to move with it, in the other repo.
 *
 * The compression rule, taken from the playbook's own framing ("Don't memorize
 * lines. Internalize the angle, then say it your way."): RAIL entries are
 * ANGLES, never lines to recite. Twelve words is the ceiling. If an entry grows
 * into a sentence he can read aloud, it has become a script and it is wrong -
 * reading makes you sound like you are reading, which is the exact failure the
 * sparse live card exists to prevent.
 *
 * ANGLES carries the same eight objections at full length. It is only ever
 * shown between calls, so it is allowed to be prose.
 */

window.PLAYBOOK = {

  // The four beats. Warmup screen only - he has five idle minutes there and
  // nothing else to do with his eyes.
  beats: [
    ['interrupt', 'name yourself, admit it’s a cold call, ask for 27 seconds'],
    ['the leak', 'one line about their world → calls after 5 go to voicemail, quotes sit, nobody chases'],
    ['the ask', '"open to 30 minutes to see exactly what’s leaking?"'],
    ['book live', 'two times, get the email, send the invite while still on the line, hang up'],
  ],

  // Non-negotiables. Short enough to hold in working memory, which is the test
  // for whether they belong on the warmup screen at all.
  rules: [
    'Never say AI, automation, or a tool name. The subject is the leak.',
    'The audit is the only CTA. No pitching, no prices, on the phone.',
    'Under 90 seconds if there is no friction.',
    'No voicemail on attempts 1 and 2. Brief one on attempt 3.',
    'No name? "Who’s the owner over there?" Never "may I speak to the owner."',
    'Front desk: get the callback window every time. Press G, never 3.',
  ],

  // The live-call rail. Trigger phrase he will actually hear, then the angle.
  // Ordered by how often the playbook expects each one, commonest first, so his
  // eye learns fixed positions and stops reading the list as a list.
  rail: [
    // First because it is not close: all 20 dials on 2026-08-13 ended here.
    ['"he’s out on a job"', 'get the window. a fact, never a pitch. press G'],
    ['"can I take a message?"', 'the form you never answered. not the one-liner'],
    ['"no time"', 'agree — that’s exactly when calls slip'],
    ['"not interested"', 'in what? you haven’t pitched. → "how many did you miss last week?"'],
    ['"wife / office mgr handles it"', 'include them, both on the call'],
    ['"we use Jobber / ServiceTitan"', 'good sign, not competition. the gap is around it'],
    ['"tried a chatbot, it sucked"', 'agree completely. supervised, your people stay in the loop'],
    ['"send me some info"', 'calendar before email. send the Calendly with it'],
    ['"who is this?"', 'direct. public business database. offer to take them off'],
    ['"take me off your list"', 'no rebuttal. "I’ll take you off right now." press D'],
  ],

  // Full angles. Long breather only - after a real conversation, when he may
  // have just fumbled one of these and has two minutes to see what he could
  // have said. Deliberately absent from the 15-second dead-end breather, where
  // it would be noise on a call that never connected.
  angles: [
    // Added 2026-08-13. Not an objection — the most common outcome on the board,
    // and the one he had no branch for. Two days of dials all ended here.
    ['"He’s out on a job — can I take a message?"',
     'A receptionist can relay a FACT. She cannot relay a pitch. "I help home service businesses stop losing jobs to missed calls" is a pitch — abstract, sounds like every vendor, gone by the time she reaches the truck. Ask for the window first, every time: when’s he usually easiest to catch, morning or afternoon? That is the thing she can actually give you — she can’t book a meeting and won’t sell one for you. Only if she offers to take a message, hand her one concrete fact: tell him I filled out the quote form on your site last week and never heard back, that’s the reason for the call. Then immediately — not calling to complain, that’s literally the thing I fix — because she may be the one who handles those forms. Close on a plan, never on a value statement: I’ll try back {window}, thanks {name}. Ending on what you do is a period she can’t act on, and that pause is where the call dies. Press G.'],
    ['"I don’t have time."',
     'Agree, then tie it to the leak. When you’re this slammed is exactly when calls slip — 30 minutes next week and I’ll show you which ones.'],
    ['"I’m not interested."',
     'Confirm what they’re not interested in, because you haven’t pitched yet. Then redirect: do you know how many calls you missed last week, or is that nobody’s job to count? If they know and it’s handled, that’s a real conversation — no rebuttal. If they go quiet, that’s the opening.'],
    ['"My office manager / wife handles all that."',
     'Respect it and include them. They’d know exactly where it slips — could we do the 30 minutes with both of you? Never imply the office manager is the problem. They are the future champion or the future blocker, nothing in between.'],
    ['"We use Jobber / ServiceTitan / Housecall Pro."',
     'That is a good sign, not competition — they invest in ops. The gap is what happens around it: the after-hours call the software never sees, the quote sitting in "sent" that nobody chases. Does the tool handle those today? The honest answer is usually no.'],
    ['"We tried a chatbot / answering service and it was terrible."',
     'Agree completely — that is the positioning lane. Most of those embarrass the business in front of customers. This runs supervised, your people stay in the loop, and you see what it caught every month.'],
    ['"Send me some info."',
     'Get the calendar before the email. Happy to — easier if I send the Calendly too, and even if you decide not to take the call you’ll have my info.'],
    ['"Who is this? / How did you get my number?"',
     'Be direct. Eliahs Hansult from Handled, I work with shops your size on lost jobs from missed calls, and I got your name from a public business database. If you’d rather not be contacted I can take you off right now.'],
    ['"Take me off your list."',
     'Honor it on the spot, no rebuttal, no last pitch. "Understood, I’ll take you off right now. Sorry for the bother." End the call and press D — that sets the DNC box, which is the deny-trail nothing will ever restage.'],
    // TRACK 3 only (2+ no-answers on file). Rare, so it stays out of the rail,
    // but it is the one objection that decides that whole call.
    ['"You’re a salesman, that’s different."',
     'Concede it fully, then reframe — do not argue the premise, he is right. Totally, and you should ignore me. But your phone can’t tell us apart: same unknown number as the homeowner whose AC died at 7pm. Never mention the voicemail, that hands him "I could tell it was a sales call."'],
  ],
};
