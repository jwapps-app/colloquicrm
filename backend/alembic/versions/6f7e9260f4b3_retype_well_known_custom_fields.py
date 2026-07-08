"""retype well-known custom fields

Imported Copper custom fields all arrived as plain text. Birthdate-style
fields become real date fields (values converted to ISO so the date picker
reads them), and Gender becomes a Male/Female dropdown (values normalized to
match the options; any unexpected values are kept and added as options so
nothing disappears).

Revision ID: 6f7e9260f4b3
Revises: 2d5215e67808
Create Date: 2026-07-07 19:21:27.756962

"""
from datetime import datetime

from alembic import op
import sqlalchemy as sa


revision = '6f7e9260f4b3'
down_revision = '2d5215e67808'
branch_labels = None
depends_on = None

DATE_FIELD_NAMES = ('birthdate', 'birthday', 'date of birth', 'anniversary')

cf = sa.table(
    'custom_fields',
    sa.column('id', sa.Uuid()),
    sa.column('name', sa.String()),
    sa.column('field_type', sa.String()),
    sa.column('options', sa.JSON()),
)
cfv = sa.table(
    'custom_field_values',
    sa.column('field_id', sa.Uuid()),
    sa.column('entity_id', sa.Uuid()),
    sa.column('value', sa.Text()),
)


def _parse_date(raw: str) -> str | None:
    raw = (raw or '').strip()
    for fmt in ('%m/%d/%Y', '%m/%d/%y', '%Y-%m-%d'):
        try:
            return datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def upgrade() -> None:
    bind = op.get_bind()

    date_fields = bind.execute(
        sa.select(cf.c.id).where(
            sa.func.lower(cf.c.name).in_(DATE_FIELD_NAMES), cf.c.field_type == 'text'
        )
    ).fetchall()
    for (fid,) in date_fields:
        bind.execute(sa.update(cf).where(cf.c.id == fid).values(field_type='date'))
        values = bind.execute(
            sa.select(cfv.c.entity_id, cfv.c.value).where(cfv.c.field_id == fid)
        ).fetchall()
        for eid, val in values:
            parsed = _parse_date(val)
            if parsed and parsed != val:
                bind.execute(
                    sa.update(cfv)
                    .where(cfv.c.field_id == fid, cfv.c.entity_id == eid)
                    .values(value=parsed)
                )

    gender_fields = bind.execute(
        sa.select(cf.c.id).where(
            sa.func.lower(cf.c.name) == 'gender', cf.c.field_type == 'text'
        )
    ).fetchall()
    for (fid,) in gender_fields:
        options = ['Male', 'Female']
        distinct = bind.execute(
            sa.select(cfv.c.value).distinct().where(cfv.c.field_id == fid)
        ).fetchall()
        for (val,) in distinct:
            if not val:
                continue
            canon = val.strip().title()
            if canon in options:
                if val != canon:
                    bind.execute(
                        sa.update(cfv)
                        .where(cfv.c.field_id == fid, cfv.c.value == val)
                        .values(value=canon)
                    )
            elif val not in options:
                options.append(val)
        bind.execute(
            sa.update(cf).where(cf.c.id == fid).values(field_type='select', options=options)
        )


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(
        sa.update(cf)
        .where(sa.func.lower(cf.c.name).in_(DATE_FIELD_NAMES), cf.c.field_type == 'date')
        .values(field_type='text')
    )
    bind.execute(
        sa.update(cf)
        .where(sa.func.lower(cf.c.name) == 'gender', cf.c.field_type == 'select')
        .values(field_type='text', options=None)
    )
