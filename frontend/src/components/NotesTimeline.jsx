import { useEffect, useMemo, useRef, useState } from 'react';
import { del, get, patch, post } from '../api';
import { EmailBody, useEmailBodies } from './EmailBody';
import { useToast } from './Toast';
import { useAuth } from '../auth';
import { fmtDateTime, humanize } from '../format';
import { Loading, Empty } from './ui';
import { callMeta, callTitle, emailTitle, gmailUrl, smsTitle } from './timelineText';
import { recordType } from '../constants/entities';

const PAGE_SIZE = 50;

function payloadSummary(payload) {
  if (!payload || typeof payload !== 'object') return null;
  const entries = Object.entries(payload).slice(0, 4);
  if (entries.length === 0) return null;
  return entries
    .map(([k, v]) => `${humanize(k)}: ${typeof v === 'object' && v !== null ? JSON.stringify(v) : String(v)}`)
    .join(' · ');
}

export default function NotesTimeline({ entityType, entityId }) {
  const toast = useToast();
  const { user } = useAuth();
  // The server merges, orders and pages the record's notes, activity,
  // emails, calls and texts (GET /timeline); `items` is every page loaded so
  // far, newest first. null = first load still in flight.
  const [items, setItems] = useState(null);
  const [pages, setPages] = useState(1);
  const [hasMore, setHasMore] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const { open: openEmail, toggle: toggleEmail, bodies, retry: retryEmail } = useEmailBodies();
  const [body, setBody] = useState('');
  const [busy, setBusy] = useState(false);
  const [logCall, setLogCall] = useState(false);
  const [callDir, setCallDir] = useState('outbound');
  const [composerFor, setComposerFor] = useState(null); // call id with open note composer
  const [callDraft, setCallDraft] = useState('');
  const [attachFor, setAttachFor] = useState(null); // call id with open attach picker
  const loadEpoch = useRef(0);

  const fetchPage = (page) =>
    get('/timeline', { entity_type: entityType, entity_id: entityId, page, page_size: PAGE_SIZE });

  // (Re)load the first `count` pages — after a change, everything already on
  // screen is refreshed, not just the newest page.
  async function load(count = pages) {
    const epoch = ++loadEpoch.current;
    try {
      const loaded = [];
      let more = false;
      for (let page = 1; page <= count; page += 1) {
        const d = await fetchPage(page);
        if (epoch !== loadEpoch.current) return; // superseded by a newer load
        loaded.push(...(d?.items || []));
        more = !!d?.has_more;
        if (!more) break;
      }
      setItems(loaded);
      setHasMore(more);
    } catch (e) {
      if (epoch !== loadEpoch.current) return;
      toast.error(e.message);
      setItems((v) => v || []);
    }
  }

  async function loadMore() {
    const epoch = ++loadEpoch.current;
    setLoadingMore(true);
    try {
      const d = await fetchPage(pages + 1);
      if (epoch !== loadEpoch.current) return;
      setItems((prev) => [...(prev || []), ...(d?.items || [])]);
      setHasMore(!!d?.has_more);
      setPages((n) => n + 1);
    } catch (e) {
      if (epoch === loadEpoch.current) toast.error(e.message);
    } finally {
      setLoadingMore(false);
    }
  }

  useEffect(() => {
    setItems(null);
    setPages(1);
    setHasMore(false);
    load(1);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [entityType, entityId]);

  // Notes of this record not yet tied to a call or text — what "Attach
  // existing note" offers. (A note already on a call shown here arrives
  // inside that call's `notes`, not as an item.)
  const unattachedNotes = useMemo(
    () => (items || []).filter((i) => i.type === 'note' && !i.phone_event_id),
    [items]
  );

  const canLogCall = !!recordType(entityType)?.logsCalls;

  async function addNote(e) {
    e.preventDefault();
    const text = body.trim();
    if (!text) return;
    setBusy(true);
    try {
      const payload = { entity_type: entityType, entity_id: entityId, body: text };
      if (canLogCall && logCall) {
        payload.log_call = true;
        payload.call_direction = callDir;
      }
      await post('/notes', payload);
      setBody('');
      setLogCall(false);
      await load();
      toast.success(logCall ? 'Call logged' : 'Note added');
    } catch (err) {
      toast.error(err.message);
    }
    setBusy(false);
  }

  async function addCallNote(phoneEventId, text) {
    const t = (text || '').trim();
    if (!t) return;
    try {
      await post('/notes', {
        entity_type: entityType,
        entity_id: entityId,
        body: t,
        phone_event_id: phoneEventId,
      });
      setCallDraft('');
      setComposerFor(null);
      await load();
      toast.success('Note added to call');
    } catch (err) {
      toast.error(err.message);
    }
  }

  async function attachNote(noteId, phoneEventId) {
    try {
      await patch(`/notes/${noteId}`, { phone_event_id: phoneEventId });
      setAttachFor(null);
      await load();
      toast.success('Note linked to call');
    } catch (err) {
      toast.error(err.message);
    }
  }

  async function detachNote(noteId) {
    try {
      await patch(`/notes/${noteId}`, { phone_event_id: null });
      await load();
    } catch (err) {
      toast.error(err.message);
    }
  }

  async function deleteNote(id) {
    if (!window.confirm('Delete this note?')) return;
    try {
      await del(`/notes/${id}`);
      setItems((list) => (list || []).filter((x) => !(x.type === 'note' && x.id === id)));
    } catch (e) {
      toast.error(e.message);
    }
  }

  // This record's notes about a call or text, shown under that event.
  const linkedNotes = (event) =>
    (event.notes || []).map((n) => (
      <div key={n.id} className="call-note">
        <div className="call-note-body">{n.body}</div>
        <div className="call-note-meta muted">
          {n.author_name || 'Someone'} · {fmtDateTime(n.created_at)}
          {(user?.is_admin || user?.id === n.author_id) && (
            <>
              {' · '}
              <button className="linklike" onClick={() => detachNote(n.id)}>unlink</button>
            </>
          )}
        </div>
      </div>
    ));

  return (
    <div className="timeline-col">
      <form className="card composer" onSubmit={addNote}>
        <textarea
          rows={3}
          placeholder="Add a note…"
          value={body}
          onChange={(e) => setBody(e.target.value)}
        />
        <div className="composer-actions">
          {canLogCall && (
            <div className="call-log-toggle">
              <label className="call-log-check">
                <input type="checkbox" checked={logCall} onChange={(e) => setLogCall(e.target.checked)} />
                ☎ This was a call
              </label>
              {logCall && (
                <div className="call-dir">
                  <button
                    type="button"
                    className={'btn btn-small' + (callDir === 'outbound' ? ' btn-primary' : '')}
                    onClick={() => setCallDir('outbound')}
                  >
                    ↗ Outgoing
                  </button>
                  <button
                    type="button"
                    className={'btn btn-small' + (callDir === 'inbound' ? ' btn-primary' : '')}
                    onClick={() => setCallDir('inbound')}
                  >
                    ↘ Incoming
                  </button>
                </div>
              )}
            </div>
          )}
          <button type="submit" className="btn btn-primary" disabled={busy || !body.trim()}>
            {busy ? 'Saving…' : logCall ? 'Log call' : 'Save note'}
          </button>
        </div>
      </form>

      {items === null ? (
        <Loading label="Loading timeline…" />
      ) : items.length === 0 ? (
        <Empty label="No notes or activity yet." hint="Notes you add will show up here." />
      ) : (
        <div className="timeline">
          {items.map((item) =>
            item.type === 'call' ? (
              <div key={`c-${item.id}`} className="timeline-item phone-item">
                <div className="timeline-head">
                  <span className="phone-icon">☎</span>
                  <strong>{callTitle(item)}</strong>
                  <span className="muted"> · {fmtDateTime(item.at)}</span>
                </div>
                <div className="muted">{callMeta(item)}</div>

                {linkedNotes(item)}

                {composerFor === item.id ? (
                  <div className="call-note-composer">
                    <textarea
                      rows={2}
                      autoFocus
                      placeholder="Note about this call…"
                      value={callDraft}
                      onChange={(e) => setCallDraft(e.target.value)}
                    />
                    <div className="composer-actions">
                      <button className="btn btn-small" onClick={() => { setComposerFor(null); setCallDraft(''); }}>
                        Cancel
                      </button>
                      <button
                        className="btn btn-small btn-primary"
                        disabled={!callDraft.trim()}
                        onClick={() => addCallNote(item.id, callDraft)}
                      >
                        Save note
                      </button>
                    </div>
                  </div>
                ) : attachFor === item.id ? (
                  <div className="call-note-composer">
                    {unattachedNotes.length === 0 ? (
                      <div className="muted">No unlinked notes to attach.</div>
                    ) : (
                      unattachedNotes.map((n) => (
                        <div key={n.id} className="attach-option">
                          <div className="attach-preview">{n.body.slice(0, 120)}</div>
                          <button className="btn btn-small" onClick={() => attachNote(n.id, item.id)}>
                            Attach
                          </button>
                        </div>
                      ))
                    )}
                    <div className="composer-actions">
                      <button className="btn btn-small" onClick={() => setAttachFor(null)}>Done</button>
                    </div>
                  </div>
                ) : (
                  <div className="call-note-actions">
                    <button className="linklike" onClick={() => { setComposerFor(item.id); setAttachFor(null); }}>
                      + Add note
                    </button>
                    {unattachedNotes.length > 0 && (
                      <button className="linklike" onClick={() => { setAttachFor(item.id); setComposerFor(null); }}>
                        Attach existing note
                      </button>
                    )}
                  </div>
                )}
              </div>
            ) : item.type === 'sms' ? (
              <div key={`s-${item.id}`} className="timeline-item phone-item sms-item">
                <div className="timeline-head">
                  <span className="phone-icon">💬</span>
                  <strong>{smsTitle(item)}</strong>
                  <span className="muted"> · {fmtDateTime(item.at)}</span>
                </div>
                {item.text && <div className="sms-body">{item.text}</div>}
                {linkedNotes(item)}
              </div>
            ) : item.type === 'email' ? (
              <div key={`e-${item.id}`} className={'timeline-item email-item' + (openEmail === item.id ? ' open' : '')}>
                <div
                  className="timeline-head email-toggle"
                  onClick={() => toggleEmail(item.id)}
                  role="button"
                  tabIndex={0}
                  onKeyDown={(e) => {
                    // Enter on the nested Gmail link must follow the link.
                    if (e.target !== e.currentTarget) return;
                    if (e.key === 'Enter' || e.key === ' ') {
                      e.preventDefault();
                      toggleEmail(item.id);
                    }
                  }}
                >
                  <span className="email-dir">{item.is_outgoing ? '↗' : '↘'}</span>
                  <strong>{emailTitle(item)}</strong>
                  <span className="muted"> · {fmtDateTime(item.at)}</span>
                  {gmailUrl(item, user) && (
                    <a
                      className="email-open muted"
                      href={gmailUrl(item, user)}
                      target="_blank"
                      rel="noreferrer"
                      title="Open in Gmail"
                    >
                      Gmail ↗
                    </a>
                  )}
                </div>
                <div className="email-subject email-toggle" onClick={() => toggleEmail(item.id)}>
                  {item.subject || '(no subject)'}
                </div>
                {openEmail !== item.id && item.snippet && (
                  <div className="muted email-snippet">{item.snippet}</div>
                )}
                {openEmail === item.id && <EmailBody body={bodies[item.id]} onRetry={() => retryEmail(item.id)} />}
              </div>
            ) : item.type === 'note' ? (
              <div key={`n-${item.id}`} className="timeline-item note-item">
                <div className="timeline-head">
                  <strong>{item.author_name || 'Someone'}</strong>
                  <span className="muted">
                    {' '}
                    added a note{item.phone_event_id ? ' on a call or text not shown here' : ''} · {fmtDateTime(item.at)}
                  </span>
                  {(user?.is_admin || user?.id === item.author_id) && (
                    <button className="icon-btn tiny" onClick={() => deleteNote(item.id)} title="Delete note" aria-label="Delete note">
                      ×
                    </button>
                  )}
                </div>
                <div className="note-body">{item.body}</div>
              </div>
            ) : (
              <div key={`a-${item.id}`} className="timeline-item activity-item">
                <div className="timeline-head">
                  <strong>{item.actor_name || 'System'}</strong>
                  <span className="muted"> {humanize(item.kind).toLowerCase()} · {fmtDateTime(item.at)}</span>
                </div>
                {payloadSummary(item.payload) && <div className="muted activity-payload">{payloadSummary(item.payload)}</div>}
              </div>
            )
          )}
          {hasMore && (
            <div className="feed-more">
              <button className="btn" disabled={loadingMore} onClick={loadMore}>
                {loadingMore ? 'Loading…' : 'Load older'}
              </button>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
