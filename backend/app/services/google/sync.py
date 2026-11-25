"""Sync orchestration: one account at a time, error recording, the manual and scheduled passes."""

import asyncio
import logging
import uuid

from sqlalchemy import select

from app.db import SessionLocal
from app.models import (
    GoogleAccount,
    GoogleIntegration,
    User,
)
from app.services.google.backfill import sync_gmail
from app.services.google.calendar import sync_calendar
from app.services.google.client import GoogleError
from app.services.google.oauth import has_gmail_scope

log = logging.getLogger("google")


SYNC_INTERVAL_SECONDS = 1800
# A backfill pass is bounded (BACKFILL_MAX_IDS_PER_PASS); while any mailbox
# still has history left to walk, the loop comes back sooner than the regular
# interval so "all history" finishes in hours, not weeks.
BACKFILL_CONTINUE_SECONDS = 60
# Ceiling on the back-to-back passes one manual sync will take (each lists up
# to BACKFILL_MAX_IDS_PER_PASS ids); the scheduled loop finishes anything left.
MANUAL_SYNC_MAX_PASSES = 250


async def sync_account(db, account: GoogleAccount, force_backfill: bool = False) -> dict:
    cfg = (
        await db.execute(
            select(GoogleIntegration).where(GoogleIntegration.org_id == account.org_id)
        )
    ).scalar_one_or_none()
    if cfg is None:
        raise GoogleError("Google integration is not configured")
    events = await sync_calendar(db, cfg, account)
    progress: dict = {}
    emails = (
        await sync_gmail(db, cfg, account, force_backfill=force_backfill, progress=progress)
        if has_gmail_scope(account)
        else 0
    )
    return {
        "events_synced": events,
        "emails_synced": emails,
        "backfill_paused": bool(progress.get("backfill_paused")),
    }


def _sync_error_text(exc: Exception) -> str:
    # GoogleError messages are already curated; anything else gets its type
    # named so the status is never a mystery.
    if isinstance(exc, GoogleError):
        return str(exc)[:500]
    return f"Sync failed: {type(exc).__name__}: {exc}"[:500]


async def _record_sync_error(user_id: uuid.UUID, exc: Exception) -> None:
    # Its own session: the one the sync used may be mid-transaction or hold
    # objects a rollback expired, and nothing here should depend on either.
    async with SessionLocal() as db:
        account = (
            await db.execute(select(GoogleAccount).where(GoogleAccount.user_id == user_id))
        ).scalar_one_or_none()
        if account is not None:
            account.sync_error = _sync_error_text(exc)
            await db.commit()


# ---- one sync at a time per account ----
#
# A manual sync and the scheduled pass would otherwise race on the same
# checkpoints and unique message rows. The app is a single process, so an
# in-process claim is enough; it is taken synchronously (no await between the
# check and the claim), which is what makes it race-free on one event loop.

_syncing: set[uuid.UUID] = set()


def try_claim_sync(user_id: uuid.UUID) -> bool:
    if user_id in _syncing:
        return False
    _syncing.add(user_id)
    return True


def release_sync(user_id: uuid.UUID) -> None:
    _syncing.discard(user_id)


def sync_in_progress(user_id: uuid.UUID) -> bool:
    return user_id in _syncing


async def _sync_claimed_account(user_id: uuid.UUID, label: str, force_backfill: bool) -> bool:
    """Run one account's sync in a session of its own. The caller holds the
    claim. Returns True when the backfill stopped only because the pass
    budget ran out (so the loop should come back soon) — not when it is rate
    limited, holding on a failed search, or has nothing to walk."""
    try:
        async with SessionLocal() as db:
            account = (
                await db.execute(select(GoogleAccount).where(GoogleAccount.user_id == user_id))
            ).scalar_one_or_none()
            if account is None:
                return False
            result = await sync_account(db, account, force_backfill=force_backfill)
            await db.commit()
            return result["backfill_paused"]
    except Exception as exc:
        # Record ANY failure, not just GoogleError — an unrecorded crash
        # leaves a stale status that looks like nothing changed. `label` is a
        # plain string captured up front: after a failed transaction the ORM
        # object's attributes are expired and must not be touched.
        log.warning("Google sync failed for %s: %s", label, exc)
        try:
            await _record_sync_error(user_id, exc)
        except Exception:
            log.exception("could not record the Google sync error for %s", label)
        return False


async def run_sync_pass() -> bool:
    """Sync every connected mailbox whose CRM user is still active. Accounts
    are read as plain tuples and each one syncs in a fresh session, so one
    account's failure (and the rollback that follows) can't poison the next.
    Returns True when some mailbox still has backfill to continue."""
    async with SessionLocal() as db:
        accounts = (
            await db.execute(
                select(GoogleAccount.user_id, GoogleAccount.email)
                .join(User, User.id == GoogleAccount.user_id)
                # A deactivated user's mailbox stops syncing the moment they
                # are switched off; the stored grant is left for an admin to
                # unlink or for reactivation to resume.
                .where(User.is_active.is_(True))
                .order_by(GoogleAccount.connected_at)
            )
        ).all()
    more = False
    for user_id, email in accounts:
        if not try_claim_sync(user_id):
            log.info("Google sync for %s already running; skipping this pass", email)
            continue
        try:
            more = await _sync_claimed_account(user_id, email, False) or more
        finally:
            release_sync(user_id)
    return more


async def recompute_all_person_metrics(org_id: uuid.UUID) -> None:
    """Rebuild interaction counts / last-contacted for every person in the
    org from stored emails and phone events. Repair tool for data synced
    before metrics were tracked per checkpoint."""
    from app.models import Person
    from app.services.interactions import update_person_aggregates

    async with SessionLocal() as db:
        ids = [
            pid
            for (pid,) in await db.execute(
                select(Person.id).where(
                    Person.org_id == org_id, Person.deleted_at.is_(None)
                )
            )
        ]
        for i in range(0, len(ids), 200):
            await update_person_aggregates(db, org_id, set(ids[i : i + 200]))
            await db.commit()
        log.info("recomputed interaction metrics for %s people", len(ids))


async def sync_account_background(
    user_id: uuid.UUID, force_backfill: bool = False, claimed: bool = False,
    label: str | None = None,
) -> None:
    """A user-triggered sync, detached from its request — a full backfill can
    run for many minutes, far past any request timeout. `claimed` means the
    caller already took the per-account claim (the endpoint does, so it can
    answer "already running" on the spot); it is released here either way."""
    if not claimed and not try_claim_sync(user_id):
        log.info("Google sync for %s already running; not starting another", user_id)
        return
    try:
        # A pass is bounded; someone who pressed Sync wants the whole walk, so
        # keep taking passes while the only thing stopping it is the budget.
        # Only the first one may be forced — forcing again would restart the
        # walk it just began.
        for _ in range(MANUAL_SYNC_MAX_PASSES):
            paused = await _sync_claimed_account(user_id, label or str(user_id), force_backfill)
            if not paused:
                break
            force_backfill = False
    finally:
        release_sync(user_id)


async def sync_loop() -> None:
    while True:
        more = False
        try:
            more = await run_sync_pass()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Google sync pass failed")
        await asyncio.sleep(BACKFILL_CONTINUE_SECONDS if more else SYNC_INTERVAL_SECONDS)
