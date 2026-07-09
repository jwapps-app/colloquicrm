import ListPage from '../components/ListPage';
import { useContactTypes } from '../hooks';
import { fmtDate, fullName, humanize } from '../format';

const columns = [
  { key: 'name', label: 'Name', sortKey: 'last_name', render: (p) => <strong>{fullName(p)}</strong> },
  { key: 'work_email', label: 'Email', render: (p) => p.work_email || p.personal_email || '—' },
  { key: 'contact_type', label: 'Contact type', render: (p) => (p.contact_type ? humanize(p.contact_type) : '—') },
  { key: 'company_name', label: 'Company', render: (p) => p.company_name || '—' },
  { key: 'last_contacted_at', label: 'Last contacted', render: (p) => fmtDate(p.last_contacted_at) },
  { key: 'interaction_count', label: 'Interactions', render: (p) => p.interaction_count ?? 0 },
];

export default function PeopleList() {
  const contactTypes = useContactTypes();
  const createFields = [
    { key: 'first_name', label: 'First name', required: true },
    { key: 'last_name', label: 'Last name' },
    { key: 'title', label: 'Title' },
    { key: 'work_email', label: 'Work email', type: 'email' },
    { key: 'mobile_phone', label: 'Mobile phone' },
    { key: 'contact_type', label: 'Contact type', type: 'select', options: contactTypes },
    { key: 'details', label: 'Details', type: 'textarea' },
  ];
  return (
    <ListPage
      title="People"
      entityType="person"
      apiPath="/people"
      route="/people"
      columns={columns}
      filterDefs={[
        { key: 'contact_type', label: 'Contact type', options: contactTypes },
        { type: 'tag' },
        { type: 'owner' },
      ]}
      createFields={createFields}
      createTitle="Add person"
      defaultSort="last_contacted_at"
      defaultOrder="desc"
    />
  );
}
