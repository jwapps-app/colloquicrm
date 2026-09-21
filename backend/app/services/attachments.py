"""Disk storage for file attachments. Rows (metadata) live in the attachments
table; bytes live at {attachments_dir}/{stored_name}. The stored name is
always server-generated (uuid + sanitized extension of the original name), so
a client filename can never influence the on-disk path.

Deleting rows and deleting files are separate steps on purpose: callers
collect stored names first, remove the rows inside the transaction, and only
unlink the files once the commit lands — a rollback must not leave rows
pointing at bytes that are already gone."""

import asyncio
import logging
import os
import re
import time
import uuid
from pathlib import Path

import anyio
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import SessionLocal
from app.models import Attachment

log = logging.getLogger("attachments")

# Conservative: an extension is cosmetic (it keeps downloads openable), so
# anything that isn't a short alphanumeric suffix is simply dropped.
_EXT_RE = re.compile(r"^\.[A-Za-z0-9]{1,12}$")
# Exactly what make_stored_name produces. The reconciler only ever touches
# files of this shape, so pointing attachments_dir at a folder that holds
# anything else can't get that anything else deleted.
_STORED_NAME_RE = re.compile(r"^[0-9a-f]{32}(\.[a-z0-9]{1,12})?$")

RECONCILE_INTERVAL_SECONDS = 86400  # daily, and once at startup
# An upload writes its file first and commits the row a moment later — a file
# younger than this may simply be one of those in flight.
ORPHAN_MIN_AGE_SECONDS = 3600


def sanitize_filename(name: str) -> str:
    """Display-safe version of the client's filename: no path separators, no
    control characters, bounded length (the extension survives truncation)."""
    name = (name or "").replace("\\", "/")
    name = os.path.basename(name)
    name = "".join(c for c in name if c.isprintable()).strip().strip(".")
    if not name:
        return "file"
    if len(name) > 200:
        stem, ext = os.path.splitext(name)
        if not _EXT_RE.match(ext):
            ext = ""
        name = stem[: 200 - len(ext)] + ext
    return name


def make_stored_name(filename: str) -> str:
    """Fresh uuid plus the original extension (when it's a sane one)."""
    ext = os.path.splitext(filename)[1]
    if not _EXT_RE.match(ext):
        ext = ""
    return uuid.uuid4().hex + ext.lower()


def attachment_path(stored_name: str) -> Path:
    return Path(settings.attachments_dir) / stored_name


def unlink_stored(stored_names: list[str]) -> None:
    """Best-effort removal of attachment files after their rows are gone. A
    file already missing is fine (crash between commit and unlink, or a
    restored volume); anything else is logged, never raised — the rows are
    committed away and a 500 here would help nobody."""
    for name in stored_names:
        try:
            attachment_path(name).unlink(missing_ok=True)
        except OSError:
            log.warning("could not remove attachment file %s", name)


async def collect_stored_names(
    db: AsyncSession, entity_type: str, entity_ids: list[uuid.UUID]
) -> list[str]:
    """stored_names of every attachment on the given records — grab these
    BEFORE deleting the rows, unlink them after the commit."""
    if not entity_ids:
        return []
    rows = await db.execute(
        select(Attachment.stored_name).where(
            Attachment.entity_type == entity_type, Attachment.entity_id.in_(entity_ids)
        )
    )
    return [name for (name,) in rows]


async def delete_attachment_rows(
    db: AsyncSession, entity_type: str, entity_ids: list[uuid.UUID]
) -> None:
    if not entity_ids:
        return
    await db.execute(
        delete(Attachment).where(
            Attachment.entity_type == entity_type, Attachment.entity_id.in_(entity_ids)
        )
    )


def _old_stored_files(min_age_seconds: int) -> list[str]:
    cutoff = time.time() - min_age_seconds
    try:
        with os.scandir(settings.attachments_dir) as entries:
            return [
                e.name
                for e in entries
                if _STORED_NAME_RE.match(e.name)
                and e.is_file(follow_symlinks=False)
                and e.stat(follow_symlinks=False).st_mtime < cutoff
            ]
    except FileNotFoundError:
        return []  # nothing uploaded yet


async def reconcile_orphan_files(min_age_seconds: int = ORPHAN_MIN_AGE_SECONDS) -> int:
    """Delete files in attachments_dir that no Attachment row points at — the
    leftovers of an upload whose process died between the write and the
    commit (the request path cleans up after every failure it can see).
    Orphans are invisible to the SQL quota sum, so they'd otherwise pile up
    uncounted. Returns how many were removed."""
    names = await anyio.to_thread.run_sync(_old_stored_files, min_age_seconds)
    if not names:
        return 0
    known: set[str] = set()
    async with SessionLocal() as db:
        for i in range(0, len(names), 500):
            rows = await db.execute(
                select(Attachment.stored_name).where(
                    Attachment.stored_name.in_(names[i : i + 500])
                )
            )
            known.update(name for (name,) in rows)
    orphans = [n for n in names if n not in known]
    # Real orphans are a handful (a crash mid-upload). A directory where most
    # files have no row is far more likely a restore in progress — files
    # copied back before the database — or a wrong DATABASE_URL, and deleting
    # then would destroy the only copy. Leave it for a human.
    if orphans and (not known or (len(orphans) > 25 and len(orphans) * 2 > len(names))):
        log.warning(
            "%s of %s attachment files have no database row — not deleting; "
            "restore the matching database or clear them by hand",
            len(orphans),
            len(names),
        )
        return 0
    if orphans:
        await anyio.to_thread.run_sync(unlink_stored, orphans)
        log.info("removed %s orphaned attachment files", len(orphans))
    return len(orphans)


async def reconcile_loop() -> None:
    while True:
        try:
            await reconcile_orphan_files()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("attachment reconcile pass failed")
        await asyncio.sleep(RECONCILE_INTERVAL_SECONDS)
