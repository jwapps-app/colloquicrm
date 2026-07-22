import os
import uuid

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import select
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

router = APIRouter()

_CHUNK = 1024 * 1024


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

    filename = sanitize_filename(file.filename or "")
    stored_name = make_stored_name(filename)
    os.makedirs(settings.attachments_dir, exist_ok=True)
    dest = attachment_path(stored_name)

    # Stream to disk with a running byte count — never the whole body in
    # memory. Blowing the cap deletes the partial file and 413s.
    max_bytes = settings.attachment_max_mb * 1024 * 1024
    size = 0
    try:
        with open(dest, "wb") as out:
            while chunk := await file.read(_CHUNK):
                size += len(chunk)
                if size > max_bytes:
                    raise HTTPException(
                        status_code=413,
                        detail=f"File exceeds the {settings.attachment_max_mb} MB limit",
                    )
                out.write(chunk)
    except BaseException:
        dest.unlink(missing_ok=True)
        raise

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
    try:
        result = (await _serialize(db, user.org_id, [a]))[0]
        await db.commit()  # visible before the client refetches
    except BaseException:
        # The row won't land — don't leave orphaned bytes behind.
        dest.unlink(missing_ok=True)
        raise
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
    # when the original name isn't plain ASCII.
    return FileResponse(path, media_type=a.content_type, filename=a.filename)


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
    unlink_stored([stored_name])
