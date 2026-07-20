import { useMemo, useState } from 'react';
import ListPage from '../components/ListPage';
import OpportunityBoard from './OpportunityBoard';
import { usePipelines } from '../hooks';
import { fmtDate, humanize, money } from '../format';
import { applyFirstStage } from '../pipelines';
import { CURRENCIES, OPPORTUNITY_STATUSES } from '../constants/options';

export default function OpportunitiesList() {
  const [mode, setMode] = useState('table');
  const pipelines = usePipelines();

  const stageMap = useMemo(() => {
    const m = {};
    pipelines.forEach((p) => p.stages?.forEach((s) => (m[s.id] = { stage: s.name, pipeline: p.name })));
    return m;
  }, [pipelines]);

  const columns = useMemo(
    () => [
      { key: 'name', label: 'Name', render: (o) => <strong>{o.name}</strong> },
      { key: 'company_name', label: 'Company', render: (o) => o.company_name || '—' },
      {
        key: 'stage_id',
        label: 'Pipeline / Stage',
        sortable: false,
        render: (o) => {
          const s = stageMap[o.stage_id];
          return s ? `${s.pipeline} / ${s.stage}` : '—';
        },
      },
      {
        key: 'status',
        label: 'Status',
        render: (o) => <span className={`badge status-${o.status}`}>{humanize(o.status)}</span>,
      },
      { key: 'value', label: 'Value', render: (o) => money(o.value, o.currency) },
      { key: 'close_date', label: 'Close date', render: (o) => fmtDate(o.close_date) },
    ],
    [stageMap]
  );

  const createFields = useMemo(
    () => [
      { key: 'name', label: 'Name', required: true },
      { key: 'value', label: 'Value', type: 'number' },
      { key: 'currency', label: 'Currency', type: 'select', options: CURRENCIES, default: 'USD' },
      { key: 'close_date', label: 'Close date', type: 'date' },
      {
        // Required so an opportunity can never be created without a pipeline —
        // if the modal opens before /pipelines resolves, the empty select
        // blocks submit until a pipeline is picked.
        key: 'pipeline_id',
        label: 'Pipeline',
        type: 'select',
        required: true,
        options: pipelines.map((p) => ({ value: p.id, label: p.name })),
        default: pipelines[0]?.id,
      },
    ],
    [pipelines]
  );

  function transformCreate(body) {
    return applyFirstStage(body, pipelines);
  }

  const toggle = (
    <div className="seg-toggle">
      <button className={mode === 'table' ? 'active' : ''} onClick={() => setMode('table')}>
        Table
      </button>
      <button className={mode === 'board' ? 'active' : ''} onClick={() => setMode('board')}>
        Board
      </button>
    </div>
  );

  if (mode === 'board') {
    return (
      <div className="page">
        <div className="page-head">
          <h1>Opportunities</h1>
          <div className="page-head-actions">{toggle}</div>
        </div>
        <OpportunityBoard pipelines={pipelines} />
      </div>
    );
  }

  return (
    <ListPage
      title="Opportunities"
      entityType="opportunity"
      apiPath="/opportunities"
      route="/opportunities"
      columns={columns}
      filterDefs={[
        { key: 'status', label: 'Status', options: OPPORTUNITY_STATUSES },
        {
          key: 'pipeline_id',
          label: 'Pipeline',
          options: pipelines.map((p) => ({ value: p.id, label: p.name })),
        },
        { type: 'tag' },
        { type: 'owner' },
      ]}
      createFields={createFields}
      createTitle="Add opportunity"
      transformCreate={transformCreate}
      headerExtra={toggle}
    />
  );
}
