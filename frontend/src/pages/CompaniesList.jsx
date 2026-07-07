import ListPage from '../components/ListPage';
import { humanize } from '../format';
import { CONTACT_TYPES } from '../constants/options';

const columns = [
  { key: 'name', label: 'Name', render: (c) => <strong>{c.name}</strong> },
  { key: 'email_domain', label: 'Email domain', render: (c) => c.email_domain || '—' },
  { key: 'contact_type', label: 'Contact type', render: (c) => (c.contact_type ? humanize(c.contact_type) : '—') },
  { key: 'work_phone', label: 'Phone', render: (c) => c.work_phone || '—' },
];

const createFields = [
  { key: 'name', label: 'Name', required: true },
  { key: 'email_domain', label: 'Email domain', placeholder: 'acme.com' },
  { key: 'work_phone', label: 'Phone' },
  { key: 'work_website', label: 'Website' },
  { key: 'contact_type', label: 'Contact type', type: 'select', options: CONTACT_TYPES },
  { key: 'details', label: 'Details', type: 'textarea' },
];

export default function CompaniesList() {
  return (
    <ListPage
      title="Companies"
      entityType="company"
      apiPath="/companies"
      route="/companies"
      columns={columns}
      filterDefs={[
        { key: 'contact_type', label: 'Contact type', options: CONTACT_TYPES },
        { type: 'tag' },
        { type: 'owner' },
      ]}
      createFields={createFields}
      createTitle="Add company"
      defaultSort="name"
      defaultOrder="asc"
    />
  );
}
