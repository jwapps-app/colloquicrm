import { useEffect, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { del, get, patch, post } from '../api';
import { useAuth } from '../auth';
import { useToast } from '../components/Toast';
import AutomationsSection from '../components/AutomationsSection';
import FormsSection from '../components/FormsSection';
import FormModal from '../components/FormModal';
import InlineField from '../components/InlineField';
import { Loading } from '../components/ui';
import { fmtDate } from '../format';
import { CF_ENTITY_TYPES, CUSTOM_FIELD_TYPES } from '../constants/options';

/* ---------- Trash ---------- */

const TRASH_TYPES = [
  { api: '/people', label: 'People' },
  { api: '/leads', label: 'Leads' },
  { api: '/companies', label: 'Companies' },
  { api: '/opportunities', label: 'Opportunities' },
];

function TrashSection() {
  const toast = useToast();
  const [groups, setGroups] = useState(null);
  const [busyId, setBusyId] = useState(null);

  async function load() {
    const out = {};
    const results = await Promise.allSettled(
      TRASH_TYPES.map((t) => get(`${t.api}/trash/list`))
    );
    results.forEach((r, i) => {
      if (r.status === 'fulfilled' && r.value.items?.length) {
        out[TRASH_TYPES[i].api] = { label: TRASH_TYPES[i].label, items: r.value.items };
      }
    });
    setGroups(out);
  }

  useEffect(() => {
    load();
  }, []);

  async function restore(api, item) {
    setBusyId(item.id);
    try {
      await post(`${api}/${item.id}/restore`);
      setGroups((g) => {
        const next = { ...g };
        const grp = { ...next[api], items: next[api].items.filter((x) => x.id !== item.id) };
        if (grp.items.length) next[api] = grp;
        else delete next[api];
        return next;
      });
      toast.success(`Restored ${item.label}`);
    } catch (e) {
      toast.error(e.message);
    }
    setBusyId(null);
  }

  const total = groups ? Object.values(groups).reduce((n, g) => n + g.items.length, 0) : 0;

  return (
    <div className="card settings-card">
      <h3>Trash</h3>
      <p className="muted">
        Deleted records are kept here for 60 days, then permanently removed automatically. There's
        no manual empty — so anything deleted has the full window to be noticed and restored.
      </p>
      {groups === null ? (
        <Loading small />
      ) : total === 0 ? (
        <div className="muted panel-empty">Trash is empty.</div>
      ) : (
        Object.entries(groups).map(([api, grp]) => (
          <div key={api} className="trash-group">
            <h4 className="panel-title">
              {grp.label} ({grp.items.length})
            </h4>
            {grp.items.map((item) => (
              <div key={item.id} className="trash-row">
                <span className="trash-label">{item.label}</span>
                <span className="muted trash-when">deleted {fmtDate(item.deleted_at)}</span>
                <button
                  className="btn btn-small"
                  onClick={() => restore(api, item)}
                  disabled={busyId === item.id}
                >
                  Restore
                </button>
              </div>
            ))}
          </div>
        ))
      )}
    </div>
  );
}

/* ---------- Profile ---------- */

function ProfileSection() {
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

  async function savePassword(e) {
    e.preventDefault();
    if (next !== confirm) {
      toast.error('New passwords do not match.');
      return;
    }
    setBusy(true);
    try {
      await patch('/users/me', { current_password: current, new_password: next });
      setCurrent('');
      setNext('');
      setConfirm('');
      toast.success('Password changed');
    } catch (err) {
      toast.error(err.message);
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
        <div className="form-actions">
          <button className="btn btn-primary" type="submit" disabled={busy}>
            Change password
          </button>
        </div>
      </form>
    </>
  );
}

/* ---------- Security (TOTP) ---------- */

function SecuritySection() {
  const { user, setUser } = useAuth();
  const toast = useToast();
  const [setup, setSetup] = useState(null); // {secret, otpauth_url}
  const [qr, setQr] = useState(null);
  const [code, setCode] = useState('');
  const [busy, setBusy] = useState(false);

  async function begin() {
    setBusy(true);
    try {
      const s = await post('/auth/totp/setup');
      setSetup(s);
      setCode('');
      try {
        // Loaded on demand — 2FA setup is the only place qrcode is used.
        const { default: QRCode } = await import('qrcode');
        setQr(await QRCode.toDataURL(s.otpauth_url, { width: 220, margin: 1 }));
      } catch {
        setQr(null); // secret + URL below still work
      }
    } catch (e) {
      toast.error(e.message);
    }
    setBusy(false);
  }

  async function enable(e) {
    e.preventDefault();
    setBusy(true);
    try {
      await post('/auth/totp/enable', { code: code.trim() });
      setUser({ ...user, totp_enabled: true });
      setSetup(null);
      setCode('');
      toast.success('Two-factor authentication enabled');
    } catch (err) {
      toast.error(err.message);
    }
    setBusy(false);
  }

  async function disable(e) {
    e.preventDefault();
    setBusy(true);
    try {
      await post('/auth/totp/disable', { code: code.trim() });
      setUser({ ...user, totp_enabled: false });
      setCode('');
      toast.success('Two-factor authentication disabled');
    } catch (err) {
      toast.error(err.message);
    }
    setBusy(false);
  }

  function copy(text, label) {
    navigator.clipboard
      .writeText(text)
      .then(() => toast.success(`${label} copied`))
      .catch(() => toast.error('Could not copy to clipboard'));
  }

  return (
    <div className="card form settings-card">
      <h3>Two-factor authentication</h3>
      {user?.totp_enabled ? (
        <form onSubmit={disable} className="form">
          <p>
            <span className="badge status-won">Enabled</span> Your account is protected with an authenticator app.
          </p>
          <label className="field">
            <span>Enter a current code to disable</span>
            <input
              className="totp-input"
              inputMode="numeric"
              value={code}
              onChange={(e) => setCode(e.target.value.replace(/\D/g, '').slice(0, 6))}
              required
            />
          </label>
          <div className="form-actions">
            <button className="btn btn-danger" type="submit" disabled={busy || code.length !== 6}>
              Disable 2FA
            </button>
          </div>
        </form>
      ) : !setup ? (
        <>
          <p className="muted">Add a second factor using any TOTP authenticator app (1Password, Google Authenticator, …).</p>
          <div className="form-actions">
            <button className="btn btn-primary" onClick={begin} disabled={busy}>
              Set up two-factor authentication
            </button>
          </div>
        </>
      ) : (
        <form onSubmit={enable} className="form">
          <p className="muted">
            Scan the QR code with your authenticator app (1Password, Google Authenticator, …), then enter
            the 6-digit code it shows. Or add the secret manually.
          </p>
          {qr && (
            <div className="qr-wrap">
              <img src={qr} alt="TOTP QR code" width={220} height={220} />
            </div>
          )}
          <div className="secret-row">
            <code>{setup.secret}</code>
            <button type="button" className="btn btn-small" onClick={() => copy(setup.secret, 'Secret')}>
              Copy
            </button>
          </div>
          <div className="secret-row">
            <code className="otpauth">{setup.otpauth_url}</code>
            <button type="button" className="btn btn-small" onClick={() => copy(setup.otpauth_url, 'URL')}>
              Copy
            </button>
          </div>
          <label className="field">
            <span>Verification code</span>
            <input
              className="totp-input"
              inputMode="numeric"
              value={code}
              onChange={(e) => setCode(e.target.value.replace(/\D/g, '').slice(0, 6))}
              required
              autoFocus
            />
          </label>
          <div className="form-actions">
            <button type="button" className="btn" onClick={() => setSetup(null)}>
              Cancel
            </button>
            <button className="btn btn-primary" type="submit" disabled={busy || code.length !== 6}>
              Verify & enable
            </button>
          </div>
        </form>
      )}
    </div>
  );
}

/* ---------- Custom fields ---------- */

function CustomFieldsSection() {
  const { user } = useAuth();
  const isAdmin = !!user?.is_admin;
  const toast = useToast();
  const [entityType, setEntityType] = useState('person');
  const [fields, setFields] = useState(null);
  const [showAdd, setShowAdd] = useState(false);
  const [version, setVersion] = useState(0);

  useEffect(() => {
    let on = true;
    setFields(null);
    get('/custom-fields', { entity_type: entityType })
      .then((d) => {
        if (on) setFields(Array.isArray(d) ? d : []);
      })
      .catch((e) => {
        toast.error(e.message);
        if (on) setFields([]);
      });
    return () => {
      on = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [entityType, version]);

  async function addField(values) {
    const body = {
      entity_type: entityType,
      name: values.name,
      field_type: values.field_type,
      options:
        values.field_type === 'select'
          ? String(values.options || '')
              .split(',')
              .map((s) => s.trim())
              .filter(Boolean)
          : null,
    };
    await post('/custom-fields', body);
    setShowAdd(false);
    setVersion((v) => v + 1);
    toast.success('Custom field added');
  }

  async function rename(f, name) {
    try {
      await patch(`/custom-fields/${f.id}`, { name });
      setVersion((v) => v + 1);
    } catch (e) {
      toast.error(e.message);
    }
  }

  async function changeType(f, field_type) {
    try {
      await patch(`/custom-fields/${f.id}`, { field_type });
      setVersion((v) => v + 1);
      toast.success(field_type === 'date' ? 'Type changed — existing values converted to dates' : 'Type changed');
    } catch (e) {
      toast.error(e.message);
    }
  }

  async function changeOptions(f, raw) {
    const options = String(raw || '').split(',').map((s) => s.trim()).filter(Boolean);
    try {
      await patch(`/custom-fields/${f.id}`, { options });
      setVersion((v) => v + 1);
    } catch (e) {
      toast.error(e.message);
    }
  }

  async function remove(f) {
    if (!window.confirm(`Delete custom field "${f.name}"? Values on records will be lost.`)) return;
    try {
      await del(`/custom-fields/${f.id}`);
      setVersion((v) => v + 1);
    } catch (e) {
      toast.error(e.message);
    }
  }

  return (
    <div className="card settings-card">
      <div className="panel-head">
        <h3>Custom fields</h3>
        {isAdmin && (
          <button className="btn btn-primary btn-small" onClick={() => setShowAdd(true)}>
            + Add field
          </button>
        )}
      </div>
      {!isAdmin && (
        <p className="muted">Custom fields are org-wide — ask an administrator to change them.</p>
      )}
      <div className="tabs">
        {CF_ENTITY_TYPES.map((t) => (
          <button key={t.value} className={'tab' + (entityType === t.value ? ' active' : '')} onClick={() => setEntityType(t.value)}>
            {t.label}
          </button>
        ))}
      </div>
      {fields === null ? (
        <Loading small />
      ) : fields.length === 0 ? (
        <div className="muted panel-empty">No custom fields for {entityType} records yet.</div>
      ) : (
        <div className="cf-list">
          {fields.map((f) => (
            <div key={f.id} className="cf-row">
              <div className="cf-name">
                {isAdmin ? <InlineField value={f.name} onSave={(v) => v && rename(f, v)} /> : f.name}
              </div>
              {isAdmin ? (
                <select
                  className="inline-select cf-type"
                  value={f.field_type}
                  onChange={(e) => changeType(f, e.target.value)}
                  title="Field type"
                >
                  {CUSTOM_FIELD_TYPES.map((t) => (
                    <option key={t.value} value={t.value}>
                      {t.label}
                    </option>
                  ))}
                </select>
              ) : (
                <span className="muted cf-type">{f.field_type}</span>
              )}
              {f.field_type === 'select' && (
                <span className="cf-options">
                  {isAdmin ? (
                    <InlineField
                      value={(f.options || []).join(', ')}
                      onSave={(v) => changeOptions(f, v)}
                    />
                  ) : (
                    <span className="muted">{(f.options || []).join(', ')}</span>
                  )}
                </span>
              )}
              {isAdmin && (
                <button className="icon-btn tiny" onClick={() => remove(f)} title="Delete field" aria-label="Delete field">
                  ×
                </button>
              )}
            </div>
          ))}
        </div>
      )}
      {showAdd && (
        <FormModal
          title="Add custom field"
          submitLabel="Create"
          onClose={() => setShowAdd(false)}
          onSubmit={addField}
          fields={[
            { key: 'name', label: 'Name', required: true },
            { key: 'field_type', label: 'Type', type: 'select', options: CUSTOM_FIELD_TYPES, required: true, default: 'text' },
            {
              key: 'options',
              label: 'Options (comma-separated)',
              show: (v) => v.field_type === 'select',
              placeholder: 'Red, Green, Blue',
            },
          ]}
        />
      )}
    </div>
  );
}

/* ---------- Users (admin) ---------- */

function UsersSection() {
  const { user: me } = useAuth();
  const toast = useToast();
  const [users, setUsers] = useState(null);
  const [showAdd, setShowAdd] = useState(false);
  const [version, setVersion] = useState(0);

  useEffect(() => {
    let on = true;
    get('/users')
      .then((d) => {
        if (on) setUsers(d?.items || []);
      })
      .catch((e) => {
        toast.error(e.message);
        if (on) setUsers([]);
      });
    return () => {
      on = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [version]);

  async function addUser(values) {
    await post('/users', {
      email: values.email,
      password: values.password,
      display_name: values.display_name,
      is_admin: !!values.is_admin,
    });
    setShowAdd(false);
    setVersion((v) => v + 1);
    toast.success('User created');
  }

  async function toggleAdmin(u) {
    try {
      await patch(`/users/${u.id}`, { is_admin: !u.is_admin });
      setVersion((v) => v + 1);
    } catch (e) {
      toast.error(e.message);
    }
  }

  async function resetTotp(u) {
    if (!window.confirm(`Reset two-factor authentication for ${u.display_name}? They can sign in with just their password until they set it up again.`)) return;
    try {
      await post(`/users/${u.id}/reset-totp`);
      setVersion((v) => v + 1);
      toast.success(`2FA reset for ${u.display_name}`);
    } catch (e) {
      toast.error(e.message);
    }
  }

  async function resetPassword(u) {
    const pw = window.prompt(
      `New temporary password for ${u.display_name} (at least 8 characters). Their existing sessions will be signed out.`
    );
    if (!pw) return;
    try {
      await post(`/users/${u.id}/reset-password`, { new_password: pw });
      toast.success(`Password reset for ${u.display_name} — share it with them securely`);
    } catch (e) {
      toast.error(e.message);
    }
  }

  async function toggleActive(u) {
    const deactivating = u.is_active !== false;
    if (
      deactivating &&
      !window.confirm(`Deactivate ${u.display_name}? They will be signed out immediately and can no longer log in.`)
    )
      return;
    try {
      await patch(`/users/${u.id}`, { is_active: !deactivating ? true : false });
      setVersion((v) => v + 1);
      toast.success(deactivating ? `${u.display_name} deactivated` : `${u.display_name} reactivated`);
    } catch (e) {
      toast.error(e.message);
    }
  }

  return (
    <div className="card settings-card">
      <div className="panel-head">
        <h3>Users</h3>
        <button className="btn btn-primary btn-small" onClick={() => setShowAdd(true)}>
          + Add user
        </button>
      </div>
      {users === null ? (
        <Loading small />
      ) : (
        <div className="table-wrap">
          <table className="table">
            <thead>
              <tr>
                <th className="no-sort">Name</th>
                <th className="no-sort">Email</th>
                <th className="no-sort">Role</th>
                <th className="no-sort">Status</th>
                <th className="no-sort"></th>
              </tr>
            </thead>
            <tbody>
              {users.map((u) => (
                <tr key={u.id} className="static-row">
                  <td>
                    <strong>{u.display_name}</strong>
                    {u.id === me?.id && <span className="muted"> (you)</span>}
                  </td>
                  <td>{u.email}</td>
                  <td>{u.is_admin ? <span className="badge badge-admin">Admin</span> : 'Member'}</td>
                  <td>{u.is_active === false ? <span className="badge badge-muted">Inactive</span> : 'Active'}</td>
                  <td>
                    {u.id !== me?.id && (
                      <button className="btn btn-small" onClick={() => toggleAdmin(u)}>
                        {u.is_admin ? 'Remove admin' : 'Make admin'}
                      </button>
                    )}{' '}
                    <button className="btn btn-small" onClick={() => resetPassword(u)} title="Sets a temporary password and signs out their sessions">
                      Reset password
                    </button>{' '}
                    {u.totp_enabled && (
                      <button className="btn btn-small" onClick={() => resetTotp(u)} title="Clears their authenticator so they can sign in with password only">
                        Reset 2FA
                      </button>
                    )}{' '}
                    {u.id !== me?.id && (
                      <button
                        className={'btn btn-small' + (u.is_active === false ? '' : ' btn-danger-ghost')}
                        onClick={() => toggleActive(u)}
                      >
                        {u.is_active === false ? 'Reactivate' : 'Deactivate'}
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {showAdd && (
        <FormModal
          title="Add user"
          submitLabel="Create user"
          onClose={() => setShowAdd(false)}
          onSubmit={addUser}
          fields={[
            { key: 'display_name', label: 'Name', required: true },
            { key: 'email', label: 'Email', type: 'email', required: true },
            { key: 'password', label: 'Temporary password', type: 'password', required: true },
            { key: 'is_admin', label: 'Administrator', type: 'checkbox' },
          ]}
        />
      )}
    </div>
  );
}

/* ---------- Integrations ---------- */

function ColloquiSection() {
  const { user } = useAuth();
  const toast = useToast();
  const [status, setStatus] = useState(null);
  const [baseUrl, setBaseUrl] = useState('');
  const [apiKey, setApiKey] = useState('');
  const [colloquiUsers, setColloquiUsers] = useState(null);
  const [selected, setSelected] = useState('');
  const [busy, setBusy] = useState(false);
  const [version, setVersion] = useState(0);

  useEffect(() => {
    let on = true;
    get('/integrations/colloqui/status')
      .then((s) => {
        if (!on) return;
        setStatus(s);
        setBaseUrl(s.base_url || '');
        if (s.configured) {
          get('/integrations/colloqui/users')
            .then((u) => on && setColloquiUsers(u))
            .catch(() => on && setColloquiUsers([]));
        }
      })
      .catch((e) => toast.error(e.message));
    return () => {
      on = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [version]);

  const [bootstrapNote, setBootstrapNote] = useState(null);

  async function connect(e) {
    e.preventDefault();
    setBusy(true);
    try {
      const res = await post('/integrations/colloqui/connect', { base_url: baseUrl, api_key: apiKey });
      setApiKey('');
      setBootstrapNote(res.bootstrap_note || null);
      setVersion((v) => v + 1);
      toast.success(
        res.bootstrap_note
          ? 'Connected — everything was set up automatically'
          : 'Connected — space and #tasks channel are ready'
      );
    } catch (err) {
      toast.error(err.message);
    }
    setBusy(false);
  }

  async function disconnect() {
    if (!window.confirm('Disconnect Colloqui? Task posts and reminders will stop.')) return;
    try {
      await del('/integrations/colloqui/connect');
      setVersion((v) => v + 1);
    } catch (e) {
      toast.error(e.message);
    }
  }

  async function sendTest() {
    setBusy(true);
    try {
      await post('/integrations/colloqui/test');
      toast.success('Test message posted to #tasks');
    } catch (e) {
      toast.error(e.message);
    }
    setBusy(false);
  }

  async function linkAccount(e) {
    e.preventDefault();
    const target = (colloquiUsers || []).find((u) => u.id === selected);
    if (!target) return;
    setBusy(true);
    try {
      await post('/integrations/colloqui/link', {
        colloqui_user_id: target.id,
        colloqui_username: target.username,
      });
      setVersion((v) => v + 1);
      toast.success(`Linked to @${target.username}`);
    } catch (err) {
      toast.error(err.message);
    }
    setBusy(false);
  }

  async function unlink() {
    try {
      await del('/integrations/colloqui/link');
      setVersion((v) => v + 1);
    } catch (e) {
      toast.error(e.message);
    }
  }

  if (status === null) {
    return (
      <div className="card settings-card integration-card">
        <h3>Connect to Colloqui</h3>
        <Loading small />
      </div>
    );
  }

  return (
    <div className="card settings-card integration-card">
      <div className="panel-head">
        <h3>Connect to Colloqui</h3>
        {status.connected && <span className="badge status-won">Connected</span>}
      </div>
      <p className="muted">
        Posts task assignments to the <strong>#tasks</strong> channel in a "{status.space_name}" space on
        your Colloqui server, and DMs people when their tasks come due.
      </p>

      {user?.is_admin && (
        <form className="form" onSubmit={connect}>
          <label className="field">
            <span>Colloqui server URL</span>
            <input
              value={baseUrl}
              onChange={(e) => setBaseUrl(e.target.value)}
              placeholder="https://colloqui.example.com"
              required
            />
          </label>
          <label className="field">
            <span>API key {status.configured && <span className="muted">(saved — enter again to replace)</span>}</span>
            <input
              value={apiKey}
              onChange={(e) => setApiKey(e.target.value)}
              placeholder="colq_…"
              required={!status.configured}
              autoComplete="off"
            />
          </label>
          {bootstrapNote && <p className="bootstrap-note">✓ {bootstrapNote}</p>}
          <details className="howto">
            <summary>How to connect</summary>
            <div className="howto-body">
              <p>
                <strong>Easiest — paste an admin key.</strong> In Colloqui, go to Admin → API keys and
                create a key bound to <em>your own admin account</em>, then paste it here. The CRM sets
                everything up for you: it creates a <code>crm</code> service user, mints that user its own
                API key, provisions the "Colloqui CRM" space with a <strong>#tasks</strong> channel, and
                keeps only the service key. Your admin key is used once and never stored — revoke it in
                Colloqui afterwards.
              </p>
              <p>
                <strong>Manual — least privilege.</strong> If you'd rather not paste an admin key:
              </p>
              <ol>
                <li>In Colloqui, create a user named <code>crm</code> (no passkey or password needed).</li>
                <li>Under Admin → API keys, mint a key <em>bound to that crm user</em> and copy the full
                  <code>colq_…</code> value.</li>
                <li>Create a space named exactly <strong>"Colloqui CRM"</strong> and add <code>crm</code> to
                  it as a <strong>manager</strong> (only admins can create spaces).</li>
                <li>Paste the crm key here and connect — the CRM finds the space and creates
                  <strong>#tasks</strong> itself.</li>
              </ol>
              <p>
                Either way, the key must <em>not</em> be bound to a person who uses the CRM: Colloqui never
                notifies the sender of a message, so a key bound to you means you'd never get pushes for
                anything the CRM posts.
              </p>
              <p>
                Afterwards, everyone picks their own account under <em>My Colloqui account</em> below —
                that's what routes due-task DMs to the right person.
              </p>
            </div>
          </details>
          <div className="form-actions">
            {status.connected && (
              <button type="button" className="btn btn-small" onClick={sendTest} disabled={busy}>
                Send test message
              </button>
            )}
            <button className="btn btn-small btn-primary" type="submit" disabled={busy || !baseUrl || (!apiKey && !status.configured)}>
              {status.connected ? 'Reconnect' : 'Connect'}
            </button>
          </div>
          {status.connected && (
            <div className="disconnect-row">
              <button type="button" className="btn btn-small btn-danger" onClick={disconnect}>
                Disconnect
              </button>
            </div>
          )}
        </form>
      )}

      {status.configured && (
        <div className="link-block">
          <h4>My Colloqui account</h4>
          {status.me?.colloqui_user_id ? (
            <p>
              Linked to <strong>@{status.me.colloqui_username || 'unknown'}</strong> — due-task reminders
              arrive as DMs.{' '}
              <button className="btn btn-small" onClick={unlink}>
                Unlink
              </button>
            </p>
          ) : colloquiUsers === null ? (
            <Loading small />
          ) : (
            <form className="form-inline" onSubmit={linkAccount}>
              <select value={selected} onChange={(e) => setSelected(e.target.value)}>
                <option value="">Pick your Colloqui user…</option>
                {colloquiUsers.map((u) => (
                  <option key={u.id} value={u.id}>
                    {u.display_name} (@{u.username})
                  </option>
                ))}
              </select>
              <button className="btn btn-primary btn-small" type="submit" disabled={busy || !selected}>
                Link
              </button>
            </form>
          )}
        </div>
      )}

      {status.last_error && <p className="form-error">Last error: {status.last_error}</p>}
    </div>
  );
}

function GoogleSection() {
  const { user } = useAuth();
  const toast = useToast();
  const [status, setStatus] = useState(null);
  const [clientId, setClientId] = useState('');
  const [clientSecret, setClientSecret] = useState('');
  const [busy, setBusy] = useState(false);
  const [version, setVersion] = useState(0);

  useEffect(() => {
    let on = true;
    get('/integrations/google/status')
      .then((s) => {
        if (!on) return;
        setStatus(s);
        setClientId(s.client_id || '');
      })
      .catch((e) => toast.error(e.message));
    return () => {
      on = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [version]);

  async function saveConfig(e) {
    e.preventDefault();
    setBusy(true);
    try {
      await post('/integrations/google/config', { client_id: clientId, client_secret: clientSecret });
      setClientSecret('');
      setVersion((v) => v + 1);
      toast.success('Google OAuth client saved');
    } catch (err) {
      toast.error(err.message);
    }
    setBusy(false);
  }

  async function connect() {
    try {
      const { url } = await get('/integrations/google/auth-url');
      window.location.href = url;
    } catch (e) {
      toast.error(e.message);
    }
  }

  async function disconnect() {
    if (!window.confirm('Disconnect your Google account? Calendar sync stops for you.')) return;
    try {
      await del('/integrations/google/link');
      setVersion((v) => v + 1);
    } catch (e) {
      toast.error(e.message);
    }
  }

  async function syncNow() {
    setBusy(true);
    try {
      // One button does it all: pull email/calendar history and rebuild
      // interaction counts. Both run in the background.
      await post('/integrations/google/sync');
      if (user?.is_admin) {
        await post('/integrations/google/recompute-metrics').catch(() => {});
      }
      toast.success('Sync started — email history and interaction counts update in the background');
      setVersion((v) => v + 1);
    } catch (e) {
      toast.error(e.message);
    }
    setBusy(false);
  }

  async function scanSuggestions() {
    setBusy(true);
    try {
      await post('/integrations/google/scan-suggestions');
      toast.success('Scanning your email — new-contact suggestions appear on the People page shortly');
    } catch (e) {
      toast.error(e.message);
    }
    setBusy(false);
  }

  function copyRedirect() {
    navigator.clipboard
      .writeText(status?.redirect_uri || '')
      .then(() => toast.success('Redirect URI copied'))
      .catch(() => toast.error('Could not copy'));
  }

  if (status === null) {
    return (
      <div className="card settings-card integration-card">
        <h3>Connect to Google Workspace</h3>
        <Loading small />
      </div>
    );
  }

  return (
    <div className="card settings-card integration-card">
      <div className="panel-head">
        <h3>Connect to Google Workspace</h3>
        {status.me?.connected && <span className="badge status-won">Connected</span>}
      </div>
      <p className="muted">
        Read-only Contacts and Calendar sync. Google is never used to sign in to the CRM — this only
        pulls your contacts for import and matches calendar events to People, Leads and Companies.
      </p>

      {user?.is_admin && (
        <form className="form" onSubmit={saveConfig}>
          <details className="howto">
            <summary>How to set up (one-time, ~5 minutes)</summary>
            <div className="howto-body">
              <ol>
                <li>Go to <strong>console.cloud.google.com</strong> → create (or pick) a project.</li>
                <li>APIs &amp; Services → Library: enable the <strong>People API</strong> and the
                  <strong> Google Calendar API</strong>.</li>
                <li>APIs &amp; Services → OAuth consent screen: External (or Internal for a Workspace
                  domain), fill in the app name, add the two scopes if prompted.</li>
                <li>APIs &amp; Services → Credentials → Create credentials →
                  <strong> OAuth client ID</strong> → type <strong>Web application</strong>.</li>
                <li>Add this exact redirect URI, then paste the client ID and secret below.</li>
              </ol>
            </div>
          </details>
          <label className="field">
            <span>Authorized redirect URI (add this in Google console)</span>
            <div className="secret-row">
              <code>{status.redirect_uri}</code>
              <button type="button" className="btn btn-small" onClick={copyRedirect}>
                Copy
              </button>
            </div>
          </label>
          <label className="field">
            <span>Client ID</span>
            <input value={clientId} onChange={(e) => setClientId(e.target.value)} placeholder="….apps.googleusercontent.com" required />
          </label>
          <label className="field">
            <span>
              Client secret {status.configured && <span className="muted">(saved — enter again to replace)</span>}
            </span>
            <input
              value={clientSecret}
              onChange={(e) => setClientSecret(e.target.value)}
              autoComplete="off"
              required={!status.configured}
            />
          </label>
          <div className="form-actions">
            <button className="btn btn-small btn-primary" type="submit" disabled={busy || !clientId || (!clientSecret && !status.configured)}>
              Save
            </button>
          </div>
        </form>
      )}

      {status.configured && (
        <div className="link-block">
          <h4>My Google account</h4>
          {status.me?.connected ? (
            <>
              <p>
                Connected as <strong>{status.me.email}</strong>
                {status.me.last_synced_at ? ` — synced ${new Date(status.me.last_synced_at).toLocaleString()}` : ' — not synced yet'}
              </p>
              {status.me.gmail_enabled ? (
                <>
                  <p className="muted">
                    ✉ Email sync is on — {status.emails_matched ?? 0} matched email
                    {(status.emails_matched ?? 0) === 1 ? '' : 's'} so far. Mail involving your People and
                    Leads shows on their timelines. After adding new people, hit "Sync now" to pull
                    their email history.
                  </p>
                  {!status.me.gmail_backfill_done && status.me.gmail_backfill_total ? (
                    <div className="import-progress">
                      <div className="progress-track">
                        <div
                          className="progress-fill"
                          style={{
                            width: `${Math.min(100, Math.round(((status.me.gmail_backfill_cursor || 0) / status.me.gmail_backfill_total) * 100))}%`,
                          }}
                        />
                      </div>
                      <p className="muted">
                        History backfill: {status.me.gmail_backfill_cursor || 0} of{' '}
                        {status.me.gmail_backfill_total} contacts scanned. Runs in the background;
                        emails and interaction counts keep filling in.
                      </p>
                    </div>
                  ) : (
                    <p className="muted">✓ History backfill complete.</p>
                  )}
                </>
              ) : (
                <p className="gmail-hint">
                  ✉ Email sync is available but needs a fresh Google consent.{' '}
                  <button type="button" className="btn btn-small" onClick={connect}>
                    Reconnect Google
                  </button>
                </p>
              )}
              {status.me.sync_error && <p className="form-error">Sync error: {status.me.sync_error}</p>}
              <div className="google-actions">
                <button className="btn btn-small btn-primary" onClick={syncNow} disabled={busy}>
                  {busy ? 'Working…' : 'Sync now'}
                </button>
                <a className="btn btn-small btn-ghost" href="/import?source=google">
                  Import contacts
                </a>
                <button className="btn btn-small btn-ghost" onClick={scanSuggestions} disabled={busy}>
                  Scan for new contacts
                </button>
                <button className="btn btn-small btn-danger-ghost google-disconnect" onClick={disconnect}>
                  Disconnect
                </button>
              </div>
            </>
          ) : (
            <button className="btn btn-primary" onClick={connect}>
              Connect Google account
            </button>
          )}
        </div>
      )}
      {!status.configured && !user?.is_admin && (
        <p className="muted">Ask an administrator to configure the Google OAuth client first.</p>
      )}
    </div>
  );
}

function RingCentralSection() {
  const { user } = useAuth();
  const toast = useToast();
  const [status, setStatus] = useState(null);
  const [clientId, setClientId] = useState('');
  const [clientSecret, setClientSecret] = useState('');
  const [jwt, setJwt] = useState('');
  const [busy, setBusy] = useState(false);
  const [version, setVersion] = useState(0);

  useEffect(() => {
    let on = true;
    get('/integrations/ringcentral/status')
      .then((s) => on && setStatus(s))
      .catch((e) => toast.error(e.message));
    return () => {
      on = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [version]);

  async function connect(e) {
    e.preventDefault();
    setBusy(true);
    try {
      await post('/integrations/ringcentral/connect', { client_id: clientId, client_secret: clientSecret, jwt });
      setClientSecret('');
      setJwt('');
      setVersion((v) => v + 1);
      toast.success('RingCentral connected');
    } catch (err) {
      toast.error(err.message);
    }
    setBusy(false);
  }

  async function disconnect() {
    if (!window.confirm('Disconnect RingCentral? Call and text syncing stops.')) return;
    try {
      await del('/integrations/ringcentral/connect');
      setVersion((v) => v + 1);
    } catch (e) {
      toast.error(e.message);
    }
  }

  async function syncNow() {
    setBusy(true);
    try {
      const res = await post('/integrations/ringcentral/sync');
      toast.success(`Synced ${res.calls_synced} calls, ${res.sms_synced} texts`);
      setVersion((v) => v + 1);
    } catch (e) {
      toast.error(e.message);
    }
    setBusy(false);
  }

  if (status === null) {
    return (
      <div className="card settings-card integration-card">
        <h3>Connect to RingCentral</h3>
        <Loading small />
      </div>
    );
  }

  return (
    <div className="card settings-card integration-card">
      <div className="panel-head">
        <h3>Connect to RingCentral</h3>
        {status.configured && <span className="badge status-won">Connected</span>}
      </div>
      <p className="muted">
        Logs calls and text messages onto matching People and Leads by phone number. Read-only — the
        CRM never places calls or sends texts.
      </p>

      {user?.is_admin && (
        <form className="form" onSubmit={connect}>
          <details className="howto">
            <summary>How to connect</summary>
            <div className="howto-body">
              <ol>
                <li>At <strong>developers.ringcentral.com</strong> → Console → Apps → Register App →
                  <strong> REST API App</strong>, auth type <strong>JWT auth flow</strong>.</li>
                <li>Scopes: <strong>Read Call Log, Read Messages, Read Accounts, Read Call Recordings</strong>.</li>
                <li>Copy the app's <strong>Client ID</strong> and <strong>Client Secret</strong>.</li>
                <li>Profile menu → Credentials → <strong>JWT</strong> → Create (Production, authorized for
                  the app) and copy the long token.</li>
              </ol>
            </div>
          </details>
          <label className="field">
            <span>Client ID</span>
            <input value={clientId} onChange={(e) => setClientId(e.target.value)} required={!status.configured} autoComplete="off" />
          </label>
          <label className="field">
            <span>Client secret {status.configured && <span className="muted">(saved — re-enter to replace)</span>}</span>
            <input value={clientSecret} onChange={(e) => setClientSecret(e.target.value)} required={!status.configured} autoComplete="off" />
          </label>
          <label className="field">
            <span>JWT credential</span>
            <textarea rows={3} value={jwt} onChange={(e) => setJwt(e.target.value)} required={!status.configured} />
          </label>
          <div className="form-actions">
            {status.configured && (
              <button type="button" className="btn btn-small" onClick={syncNow} disabled={busy}>
                Sync now
              </button>
            )}
            <button className="btn btn-small btn-primary" type="submit" disabled={busy}>
              {status.configured ? 'Reconnect' : 'Connect'}
            </button>
          </div>
          {status.configured && (
            <div className="disconnect-row">
              <button type="button" className="btn btn-small btn-danger" onClick={disconnect}>
                Disconnect
              </button>
            </div>
          )}
        </form>
      )}

      {status.configured && (
        <p className="muted">
          Account numbers: {(status.own_numbers || []).join(', ') || 'none found'}
          {status.last_synced_at ? ` — synced ${new Date(status.last_synced_at).toLocaleString()}` : ' — not synced yet'}
        </p>
      )}
      {status.sync_error && <p className="form-error">Sync error: {status.sync_error}</p>}
    </div>
  );
}

function IntegrationsSection() {
  return (
    <div className="integrations">
      <ColloquiSection />
      <GoogleSection />
      <RingCentralSection />
    </div>
  );
}

/* ---------- Page ---------- */

const GOOGLE_RESULT_MESSAGES = {
  connected: null, // success toast
  denied: 'Google access was denied.',
  state_error: 'The sign-in link expired — try connecting again.',
  not_configured: 'The Google OAuth client is not configured.',
  exchange_error: 'Google rejected the token exchange — check the client ID and secret.',
  no_refresh_token: 'Google did not return an offline token — try again (you may need to remove the app at myaccount.google.com/permissions first).',
};

export default function Settings() {
  const { user } = useAuth();
  const toast = useToast();
  const [searchParams, setSearchParams] = useSearchParams();
  const [tab, setTab] = useState(
    searchParams.get('tab') || (searchParams.get('google') ? 'integrations' : 'profile')
  );

  useEffect(() => {
    const result = searchParams.get('google');
    if (!result) return;
    if (result === 'connected') {
      toast.success('Google account connected');
    } else {
      toast.error(GOOGLE_RESULT_MESSAGES[result] || `Google connection failed (${result}).`);
    }
    setSearchParams({}, { replace: true });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const tabs = [
    { id: 'profile', label: 'Profile' },
    { id: 'security', label: 'Security' },
    { id: 'fields', label: 'Custom Fields' },
    ...(user?.is_admin ? [{ id: 'users', label: 'Users' }] : []),
    ...(user?.is_admin ? [{ id: 'automations', label: 'Automations' }] : []),
    ...(user?.is_admin ? [{ id: 'forms', label: 'Forms' }] : []),
    { id: 'integrations', label: 'Integrations' },
    { id: 'trash', label: 'Trash' },
  ];

  return (
    <div className="page page-narrow">
      <div className="page-head">
        <h1>Settings</h1>
      </div>
      <div className="tabs settings-tabs">
        {tabs.map((t) => (
          <button key={t.id} className={'tab' + (tab === t.id ? ' active' : '')} onClick={() => setTab(t.id)}>
            {t.label}
          </button>
        ))}
      </div>
      {tab === 'profile' && <ProfileSection />}
      {tab === 'security' && <SecuritySection />}
      {tab === 'fields' && <CustomFieldsSection />}
      {tab === 'users' && user?.is_admin && <UsersSection />}
      {tab === 'automations' && user?.is_admin && <AutomationsSection />}
      {tab === 'forms' && user?.is_admin && <FormsSection />}
      {tab === 'integrations' && <IntegrationsSection />}
      {tab === 'trash' && <TrashSection />}
    </div>
  );
}
