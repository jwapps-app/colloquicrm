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
  const [body, setBody] = useState('');
  const [busy, setBusy] = useState(false);

  async function load() {
    try {
      const [n, a] = await Promise.all([
        get('/notes', { entity_type: entityType, entity_id: entityId }),
        get('/activities', { entity_type: entityType, entity_id: entityId, page: 1, page_size: 50 }),
      ]);
      setNotes(n?.items || []);
      setActivities(a?.items || []);
    } catch (e) {
      toast.error(e.message);
      setNotes((v) => v || []);
      setActivities((v) => v || []);
    }
  }

  useEffect(() => {
    setNotes(null);
    setActivities(null);
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [entityType, entityId]);

  const merged = useMemo(() => {
    if (!notes || !activities) return null;
    return [
      ...notes.map((n) => ({ ...n, _type: 'note' })),
      ...activities.map((a) => ({ ...a, _type: 'activity' })),
    ].sort((x, y) => new Date(y.created_at) - new Date(x.created_at));
  }, [notes, activities]);

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
            item._type === 'note' ? (
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
