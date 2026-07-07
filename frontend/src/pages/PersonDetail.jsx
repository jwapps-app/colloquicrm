import { useEffect, useState } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';
import { del, get } from '../api';
import { useEntity, useUsers } from '../hooks';
import { useToast } from '../components/Toast';
import DetailShell from '../components/DetailShell';
import ProfilePanel from '../components/ProfilePanel';
import TagEditor from '../components/TagEditor';
import TasksPanel from '../components/TasksPanel';
import CalendarPanel from '../components/CalendarPanel';
import RelatedPanel from '../components/RelatedPanel';
import { Empty, Loading } from '../components/ui';
import { fullName, humanize, money } from '../format';
import { PERSON_FIELDS } from '../constants/fields';

function PersonOpportunities({ personId }) {
  const [items, setItems] = useState(null);
  useEffect(() => {
    let on = true;
    // The list endpoint has no person filter in the contract; we pass
    // primary_person_id anyway and also filter client-side as a safety net.
    get('/opportunities', { primary_person_id: personId, page: 1, page_size: 100 })
      .then((d) => {
        if (on) setItems((d?.items || []).filter((o) => o.primary_person_id === personId));
      })
      .catch(() => {
        if (on) setItems([]);
      });
    return () => {
      on = false;
    };
  }, [personId]);

  return (
    <RelatedPanel
      title="Opportunities"
      items={items}
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
  );
}

export default function PersonDetail() {
  const { id } = useParams();
  const nav = useNavigate();
  const toast = useToast();
  const users = useUsers();
  const { entity: person, save, error } = useEntity('/people', id);

  if (error) return <div className="page"><Empty label="Person not found." hint={error} /></div>;
  if (!person) return <Loading label="Loading person…" />;

  async function remove() {
    if (!window.confirm('Delete this person? This cannot be undone.')) return;
    try {
      await del(`/people/${id}`);
      toast.success('Person deleted');
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
      onDelete={remove}
      entityType="person"
      entityId={id}
      left={
        <>
          <ProfilePanel entity={person} entityType="person" fields={PERSON_FIELDS} users={users} onSave={save} />
          <div className="card">
            <h4 className="panel-title">Tags</h4>
            <TagEditor tags={person.tags || []} onChange={(tags) => save({ tags })} />
          </div>
        </>
      }
      right={
        <>
          <TasksPanel entityType="person" entityId={id} />
          <CalendarPanel entityType="person" entityId={id} />
          <PersonOpportunities personId={id} />
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
