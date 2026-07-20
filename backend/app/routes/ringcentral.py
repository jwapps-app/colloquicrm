import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps import get_current_user, require_admin
from app.models import Company, Lead, Person, PhoneEvent, RingCentralIntegration, User, utcnow
from app.schemas import RingCentralConnectIn
from app.services import ringcentral as rc
from app.services.common import company_people

router = APIRouter()


async def _cfg(db: AsyncSession, org_id: uuid.UUID) -> RingCentralIntegration | None:
    return (
        await db.execute(
            select(RingCentralIntegration).where(RingCentralIntegration.org_id == org_id)
        )
    ).scalar_one_or_none()


def _status(cfg: RingCentralIntegration | None) -> dict:
    return {
        "configured": cfg is not None,
        "own_numbers": cfg.own_numbers if cfg else [],
        "last_synced_at": cfg.last_synced_at.isoformat() if cfg and cfg.last_synced_at else None,
        "sync_error": cfg.sync_error if cfg else None,
        "connected_at": cfg.connected_at.isoformat() if cfg and cfg.connected_at else None,
    }


@router.get("/status")
async def status(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    return _status(await _cfg(db, user.org_id))


@router.post("/connect")
async def connect(
    body: RingCentralConnectIn,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    cfg = await _cfg(db, admin.org_id)
    if cfg is None:
        cfg = RingCentralIntegration(
            org_id=admin.org_id,
            client_id=body.client_id.strip(),
            client_secret=body.client_secret.strip(),
            jwt=body.jwt.strip(),
        )
        db.add(cfg)
    else:
        cfg.client_id = body.client_id.strip()
        cfg.client_secret = body.client_secret.strip()
        cfg.jwt = body.jwt.strip()
        cfg.access_token = None
        cfg.access_expires_at = None
        cfg.own_numbers = None
    try:
        access = await rc.ensure_access_token(db, cfg)
        cfg.own_numbers = await rc.fetch_own_numbers(access)
    except rc.RingCentralError as exc:
        await db.rollback()
        raise HTTPException(status_code=502, detail=str(exc))
    cfg.connected_at = utcnow()
    cfg.sync_error = None
    await db.commit()  # visible before the client refetches
    return _status(cfg)


@router.delete("/connect", status_code=204)
async def disconnect(admin: User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    await db.execute(
        delete(RingCentralIntegration).where(RingCentralIntegration.org_id == admin.org_id)
    )
    await db.commit()  # visible before the client refetches


@router.post("/sync")
async def sync_now(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    cfg = await _cfg(db, user.org_id)
    if cfg is None:
        raise HTTPException(status_code=400, detail="RingCentral is not connected")
    try:
        result = await rc.sync_org(db, cfg)
    except rc.RingCentralError as exc:
        cfg.sync_error = str(exc)[:500]
        # The raise makes get_db roll back — commit or the recorded error
        # (the whole point of sync_error) never lands.
        await db.commit()
        raise HTTPException(status_code=502, detail=str(exc))
    await db.commit()  # visible before the client refetches
    return {**result, "last_synced_at": cfg.last_synced_at.isoformat()}


@router.get("/diagnose")
async def diagnose_number(
    number: str,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Trace one phone number through the pipeline: normalization, CRM match,
    what RingCentral's call log holds for it, and what is stored."""
    cfg = await _cfg(db, admin.org_id)
    if cfg is None:
        raise HTTPException(status_code=400, detail="RingCentral is not connected")
    normalized = rc.normalize_phone(number)
    if not normalized:
        return {"input": number, "normalized": None, "note": "could not normalize"}
    phone_map = await rc._crm_phone_map(db, admin.org_id)
    match = phone_map.get(normalized)
    from datetime import timedelta

    from app.config import settings as _settings

    live_hits = []
    try:
        access = await rc.ensure_access_token(db, cfg)
        data = await rc._get_json(
            f"{_settings.ringcentral_base}/restapi/v1.0/account/~/call-log",
            access,
            {
                "view": "Simple",
                "perPage": 25,
                "phoneNumber": normalized,
                "dateFrom": (utcnow() - timedelta(days=_settings.ringcentral_backfill_days)).isoformat(),
            },
        )
        for r in data.get("records", []):
            live_hits.append(
                {
                    "direction": r.get("direction"),
                    "start": r.get("startTime"),
                    "duration": r.get("duration"),
                    "result": r.get("result"),
                }
            )
    except rc.RingCentralError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    stored = (
        await db.execute(
            select(PhoneEvent).where(
                PhoneEvent.org_id == admin.org_id, PhoneEvent.other_number == normalized
            )
        )
    ).scalars().all()
    return {
        "input": number,
        "normalized": normalized,
        "crm_match": {"entity_type": match[0], "entity_id": str(match[1])} if match else None,
        "ringcentral_call_hits": len(live_hits),
        "samples": live_hits[:5],
        "stored_events": len(stored),
        "own_numbers": cfg.own_numbers,
    }


PHONE_ENTITY_MODELS = {"person": Person, "lead": Lead, "company": Company}


@router.get("/events")
async def phone_events(
    entity_type: str,
    entity_id: uuid.UUID,
    limit: int = 50,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    model = PHONE_ENTITY_MODELS.get(entity_type)
    if model is None:
        raise HTTPException(status_code=422, detail="entity_type must be person, lead or company")
    obj = (
        await db.execute(select(model).where(model.id == entity_id, model.org_id == user.org_id))
    ).scalar_one_or_none()
    if obj is None:
        raise HTTPException(status_code=404, detail=f"{entity_type} not found")

    if entity_type == "company":
        # A company's call/text history is everyone associated with it: events
        # matched to any person's number, events logged directly on those
        # people, plus anything logged on the company itself.
        people = await company_people(db, user.org_id, entity_id)
        numbers = {
            n
            for p in people
            for n in (rc.normalize_phone(p.work_phone), rc.normalize_phone(p.mobile_phone))
            if n
        }
        conditions = [
            (PhoneEvent.entity_type == "company") & (PhoneEvent.entity_id == entity_id)
        ]
        if numbers:
            conditions.append(PhoneEvent.other_number.in_(numbers))
        person_ids = [p.id for p in people]
        if person_ids:
            conditions.append(
                (PhoneEvent.entity_type == "person")
                & (PhoneEvent.entity_id.in_(person_ids))
            )
    else:
        numbers = [
            n
            for n in (rc.normalize_phone(obj.work_phone), rc.normalize_phone(obj.mobile_phone))
            if n
        ]
        conditions = [
            (PhoneEvent.entity_type == entity_type) & (PhoneEvent.entity_id == entity_id)
        ]
        if numbers:
            conditions.append(PhoneEvent.other_number.in_(numbers))

    events = (
        (
            await db.execute(
                select(PhoneEvent)
                .where(PhoneEvent.org_id == user.org_id, or_(*conditions))
                .order_by(PhoneEvent.happened_at.desc())
                .limit(min(max(limit, 1), 200))
            )
        )
        .scalars()
        .all()
    )
    return {
        "items": [
            {
                "id": str(e.id),
                "kind": e.kind,
                "direction": e.direction,
                "other_number": e.other_number,
                "other_name": e.other_name,
                "happened_at": e.happened_at.isoformat() if e.happened_at else None,
                "duration_seconds": e.duration_seconds,
                "result": e.result,
                "text": e.text,
                "recording_id": e.recording_id,
            }
            for e in events
        ]
    }
