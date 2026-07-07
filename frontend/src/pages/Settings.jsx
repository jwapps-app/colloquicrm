import { useEffect, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import QRCode from 'qrcode';
import { del, get, patch, post } from '../api';
import { useAuth } from '../auth';
import { useToast } from '../components/Toast';
import FormModal from '../components/FormModal';
import InlineField from '../components/InlineField';
import { Loading } from '../components/ui';
import { humanize } from '../format';
import { CF_ENTITY_TYPES, CUSTOM_FIELD_TYPES } from '../constants/options';

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
              maxLength={6}
              value={code}
              onChange={(e) => setCode(e.target.value.replace(/\D/g, ''))}
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
              maxLength={6}
              value={code}
              onChange={(e) => setCode(e.target.value.replace(/\D/g, ''))}
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
        <button className="btn btn-primary btn-small" onClick={() => setShowAdd(true)}>
          + Add field
        </button>
      </div>
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
                <InlineField value={f.name} onSave={(v) => v && rename(f, v)} />
              </div>
              <span className="badge badge-muted">{humanize(f.field_type)}</span>
              {f.field_type === 'select' && <span className="muted cf-options">{(f.options || []).join(', ')}</span>}
              <button className="icon-btn tiny" onClick={() => remove(f)} title="Delete field">
                ×
              </button>
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
      const res = await post('/integrations/google/sync');
      toast.success(`Synced ${res.events_synced} calendar events`);
      setVersion((v) => v + 1);
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
                <p className="muted">✉ Email sync is on — mail involving your People and Leads shows on their timelines.</p>
              ) : (
                <p className="gmail-hint">
                  ✉ Email sync is available but needs a fresh Google consent.{' '}
                  <button type="button" className="btn btn-small" onClick={connect}>
                    Reconnect Google
                  </button>
                </p>
              )}
              {status.me.sync_error && <p className="form-error">Sync error: {status.me.sync_error}</p>}
              <div className="form-actions">
                <button className="btn btn-small" onClick={syncNow} disabled={busy}>
                  Sync calendar now
                </button>
                <a className="btn btn-small" href="/import?source=google">
                  Import contacts
                </a>
              </div>
              <div className="disconnect-row">
                <button className="btn btn-small btn-danger" onClick={disconnect}>
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

function IntegrationsSection() {
  return (
    <div className="integrations">
      <ColloquiSection />
      <GoogleSection />
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
  const [tab, setTab] = useState(searchParams.get('google') ? 'integrations' : 'profile');

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
    { id: 'integrations', label: 'Integrations' },
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
      {tab === 'integrations' && <IntegrationsSection />}
    </div>
  );
}
