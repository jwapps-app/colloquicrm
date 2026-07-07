import { useEffect, useState } from 'react';
import { get } from '../api';
import InlineField from './InlineField';
import { fmtDate } from '../format';

const INPUT_TYPE = { number: 'number', currency: 'number', date: 'date' };

export default function CustomFieldsPanel({ entityType, entity, onSave }) {
  const [defs, setDefs] = useState(null);

  useEffect(() => {
    get('/custom-fields', { entity_type: entityType })
      .then((d) => setDefs(Array.isArray(d) ? [...d].sort((a, b) => (a.position ?? 0) - (b.position ?? 0)) : []))
      .catch(() => setDefs([]));
  }, [entityType]);

  if (!defs || defs.length === 0) return null;

  const values = entity.custom_fields || {};
  const save = (fieldId, value) => onSave({ custom_fields: { ...values, [fieldId]: value } });

  return (
    <div className="card">
      <h4 className="panel-title">Custom fields</h4>
      {defs.map((d) => (
        <div className="profile-row" key={d.id}>
          <div className="profile-label">{d.name}</div>
          {d.field_type === 'checkbox' ? (
            <label className="cf-checkbox">
              <input type="checkbox" checked={!!values[d.id]} onChange={(e) => save(d.id, e.target.checked)} />
            </label>
          ) : d.field_type === 'select' ? (
            <select className="inline-select" value={values[d.id] ?? ''} onChange={(e) => save(d.id, e.target.value || null)}>
              <option value="">—</option>
              {(d.options || []).map((o) => (
                <option key={o} value={o}>
                  {o}
                </option>
              ))}
            </select>
          ) : (
            <InlineField
              value={values[d.id]}
              type={INPUT_TYPE[d.field_type] || 'text'}
              render={d.field_type === 'date' ? (v) => (v ? fmtDate(v) : '') : undefined}
              onSave={(v) => save(d.id, v)}
            />
          )}
        </div>
      ))}
    </div>
  );
}
