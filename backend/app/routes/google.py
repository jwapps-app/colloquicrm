import uuid
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import RedirectResponse
from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import get_db
from app.deps import get_current_user, require_admin
from app.models import (
    CalendarEvent,
    CalendarEventAttendee,
    Company,
    EmailMessage,
    EmailParticipant,
    GoogleAccount,
    GoogleIntegration,
    Lead,
    Person,
    User,
    utcnow,
)
from sqlalchemy import func

from app.schemas import GoogleConfigIn
from app.services import google as g
from app.services.importer import find_duplicates

router = APIRouter()


async def _cfg(db: AsyncSession, org_id: uuid.UUID) -> GoogleIntegration | None:
    return (
        await db.execute(select(GoogleIntegration).where(GoogleIntegration.org_id == org_id))
    ).scalar_one_or_none()


async def _account(db: AsyncSession, user_id: uuid.UUID) -> GoogleAccount | None:
    return (
        await db.execute(select(GoogleAccount).where(GoogleAccount.user_id == user_id))
    ).scalar_one_or_none()


@router.get("/status")
async def status(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    cfg = await _cfg(db, user.org_id)
    account = await _account(db, user.id)
    return {
        "configured": cfg is not None,
        "client_id": cfg.client_id if cfg else None,
        "redirect_uri": g.redirect_uri(),
        "me": {
            "connected": account is not None,
            "email": account.email if account else None,
            "last_synced_at": account.last_synced_at.isoformat()
            if account and account.last_synced_at
            else None,
            "sync_error": account.sync_error if account else None,
            "gmail_enabled": g.has_gmail_scope(account) if account else False,
            "gmail_backfill_done": bool(account.gmail_backfill_done) if account else False,
        },
        "emails_matched": (
            await db.execute(
                select(func.count()).select_from(EmailMessage).where(
                    EmailMessage.org_id == user.org_id
                )
            )
        ).scalar_one(),
    }


@router.post("/config")
async def set_config(
    body: GoogleConfigIn, admin: User = Depends(require_admin), db: AsyncSession = Depends(get_db)
):
    cfg = await _cfg(db, admin.org_id)
    if cfg is None:
        cfg = GoogleIntegration(
            org_id=admin.org_id,
            client_id=body.client_id.strip(),
            client_secret=body.client_secret.strip(),
        )
        db.add(cfg)
    else:
        cfg.client_id = body.client_id.strip()
        cfg.client_secret = body.client_secret.strip()
    return {"configured": True, "client_id": cfg.client_id, "redirect_uri": g.redirect_uri()}


@router.delete("/config", status_code=204)
async def delete_config(admin: User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    await db.execute(delete(GoogleAccount).where(GoogleAccount.org_id == admin.org_id))
    await db.execute(delete(GoogleIntegration).where(GoogleIntegration.org_id == admin.org_id))


@router.get("/auth-url")
async def auth_url(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    cfg = await _cfg(db, user.org_id)
    if cfg is None:
        raise HTTPException(status_code=400, detail="Google integration is not configured")
    return {"url": g.auth_url(cfg.client_id, user.id)}


@router.get("/callback")
async def callback(
    state: str,
    code: str | None = None,
    error: str | None = None,
    db: AsyncSession = Depends(get_db),
):
    """Browser redirect target from Google — authenticated by the signed
    state, not a bearer token."""
    dest = f"{settings.app_url.rstrip('/')}/settings"
    if error or not code:
        return RedirectResponse(f"{dest}?google=denied")
    user_id = g.check_state(state)
    if user_id is None:
        return RedirectResponse(f"{dest}?google=state_error")
    user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if user is None:
        return RedirectResponse(f"{dest}?google=state_error")
    cfg = await _cfg(db, user.org_id)
    if cfg is None:
        return RedirectResponse(f"{dest}?google=not_configured")
    try:
        tokens = await g.exchange_code(cfg, code)
        info = await g.get_userinfo(tokens["access_token"])
    except (g.GoogleError, KeyError):
        return RedirectResponse(f"{dest}?google=exchange_error")

    refresh = tokens.get("refresh_token")
    account = await _account(db, user.id)
    if refresh is None and account is None:
        # Without a refresh token we can't sync later; force re-consent.
        return RedirectResponse(f"{dest}?google=no_refresh_token")

    if account is None:
        account = GoogleAccount(
            user_id=user.id, org_id=user.org_id, email=info.get("email") or "", refresh_token=refresh
        )
        db.add(account)
    else:
        account.email = info.get("email") or account.email
        if refresh:
            account.refresh_token = refresh
    account.access_token = tokens["access_token"]
    account.access_expires_at = utcnow() + timedelta(seconds=int(tokens.get("expires_in", 3600)))
    account.scopes = tokens.get("scope")
    account.connected_at = utcnow()
    account.sync_error = None
    await db.flush()
    return RedirectResponse(f"{dest}?google=connected")


@router.delete("/link", status_code=204)
async def unlink(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    account = await _account(db, user.id)
    if account is not None:
        await g.revoke(account)
        await db.delete(account)


@router.post("/sync")
async def sync_now(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    account = await _account(db, user.id)
    if account is None:
        raise HTTPException(status_code=400, detail="No Google account connected")
    try:
        result = await g.sync_account(db, account, force_backfill=True)
    except g.GoogleError as exc:
        account.sync_error = str(exc)[:500]
        raise HTTPException(status_code=502, detail=str(exc))
    return {**result, "last_synced_at": account.last_synced_at.isoformat()}


@router.get("/diagnose")
async def diagnose_address(
    email: str,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Trace one address through the whole email-sync pipeline: is it a CRM
    contact, what does the Gmail search return, how do the messages parse,
    and what is already stored. Admin-only troubleshooting."""
    account = await _account(db, user.id)
    cfg = await _cfg(db, user.org_id)
    if account is None or cfg is None:
        raise HTTPException(status_code=400, detail="Google is not connected")
    addr = email.lower().strip()
    try:
        access = await g.ensure_access_token(db, cfg, account)
        ids = await g._search_contact_mail(access, [addr])
        contact_map = await g._crm_email_map(db, user.org_id)
        samples = []
        for gid in ids[:5]:
            item = await g._fetch_message_meta(access, gid)
            if item is None:
                continue
            parsed = g._parse_message(item, (account.email or "").lower().strip())
            if parsed is None:
                samples.append({"gmail_id": gid, "parse": "no participants"})
                continue
            participants = parsed["participants"]
            samples.append(
                {
                    "gmail_id": gid,
                    "subject": parsed["subject"],
                    "participants": [f"{k}:{e}" for k, e, _ in participants],
                    "would_match": [
                        f"{contact_map[e][0]}:{e}"
                        for _, e, _ in participants
                        if e != (account.email or "").lower().strip() and e in contact_map
                    ],
                }
            )
    except g.GoogleError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    stored = (
        await db.execute(
            select(func.count(func.distinct(EmailMessage.id)))
            .select_from(EmailMessage)
            .join(EmailParticipant, EmailParticipant.email_id == EmailMessage.id)
            .where(EmailMessage.org_id == user.org_id, EmailParticipant.email == addr)
        )
    ).scalar_one()
    return {
        "address": addr,
        "is_crm_contact": addr in contact_map,
        "crm_contacts_total": len(contact_map),
        "gmail_search_hits": len(ids),
        "mailbox": account.email,
        "samples": samples,
        "already_stored": stored,
    }


@router.get("/contacts/preview")
async def contacts_preview(
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    """Google contacts shaped exactly like a CSV import preview, so the same
    review screen and commit endpoint handle them."""
    account = await _account(db, user.id)
    if account is None:
        raise HTTPException(status_code=400, detail="No Google account connected")
    cfg = await _cfg(db, user.org_id)
    if cfg is None:
        raise HTTPException(status_code=400, detail="Google integration is not configured")
    try:
        access = await g.ensure_access_token(db, cfg, account)
        contacts = await g.fetch_contacts(access)
    except g.GoogleError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    rows = [row for c in contacts if (row := g.contact_to_row(c)) is not None]
    duplicates_found = await find_duplicates(db, user.org_id, "people", rows)
    return {
        "type": "people",
        "source": "google",
        "total": len(rows),
        "unmapped_headers": [],
        "duplicates_found": duplicates_found,
        "rows": rows,
    }


CALENDAR_ENTITY_MODELS = {"person": Person, "lead": Lead, "company": Company}

calendar_router = APIRouter()


@calendar_router.get("")
async def calendar_events(
    entity_type: str,
    entity_id: uuid.UUID,
    limit: int = 25,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    model = CALENDAR_ENTITY_MODELS.get(entity_type)
    if model is None:
        raise HTTPException(status_code=422, detail="entity_type must be person, lead or company")
    obj = (
        await db.execute(select(model).where(model.id == entity_id, model.org_id == user.org_id))
    ).scalar_one_or_none()
    if obj is None:
        raise HTTPException(status_code=404, detail=f"{entity_type} not found")

    stmt = (
        select(CalendarEvent)
        .join(CalendarEventAttendee, CalendarEventAttendee.event_id == CalendarEvent.id)
        .where(CalendarEvent.org_id == user.org_id)
        .distinct()
    )
    if entity_type == "company":
        domain = (obj.email_domain or "").lower().strip()
        if not domain:
            return {"items": []}
        stmt = stmt.where(CalendarEventAttendee.email.like(f"%@{domain}"))
    else:
        emails = []
        if entity_type == "person":
            emails = [e.lower() for e in (obj.work_email, obj.personal_email) if e]
        elif entity_type == "lead":
            emails = [obj.email.lower()] if obj.email else []
        if not emails:
            return {"items": []}
        stmt = stmt.where(CalendarEventAttendee.email.in_(emails))

    stmt = stmt.order_by(CalendarEvent.starts_at.desc()).limit(min(max(limit, 1), 100))
    events = (await db.execute(stmt)).scalars().all()
    ids = [e.id for e in events]
    attendee_map: dict = {}
    if ids:
        rows = await db.execute(
            select(CalendarEventAttendee).where(CalendarEventAttendee.event_id.in_(ids))
        )
        for att in rows.scalars():
            attendee_map.setdefault(att.event_id, []).append(
                {"email": att.email, "display_name": att.display_name}
            )
    return {
        "items": [
            {
                "id": str(e.id),
                "summary": e.summary,
                "location": e.location,
                "starts_at": e.starts_at.isoformat() if e.starts_at else None,
                "ends_at": e.ends_at.isoformat() if e.ends_at else None,
                "all_day": e.all_day,
                "html_link": e.html_link,
                "attendees": attendee_map.get(e.id, []),
            }
            for e in events
        ]
    }


emails_router = APIRouter()


@emails_router.get("")
async def emails_for_entity(
    entity_type: str,
    entity_id: uuid.UUID,
    limit: int = 50,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    model = CALENDAR_ENTITY_MODELS.get(entity_type)
    if model is None:
        raise HTTPException(status_code=422, detail="entity_type must be person, lead or company")
    obj = (
        await db.execute(select(model).where(model.id == entity_id, model.org_id == user.org_id))
    ).scalar_one_or_none()
    if obj is None:
        raise HTTPException(status_code=404, detail=f"{entity_type} not found")

    stmt = (
        select(EmailMessage)
        .join(EmailParticipant, EmailParticipant.email_id == EmailMessage.id)
        .where(EmailMessage.org_id == user.org_id)
        .distinct()
    )
    if entity_type == "company":
        domain = (obj.email_domain or "").lower().strip()
        if not domain:
            return {"items": []}
        stmt = stmt.where(EmailParticipant.email.like(f"%@{domain}"))
    else:
        emails = []
        if entity_type == "person":
            emails = [e.lower() for e in (obj.work_email, obj.personal_email) if e]
        elif entity_type == "lead":
            emails = [obj.email.lower()] if obj.email else []
        if not emails:
            return {"items": []}
        stmt = stmt.where(EmailParticipant.email.in_(emails))

    stmt = stmt.order_by(EmailMessage.sent_at.desc()).limit(min(max(limit, 1), 200))
    messages = (await db.execute(stmt)).scalars().all()
    return {
        "items": [
            {
                "id": str(m.id),
                "subject": m.subject,
                "snippet": m.snippet,
                "from_email": m.from_email,
                "from_name": m.from_name,
                "is_outgoing": m.is_outgoing,
                "sent_at": m.sent_at.isoformat() if m.sent_at else None,
                "gmail_id": m.gmail_id,
                "owner_user_id": str(m.owner_user_id) if m.owner_user_id else None,
            }
            for m in messages
        ]
    }
