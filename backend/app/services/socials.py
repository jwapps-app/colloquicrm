"""Find a person's social profiles from data we already have.

Two free, private sources: URLs in the signatures of emails the person
wrote to us (their signature, not ours — only inbound mail they authored is
scanned), and their Gravatar profile's verified accounts. Nothing about the
contact is sent to any third party beyond a one-way email hash to Gravatar.
"""

import hashlib
import logging
import re

import httpx
from sqlalchemy import select

from app.config import settings
from app.models import (
    EmailMessage,
    EmailParticipant,
    GoogleAccount,
    GoogleIntegration,
    Person,
    utcnow,
)
from app.services.google import (
    GoogleError,
    ensure_access_token,
    fetch_message_body,
    normalize_email,
)

log = logging.getLogger("socials")

MESSAGES_TO_SCAN = 15
BODIES_TO_FETCH = 8  # cap live Gmail calls per run

LINKEDIN_RE = re.compile(
    r"https?://(?:[\w-]+\.)?linkedin\.com/(?:in|company)/[A-Za-z0-9\-_%.]+", re.I
)
FACEBOOK_RE = re.compile(r"https?://(?:www\.)?facebook\.com/[A-Za-z0-9.\-_]+", re.I)
FB_NOT_PROFILES = {
    "sharer", "share", "share.php", "events", "groups", "plugins", "tr",
    "profile.php", "pages", "login", "home.php", "hashtag", "watch",
    "story.php", "photo.php", "dialog", "l.php",
}


def _clean(url: str) -> str:
    url = url.split("?", 1)[0].split("#", 1)[0]
    return url.rstrip(".,;:)]}>\"'")


def extract_socials(text: str) -> dict[str, list[str]]:
    found = {"linkedin": [], "facebook": []}
    for m in LINKEDIN_RE.finditer(text):
        url = _clean(m.group(0))
        if url not in found["linkedin"]:
            found["linkedin"].append(url)
    for m in FACEBOOK_RE.finditer(text):
        url = _clean(m.group(0))
        handle = url.rstrip("/").rsplit("/", 1)[-1].lower()
        if handle in FB_NOT_PROFILES or len(handle) < 3:
            continue
        if url not in found["facebook"]:
            found["facebook"].append(url)
    return found


async def _gravatar_accounts(email: str) -> dict[str, list[str]]:
    found = {"linkedin": [], "facebook": []}
    digest = hashlib.md5(email.lower().strip().encode()).hexdigest()
    try:
        async with httpx.AsyncClient(timeout=6.0) as client:
            resp = await client.get(
                f"{settings.gravatar_base}/{digest}.json",
                headers={"User-Agent": "crm-app"},
                follow_redirects=True,
            )
    except httpx.HTTPError:
        return found
    if resp.status_code != 200:
        return found
    try:
        entries = resp.json().get("entry") or []
    except Exception:
        return found
    for entry in entries:
        for account in entry.get("accounts") or []:
            shortname = (account.get("shortname") or "").lower()
            url = _clean(account.get("url") or "")
            if shortname in found and url and url not in found[shortname]:
                found[shortname].append(url)
    return found


async def _ensure_body(db, msg: EmailMessage) -> bool:
    """Best-effort lazy body fetch via the mailbox owner's Google account."""
    if msg.body_fetched_at is not None:
        return True
    if msg.owner_user_id is None:
        return False
    account = (
        await db.execute(select(GoogleAccount).where(GoogleAccount.user_id == msg.owner_user_id))
    ).scalar_one_or_none()
    cfg = (
        await db.execute(
            select(GoogleIntegration).where(GoogleIntegration.org_id == msg.org_id)
        )
    ).scalar_one_or_none()
    if account is None or cfg is None:
        return False
    try:
        access = await ensure_access_token(db, cfg, account)
        body = await fetch_message_body(access, msg.gmail_id)
    except GoogleError as exc:
        log.warning("body fetch failed for %s: %s", msg.gmail_id, exc)
        return False
    msg.body_text = body.get("text")
    msg.body_html = body.get("html")
    msg.body_fetched_at = utcnow()
    return True


async def find_for_person(db, person: Person) -> dict:
    emails = [e for e in (person.work_email, person.personal_email) if e]
    normalized = [normalize_email(e) for e in emails]
    results = {"linkedin": [], "facebook": []}
    scanned = 0
    fetched = 0

    if normalized:
        # Only mail the person authored — that's where THEIR signature lives.
        messages = (
            (
                await db.execute(
                    select(EmailMessage)
                    .join(EmailParticipant, EmailParticipant.email_id == EmailMessage.id)
                    .where(
                        EmailMessage.org_id == person.org_id,
                        EmailMessage.is_outgoing.is_(False),
                        EmailParticipant.email.in_(normalized),
                        EmailParticipant.kind == "from",
                    )
                    .distinct()
                    .order_by(EmailMessage.sent_at.desc())
                    .limit(MESSAGES_TO_SCAN)
                )
            )
            .scalars()
            .all()
        )
        for msg in messages:
            if msg.body_fetched_at is None:
                if fetched >= BODIES_TO_FETCH:
                    continue
                fetched += 1
                if not await _ensure_body(db, msg):
                    continue
            text = " ".join(filter(None, [msg.body_text, msg.body_html]))
            if not text:
                continue
            scanned += 1
            for network, urls in extract_socials(text).items():
                for url in urls:
                    if url not in results[network]:
                        results[network].append(url)

    for email in emails:
        grav = await _gravatar_accounts(email)
        for network, urls in grav.items():
            for url in urls:
                if url not in results[network]:
                    results[network].append(url)

    return {
        "linkedin": results["linkedin"][:3],
        "facebook": results["facebook"][:3],
        "emails_scanned": scanned,
        "bodies_fetched": fetched,
    }
