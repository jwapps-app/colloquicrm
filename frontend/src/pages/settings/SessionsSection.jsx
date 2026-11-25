import { del, get } from '../../api';
import { useLoad } from '../../hooks';
import { useToast } from '../../components/Toast';
import { fmtDateTime } from '../../format';

/* ---------- Sessions ---------- */

export default function SessionsSection() {
  const toast = useToast();
  const { data: sessions, error, reload } = useLoad(() =>
    get('/auth/sessions')
      .then((d) => (Array.isArray(d) ? d : d?.items || []))
      .catch((e) => {
        // Only a backend without the endpoint (404) hides the card; any other
        // failure is shown — "couldn't load" is not "no sessions".
        if (e.status === 404) return [];
        throw e;
      })
  );

  async function revoke(s) {
    if (!window.confirm('Sign out that session? Whatever device it is will need to log in again.')) return;
    try {
      await del(`/auth/sessions/${s.id}`);
      toast.success('Session signed out');
      reload();
    } catch (e) {
      toast.error(e.message);
    }
  }

  if (error) {
    return (
      <div className="card settings-card">
        <h3>Active sessions</h3>
        <div className="muted panel-empty">
          Couldn&apos;t load your sessions.{' '}
          <button className="linklike" onClick={reload}>
            Retry
          </button>
        </div>
      </div>
    );
  }
  if (!sessions || sessions.length === 0) return null;

  return (
    <div className="card settings-card">
      <h3>Active sessions</h3>
      <p className="muted">Where this account is currently signed in.</p>
      <div className="cf-list">
        {sessions.map((s) => (
          <div key={s.id} className="cf-row">
            <div className="cf-name">
              {s.current ? 'This session' : 'Session'}
              {s.created_at && <span className="muted"> — signed in {fmtDateTime(s.created_at)}</span>}
            </div>
            {/* last_seen_at may be absent on an older backend — show nothing then */}
            {s.last_seen_at && <span className="muted">Last active {fmtDateTime(s.last_seen_at)}</span>}
            {/* The current session ends via Sign out, not here. */}
            {!s.current && (
              <button className="btn btn-small btn-danger-ghost" onClick={() => revoke(s)}>
                Revoke
              </button>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}
