import { useState } from 'react';
import { Navigate, useNavigate } from 'react-router-dom';
import { post } from '../api';
import { useAuth } from '../auth';

export default function Login() {
  const { appName, login, user } = useAuth();
  const nav = useNavigate();
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [pendingToken, setPendingToken] = useState(null);
  const [code, setCode] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [signoutReason] = useState(() => {
    try {
      return sessionStorage.getItem('crm_signout_reason');
    } catch {
      return null;
    }
  });

  if (user) return <Navigate to="/" replace />;

  async function submit(e) {
    e.preventDefault();
    setBusy(true);
    setError('');
    try {
      const res = await post('/auth/login', { email, password });
      if (res.totp_required) {
        setPendingToken(res.pending_token);
      } else {
        try {
          sessionStorage.removeItem('crm_signout_reason');
        } catch {
          // best-effort
        }
        login(res.token, res.user);
        nav('/', { replace: true });
      }
    } catch (err) {
      setError(err.message);
    }
    setBusy(false);
  }

  async function submitCode(e) {
    e.preventDefault();
    setBusy(true);
    setError('');
    try {
      const res = await post('/auth/totp', { pending_token: pendingToken, code: code.trim() });
      try {
        sessionStorage.removeItem('crm_signout_reason');
      } catch {
        // best-effort
      }
      login(res.token, res.user);
      nav('/', { replace: true });
    } catch (err) {
      setError(err.message);
    }
    setBusy(false);
  }

  return (
    <div className="auth-page">
      <div className="auth-card card">
        <div className="auth-brand">
          <span className="brand-dot" />
          <h1>{appName}</h1>
        </div>
        {!pendingToken ? (
          <form className="form" onSubmit={submit}>
            <h2>Sign in</h2>
            {signoutReason && (
              <p className="muted">Your session ended unexpectedly ({signoutReason}).</p>
            )}
            <label className="field">
              <span>Email</span>
              <input type="email" value={email} onChange={(e) => setEmail(e.target.value)} required autoFocus autoComplete="username" />
            </label>
            <label className="field">
              <span>Password</span>
              <input type="password" value={password} onChange={(e) => setPassword(e.target.value)} required autoComplete="current-password" />
            </label>
            {error && <div className="form-error">{error}</div>}
            <button type="submit" className="btn btn-primary btn-block" disabled={busy}>
              {busy ? 'Signing in…' : 'Sign in'}
            </button>
          </form>
        ) : (
          <form className="form" onSubmit={submitCode}>
            <h2>Two-factor code</h2>
            <p className="muted">Enter the 6-digit code from your authenticator app.</p>
            <label className="field">
              <span>Code</span>
              <input
                className="totp-input"
                inputMode="numeric"
                pattern="[0-9]*"
                value={code}
                onChange={(e) => setCode(e.target.value.replace(/\D/g, '').slice(0, 6))}
                required
                autoFocus
                autoComplete="one-time-code"
              />
            </label>
            {error && <div className="form-error">{error}</div>}
            <button type="submit" className="btn btn-primary btn-block" disabled={busy || code.length !== 6}>
              {busy ? 'Verifying…' : 'Verify'}
            </button>
            <button
              type="button"
              className="btn btn-ghost btn-block"
              onClick={() => {
                setPendingToken(null);
                setCode('');
                setError('');
              }}
            >
              ← Back to sign in
            </button>
          </form>
        )}
      </div>
    </div>
  );
}
