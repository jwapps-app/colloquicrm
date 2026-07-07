import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { get, patch } from '../api';
import { useToast } from '../components/Toast';
import { Empty, Loading } from '../components/ui';
import { money } from '../format';

export default function OpportunityBoard({ pipelines }) {
  const toast = useToast();
  const [pipelineId, setPipelineId] = useState('');
  const [opps, setOpps] = useState(null);
  const [dragOver, setDragOver] = useState(null);

  useEffect(() => {
    if (!pipelineId && pipelines.length) setPipelineId(pipelines[0].id);
  }, [pipelines, pipelineId]);

  useEffect(() => {
    if (!pipelineId) return;
    let on = true;
    setOpps(null);
    get('/opportunities', { pipeline_id: pipelineId, status: 'open', page: 1, page_size: 200 })
      .then((d) => {
        if (on) setOpps(d?.items || []);
      })
      .catch((e) => {
        toast.error(e.message);
        if (on) setOpps([]);
      });
    return () => {
      on = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pipelineId]);

  const pipeline = pipelines.find((p) => p.id === pipelineId);
  const stages = pipeline ? [...(pipeline.stages || [])].sort((a, b) => a.position - b.position) : [];

  async function move(opp, stageId) {
    if (!stageId || opp.stage_id === stageId) return;
    const prev = opps;
    setOpps((os) => os.map((o) => (o.id === opp.id ? { ...o, stage_id: stageId } : o)));
    try {
      await patch(`/opportunities/${opp.id}`, { stage_id: stageId });
    } catch (e) {
      toast.error(e.message);
      setOpps(prev);
    }
  }

  if (pipelines.length === 0) {
    return <Empty label="No pipelines configured." hint="Create a pipeline in the backend to use the board." />;
  }

  return (
    <div>
      <div className="board-toolbar">
        <label className="muted" htmlFor="board-pipeline">
          Pipeline
        </label>
        <select id="board-pipeline" value={pipelineId} onChange={(e) => setPipelineId(e.target.value)}>
          {pipelines.map((p) => (
            <option key={p.id} value={p.id}>
              {p.name}
            </option>
          ))}
        </select>
        <span className="muted">Showing open opportunities</span>
      </div>

      {opps === null ? (
        <Loading label="Loading board…" />
      ) : (
        <div className="kanban">
          {stages.map((s) => {
            const cards = opps.filter((o) => o.stage_id === s.id);
            const totalValue = cards.reduce((sum, o) => sum + (Number(o.value) || 0), 0);
            const currency = cards.find((o) => o.currency)?.currency || 'USD';
            return (
              <div
                key={s.id}
                className={'kcol' + (dragOver === s.id ? ' drag-over' : '')}
                onDragOver={(e) => {
                  e.preventDefault();
                  setDragOver(s.id);
                }}
                onDragLeave={() => setDragOver((d) => (d === s.id ? null : d))}
                onDrop={(e) => {
                  e.preventDefault();
                  setDragOver(null);
                  const id = e.dataTransfer.getData('text/plain');
                  const opp = opps.find((o) => o.id === id);
                  if (opp) move(opp, s.id);
                }}
              >
                <div className="kcol-head">
                  <span className="kcol-name">{s.name}</span>
                  <span className="muted">
                    {cards.length} · {money(totalValue, currency)}
                  </span>
                </div>
                <div className="kcol-cards">
                  {cards.map((o) => (
                    <div
                      key={o.id}
                      className="kcard"
                      draggable
                      onDragStart={(e) => e.dataTransfer.setData('text/plain', o.id)}
                    >
                      <Link to={`/opportunities/${o.id}`} className="kcard-name">
                        {o.name}
                      </Link>
                      {o.company_name && <div className="muted kcard-company">{o.company_name}</div>}
                      <div className="kcard-foot">
                        <span className="kcard-value">{money(o.value, o.currency)}</span>
                        <select value={o.stage_id || ''} onChange={(e) => move(o, e.target.value)} title="Move to stage">
                          {stages.map((st) => (
                            <option key={st.id} value={st.id}>
                              {st.name}
                            </option>
                          ))}
                        </select>
                      </div>
                    </div>
                  ))}
                  {cards.length === 0 && <div className="kcol-empty muted">No cards</div>}
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
