import uuid

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps import get_current_user
from app.models import ImportJob, User
from app.schemas import ImportCommitIn
from app.services.background import spawn
from app.services.importer import IMPORT_TYPES, find_duplicates, parse_csv, run_import_job

router = APIRouter()

MAX_UPLOAD_BYTES = 50 * 1024 * 1024


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
    body: ImportCommitIn,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Stores the rows as a job and processes them in the background — a 19k-row
    import takes minutes, far past what a request (or the tunnel) will hold."""
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
    job = (
        await db.execute(
            select(ImportJob).where(ImportJob.id == job_id, ImportJob.org_id == user.org_id)
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
