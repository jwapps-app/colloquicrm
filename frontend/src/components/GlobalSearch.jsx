import { useEffect, useRef, useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { get } from '../api';
import { fullName } from '../format';
import Icon from './Icon';

const SECTIONS = [
  { api: '/people', route: '/people', label: 'People', name: (r) => fullName(r), sub: (r) => r.company_name },
  { api: '/companies', route: '/companies', label: 'Companies', name: (r) => r.name, sub: (r) => r.email_domain },
  { api: '/leads', route: '/leads', label: 'Leads', name: (r) => fullName(r), sub: (r) => r.company_name },
  { api: '/opportunities', route: '/opportunities', label: 'Opportunities', name: (r) => r.name, sub: (r) => r.company_name },
];

export default function GlobalSearch() {
  const nav = useNavigate();
  const [q, setQ] = useState('');
  const [results, setResults] = useState(null);
  const [open, setOpen] = useState(false);
  const [loading, setLoading] = useState(false);
  const boxRef = useRef(null);

  useEffect(() => {
    const term = q.trim();
    if (term.length < 2) {
      setResults(null);
      setLoading(false);
      return;
    }
    setLoading(true);
    const t = setTimeout(async () => {
      try {
        const res = await Promise.all(
          SECTIONS.map((s) => get(s.api, { q: term, page: 1, page_size: 5 }).catch(() => ({ items: [] })))
        );
        setResults(
          SECTIONS.map((s, i) => ({ ...s, items: res[i]?.items || [] })).filter((s) => s.items.length > 0)
        );
      } finally {
        setLoading(false);
      }
    }, 300);
    return () => clearTimeout(t);
  }, [q]);

  useEffect(() => {
    function onDoc(e) {
      if (boxRef.current && !boxRef.current.contains(e.target)) setOpen(false);
    }
    document.addEventListener('mousedown', onDoc);
    return () => document.removeEventListener('mousedown', onDoc);
  }, []);

  function go(section, item) {
    setOpen(false);
    setQ('');
    setResults(null);
    nav(`${section.route}/${item.id}`);
  }

  return (
    <div className="global-search" ref={boxRef}>
      <Icon name="search" size={16} />
      <input
        type="search"
        placeholder="Search people, companies, leads, opportunities…"
        value={q}
        onChange={(e) => {
          setQ(e.target.value);
          setOpen(true);
        }}
        onFocus={() => setOpen(true)}
      />
      {open && q.trim().length >= 2 && (
        <div className="search-results">
          {loading && <div className="search-hint muted">Searching…</div>}
          {!loading && (!results || results.length === 0) && <div className="search-hint muted">No matches.</div>}
          {!loading &&
            results &&
            results.map((s) => (
              <div key={s.label} className="search-group">
                <div className="search-group-label">{s.label}</div>
                {s.items.map((item) => (
                  <button key={item.id} className="search-item" onClick={() => go(s, item)}>
                    <span>{s.name(item) || '(unnamed)'}</span>
                    {s.sub(item) && <span className="muted">{s.sub(item)}</span>}
                  </button>
                ))}
              </div>
            ))}
        </div>
      )}
    </div>
  );
}
