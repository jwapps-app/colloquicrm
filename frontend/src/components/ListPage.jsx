import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { del, get, post } from '../api';
import { useToast } from './Toast';
import FormModal from './FormModal';
import { Empty, Loading } from './ui';

/**
 * Generic entity list page: debounced search, sortable columns, filter bar,
 * saved-filter chips, pagination, and an "Add" modal.
 *
 * columns: [{ key, label, sortKey?, sortable?, render?(row) }]
 * filterDefs: [{ key, label, options }] or [{ type: 'tag' }] or [{ type: 'owner' }]
 */
export default function ListPage({
  title,
  entityType,
  apiPath,
  route,
  columns,
  filterDefs = [],
  createFields,
  createTitle,
  transformCreate,
  defaultSort = 'created_at',
  defaultOrder = 'desc',
  headerExtra,
}) {
  const nav = useNavigate();
  const toast = useToast();

  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [qInput, setQInput] = useState('');
  const [q, setQ] = useState('');
  const [page, setPage] = useState(1);
  const [sort, setSort] = useState(defaultSort);
  const [order, setOrder] = useState(defaultOrder);
  const [filters, setFilters] = useState({});
  const [tags, setTags] = useState([]);
  const [users, setUsers] = useState([]);
  const [saved, setSaved] = useState([]);
  const [showCreate, setShowCreate] = useState(false);
  const pageSize = 25;

  // Debounced search.
  useEffect(() => {
    const t = setTimeout(() => {
      setQ(qInput.trim());
      setPage(1);
    }, 300);
    return () => clearTimeout(t);
  }, [qInput]);

  // Main fetch.
  useEffect(() => {
    let on = true;
    setLoading(true);
    get(apiPath, { q: q || undefined, page, page_size: pageSize, sort, order, ...filters })
      .then((d) => {
        if (on) setData(d);
      })
      .catch((e) => {
        toast.error(e.message);
        if (on) setData({ items: [], total: 0 });
      })
      .finally(() => {
        if (on) setLoading(false);
      });
    return () => {
      on = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [apiPath, q, page, sort, order, filters]);

  // Filter dropdown sources + saved filters.
  useEffect(() => {
    get('/tags')
      .then((d) => setTags(Array.isArray(d) ? d : []))
      .catch(() => {});
    get('/users')
      .then((d) => setUsers(d?.items || []))
      .catch(() => {});
    get('/saved-filters', { entity_type: entityType })
      .then((d) => setSaved(Array.isArray(d) ? d : d?.items || []))
      .catch(() => {});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [entityType]);

  const defs = filterDefs.map((d) => {
    if (d.type === 'tag')
      return { key: 'tag', label: 'Tag', options: tags.map((t) => ({ value: t.name, label: `${t.name} (${t.count})` })) };
    if (d.type === 'owner')
      return {
        key: d.key || 'owner_id',
        label: d.label || 'Owner',
        options: users.map((u) => ({ value: u.id, label: u.display_name || u.email })),
      };
    return d;
  });

  function setFilter(key, value) {
    setPage(1);
    setFilters((f) => {
      const next = { ...f };
      if (value) next[key] = value;
      else delete next[key];
      return next;
    });
  }

  function onSort(col) {
    if (col.sortable === false) return;
    const key = col.sortKey || col.key;
    if (!key) return;
    if (sort === key) setOrder((o) => (o === 'asc' ? 'desc' : 'asc'));
    else {
      setSort(key);
      setOrder('asc');
    }
    setPage(1);
  }

  function applySaved(sf) {
    const f = { ...(sf.filters || {}) };
    const savedQ = f.q || '';
    delete f.q;
    setQInput(savedQ);
    setQ(savedQ);
    setFilters(f);
    setPage(1);
  }

  async function saveCurrent() {
    const name = window.prompt('Name for this filter:');
    if (!name) return;
    try {
      const created = await post('/saved-filters', {
        entity_type: entityType,
        name,
        filters: { ...(q ? { q } : {}), ...filters },
        is_public: false,
      });
      setSaved((s) => [...s, created]);
      toast.success('Filter saved');
    } catch (e) {
      toast.error(e.message);
    }
  }

  async function deleteSaved(e, sf) {
    e.stopPropagation();
    if (!window.confirm(`Delete saved filter "${sf.name}"?`)) return;
    try {
      await del(`/saved-filters/${sf.id}`);
      setSaved((s) => s.filter((x) => x.id !== sf.id));
    } catch (err) {
      toast.error(err.message);
    }
  }

  async function handleCreate(values) {
    const body = {};
    createFields.forEach((f) => {
      let v = values[f.key];
      if (v === '' || v === undefined) return;
      if (f.type === 'number') v = Number(v);
      body[f.key] = v;
    });
    const payload = transformCreate ? transformCreate(body) : body;
    const created = await post(apiPath, payload);
    setShowCreate(false);
    toast.success('Created');
    if (created?.id) nav(`${route}/${created.id}`);
  }

  const items = data?.items || [];
  const total = data?.total || 0;
  const from = total === 0 ? 0 : (page - 1) * pageSize + 1;
  const to = Math.min(page * pageSize, total);
  const hasFilters = Object.keys(filters).length > 0 || q;

  return (
    <div className="page">
      <div className="page-head">
        <h1>{title}</h1>
        <div className="page-head-actions">
          {headerExtra}
          {createFields && (
            <button className="btn btn-primary" onClick={() => setShowCreate(true)}>
              + Add
            </button>
          )}
        </div>
      </div>

      <div className="list-toolbar">
        <input
          type="search"
          className="search-input"
          placeholder={`Search ${title.toLowerCase()}…`}
          value={qInput}
          onChange={(e) => setQInput(e.target.value)}
        />
        {defs.map((d) => (
          <select key={d.key} value={filters[d.key] || ''} onChange={(e) => setFilter(d.key, e.target.value)}>
            <option value="">{d.label}: all</option>
            {(d.options || []).map((o) => (
              <option key={o.value} value={o.value}>
                {o.label}
              </option>
            ))}
          </select>
        ))}
        {hasFilters && (
          <button
            className="btn btn-ghost"
            onClick={() => {
              setFilters({});
              setQInput('');
              setQ('');
              setPage(1);
            }}
          >
            Clear
          </button>
        )}
      </div>

      <div className="saved-filters">
        {saved.map((sf) => (
          <button key={sf.id} className="chip chip-filter" onClick={() => applySaved(sf)} title="Apply this filter">
            {sf.name}
            <span className="chip-x" onClick={(e) => deleteSaved(e, sf)} role="button" aria-label="Delete filter">
              ×
            </span>
          </button>
        ))}
        {hasFilters && (
          <button className="btn btn-ghost btn-small" onClick={saveCurrent}>
            + Save current filter
          </button>
        )}
      </div>

      <div className="card table-card">
        {loading && !data ? (
          <Loading />
        ) : (
          <>
            <div className="table-wrap">
              <table className="table">
                <thead>
                  <tr>
                    {columns.map((c) => {
                      const key = c.sortKey || c.key;
                      const active = sort === key && c.sortable !== false;
                      return (
                        <th key={c.key} onClick={() => onSort(c)} className={c.sortable === false ? 'no-sort' : ''}>
                          {c.label}
                          {active && <span className="sort-arrow">{order === 'asc' ? ' ▲' : ' ▼'}</span>}
                        </th>
                      );
                    })}
                  </tr>
                </thead>
                <tbody>
                  {items.map((row) => (
                    <tr key={row.id} onClick={() => nav(`${route}/${row.id}`)}>
                      {columns.map((c) => (
                        <td key={c.key}>{c.render ? c.render(row) : row[c.key] ?? '—'}</td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            {items.length === 0 && !loading && (
              <Empty
                label={hasFilters ? 'No results match your search.' : `No ${title.toLowerCase()} yet.`}
                hint={hasFilters ? 'Try clearing filters.' : createFields ? 'Use “+ Add” to create one.' : undefined}
              />
            )}
            <div className="table-foot">
              <span className="muted">
                {from}–{to} of {total}
              </span>
              <div className="pager">
                <button className="btn btn-small" disabled={page <= 1} onClick={() => setPage((p) => p - 1)}>
                  ‹ Prev
                </button>
                <button className="btn btn-small" disabled={to >= total} onClick={() => setPage((p) => p + 1)}>
                  Next ›
                </button>
              </div>
            </div>
          </>
        )}
      </div>

      {showCreate && (
        <FormModal
          title={createTitle || `Add ${title.replace(/ies$/, 'y').replace(/s$/, '').toLowerCase()}`}
          fields={createFields}
          submitLabel="Create"
          onSubmit={handleCreate}
          onClose={() => setShowCreate(false)}
        />
      )}
    </div>
  );
}
