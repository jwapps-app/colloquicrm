import { useEffect, useMemo, useState } from 'react';
import { get } from '../api';
import InlineField from './InlineField';
import { fmtDate } from '../format';

const CF_INPUT_TYPE = { number: 'number', currency: 'number', date: 'date' };

// Custom fields slot in right after the name block — a Nickname or Spouse
// belongs with the name, not in a separate section at the bottom.
const NAME_KEYS = ['suffix', 'last_name', 'middle_name', 'first_name', 'name'];

function isFilled(value) {
  return value !== null && value !== undefined && value !== '' && value !== false;
}

/**
 * Left-column profile panel: standard + custom fields in one click-to-edit
 * list. Only filled fields show by default; a toggle reveals the empty ones.
 */
export default function ProfilePanel({ entity, entityType, fields, onSave, users = [] }) {
  const [defs, setDefs] = useState([]);
  const [showEmpty, setShowEmpty] = useState(false);

  useEffect(() => {
    if (!entityType) return;
    get('/custom-fields', { entity_type: entityType })
      .then((d) =>
        setDefs(Array.isArray(d) ? [...d].sort((a, b) => (a.position ?? 0) - (b.position ?? 0)) : [])
      )
      .catch(() => setDefs([]));
  }, [entityType]);

  const merged = useMemo(() => {
    const customs = defs.map((d) => ({ key: `cf:${d.id}`, label: d.name, _def: d }));
    if (customs.length === 0) return fields;
    let insertAt = -1;
    for (const nk of NAME_KEYS) {
      const idx = fields.findIndex((f) => f.key === nk);
      if (idx !== -1) {
        insertAt = idx + 1;
        break;
      }
    }
    if (insertAt === -1) return [...fields, ...customs];
    return [...fields.slice(0, insertAt), ...customs, ...fields.slice(insertAt)];
  }, [fields, defs]);

  const cfValues = entity.custom_fields || {};
  const valueOf = (f) => (f._def ? cfValues[f._def.id] : entity[f.key]);

  const emptyCount = merged.filter((f) => !isFilled(valueOf(f))).length;
  const allEmpty = emptyCount === merged.length;
  const visible = showEmpty || allEmpty ? merged : merged.filter((f) => isFilled(valueOf(f)));

  const saveCustom = (def, value) => onSave({ custom_fields: { ...cfValues, [def.id]: value } });

  const renderRow = (f) => {
    const value = valueOf(f);

    if (f._def) {
      const d = f._def;
      let control;
      if (d.field_type === 'checkbox') {
        control = (
          <label className="cf-checkbox">
            <input type="checkbox" checked={!!value} onChange={(e) => saveCustom(d, e.target.checked)} />
          </label>
        );
      } else if (d.field_type === 'select') {
        control = (
          <select className="inline-select" value={value ?? ''} onChange={(e) => saveCustom(d, e.target.value || null)}>
            <option value="">—</option>
            {(d.options || []).map((o) => (
              <option key={o} value={o}>
                {o}
              </option>
            ))}
          </select>
        );
      } else {
        control = (
          <InlineField
            value={value}
            type={CF_INPUT_TYPE[d.field_type] || 'text'}
            render={d.field_type === 'date' ? (v) => (v ? fmtDate(v) : '') : undefined}
            onSave={(v) => saveCustom(d, v)}
          />
        );
      }
      return (
        <div className="profile-row" key={f.key}>
          <div className="profile-label">{f.label}</div>
          {control}
        </div>
      );
    }

    let type = f.type || 'text';
    let options = f.options;
    let render = f.render ? (v) => f.render(v, entity) : undefined;

    if (f.key === 'owner_id') {
      type = 'select';
      options = users.map((u) => ({ value: u.id, label: u.display_name || u.email }));
      render = (v) => {
        const u = users.find((x) => x.id === v);
        return u ? u.display_name || u.email : entity.owner_name || '';
      };
    } else if (type === 'date' && !render) {
      render = (v) => (v ? fmtDate(v) : '');
    }

    return (
      <div className="profile-row" key={f.key}>
        <div className="profile-label">{f.label}</div>
        <InlineField
          value={value}
          type={type}
          options={options}
          render={render}
          onSave={(v) => onSave({ [f.key]: v })}
        />
      </div>
    );
  };

  return (
    <div className="card profile">
      <h4 className="panel-title">Details</h4>
      {visible.map(renderRow)}
      {!allEmpty && emptyCount > 0 && (
        <button className="linklike profile-toggle" onClick={() => setShowEmpty((v) => !v)}>
          {showEmpty ? 'Hide empty fields' : `+ Show ${emptyCount} empty field${emptyCount === 1 ? '' : 's'}`}
        </button>
      )}
    </div>
  );
}
