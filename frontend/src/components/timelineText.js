// How a timeline item reads, wherever it is drawn. The feed and a record's
// own timeline lay items out differently, but they say the same thing about
// them — the wording lives here so the two can't drift apart.

// Results that just mean "the call happened" — not worth repeating.
const QUIET_CALL_RESULTS = ['Call connected', 'Accepted'];

export function callTitle(item) {
  const base = item.direction === 'outbound' ? 'Outgoing call' : 'Incoming call';
  return item.result && !QUIET_CALL_RESULTS.includes(item.result) ? `${base} — ${item.result}` : base;
}

export function smsTitle(item) {
  return item.direction === 'outbound' ? 'Text sent' : 'Text received';
}

/** "3m 12s · +15551234567 · recorded" */
export function callMeta(item) {
  const secs = item.duration_seconds;
  const duration = secs != null && secs > 0 ? `${Math.floor(secs / 60)}m ${secs % 60}s · ` : '';
  return `${duration}${item.other_number || ''}${item.recording_id ? ' · recorded' : ''}`;
}

export function emailTitle(item) {
  return item.is_outgoing ? 'Email sent' : `Email from ${item.from_name || item.from_email || 'unknown'}`;
}

/** Link to the message in Gmail — only for the user whose mailbox holds it. */
export function gmailUrl(item, user) {
  return user?.id === item.owner_user_id && item.gmail_id
    ? `https://mail.google.com/mail/u/0/#all/${item.gmail_id}`
    : null;
}
