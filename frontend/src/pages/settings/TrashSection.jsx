import { useEffect, useState } from 'react';
import { bustCache, get, post } from '../../api';
import { useToast } from '../../components/Toast';
import { Loading } from '../../components/ui';
import { fmtDate } from '../../format';
import { RECORD_TYPES } from '../../constants/entities';

/* ---------- Trash ---------- */

const TRASH_TYPES = RECORD_TYPES.map((r) => ({ api: `/${r.plural}`, label: r.pluralLabel }));

export default function TrashSection() {
  const toast = useToast();
  const [groups, setGroups] = useState(null);
  const [loadError, setLoadError] = useState(false);
  const [busyId, setBusyId] = useState(null);

  async function load() {
    const out = {};
    const results = await Promise.allSettled(
      TRASH_TYPES.map((t) => get(`${t.api}/trash/list`))
    );
    results.forEach((r, i) => {
      if (r.status === 'fulfilled' && r.value.items?.length) {
        out[TRASH_TYPES[i].api] = { label: TRASH_TYPES[i].label, items: r.value.items };
      }
    });
    // A failed list must not masquerade as an empty Trash.
    setLoadError(results.some((r) => r.status === 'rejected'));
    setGroups(out);
  }

  useEffect(() => {
    load();
  }, []);

  async function restore(api, item) {
    setBusyId(item.id);
    try {
      await post(`${api}/${item.id}/restore`);
      bustCache('/tags'); // restoring can revive tags hidden while trashed
      setGroups((g) => {
        const next = { ...g };
        const grp = { ...next[api], items: next[api].items.filter((x) => x.id !== item.id) };
        if (grp.items.length) next[api] = grp;
        else delete next[api];
        return next;
      });
      toast.success(`Restored ${item.label}`);
    } catch (e) {
      toast.error(e.message);
    }
    setBusyId(null);
  }

  const total = groups ? Object.values(groups).reduce((n, g) => n + g.items.length, 0) : 0;

  return (
    <div className="card settings-card">
      <h3>Trash</h3>
      <p className="muted">
        Deleted records are kept here for 60 days, then permanently removed automatically. There's
        no manual empty — so anything deleted has the full window to be noticed and restored.
      </p>
      {groups === null ? (
        <Loading small />
      ) : loadError ? (
        <div className="muted panel-empty">
          Couldn&apos;t load the Trash.{' '}
          <button
            className="linklike"
            onClick={() => {
              setGroups(null);
              load();
            }}
          >
            Retry
          </button>
        </div>
      ) : total === 0 ? (
        <div className="muted panel-empty">Trash is empty.</div>
      ) : (
        Object.entries(groups).map(([api, grp]) => (
          <div key={api} className="trash-group">
            <h4 className="panel-title">
              {grp.label} ({grp.items.length})
            </h4>
            {grp.items.map((item) => (
              <div key={item.id} className="trash-row">
                <span className="trash-label">{item.label}</span>
                <span className="muted trash-when">deleted {fmtDate(item.deleted_at)}</span>
                <button
                  className="btn btn-small"
                  onClick={() => restore(api, item)}
                  disabled={busyId === item.id}
                >
                  Restore
                </button>
              </div>
            ))}
          </div>
        ))
      )}
    </div>
  );
}
