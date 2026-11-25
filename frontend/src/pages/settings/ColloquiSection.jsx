import { useEffect, useState } from 'react';
import { del, get, post } from '../../api';
import { useAuth } from '../../auth';
import { useToast } from '../../components/Toast';
import { Loading, SecretInput, withoutBlankSecrets } from '../../components/ui';
import IntegrationErrorCard from './IntegrationErrorCard';

export default function ColloquiSection() {
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
    setStatus(null);
    get('/integrations/colloqui/status')
      .then((s) => {
        if (!on) return;
        setStatus(s);
        setBaseUrl(s.base_url || '');
        // Only admins link accounts, so only they need the remote user list.
        if (s.configured && user?.is_admin) {
          get('/integrations/colloqui/users')
            .then((u) => on && setColloquiUsers(u))
            .catch(() => on && setColloquiUsers([]));
        }
      })
      .catch((e) => {
        toast.error(e.message);
        // Error sentinel — a permanent spinner would otherwise sit here.
        if (on) setStatus({ load_error: true });
      });
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
      // A blank key on a configured integration is left out: "keep the stored one".
      const res = await post(
        '/integrations/colloqui/connect',
        withoutBlankSecrets({ base_url: baseUrl, api_key: apiKey }, ['api_key'])
      );
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
      // The server looks the account up itself and records its username.
      await post('/integrations/colloqui/link', { colloqui_user_id: target.id });
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
  if (status.load_error) {
    return <IntegrationErrorCard title="Connect to Colloqui" onRetry={() => setVersion((v) => v + 1)} />;
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
            <span>API key {status.configured && <span className="muted">(saved — leave blank to keep it)</span>}</span>
            <SecretInput value={apiKey} onChange={setApiKey} saved={!!status.configured} placeholder="colq_…" />
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
                Afterwards, an administrator maps each CRM user to their Colloqui account — under
                Settings → Users, or <em>My Colloqui account</em> below for yourself. That's what routes
                due-task DMs to the right person.
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
          {/* Linking decides who receives whose reminders and who is invited
              into the CRM space, so it is an administrator's call. Everyone
              else sees their mapping read-only. */}
          {!user?.is_admin ? (
            status.me?.colloqui_user_id ? (
              <p>
                Linked to <strong>@{status.me.colloqui_username || 'unknown'}</strong> — due-task reminders
                arrive as DMs. <span className="muted">Ask an administrator if this needs to change.</span>
              </p>
            ) : (
              <p className="muted">Not linked yet. Ask an administrator to link your Colloqui account.</p>
            )
          ) : status.me?.colloqui_user_id ? (
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
              <select value={selected} onChange={(e) => setSelected(e.target.value)} aria-label="Your Colloqui user">
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
          {user?.is_admin && (
            <p className="muted">Map other people&apos;s accounts under Settings → Users.</p>
          )}
        </div>
      )}

      {status.last_error && <p className="form-error">Last error: {status.last_error}</p>}
    </div>
  );
}
