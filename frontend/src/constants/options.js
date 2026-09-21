import { humanize } from '../format';

const opt = (v) => ({ value: v, label: humanize(v) });

// The backend doesn't publish enums for these; values are conventional.
export const CONTACT_TYPES = ['Potential Customer', 'Current Customer', 'Uncategorized', 'Other'].map(
  (v) => ({ value: v, label: v })
);

// Canonical Title-Case values — exactly what the server stores and returns
// (it normalizes input case-insensitively, but filters, automation rules and
// option matching here all compare against these literals).
export const LEAD_STATUSES = ['New', 'Open', 'Contacted', 'Qualified', 'Unqualified', 'Converted'].map(opt);

// "Converted" is set by the convert action, not picked by hand on a new lead.
export const LEAD_CREATE_STATUSES = LEAD_STATUSES.filter((o) => o.value !== 'Converted');

/** CSS class for a status badge. Class names stay lowercase whatever the
 * stored casing is ("New" → status-new). */
export const statusClass = (status) => `status-${String(status || '').toLowerCase().replace(/\s+/g, '-')}`;

export const OPPORTUNITY_STATUSES = ['open', 'won', 'lost', 'abandoned'].map(opt);

export const PRIORITIES = ['none', 'low', 'medium', 'high'].map(opt);

// Task recurrence presets. Values encode (repeat_every, repeat_unit) as
// "every:unit" so a single <select> round-trips the pair; '' = no repeat.
export const REPEAT_OPTIONS = [
  { value: '', label: "Doesn't repeat" },
  { value: '1:day', label: 'Daily' },
  { value: '1:week', label: 'Weekly' },
  { value: '2:week', label: 'Every 2 weeks' },
  { value: '1:month', label: 'Monthly' },
  { value: '3:month', label: 'Every 3 months' },
  { value: '1:year', label: 'Yearly' },
];

export const CURRENCIES =['USD', 'EUR', 'GBP', 'CAD', 'AUD', 'JPY'].map((v) => ({ value: v, label: v }));

export const PREFIXES = ['Mr.', 'Mrs.', 'Miss', 'Ms.', 'Dr.'].map((v) => ({ value: v, label: v }));

export const CUSTOM_FIELD_TYPES = ['text', 'number', 'date', 'select', 'checkbox', 'url', 'currency'].map(opt);

export const IMPORT_TYPES = [
  { value: 'people', label: 'People' },
  { value: 'leads', label: 'Leads' },
  { value: 'companies', label: 'Companies' },
  { value: 'opportunities', label: 'Opportunities' },
];

export const CF_ENTITY_TYPES = [
  { value: 'person', label: 'People' },
  { value: 'lead', label: 'Leads' },
  { value: 'company', label: 'Companies' },
  { value: 'opportunity', label: 'Opportunities' },
];
