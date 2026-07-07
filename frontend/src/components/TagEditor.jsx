import { useState } from 'react';

export default function TagEditor({ tags = [], onChange }) {
  const [input, setInput] = useState('');

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
    </div>
  );
}
