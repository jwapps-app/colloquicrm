import { useEffect, useState } from 'react';
import { del, get, patch, post } from '../api';
import { useToast } from './Toast';
import Modal from './Modal';
import { Loading } from './ui';

// Mirrors the backend field catalog (routes/forms.py). Order is render order.
const FORM_FIELDS = [
  { key: 'first_name', label: 'First name', always: true, max: 120 },
  { key: 'last_name', label: 'Last name', always: true, max: 120 },
  { key: 'email', label: 'Email', type: 'email', max: 255 },
  { key: 'work_phone', label: 'Phone', type: 'tel', max: 60 },
  { key: 'company_name', label: 'Company', max: 255 },
  { key: 'title', label: 'Title', max: 255 },
  { key: 'details', label: 'Message', textarea: true, max: 10000 },
];
const DEFAULT_SUCCESS = "Thanks — we'll be in touch shortly.";

function slugPreview(name) {
  const base = (name || '')
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 40)
    .replace(/-+$/, '');
  return (base || 'form') + '-xxxxxx';
}

/** Plain-HTML form for pasting into an external site — posts straight to the
 * public endpoint, honeypot included, no scripts needed. */
function embedSnippet(form) {
  const lines = [`<form action="${form.public_url}" method="post">`];
  for (const f of FORM_FIELDS) {
    if (!(form.fields || []).includes(f.key)) continue;
    const required =
      f.key === 'first_name' || (f.key === 'email' && form.require_email) ? ' required' : '';
    if (f.textarea) {
      lines.push(`  <label>${f.label}<br><textarea name="${f.key}" rows="4" maxlength="${f.max}"${required}></textarea></label><br>`);
    } else {
      lines.push(`  <label>${f.label}<br><input type="${f.type || 'text'}" name="${f.key}" maxlength="${f.max}"${required}></label><br>`);
    }
  }
  lines.push('  <!-- spam trap: keep this field hidden and empty -->');
  lines.push('  <input type="text" name="website" value="" style="position:absolute;left:-9999px" tabindex="-1" aria-hidden="true">');
  lines.push('  <button type="submit">Send</button>');
  lines.push('</form>');
  return lines.join('\n');
}

function FieldChecks({ fields, requireEmail, onChange }) {
  return (
    <div className="auto-status-checks">
      {FORM_FIELDS.map((f) => {
        const forced = f.always || (f.key === 'email' && requireEmail);
        const checked = forced || fields.includes(f.key);
        return (
          <label key={f.key} className="cf-checkbox">
            <input
              type="checkbox"
              checked={checked}
              disabled={forced}
              onChange={(e) =>
                onChange(
                  e.target.checked ? [...fields, f.key] : fields.filter((k) => k !== f.key)
                )
              }
            />
            <span>{f.label}</span>
          </label>
        );
      })}
    </div>
  );
}

