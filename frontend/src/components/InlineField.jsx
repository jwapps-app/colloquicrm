import { useEffect, useRef, useState } from 'react';

/**
 * Click-to-edit field. Shows value (via optional render); click swaps in an
 * input; saves on blur/Enter (or immediately for selects); Escape cancels.
 */
export default function InlineField({ value, type = 'text', options, onSave, placeholder = '—', render }) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState('');
  const ref = useRef(null);

  useEffect(() => {
    if (editing && ref.current) ref.current.focus();
  }, [editing]);

  function start() {
    let v = value ?? '';
    if (type === 'date' && typeof v === 'string') v = v.slice(0, 10);
    setDraft(v);
    setEditing(true);
  }

  function commit(explicit) {
    setEditing(false);
    const raw = explicit !== undefined ? explicit : draft;
    let norm;
    if (raw === '' || raw === null || raw === undefined) norm = null;
    else if (type === 'number') {
      norm = Number(raw);
      if (Number.isNaN(norm)) return;
    } else norm = raw;
    const current = value === '' || value === undefined ? null : value;
    if (norm === current) return;
    onSave(norm);
  }

  if (!editing) {
    const display = render ? render(value) : value;
    const isEmpty = display === '' || display === null || display === undefined;
    return (
      <button type="button" className="inline-value" onClick={start}>
        {isEmpty ? <span className="muted">{placeholder}</span> : display}
      </button>
    );
  }

  if (type === 'select') {
    return (
      <select
        ref={ref}
        className="inline-input"
        value={draft}
        onChange={(e) => commit(e.target.value)}
        onBlur={() => setEditing(false)}
      >
        <option value="">—</option>
        {(options || []).map((o) => (
          <option key={o.value} value={o.value}>
            {o.label}
          </option>
        ))}
      </select>
    );
  }

  if (type === 'textarea') {
    return (
      <textarea
        ref={ref}
        className="inline-input"
        rows={3}
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        onBlur={() => commit()}
        onKeyDown={(e) => {
          if (e.key === 'Escape') setEditing(false);
        }}
      />
    );
  }

  return (
    <input
      ref={ref}
      className="inline-input"
      type={type}
      step={type === 'number' ? 'any' : undefined}
      value={draft}
      onChange={(e) => setDraft(e.target.value)}
      onBlur={() => commit()}
      onKeyDown={(e) => {
        if (e.key === 'Enter') e.currentTarget.blur();
        if (e.key === 'Escape') setEditing(false);
      }}
    />
  );
}
