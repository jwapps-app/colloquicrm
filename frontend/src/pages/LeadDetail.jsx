import { useEffect, useState } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';
import { bustCache, del, post } from '../api';
import { useEntity, usePipelines, useUsers } from '../hooks';
import { useToast } from '../components/Toast';
import DetailShell from '../components/DetailShell';
import MergeButton from '../components/MergeButton';
import ProfilePanel from '../components/ProfilePanel';
import TagEditor from '../components/TagEditor';
import TasksPanel from '../components/TasksPanel';
import CalendarPanel from '../components/CalendarPanel';
import Modal from '../components/Modal';
import { Empty, Loading } from '../components/ui';
import { fmtDate, fullName } from '../format';
import { LEAD_FIELDS } from '../constants/fields';

function ConvertModal({ lead, onClose }) {
  const nav = useNavigate();
  const toast = useToast();
  const pipelines = usePipelines();
  const [createCompany, setCreateCompany] = useState(!!lead.company_name);
  const [withOpp, setWithOpp] = useState(false);
  const [pipelineId, setPipelineId] = useState('');
  const [oppName, setOppName] = useState(lead.company_name ? `${lead.company_name} — new business` : fullName(lead));
  const [oppValue, setOppValue] = useState(lead.value ?? '');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    if (!pipelineId && pipelines.length) setPipelineId(pipelines[0].id);
  }, [pipelines, pipelineId]);

  async function submit(e) {
    e.preventDefault();
    setBusy(true);
    setError('');
    try {
      const body = { create_company: !!(lead.company_name && createCompany) };
      if (withOpp && pipelineId) {
        body.pipeline_id = pipelineId;
        if (oppName.trim()) body.opportunity_name = oppName.trim();
        if (oppValue !== '' && oppValue !== null) body.opportunity_value = Number(oppValue);
      }
      const res = await post(`/leads/${lead.id}/convert`, body);
      toast.success('Lead converted');
      if (res?.person_id) nav(`/people/${res.person_id}`);
      else onClose();
    } catch (err) {
      setError(err.message);
      setBusy(false);
    }
  }

  return (
    <Modal title="Convert lead" onClose={onClose}>
      <form className="form" onSubmit={submit}>
        <p className="muted">
          Converting creates a person from <strong>{fullName(lead)}</strong>.
        </p>
        {lead.company_name && (
          <label className="field field-inline">
            <span>Also create company “{lead.company_name}”</span>
            <input type="checkbox" checked={createCompany} onChange={(e) => setCreateCompany(e.target.checked)} />
          </label>
        )}
        <label className="field field-inline">
          <span>Create an opportunity</span>
          <input type="checkbox" checked={withOpp} onChange={(e) => setWithOpp(e.target.checked)} />
        </label>
        {withOpp && (
          <>
            <label className="field">
              <span>Pipeline</span>
              <select value={pipelineId} onChange={(e) => setPipelineId(e.target.value)} required>
                {pipelines.length === 0 && <option value="">No pipelines available</option>}
                {pipelines.map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.name}
                  </option>
                ))}
              </select>
            </label>
            <label className="field">
              <span>Opportunity name</span>
              <input value={oppName} onChange={(e) => setOppName(e.target.value)} />
            </label>
            <label className="field">
              <span>Value</span>
              <input type="number" step="any" value={oppValue} onChange={(e) => setOppValue(e.target.value)} />
            </label>
          </>
        )}
        {error && <div className="form-error">{error}</div>}
        <div className="form-actions">
          <button type="button" className="btn" onClick={onClose}>
            Cancel
          </button>
          <button type="submit" className="btn btn-primary" disabled={busy || (withOpp && !pipelineId)}>
            {busy ? 'Converting…' : 'Convert'}
          </button>
        </div>
      </form>
    </Modal>
  );
}

export default function LeadDetail() {
  const { id } = useParams();
  const nav = useNavigate();
  const toast = useToast();
  const users = useUsers();
  const { entity: lead, save, error, refresh } = useEntity('/leads', id);
  const [showConvert, setShowConvert] = useState(false);

  if (error) return <div className="page"><Empty label="Lead not found." hint={error} /></div>;
  if (!lead) return <Loading label="Loading lead…" />;

  async function remove() {
    if (!window.confirm('Move this lead to Trash? Recoverable from Settings → Trash for 60 days.')) return;
    try {
      await del(`/leads/${id}`);
      toast.success('Lead moved to Trash');
      nav('/leads');
    } catch (e) {
      toast.error(e.message);
    }
  }

  const converted = !!lead.converted_at;

  return (
    <>
      <DetailShell
        backTo="/leads"
        backLabel="Leads"
        title={fullName(lead)}
        subtitle={[lead.title, lead.company_name].filter(Boolean).join(' · ')}
        actions={<MergeButton apiPath="/leads" entityId={id} label={fullName(lead)} onMerged={refresh} />}
        onDelete={remove}
        entityType="lead"
        entityId={id}
        left={
          <>
            <ProfilePanel entity={lead} entityType="lead" fields={LEAD_FIELDS} users={users} onSave={save} />
            <div className="card">
              <h4 className="panel-title">Tags</h4>
              <TagEditor tags={lead.tags || []} onChange={(tags) => save({ tags }).then(() => bustCache('/tags'))} />
            </div>
          </>
        }
        right={
          <>
            <div className="card convert-card">
              <h4 className="panel-title">Convert</h4>
              {converted ? (
                <>
                  <div className="muted">Converted on {fmtDate(lead.converted_at)}.</div>
                  {lead.converted_person_id && (
                    <Link className="btn btn-block convert-link" to={`/people/${lead.converted_person_id}`}>
                      View person →
                    </Link>
                  )}
                </>
              ) : (
                <>
                  <p className="muted">Turn this lead into a person (and optionally a company and opportunity).</p>
                  <button className="btn btn-primary btn-block" onClick={() => setShowConvert(true)}>
                    Convert lead
                  </button>
                </>
              )}
            </div>
            <TasksPanel entityType="lead" entityId={id} />
          <CalendarPanel entityType="lead" entityId={id} />
          </>
        }
      />
      {showConvert && <ConvertModal lead={lead} onClose={() => setShowConvert(false)} />}
    </>
  );
}
