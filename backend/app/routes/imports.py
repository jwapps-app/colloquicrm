import uuid

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer

from app.db import get_db
from app.deps import get_current_user
from app.models import ImportJob, User
from app.schemas import ImportCommitIn
from app.services.background import spawn
from app.services.importer import IMPORT_TYPES, find_duplicates, parse_csv, run_import_job

router = APIRouter()

MAX_UPLOAD_BYTES = 50 * 1024 * 1024
# The commit payload is the preview rows round-tripped as JSON; the same byte
# bound as the CSV upload and a row ceiling well above any real Copper book.
MAX_COMMIT_BYTES = MAX_UPLOAD_BYTES
MAX_COMMIT_ROWS = 50_000


def _body_too_large(request: Request) -> bool:
    # Same shape as the public-form check: decide on Content-Length before
    # the body is read, so an oversized payload never gets parsed.
    raw = request.headers.get("content-length")
    if raw is None:
        return False
    try:
        return int(raw) > MAX_COMMIT_BYTES
    except ValueError:
        return True


@router.post("/preview")
async def preview_import(
    file: UploadFile = File(...),
    type: str = Form(...),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if type not in IMPORT_TYPES:
        raise HTTPException(
            status_code=422, detail=f"type must be one of {sorted(IMPORT_TYPES)}"
        )
    if file.size and file.size > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="File too large (50MB max)")
    content = await file.read()
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="File too large (50MB max)")
    rows, unmapped = parse_csv(content, type)
    if not rows:
        raise HTTPException(status_code=422, detail="No data rows found in the file")
    duplicates_found = await find_duplicates(db, user.org_id, type, rows)
    return {
        "type": type,
        "total": len(rows),
        "unmapped_headers": unmapped,
        "duplicates_found": duplicates_found,
        "rows": rows,
    }


@router.post("/commit", status_code=202)
async def commit_import(
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Stores the rows as a job and processes them in the background — a 19k-row
    import takes minutes, far past what a request (or the tunnel) will hold."""
    # Parsed by hand rather than as a `body:` parameter: FastAPI reads and
    # validates a body parameter before any dependency runs, which would put
    # the size check after the very parse it's meant to prevent.
    if _body_too_large(request):
        raise HTTPException(status_code=413, detail="Import payload too large (50MB max)")
    try:
        raw = await request.json()
    except ValueError:
        raise HTTPException(status_code=422, detail="Malformed JSON body")
    try:
        body = ImportCommitIn.model_validate(raw)
    except ValidationError as exc:
        # Re-raise in FastAPI's own shape so the client sees the usual 422.
        raise RequestValidationError(
            [{**e, "loc": ("body", *e["loc"])} for e in exc.errors(include_url=False)]
        )
    if len(body.rows) > MAX_COMMIT_ROWS:
        raise HTTPException(
            status_code=422,
            detail=f"Too many rows ({len(body.rows)}; {MAX_COMMIT_ROWS} max). Split the file.",
        )
    if body.type not in IMPORT_TYPES:
        raise HTTPException(
            status_code=422, detail=f"type must be one of {sorted(IMPORT_TYPES)}"
        )
    running = (
        await db.execute(
            select(ImportJob.id)
            .where(ImportJob.org_id == user.org_id, ImportJob.status == "running")
            .limit(1)
        )
    ).scalar_one_or_none()
    if running is not None:
        raise HTTPException(status_code=409, detail="An import is already running")
    job = ImportJob(
        org_id=user.org_id,
        user_id=user.id,
        import_type=body.type,
        payload=[r.model_dump(mode="json") for r in body.rows],
        total=len(body.rows),
    )
    db.add(job)
    await db.commit()  # the job must exist before the worker looks for it
    spawn(run_import_job(job.id))
    return {"job_id": str(job.id), "total": job.total}


@router.get("/jobs/{job_id}")
async def import_job_status(
    job_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    # The client polls this every second or two while the job runs, and the
    # payload is the whole import (megabytes of JSON) — leave it in the table.
    job = (
        await db.execute(
            select(ImportJob)
            .options(defer(ImportJob.payload))
            .where(ImportJob.id == job_id, ImportJob.org_id == user.org_id)
        )
    ).scalar_one_or_none()
    if job is None:
        raise HTTPException(status_code=404, detail="Import not found")
    return {
        "job_id": str(job.id),
        "status": job.status,
        "type": job.import_type,
        "total": job.total,
        "processed": job.processed,
        "created": job.created_count,
        "merged": job.merged_count,
        "skipped": job.skipped_count,
        "custom_fields_created": job.fields_created or [],
        "error": job.error,
    }
