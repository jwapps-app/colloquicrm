import asyncio
import os
import uuid
from functools import partial

import anyio
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import get_db
from app.deps import get_current_user
from app.models import Attachment, User
from app.services.attachments import (
    attachment_path,
    make_stored_name,
    sanitize_filename,
    unlink_stored,
)
from app.services.common import display_name_map, log_activity, validate_entity_ref
from app.services.throttle import SlidingWindowLimiter

router = APIRouter()

_CHUNK = 1024 * 1024

# Per-user upload rate — generous for a person attaching files by hand,
# tight for a script trying to churn through the org's storage budget.
# Same in-process limiter as login; counts accepted uploads. The slot is
# reserved before the write and handed back if the upload fails, so a parallel
# burst can't all pass the check before any of them is counted.
_UPLOAD_WINDOW_SECONDS = 3600
_UPLOAD_LIMIT = 60
_uploads = SlidingWindowLimiter(_UPLOAD_WINDOW_SECONDS, _UPLOAD_LIMIT)

# Bytes promised to uploads that are still being written, per org. The quota
# check counts committed rows PLUS these, so two parallel uploads can't both
# fit into the same remaining space. In-process like the limiter — this is a
# single-process app. The lock covers "read the committed sum + reserve" and
# "commit the row + release", so a finishing upload is always visible to the
# next check as one or the other, never as neither.
_reserved_bytes: dict[uuid.UUID, int] = {}
_ledger_lock = asyncio.Lock()


def _release_bytes(org_id: uuid.UUID, amount: int) -> None:
    left = _reserved_bytes.get(org_id, 0) - amount
    if left > 0:
        _reserved_bytes[org_id] = left
    else:
        _reserved_bytes.pop(org_id, None)


def _quota_bytes() -> int:
    return settings.attachment_quota_mb * 1024 * 1024


def _quota_label() -> str:
    mb = settings.attachment_quota_mb
    return f"{mb // 1024} GB" if mb % 1024 == 0 else f"{mb} MB"


def _quota_error() -> HTTPException:
    # 507 Insufficient Storage — deliberately NOT 413, which the clients
    # render as "file is too large" (the per-file cap).
    return HTTPException(
        status_code=507, detail=f"Attachment storage is full (limit {_quota_label()})"
    )


def _too_large_error() -> HTTPException:
    return HTTPException(
        status_code=413, detail=f"File exceeds the {settings.attachment_max_mb} MB limit"
    )


async def _org_bytes_used(db: AsyncSession, org_id) -> int:
    return (
        await db.execute(
            select(func.coalesce(func.sum(Attachment.size_bytes), 0)).where(
                Attachment.org_id == org_id
            )
        )
    ).scalar_one()


async def _serialize(db, org_id, items: list[Attachment]) -> list[dict]:
    # Deliberately not row_to_dict: stored_name is a server-internal disk key
    # and stays out of API responses.
    names = await display_name_map(db, {a.uploaded_by for a in items}, org_id)
    return [
        {
            "id": str(a.id),
            "filename": a.filename,
            "content_type": a.content_type,
            "size_bytes": a.size_bytes,
            "entity_type": a.entity_type,
            "entity_id": str(a.entity_id),
            "uploaded_by": str(a.uploaded_by) if a.uploaded_by else None,
            "uploaded_by_name": names.get(str(a.uploaded_by)) if a.uploaded_by else None,
            "created_at": a.created_at.isoformat(),
        }
        for a in items
    ]


async def _discard(path) -> None:
    # A partial or orphaned upload — remove it off the event loop; already
    # gone is fine.
    await anyio.to_thread.run_sync(partial(path.unlink, missing_ok=True))


async def _get_owned(db: AsyncSession, user: User, attachment_id: uuid.UUID) -> Attachment:
    a = (
        await db.execute(
            select(Attachment).where(
                Attachment.id == attachment_id, Attachment.org_id == user.org_id
            )
        )
    ).scalar_one_or_none()
    if a is None:
        raise HTTPException(status_code=404, detail="attachment not found")
    return a


