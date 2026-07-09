import { useEffect, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { get } from '../api';
import { EmailBody, useEmailBodies } from '../components/EmailBody';
import { useToast } from '../components/Toast';
import { Empty, Loading } from '../components/ui';
import { entityPath, fmtDate } from '../format';

const PAGE_SIZE = 25;

export default function EmailSearch() {
  const toast = useToast();
  const [input, setInput] = useState('');
  const [q, setQ] = useState('');
  const [items, setItems] = useState(null);
  const [page, setPage] = useState(1);
  const [hasMore, setHasMore] = useState(false);
  const [loading, setLoading] = useState(false);
  const { open, toggle, bodies } = useEmailBodies();
  const boxRef = useRef(null);

  useEffect(() => {
    boxRef.current?.focus();
  }, []);

  // Debounce the query.
  useEffect(() => {
    const t = setTimeout(() => {
      setQ(input.trim());
      setPage(1);
    }, 300);
    return () => clearTimeout(t);
  }, [input]);

  useEffect(() => {
    if (q.length < 2) {
      setItems(null);
      setHasMore(false);
      return;
    }
    let on = true;
    setLoading(true);
    get('/emails/search', { q, page, page_size: PAGE_SIZE })
      .then((d) => {
        if (!on) return;
        setItems((prev) => (page === 1 ? d.items || [] : [...(prev || []), ...(d.items || [])]));
        setHasMore(!!d.has_more);
      })
      .catch((e) => {
        toast.error(e.message);
        if (on) setItems((prev) => prev || []);
      })
      .finally(() => on && setLoading(false));
    return () => {
      on = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [q, page]);

  return (
    <div className="page">
      <div className="page-head">
        <h1>Email search</h1>
      </div>
      <div className="list-toolbar">
        <input
          ref={boxRef}
          type="search"
          className="search-input"
          placeholder="Search all email — subject, sender, or body…"
          value={input}
          onChange={(e) => setInput(e.target.value)}
        />
      </div>

      {q.length < 2 ? (
        <div className="card">
          <Empty label="Search your synced email" hint="Type at least two characters to search subjects, senders, and message bodies." />
        </div>
      ) : loading && !items ? (
        <div className="card">
          <Loading label="Searching…" />
        </div>
      ) : items && items.length === 0 ? (
        <div className="card">
          <Empty label={`No emails match “${q}”.`} />
        </div>
      ) : (
        <div className="card email-results">
          {(items || []).map((m) => {
            const b = bodies[m.id];
            const from = m.is_outgoing ? 'You' : m.from_name || m.from_email || 'Unknown';
            return (
              <div key={m.id} className={'email-result' + (open === m.id ? ' open' : '')}>
                <button className="email-result-head" onClick={() => toggle(m.id)}>
                  <div className="email-result-main">
                    <span className="email-subject">{m.subject || '(no subject)'}</span>
                    <span className="email-snippet">{m.snippet}</span>
                  </div>
                  <div className="email-result-meta">
                    <span className={'email-dir' + (m.is_outgoing ? ' out' : '')}>
                      {m.is_outgoing ? '↑ sent' : '↓ received'}
                    </span>
                    <span className="muted">{from}</span>
                    <span className="muted">{fmtDate(m.sent_at)}</span>
                  </div>
                </button>
                {(m.related || []).length > 0 && (
                  <div className="email-related">
                    {m.related.map((r) => {
                      const link = entityPath(r.entity_type, r.entity_id);
                      return link ? (
                        <Link key={r.entity_id} className="chip chip-filter" to={link}>
                          {r.label}
                        </Link>
                      ) : (
                        <span key={r.entity_id} className="chip">{r.label}</span>
                      );
                    })}
                  </div>
                )}
                {open === m.id && <EmailBody body={b} />}
              </div>
            );
          })}
          {hasMore && (
            <div className="table-foot">
              <button className="btn btn-small" disabled={loading} onClick={() => setPage((p) => p + 1)}>
                {loading ? 'Loading…' : 'Load more'}
              </button>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
