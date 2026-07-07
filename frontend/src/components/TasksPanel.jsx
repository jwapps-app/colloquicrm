import { useEffect, useState } from 'react';
import { get, post } from '../api';
import { useToast } from './Toast';
import { fmtDate } from '../format';
import { Loading } from './ui';

export default function TasksPanel({ entityType, entityId }) {
  const toast = useToast();
  const [tasks, setTasks] = useState(null);
  const [name, setName] = useState('');
  const [busy, setBusy] = useState(false);

  async function load() {
    try {
      const d = await get('/tasks', { entity_type: entityType, entity_id: entityId, status: 'open', page_size: 50 });
      setTasks(d?.items || []);
    } catch (e) {
      toast.error(e.message);
      setTasks([]);
    }
  }

  useEffect(() => {
    setTasks(null);
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [entityType, entityId]);

  async function add(e) {
    e.preventDefault();
    const n = name.trim();
    if (!n) return;
    setBusy(true);
    try {
      await post('/tasks', { name: n, entity_type: entityType, entity_id: entityId });
      setName('');
      await load();
    } catch (err) {
      toast.error(err.message);
    }
    setBusy(false);
  }

  async function complete(t) {
    try {
      await post(`/tasks/${t.id}/complete`);
      setTasks((ts) => ts.filter((x) => x.id !== t.id));
      toast.success('Task completed');
    } catch (e) {
      toast.error(e.message);
    }
  }

  const now = new Date();

  return (
    <div className="card">
      <h4 className="panel-title">Tasks</h4>
      {tasks === null ? (
        <Loading small label="Loading…" />
      ) : tasks.length === 0 ? (
        <div className="muted panel-empty">No open tasks.</div>
      ) : (
        <div className="mini-tasks">
          {tasks.map((t) => {
            const overdue = t.due_at && new Date(t.due_at) < now;
            return (
              <label key={t.id} className="mini-task">
                <input type="checkbox" checked={false} onChange={() => complete(t)} title="Mark done" />
                <span className="mini-task-name">{t.name}</span>
                {t.due_at && <span className={'mini-task-due' + (overdue ? ' overdue' : '')}>{fmtDate(t.due_at)}</span>}
              </label>
            );
          })}
        </div>
      )}
      <form className="quick-add" onSubmit={add}>
        <input placeholder="Quick add task…" value={name} onChange={(e) => setName(e.target.value)} />
        <button className="btn btn-small" type="submit" disabled={busy || !name.trim()}>
          Add
        </button>
      </form>
    </div>
  );
}
