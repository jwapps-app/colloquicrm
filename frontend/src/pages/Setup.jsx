import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { post } from '../api';
import { useAuth } from '../auth';

export default function Setup() {
  const { appName, finishSetup } = useAuth();
  const nav = useNavigate();
  const [displayName, setDisplayName] = useState('');
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [confirm, setConfirm] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);

  async function submit(e) {
    e.preventDefault();
    if (password !== confirm) {
      setError('Passwords do not match.');
      return;
    }
    setBusy(true);
    setError('');
    try {
      const res = await post('/auth/setup', { email, password, display_name: displayName });
      finishSetup(res.token, res.user);
      nav('/', { replace: true });
    } catch (err) {
      setError(err.message);
      setBusy(false);
    }
  }

  return (
    <div className="auth-page">
      <div className="auth-card card">
        <div className="auth-brand">
          <span className="brand-dot" />
          <h1>{appName}</h1>
        </div>
        <form className="form" onSubmit={submit}>
          <h2>Welcome — let’s set up your admin account</h2>
          <p className="muted">This first account will be the administrator.</p>
          <label className="field">
            <span>Your name</span>
            <input value={displayName} onChange={(e) => setDisplayName(e.target.value)} required autoFocus />
          </label>
          <label className="field">
            <span>Email</span>
            <input type="email" value={email} onChange={(e) => setEmail(e.target.value)} required autoComplete="username" />
          </label>
          <label className="field">
            <span>Password</span>
            <input type="password" value={password} onChange={(e) => setPassword(e.target.value)} required minLength={8} autoComplete="new-password" />
          </label>
          <label className="field">
            <span>Confirm password</span>
            <input type="password" value={confirm} onChange={(e) => setConfirm(e.target.value)} required autoComplete="new-password" />
          </label>
          {error && <div className="form-error">{error}</div>}
          <button type="submit" className="btn btn-primary btn-block" disabled={busy}>
            {busy ? 'Creating…' : 'Create account'}
          </button>
        </form>
      </div>
    </div>
  );
}
