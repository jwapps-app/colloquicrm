import { useState } from 'react';

export function Loading({ label = 'Loading…', small }) {
  return (
    <div className={'state' + (small ? ' state-small' : '')}>
      <div className="spinner" />
      <span>{label}</span>
    </div>
  );
}

export function Empty({ label = 'Nothing here yet.', hint }) {
  return (
    <div className="state empty">
      <span>{label}</span>
      {hint && <span className="muted">{hint}</span>}
    </div>
  );
}

/** Masked input for a stored credential, with a deliberate reveal toggle.
 * `saved` = a value is already stored server-side: the field may stay blank
 * (meaning "unchanged") and says so in its placeholder. */
export function SecretInput({ value, onChange, saved = false, placeholder, required, ...rest }) {
  const [shown, setShown] = useState(false);
  return (
    <div className="secret-input">
      <input
        type={shown ? 'text' : 'password'}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={saved ? '•••••• (unchanged)' : placeholder}
        required={required ?? !saved}
        autoComplete="new-password"
        spellCheck={false}
        {...rest}
      />
      <button
        type="button"
        className="btn btn-small"
        onClick={() => setShown((v) => !v)}
        aria-pressed={shown}
        aria-label={shown ? 'Hide value' : 'Show value'}
      >
        {shown ? 'Hide' : 'Show'}
      </button>
    </div>
  );
}

/** Body for a config save: blank secrets are left out entirely, so the
 * server keeps what it has stored instead of being handed "". */
export function withoutBlankSecrets(body, secretKeys) {
  const out = { ...body };
  secretKeys.forEach((k) => {
    if (typeof out[k] !== 'string' || out[k].trim() === '') delete out[k];
  });
  return out;
}
