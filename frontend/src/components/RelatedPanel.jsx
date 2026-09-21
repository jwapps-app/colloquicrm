import { Link } from 'react-router';
import { Loading } from './ui';

/** `total` is the server's full count; when it exceeds the rows on hand the
 * panel says so instead of passing a partial list off as complete.
 * `viewAllTo` links to the list page filtered to the same records. */
export default function RelatedPanel({ title, items, total, viewAllTo, empty = 'None yet.', renderItem, action }) {
  const partial = Array.isArray(items) && Number(total) > items.length;
  return (
    <div className="card">
      <div className="panel-head">
        <h4 className="panel-title">{title}</h4>
        {action}
      </div>
      {items === null ? (
        <Loading small label="Loading…" />
      ) : items === 'error' ? (
        <div className="muted panel-empty">Couldn&apos;t load these — reload to try again.</div>
      ) : items.length === 0 ? (
        <div className="muted panel-empty">{empty}</div>
      ) : (
        <>
          <div className="related-list">{items.map(renderItem)}</div>
          {partial && (
            <div className="muted panel-more">
              Showing {items.length} of {total}
              {viewAllTo && (
                <>
                  {' — '}
                  <Link to={viewAllTo}>View all</Link>
                </>
              )}
            </div>
          )}
        </>
      )}
    </div>
  );
}
