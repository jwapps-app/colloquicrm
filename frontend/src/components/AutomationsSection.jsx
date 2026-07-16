import { useEffect, useMemo, useState } from 'react';
import { cachedGet, del, get, patch, post } from '../api';
import { useToast } from './Toast';
import Modal from './Modal';
import ToggleListRow from './ToggleListRow';
import { Loading } from './ui';
import { fmtDateTime } from '../format';
import { LEAD_STATUSES, OPPORTUNITY_STATUSES } from '../constants/options';

const ENTITY_OPTIONS = [
  { value: 'lead', label: 'Lead', a: 'a lead' },
  { value: 'person', label: 'Person', a: 'a person' },
  { value: 'opportunity', label: 'Opportunity', a: 'an opportunity' },
  { value: 'task', label: 'Task', a: 'a task' },
];

const TRIGGER_OPTIONS = [
  { value: 'stale_record', label: 'is untouched for N days', entities: ['lead', 'person', 'opportunity'] },
  { value: 'record_created', label: 'is created', entities: ['lead', 'person', 'opportunity'] },
  { value: 'stage_entered', label: 'enters a pipeline stage', entities: ['opportunity'] },
  { value: 'task_overdue', label: 'is overdue by N days', entities: ['task'] },
];

const ACTION_OPTIONS = [
  { value: 'create_task', label: 'Create a follow-up task', entities: ['lead', 'person', 'opportunity', 'task'] },
  { value: 'notify', label: 'Send a notification', entities: ['lead', 'person', 'opportunity', 'task'] },
  { value: 'add_tag', label: 'Add a tag', entities: ['lead', 'person', 'opportunity'] },
];

// One-click starters shown when there are no rules yet. They prefill the
// wizard — nothing is created until the admin hits Create.
const TEMPLATES = [
  {
    label: 'Stale lead follow-up (14d)',
    values: {
      entity_type: 'lead', trigger_type: 'stale_record', days: '14',
      action_type: 'create_task', task_name: 'Follow up with {name}', due_in_days: '3', assignee: 'owner',
    },
  },
  {
    label: 'New lead intro task',
    values: {
      entity_type: 'lead', trigger_type: 'record_created',
      action_type: 'create_task', task_name: 'Send intro to {name}', due_in_days: '1', assignee: 'owner',
    },
  },
  {
    label: 'Overdue task nudge (3d)',
    values: {
      entity_type: 'task', trigger_type: 'task_overdue', days: '3',
      action_type: 'notify', recipient: 'owner', message: 'Still open and overdue: {name}',
    },
  },
];

function entityA(entityType) {
  return ENTITY_OPTIONS.find((e) => e.value === entityType)?.a || entityType;
}

function userName(users, id) {
  return (users || []).find((u) => u.id === id)?.display_name || 'a teammate';
}

function stageName(pipelines, stageId) {
  for (const p of pipelines || []) {
    const s = (p.stages || []).find((x) => x.id === stageId);
    if (s) return `${s.name} (${p.name})`;
  }
  return 'a stage';
}

/** "When a lead is untouched for 14 days → create task 'Follow up with {name}' for the owner" */
export function ruleSummary(rule, users, pipelines) {
  const tc = rule.trigger_config || {};
  const ac = rule.action_config || {};
  let when;
  switch (rule.trigger_type) {
    case 'stale_record':
      when = `${entityA(rule.entity_type)} is untouched for ${tc.days} day${tc.days === 1 ? '' : 's'}`;
      if (tc.status?.length) when += ` (status: ${tc.status.join(', ')})`;
      break;
    case 'stage_entered':
      when = `an opportunity enters ${stageName(pipelines, tc.stage_id)}`;
      break;
    case 'task_overdue':
      when = `a task is overdue by ${tc.days} day${tc.days === 1 ? '' : 's'}`;
      break;
    case 'record_created':
      when = `${entityA(rule.entity_type)} is created`;
      break;
    default:
      when = rule.trigger_type;
  }
  let then;
  const who = (spec) => (!spec || spec === 'owner' ? 'the owner' : userName(users, spec));
  switch (rule.action_type) {
    case 'create_task':
      then = `create task “${ac.name}” for ${who(ac.assignee)}`;
      if (ac.due_in_days !== undefined && ac.due_in_days !== null && ac.due_in_days !== '')
        then += `, due in ${ac.due_in_days} day${Number(ac.due_in_days) === 1 ? '' : 's'}`;
      break;
    case 'notify':
      then = `notify ${who(ac.recipient)}: “${ac.message}”`;
      break;
    case 'add_tag':
      then = `add tag “${ac.tag}”`;
      break;
    default:
      then = rule.action_type;
  }
  return `When ${when} → ${then}`;
}

