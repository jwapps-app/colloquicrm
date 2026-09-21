import { useEffect, useState } from 'react';
import { Link } from 'react-router';
import { get, patch } from '../api';
import { useToast } from '../components/Toast';
import { Empty, Loading } from '../components/ui';
import { money, moneyTotals } from '../format';

// One request's worth of cards. Past this the board says it is partial.
const BOARD_CAP = 200;
const UNSTAGED = '__unstaged__';

export default function OpportunityBoard({ pipelines, onViewAll }) {
  const toast = useToast();
  const [pipelineId, setPipelineId] = useState('');
  const [opps, setOpps] = useState(null);
  const [total, setTotal] = useState(0);
  const [dragOver, setDragOver] = useState(null);

  useEffect(() => {
    if (!pipelineId && pipelines.length) setPipelineId(pipelines[0].id);
  }, [pipelines, pipelineId]);

  useEffect(() => {
    if (!pipelineId) return;
    let on = true;
    setOpps(null);
    get('/opportunities', { pipeline_id: pipelineId, status: 'open', page: 1, page_size: BOARD_CAP })
      .then((d) => {
        if (!on) return;
        const items = d?.items || [];
        setOpps(items);
        setTotal(Math.max(Number(d?.total) || 0, items.length));
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
  // Open deals in this pipeline with no stage (or one that no longer exists
  // here) get their own column — otherwise they are simply invisible.
  const stageIds = new Set(stages.map((s) => s.id));
  const unstaged = (opps || []).filter((o) => !o.stage_id || !stageIds.has(o.stage_id));
  // Unstaged is a read-only holding column: cards leave it, nothing drops in.
  const columns = [
    ...(unstaged.length ? [{ id: UNSTAGED, name: 'Unstaged', cards: unstaged, synthetic: true }] : []),
    ...stages.map((s) => ({ id: s.id, name: s.name, cards: (opps || []).filter((o) => o.stage_id === s.id) })),
  ];

  async function move(opp, stageId) {
    if (!stageId || opp.stage_id === stageId) return;
    const prevStageId = opp.stage_id;
    setOpps((os) => os.map((o) => (o.id === opp.id ? { ...o, stage_id: stageId } : o)));
    try {
      await patch(`/opportunities/${opp.id}`, { stage_id: stageId });
    } catch (e) {
      toast.error(e.message);
      // Roll back only this card — other moves made meanwhile stay put.
      setOpps((os) => os && os.map((o) => (o.id === opp.id ? { ...o, stage_id: prevStageId } : o)));
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

      {opps !== null && total > opps.length && (
        <div className="board-partial" role="status">
          Showing {opps.length} of {total} open opportunities — column counts and totals cover only the cards shown.{' '}
          {onViewAll && (
            <button type="button" className="linklike" onClick={() => onViewAll({ status: 'open', pipeline_id: pipelineId })}>
              View all
            </button>
          )}
        </div>
      )}

      {opps === null ? (
        <Loading label="Loading board…" />
      ) : (
        <div className="kanban">
          {columns.map((s) => {
            const cards = s.cards;
            const droppable = !s.synthetic;
            return (
              <div
                key={s.id}
                className={'kcol' + (s.synthetic ? ' kcol-unstaged' : '') + (dragOver === s.id ? ' drag-over' : '')}
                onDragOver={(e) => {
                  if (!droppable) return;
                  e.preventDefault();
                  setDragOver(s.id);
                }}
                onDragLeave={() => setDragOver((d) => (d === s.id ? null : d))}
                onDrop={(e) => {
                  if (!droppable) return;
                  e.preventDefault();
                  setDragOver(null);
                  const id = e.dataTransfer.getData('text/plain');
                  const opp = opps.find((o) => o.id === id);
                  if (opp) move(opp, s.id);
                }}
              >
                <div className="kcol-head">
                  <span className="kcol-name">{s.name}</span>
                  {/* One subtotal per currency — dollars and euros never add. */}
                  <span className="muted kcol-total">
                    {cards.length} · {moneyTotals(cards)}
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
                        <select
                          value={stageIds.has(o.stage_id) ? o.stage_id : ''}
                          onChange={(e) => move(o, e.target.value)}
                          title="Move to stage"
                        >
                          {!stageIds.has(o.stage_id) && (
                            <option value="" disabled>
                              No stage
                            </option>
                          )}
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
