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

export function fmtDate(iso) {
  if (!iso) return '—';
  const d = new Date(iso.length === 10 ? iso + 'T00:00:00' : iso);
  return Number.isNaN(d.getTime()) ? String(iso) : d.toLocaleDateString();
}

export function fmtDateTime(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? String(iso) : d.toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' });
}

export function timeOfDay(iso) {
  if (!iso) return '';
  return new Date(iso).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
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
