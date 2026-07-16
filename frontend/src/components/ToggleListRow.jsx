/**
 * A settings-list row with a leading on/off switch, a name (+ optional badge)
 * over a subtitle, an expandable meta button on the right, and a delete ×.
 * Shared by Automations and Lead-capture forms — both render identical rows.
 */
export default function ToggleListRow({
  enabled,
  onToggle,
  switchTitle,
  title,
  badge,
  subtitle,
  meta,
  metaTitle,
  expanded,
  onToggleExpand,
  onDelete,
  deleteTitle,
  children,
}) {
  return (
    <div className={'toggle-row' + (enabled ? '' : ' toggle-row-off')}>
      <div className="toggle-row-main">
        <label className="toggle-switch" title={switchTitle}>
          <input type="checkbox" checked={enabled} onChange={onToggle} />
          <span className="toggle-switch-track" />
        </label>
        <div className="toggle-row-text">
          <div className="toggle-row-name">
            <strong>{title}</strong>
            {badge}
          </div>
          {subtitle}
        </div>
        <button
          type="button"
          className="btn btn-small toggle-row-meta"
          onClick={onToggleExpand}
          title={metaTitle}
        >
          {meta}
        </button>
        <button
          type="button"
          className="icon-btn tiny"
          onClick={onDelete}
          title={deleteTitle}
          aria-label={deleteTitle}
        >
          ×
        </button>
      </div>
      {expanded && children}
    </div>
  );
}
