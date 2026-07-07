import ListPage from '../components/ListPage';
import { fmtDate, fullName, humanize } from '../format';
import { CONTACT_TYPES } from '../constants/options';

const columns = [
  { key: 'name', label: 'Name', sortKey: 'last_name', render: (p) => <strong>{fullName(p)}</strong> },
  { key: 'work_email', label: 'Email', render: (p) => p.work_email || p.personal_email || '—' },
  { key: 'contact_type', label: 'Contact type', render: (p) => (p.contact_type ? humanize(p.contact_type) : '—') },
  { key: 'company_name', label: 'Company', render: (p) => p.company_name || '—' },
  { key: 'last_contacted_at', label: 'Last contacted', render: (p) => fmtDate(p.last_contacted_at) },
  { key: 'interaction_count', label: 'Interactions', render: (p) => p.interaction_count ?? 0 },
];

const createFields = [
  { key: 'first_name', label: 'First name', required: true },
  { key: 'last_name', label: 'Last name' },
  { key: 'title', label: 'Title' },
  { key: 'work_email', label: 'Work email', type: 'email' },
  { key: 'mobile_phone', label: 'Mobile phone' },
  { key: 'contact_type', label: 'Contact type', type: 'select', options: CONTACT_TYPES },
  { key: 'details', label: 'Details', type: 'textarea' },
];

export default function PeopleList() {
  return (
    <ListPage
      title="People"
      entityType="person"
      apiPath="/people"
      route="/people"
      columns={columns}
      filterDefs={[
        { key: 'contact_type', label: 'Contact type', options: CONTACT_TYPES },
        { type: 'tag' },
        { type: 'owner' },
      ]}
      createFields={createFields}
      createTitle="Add person"
      defaultSort="last_name"
      defaultOrder="asc"
    />
  );
}