function NewFormModal({ onClose, onCreated }) {
  const [name, setName] = useState('');
  const [fields, setFields] = useState(['email', 'company_name', 'details']);
  const [requireEmail, setRequireEmail] = useState(true);
  const [source, setSource] = useState('');
  const [successMessage, setSuccessMessage] = useState(DEFAULT_SUCCESS);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  async function submit(e) {
    e.preventDefault();
    setBusy(true);
    setError('');
    try {
      await post('/forms', {
        name,
        fields,
        require_email: requireEmail,
        source: source.trim() || undefined,
        success_message: successMessage,
      });
      onCreated();
    } catch (err) {
      setError(err.message);
      setBusy(false);
    }
  }

  return (
    <Modal title="New form" onClose={onClose}>
      <form className="form" onSubmit={submit}>
        <label className="field">
          <span>Form name</span>
          <input value={name} onChange={(e) => setName(e.target.value)} required autoFocus />
        </label>
        {name.trim() && (
          <p className="muted leadform-slug-preview">
            Public URL will look like <code>/f/{slugPreview(name)}</code> (a random suffix is
            added; the URL never changes once created).
          </p>
        )}
        <div className="field">
          <span>Fields to show <span className="muted">(name fields are always included)</span></span>
          <FieldChecks fields={fields} requireEmail={requireEmail} onChange={setFields} />
        </div>
        <label className="cf-checkbox settings-check">
          <input
            type="checkbox"
            checked={requireEmail}
            onChange={(e) => setRequireEmail(e.target.checked)}
          />
          <span>Require an email address</span>
        </label>
        <label className="field">
          <span>Lead source <span className="muted">— stamped onto every lead this form creates</span></span>
          <input
            value={source}
            onChange={(e) => setSource(e.target.value)}
            placeholder={name.trim() || 'Defaults to the form name'}
          />
        </label>
        <label className="field">
          <span>Success message</span>
          <textarea
            rows={2}
            value={successMessage}
            onChange={(e) => setSuccessMessage(e.target.value)}
          />
        </label>
        {error && <div className="form-error">{error}</div>}
        <div className="form-actions">
          <button type="button" className="btn" onClick={onClose}>Cancel</button>
          <button type="submit" className="btn btn-primary" disabled={busy || !name.trim()}>
            {busy ? 'Creating…' : 'Create form'}
          </button>
        </div>
      </form>
    </Modal>
  );
}

function FormEditor({ form, onSaved }) {
  const toast = useToast();
  const [fields, setFields] = useState(form.fields || []);
  const [requireEmail, setRequireEmail] = useState(form.require_email);
  const [source, setSource] = useState(form.source || '');
  const [successMessage, setSuccessMessage] = useState(form.success_message || DEFAULT_SUCCESS);
  const [busy, setBusy] = useState(false);

  async function save(e) {
    e.preventDefault();
    setBusy(true);
    try {
      const updated = await patch(`/forms/${form.id}`, {
        fields,
        require_email: requireEmail,
        source,
        success_message: successMessage,
      });
      toast.success('Form updated');
      onSaved(updated);
    } catch (err) {
      toast.error(err.message);
    }
    setBusy(false);
  }

  function copySnippet() {
    navigator.clipboard
      .writeText(embedSnippet({ ...form, fields, require_email: requireEmail }))
      .then(() => toast.success('Embed snippet copied'))
      .catch(() => toast.error('Could not copy to clipboard'));
  }

  return (
    <form className="form leadform-editor" onSubmit={save}>
      <div className="field">
        <span>Fields</span>
        <FieldChecks fields={fields} requireEmail={requireEmail} onChange={setFields} />
      </div>
      <label className="cf-checkbox settings-check">
        <input
          type="checkbox"
          checked={requireEmail}
          onChange={(e) => setRequireEmail(e.target.checked)}
        />
        <span>Require an email address</span>
      </label>
      <label className="field">
        <span>Lead source</span>
        <input value={source} onChange={(e) => setSource(e.target.value)} />
      </label>
      <label className="field">
        <span>Success message</span>
        <textarea
          rows={2}
          value={successMessage}
          onChange={(e) => setSuccessMessage(e.target.value)}
        />
      </label>
      <div className="field">
        <span>
          Embed on an external site <span className="muted">— plain HTML, posts straight to this form</span>
        </span>
        <pre className="leadform-embed">{embedSnippet({ ...form, fields, require_email: requireEmail })}</pre>
        <div className="form-actions leadform-embed-actions">
          <button type="button" className="btn btn-small" onClick={copySnippet}>
            Copy snippet
          </button>
          <button type="submit" className="btn btn-primary btn-small" disabled={busy}>
            {busy ? 'Saving…' : 'Save changes'}
          </button>
        </div>
      </div>
    </form>
  );
}

