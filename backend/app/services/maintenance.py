"""Trash retention: records soft-deleted past the configured retention window are
permanently purged. This is the ONLY path that removes trashed records — there
is no manual 'empty trash', so anything deleted has the full window to be
noticed as missing and restored."""

import asyncio
import logging
from datetime import timedelta

from sqlalchemy import delete, select

from app.config import settings
from app.db import SessionLocal
from app.models import (
    Company,
    CustomFieldValue,
    EntityTag,
    Lead,
    Note,
    Opportunity,
    Person,
    utcnow,
)

log = logging.getLogger("maintenance")

PURGE_INTERVAL_SECONDS = 86400  # daily

# (model, entity_type used by the polymorphic satellites)
_TRASH_MODELS = [
    (Person, "person"),
    (Lead, "lead"),
    (Company, "company"),
    (Opportunity, "opportunity"),
]


async def purge_expired_trash() -> int:
    cutoff = utcnow() - timedelta(days=settings.trash_retention_days)
    purged = 0
    async with SessionLocal() as db:
        for model, entity_type in _TRASH_MODELS:
            ids = [
                rid
                for (rid,) in await db.execute(
                    select(model.id).where(
                        model.deleted_at.is_not(None), model.deleted_at < cutoff
                    )
                )
            ]
            if not ids:
                continue
            for i in range(0, len(ids), 500):
                chunk = ids[i : i + 500]
                for sat, type_col, id_col in (
                    (EntityTag, EntityTag.entity_type, EntityTag.entity_id),
                    (CustomFieldValue, CustomFieldValue.entity_type, CustomFieldValue.entity_id),
                    (Note, Note.entity_type, Note.entity_id),
                ):
                    await db.execute(
                        delete(sat).where(type_col == entity_type, id_col.in_(chunk))
                    )
                await db.execute(delete(model).where(model.id.in_(chunk)))
                await db.commit()
            purged += len(ids)
            log.info("purged %s expired %s records from trash", len(ids), entity_type)
    return purged


async def purge_loop() -> None:
    while True:
        try:
            await purge_expired_trash()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("trash purge pass failed")
        await asyncio.sleep(PURGE_INTERVAL_SECONDS)
