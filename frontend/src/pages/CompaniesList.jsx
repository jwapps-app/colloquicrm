import { useState } from 'react';
import ListPage from '../components/ListPage';
import DuplicatesBanner from '../components/DuplicatesBanner';
import { useContactTypes } from '../hooks';
import { humanize } from '../format';

const columns = [
  { key: 'name', label: 'Name', render: (c) => <strong>{c.name}</strong> },
  { key: 'email_domain', label: 'Email domain', render: (c) => c.email_domain || '—' },
  { key: 'contact_type', label: 'Contact type', render: (c) => (c.contact_type ? humanize(c.contact_type) : '—') },
  { key: 'work_phone', label: 'Phone', render: (c) => c.work_phone || '—' },
];

export default function CompaniesList() {
  const contactTypes = useContactTypes();
  const [refreshToken, setRefreshToken] = useState(0);
  const createFields = [
    { key: 'name', label: 'Name', required: true },
    { key: 'email_domain', label: 'Email domain', placeholder: 'acme.com' },
    { key: 'work_phone', label: 'Phone' },
    { key: 'work_website', label: 'Website' },
    { key: 'contact_type', label: 'Contact type', type: 'select', options: contactTypes },
    { key: 'details', label: 'Details', type: 'textarea' },
  ];
  return (
    <ListPage
      title="Companies"
      entityType="company"
      apiPath="/companies"
      route="/companies"
      banner={<DuplicatesBanner entityType="company" onMerged={() => setRefreshToken((k) => k + 1)} />}
      columns={columns}
      filterDefs={[
        { key: 'contact_type', label: 'Contact type', options: contactTypes },
        { type: 'tag' },
        { type: 'owner' },
      ]}
      createFields={createFields}
      createTitle="Add company"
      defaultSort="name"
      defaultOrder="asc"
      refreshToken={refreshToken}
    />
  );
}