export default function FormsSection() {
  const toast = useToast();
  const [forms, setForms] = useState(null);
  const [showNew, setShowNew] = useState(false);
  const [expanded, setExpanded] = useState(null);
  const [version, setVersion] = useState(0);

  useEffect(() => {
    let on = true;
    get('/forms')
      .then((d) => on && setForms(d.items || []))
      .catch((e) => {
        toast.error(e.message);
        if (on) setForms([]);
      });
    return () => { on = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [version]);

  async function toggle(form) {
    try {
      await patch(`/forms/${form.id}`, { enabled: !form.enabled });
      setForms((fs) => fs.map((f) => (f.id === form.id ? { ...f, enabled: !f.enabled } : f)));
    } catch (e) {
      toast.error(e.message);
    }
  }

  async function remove(form) {
    if (!window.confirm(`Delete form "${form.name}"? Its public URL stops working immediately. Leads it already created are kept.`)) return;
    try {
      await del(`/forms/${form.id}`);
      setForms((fs) => fs.filter((f) => f.id !== form.id));
      toast.success('Form deleted');
    } catch (e) {
      toast.error(e.message);
    }
  }

  function copyUrl(form) {
    navigator.clipboard
      .writeText(form.public_url)
      .then(() => toast.success('Public URL copied'))
      .catch(() => toast.error('Could not copy to clipboard'));
  }

  return (
    <div className="card settings-card">
      <div className="panel-head">
        <h3>Lead capture forms</h3>
        <button className="btn btn-primary btn-small" onClick={() => setShowNew(true)}>
          + New form
        </button>
      </div>
      <p className="muted">
        Public pages this CRM hosts — share the link (or paste the embed snippet into your site)
        and every submission becomes a Lead with the form's source. Spam is filtered by a hidden
        trap field and a per-visitor rate limit. If a submission's email matches an open lead,
        no duplicate is created — the message is appended to that lead's details instead.
      </p>
      {forms === null ? (
        <Loading small />
      ) : forms.length === 0 ? (
        <div className="muted panel-empty">No forms yet — create one and share its link.</div>
      ) : (
        <div className="auto-rules">
          {forms.map((f) => (
            <div key={f.id} className={'auto-rule' + (f.enabled ? '' : ' auto-rule-off')}>
              <div className="auto-rule-main">
                <label
                  className="auto-switch"
                  title={f.enabled ? 'Live — click to disable' : 'Disabled — click to enable'}
                >
                  <input type="checkbox" checked={f.enabled} onChange={() => toggle(f)} />
                  <span className="auto-switch-track" />
                </label>
                <div className="auto-rule-text">
                  <div className="auto-rule-name">
                    <strong>{f.name}</strong>
                    {!f.enabled && <span className="badge badge-muted">Disabled</span>}
                  </div>
                  <div className="secret-row leadform-url">
                    <code>{f.public_url}</code>
                    <button type="button" className="btn btn-small" onClick={() => copyUrl(f)}>
                      Copy
                    </button>
                  </div>
                </div>
                <button
                  className="btn btn-small auto-fire-count"
                  onClick={() => setExpanded(expanded === f.id ? null : f.id)}
                  title="Edit form"
                >
                  {f.submission_count} submission{f.submission_count === 1 ? '' : 's'}
                  {expanded === f.id ? ' ▴' : ' ▾'}
                </button>
                <button className="icon-btn tiny" onClick={() => remove(f)} title="Delete form">
                  ×
                </button>
              </div>
              {expanded === f.id && (
                <FormEditor
                  key={f.updated_at}
                  form={f}
                  onSaved={(updated) =>
                    setForms((fs) => fs.map((x) => (x.id === updated.id ? updated : x)))
                  }
                />
              )}
            </div>
          ))}
        </div>
      )}
      {showNew && (
        <NewFormModal
          onClose={() => setShowNew(false)}
          onCreated={() => {
            setShowNew(false);
            setVersion((x) => x + 1);
            toast.success('Form created — copy its public URL below');
          }}
        />
      )}
    </div>
  );
}
