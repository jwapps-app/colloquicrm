import { useEffect, useState } from 'react';
import { get } from '../api';
import { fmtDate, fmtDateTime } from '../format';

/** Google Calendar events matched to this record by attendee email.
 * Renders nothing at all unless the Google integration has events to show. */
export default function CalendarPanel({ entityType, entityId }) {
  const [items, setItems] = useState(null);

  useEffect(() => {
    let on = true;
    get('/calendar-events', { entity_type: entityType, entity_id: entityId })
      .then((d) => on && setItems(d.items || []))
      .catch(() => on && setItems([]));
    return () => {
      on = false;
    };
  }, [entityType, entityId]);

  if (!items || items.length === 0) return null;

  return (
    <div className="card">
      <div className="panel-head">
        <h4 className="panel-title">Calendar</h4>
      </div>
      <div className="related-list">
        {items.map((e) => (
          <div key={e.id} className="related-item cal-event">
            <div>
              <strong>{e.summary || '(no title)'}</strong>
              {e.html_link && (
                <a href={e.html_link} target="_blank" rel="noreferrer" className="cal-link" title="Open in Google Calendar">
                  ↗
                </a>
              )}
            </div>
            <div className="muted">
              {e.all_day ? fmtDate(e.starts_at) : fmtDateTime(e.starts_at)}
              {e.location ? ` · ${e.location}` : ''}
              {e.attendees?.length ? ` · ${e.attendees.length} attendees` : ''}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
