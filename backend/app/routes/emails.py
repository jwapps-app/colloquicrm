"""Reading the archived email: search, one record's correspondence, and a
message's full body. Where the mail comes from is the Google integration's
business (routes/google.py, services/google.py); this is just the archive."""

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps import get_current_user
from app.models import (
    EmailMessage,
    EmailParticipant,
    GoogleAccount,
    GoogleIntegration,
    User,
    utcnow,
)
from app.schemas import EmailListOut, EmailSearchOut
from app.services import google as g
from app.services import timeline

router = APIRouter()


async def _google_config(db: AsyncSession, org_id: uuid.UUID) -> GoogleIntegration | None:
    return (
        await db.execute(select(GoogleIntegration).where(GoogleIntegration.org_id == org_id))
    ).scalar_one_or_none()


@router.get("/search", response_model=EmailSearchOut)
async def search_emails(
    q: str,
    page: int = 1,
    page_size: int = 25,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Full-text search across every synced email — subject, snippet, sender,
    and archived body — newest first."""
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
    # the leading-wildcard match.
    cols = [
        EmailMessage.subject,
        EmailMessage.snippet,
        EmailMessage.from_name,
        EmailMessage.from_email,
        EmailMessage.body_text,
    ]
    stmt = (
        select(*timeline.EMAIL_COLUMNS)
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
    related = await timeline.email_related(db, user.org_id, [m.id for m in messages])
    return {
        "items": [
            {**timeline.email_row(m), "related": related.get(m.id, [])} for m in messages
        ],
        "page": page,
        "has_more": has_more,
    }


@router.get("", response_model=EmailListOut)
async def emails_for_entity(
    entity_type: str,
    entity_id: uuid.UUID,
    limit: int = 50,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """The newest emails a person, lead or company was directly part of."""
    scope = await timeline.require_scope(
        db, user.org_id, entity_type, entity_id, correspondents_only=True
    )
    belongs = timeline.email_belongs(scope)
    if belongs is None:
        return {"items": []}
    stmt = (
        select(*timeline.EMAIL_COLUMNS)
        .join(EmailParticipant, EmailParticipant.email_id == EmailMessage.id)
        .where(EmailMessage.org_id == user.org_id, EmailParticipant.direct.is_(True), belongs)
        .distinct()
        .order_by(EmailMessage.sent_at.desc())
        .limit(min(max(limit, 1), 200))
    )
    messages = (await db.execute(stmt)).all()
    return {"items": [timeline.email_row(m) for m in messages]}


@router.get("/{email_id}/body")
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
        cfg = await _google_config(db, user.org_id)
        if owner_account is None or cfg is None:
            raise HTTPException(
                status_code=409,
                detail="The mailbox owner is no longer connected to Google",
            )
        try:
            access = await g.ensure_access_token(db, cfg, owner_account)
            await g.release_db(db)  # no transaction open across the Gmail fetch
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
