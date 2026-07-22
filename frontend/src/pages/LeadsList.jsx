import { useState } from 'react';
import ListPage from '../components/ListPage';
import DuplicatesBanner from '../components/DuplicatesBanner';
import { fullName, humanize, money } from '../format';
import { CURRENCIES, LEAD_STATUSES } from '../constants/options';

const columns = [
  {
    key: 'name',
    label: 'Name',
    sortKey: 'last_name',
    render: (l) => (
      <span>
        <strong>{fullName(l)}</strong>
        {l.converted_at && <span className="badge badge-muted converted-badge">Converted</span>}
      </span>
    ),
  },
  { key: 'email', label: 'Email' },
  { key: 'company_name', label: 'Company', render: (l) => l.company_name || '—' },
  {
    key: 'status',
    label: 'Status',
    render: (l) => (l.status ? <span className={`badge status-${l.status}`}>{humanize(l.status)}</span> : '—'),
  },
  { key: 'value', label: 'Value', render: (l) => money(l.value, l.currency) },
];

const createFields = [
  { key: 'first_name', label: 'First name', required: true },
  { key: 'last_name', label: 'Last name' },
  { key: 'email', label: 'Email', type: 'email' },
  { key: 'company_name', label: 'Company' },
  { key: 'title', label: 'Title' },
  { key: 'status', label: 'Status', type: 'select', options: LEAD_STATUSES },
  { key: 'value', label: 'Value', type: 'number' },
  { key: 'currency', label: 'Currency', type: 'select', options: CURRENCIES, default: 'USD' },
  { key: 'source', label: 'Source' },
];

export default function LeadsList() {
  const [refreshToken, setRefreshToken] = useState(0);
  return (
    <ListPage
      title="Leads"
      entityType="lead"
      apiPath="/leads"
      route="/leads"
      banner={<DuplicatesBanner entityType="lead" onMerged={() => setRefreshToken((k) => k + 1)} />}
      columns={columns}
      filterDefs={[
        { key: 'status', label: 'Status', options: LEAD_STATUSES },
        { type: 'tag' },
        { type: 'owner' },
      ]}
      createFields={createFields}
      createTitle="Add lead"
      refreshToken={refreshToken}
    />
  );
}
