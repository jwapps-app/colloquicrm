import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { get, post } from '../api';
import { useToast } from './Toast';
import Modal from './Modal';
import { entityPath, fmtDate } from '../format';

const MERGE_PATHS = { person: '/people', company: '/companies', lead: '/leads' };

const groupKey = (g) => g.items.map((i) => i.id).join(':');

/**
 * Slim advisory strip above a list: possible duplicate groups from
 * /duplicates, with a review modal that merges a group's records into the
 * one the user keeps. Renders nothing when there's nothing to review.
 */
export default function DuplicatesBanner({ entityType, onMerged }) {
  const toast = useToast();
  const [groups, setGroups] = useState([]);
  const [open, setOpen] = useState(false);
  const [keep, setKeep] = useState({}); // group key -> id of the record to keep
  const [busyKey, setBusyKey] = useState(null);
  const [version, setVersion] = useState(0);

  useEffect(() => {
    let on = true;
    // Advisory only — a failed fetch just leaves the banner hidden.
    get('/duplicates', { entity_type: entityType })
      .then((d) => {
        if (on) setGroups(d?.groups || []);
      })
      .catch(() => {});
    return () => {
      on = false;
    };
  }, [entityType, version]);

  if (!MERGE_PATHS[entityType]) return null;

  async function mergeGroup(g) {
    const key = groupKey(g);
    const target = g.items.find((i) => i.id === (keep[key] || g.items[0].id));
    const sources = g.items.filter((i) => i.id !== target.id);
    if (
      !window.confirm(
        `Merge ${g.items.length} records into "${target.label}"? This can't be undone.`
      )
    )
      return;
    setBusyKey(key);
    let merged = 0;
    try {
      for (const s of sources) {
        // Sequential on purpose — parallel merges into the same target race.
        await post(`${MERGE_PATHS[entityType]}/${target.id}/merge`, { source_id: s.id });
        merged += 1;
      }
      toast.success(
        `Merged ${merged} record${merged === 1 ? '' : 's'} into “${target.label}”`
      );
      setGroups((gs) => {
        const next = gs.filter((x) => groupKey(x) !== key);
        if (next.length === 0) setOpen(false);
        return next;
      });
      onMerged?.(); // refresh the list behind the modal
      setVersion((v) => v + 1); // a merge can dissolve other groups too
    } catch (e) {
      // Stop this group where it failed; anything already merged stands.
      toast.error(
        merged > 0 ? `Merged ${merged} of ${sources.length}, then failed: ${e.message}` : e.message
      );
      if (merged > 0) {
        onMerged?.();
        setVersion((v) => v + 1);
      }
    }
    setBusyKey(null);
  }

  if (groups.length === 0) return null;

  return (
    <>
      <div className="dup-banner">
        <span className="muted">
          {groups.length} possible duplicate group{groups.length === 1 ? '' : 's'} found
        </span>
        <button className="linklike" onClick={() => setOpen(true)}>
          Review
        </button>
      </div>
      {open && (
        <Modal title="Possible duplicates" onClose={() => setOpen(false)}>
          <p className="muted">
            Pick the record to keep in each group. The others&apos; notes, tasks, and fields fold
            into it, then they&apos;re merged away.
          </p>
          {groups.map((g) => {
            const key = groupKey(g);
            const kept = keep[key] || g.items[0].id;
            const busy = busyKey === key;
            return (
              <div key={key} className="dup-group">
                <div className="dup-reason">{g.reason}</div>
                {g.items.map((it) => (
                  <label key={it.id} className="dup-item">
                    <input
                      type="radio"
                      name={`keep-${key}`}
                      checked={kept === it.id}
                      onChange={() => setKeep((k) => ({ ...k, [key]: it.id }))}
                      title="Keep this record"
                    />
                    <span className="dup-item-main">
                      <Link to={entityPath(g.entity_type, it.id)}>{it.label}</Link>
                      {it.sublabel && <span className="muted dup-sublabel">{it.sublabel}</span>}
                    </span>
                    <span className="muted dup-created">Added {fmtDate(it.created_at)}</span>
                  </label>
                ))}
                <div className="dup-group-actions">
                  <button
                    className="btn btn-small btn-primary"
                    onClick={() => mergeGroup(g)}
                    disabled={busyKey !== null}
                  >
                    {busy ? 'Merging…' : 'Merge into selected'}
                  </button>
                </div>
              </div>
            );
          })}
        </Modal>
      )}
    </>
  );
}
