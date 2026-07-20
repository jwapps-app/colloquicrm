import { useEffect, useMemo, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { get } from '../api';
import { EmailBody, useEmailBodies } from '../components/EmailBody';
import { useAuth } from '../auth';
import { useToast } from '../components/Toast';
import { Empty, Loading } from '../components/ui';
import { entityPath, humanize, timeOfDay, parseWhen } from '../format';

const PAGE_SIZE = 40;
const TABS = [
  { id: 'all', label: 'All' },
  { id: 'email', label: 'Emails' },
  { id: 'phone', label: 'Calls & Texts' },
  { id: 'note', label: 'Notes' },
  { id: 'activity', label: 'Activity' },
];

function dayLabel(d) {
  const today = new Date();
  const yesterday = new Date(Date.now() - 86400000);
  if (d.toDateString() === today.toDateString()) return 'Today';
  if (d.toDateString() === yesterday.toDateString()) return 'Yesterday';
  return d.toLocaleDateString([], { weekday: 'long', month: 'long', day: 'numeric', year: 'numeric' });
}

function RelatedChips({ related }) {
  if (!related?.length) return null;
  return (
    <span className="feed-related">
      {related.map((r) => {
        const link = entityPath(r.entity_type, r.entity_id);
        return link ? (
          <Link key={`${r.entity_type}-${r.entity_id}`} className="chip chip-filter" to={link}>
            {r.label}
          </Link>
        ) : (
          <span key={`${r.entity_type}-${r.entity_id}`} className="chip">{r.label}</span>
        );
      })}
    </span>
  );
}

export default function Feed() {
  const toast = useToast();
  const { user } = useAuth();
  const [tab, setTab] = useState('all');
  const [items, setItems] = useState(null);
  const [hasMore, setHasMore] = useState(false);
  const [page, setPage] = useState(1);
  const [loadingMore, setLoadingMore] = useState(false);
  const { open: openEmail, toggle: toggleEmail, bodies, close: closeEmail } = useEmailBodies();
  // Set when a failed load-more rolls the page back: that page's data is
  // already loaded, so the effect run the rollback triggers must not append
  // it again. Clicking "Load more" then re-requests the failed page.
  const skipNextFetch = useRef(false);

  useEffect(() => {
    if (skipNextFetch.current) {
      skipNextFetch.current = false;
      return;
    }
    let on = true;
    setLoadingMore(true);
    get('/feed', { page, page_size: PAGE_SIZE, kind: tab })
      .then((d) => {
        if (!on) return;
        setItems((prev) => (page === 1 ? d.items || [] : [...(prev || []), ...(d.items || [])]));
        setHasMore(!!d.has_more);
      })
      .catch((e) => {
        toast.error(e.message);
        if (!on) return;
        setItems((prev) => prev || []);
        if (page > 1) {
          skipNextFetch.current = true;
          setPage((p) => p - 1);
        }
      })
      .finally(() => {
        if (on) setLoadingMore(false);
      });
    return () => {
      on = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [page, tab]);

  function switchTab(id) {
    if (id === tab) return;
    setTab(id);
    setItems(null);
    setPage(1);
    closeEmail();
  }

  const groups = useMemo(() => {
    if (!items) return [];
    const map = new Map();
    items.forEach((it) => {
      const d = parseWhen(it.at);
      const key = d.toDateString();
      if (!map.has(key)) map.set(key, { key, label: dayLabel(d), items: [] });
      map.get(key).items.push(it);
    });
    return [...map.values()];
  }, [items]);

  return (
    <div className="page page-narrow">
      <div className="page-head">
        <h1>Feed</h1>
      </div>
      <div className="tabs settings-tabs">
        {TABS.map((t) => (
          <button key={t.id} className={'tab' + (tab === t.id ? ' active' : '')} onClick={() => switchTab(t.id)}>
            {t.label}
          </button>
        ))}
      </div>
      {items === null ? (
        <Loading label="Loading the feed…" />
      ) : items.length === 0 ? (
        <Empty label="Nothing here yet." hint="Emails, notes, and record activity will show up here." />
      ) : (
        <>
          {groups.map((g) => (
            <div key={g.key} className="feed-group">
              <div className="feed-day">{g.label}</div>
              <div className="card feed-card">
                {g.items.map((it) => {
                  if (it.type === 'call' || it.type === 'sms') {
                    const isCall = it.type === 'call';
                    return (
                      <div key={`p-${it.id}`} className="feed-item feed-phone">
                        <span className="phone-icon">{isCall ? '☎' : '💬'}</span>
                        <div className="feed-body">
                          <span>
                            <strong>
                              {isCall
                                ? (it.direction === 'outbound' ? 'Outgoing call' : 'Incoming call')
                                : (it.direction === 'outbound' ? 'Text sent' : 'Text received')}
                              {isCall && it.result && it.result !== 'Call connected' && it.result !== 'Accepted' ? ` — ${it.result}` : ''}
                            </strong>
                            <RelatedChips related={it.related} />
                          </span>
                          <div className="muted">
                            {isCall
                              ? `${it.duration_seconds ? `${Math.floor(it.duration_seconds / 60)}m ${it.duration_seconds % 60}s · ` : ''}${it.other_number}${it.recording_id ? ' · recorded' : ''}`
                              : null}
                          </div>
                          {!isCall && it.text && <div className="sms-body">{it.text}</div>}
                        </div>
                        <span className="muted feed-time">{timeOfDay(it.at)}</span>
                      </div>
                    );
                  }
                  if (it.type === 'email') {
                    const b = bodies[it.id];
                    const open = openEmail === it.id;
                    return (
                      <div key={`e-${it.id}`} className={'feed-item feed-email' + (open ? ' open' : '')}>
                        <span className="email-dir">{it.is_outgoing ? '↗' : '↘'}</span>
                        <div className="feed-body">
                          <div
                            className="email-toggle"
                            onClick={() => toggleEmail(it.id)}
                            role="button"
                            tabIndex={0}
                            onKeyDown={(e) => {
                              if (e.key === 'Enter' || e.key === ' ') {
                                e.preventDefault();
                                toggleEmail(it.id);
                              }
                            }}
                          >
                            <strong>
                              {it.is_outgoing ? 'Email sent' : `Email from ${it.from_name || it.from_email || 'unknown'}`}
                            </strong>
                            {' — '}
                            <span className="feed-subject">{it.subject || '(no subject)'}</span>
                            <RelatedChips related={it.related} />
                          </div>
                          {!open && it.snippet && <div className="muted email-snippet">{it.snippet}</div>}
                          {open && (
                            <EmailBody body={b}>
                              {user?.id === it.owner_user_id && it.gmail_id && (
                                <a
                                  className="muted email-open"
                                  href={`https://mail.google.com/mail/u/0/#all/${it.gmail_id}`}
                                  target="_blank"
                                  rel="noreferrer"
                                >
                                  Open in Gmail ↗
                                </a>
                              )}
                            </EmailBody>
                          )}
                        </div>
                        <span className="muted feed-time">{timeOfDay(it.at)}</span>
                      </div>
                    );
                  }
                  if (it.type === 'note') {
                    return (
                      <div key={`n-${it.id}`} className="feed-item">
                        <span className="feed-dot note-dot" />
                        <div className="feed-body">
                          <span>
                            <strong>{it.author_name || 'Someone'}</strong>{' '}
                            <span className="muted">added a note</span>
                            <RelatedChips related={it.related} />
                          </span>
                          <div className="feed-note-body">{it.body}</div>
                        </div>
                        <span className="muted feed-time">{timeOfDay(it.at)}</span>
                      </div>
                    );
                  }
                  return (
                    <div key={`a-${it.id}`} className="feed-item">
                      <span className="feed-dot" />
                      <div className="feed-body">
                        <span>
                          <strong>{it.actor_name || 'System'}</strong>{' '}
                          <span className="muted">{humanize(it.kind).toLowerCase()}</span>
                          <RelatedChips related={it.related} />
                        </span>
                      </div>
                      <span className="muted feed-time">{timeOfDay(it.at)}</span>
                    </div>
                  );
                })}
              </div>
            </div>
          ))}
          {hasMore && (
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
