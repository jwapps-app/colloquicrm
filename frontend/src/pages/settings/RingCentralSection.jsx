import { useEffect, useState } from 'react';
import { del, get, post } from '../../api';
import { useAuth } from '../../auth';
import { useToast } from '../../components/Toast';
import { Loading, SecretInput, withoutBlankSecrets } from '../../components/ui';
import IntegrationErrorCard from './IntegrationErrorCard';

export default function RingCentralSection() {
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
    setStatus(null);
    get('/integrations/ringcentral/status')
      .then((s) => on && setStatus(s))
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

  async function connect(e) {
    e.preventDefault();
    setBusy(true);
    try {
      // Blank secrets are omitted — the server keeps the stored ones.
      await post(
        '/integrations/ringcentral/connect',
        withoutBlankSecrets({ client_id: clientId, client_secret: clientSecret, jwt }, ['client_secret', 'jwt'])
      );
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
      if (e.status === 409) toast.info('A RingCentral sync is already running — it will finish on its own.');
      else toast.error(e.message);
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
  if (status.load_error) {
    return (
      <IntegrationErrorCard title="Connect to RingCentral" onRetry={() => setVersion((v) => v + 1)} />
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
            {/* Not a secret, but the status endpoint doesn't return it and the
                server requires it on every connect. */}
            <span>Client ID {status.configured && <span className="muted">(re-enter to reconnect)</span>}</span>
            <input value={clientId} onChange={(e) => setClientId(e.target.value)} required autoComplete="off" />
          </label>
          <label className="field">
            <span>Client secret {status.configured && <span className="muted">(saved — leave blank to keep it)</span>}</span>
            <SecretInput value={clientSecret} onChange={setClientSecret} saved={!!status.configured} />
          </label>
          <label className="field">
            <span>JWT credential {status.configured && <span className="muted">(saved — leave blank to keep it)</span>}</span>
            <SecretInput value={jwt} onChange={setJwt} saved={!!status.configured} />
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
