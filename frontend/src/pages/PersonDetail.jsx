import { useState } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';
import { del, post } from '../api';
import { useContactTypes, useEntity, usePipelines, useRelated, useUsers } from '../hooks';
import { useToast } from '../components/Toast';
import DetailShell from '../components/DetailShell';
import FormModal from '../components/FormModal';
import MergeButton from '../components/MergeButton';
import ProfilePanel from '../components/ProfilePanel';
import TagEditor from '../components/TagEditor';
import TasksPanel from '../components/TasksPanel';
import CalendarPanel from '../components/CalendarPanel';
import RelatedPanel from '../components/RelatedPanel';
import { Empty, Loading } from '../components/ui';
import { fullName, humanize, money, safeHref } from '../format';
import { PERSON_FIELDS } from '../constants/fields';
import { CURRENCIES } from '../constants/options';

function PersonOpportunities({ personId, companyId }) {
  const toast = useToast();
  const pipelines = usePipelines();
  const [refreshKey, setRefreshKey] = useState(0);
  const [creating, setCreating] = useState(false);
  const items = useRelated('/opportunities', { primary_person_id: personId }, [personId, refreshKey]);

  const createFields = [
    { key: 'name', label: 'Name', required: true },
    { key: 'value', label: 'Value', type: 'number' },
    { key: 'currency', label: 'Currency', type: 'select', options: CURRENCIES, default: 'USD' },
    { key: 'close_date', label: 'Close date', type: 'date' },
    {
      key: 'pipeline_id',
      label: 'Pipeline',
      type: 'select',
      options: pipelines.map((p) => ({ value: p.id, label: p.name })),
      default: pipelines[0]?.id,
    },
  ];

  async function create(values) {
    const body = {};
    createFields.forEach((f) => {
      let v = values[f.key];
      if (v === '' || v === undefined) return;
      if (f.type === 'number') v = Number(v);
      body[f.key] = v;
    });
    body.primary_person_id = personId;
    if (companyId) body.company_id = companyId;
    if (body.pipeline_id) {
      const p = pipelines.find((x) => x.id === body.pipeline_id);
      const first = p?.stages?.slice().sort((a, b) => a.position - b.position)[0];
      if (first) body.stage_id = first.id;
    }
    await post('/opportunities', body);
    setCreating(false);
    setRefreshKey((k) => k + 1);
    toast.success('Opportunity created');
  }

  return (
    <>
      <RelatedPanel
        title="Opportunities"
        items={items}
        empty="No opportunities."
        action={
          <button className="btn btn-small" onClick={() => setCreating(true)}>
            + New
          </button>
        }
        renderItem={(o) => (
          <Link key={o.id} to={`/opportunities/${o.id}`} className="related-item">
            <span>{o.name}</span>
            <span className="muted">
              {money(o.value, o.currency)} · {humanize(o.status)}
            </span>
          </Link>
        )}
      />
      {creating && (
        <FormModal
          title="New opportunity"
          fields={createFields}
          submitLabel="Create"
          onSubmit={create}
          onClose={() => setCreating(false)}
        />
      )}
    </>
  );
}


function SocialFinder({ person, onSave }) {
  const toast = useToast();
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState(null);

  const missing = ['linkedin', 'facebook'].filter((k) => !person[k]);
  if (missing.length === 0 && !result) return null;
  if (!person.work_email && !person.personal_email) return null;

  async function run() {
    setBusy(true);
    try {
      const res = await post(`/people/${person.id}/find-socials`);
      setResult(res);
      const total = (res.linkedin?.length || 0) + (res.facebook?.length || 0);
      if (total === 0) toast.info('No profiles found in their emails or Gravatar.');
    } catch (e) {
      toast.error(e.message);
    }
    setBusy(false);
  }

  const suggestions = [];
  if (result) {
    for (const network of ['linkedin', 'facebook']) {
      if (person[network]) continue; // already filled (maybe just applied)
      for (const url of result[network] || []) {
        suggestions.push({ network, url });
      }
    }
  }

  return (
    <div className="card social-finder">
      <div className="panel-head">
        <h4 className="panel-title">Social profiles</h4>
        <button className="btn btn-small" onClick={run} disabled={busy}>
          {busy ? 'Searching…' : result ? 'Search again' : 'Find profiles'}
        </button>
      </div>
      {!result && (
        <div className="muted panel-empty">
          Searches their email signatures and Gravatar — nothing leaves your server.
        </div>
      )}
      {result && suggestions.length === 0 && (
        <div className="muted panel-empty">Nothing new found.</div>
      )}
      {suggestions.map((s) => (
        <div key={s.url} className="social-suggestion">
          <span className="badge badge-muted">{s.network === 'linkedin' ? 'LinkedIn' : 'Facebook'}</span>
          <a href={safeHref(s.url)} target="_blank" rel="noreferrer" className="social-url">
            {s.url.replace(/^https?:\/\/(www\.)?/, '')}
          </a>
          <button className="btn btn-small btn-primary" onClick={() => onSave({ [s.network]: s.url })}>
            Use
          </button>
        </div>
      ))}
    </div>
  );
}

export default function PersonDetail() {
  const { id } = useParams();
  const nav = useNavigate();
  const toast = useToast();
  const users = useUsers();
  const contactTypes = useContactTypes();
  const { entity: person, save, error, refresh } = useEntity('/people', id);

  if (error) return <div className="page"><Empty label="Person not found." hint={error} /></div>;
  if (!person) return <Loading label="Loading person…" />;

  async function remove() {
    if (!window.confirm('Move this person to Trash? Recoverable from Settings → Trash for 60 days.')) return;
    try {
      await del(`/people/${id}`);
      toast.success('Person moved to Trash');
      nav('/people');
    } catch (e) {
      toast.error(e.message);
    }
  }

  return (
    <DetailShell
      backTo="/people"
      backLabel="People"
      title={fullName(person)}
      subtitle={[person.title, person.company_name].filter(Boolean).join(' · ')}
      actions={<MergeButton apiPath="/people" entityId={id} label={fullName(person)} onMerged={refresh} />}
      onDelete={remove}
      entityType="person"
      entityId={id}
      left={
        <>
          <ProfilePanel entity={person} entityType="person" fields={PERSON_FIELDS} users={users} contactTypes={contactTypes} onSave={save} />
          <div className="card">
            <h4 className="panel-title">Tags</h4>
            <TagEditor tags={person.tags || []} onChange={(tags) => save({ tags })} />
          </div>
          <SocialFinder person={person} onSave={save} />
        </>
      }
      right={
        <>
          <TasksPanel entityType="person" entityId={id} />
          <CalendarPanel entityType="person" entityId={id} />
          <PersonOpportunities personId={id} companyId={person.company_id} />
          {person.company_id && (
            <div className="card">
              <h4 className="panel-title">Company</h4>
              <Link to={`/companies/${person.company_id}`} className="related-item">
                <span>{person.company_name || 'View company'}</span>
              </Link>
            </div>
          )}
        </>
      }
    />
  );
}
