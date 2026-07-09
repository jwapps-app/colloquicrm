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
    backfill_total = None
    backfill_done = bool(account.gmail_backfill_done) if account else False
    if account and g.has_gmail_scope(account) and not backfill_done:
        contact_map = await g._crm_email_map(db, user.org_id)
        backfill_total = len(g._backfill_addresses(contact_map))
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
            "gmail_backfill_done": backfill_done,
            "gmail_backfill_cursor": account.gmail_backfill_cursor if account else 0,
            "gmail_backfill_total": backfill_total,
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
    await db.commit()  # visible before the client refetches
    return {"configured": True, "client_id": cfg.client_id, "redirect_uri": g.redirect_uri()}


@router.delete("/config", status_code=204)
async def delete_config(admin: User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    await db.execute(delete(GoogleAccount).where(GoogleAccount.org_id == admin.org_id))
    await db.execute(delete(GoogleIntegration).where(GoogleIntegration.org_id == admin.org_id))
    await db.commit()  # visible before the client refetches


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
    await db.commit()  # the redirect target refetches status immediately
    return RedirectResponse(f"{dest}?google=connected")


@router.delete("/link", status_code=204)
async def unlink(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    account = await _account(db, user.id)
    if account is not None:
        await g.revoke(account)
        await db.delete(account)
        await db.commit()  # visible before the client refetches


@router.post("/sync", status_code=202)
async def sync_now(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """Kicks a full sync (including contact-history backfill) in the
    background — the backfill alone can outlive any request timeout."""
    import asyncio

    account = await _account(db, user.id)
    if account is None:
        raise HTTPException(status_code=400, detail="No Google account connected")
    asyncio.create_task(g.sync_account_background(user.id, force_backfill=True))
    return {"status": "started"}


@router.get("/test-connection")
async def test_connection(admin: User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    """Live probe: from the server, right now, try the OAuth token refresh and a
    Gmail call and report exactly what happens — timings and precise errors.
    Bypasses the background sync loop, so it works even if the loop is stuck."""
    import time

    import httpx

    account = await _account(db, admin.id)
    cfg = await _cfg(db, admin.org_id)
    if account is None or cfg is None:
        raise HTTPException(status_code=400, detail="Google is not connected")

    out: dict = {
        "backfill_cursor": account.gmail_backfill_cursor,
        "backfill_done": bool(account.gmail_backfill_done),
        "stored_sync_error": account.sync_error,
        "token_endpoint": settings.google_token_url,
        "gmail_endpoint": settings.google_gmail_base,
    }

    # Step 1 — refresh the access token (single attempt, short timeout).
    t0 = time.monotonic()
    access = None
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(
                settings.google_token_url,
                data={
                    "client_id": cfg.client_id,
                    "client_secret": cfg.client_secret,
                    "refresh_token": account.refresh_token,
                    "grant_type": "refresh_token",
                },
            )
        out["token_http_status"] = resp.status_code
        if resp.status_code < 400:
            out["token_ok"] = True
            access = resp.json().get("access_token")
        else:
            out["token_ok"] = False
            out["token_error"] = resp.text[:300]
    except Exception as exc:
        out["token_ok"] = False
        out["token_error"] = f"{type(exc).__name__}: {exc}"
    out["token_ms"] = round((time.monotonic() - t0) * 1000)

    # Step 2 — a real Gmail call (mailbox profile), only if we got a token.
    if access:
        t0 = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                resp = await client.get(
                    f"{settings.google_gmail_base}/users/me/profile",
                    headers={"Authorization": f"Bearer {access}"},
                )
            out["gmail_http_status"] = resp.status_code
            if resp.status_code < 400:
                out["gmail_ok"] = True
                out["gmail_messages_total"] = resp.json().get("messagesTotal")
            else:
                out["gmail_ok"] = False
                out["gmail_error"] = resp.text[:300]
        except Exception as exc:
            out["gmail_ok"] = False
            out["gmail_error"] = f"{type(exc).__name__}: {exc}"
        out["gmail_ms"] = round((time.monotonic() - t0) * 1000)
    return out


@router.get("/diagnose-backfill")
async def diagnose_backfill(admin: User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    """Run the exact backfill step for the current cursor chunk — the search
    and the dedup query — synchronously, right now, and report what happens.
    Reproduces the failing path live without waiting on the background loop."""
    import time

    account = await _account(db, admin.id)
    cfg = await _cfg(db, admin.org_id)
    if account is None or cfg is None:
        raise HTTPException(status_code=400, detail="Google is not connected")

    out: dict = {"cursor": account.gmail_backfill_cursor, "done": bool(account.gmail_backfill_done)}
    try:
        access = await g.ensure_access_token(db, cfg, account)
        contact_map = await g._crm_email_map(db, admin.org_id)
        addresses = g._backfill_addresses(contact_map)
        out["total_addresses"] = len(addresses)
        cursor = account.gmail_backfill_cursor or 0
        chunk = addresses[cursor : cursor + g.BACKFILL_ADDRESSES_PER_CHECKPOINT]
        out["chunk_size"] = len(chunk)
        out["sample_addresses"] = chunk[:5]

        t0 = time.monotonic()
        ids = await g._search_contact_mail(access, chunk)
        out["search_ms"] = round((time.monotonic() - t0) * 1000)
        out["search_hits"] = len(ids)

        t0 = time.monotonic()
        known = await g._known_gmail_ids(db, account.user_id, ids)
        out["dedup_ms"] = round((time.monotonic() - t0) * 1000)
        out["already_stored"] = len(known)
        out["unseen"] = len(ids) - len(known)
        out["step_result"] = "search + dedup OK on current code"
    except Exception as exc:
        import traceback

        out["error_type"] = type(exc).__name__
        out["error"] = str(exc)[:400]
        out["traceback_tail"] = traceback.format_exc()[-600:]
    return out


@router.post("/recompute-metrics", status_code=202)
async def recompute_metrics(admin: User = Depends(require_admin)):
    """Rebuild every person's interaction count and last-contacted date from
    stored emails and calls, in the background."""
    import asyncio

    asyncio.create_task(g.recompute_all_person_metrics(admin.org_id))
    return {"status": "started"}


@router.get("/diagnose")
async def diagnose_address(
    email: str,
    q: str | None = None,
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
    addr = g.normalize_email(email)
    try:
        access = await g.ensure_access_token(db, cfg, account)
        # Ask Gmail directly whose mailbox this token opens — the stored email
        # can lie if the consent screen picked a different account.
        profile = await g._get_json(f"{settings.google_gmail_base}/users/me/profile", access)
        if q:
            # Raw Gmail query passthrough for troubleshooting.
            data = await g._get_json(
                f"{settings.google_gmail_base}/users/me/messages",
                access,
                {"q": q, "maxResults": 100},
            )
            ids = [m["id"] for m in data.get("messages", []) if m.get("id")]
        else:
            ids = await g._search_contact_mail(access, [addr])
        contact_map = await g._crm_email_map(db, user.org_id)
        samples = []
        for gid in ids[:5]:
            item = await g._fetch_message(access, gid, full=False)
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
        "mailbox_stored": account.email,
        "mailbox_actual": profile.get("emailAddress"),
        "mailbox_total_messages": profile.get("messagesTotal"),
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
            emails = [g.normalize_email(e) for e in (obj.work_email, obj.personal_email) if e]
        elif entity_type == "lead":
            emails = [g.normalize_email(obj.email)] if obj.email else []
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
        .where(EmailMessage.org_id == user.org_id, EmailParticipant.direct.is_(True))
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
            emails = [g.normalize_email(e) for e in (obj.work_email, obj.personal_email) if e]
        elif entity_type == "lead":
            emails = [g.normalize_email(obj.email)] if obj.email else []
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


@emails_router.get("/{email_id}/body")
async def email_body(
    email_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    msg = (
        await db.execute(
            select(EmailMessage).where(
                EmailMessage.id == email_id, EmailMessage.org_id == user.org_id
            )
        )
    ).scalar_one_or_none()
    if msg is None:
        raise HTTPException(status_code=404, detail="Email not found")
    if msg.body_fetched_at is None:
        if msg.owner_user_id is None:
            raise HTTPException(status_code=409, detail="The mailbox this email came from is gone")
        owner_account = (
            await db.execute(
                select(GoogleAccount).where(GoogleAccount.user_id == msg.owner_user_id)
            )
        ).scalar_one_or_none()
        cfg = await _cfg(db, user.org_id)
        if owner_account is None or cfg is None:
            raise HTTPException(
                status_code=409,
                detail="The mailbox owner is no longer connected to Google",
            )
        try:
            access = await g.ensure_access_token(db, cfg, owner_account)
            body = await g.fetch_message_body(access, msg.gmail_id)
        except g.GoogleError as exc:
            raise HTTPException(status_code=502, detail=str(exc))
        msg.body_text = body.get("text")
        msg.body_html = body.get("html")
        msg.body_fetched_at = utcnow()
    return {
        "id": str(msg.id),
        "subject": msg.subject,
        "body_text": msg.body_text,
        "body_html": msg.body_html,
    }
