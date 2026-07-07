import InlineField from './InlineField';
import { fmtDate } from '../format';

/**
 * Left-column profile panel: every field is click-to-edit and PATCHes via onSave.
 */
export default function ProfilePanel({ entity, fields, onSave, users = [] }) {
  return (
    <div className="card profile">
      <h4 className="panel-title">Details</h4>
      {fields.map((f) => {
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
              value={entity[f.key]}
              type={type}
              options={options}
              render={render}
              onSave={(v) => onSave({ [f.key]: v })}
            />
          </div>
        );
      })}
    </div>
  );
}
