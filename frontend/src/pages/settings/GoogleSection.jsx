import { useEffect, useState } from 'react';
import { del, get, post } from '../../api';
import { useAuth } from '../../auth';
import { useToast } from '../../components/Toast';
import { Loading, SecretInput, withoutBlankSecrets } from '../../components/ui';
import IntegrationErrorCard from './IntegrationErrorCard';

/** Failed Gmail work that is still owed a retry, or was given up on — shown
 * only when there is any, so a clean "no sync error" can't hide a gap. */
function GmailRetryCounts({ me }) {
  const n = (k) => Number(me?.[k]) || 0;
  const pending = { messages: n('gmail_messages_pending_retry'), addresses: n('gmail_addresses_pending_retry') };
  const givenUp = { messages: n('gmail_messages_given_up'), addresses: n('gmail_addresses_given_up') };
  const describe = ({ messages, addresses }) =>
    [
      messages ? `${messages} message${messages === 1 ? '' : 's'}` : null,
      addresses ? `${addresses} contact address${addresses === 1 ? '' : 'es'}` : null,
    ]
      .filter(Boolean)
      .join(' and ');
  const hasPending = pending.messages + pending.addresses > 0;
  const hasGivenUp = givenUp.messages + givenUp.addresses > 0;
  if (!hasPending && !hasGivenUp) return null;
  return (
    <>
      {hasPending && (
        <p className="gmail-hint">
          ↻ {describe(pending)} couldn&apos;t be fetched and will be retried on the next syncs.
        </p>
      )}
      {hasGivenUp && (
        <p className="form-error">
          {describe(givenUp)} failed repeatedly and {givenUp.messages + givenUp.addresses === 1 ? 'was' : 'were'} given
          up on — that mail is missing from the timelines.
        </p>
      )}
    </>
  );
}

export default function GoogleSection() {
  const { user } = useAuth();
  const toast = useToast();
  const [status, setStatus] = useState(null);
  const [clientId, setClientId] = useState('');
  const [clientSecret, setClientSecret] = useState('');
  const [busy, setBusy] = useState(false);
  const [version, setVersion] = useState(0);

  useEffect(() => {
    let on = true;
    setStatus(null);
    get('/integrations/google/status')
      .then((s) => {
        if (!on) return;
        setStatus(s);
        setClientId(s.client_id || '');
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

  async function saveConfig(e) {
    e.preventDefault();
    setBusy(true);
    try {
      // Blank secret = omitted = the server keeps the stored one.
      await post(
        '/integrations/google/config',
        withoutBlankSecrets({ client_id: clientId, client_secret: clientSecret }, ['client_secret'])
      );
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
      // 409 = a sync (manual or scheduled) is already in flight. Not an error
      // worth alarming anyone over — it's doing what they asked for.
      if (e.status === 409) toast.info('A Google sync is already running — it will finish on its own. Check back in a minute.');
      else toast.error(e.message);
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
  if (status.load_error) {
    return (
      <IntegrationErrorCard title="Connect to Google Workspace" onRetry={() => setVersion((v) => v + 1)} />
    );
  }

  return (
    <div className="card settings-card integration-card">
      <div className="panel-head">
        <h3>Connect to Google Workspace</h3>
        {status.me?.connected && <span className="badge status-won">Connected</span>}
      </div>
      <p className="muted">
        Read-only Contacts, Calendar and Gmail sync. Google is never used to sign in to the CRM — this
        pulls your contacts for import, matches calendar events to People, Leads and Companies, and
        shows mail exchanged with them on their timelines.
      </p>

      {user?.is_admin && (
        <form className="form" onSubmit={saveConfig}>
          <details className="howto">
            <summary>How to set up (one-time, ~5 minutes)</summary>
            <div className="howto-body">
              <ol>
                <li>Go to <strong>console.cloud.google.com</strong> → create (or pick) a project.</li>
                <li>APIs &amp; Services → Library: enable the <strong>People API</strong>, the
                  <strong> Google Calendar API</strong> and the <strong>Gmail API</strong>.</li>
                <li>APIs &amp; Services → OAuth consent screen: External (or Internal for a Workspace
                  domain), fill in the app name, and add the scopes the CRM requests — all read-only:
                  <code> openid</code>, <code>email</code>, <code>…/auth/contacts.readonly</code>,
                  <code> …/auth/calendar.readonly</code> and <code>…/auth/gmail.readonly</code>. Gmail
                  read access is a restricted scope: an External app stays in Testing (add each user as
                  a test user) unless you take it through Google&apos;s verification.</li>
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
              Client secret {status.configured && <span className="muted">(saved — leave blank to keep it)</span>}
            </span>
            <SecretInput value={clientSecret} onChange={setClientSecret} saved={!!status.configured} />
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
                  ) : status.me.gmail_backfill_done ? (
                    <p className="muted">✓ History backfill complete.</p>
                  ) : (
                    // Not done and nothing to measure yet: it hasn't started.
                    <p className="muted">
                      History backfill hasn&apos;t started yet — it begins with the next sync (or hit
                      &ldquo;Sync now&rdquo;).
                    </p>
                  )}
                  <GmailRetryCounts me={status.me} />
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
