export function money(value, currency) {
  if (value === null || value === undefined || value === '') return '—';
  const n = Number(value);
  if (Number.isNaN(n)) return String(value);
  try {
    return new Intl.NumberFormat('en-US', { style: 'currency', currency: currency || 'USD' }).format(n);
  } catch {
    return new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' }).format(n);
  }
}

/** Parse a server timestamp. Naive datetimes (no zone suffix) are UTC —
 * SQLite dev returns them that way; Postgres sends +00:00. Date-only
 * strings are calendar dates and stay local. */
export function parseWhen(iso) {
  if (!iso) return null;
  if (typeof iso === 'string') {
    if (/^\d{4}-\d{2}-\d{2}$/.test(iso)) return new Date(iso + 'T00:00:00');
    if (!/(?:[zZ]|[+-]\d{2}:?\d{2})$/.test(iso)) return new Date(iso + 'Z');
  }
  return new Date(iso);
}

export function fmtDate(iso) {
  if (!iso) return '—';
  const d = parseWhen(iso);
  return Number.isNaN(d.getTime()) ? String(iso) : d.toLocaleDateString();
}

export function fmtDateTime(iso) {
  if (!iso) return '—';
  const d = parseWhen(iso);
  return Number.isNaN(d.getTime()) ? String(iso) : d.toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' });
}

export function timeOfDay(iso) {
  if (!iso) return '';
  return parseWhen(iso).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
}

export function fullName(o) {
  if (!o) return '';
  return [o.prefix, o.first_name, o.middle_name, o.last_name, o.suffix].filter(Boolean).join(' ') || '(no name)';
}

export function humanize(s) {
  if (!s) return '';
  const str = String(s).replace(/[_-]+/g, ' ').trim();
  return str.charAt(0).toUpperCase() + str.slice(1);
}

export const ENTITY_ROUTES = {
  person: 'people',
  lead: 'leads',
  company: 'companies',
  opportunity: 'opportunities',
};

export function entityPath(entityType, entityId) {
  const route = ENTITY_ROUTES[entityType];
  return route && entityId ? `/${route}/${entityId}` : null;
}
