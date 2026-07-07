from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps import get_current_user
from app.models import User
from app.schemas import ImportCommitIn
from app.services.importer import IMPORT_TYPES, commit_rows, find_duplicates, parse_csv

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


@router.post("/commit")
async def commit_import(
    body: ImportCommitIn,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if body.type not in IMPORT_TYPES:
        raise HTTPException(
            status_code=422, detail=f"type must be one of {sorted(IMPORT_TYPES)}"
        )
    return await commit_rows(db, user, body.type, body.rows)
