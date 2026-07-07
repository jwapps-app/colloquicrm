import { useEffect, useMemo, useState } from 'react';
import { del, get, post } from '../api';
import { useToast } from './Toast';
import { useAuth } from '../auth';
import { fmtDateTime, humanize } from '../format';
import { Loading, Empty } from './ui';

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
  const [notes, setNotes] = useState(null);
  const [activities, setActivities] = useState(null);
  const [emails, setEmails] = useState(null);
  const [openEmail, setOpenEmail] = useState(null); // id currently expanded
  const [bodies, setBodies] = useState({}); // id -> {loading, body_text, body_html, error}
  const [body, setBody] = useState('');
  const [busy, setBusy] = useState(false);

  async function load() {
    try {
      const [n, a, em] = await Promise.all([
        get('/notes', { entity_type: entityType, entity_id: entityId }),
        get('/activities', { entity_type: entityType, entity_id: entityId, page: 1, page_size: 50 }),
        entityType === 'opportunity'
          ? Promise.resolve({ items: [] })
          : get('/emails', { entity_type: entityType, entity_id: entityId }).catch(() => ({ items: [] })),
      ]);
      setNotes(n?.items || []);
      setActivities(a?.items || []);
      setEmails(em?.items || []);
    } catch (e) {
      toast.error(e.message);
      setNotes((v) => v || []);
      setActivities((v) => v || []);
      setEmails((v) => v || []);
    }
  }

  useEffect(() => {
    setNotes(null);
    setActivities(null);
    setEmails(null);
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [entityType, entityId]);

  const merged = useMemo(() => {
    if (!notes || !activities || !emails) return null;
    return [
      ...notes.map((n) => ({ ...n, _type: 'note', _at: n.created_at })),
      ...activities.map((a) => ({ ...a, _type: 'activity', _at: a.created_at })),
      ...emails.map((e) => ({ ...e, _type: 'email', _at: e.sent_at || e.created_at })),
    ].sort((x, y) => new Date(y._at) - new Date(x._at));
  }, [notes, activities, emails]);

  async function addNote(e) {
    e.preventDefault();
    const text = body.trim();
    if (!text) return;
    setBusy(true);
    try {
      await post('/notes', { entity_type: entityType, entity_id: entityId, body: text });
      setBody('');
      await load();
      toast.success('Note added');
    } catch (err) {
      toast.error(err.message);
    }
    setBusy(false);
  }

  async function toggleEmail(id) {
    if (openEmail === id) {
      setOpenEmail(null);
      return;
    }
    setOpenEmail(id);
    if (!bodies[id]) {
      setBodies((b) => ({ ...b, [id]: { loading: true } }));
      try {
        const body = await get(`/emails/${id}/body`);
        setBodies((b) => ({ ...b, [id]: { ...body, loading: false } }));
      } catch (e) {
        setBodies((b) => ({ ...b, [id]: { loading: false, error: e.message } }));
      }
    }
  }

  async function deleteNote(id) {
    if (!window.confirm('Delete this note?')) return;
    try {
      await del(`/notes/${id}`);
      setNotes((n) => n.filter((x) => x.id !== id));
    } catch (e) {
      toast.error(e.message);
    }
  }

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
          <button type="submit" className="btn btn-primary" disabled={busy || !body.trim()}>
            {busy ? 'Saving…' : 'Save note'}
          </button>
        </div>
      </form>

      {merged === null ? (
        <Loading label="Loading timeline…" />
      ) : merged.length === 0 ? (
        <Empty label="No notes or activity yet." hint="Notes you add will show up here." />
      ) : (
        <div className="timeline">
          {merged.map((item) =>
            item._type === 'email' ? (
              <div key={`e-${item.id}`} className={'timeline-item email-item' + (openEmail === item.id ? ' open' : '')}>
                <div className="timeline-head email-toggle" onClick={() => toggleEmail(item.id)} role="button" tabIndex={0}>
                  <span className="email-dir">{item.is_outgoing ? '↗' : '↘'}</span>
                  <strong>{item.is_outgoing ? 'Email sent' : `Email from ${item.from_name || item.from_email || 'unknown'}`}</strong>
                  <span className="muted"> · {fmtDateTime(item._at)}</span>
                  {user?.id === item.owner_user_id && item.gmail_id && (
                    <a
                      className="email-open muted"
                      href={`https://mail.google.com/mail/u/0/#all/${item.gmail_id}`}
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
                {openEmail === item.id && (
                  <div className="email-body-wrap">
                    {bodies[item.id]?.loading && <div className="muted">Loading message…</div>}
                    {bodies[item.id]?.error && <div className="form-error">{bodies[item.id].error}</div>}
                    {bodies[item.id]?.body_text && (
                      <div className="email-body">{bodies[item.id].body_text}</div>
                    )}
                    {!bodies[item.id]?.body_text && bodies[item.id]?.body_html && (
                      <iframe
                        title="email"
                        className="email-frame"
                        sandbox=""
                        srcDoc={bodies[item.id].body_html}
                      />
                    )}
                    {bodies[item.id] && !bodies[item.id].loading && !bodies[item.id].error
                      && !bodies[item.id].body_text && !bodies[item.id].body_html && (
                      <div className="muted">No readable content in this message.</div>
                    )}
                  </div>
                )}
              </div>
            ) : item._type === 'note' ? (
              <div key={`n-${item.id}`} className="timeline-item note-item">
                <div className="timeline-head">
                  <strong>{item.author_name || 'Someone'}</strong>
                  <span className="muted"> added a note · {fmtDateTime(item.created_at)}</span>
                  {(user?.is_admin || user?.id === item.author_id) && (
                    <button className="icon-btn tiny" onClick={() => deleteNote(item.id)} title="Delete note">
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
                  <span className="muted"> {humanize(item.kind).toLowerCase()} · {fmtDateTime(item.created_at)}</span>
                </div>
                {payloadSummary(item.payload) && <div className="muted activity-payload">{payloadSummary(item.payload)}</div>}
              </div>
            )
          )}
        </div>
      )}
    </div>
  );
}
