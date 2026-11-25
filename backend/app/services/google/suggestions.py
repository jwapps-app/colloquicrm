"""Contact suggestions: frequent correspondents who are not in the CRM yet."""

import asyncio
import logging
import uuid

from sqlalchemy import select

from app.db import SessionLocal
from app.models import (
    ContactSuggestion,
    GoogleAccount,
    GoogleIntegration,
)
from app.services.contacts import load_contact_index, normalize_email
from app.services.google.backfill import _run_search
from app.services.google.client import GoogleError, release_db, ensure_access_token
from app.services.google.gmail import _fetch_message, _parse_message
from app.services.google.oauth import has_gmail_scope

log = logging.getLogger("google")


# How many recent messages to scan per mailbox folder when suggesting contacts.
SUGGESTION_SCAN_LIMIT = 400
# Local-part tokens that are obviously automated — never worth suggesting as
# a contact. Matched against the LOCAL PART only, so a real address like
# greenews@example.com isn't caught by "news".
SUGGESTION_LOCAL_EXACT = {
    "noreply", "no-reply", "donotreply", "do-not-reply", "no_reply",
    "mailer-daemon", "postmaster", "bounce", "notifications", "notification",
    "alerts", "updates", "news", "newsletter", "support", "info", "hello",
    "automated", "marketing", "billing", "sales",
}
SUGGESTION_LOCAL_PREFIXES = ("noreply", "no-reply", "donotreply", "do-not-reply", "bounce")


def _looks_automated(email: str) -> bool:
    local = email.lower().split("@", 1)[0]
    return local in SUGGESTION_LOCAL_EXACT or local.startswith(SUGGESTION_LOCAL_PREFIXES)


async def scan_contact_suggestions(user_id: uuid.UUID) -> None:
    """Scan recent sent/received mail for frequent correspondents who aren't
    CRM contacts yet, and upsert them as suggestions. On-demand and bounded."""
    async with SessionLocal() as db:
        account = (
            await db.execute(select(GoogleAccount).where(GoogleAccount.user_id == user_id))
        ).scalar_one_or_none()
        if account is None or not has_gmail_scope(account):
            return
        cfg = (
            await db.execute(
                select(GoogleIntegration).where(GoogleIntegration.org_id == account.org_id)
            )
        ).scalar_one_or_none()
        if cfg is None:
            return
        try:
            access = await ensure_access_token(db, cfg, account)
            owner_email = normalize_email(account.email)
            # Fetch first, write after: the scan is up to two searches and
            # several hundred message reads, none of which may sit inside a
            # transaction. This also persists a refreshed token.
            await release_db(db)

            tally: dict[str, dict] = {}
            for query in ("in:sent", "in:inbox"):
                ids = await _run_search(
                    access, f"{query} -in:spam -in:trash", max_ids=SUGGESTION_SCAN_LIMIT
                )
                for i in range(0, len(ids), 10):
                    chunk = ids[i : i + 10]
                    items = await asyncio.gather(
                        *[_fetch_message(access, g, False) for g in chunk],
                        return_exceptions=True,
                    )
                    for item in items:
                        if not isinstance(item, dict):
                            continue
                        parsed = _parse_message(item, owner_email)
                        if parsed is None:
                            continue
                        sent_at = parsed["sent_at"]
                        outgoing = bool(parsed["is_outgoing"])
                        for kind, email, name in parsed["participants"]:
                            if not email or email == owner_email or "@" not in email:
                                continue
                            if _looks_automated(email):
                                continue
                            # "You emailed them" = you sent to this recipient.
                            # "They emailed you" = they are the sender inbound.
                            counts = outgoing and kind in ("to", "cc")
                            counts = counts or (not outgoing and kind == "from")
                            if not counts:
                                continue
                            t = tally.setdefault(
                                email, {"count": 0, "out": 0, "name": None, "last": None}
                            )
                            t["count"] += 1
                            if outgoing:
                                t["out"] += 1
                            if name and not t["name"]:
                                t["name"] = name
                            if sent_at and (t["last"] is None or sent_at > t["last"]):
                                t["last"] = sent_at

            # The network part is over; everything from here is local. Read
            # the CRM's addresses and the standing suggestions now, so they
            # are as fresh as the write that follows.
            # already-known addresses
            contact_map = (await load_contact_index(db, account.org_id)).by_email
            existing = {
                s.email: s
                for s in (
                    await db.execute(
                        select(ContactSuggestion).where(
                            ContactSuggestion.org_id == account.org_id
                        )
                    )
                ).scalars()
            }
            for email, t in tally.items():
                if email in contact_map:  # already a Person/Lead
                    continue
                # A single message you never replied to isn't worth suggesting.
                # Qualify only if you emailed them at least once, or there's a
                # real back-and-forth (2+ messages).
                if t["out"] == 0 and t["count"] < 2:
                    continue
                s = existing.get(email)
                if s is None:
                    db.add(
                        ContactSuggestion(
                            org_id=account.org_id, email=email, display_name=t["name"],
                            message_count=t["count"], last_seen_at=t["last"], status="pending",
                        )
                    )
                else:
                    # Keep ignored/added decisions; just refresh the stats.
                    s.message_count = t["count"]
                    if t["name"] and not s.display_name:
                        s.display_name = t["name"]
                    if t["last"] and (s.last_seen_at is None or t["last"] > s.last_seen_at):
                        s.last_seen_at = t["last"]
            await db.commit()
            log.info("contact-suggestion scan for %s: %s candidates", account.email, len(tally))
        except GoogleError as exc:
            log.warning("suggestion scan failed for %s: %s", account.email, exc)
