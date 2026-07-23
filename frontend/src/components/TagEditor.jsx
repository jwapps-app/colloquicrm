import { useEffect, useId, useState } from 'react';
import { cachedGet } from '../api';

export default function TagEditor({ tags = [], onChange }) {
  const [input, setInput] = useState('');
  const [known, setKnown] = useState([]);
  const listId = useId();

  // Existing org tags feed the input's native type-ahead. Org-wide on
  // purpose — reusing a tag from another record type is the common case.
  useEffect(() => {
    let on = true;
    cachedGet('/tags')
      .then((d) => {
        if (on) setKnown(Array.isArray(d) ? d : []);
      })
      .catch(() => {});
    return () => {
      on = false;
    };
  }, []);

  function add() {
    const t = input.trim();
    setInput('');
    if (!t || tags.includes(t)) return;
    onChange([...tags, t]);
  }

  return (
    <div className="tag-editor">
      {tags.map((t) => (
        <span key={t} className="chip">
          {t}
          <button type="button" className="chip-x" onClick={() => onChange(tags.filter((x) => x !== t))} aria-label={`Remove ${t}`}>
            ×
          </button>
        </span>
      ))}
      <input
        value={input}
        list={listId}
        placeholder="Add tag…"
        onChange={(e) => setInput(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === 'Enter') {
            e.preventDefault();
            add();
          }
        }}
        onBlur={add}
      />
      <datalist id={listId}>
        {known
          .filter((t) => !tags.includes(t.name))
          .map((t) => (
            <option key={t.id} value={t.name} />
          ))}
      </datalist>
    </div>
  );
}
