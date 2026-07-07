import { Loading } from './ui';

export default function RelatedPanel({ title, items, empty = 'None yet.', renderItem, action }) {
  return (
    <div className="card">
      <div className="panel-head">
        <h4 className="panel-title">{title}</h4>
        {action}
      </div>
      {items === null ? (
        <Loading small label="Loading…" />
      ) : items.length === 0 ? (
        <div className="muted panel-empty">{empty}</div>
      ) : (
        <div className="related-list">{items.map(renderItem)}</div>
      )}
    </div>
  );
}
