import { useEffect, useState } from 'react';
import { get, post } from '../api';
import { useToast } from './Toast';
import Modal from './Modal';

const nameOf = (r) =>
  r.name || [r.first_name, r.last_name].filter(Boolean).join(' ') || '(unnamed)';
const hintOf = (r) => r.work_email || r.personal_email || r.email || r.email_domain || '';

/**
 * "Merge…" action for detail pages. The open record survives; the picked
 * duplicate's data folds into it and the duplicate is deleted.
 */
export default function MergeButton({ apiPath, entityId, label, onMerged }) {
  const toast = useToast();
  const [open, setOpen] = useState(false);
  const [q, setQ] = useState('');
  const [results, setResults] = useState(null);
  const [pick, setPick] = useState(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!open) return;
    let on = true;
    const t = setTimeout(() => {
      get(apiPath, { q: q || undefined, page_size: 8 })
        .then((d) => {
          if (on) setResults((d?.items || []).filter((r) => r.id !== entityId));
        })
        .catch(() => {
          if (on) setResults([]);
        });
    }, 250);
    return () => {
      on = false;
      clearTimeout(t);
    };
  }, [open, q, apiPath, entityId]);

  function close() {
    setOpen(false);
    setPick(null);
    setQ('');
    setResults(null);
  }

  async function merge() {
    setBusy(true);
    try {
      await post(`${apiPath}/${entityId}/merge`, { source_id: pick.id });
      toast.success(`Merged “${nameOf(pick)}” into this record`);
      close();
      onMerged?.();
    } catch (e) {
      toast.error(e.message);
    }
    setBusy(false);
  }

  return (
    <>
      <button className="btn" onClick={() => setOpen(true)}>
        Merge…
      </button>
      {open && (
        <Modal title={`Merge a duplicate into “${label}”`} onClose={close}>
          {!pick ? (
            <>
              <p className="muted">
                Find the duplicate record. Its notes, tasks, calls, tags, and custom fields move
                here; fields this record is missing fill in from it; then it's deleted.
              </p>
              <input
                type="search"
                className="search-input merge-search"
                placeholder="Search for the duplicate…"
                value={q}
                onChange={(e) => setQ(e.target.value)}
                autoFocus
              />
              <div className="merge-results">
                {(results || []).map((r) => (
                  <button key={r.id} className="merge-candidate" onClick={() => setPick(r)}>
                    <span>{nameOf(r)}</span>
                    <span className="muted">{hintOf(r)}</span>
                  </button>
                ))}
                {results && results.length === 0 && <p className="muted">No matches.</p>}
              </div>
            </>
          ) : (
            <>
              <p>
                Keep <strong>{label}</strong> and merge <strong>{nameOf(pick)}</strong> into it?
              </p>
              <p className="muted">
                “{nameOf(pick)}” will be deleted after its data moves over. This can't be undone.
              </p>
              <div className="form-actions">
                <button className="btn btn-ghost" onClick={() => setPick(null)} disabled={busy}>
                  ← Back
                </button>
                <button className="btn btn-primary" onClick={merge} disabled={busy}>
                  {busy ? 'Merging…' : 'Merge records'}
                </button>
              </div>
            </>
          )}
        </Modal>
      )}
    </>
  );
}
