import { useState } from 'react';
import { patch } from '../../api';
import { useAuth } from '../../auth';
import { useToast } from '../../components/Toast';

/* ---------- Profile ---------- */

export default function ProfileSection() {
  const { user, setUser } = useAuth();
  const toast = useToast();
  const [displayName, setDisplayName] = useState(user?.display_name || '');
  const [current, setCurrent] = useState('');
  const [next, setNext] = useState('');
  const [confirm, setConfirm] = useState('');
  const [busy, setBusy] = useState(false);

  async function saveName(e) {
    e.preventDefault();
    setBusy(true);
    try {
      await patch('/users/me', { display_name: displayName });
      setUser({ ...user, display_name: displayName });
      toast.success('Name updated');
    } catch (err) {
      toast.error(err.message);
    }
    setBusy(false);
  }

  const [pwError, setPwError] = useState('');

  async function savePassword(e) {
    e.preventDefault();
    if (next !== confirm) {
      toast.error('New passwords do not match.');
      return;
    }
    setBusy(true);
    setPwError('');
    try {
      await patch('/users/me', { current_password: current, new_password: next });
      setCurrent('');
      setNext('');
      setConfirm('');
      toast.success('Password changed');
    } catch (err) {
      // A wrong current password is a 401 that leaves the session alone
      // (api.js step-up handling) — say so next to the field, where it's fixed.
      setPwError(err.message);
    }
    setBusy(false);
  }

  const [nameOrder, setNameOrder] = useState(() => {
    try {
      return localStorage.getItem('crm_name_format') === 'last-first' ? 'last-first' : 'first-last';
    } catch {
      return 'first-last';
    }
  });

  function changeNameOrder(value) {
    setNameOrder(value);
    try {
      localStorage.setItem('crm_name_format', value);
    } catch {
      // storage unavailable — the choice just won't persist
    }
    // Names are rendered from static column definitions across many pages;
    // reload so the new order applies everywhere at once.
    window.location.reload();
  }

  const [hideSelf, setHideSelf] = useState(() => {
    try {
      return localStorage.getItem('crm_hide_self') === '1';
    } catch {
      return false;
    }
  });

  const [notifyChannel, setNotifyChannel] = useState(user?.notify_channel || 'colloqui_chat');

  async function changeNotifyChannel(value) {
    const previous = notifyChannel;
    setNotifyChannel(value);
    try {
      await patch('/users/me', { notify_channel: value });
      setUser({ ...user, notify_channel: value });
      toast.success(
        value === 'crm_push'
          ? 'Task notifications will go to the iOS app'
          : 'Task notifications will go to Colloqui chat'
      );
    } catch (err) {
      setNotifyChannel(previous);
      toast.error(err.message);
    }
  }

  function changeHideSelf(checked) {
    setHideSelf(checked);
    try {
      localStorage.setItem('crm_hide_self', checked ? '1' : '0');
    } catch {
      // best-effort
    }
  }

  return (
    <>
      <form className="card form settings-card" onSubmit={saveName}>
        <h3>Profile</h3>
        <label className="field">
          <span>Display name</span>
          <input value={displayName} onChange={(e) => setDisplayName(e.target.value)} required />
        </label>
        <label className="field">
          <span>Email</span>
          <input value={user?.email || ''} disabled />
        </label>
        <label className="field">
          <span>Name display</span>
          <select value={nameOrder} onChange={(e) => changeNameOrder(e.target.value)}>
            <option value="first-last">First Last (John Doe)</option>
            <option value="last-first">Last, First (Doe, John)</option>
          </select>
        </label>
        <label className="cf-checkbox settings-check">
          <input
            type="checkbox"
            checked={hideSelf}
            onChange={(e) => changeHideSelf(e.target.checked)}
          />
          <span>Hide my own contact from the People list</span>
        </label>
        <label className="field">
          <span>Send task notifications to</span>
          <select value={notifyChannel} onChange={(e) => changeNotifyChannel(e.target.value)}>
            <option value="colloqui_chat">Colloqui chat</option>
            <option value="crm_push">The iOS app (push)</option>
          </select>
          <span className="muted" style={{ fontSize: 12 }}>
            Reminders and assignments arrive on exactly one channel.
          </span>
        </label>
        <div className="form-actions">
          <button className="btn btn-primary" type="submit" disabled={busy}>
            Save
          </button>
        </div>
      </form>

      <form className="card form settings-card" onSubmit={savePassword}>
        <h3>Change password</h3>
        <label className="field">
          <span>Current password</span>
          <input type="password" value={current} onChange={(e) => setCurrent(e.target.value)} required autoComplete="current-password" />
        </label>
        <label className="field">
          <span>New password</span>
          <input type="password" value={next} onChange={(e) => setNext(e.target.value)} required minLength={8} autoComplete="new-password" />
        </label>
        <label className="field">
          <span>Confirm new password</span>
          <input type="password" value={confirm} onChange={(e) => setConfirm(e.target.value)} required autoComplete="new-password" />
        </label>
        {pwError && (
          <div className="form-error" role="alert">
            {pwError}
          </div>
        )}
        <div className="form-actions">
          <button className="btn btn-primary" type="submit" disabled={busy}>
            Change password
          </button>
        </div>
      </form>
    </>
  );
}
