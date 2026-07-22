import { useEffect, useRef, useState } from 'react';
import { del, download, get, upload } from '../api';
import { useToast } from './Toast';
import { fmtDate, humanSize } from '../format';
import { Loading } from './ui';

/** Files attached to a record. Upload goes through the hidden file input;
 * clicking a row downloads the file through the authed blob helper. */
export default function AttachmentsPanel({ entityType, entityId }) {
  const toast = useToast();
  const [items, setItems] = useState(null);
  const [error, setError] = useState(null);
  const [uploading, setUploading] = useState(false);
  const [version, setVersion] = useState(0);
  const fileRef = useRef(null);

  useEffect(() => {
    let on = true;
    setError(null);
    get('/attachments', { entity_type: entityType, entity_id: entityId })
      .then((d) => {
        if (on) setItems(d?.items || []);
      })
      .catch((e) => {
        if (!on) return;
        setError(e.message);
        setItems([]);
      });
    return () => {
      on = false;
    };
  }, [entityType, entityId, version]);

  async function onPick(e) {
    const file = e.target.files?.[0];
    e.target.value = ''; // so picking the same file again re-fires onChange
    if (!file) return;
    const fd = new FormData();
    fd.append('file', file);
    fd.append('entity_type', entityType);
    fd.append('entity_id', entityId);
    setUploading(true);
    try {
      await upload('/attachments', fd);
      toast.success(`Uploaded ${file.name}`);
      setVersion((v) => v + 1);
    } catch (err) {
      toast.error(err.status === 413 ? 'File is too large (max 25 MB)' : err.message);
    }
    setUploading(false);
  }

  async function open(a) {
    try {
      await download(`/attachments/${a.id}/download`);
    } catch (e) {
      toast.error(e.message);
    }
  }

  async function remove(e, a) {
    e.stopPropagation(); // the row click would also trigger a download
    if (!window.confirm(`Delete "${a.filename}"? This can't be undone.`)) return;
    try {
      await del(`/attachments/${a.id}`);
      setVersion((v) => v + 1);
    } catch (err) {
      toast.error(err.message);
    }
  }

  return (
    <div className="card">
      <div className="panel-head">
        <h4 className="panel-title">Attachments</h4>
        <button
          className="btn btn-small"
          onClick={() => fileRef.current?.click()}
          disabled={uploading}
        >
          {uploading ? 'Uploading…' : 'Add file'}
        </button>
        <input type="file" ref={fileRef} onChange={onPick} hidden />
      </div>
      {items === null ? (
        <Loading small label="Loading…" />
      ) : error ? (
        <div className="muted panel-empty">
          Couldn&apos;t load attachments.{' '}
          <button className="linklike" onClick={() => setVersion((v) => v + 1)}>
            Retry
          </button>
        </div>
      ) : items.length === 0 ? (
        <div className="muted panel-empty">No attachments.</div>
      ) : (
        <div className="attachment-list">
          {items.map((a) => (
            <div
              key={a.id}
              className="attachment-row"
              onClick={() => open(a)}
              role="button"
              tabIndex={0}
              onKeyDown={(e) => {
                if (e.key === 'Enter' || e.key === ' ') {
                  e.preventDefault();
                  open(a);
                }
              }}
              title={`Download ${a.filename}`}
            >
              <div className="attachment-main">
                <span className="attachment-name">{a.filename}</span>
                <span className="muted attachment-meta">
                  {humanSize(a.size_bytes)}
                  {a.uploaded_by_name ? ` · ${a.uploaded_by_name}` : ''} · {fmtDate(a.created_at)}
                </span>
              </div>
              <button
                className="icon-btn tiny"
                onClick={(e) => remove(e, a)}
                title="Delete attachment"
                aria-label="Delete attachment"
              >
                ×
              </button>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
