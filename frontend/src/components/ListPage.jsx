import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { del, download, get, post } from '../api';
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
  const [selected, setSelected] = useState(new Set());
  const [allMatching, setAllMatching] = useState(false);
  const [bulkBusy, setBulkBusy] = useState(false);
  const [exporting, setExporting] = useState(false);
  const [refresh, setRefresh] = useState(0);
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
  }, [apiPath, q, page, sort, order, filters, refresh]);

  // A different result set makes the old selection meaningless.
  useEffect(() => {
    setSelected(new Set());
    setAllMatching(false);
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

  const listParams = { q: q || undefined, sort, order, ...filters };

  async function exportCsv() {
    setExporting(true);
    try {
      await download(`${apiPath}/export`, listParams);
    } catch (e) {
      toast.error(e.message);
    }
    setExporting(false);
  }

  function toggleRow(e, id) {
    e.stopPropagation();
    setAllMatching(false);
    setSelected((s) => {
      const next = new Set(s);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  function togglePage(e) {
    setAllMatching(false);
    const ids = (data?.items || []).map((r) => r.id);
    setSelected(e.target.checked ? new Set(ids) : new Set());
  }

  const selectedCount = allMatching ? data?.total || 0 : selected.size;

  async function runBulk(body) {
    setBulkBusy(true);
    try {
      const payload = allMatching
        ? { ...body, select_all: true }
        : { ...body, ids: [...selected] };
      const res = await post(`${apiPath}/bulk`, payload, allMatching ? listParams : undefined);
      toast.success(`${res.affected} record${res.affected === 1 ? '' : 's'} ${body.action === 'delete' ? 'deleted' : 'updated'}`);
      setSelected(new Set());
      setAllMatching(false);
      setPage(1);
      setRefresh((r) => r + 1);
    } catch (e) {
      toast.error(e.message);
    }
    setBulkBusy(false);
  }

  function bulkDelete() {
    if (!window.confirm(`Delete ${selectedCount} record${selectedCount === 1 ? '' : 's'}? This can't be undone.`)) return;
    runBulk({ action: 'delete' });
  }

  function bulkTag() {
    const name = window.prompt('Tag to add to the selected records:');
    if (!name || !name.trim()) return;
    runBulk({ action: 'add_tags', tags: [name.trim()] });
  }

  function bulkOwner(ownerId) {
    if (!ownerId) return;
    runBulk({ action: 'set_owner', owner_id: ownerId });
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
          <button className="btn" onClick={exportCsv} disabled={exporting || total === 0}>
            {exporting ? 'Exporting…' : 'Export CSV'}
          </button>
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

      {selectedCount > 0 && (
        <div className="bulk-bar">
          <span className="bulk-count">
            {allMatching ? `All ${selectedCount} matching selected` : `${selectedCount} selected`}
          </span>
          {!allMatching && selected.size === items.length && total > items.length && (
            <button className="linklike" onClick={() => setAllMatching(true)}>
              Select all {total} matching
            </button>
          )}
          <span className="bulk-actions">
            <button className="btn btn-small" onClick={bulkTag} disabled={bulkBusy}>
              Add tag…
            </button>
            <select
              className="bulk-owner"
              value=""
              disabled={bulkBusy}
              onChange={(e) => bulkOwner(e.target.value)}
            >
              <option value="">Change owner…</option>
              {users.map((u) => (
                <option key={u.id} value={u.id}>
                  {u.display_name || u.email}
                </option>
              ))}
            </select>
            <button className="btn btn-small btn-danger-ghost" onClick={bulkDelete} disabled={bulkBusy}>
              {bulkBusy ? 'Working…' : 'Delete'}
            </button>
            <button
              className="btn btn-small btn-ghost"
              onClick={() => {
                setSelected(new Set());
                setAllMatching(false);
              }}
            >
              Clear
            </button>
          </span>
        </div>
      )}

      <div className="card table-card">
        {loading && !data ? (
          <Loading />
        ) : (
          <>
            <div className="table-wrap">
              <table className="table">
                <thead>
                  <tr>
                    <th className="no-sort col-check">
                      <input
                        type="checkbox"
                        checked={items.length > 0 && (allMatching || selected.size === items.length)}
                        onChange={togglePage}
                        aria-label="Select all on this page"
                      />
                    </th>
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
                      <td className="col-check" onClick={(e) => e.stopPropagation()}>
                        <input
                          type="checkbox"
                          checked={allMatching || selected.has(row.id)}
                          onChange={(e) => toggleRow(e, row.id)}
                          aria-label="Select row"
                        />
                      </td>
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
