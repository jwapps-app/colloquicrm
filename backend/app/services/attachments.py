"""Disk storage for file attachments. Rows (metadata) live in the attachments
table; bytes live at {attachments_dir}/{stored_name}. The stored name is
always server-generated (uuid + sanitized extension of the original name), so
a client filename can never influence the on-disk path.

Deleting rows and deleting files are separate steps on purpose: callers
collect stored names first, remove the rows inside the transaction, and only
unlink the files once the commit lands — a rollback must not leave rows
pointing at bytes that are already gone."""

import logging
import os
import re
import uuid
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Attachment

log = logging.getLogger("attachments")

# Conservative: an extension is cosmetic (it keeps downloads openable), so
# anything that isn't a short alphanumeric suffix is simply dropped.
_EXT_RE = re.compile(r"^\.[A-Za-z0-9]{1,12}$")


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
