import hmac
import secrets
import uuid
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import get_db
from app.deps import get_current_user, require_admin
from app.models import (
    EmailMessage,
    GoogleAccount,
    GoogleIntegration,
    User,
    utcnow,
)
from app.services import contacts
from app.services import google as g
from app.services.background import spawn
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


async def _connected(db: AsyncSession, user: User) -> tuple[GoogleIntegration, GoogleAccount]:
    """The org's OAuth client and this user's connected account, or a 400."""
    account = await _account(db, user.id)
    cfg = await _cfg(db, user.org_id)
    if account is None or cfg is None:
        raise HTTPException(status_code=400, detail="Google is not connected")
    return cfg, account


@router.get("/status")
async def status(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    cfg = await _cfg(db, user.org_id)
    account = await _account(db, user.id)
    backfill_total = None
    backfill_done = bool(account.gmail_backfill_done) if account else False
    if account and g.has_gmail_scope(account) and not backfill_done:
        # The sync stamps the walk's length when it starts. Before the first
        # pass has run there is nothing stamped yet, so size the walk from the
        # short-lived shared contact index rather than rebuilding the org's
        # address book on every poll.
        backfill_total = account.gmail_backfill_total
        if backfill_total is None:
            index = await contacts.cached_contact_index(db, user.org_id)
            backfill_total = g.count_backfill_addresses(index.by_email)
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
    await g.release_db(db)  # the token exchange must not sit inside a transaction
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
    cfg, account = await _connected(db, admin)
    return await g.diagnostics.probe_connection(cfg, account)


@router.get("/diagnose-backfill")
async def diagnose_backfill(admin: User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    """Run the exact backfill step for the current cursor chunk — the search
    and the dedup query — synchronously, right now, and report what happens.
    Reproduces the failing path live without waiting on the background loop."""
    cfg, account = await _connected(db, admin)
    return await g.diagnostics.probe_backfill(db, cfg, account)


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
    cfg, account = await _connected(db, user)
    try:
        return await g.diagnostics.trace_address(db, cfg, account, email, q)
    except g.GoogleError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


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
        await g.release_db(db)  # no transaction open across the People API walk
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