const EMPTY_FORM = {
  entity_type: 'lead',
  trigger_type: 'stale_record',
  days: '14',
  pipeline_id: '',
  stage_id: '',
  status: [],
  action_type: 'create_task',
  task_name: 'Follow up with {name}',
  due_in_days: '3',
  assignee: 'owner',
  recipient: 'owner',
  message: '',
  tag: '',
  name: '',
};

function buildPayload(v) {
  const trigger_config = {};
  if (v.trigger_type === 'stale_record' || v.trigger_type === 'task_overdue')
    trigger_config.days = Number(v.days);
  if (v.trigger_type === 'stale_record' && v.status.length) trigger_config.status = v.status;
  if (v.trigger_type === 'stage_entered') trigger_config.stage_id = v.stage_id;
  const action_config = {};
  if (v.action_type === 'create_task') {
    action_config.name = v.task_name;
    if (v.due_in_days !== '') action_config.due_in_days = Number(v.due_in_days);
    action_config.assignee = v.assignee;
  } else if (v.action_type === 'notify') {
    action_config.recipient = v.recipient;
    action_config.message = v.message;
  } else if (v.action_type === 'add_tag') {
    action_config.tag = v.tag;
  }
  return {
    name: v.name,
    entity_type: v.entity_type,
    trigger_type: v.trigger_type,
    trigger_config,
    action_type: v.action_type,
    action_config,
  };
}

