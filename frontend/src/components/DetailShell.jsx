import { Link } from 'react-router-dom';
import NotesTimeline from './NotesTimeline';

/**
 * Copper-style 3-column detail layout:
 * left = profile / tags / custom fields, center = notes + activity, right = related panels.
 */
export default function DetailShell({
  backTo,
  backLabel,
  title,
  subtitle,
  actions,
  onDelete,
  banner,
  entityType,
  entityId,
  left,
  right,
}) {
  return (
    <div className="page">
      <div className="detail-header">
        <div className="detail-title">
          <Link to={backTo} className="back-link">
            ← {backLabel}
          </Link>
          <h1>{title}</h1>
          {subtitle && <div className="muted subtitle">{subtitle}</div>}
        </div>
        <div className="detail-actions">
          {actions}
          {onDelete && (
            <button className="btn btn-danger-ghost" onClick={onDelete}>
              Delete
            </button>
          )}
        </div>
      </div>
      {banner}
      <div className="detail-grid">
        <div className="detail-col detail-col-left">{left}</div>
        <div className="detail-col detail-col-center">
          <NotesTimeline entityType={entityType} entityId={entityId} />
        </div>
        <div className="detail-col detail-col-right">{right}</div>
      </div>
    </div>
  );
}
