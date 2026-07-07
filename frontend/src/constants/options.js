import { humanize } from '../format';

const opt = (v) => ({ value: v, label: humanize(v) });

// The backend doesn't publish enums for these; values are conventional.
export const CONTACT_TYPES = ['Potential Customer', 'Current Customer', 'Uncategorized', 'Other'].map(
  (v) => ({ value: v, label: v })
);

export const LEAD_STATUSES = ['new', 'open', 'contacted', 'qualified', 'unqualified'].map(opt);

export const OPPORTUNITY_STATUSES = ['open', 'won', 'lost', 'abandoned'].map(opt);

export const PRIORITIES = ['none', 'low', 'medium', 'high'].map(opt);

export const CURRENCIES = ['USD', 'EUR', 'GBP', 'CAD', 'AUD', 'JPY'].map((v) => ({ value: v, label: v }));

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
