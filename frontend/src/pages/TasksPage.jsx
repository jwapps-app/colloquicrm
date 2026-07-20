import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { del, get, post } from '../api';
import { useToast } from '../components/Toast';
import { Empty, Loading } from '../components/ui';
import { entityPath, fmtDate, humanize, parseWhen } from '../format';
import { PRIORITIES } from '../constants/options';

const PAGE_SIZE = 50;

export default function TasksPage() {
  const toast = useToast();
  const [tab, setTab] = useState('open');
  const [data, setData] = useState(null);
  const [page, setPage] = useState(1);
  const [version, setVersion] = useState(0);
  const [name, setName] = useState('');
  const [due, setDue] = useState('');
  const [priority, setPriority] = useState('');
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let on = true;
    setData(null);
    // Open tasks read best soonest-due first; Done reads best most-recently
    // finished first.
    const sort = tab === 'done'
      ? { sort: 'completed_at', order: 'desc' }
      : { sort: 'due_at', order: 'asc' };
    get('/tasks', { status: tab, page, page_size: PAGE_SIZE, ...sort })
      .then((d) => {
        if (!on) return;
        // Completing/deleting the last task on a later page leaves it empty —
        // step back so the user isn't stranded on a blank page.
        if ((d?.items || []).length === 0 && (d?.total || 0) > 0 && page > 1) {
          setPage((p) => p - 1);
          return;
        }
        setData(d);
      })
      .catch((e) => {
        toast.error(e.message);
        if (on) setData({ items: [], total: 0 });
      });
    return () => {
      on = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tab, page, version]);

  async function add(e) {
    e.preventDefault();
    const n = name.trim();
    if (!n) return;
    setBusy(true);
    try {
      const body = { name: n };
      if (due) {
        // 9 AM local on the chosen day, sent as a real instant — a naive
        // wall-time string would be stored as UTC and fire hours early.
        const [y, m, d] = due.split('-').map(Number);
        body.due_at = new Date(y, m - 1, d, 9).toISOString();
      }
      if (priority) body.priority = priority;
      await post('/tasks', body);
      setName('');
      setDue('');
      setPriority('');
      toast.success('Task added');
      if (tab === 'open' && page === 1) setVersion((v) => v + 1);
      else {
        setTab('open');
        setPage(1);
      }
    } catch (err) {
      toast.error(err.message);
    }
    setBusy(false);
  }

  async function toggle(t) {
    try {
      await post(`/tasks/${t.id}/${t.status === 'open' ? 'complete' : 'reopen'}`);
      setVersion((v) => v + 1);
    } catch (e) {
      toast.error(e.message);
    }
  }

  async function remove(t) {
    if (!window.confirm(`Delete task "${t.name}"?`)) return;
    try {
      await del(`/tasks/${t.id}`);
      setVersion((v) => v + 1);
    } catch (e) {
      toast.error(e.message);
    }
  }

  const items = data?.items || [];
  const total = data?.total || 0;
  const from = total === 0 ? 0 : (page - 1) * PAGE_SIZE + 1;
  const to = Math.min(page * PAGE_SIZE, total);
  const now = new Date();

  return (
    <div className="page page-narrow">
      <div className="page-head">
        <h1>Tasks</h1>
      </div>

      <form className="card task-add" onSubmit={add}>
        <input className="task-add-name" placeholder="Add a task…" value={name} onChange={(e) => setName(e.target.value)} />
        <input type="date" value={due} onChange={(e) => setDue(e.target.value)} title="Due date" />
        <select value={priority} onChange={(e) => setPriority(e.target.value)} title="Priority">
          <option value="">Priority</option>
          {PRIORITIES.map((p) => (
            <option key={p.value} value={p.value}>
              {p.label}
            </option>
          ))}
        </select>
        <button className="btn btn-primary" type="submit" disabled={busy || !name.trim()}>
          Add
        </button>
      </form>

      <div className="tabs">
        {['open', 'done'].map((t) => (
          <button
            key={t}
            className={'tab' + (tab === t ? ' active' : '')}
            onClick={() => {
              setTab(t);
              setPage(1);
            }}
          >
            {humanize(t)}
          </button>
        ))}
      </div>

      <div className="card task-list-card">
        {data === null ? (
          <Loading label="Loading tasks…" />
        ) : items.length === 0 ? (
          <Empty label={tab === 'open' ? 'No open tasks. Nice.' : 'No completed tasks yet.'} />
        ) : (
          <div className="task-list">
            {items.map((t) => {
              const overdue = t.status === 'open' && t.due_at && parseWhen(t.due_at) < now;
              const link = entityPath(t.entity_type, t.entity_id);
              return (
                <div key={t.id} className={'task-row' + (t.status === 'done' ? ' done' : '')}>
                  <input
                    type="checkbox"
                    checked={t.status === 'done'}
                    onChange={() => toggle(t)}
                    title={t.status === 'open' ? 'Mark done' : 'Reopen'}
                  />
                  <div className="task-main">
                    <span className="task-name">{t.name}</span>
                    {t.details && <span className="muted task-details">{t.details}</span>}
                  </div>
                  {t.priority && t.priority !== 'none' && (
                    <span className={`badge priority-${t.priority}`}>{humanize(t.priority)}</span>
                  )}
                  {t.due_at && (
                    <span className={'task-due' + (overdue ? ' overdue' : '')}>
                      {overdue ? 'Overdue · ' : 'Due '}
                      {fmtDate(t.due_at)}
                    </span>
                  )}
                  {link && (
                    <span className="task-entity muted">
                      Related to{' '}
                      <Link to={link}>{t.entity_label || humanize(t.entity_type)}</Link>
                    </span>
                  )}
                  {t.assignee_name && <span className="muted task-assignee">{t.assignee_name}</span>}
                  <button className="icon-btn tiny" onClick={() => remove(t)} title="Delete task" aria-label="Delete task">
                    ×
                  </button>
                </div>
              );
            })}
          </div>
        )}
        {data && total > 0 && (
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
        )}
      </div>
    </div>
  );
}
