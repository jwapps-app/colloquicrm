// The record types — the one list on this side of the API. Everything that
// needs to know "which types exist" or "what can this type do" derives from
// it: routes, the custom-field and import pickers, the trash, duplicate
// review, call logging. The server's list is services/entities.py, published
// at GET /options/record-types; `type`, `plural`, `correspondent` and
// `logsCalls` here mirror it.
//
//   type           entity_type used across the API
//   plural         URL segment, for both /api/v1/<plural> and the page route
//   correspondent  has emails, calls and calendar events on its timeline
//   logsCalls      a call can be logged on it by hand
//   duplicates     the server looks for duplicates of this type
export const RECORD_TYPES = [
  { type: 'person', plural: 'people', label: 'Person', pluralLabel: 'People', article: 'a person', correspondent: true, logsCalls: true, duplicates: true },
  { type: 'lead', plural: 'leads', label: 'Lead', pluralLabel: 'Leads', article: 'a lead', correspondent: true, logsCalls: true, duplicates: true },
  { type: 'company', plural: 'companies', label: 'Company', pluralLabel: 'Companies', article: 'a company', correspondent: true, logsCalls: false, duplicates: true },
  { type: 'opportunity', plural: 'opportunities', label: 'Opportunity', pluralLabel: 'Opportunities', article: 'an opportunity', correspondent: false, logsCalls: false, duplicates: false },
];

const BY_TYPE = Object.fromEntries(RECORD_TYPES.map((r) => [r.type, r]));

/** The record type's entry, or undefined for anything that isn't one. */
export const recordType = (type) => BY_TYPE[type];

/** API collection path and page route for a type: "/people". */
export const recordPath = (type) => (BY_TYPE[type] ? `/${BY_TYPE[type].plural}` : null);
