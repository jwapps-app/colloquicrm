import hmac
import secrets
import uuid
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, Response
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
from pydantic import BaseModel
from sqlalchemy import func

from app.services import google as g
from app.services.background import spawn
from app.services.common import company_people
from app.services.importer import find_duplicates

router = APIRouter()


class GoogleConfigBody(BaseModel):
    client_id: str
    # Blank (or omitted) means "keep the secret already stored" — the
    # settings form never gets the secret back, so re-saving it sends "".
    client_secret: str = ""


# Binds the OAuth callback to the browser that asked for the auth URL: the
# cookie holds a nonce whose hash rides in the signed state, and /callback
# insists the two match before any token is stored. Scoped to the callback
# path so it never travels with ordinary API calls; SameSite=Lax is what
# lets it accompany the top-level redirect back from Google.
_NONCE_COOKIE = "google_oauth_nonce"
_NONCE_COOKIE_PATH = "/api/v1/integrations/google/callback"


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
    from app.routes.feed import _org_contact_maps

    cfg = await _cfg(db, user.org_id)
    account = await _account(db, user.id)
    backfill_total = None
    backfill_done = bool(account.gmail_backfill_done) if account else False
    if account and g.has_gmail_scope(account) and not backfill_done:
        # The sync stamps the walk's length when it starts. Before the first
        # pass has run there is nothing stamped yet, so size the walk from the
        # feed's short-lived contact map rather than rebuilding the org's
        # address book on every poll.
        backfill_total = account.gmail_backfill_total
        if backfill_total is None:
            addr_map, _ = await _org_contact_maps(db, user.org_id)
            backfill_total = g.count_backfill_addresses(addr_map)
    # Additive: how much failed work is still owed a retry, and how much was
    # given up on — so "no sync error" can't hide a hole in the archive.
    retry_counts = await g.failure_counts(db, user.id) if account else {}
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
            **retry_counts,
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
    body: GoogleConfigBody, admin: User = Depends(require_admin), db: AsyncSession = Depends(get_db)
):
    client_id = body.client_id.strip()
    client_secret = body.client_secret.strip()
    if not client_id:
        raise HTTPException(status_code=422, detail="Client ID is required")
    cfg = await _cfg(db, admin.org_id)
    if cfg is None:
        if not client_secret:
            raise HTTPException(status_code=422, detail="Client secret is required")
        cfg = GoogleIntegration(
            org_id=admin.org_id, client_id=client_id, client_secret=client_secret
        )
        db.add(cfg)
    else:
        cfg.client_id = client_id
        if client_secret:
            cfg.client_secret = client_secret
        elif not cfg.client_secret:
            # Nothing stored to fall back on (an earlier blank save wiped it).
            raise HTTPException(status_code=422, detail="Client secret is required")
    await db.commit()  # visible before the client refetches
    return {"configured": True, "client_id": cfg.client_id, "redirect_uri": g.redirect_uri()}