function RuleWizard({ initial, users, pipelines, onClose, onCreated }) {
  const [v, setV] = useState({ ...EMPTY_FORM, ...(initial || {}) });
  const [nameEdited, setNameEdited] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  const triggers = TRIGGER_OPTIONS.filter((t) => t.entities.includes(v.entity_type));
  const actions = ACTION_OPTIONS.filter((a) => a.entities.includes(v.entity_type));
  const statusOptions =
    v.entity_type === 'lead' ? LEAD_STATUSES : v.entity_type === 'opportunity' ? OPPORTUNITY_STATUSES : null;
  const stages = (pipelines || []).find((p) => p.id === v.pipeline_id)?.stages || [];

  const suggestedName = useMemo(() => {
    const preview = buildPayload({ ...v, name: '' });
    const s = ruleSummary(preview, users, pipelines);
    return s.length > 90 ? s.slice(0, 87) + '…' : s;
  }, [v, users, pipelines]);

  function set(key, value) {
    setV((prev) => {
      const next = { ...prev, [key]: value };
      if (key === 'entity_type') {
        // Keep dependent pickers coherent when the entity changes.
        if (!TRIGGER_OPTIONS.find((t) => t.value === next.trigger_type)?.entities.includes(value))
          next.trigger_type = TRIGGER_OPTIONS.find((t) => t.entities.includes(value))?.value || '';
        if (!ACTION_OPTIONS.find((a) => a.value === next.action_type)?.entities.includes(value))
          next.action_type = 'create_task';
        next.status = [];
      }
      return next;
    });
  }

  async function submit(e) {
    e.preventDefault();
    setBusy(true);
    setError('');
    try {
      const payload = buildPayload(v);
      payload.name = (nameEdited && v.name.trim()) || suggestedName;
      await post('/automations', payload);
      onCreated();
    } catch (err) {
      setError(err.message);
      setBusy(false);
    }
  }

  return (
    <Modal title="New automation" onClose={onClose}>
      <form className="form" onSubmit={submit}>
        <div className="auto-wizard-step">
          <h4 className="panel-title">When…</h4>
          <div className="auto-wizard-row">
            <label className="field">
              <span>Record type</span>
              <select value={v.entity_type} onChange={(e) => set('entity_type', e.target.value)}>
                {ENTITY_OPTIONS.map((o) => (
                  <option key={o.value} value={o.value}>{o.label}</option>
                ))}
              </select>
            </label>
            <label className="field">
              <span>Trigger</span>
              <select value={v.trigger_type} onChange={(e) => set('trigger_type', e.target.value)}>
                {triggers.map((t) => (
                  <option key={t.value} value={t.value}>{t.label}</option>
                ))}
              </select>
            </label>
          </div>
          {(v.trigger_type === 'stale_record' || v.trigger_type === 'task_overdue') && (
            <label className="field auto-days">
              <span>{v.trigger_type === 'stale_record' ? 'Untouched for (days)' : 'Overdue by (days)'}</span>
              <input
                type="number" min="1" max="3650" step="1" required
                value={v.days}
                onChange={(e) => set('days', e.target.value)}
              />
            </label>
          )}
          {v.trigger_type === 'stage_entered' && (
            <div className="auto-wizard-row">
              <label className="field">
                <span>Pipeline</span>
                <select value={v.pipeline_id} onChange={(e) => { set('pipeline_id', e.target.value); set('stage_id', ''); }} required>
                  <option value="">Select…</option>
                  {(pipelines || []).map((p) => (
                    <option key={p.id} value={p.id}>{p.name}</option>
                  ))}
                </select>
              </label>
              <label className="field">
                <span>Stage</span>
                <select value={v.stage_id} onChange={(e) => set('stage_id', e.target.value)} required disabled={!v.pipeline_id}>
                  <option value="">Select…</option>
                  {stages.map((s) => (
                    <option key={s.id} value={s.id}>{s.name}</option>
                  ))}
                </select>
              </label>
            </div>
          )}
          {v.trigger_type === 'stale_record' && statusOptions && (
            <div className="field">
              <span>Only these statuses <span className="muted">(optional — empty = any)</span></span>
              <div className="auto-status-checks">
                {statusOptions.map((s) => (
                  <label key={s.value} className="cf-checkbox">
                    <input
                      type="checkbox"
                      checked={v.status.includes(s.value)}
                      onChange={(e) =>
                        set('status', e.target.checked ? [...v.status, s.value] : v.status.filter((x) => x !== s.value))
                      }
                    />
                    <span>{s.label}</span>
                  </label>
                ))}
              </div>
            </div>
          )}
        </div>

        <div className="auto-wizard-step">
          <h4 className="panel-title">Then…</h4>
          <label className="field">
            <span>Action</span>
            <select value={v.action_type} onChange={(e) => set('action_type', e.target.value)}>
              {actions.map((a) => (
                <option key={a.value} value={a.value}>{a.label}</option>
              ))}
            </select>
          </label>
          {v.action_type === 'create_task' && (
            <>
              <label className="field">
                <span>Task name <span className="muted">— {'{name}'} inserts the record’s name</span></span>
                <input value={v.task_name} onChange={(e) => set('task_name', e.target.value)} required />
              </label>
              <div className="auto-wizard-row">
                <label className="field">
                  <span>Due in (days)</span>
                  <input type="number" min="0" max="3650" step="1" value={v.due_in_days} onChange={(e) => set('due_in_days', e.target.value)} />
                </label>
                <label className="field">
                  <span>Assign to</span>
                  <select value={v.assignee} onChange={(e) => set('assignee', e.target.value)}>
                    <option value="owner">Record owner</option>
                    {(users || []).map((u) => (
                      <option key={u.id} value={u.id}>{u.display_name}</option>
                    ))}
                  </select>
                </label>
              </div>
            </>
          )}
          {v.action_type === 'notify' && (
            <>
              <label className="field">
                <span>Notify</span>
                <select value={v.recipient} onChange={(e) => set('recipient', e.target.value)}>
                  <option value="owner">Record owner</option>
                  {(users || []).map((u) => (
                    <option key={u.id} value={u.id}>{u.display_name}</option>
                  ))}
                </select>
              </label>
              <label className="field">
                <span>Message <span className="muted">— {'{name}'} inserts the record’s name</span></span>
                <textarea rows={2} value={v.message} onChange={(e) => set('message', e.target.value)} required />
              </label>
              <p className="muted auto-hint">
                Delivered the same way as task reminders: app push or Colloqui DM, per each person’s
                notification preference.
              </p>
            </>
          )}
          {v.action_type === 'add_tag' && (
            <label className="field">
              <span>Tag</span>
              <input value={v.tag} onChange={(e) => set('tag', e.target.value)} placeholder="needs-follow-up" required />
            </label>
          )}
        </div>

        <label className="field">
          <span>Automation name</span>
          <input
            value={nameEdited ? v.name : suggestedName}
            onChange={(e) => { setNameEdited(true); set('name', e.target.value); }}
          />
        </label>

        {error && <div className="form-error">{error}</div>}
        <div className="form-actions">
          <button type="button" className="btn" onClick={onClose}>Cancel</button>
          <button type="submit" className="btn btn-primary" disabled={busy}>
            {busy ? 'Creating…' : 'Create automation'}
          </button>
        </div>
      </form>
    </Modal>
  );
}

