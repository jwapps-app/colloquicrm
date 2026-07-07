import { useEffect, useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import { get } from '../api';
import { useToast } from '../components/Toast';
import { Empty, Loading } from '../components/ui';
import { entityPath, humanize, timeOfDay } from '../format';

const PAGE_SIZE = 50;

function dayLabel(d) {
  const today = new Date();
  const yesterday = new Date(Date.now() - 86400000);
  if (d.toDateString() === today.toDateString()) return 'Today';
  if (d.toDateString() === yesterday.toDateString()) return 'Yesterday';
  return d.toLocaleDateString([], { weekday: 'long', month: 'long', day: 'numeric', year: 'numeric' });
}

export default function Feed() {
  const toast = useToast();
  const [items, setItems] = useState(null);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [loadingMore, setLoadingMore] = useState(false);

  useEffect(() => {
    let on = true;
    setLoadingMore(true);
    get('/activities', { page, page_size: PAGE_SIZE })
      .then((d) => {
        if (!on) return;
        setItems((prev) => (page === 1 ? d.items || [] : [...(prev || []), ...(d.items || [])]));
        setTotal(d.total || 0);
      })
      .catch((e) => {
        toast.error(e.message);
        if (on) setItems((prev) => prev || []);
      })
      .finally(() => {
        if (on) setLoadingMore(false);
      });
    return () => {
      on = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [page]);

  const groups = useMemo(() => {
    if (!items) return [];
    const map = new Map();
    items.forEach((a) => {
      const d = new Date(a.created_at);
      const key = d.toDateString();
      if (!map.has(key)) map.set(key, { key, label: dayLabel(d), items: [] });
      map.get(key).items.push(a);
    });
    return [...map.values()];
  }, [items]);

  return (
    <div className="page page-narrow">
      <div className="page-head">
        <h1>Feed</h1>
      </div>
      {items === null ? (
        <Loading label="Loading activity…" />
      ) : items.length === 0 ? (
        <Empty label="No activity yet." hint="Create a person, lead, or company to get things moving." />
      ) : (
        <>
          {groups.map((g) => (
            <div key={g.key} className="feed-group">
              <div className="feed-day">{g.label}</div>
              <div className="card feed-card">
                {g.items.map((a) => {
                  const link = entityPath(a.entity_type, a.entity_id);
                  const label = a.payload?.name || a.payload?.display_name || humanize(a.entity_type);
                  return (
                    <div key={a.id} className="feed-item">
                      <span className="feed-dot" />
                      <div className="feed-body">
                        <span>
                          <strong>{a.actor_name || 'System'}</strong>{' '}
                          <span className="muted">{humanize(a.kind).toLowerCase()}</span>
                          {link && (
                            <>
                              {' — '}
                              <Link to={link}>{label}</Link>
                            </>
                          )}
                        </span>
                      </div>
                      <span className="muted feed-time">{timeOfDay(a.created_at)}</span>
                    </div>
                  );
                })}
              </div>
            </div>
          ))}
          {items.length < total && (
            <div className="feed-more">
              <button className="btn" disabled={loadingMore} onClick={() => setPage((p) => p + 1)}>
                {loadingMore ? 'Loading…' : 'Load more'}
              </button>
            </div>
          )}
        </>
      )}
    </div>
  );
}
