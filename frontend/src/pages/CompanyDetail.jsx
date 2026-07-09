import { useEffect, useState } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';
import { del, get } from '../api';
import { useContactTypes, useEntity, useUsers } from '../hooks';
import { useToast } from '../components/Toast';
import DetailShell from '../components/DetailShell';
import MergeButton from '../components/MergeButton';
import ProfilePanel from '../components/ProfilePanel';
import TagEditor from '../components/TagEditor';
import TasksPanel from '../components/TasksPanel';
import CalendarPanel from '../components/CalendarPanel';
import RelatedPanel from '../components/RelatedPanel';
import { Empty, Loading } from '../components/ui';
import { fullName, humanize, money } from '../format';
import { COMPANY_FIELDS } from '../constants/fields';

function useRelated(apiPath, params, deps) {
  const [items, setItems] = useState(null);
  useEffect(() => {
    let on = true;
    setItems(null);
    get(apiPath, { ...params, page: 1, page_size: 100 })
      .then((d) => {
        if (on) setItems(d?.items || []);
      })
      .catch(() => {
        if (on) setItems([]);
      });
    return () => {
      on = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
  return items;
}

export default function CompanyDetail() {
  const { id } = useParams();
  const nav = useNavigate();
  const toast = useToast();
  const users = useUsers();
  const contactTypes = useContactTypes();
  const { entity: company, save, error, refresh } = useEntity('/companies', id);
  const people = useRelated('/people', { company_id: id }, [id]);
  const opps = useRelated('/opportunities', { company_id: id }, [id]);

  if (error) return <div className="page"><Empty label="Company not found." hint={error} /></div>;
  if (!company) return <Loading label="Loading company…" />;

  async function remove() {
    if (!window.confirm('Move this company to Trash? Recoverable from Settings → Trash for 60 days.')) return;
    try {
      await del(`/companies/${id}`);
      toast.success('Company moved to Trash');
      nav('/companies');
    } catch (e) {
      toast.error(e.message);
    }
  }

  return (
    <DetailShell
      backTo="/companies"
      backLabel="Companies"
      title={company.name}
      subtitle={[company.email_domain, company.city].filter(Boolean).join(' · ')}
      actions={<MergeButton apiPath="/companies" entityId={id} label={company.name} onMerged={refresh} />}
      onDelete={remove}
      entityType="company"
      entityId={id}
      left={
        <>
          <ProfilePanel entity={company} entityType="company" fields={COMPANY_FIELDS} users={users} contactTypes={contactTypes} onSave={save} />
          <div className="card">
            <h4 className="panel-title">Tags</h4>
            <TagEditor tags={company.tags || []} onChange={(tags) => save({ tags })} />
          </div>
        </>
      }
      right={
        <>
          <TasksPanel entityType="company" entityId={id} />
          <CalendarPanel entityType="company" entityId={id} />
          <RelatedPanel
            title="People"
            items={people}
            empty="No people at this company."
            renderItem={(p) => (
              <Link key={p.id} to={`/people/${p.id}`} className="related-item">
                <span>{fullName(p)}</span>
                {p.title && <span className="muted">{p.title}</span>}
              </Link>
            )}
          />
          <RelatedPanel
            title="Opportunities"
            items={opps}
            empty="No opportunities."
            renderItem={(o) => (
              <Link key={o.id} to={`/opportunities/${o.id}`} className="related-item">
                <span>{o.name}</span>
                <span className="muted">
                  {money(o.value, o.currency)} · {humanize(o.status)}
                </span>
              </Link>
            )}
          />
        </>
      }
    />
  );
}
