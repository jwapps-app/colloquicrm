"""Trash retention: records soft-deleted past the configured retention window are
permanently purged. This is the ONLY path that removes trashed records — there
is no manual 'empty trash', so anything deleted has the full window to be
noticed as missing and restored."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import timedelta

from sqlalchemy import delete, select

from app.config import settings
from app.db import SessionLocal
from app.services.attachments import unlink_stored
from app.services.common import prune_orphan_tags, remove_entity_satellites
from app.models import (
    Company,
    Lead,
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


async def purge_expired_trash(
    _after_select: Callable[[str, list], Awaitable[None]] | None = None,
) -> int:
    """Permanently delete records trashed longer ago than the retention window.

    The candidate ids come from a plain SELECT, and a user can restore one of
    them at any moment after it. So the SELECT decides nothing: per chunk, the
    parents are deleted FIRST, by a statement that re-states the whole
    condition (still trashed, still past the cutoff) and RETURNs the ids it
    actually removed. Satellites and attachment files are then removed for
    exactly those ids, in the same transaction. A record restored in the gap
    fails the condition, is not returned, and keeps everything it owns. A
    restore that arrives mid-DELETE waits on the row lock and then finds no
    row — restore_item's conditional UPDATE reports that as a 404, never as a
    success for a record that is gone.

    `_after_select(entity_type, ids)` is a seam for tests to act in that gap."""
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
            # End the read transaction: the snapshot above must not outlive
            # its usefulness, and each chunk below is its own transaction.
            await db.rollback()
            if not ids:
                continue
            if _after_select is not None:
                await _after_select(entity_type, ids)
            gone_total = 0
            for i in range(0, len(ids), 500):
                chunk = ids[i : i + 500]
                gone = [
                    rid
                    for (rid,) in await db.execute(
                        delete(model)
                        .where(
                            model.id.in_(chunk),
                            model.deleted_at.is_not(None),
                            model.deleted_at < cutoff,
                        )
                        .returning(model.id)
                    )
                ]
                # Attachment rows go with the purge; the files are unlinked
                # only after the commit lands (missing files tolerated).
                stored = await remove_entity_satellites(db, entity_type, gone)
                await db.commit()
                unlink_stored(stored)
                gone_total += len(gone)
            purged += gone_total
            if gone_total != len(ids):
                log.info(
                    "%s %s records left the trash before they could be purged",
                    len(ids) - gone_total, entity_type,
                )
            log.info("purged %s expired %s records from trash", gone_total, entity_type)
        if purged:
            # Purging removed tag links across orgs — sweep out unused tags.
            await prune_orphan_tags(db)
            await db.commit()
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
