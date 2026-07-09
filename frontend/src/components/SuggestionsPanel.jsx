import { useEffect, useState } from 'react';
import { get, post } from '../api';
import { useContactTypes } from '../hooks';
import { useToast } from './Toast';

/**
 * Contact suggestions mined from Gmail: frequent correspondents who aren't in
 * the CRM yet. Add one (creates a Person, typed by the header selector) or
 * Ignore it (never resurfaces). Collapsed by default — just the count.
 */
export default function SuggestionsPanel({ onAdded }) {
  const toast = useToast();
  const contactTypes = useContactTypes();
  const [items, setItems] = useState([]);
  const [collapsed, setCollapsed] = useState(true);
  const [scanning, setScanning] = useState(false);
  const [busyId, setBusyId] = useState(null);
  const [addType, setAddType] = useState('Personal');

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
      await post(`/contact-suggestions/${s.id}/add`, { contact_type: addType });
      setItems((xs) => xs.filter((x) => x.id !== s.id));
      toast.success(`Added ${s.display_name || s.email} as ${addType}`);
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
        <button className="suggestions-toggle" onClick={() => setCollapsed((c) => !c)}>
          <span className="suggestions-caret">{collapsed ? '▸' : '▾'}</span>
          <strong>
            {items.length} suggested contact{items.length === 1 ? '' : 's'} from your email
          </strong>
        </button>
        <div className="suggestions-head-actions">
          {!collapsed && (
            <label className="suggestions-type">
              Add as
              <select value={addType} onChange={(e) => setAddType(e.target.value)}>
                {(contactTypes.length ? contactTypes : [{ value: 'Personal', label: 'Personal' }]).map(
                  (t) => (
                    <option key={t.value} value={t.value}>
                      {t.label}
                    </option>
                  )
                )}
              </select>
            </label>
          )}
          <button className="btn btn-small" onClick={scan} disabled={scanning}>
            {scanning ? 'Scanning…' : 'Rescan email'}
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