@router.delete("/config", status_code=204)
async def delete_config(admin: User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    await db.execute(delete(GoogleAccount).where(GoogleAccount.org_id == admin.org_id))
    await db.execute(delete(GoogleIntegration).where(GoogleIntegration.org_id == admin.org_id))
    await db.commit()  # visible before the client refetches


@router.get("/auth-url")
async def auth_url(
    response: Response,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    cfg = await _cfg(db, user.org_id)
    if cfg is None:
        raise HTTPException(status_code=400, detail="Google integration is not configured")
    nonce = secrets.token_urlsafe(32)
    response.set_cookie(
        _NONCE_COOKIE,
        nonce,
        max_age=g.STATE_TTL_SECONDS,
        path=_NONCE_COOKIE_PATH,
        httponly=True,
        secure=settings.environment == "production",
        samesite="lax",
    )
    return {"url": g.auth_url(cfg.client_id, user.id, nonce)}


@router.get("/callback")
async def callback(
    request: Request,
    state: str,
    code: str | None = None,
    error: str | None = None,
    db: AsyncSession = Depends(get_db),
):
    """Browser redirect target from Google — authenticated by the signed
    state plus the nonce cookie set when the flow started, not a bearer
    token."""
    dest = f"{settings.app_url.rstrip('/')}/settings"

    def finish(result: str) -> RedirectResponse:
        # One-shot: the nonce is spent whichever way the flow ended.
        resp = RedirectResponse(f"{dest}?google={result}")
        resp.delete_cookie(_NONCE_COOKIE, path=_NONCE_COOKIE_PATH)
        return resp

    if error or not code:
        return finish("denied")
    checked = g.check_state(state)
    if checked is None:
        return finish("state_error")
    user_id, nonce_hash = checked
    nonce = request.cookies.get(_NONCE_COOKIE, "")
    if not nonce or not hmac.compare_digest(g.hash_nonce(nonce), nonce_hash):
        # No cookie (a pasted/forwarded callback URL) or the wrong one: this
        # isn't the browser that started the flow.
        return finish("state_error")
    user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if user is None:
        return finish("state_error")
    cfg = await _cfg(db, user.org_id)
    if cfg is None:
        return finish("not_configured")
    org_id = user.org_id
    await g._release_db(db)  # the token exchange must not sit inside a transaction
    try:
        tokens = await g.exchange_code(cfg, code)
        info = await g.get_userinfo(tokens["access_token"])
    except (g.GoogleError, KeyError):
        return finish("exchange_error")

    refresh = tokens.get("refresh_token")
    account = await _account(db, user_id)
    new_email = (info.get("email") or "").strip()
    # A different Google identity than the one on file: everything that
    # tracks sync position describes the OLD mailbox.
    switched = (
        account is not None
        and bool(new_email)
        and new_email.lower() != (account.email or "").strip().lower()
    )
    if switched and g.sync_in_progress(user_id):
        # A running sync holds the old mailbox's position in memory and would
        # write it back over the reset below. Rare, and retrying is cheap.
        return finish("sync_running")
    if refresh is None and (account is None or switched):
        # Without a refresh token we can't sync later (and the stored one
        # belongs to the other mailbox); force re-consent.
        return finish("no_refresh_token")

    if account is None:
        account = GoogleAccount(
            user_id=user_id, org_id=org_id, email=new_email, refresh_token=refresh
        )
        db.add(account)
    else:
        if switched:
            # Start the new mailbox from scratch — history id, backfill walk
            # and retry queue. Mail already archived from the old one stays.
            await g.reset_mailbox_state(db, account)
        account.email = new_email or account.email
        if refresh:
            account.refresh_token = refresh
    account.access_token = tokens["access_token"]
    account.access_expires_at = utcnow() + timedelta(seconds=int(tokens.get("expires_in", 3600)))
    account.scopes = tokens.get("scope")
    account.connected_at = utcnow()
    account.sync_error = None
    await db.commit()  # the redirect target refetches status immediately
    return finish("connected")


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
    account = await _account(db, user.id)
    if account is None:
        raise HTTPException(status_code=400, detail="No Google account connected")
    # One sync per account at a time: if the scheduled pass (or an earlier
    # click) is mid-sync, say so rather than queue a second walk behind it.
    if not g.try_claim_sync(user.id):
        raise HTTPException(
            status_code=409,
            detail="A sync is already running for this account — try again when it finishes",
        )
    try:
        spawn(
            g.sync_account_background(
                user.id, force_backfill=True, claimed=True, label=account.email
            )
        )
    except BaseException:
        g.release_sync(user.id)
        raise
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
        cursor = g.backfill_position(addresses, account.gmail_backfill_after)
        out["cursor"] = cursor
        out["mid_group_page_saved"] = bool(account.gmail_backfill_page_token)
        chunk = addresses[cursor : cursor + g.ADDRESSES_PER_QUERY]
        out["chunk_size"] = len(chunk)
        out["sample_addresses"] = chunk[:5]

        await g._release_db(db)
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
    spawn(g.recompute_all_person_metrics(admin.org_id))
    return {"status": "started"}


@router.post("/scan-suggestions", status_code=202)
async def scan_suggestions(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """Scan recent mail for frequent correspondents not yet in the CRM."""
    account = await _account(db, user.id)
    if account is None or not g.has_gmail_scope(account):
        raise HTTPException(status_code=400, detail="Connect Google with email access first")
    spawn(g.scan_contact_suggestions(user.id))
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
        await g._release_db(db)  # no transaction open across the Gmail calls
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
        await g._release_db(db)  # no transaction open across the People API walk
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


@emails_router.get("/search")
async def search_emails(
    q: str,
    page: int = 1,
    page_size: int = 25,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Full-text search across every synced email — subject, snippet, sender,
    and archived body — newest first."""
    from app.routes.feed import _org_contact_maps

    term = (q or "").strip()
    # Three characters is the floor: a two-character pattern yields no usable
    # trigram, so the GIN indexes can't help and the OR over five columns
    # falls back to scanning every archived body. Short terms get the normal
    # empty envelope, not an error — the clients fire as the user types.
    if len(term) < 3:
        return {"items": [], "page": 1, "has_more": False}
    page = max(1, page)
    page_size = min(max(page_size, 1), 50)
    pattern = f"%{term}%"
    # Plain ilike (no lower() wrapper) so the pg_trgm GIN indexes can serve
    # the leading-wildcard match; and skinny columns only — archived bodies
    # can be a megabyte each, and the response never returns them.
    cols = [
        EmailMessage.subject,
        EmailMessage.snippet,
        EmailMessage.from_name,
        EmailMessage.from_email,
        EmailMessage.body_text,
    ]
    stmt = (
        select(
            EmailMessage.id,
            EmailMessage.subject,
            EmailMessage.snippet,
            EmailMessage.from_email,
            EmailMessage.from_name,
            EmailMessage.is_outgoing,
            EmailMessage.sent_at,
            EmailMessage.gmail_id,
            EmailMessage.owner_user_id,
        )
        .where(
            EmailMessage.org_id == user.org_id,
            or_(*[c.ilike(pattern) for c in cols]),
        )
        .order_by(EmailMessage.sent_at.desc().nulls_last())
        .offset((page - 1) * page_size)
        .limit(page_size + 1)  # one extra to detect more
    )
    messages = (await db.execute(stmt)).all()
    has_more = len(messages) > page_size
    messages = messages[:page_size]

    related: dict = {}
    if messages:
        parts = await db.execute(
            select(EmailParticipant.email_id, EmailParticipant.email).where(
                EmailParticipant.email_id.in_([m.id for m in messages]),
                EmailParticipant.direct.is_(True),
            )
        )
        by_addr: dict = {}
        for eid, addr in parts:
            by_addr.setdefault(addr, []).append(eid)
        addr_map, _ = await _org_contact_maps(db, user.org_id)
        for addr, eids in by_addr.items():
            hit = addr_map.get(addr)
            if not hit:
                continue
            entry = {"entity_type": hit[0], "entity_id": str(hit[1]), "label": hit[2]}
            for eid in eids:
                bucket = related.setdefault(eid, [])
                if entry not in bucket:
                    bucket.append(entry)

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
                "related": related.get(m.id, []),
            }
            for m in messages
        ],
        "page": page,
        "has_more": has_more,
    }


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

    # Skinny columns — cached bodies are large and this endpoint never
    # returns them.
    stmt = (
        select(
            EmailMessage.id,
            EmailMessage.subject,
            EmailMessage.snippet,
            EmailMessage.from_email,
            EmailMessage.from_name,
            EmailMessage.is_outgoing,
            EmailMessage.sent_at,
            EmailMessage.gmail_id,
            EmailMessage.owner_user_id,
        )
        .join(EmailParticipant, EmailParticipant.email_id == EmailMessage.id)
        .where(EmailMessage.org_id == user.org_id, EmailParticipant.direct.is_(True))
        .distinct()
    )
    if entity_type == "company":
        # A company's correspondence is the union of everyone associated with
        # it: any address at the company's email_domain, PLUS every work/
        # personal address on the people linked to the company (this catches a
        # contact's personal Gmail, which the domain filter alone misses).
        conds = []
        domain = (obj.email_domain or "").lower().strip()
        if domain:
            conds.append(EmailParticipant.email.like(f"%@{domain}"))
        people = await company_people(db, user.org_id, entity_id)
        people_emails = {
            g.normalize_email(e)
            for p in people
            for e in (p.work_email, p.personal_email)
            if e
        }
        if people_emails:
            conds.append(EmailParticipant.email.in_(people_emails))
        if not conds:
            return {"items": []}
        stmt = stmt.where(or_(*conds))
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
    messages = (await db.execute(stmt)).all()
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
            await g._release_db(db)  # no transaction open across the Gmail fetch
            body = await g.fetch_message_body(access, msg.gmail_id)
        except g.MessageGone:
            # Not an upstream failure and not worth a retry: the id belongs
            # to a mailbox that is no longer the connected one.
            raise HTTPException(
                status_code=410,
                detail=(
                    "This message was archived from a previously connected mailbox "
                    "and its full text is no longer available."
                ),
            )
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
