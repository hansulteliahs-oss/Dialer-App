/**
 * No Brakes dialer - the entire server-side dial path.
 *
 * Deployed once to Twilio Functions with visibility PROTECTED, so only requests
 * carrying a valid Twilio signature reach it. Public would mean a stranger with
 * the URL can place calls billed to this account.
 *
 * Invoked by Twilio itself when the browser calls device.connect({params:{To}}).
 * Custom params arrive merged with the standard call params, so `event.To` is
 * the number the browser asked for.
 *
 * CALLER_ID is a Function environment variable, not a literal, so changing the
 * outbound number needs no redeploy.
 *
 * Not an autodialer, by design (post-Facebook v. Duguid an ATDS requires a
 * random or sequential number generator):
 *   - one call at a time, never predictive
 *   - no prerecorded or synthetic voice, ever
 *   - numbers come only from the curated Airtable list, never generated
 */
exports.handler = function (context, event, callback) {
  const twiml = new Twilio.twiml.VoiceResponse();

  const to = (event.To || '').trim();
  const callerId = (context.CALLER_ID || '').trim();

  // Hard refusal: the mystery-shop persona line must never be shared with a
  // prospect. Mirrors the startup guard in server.py - defence in both layers.
  const PERSONA_LINE = '+18583564281';
  if (callerId === PERSONA_LINE || callerId.replace(/\D/g, '').endsWith('8583564281')) {
    console.error('REFUSED: persona line configured as CALLER_ID');
    twiml.say('Configuration error. The caller ID is not permitted.');
    return callback(null, twiml);
  }

  if (!callerId) {
    console.error('REFUSED: CALLER_ID env var is not set');
    twiml.say('Configuration error. No caller ID is configured.');
    return callback(null, twiml);
  }

  // E.164 only. Never dial anything the browser did not hand us verbatim.
  if (!/^\+1\d{10}$/.test(to)) {
    console.error(`REFUSED: bad destination ${JSON.stringify(to)}`);
    twiml.say('Configuration error. The destination number is not valid.');
    return callback(null, twiml);
  }

  // answerOnBridge: he hears real ringback, and the child leg is not marked
  // answered until the lead actually picks up - which is what makes
  // "completed with nonzero duration" a trustworthy connect signal.
  const dial = twiml.dial({
    callerId: callerId,
    answerOnBridge: true,
    timeout: 22,
  });
  dial.number(to);

  callback(null, twiml);
};