function FiresLog({ ruleId }) {
  const toast = useToast();
  const [fires, setFires] = useState(null);

  useEffect(() => {
    let on = true;
    get(`/automations/${ruleId}/fires`, { limit: 50 })
      .then((d) => on && setFires(d.items || []))
      .catch((e) => {
        toast.error(e.message);
        if (on) setFires([]);
      });
    return () => { on = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ruleId]);

  if (fires === null) return <Loading small />;
  if (!fires.length) return <div className="muted panel-empty">Hasn’t fired yet.</div>;
  return (
    <div className="auto-fires">
      {fires.map((f) => (
        <div key={f.id} className="auto-fire-row">
          <span className="auto-fire-when muted">{fmtDateTime(f.fired_at)}</span>
          <span className="auto-fire-label">{f.entity_label}</span>
          <span className="muted auto-fire-detail">
            {f.detail.action === 'create_task' && `→ task “${f.detail.task_name}”${f.detail.assignee ? ` for ${f.detail.assignee}` : ''}`}
            {f.detail.action === 'notify' &&
              `→ ${f.detail.delivered ? `notified ${f.detail.recipient || '?'} via ${f.detail.delivered}` : `notification to ${f.detail.recipient || '?'} could not be delivered`}`}
            {f.detail.action === 'add_tag' && `→ tagged “${f.detail.tag}”`}
          </span>
        </div>
      ))}
    </div>
  );
}

export default function AutomationsSection() {
  const toast = useToast();
  const [rules, setRules] = useState(null);
  const [users, setUsers] = useState([]);
  const [pipelines, setPipelines] = useState([]);
  const [wizard, setWizard] = useState(null); // null | {initial values}
  const [expanded, setExpanded] = useState(null);
  const [busy, setBusy] = useState(false);
  const [version, setVersion] = useState(0);

  useEffect(() => {
    let on = true;
    get('/automations')
      .then((d) => on && setRules(d.items || []))
      .catch((e) => {
        toast.error(e.message);
        if (on) setRules([]);
      });
    cachedGet('/users').then((d) => on && setUsers(d?.items || [])).catch(() => {});
    cachedGet('/pipelines').then((d) => on && setPipelines(Array.isArray(d) ? d : [])).catch(() => {});
    return () => { on = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [version]);

  async function toggle(rule) {
    try {
      await patch(`/automations/${rule.id}`, { enabled: !rule.enabled });
      setRules((rs) => rs.map((r) => (r.id === rule.id ? { ...r, enabled: !r.enabled } : r)));
    } catch (e) {
      toast.error(e.message);
    }
  }

  async function remove(rule) {
    if (!window.confirm(`Delete automation "${rule.name}"? Its fire history is removed too.`)) return;
    try {
      await del(`/automations/${rule.id}`);
      setRules((rs) => rs.filter((r) => r.id !== rule.id));
      toast.success('Automation deleted');
    } catch (e) {
      toast.error(e.message);
    }
  }

  async function runNow() {
    setBusy(true);
    try {
      const res = await post('/automations/run-now');
      toast.success(res.fired === 0 ? 'Sweep ran — nothing to fire' : `Sweep ran — ${res.fired} automation fire${res.fired === 1 ? '' : 's'}`);
      setVersion((x) => x + 1);
      setExpanded(null);
    } catch (e) {
      toast.error(e.message);
    }
    setBusy(false);
  }

  return (
    <div className="card settings-card">
      <div className="panel-head">
        <h3>Automations</h3>
        <div className="auto-head-actions">
          <button className="btn btn-small" onClick={runNow} disabled={busy || rules === null}>
            {busy ? 'Running…' : 'Run now'}
          </button>
          <button className="btn btn-primary btn-small" onClick={() => setWizard({})}>
            + New automation
          </button>
        </div>
      </div>
      <p className="muted">
        Rules that keep the CRM proactive — they sweep every 5 minutes and each rule acts once per
        record until the record changes again. “Run now” sweeps immediately.
      </p>
      {rules === null ? (
        <Loading small />
      ) : rules.length === 0 ? (
        <div className="auto-templates">
          <p className="muted">No automations yet. Start from a template:</p>
          <div className="auto-template-btns">
            {TEMPLATES.map((t) => (
              <button key={t.label} className="btn btn-small" onClick={() => setWizard(t.values)}>
                {t.label}
              </button>
            ))}
          </div>
        </div>
      ) : (
        <div className="toggle-rows">
          {rules.map((r) => (
            <ToggleListRow
              key={r.id}
              enabled={r.enabled}
              onToggle={() => toggle(r)}
              switchTitle={r.enabled ? 'Enabled — click to pause' : 'Paused — click to enable'}
              title={r.name}
              badge={!r.enabled && <span className="badge badge-muted">Paused</span>}
              subtitle={<div className="muted auto-summary">{ruleSummary(r, users, pipelines)}</div>}
              meta={
                <>
                  {r.fire_count} fire{r.fire_count === 1 ? '' : 's'}
                  {r.last_fired_at ? ` · last ${fmtDateTime(r.last_fired_at)}` : ''}
                </>
              }
              metaTitle="Show recent fires"
              expanded={expanded === r.id}
              onToggleExpand={() => setExpanded(expanded === r.id ? null : r.id)}
              onDelete={() => remove(r)}
              deleteTitle="Delete automation"
            >
              <FiresLog ruleId={r.id} />
            </ToggleListRow>
          ))}
        </div>
      )}
      {wizard && (
        <RuleWizard
          initial={wizard}
          users={users}
          pipelines={pipelines}
          onClose={() => setWizard(null)}
          onCreated={() => {
            setWizard(null);
            setVersion((x) => x + 1);
            toast.success('Automation created');
          }}
        />
      )}
    </div>
  );
}
