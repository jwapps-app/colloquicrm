/** Guard a server- or user-supplied URL before using it as an <a href>.
 * Returns the trimmed URL only when it uses the http/https scheme, so a
 * `javascript:` or `data:` URL can never execute in our origin. Returns
 * undefined otherwise, which leaves the anchor inert (no navigable href). */
export function safeHref(url) {
  if (typeof url !== 'string') return undefined;
  const trimmed = url.trim();
  return /^https?:\/\//i.test(trimmed) ? trimmed : undefined;
}

export function money(value, currency, { maxFractionDigits } = {}) {
  if (value === null || value === undefined || value === '') return '—';
  const n = Number(value);
  if (Number.isNaN(n)) return String(value);
  const opts = { style: 'currency', currency: currency || 'USD' };
  if (maxFractionDigits !== undefined) opts.maximumFractionDigits = maxFractionDigits;
  try {
    return new Intl.NumberFormat('en-US', opts).format(n);
  } catch {
    return new Intl.NumberFormat('en-US', { ...opts, currency: 'USD' }).format(n);
  }
}

/** Whole-dollar money — no cents. Used for report totals. */
export const money0 = (value) => money(value, 'USD', { maxFractionDigits: 0 });

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

// Per-device display preference: 'first-last' (John A. Doe) or 'last-first'
// (Doe, John A.). Read fresh each call so a change applies everywhere.
export function nameFormat() {
  try {
    return localStorage.getItem('crm_name_format') === 'last-first' ? 'last-first' : 'first-last';
  } catch {
    return 'first-last';
  }
}

export function fullName(o) {
  if (!o) return '';
  const { prefix, first_name, middle_name, last_name, suffix } = o;
  if (nameFormat() === 'last-first' && last_name && (first_name || middle_name)) {
    const lastPart = [last_name, suffix].filter(Boolean).join(' ');
    const firstPart = [first_name, middle_name].filter(Boolean).join(' ');
    return `${lastPart}, ${firstPart}`;
  }
  return [prefix, first_name, middle_name, last_name, suffix].filter(Boolean).join(' ') || '(no name)';
}

/** File size for display: bytes under 1 KB, else KB/MB with one decimal
 * where it matters (1.5 KB, 12 KB, 3.2 MB). */
export function humanSize(bytes) {
  const n = Number(bytes);
  if (!Number.isFinite(n) || n < 0) return '';
  if (n < 1024) return `${n} B`;
  const kb = n / 1024;
  if (kb < 1024) return `${kb >= 100 ? Math.round(kb) : kb.toFixed(1)} KB`;
  const mb = kb / 1024;
  return `${mb >= 100 ? Math.round(mb) : mb.toFixed(1)} MB`;
}

/** "Repeats every week" / "Repeats every 2 weeks" for a task's recurrence. */
export function repeatLabel(every, unit) {
  if (!every || !unit) return '';
  return `Repeats every ${every === 1 ? unit : `${every} ${unit}s`}`;
}

export function humanize(s) {
  if (!s) return '';
  const str = String(s).replace(/[_-]+/g, ' ').trim();
  return str.charAt(0).toUpperCase() + str.slice(1);
}

const ENTITY_ROUTES = {
  person: 'people',
  lead: 'leads',
  company: 'companies',
  opportunity: 'opportunities',
};

export function entityPath(entityType, entityId) {
  const route = ENTITY_ROUTES[entityType];
  return route && entityId ? `/${route}/${entityId}` : null;
}
