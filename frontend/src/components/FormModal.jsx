import { useState } from 'react';
import Modal from './Modal';

/**
 * Generic small form in a modal.
 * fields: [{ key, label, type: 'text'|'email'|'password'|'number'|'date'|'select'|'textarea'|'checkbox',
 *            options: [{value,label}], required, default, show?: (values) => bool }]
 * onSubmit(values) may throw — the message is shown inline.
 */
export default function FormModal({ title, fields, initial = {}, submitLabel = 'Save', onSubmit, onClose }) {
  const [values, setValues] = useState(() => {
    const v = {};
    fields.forEach((f) => {
      if (initial[f.key] !== undefined) v[f.key] = initial[f.key];
      else if (f.default !== undefined) v[f.key] = f.default;
      else v[f.key] = f.type === 'checkbox' ? false : '';
    });
    return v;
  });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  const set = (key, val) => setValues((v) => ({ ...v, [key]: val }));

  async function submit(e) {
    e.preventDefault();
    setBusy(true);
    setError('');
    try {
      await onSubmit(values);
    } catch (err) {
      setError(err.message);
      setBusy(false);
    }
  }

  return (
    <Modal title={title} onClose={onClose}>
      <form className="form" onSubmit={submit}>
        {fields
          .filter((f) => !f.show || f.show(values))
          .map((f) => (
            <label key={f.key} className={'field' + (f.type === 'checkbox' ? ' field-inline' : '')}>
              <span>
                {f.label}
                {f.required ? ' *' : ''}
              </span>
              {f.type === 'select' ? (
                <select value={values[f.key]} onChange={(e) => set(f.key, e.target.value)} required={f.required}>
                  <option value="">{f.required ? 'Select…' : '—'}</option>
                  {(f.options || []).map((o) => (
                    <option key={o.value} value={o.value}>
                      {o.label}
                    </option>
                  ))}
                </select>
              ) : f.type === 'textarea' ? (
                <textarea rows={3} value={values[f.key]} onChange={(e) => set(f.key, e.target.value)} />
              ) : f.type === 'checkbox' ? (
                <input type="checkbox" checked={!!values[f.key]} onChange={(e) => set(f.key, e.target.checked)} />
              ) : (
                <input
                  type={f.type || 'text'}
                  value={values[f.key]}
                  onChange={(e) => set(f.key, e.target.value)}
                  required={f.required}
                  step={f.type === 'number' ? 'any' : undefined}
                  placeholder={f.placeholder}
                />
              )}
            </label>
          ))}
        {error && <div className="form-error">{error}</div>}
        <div className="form-actions">
          <button type="button" className="btn" onClick={onClose}>
            Cancel
          </button>
          <button type="submit" className="btn btn-primary" disabled={busy}>
            {busy ? 'Saving…' : submitLabel}
          </button>
        </div>
      </form>
    </Modal>
  );
}