@router.post("", status_code=201)
async def upload_attachment(
    file: UploadFile = File(...),
    entity_type: str = Form(...),
    entity_id: uuid.UUID = Form(...),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    # The target must be a real in-org record — same rule as notes and tasks.
    await validate_entity_ref(db, user.org_id, entity_type, entity_id)

    rate_keys = [f"upload:{user.id}"]
    slot = _uploads.reserve(rate_keys)
    if slot is None:
        raise HTTPException(
            status_code=429,
            detail="Too many uploads in the last hour — try again in a little while.",
        )

    max_bytes = settings.attachment_max_mb * 1024 * 1024
    quota = _quota_bytes()
    reserved = 0
    committed = False
    try:
        # Org storage budget. The multipart parser has already sized the part,
        # so an upload that can't fit is refused before a byte hits the disk;
        # its size is reserved against the quota until the row commits (or the
        # upload fails), and the running count below re-checks against what
        # actually arrived.
        if file.size and file.size > max_bytes:
            raise _too_large_error()
        async with _ledger_lock:
            used = await _org_bytes_used(db, user.org_id)
            want = file.size if file.size is not None else max_bytes
            if used + _reserved_bytes.get(user.org_id, 0) + want > quota:
                raise _quota_error()
            _reserved_bytes[user.org_id] = _reserved_bytes.get(user.org_id, 0) + want
            reserved = want

        filename = sanitize_filename(file.filename or "")
        stored_name = make_stored_name(filename)
        await anyio.to_thread.run_sync(
            partial(os.makedirs, settings.attachments_dir, exist_ok=True)
        )
        dest = attachment_path(stored_name)

        # Everything from the first byte on disk to the commit sits inside one
        # handler: ANY failure — the cap, a full disk, flush, log_activity,
        # serialization, the commit, a cancelled request — removes the file.
        # The row won't land, so the bytes must not stay behind uncounted.
        try:
            # Stream to disk with a running byte count — never the whole body
            # in memory. The disk writes themselves block, so they run on a
            # worker thread — a 25 MB upload to a slow volume must not stall
            # every other request.
            size = 0
            out = await anyio.to_thread.run_sync(partial(open, dest, "wb"))
            try:
                while chunk := await file.read(_CHUNK):
                    size += len(chunk)
                    if size > max_bytes:
                        raise _too_large_error()
                    if size > reserved:
                        # More arrived than the parser said — can't happen
                        # with a sized part, but never write past the promise.
                        raise _quota_error()
                    await anyio.to_thread.run_sync(out.write, chunk)
            finally:
                await anyio.to_thread.run_sync(out.close)

            a = Attachment(
                org_id=user.org_id,
                entity_type=entity_type,
                entity_id=entity_id,
                filename=filename,
                content_type=(file.content_type or "application/octet-stream")[:255],
                size_bytes=size,
                stored_name=stored_name,
                uploaded_by=user.id,
            )
            db.add(a)
            await db.flush()
            await log_activity(
                db, user.org_id, entity_type, entity_id, "attachment_added", user.id,
                {"filename": filename},
            )
            result = (await _serialize(db, user.org_id, [a]))[0]
            async with _ledger_lock:
                await db.commit()  # visible before the client refetches
                # The committed row carries the bytes from here on.
                _release_bytes(user.org_id, reserved)
                reserved = 0
            committed = True
        except BaseException:
            await _discard(dest)
            raise
    finally:
        _release_bytes(user.org_id, reserved)
        if not committed:
            # The limiter counts accepted uploads — hand the slot back.
            _uploads.release(rate_keys, slot)
    return result


@router.get("")
async def list_attachments(
    entity_type: str,
    entity_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    items = (
        (
            await db.execute(
                select(Attachment)
                .where(
                    Attachment.org_id == user.org_id,
                    Attachment.entity_type == entity_type,
                    Attachment.entity_id == entity_id,
                )
                .order_by(Attachment.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    return {"items": await _serialize(db, user.org_id, list(items))}


@router.get("/{attachment_id}/download")
async def download_attachment(
    attachment_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    a = await _get_owned(db, user, attachment_id)
    path = attachment_path(a.stored_name)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="attachment file is missing")
    # FileResponse emits Content-Disposition with an RFC 5987 filename* form
    # when the original name isn't plain ASCII. The stored bytes never change
    # (a new upload is a new id), so the browser may keep its copy for good;
    # private keeps it out of shared caches since the URL is authenticated.
    return FileResponse(
        path,
        media_type=a.content_type,
        filename=a.filename,
        headers={"Cache-Control": "private, max-age=31536000, immutable"},
    )


@router.delete("/{attachment_id}", status_code=204)
async def delete_attachment(
    attachment_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    a = await _get_owned(db, user, attachment_id)
    stored_name = a.stored_name
    await log_activity(
        db, user.org_id, a.entity_type, a.entity_id, "attachment_deleted", user.id,
        {"filename": a.filename},
    )
    await db.delete(a)
    await db.commit()  # visible before the client refetches
    # Row is gone for good — now the bytes. Missing file is fine.
    await anyio.to_thread.run_sync(unlink_stored, [stored_name])
