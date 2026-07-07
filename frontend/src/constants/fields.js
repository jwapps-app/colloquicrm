import { CONTACT_TYPES, LEAD_STATUSES, PRIORITIES, CURRENCIES, PREFIXES } from './options';
import { money } from '../format';

const ADDRESS = [
  { key: 'street', label: 'Street' },
  { key: 'city', label: 'City' },
  { key: 'state', label: 'State' },
  { key: 'postal_code', label: 'Postal code' },
  { key: 'country', label: 'Country' },
];

export const PERSON_FIELDS = [
  { key: 'prefix', label: 'Prefix', type: 'select', options: PREFIXES },
  { key: 'first_name', label: 'First name' },
  { key: 'middle_name', label: 'Middle name' },
  { key: 'last_name', label: 'Last name' },
  { key: 'suffix', label: 'Suffix' },
  { key: 'title', label: 'Title' },
  { key: 'contact_type', label: 'Contact type', type: 'select', options: CONTACT_TYPES },
  { key: 'owner_id', label: 'Owner' },
  { key: 'work_email', label: 'Work email' },
  { key: 'personal_email', label: 'Personal email' },
  { key: 'work_phone', label: 'Work phone' },
  { key: 'mobile_phone', label: 'Mobile phone' },
  ...ADDRESS,
  { key: 'work_website', label: 'Work website' },
  { key: 'personal_website', label: 'Personal website' },
  { key: 'linkedin', label: 'LinkedIn' },
  { key: 'facebook', label: 'Facebook' },
  { key: 'details', label: 'Details', type: 'textarea' },
];

export const LEAD_FIELDS = [
  { key: 'prefix', label: 'Prefix', type: 'select', options: PREFIXES },
  { key: 'first_name', label: 'First name' },
  { key: 'middle_name', label: 'Middle name' },
  { key: 'last_name', label: 'Last name' },
  { key: 'suffix', label: 'Suffix' },
  { key: 'title', label: 'Title' },
  { key: 'company_name', label: 'Company' },
  { key: 'status', label: 'Status', type: 'select', options: LEAD_STATUSES },
  { key: 'owner_id', label: 'Owner' },
  { key: 'value', label: 'Value', type: 'number', render: (v, e) => (v == null ? '' : money(v, e.currency)) },
  { key: 'currency', label: 'Currency', type: 'select', options: CURRENCIES },
  { key: 'source', label: 'Source' },
  { key: 'email', label: 'Email' },
  { key: 'work_phone', label: 'Work phone' },
  { key: 'mobile_phone', label: 'Mobile phone' },
  ...ADDRESS,
  { key: 'work_website', label: 'Work website' },
  { key: 'personal_website', label: 'Personal website' },
  { key: 'linkedin', label: 'LinkedIn' },
  { key: 'facebook', label: 'Facebook' },
  { key: 'details', label: 'Details', type: 'textarea' },
];

export const COMPANY_FIELDS = [
  { key: 'name', label: 'Name' },
  { key: 'email_domain', label: 'Email domain' },
  { key: 'contact_type', label: 'Contact type', type: 'select', options: CONTACT_TYPES },
  { key: 'owner_id', label: 'Owner' },
  { key: 'work_phone', label: 'Work phone' },
  { key: 'work_website', label: 'Website' },
  { key: 'linkedin', label: 'LinkedIn' },
  { key: 'facebook', label: 'Facebook' },
  ...ADDRESS,
  { key: 'details', label: 'Details', type: 'textarea' },
];

export const OPPORTUNITY_FIELDS = [
  { key: 'name', label: 'Name' },
  { key: 'owner_id', label: 'Owner' },
  { key: 'priority', label: 'Priority', type: 'select', options: PRIORITIES },
  { key: 'value', label: 'Value', type: 'number', render: (v, e) => (v == null ? '' : money(v, e.currency)) },
  { key: 'currency', label: 'Currency', type: 'select', options: CURRENCIES },
  { key: 'close_date', label: 'Close date', type: 'date' },
  { key: 'win_probability', label: 'Win probability', type: 'number', render: (v) => (v == null ? '' : `${v}%`) },
  { key: 'source', label: 'Source' },
  { key: 'details', label: 'Details', type: 'textarea' },
];
