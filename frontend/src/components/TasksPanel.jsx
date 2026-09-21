import { useEffect, useRef, useState } from 'react';
import { get, post } from '../api';
import { useAuth } from '../auth';
import { useToast } from './Toast';
import { fmtDateTime, parseWhen, repeatLabel } from '../format';
import { REPEAT_OPTIONS } from '../constants/options';
import { Loading } from './ui';

const HOURS = Array.from({ length: 11 }, (_, i) => i + 8); // 8 AM – 6 PM
const MINUTES = [0, 15, 30, 45];
const PAGE = 50; // open tasks fetched per request

// Default task time: an hour from now rounded up to the quarter hour,
// clamped into the 8 AM–6 PM window (else tomorrow morning).
function suggestWhen() {
  const d = new Date(Date.now() + 3600000);
  d.setMinutes(Math.ceil(d.getMinutes() / 15) * 15, 0, 0);
  if (d.getHours() < 8) {
    d.setHours(8, 0, 0, 0);
  } else if (d.getHours() > 18) {
    d.setDate(d.getDate() + 1);
    d.setHours(8, 0, 0, 0);
  }
  return d;
}

function pad(n) {
  return String(n).padStart(2, '0');
}

function hourLabel(h) {
  if (h === 12) return '12 PM';
  return h < 12 ? `${h} AM` : `${h - 12} PM`;
}

export default function TasksPanel({ entityType, entityId }) {
  const toast = useToast();
  const { user } = useAuth();
  const [tasks, setTasks] = useState(null);
  // The server's count of open tasks here — the panel loads PAGE at a time
  // and says so when there are more, rather than passing a slice off as all.
  const [total, setTotal] = useState(0);
  const [pages, setPages] = useState(1);
  const [name, setName] = useState('');
  const [picking, setPicking] = useState(false);
  const [whenDate, setWhenDate] = useState('');
  const [whenHour, setWhenHour] = useState(9);
  const [whenMin, setWhenMin] = useState(0);
  const [repeat, setRepeat] = useState('');
  const [busy, setBusy] = useState(false);

  const loadEpoch = useRef(0);

  async function load(wantPages = pages) {
    const epoch = ++loadEpoch.current;
    try {
      const items = [];
      let count = 0;
      for (let page = 1; page <= wantPages; page += 1) {
        const d = await get('/tasks', {
          entity_type: entityType,
          entity_id: entityId,
          status: 'open',
          page,
          page_size: PAGE,
        });
        if (epoch !== loadEpoch.current) return; // superseded by a newer load
        items.push(...(d?.items || []));
        count = Number(d?.total) || 0;
        if (items.length >= count || !(d?.items || []).length) break;
      }
      setTasks(items);
      setTotal(Math.max(count, items.length));
    } catch (e) {
      if (epoch !== loadEpoch.current) return;
      toast.error(e.message);
      setTasks([]);
    }
  }

  useEffect(() => {
    setTasks(null);
    setTotal(0);
    setPages(1);
    load(1);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [entityType, entityId]);

  function openPicker(e) {
    e.preventDefault();
    if (!name.trim()) return;
    const s = suggestWhen();
    setWhenDate(`${s.getFullYear()}-${pad(s.getMonth() + 1)}-${pad(s.getDate())}`);
    setWhenHour(s.getHours());
    setWhenMin(s.getMinutes());
    setPicking(true);
  }

  async function addTask() {
    const n = name.trim();
    if (!n || !whenDate) return;
    const [y, m, d] = whenDate.split('-').map(Number);
    const due = new Date(y, m - 1, d, whenHour, whenMin, 0, 0);
    setBusy(true);
    try {
      const body = {
        name: n,
        entity_type: entityType,
        entity_id: entityId,
        due_at: due.toISOString(),
        assignee_id: user?.id,
      };
      if (repeat) {
        // A due date is always set here (the composer requires one), so a
        // repeat can't be created without the date it schedules from.
        const [every, unit] = repeat.split(':');
        body.repeat_every = Number(every);
        body.repeat_unit = unit;
      }
      await post('/tasks', body);
      setName('');
      setPicking(false);
      setRepeat('');
      await load();
      toast.success(`Task added, due ${due.toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' })}`);
    } catch (err) {
      toast.error(err.message);
    }
    setBusy(false);
  }

  async function complete(t) {
    try {
      await post(`/tasks/${t.id}/complete`);
      setTasks((ts) => ts.filter((x) => x.id !== t.id));
      setTotal((n) => Math.max(0, n - 1));
      toast.success('Done');
      // Completing a recurring task spawns its next occurrence server-side —
      // refetch so it shows up right away.
      if (t.repeat_every) await load();
    } catch (e) {
      toast.error(e.message);
    }
  }

  function showAll() {
    const want = Math.ceil(total / PAGE);
    setPages(want);
    load(want);
  }

  const now = new Date();
  const today = `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`;

  return (
    <div className="card">
      <h4 className="panel-title">Tasks</h4>
      {tasks === null ? (
        <Loading small label="Loading…" />
      ) : tasks.length === 0 ? (
        <div className="muted panel-empty">No tasks yet.</div>
      ) : (
        <div className="mini-tasks">
          {tasks.map((t) => {
            const overdue = t.due_at && parseWhen(t.due_at) < now;
            return (
              <label key={t.id} className="mini-task">
                <input type="checkbox" checked={false} onChange={() => complete(t)} title="Mark done" />
                <span className="mini-task-name">{t.name}</span>
                {t.repeat_every && (
                  <span className="task-repeat" title={repeatLabel(t.repeat_every, t.repeat_unit)}>
                    ↻
                  </span>
                )}
                {t.due_at && (
                  <span className={'mini-task-due' + (overdue ? ' overdue' : '')}>{fmtDateTime(t.due_at)}</span>
                )}
              </label>
            );
          })}
          {total > tasks.length && (
            <div className="muted panel-more">
              Showing {tasks.length} of {total} open tasks —{' '}
              <button type="button" className="linklike" onClick={showAll}>
                Show all
              </button>
            </div>
          )}
        </div>
      )}

      {!picking ? (
        <form className="quick-add" onSubmit={openPicker}>
          <input placeholder="Add a task…" value={name} onChange={(e) => setName(e.target.value)} />
          <button className="btn btn-small" type="submit" disabled={busy || !name.trim()}>
            When…
          </button>
        </form>
      ) : (
        <div className="when-row">
          <div className="when-inputs">
            <input type="date" min={today} value={whenDate} onChange={(e) => setWhenDate(e.target.value)} />
            <select value={whenHour} onChange={(e) => setWhenHour(Number(e.target.value))}>
              {HOURS.map((h) => (
                <option key={h} value={h}>
                  {hourLabel(h)}
                </option>
              ))}
            </select>
            <select value={whenMin} onChange={(e) => setWhenMin(Number(e.target.value))}>
              {MINUTES.map((m) => (
                <option key={m} value={m}>
                  :{pad(m)}
                </option>
              ))}
            </select>
            <select value={repeat} onChange={(e) => setRepeat(e.target.value)} title="Repeat">
              {REPEAT_OPTIONS.map((o) => (
                <option key={o.value} value={o.value}>
                  {o.label}
                </option>
              ))}
            </select>
          </div>
          <div className="composer-actions">
            <button className="btn btn-small" onClick={() => setPicking(false)} disabled={busy}>
              Cancel
            </button>
            <button className="btn btn-small btn-primary" onClick={addTask} disabled={busy || !whenDate}>
              Add task
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
