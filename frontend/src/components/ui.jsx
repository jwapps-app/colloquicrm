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
