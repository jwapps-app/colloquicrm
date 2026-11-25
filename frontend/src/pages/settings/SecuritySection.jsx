import { useState } from 'react';
import { post } from '../../api';
import { useAuth } from '../../auth';
import { useToast } from '../../components/Toast';

/* ---------- Security (TOTP) ---------- */

export default function SecuritySection() {
  const { user, setUser } = useAuth();
  const toast = useToast();
  const [setup, setSetup] = useState(null); // {secret, otpauth_url}
  const [qr, setQr] = useState(null);
  const [code, setCode] = useState('');
  const [busy, setBusy] = useState(false);
  // Wrong authenticator code (a 401 that keeps the session) — shown by the input.
  const [codeError, setCodeError] = useState('');

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
    setCodeError('');
    try {
      await post('/auth/totp/enable', { code: code.trim() });
      setUser({ ...user, totp_enabled: true });
      setSetup(null);
      setCode('');
      toast.success('Two-factor authentication enabled');
    } catch (err) {
      setCodeError(err.message);
    }
    setBusy(false);
  }

  async function disable(e) {
    e.preventDefault();
    setBusy(true);
    setCodeError('');
    try {
      await post('/auth/totp/disable', { code: code.trim() });
      setUser({ ...user, totp_enabled: false });
      setCode('');
      toast.success('Two-factor authentication disabled');
    } catch (err) {
      setCodeError(err.message);
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
          {codeError && (
            <div className="form-error" role="alert">
              {codeError}
            </div>
          )}
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
          {codeError && (
            <div className="form-error" role="alert">
              {codeError}
            </div>
          )}
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
