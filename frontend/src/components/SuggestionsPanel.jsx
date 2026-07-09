import { useEffect, useState } from 'react';
import { get, post } from '../api';
import { useToast } from './Toast';

/**
 * Contact suggestions mined from Gmail: frequent correspondents who aren't in
 * the CRM yet. Add one (creates a Person) or Ignore it (never resurfaces).
 * Shown as a dismissible banner atop the People list.
 */
export default function SuggestionsPanel({ onAdded }) {
  const toast = useToast();
  const [items, setItems] = useState([]);
  const [collapsed, setCollapsed] = useState(false);
  const [scanning, setScanning] = useState(false);
  const [busyId, setBusyId] = useState(null);

  async function load() {
    try {
      const d = await get('/contact-suggestions');
      setItems(d.items || []);
    } catch {
      // silent — suggestions are optional
    }
  }

  useEffect(() => {
    load();
  }, []);

  async function scan() {
    setScanning(true);
    try {
      await post('/integrations/google/scan-suggestions');
      toast.success('Scanning your recent email — new suggestions appear in a minute');
      // Poll a couple times so freshly-found suggestions surface without a refresh.
      setTimeout(load, 8000);
      setTimeout(load, 20000);
    } catch (e) {
      toast.error(e.message);
    }
    setScanning(false);
  }

  async function add(s) {
    setBusyId(s.id);
    try {
      await post(`/contact-suggestions/${s.id}/add`);
      setItems((xs) => xs.filter((x) => x.id !== s.id));
      toast.success(`Added ${s.display_name || s.email}`);
      onAdded?.();
    } catch (e) {
      toast.error(e.message);
    }
    setBusyId(null);
  }

  async function ignore(s) {
    setBusyId(s.id);
    try {
      await post(`/contact-suggestions/${s.id}/ignore`);
      setItems((xs) => xs.filter((x) => x.id !== s.id));
    } catch (e) {
      toast.error(e.message);
    }
    setBusyId(null);
  }

  if (items.length === 0) return null;

  return (
    <div className="card suggestions">
      <div className="suggestions-head">
        <strong>{items.length} suggested contact{items.length === 1 ? '' : 's'} from your email</strong>
        <div className="suggestions-head-actions">
          <button className="btn btn-small" onClick={scan} disabled={scanning}>
            {scanning ? 'Scanning…' : 'Rescan email'}
          </button>
          <button className="linklike" onClick={() => setCollapsed((c) => !c)}>
            {collapsed ? 'Show' : 'Hide'}
          </button>
        </div>
      </div>
      {!collapsed && (
        <div className="suggestions-list">
          {items.map((s) => (
            <div key={s.id} className="suggestion-row">
              <div className="suggestion-who">
                <span className="suggestion-name">{s.display_name || s.email}</span>
                {s.display_name && <span className="muted suggestion-email">{s.email}</span>}
              </div>
              <span className="muted suggestion-count">
                {s.message_count} message{s.message_count === 1 ? '' : 's'}
              </span>
              <div className="suggestion-actions">
                <button className="btn btn-small btn-primary" onClick={() => add(s)} disabled={busyId === s.id}>
                  Add
                </button>
                <button className="btn btn-small btn-ghost" onClick={() => ignore(s)} disabled={busyId === s.id}>
                  Ignore
                </button>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
