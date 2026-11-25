import { useEffect, useState } from 'react';
import { bustCache, del, get, patch, post } from '../../api';
import { useAuth } from '../../auth';
import { useToast } from '../../components/Toast';
import FormModal from '../../components/FormModal';
import { Loading } from '../../components/ui';

/* ---------- Users (admin) ---------- */

export default function UsersSection() {
  const { user: me } = useAuth();
  const toast = useToast();
  const [users, setUsers] = useState(null);
  const [showAdd, setShowAdd] = useState(false);
  const [version, setVersion] = useState(0);
  // Colloqui accounts to map CRM users onto: null = integration not set up
  // (no column), 'error' = couldn't list them, else the remote users.
  const [colloquiUsers, setColloquiUsers] = useState(null);
  const [linkBusy, setLinkBusy] = useState(null);

  useEffect(() => {
    let on = true;
    get('/integrations/colloqui/status')
      .then((s) => {
        if (!on || !s?.configured) return null;
        return get('/integrations/colloqui/users').then((list) => {
          if (on) setColloquiUsers(Array.isArray(list) ? list : []);
        });
      })
      .catch(() => {
        if (on) setColloquiUsers('error');
      });
    return () => {
      on = false;
    };
  }, []);

  /** Map (or un-map) a CRM user's Colloqui account — an admin-only action;
   * the server verifies the remote account and records its username. */
  async function setColloquiLink(u, colloquiUserId) {
    setLinkBusy(u.id);
    try {
      if (colloquiUserId) {
        const res = await post('/integrations/colloqui/link', { colloqui_user_id: colloquiUserId, user_id: u.id });
        toast.success(`${u.display_name} linked to @${res?.colloqui_username || 'their Colloqui account'}`);
      } else {
        await del(`/integrations/colloqui/link?user_id=${encodeURIComponent(u.id)}`);
        toast.success(`${u.display_name} unlinked from Colloqui`);
      }
      bustCache('/users');
      setVersion((v) => v + 1);
    } catch (e) {
      toast.error(e.message);
    }
    setLinkBusy(null);
  }

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
    bustCache('/users');
    setShowAdd(false);
    setVersion((v) => v + 1);
    toast.success('User created');
  }

  async function toggleAdmin(u) {
    try {
      await patch(`/users/${u.id}`, { is_admin: !u.is_admin });
      bustCache('/users');
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
      bustCache('/users');
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
                {colloquiUsers !== null && <th className="no-sort">Colloqui account</th>}
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
                  {colloquiUsers !== null && (
                    <td>
                      {colloquiUsers === 'error' ? (
                        <span className="muted">{u.colloqui_username ? `@${u.colloqui_username}` : '—'}</span>
                      ) : (
                        (() => {
                          // /users carries the linked username; the option
                          // values are Colloqui user ids.
                          const linked = colloquiUsers.find((cu) => cu.username === u.colloqui_username);
                          const orphan = u.colloqui_username && !linked;
                          return (
                            <select
                              className="user-colloqui"
                              aria-label={`Colloqui account for ${u.display_name}`}
                              value={linked ? linked.id : orphan ? '__orphan__' : ''}
                              disabled={linkBusy === u.id}
                              onChange={(e) => {
                                if (e.target.value !== '__orphan__') setColloquiLink(u, e.target.value);
                              }}
                            >
                              <option value="">Not linked</option>
                              {orphan && <option value="__orphan__">@{u.colloqui_username} (not on the server)</option>}
                              {colloquiUsers.map((cu) => (
                                <option key={cu.id} value={cu.id}>
                                  {cu.display_name} (@{cu.username})
                                </option>
                              ))}
                            </select>
                          );
                        })()
                      )}
                    </td>
                  )}
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
