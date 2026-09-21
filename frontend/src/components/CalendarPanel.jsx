import { useEffect, useState } from 'react';
import { get } from '../api';
import { fmtAllDayDate, fmtDateTime, safeHref } from '../format';

/** Google Calendar events matched to this record by attendee email.
 * Renders nothing when there are no events to show — but a failed fetch is
 * not "no events": it gets an inline error with Retry. */
export default function CalendarPanel({ entityType, entityId }) {
  // null = loading, 'error' = fetch failed, else the events.
  const [items, setItems] = useState(null);
  const [version, setVersion] = useState(0);

  useEffect(() => {
    let on = true;
    setItems(null);
    get('/calendar-events', { entity_type: entityType, entity_id: entityId })
      .then((d) => on && setItems(d?.items || []))
      .catch(() => on && setItems('error'));
    return () => {
      on = false;
    };
  }, [entityType, entityId, version]);

  if (!items || items.length === 0) return null;

  return (
    <div className="card">
      <div className="panel-head">
        <h4 className="panel-title">Calendar</h4>
      </div>
      {items === 'error' ? (
        <div className="muted panel-empty">
          Couldn&apos;t load calendar events.{' '}
          <button className="linklike" onClick={() => setVersion((v) => v + 1)}>
            Retry
          </button>
        </div>
      ) : (
        <div className="related-list">
          {items.map((e) => (
            <div key={e.id} className="related-item cal-event">
              <div>
                <strong>{e.summary || '(no title)'}</strong>
                {e.html_link && (
                  <a href={safeHref(e.html_link)} target="_blank" rel="noreferrer" className="cal-link" title="Open in Google Calendar">
                    ↗
                  </a>
                )}
              </div>
              <div className="muted">
                {/* All-day events are a calendar date, not an instant. */}
                {e.all_day ? fmtAllDayDate(e.starts_at) : fmtDateTime(e.starts_at)}
                {e.location ? ` · ${e.location}` : ''}
                {e.attendees?.length ? ` · ${e.attendees.length} attendees` : ''}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
