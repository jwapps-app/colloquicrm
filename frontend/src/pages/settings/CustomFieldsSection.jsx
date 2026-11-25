import { useEffect, useState } from 'react';
import { bustCache, del, get, patch, post } from '../../api';
import { useAuth } from '../../auth';
import { useToast } from '../../components/Toast';
import FormModal from '../../components/FormModal';
import InlineField from '../../components/InlineField';
import { Loading } from '../../components/ui';
import { CF_ENTITY_TYPES, CUSTOM_FIELD_TYPES } from '../../constants/options';

/* ---------- Custom fields ---------- */

export default function CustomFieldsSection() {
  const { user } = useAuth();
  const isAdmin = !!user?.is_admin;
  const toast = useToast();
  const [entityType, setEntityType] = useState('person');
  const [fields, setFields] = useState(null);
  const [showAdd, setShowAdd] = useState(false);
  const [version, setVersion] = useState(0);

  useEffect(() => {
    let on = true;
    setFields(null);
    get('/custom-fields', { entity_type: entityType })
      .then((d) => {
        if (on) setFields(Array.isArray(d) ? d : []);
      })
      .catch((e) => {
        toast.error(e.message);
        if (on) setFields([]);
      });
    return () => {
      on = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [entityType, version]);

  async function addField(values) {
    const body = {
      entity_type: entityType,
      name: values.name,
      field_type: values.field_type,
      options:
        values.field_type === 'select'
          ? String(values.options || '')
              .split(',')
              .map((s) => s.trim())
              .filter(Boolean)
          : null,
    };
    await post('/custom-fields', body);
    bustCache('/custom-fields');
    setShowAdd(false);
    setVersion((v) => v + 1);
    toast.success('Custom field added');
  }

  async function rename(f, name) {
    try {
      await patch(`/custom-fields/${f.id}`, { name });
      bustCache('/custom-fields');
      setVersion((v) => v + 1);
    } catch (e) {
      toast.error(e.message);
    }
  }

  async function changeType(f, field_type) {
    try {
      await patch(`/custom-fields/${f.id}`, { field_type });
      bustCache('/custom-fields');
      setVersion((v) => v + 1);
      toast.success(field_type === 'date' ? 'Type changed — existing values converted to dates' : 'Type changed');
    } catch (e) {
      toast.error(e.message);
    }
  }

  async function changeOptions(f, raw) {
    const options = String(raw || '').split(',').map((s) => s.trim()).filter(Boolean);
    try {
      await patch(`/custom-fields/${f.id}`, { options });
      bustCache('/custom-fields');
      setVersion((v) => v + 1);
    } catch (e) {
      toast.error(e.message);
    }
  }

  async function remove(f) {
    if (!window.confirm(`Delete custom field "${f.name}"? Values on records will be lost.`)) return;
    try {
      await del(`/custom-fields/${f.id}`);
      bustCache('/custom-fields');
      setVersion((v) => v + 1);
    } catch (e) {
      toast.error(e.message);
    }
  }

  return (
    <div className="card settings-card">
      <div className="panel-head">
        <h3>Custom fields</h3>
        {isAdmin && (
          <button className="btn btn-primary btn-small" onClick={() => setShowAdd(true)}>
            + Add field
          </button>
        )}
      </div>
      {!isAdmin && (
        <p className="muted">Custom fields are org-wide — ask an administrator to change them.</p>
      )}
      <div className="tabs">
        {CF_ENTITY_TYPES.map((t) => (
          <button key={t.value} className={'tab' + (entityType === t.value ? ' active' : '')} onClick={() => setEntityType(t.value)}>
            {t.label}
          </button>
        ))}
      </div>
      {fields === null ? (
        <Loading small />
      ) : fields.length === 0 ? (
        <div className="muted panel-empty">No custom fields for {entityType} records yet.</div>
      ) : (
        <div className="cf-list">
          {fields.map((f) => (
            <div key={f.id} className="cf-row">
              <div className="cf-name">
                {isAdmin ? <InlineField value={f.name} onSave={(v) => v && rename(f, v)} /> : f.name}
              </div>
              {isAdmin ? (
                <select
                  className="inline-select cf-type"
                  value={f.field_type}
                  onChange={(e) => changeType(f, e.target.value)}
                  title="Field type"
                >
                  {CUSTOM_FIELD_TYPES.map((t) => (
                    <option key={t.value} value={t.value}>
                      {t.label}
                    </option>
                  ))}
                </select>
              ) : (
                <span className="muted cf-type">{f.field_type}</span>
              )}
              {f.field_type === 'select' && (
                <span className="cf-options">
                  {isAdmin ? (
                    <InlineField
                      value={(f.options || []).join(', ')}
                      onSave={(v) => changeOptions(f, v)}
                    />
                  ) : (
                    <span className="muted">{(f.options || []).join(', ')}</span>
                  )}
                </span>
              )}
              {isAdmin && (
                <button className="icon-btn tiny" onClick={() => remove(f)} title="Delete field" aria-label="Delete field">
                  ×
                </button>
              )}
            </div>
          ))}
        </div>
      )}
      {showAdd && (
        <FormModal
          title="Add custom field"
          submitLabel="Create"
          onClose={() => setShowAdd(false)}
          onSubmit={addField}
          fields={[
            { key: 'name', label: 'Name', required: true },
            { key: 'field_type', label: 'Type', type: 'select', options: CUSTOM_FIELD_TYPES, required: true, default: 'text' },
            {
              key: 'options',
              label: 'Options (comma-separated)',
              show: (v) => v.field_type === 'select',
              placeholder: 'Red, Green, Blue',
            },
          ]}
        />
      )}
    </div>
  );
}
