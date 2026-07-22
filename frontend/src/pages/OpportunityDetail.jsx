import { Link, useNavigate, useParams } from 'react-router-dom';
import { bustCache, del } from '../api';
import { useEntity, usePipelines, useUsers } from '../hooks';
import { useToast } from '../components/Toast';
import DetailShell from '../components/DetailShell';
import ProfilePanel from '../components/ProfilePanel';
import TagEditor from '../components/TagEditor';
import TasksPanel from '../components/TasksPanel';
import AttachmentsPanel from '../components/AttachmentsPanel';
import { Empty, Loading } from '../components/ui';
import { humanize, money } from '../format';
import { OPPORTUNITY_FIELDS } from '../constants/fields';

function StageBar({ pipeline, stageId, onSelect }) {
  const stages = [...(pipeline.stages || [])].sort((a, b) => a.position - b.position);
  const idx = stages.findIndex((s) => s.id === stageId);
  return (
    <div className="stagebar" role="group" aria-label="Pipeline stages">
      {stages.map((s, i) => (
        <button
          key={s.id}
          type="button"
          className={'stage' + (i < idx ? ' done' : '') + (i === idx ? ' current' : '')}
          onClick={() => onSelect(s.id)}
          title={s.win_probability != null ? `${s.name} — ${s.win_probability}% win probability` : s.name}
        >
          {s.name}
        </button>
      ))}
    </div>
  );
}

export default function OpportunityDetail() {
  const { id } = useParams();
  const nav = useNavigate();
  const toast = useToast();
  const users = useUsers();
  const pipelines = usePipelines();
  const { entity: opp, save, error } = useEntity('/opportunities', id);

  if (error) return <div className="page"><Empty label="Opportunity not found." hint={error} /></div>;
  if (!opp) return <Loading label="Loading opportunity…" />;

  const pipeline = pipelines.find((p) => p.id === opp.pipeline_id);

  async function remove() {
    if (!window.confirm('Move this opportunity to Trash? Recoverable from Settings → Trash for 60 days.')) return;
    try {
      await del(`/opportunities/${id}`);
      bustCache('/tags'); // this record's tags may have lost their last live use
      toast.success('Opportunity moved to Trash');
      nav('/opportunities');
    } catch (e) {
      toast.error(e.message);
    }
  }

  function markLost() {
    const reason = window.prompt('Loss reason (optional):');
    if (reason === null) return; // cancelled
    save({ status: 'lost', loss_reason: reason || null });
  }

  const banner = (
    <div className="opp-banner">
      {pipeline && <StageBar pipeline={pipeline} stageId={opp.stage_id} onSelect={(sid) => save({ stage_id: sid })} />}
      <div className="status-row">
        <span className={`badge status-${opp.status}`}>{humanize(opp.status)}</span>
        {opp.status === 'open' ? (
          <>
            <button className="btn btn-success" onClick={() => save({ status: 'won' })}>
              Won
            </button>
            <button className="btn btn-danger" onClick={markLost}>
              Lost
            </button>
            <button className="btn" onClick={() => save({ status: 'abandoned' })}>
              Abandoned
            </button>
          </>
        ) : (
          <button className="btn" onClick={() => save({ status: 'open', loss_reason: null })}>
            Reopen
          </button>
        )}
        {opp.status === 'lost' && opp.loss_reason && <span className="muted">Reason: {opp.loss_reason}</span>}
      </div>
    </div>
  );

  return (
    <DetailShell
      backTo="/opportunities"
      backLabel="Opportunities"
      title={opp.name}
      subtitle={[opp.company_name, money(opp.value, opp.currency)].filter(Boolean).join(' · ')}
      onDelete={remove}
      banner={banner}
      entityType="opportunity"
      entityId={id}
      left={
        <>
          <ProfilePanel entity={opp} entityType="opportunity" fields={OPPORTUNITY_FIELDS} users={users} onSave={save} />
          <div className="card">
            <h4 className="panel-title">Tags</h4>
            <TagEditor tags={opp.tags || []} onChange={(tags) => save({ tags }).then(() => bustCache('/tags'))} />
          </div>
        </>
      }
      right={
        <>
          <TasksPanel entityType="opportunity" entityId={id} />
          <AttachmentsPanel entityType="opportunity" entityId={id} />
          {(opp.company_id || opp.primary_person_id) && (
            <div className="card">
              <h4 className="panel-title">Related</h4>
              <div className="related-list">
                {opp.company_id && (
                  <Link to={`/companies/${opp.company_id}`} className="related-item">
                    <span>{opp.company_name || 'Company'}</span>
                    <span className="muted">Company</span>
                  </Link>
                )}
                {opp.primary_person_id && (
                  <Link to={`/people/${opp.primary_person_id}`} className="related-item">
                    <span>{opp.person_name || 'Person'}</span>
                    <span className="muted">Primary contact</span>
                  </Link>
                )}
              </div>
            </div>
          )}
        </>
      }
    />
  );
}
