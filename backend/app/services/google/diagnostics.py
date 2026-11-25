"""Admin troubleshooting probes: run one piece of the sync right now, from the
server, and report exactly what happened. None of these change sync state."""

import time
import traceback

import httpx
from sqlalchemy import func, select

from app.config import settings
from app.models import EmailMessage, EmailParticipant, GoogleAccount, GoogleIntegration
from app.services.contacts import load_contact_index, normalize_email
from app.services.google.backfill import (
    ADDRESSES_PER_QUERY,
    _backfill_addresses,
    _search_contact_mail,
    backfill_position,
)
from app.services.google.client import _get_json, ensure_access_token, release_db
from app.services.google.gmail import _fetch_message, _known_gmail_ids, _parse_message


async def probe_connection(cfg: GoogleIntegration, account: GoogleAccount) -> dict:
    """Try the OAuth token refresh and one Gmail call, with timings and
    precise errors. Uses its own short-lived HTTP clients, so it works even
    when the sync loop (and its shared pool) is stuck."""
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


async def probe_backfill(db, cfg: GoogleIntegration, account: GoogleAccount) -> dict:
    """Run the backfill step for the current cursor chunk — the search and
    the dedup query — synchronously, and report what happens. Any failure is
    part of the answer, not an error."""
    out: dict = {"cursor": account.gmail_backfill_cursor, "done": bool(account.gmail_backfill_done)}
    try:
        access = await ensure_access_token(db, cfg, account)
        contact_map = (await load_contact_index(db, account.org_id)).by_email
        addresses = _backfill_addresses(contact_map)
        out["total_addresses"] = len(addresses)
        cursor = backfill_position(addresses, account.gmail_backfill_after)
        out["cursor"] = cursor
        out["mid_group_page_saved"] = bool(account.gmail_backfill_page_token)
        chunk = addresses[cursor : cursor + ADDRESSES_PER_QUERY]
        out["chunk_size"] = len(chunk)
        out["sample_addresses"] = chunk[:5]

        await release_db(db)
        t0 = time.monotonic()
        ids = await _search_contact_mail(access, chunk)
        out["search_ms"] = round((time.monotonic() - t0) * 1000)
        out["search_hits"] = len(ids)

        t0 = time.monotonic()
        known = await _known_gmail_ids(db, account.user_id, ids)
        out["dedup_ms"] = round((time.monotonic() - t0) * 1000)
        out["already_stored"] = len(known)
        out["unseen"] = len(ids) - len(known)
        out["step_result"] = "search + dedup OK on current code"
    except Exception as exc:
        out["error_type"] = type(exc).__name__
        out["error"] = str(exc)[:400]
        out["traceback_tail"] = traceback.format_exc()[-600:]
    return out


async def trace_address(
    db, cfg: GoogleIntegration, account: GoogleAccount, email: str, q: str | None = None
) -> dict:
    """Follow one address through the email-sync pipeline: is it a CRM
    contact, what does the Gmail search return, how do the messages parse,
    and what is already stored. `q` replaces the address search with a raw
    Gmail query. Raises GoogleError when Google can't be asked."""
    addr = normalize_email(email)
    owner = (account.email or "").lower().strip()
    access = await ensure_access_token(db, cfg, account)
    await release_db(db)  # no transaction open across the Gmail calls
    # Ask Gmail directly whose mailbox this token opens — the stored email
    # can lie if the consent screen picked a different account.
    profile = await _get_json(f"{settings.google_gmail_base}/users/me/profile", access)
    if q:
        data = await _get_json(
            f"{settings.google_gmail_base}/users/me/messages",
            access,
            {"q": q, "maxResults": 100},
        )
        ids = [m["id"] for m in data.get("messages", []) if m.get("id")]
    else:
        ids = await _search_contact_mail(access, [addr])
    contact_map = (await load_contact_index(db, account.org_id)).by_email
    samples = []
    for gid in ids[:5]:
        item = await _fetch_message(access, gid, full=False)
        if item is None:
            continue
        parsed = _parse_message(item, owner)
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
                    f"{contact_map[e].entity_type}:{e}"
                    for _, e, _ in participants
                    if e != owner and e in contact_map
                ],
            }
        )
    stored = (
        await db.execute(
            select(func.count(func.distinct(EmailMessage.id)))
            .select_from(EmailMessage)
            .join(EmailParticipant, EmailParticipant.email_id == EmailMessage.id)
            .where(EmailMessage.org_id == account.org_id, EmailParticipant.email == addr)
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
