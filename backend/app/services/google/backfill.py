"""Gmail sync: the history feed for new mail and the resumable backfill walk over known contacts' addresses."""

import bisect
import hashlib
import logging
import uuid

from sqlalchemy import update

from app.config import settings
from app.models import (
    GmailSyncFailure,
    GoogleAccount,
    GoogleIntegration,
)
from app.services.contacts import load_contact_index, normalize_email
from app.services.google.client import (
    SEARCH_TIMEOUT,
    GoogleError,
    _get_json,
    _is_rate_limit,
    release_db,
    ensure_access_token,
)
from app.services.google.gmail import (
    _clear_failures,
    _failure_rows,
    _note_failure,
    _retry_failed_messages,
    _store_messages,
)

log = logging.getLogger("google")


ADDRESSES_PER_QUERY = 10
# How many message ids one backfill pass lists before it stops and lets the
# next pass carry on. This bounds a PASS, not the history: the walk's position
# (last finished address + the page token inside the current group's search)
# is saved, so a mailbox with tens of thousands of matches is traversed to the
# end over as many passes as it takes. Nothing past the bound is skipped.
BACKFILL_MAX_IDS_PER_PASS = 4000
# Kept under the old name for the diagnostics that still import it.
BACKFILL_MAX_IDS_PER_CHUNK = BACKFILL_MAX_IDS_PER_PASS
BACKFILL_ADDRESSES_PER_CHECKPOINT = ADDRESSES_PER_QUERY


def _expand_addresses(addresses: list[str]) -> list[str]:
    expanded: list[str] = []
    seen: set[str] = set()
    for a in addresses:
        for variant in (a.lower().strip(), normalize_email(a)):
            if variant and variant not in seen:
                seen.add(variant)
                expanded.append(variant)
    return expanded


def _contact_query(addrs: list[str]) -> str:
    # Header operators match literal addresses; the quoted free-text term
    # catches variants the operators miss (e.g. dotted gmail headers).
    # Over-fetching is fine: the metadata matcher filters on real headers.
    clause = " OR ".join(
        f'from:{a} OR to:{a} OR cc:{a} OR "{a}"' for a in _expand_addresses(addrs)
    )
    # GMAIL_BACKFILL_DAYS=0 means no window at all: every page of every
    # address's history is walked.
    window = (
        f"newer_than:{settings.gmail_backfill_days}d " if settings.gmail_backfill_days > 0 else ""
    )
    return f"{window}-in:spam -in:trash ({clause})"


async def _search_page(access: str, q: str, page_token: str | None = None) -> dict:
    params = {"q": q, "maxResults": 500}
    if page_token:
        params["pageToken"] = page_token
    return await _get_json(
        f"{settings.google_gmail_base}/users/me/messages", access, params,
        timeout=SEARCH_TIMEOUT,
    )


async def _run_search(access: str, q: str, max_ids: int | None = None) -> list[str]:
    """A whole search held in memory — for the bounded callers (suggestion
    scan, diagnostics). The backfill pages through _search_page itself so it
    can checkpoint between pages."""
    ids: list[str] = []
    seen: set[str] = set()
    page_token = None
    while True:
        data = await _search_page(access, q, page_token)
        for m in data.get("messages", []):
            mid = m.get("id")
            if mid and mid not in seen:
                seen.add(mid)
                ids.append(mid)
        if max_ids is not None and len(ids) >= max_ids:
            break  # newest-first; stop once we have enough recent history
        page_token = data.get("nextPageToken")
        if not page_token:
            break
    return ids


async def _search_contact_mail(
    access: str, addresses: list[str], max_ids: int = BACKFILL_MAX_IDS_PER_PASS
) -> list[str]:
    """A capped sample of the mail exchanged with the given addresses — the
    diagnostics endpoints' view of what the backfill would search. NOT the
    backfill itself (see _walk_group): this stops at max_ids."""
    ids: list[str] = []
    seen: set[str] = set()
    for i in range(0, len(addresses), ADDRESSES_PER_QUERY):
        remaining = max_ids - len(ids)
        if remaining <= 0:
            break
        group = addresses[i : i + ADDRESSES_PER_QUERY]
        for mid in await _run_search(access, _contact_query(group), max_ids=remaining):
            if mid not in seen:
                seen.add(mid)
                ids.append(mid)
    return ids


def _backfill_addresses(contact_map: dict) -> list[str]:
    """People's addresses drive the backfill (leads only if opted in), sorted
    so the walk's position is stable across restarts."""
    return sorted(
        a
        for a, ref in contact_map.items()
        if ref.entity_type == "person" or settings.gmail_backfill_leads
    )


def count_backfill_addresses(addr_map: dict) -> int:
    """How long the walk over addr_map would be — the same rule as
    _backfill_addresses, for any map keyed by normalized address whose values
    lead with the entity type (the feed's cached contact map qualifies)."""
    return sum(
        1 for hit in addr_map.values() if hit[0] == "person" or settings.gmail_backfill_leads
    )


def backfill_position(addresses: list[str], after: str | None) -> int:
    """Index of the first address the walk still owes. The walk remembers the
    last address it finished rather than an index, so contacts added or
    removed mid-walk can't shift it past someone unsearched."""
    return bisect.bisect_right(addresses, after) if after else 0


def _query_fingerprint(q: str) -> str:
    return hashlib.sha256(q.encode()).hexdigest()[:16]


async def _walk_group(
    db, account: GoogleAccount, access: str, group: list[str], contact_map: dict,
    budget: int, attempted: set[str],
) -> tuple[bool, int, int]:
    """Search one group of addresses and store what matches, a page at a
    time. Returns (finished, ids_listed, stored).

    After each page is stored, the next page's token is committed (tagged
    with the query it belongs to), so a pass that runs out of budget — or a
    restart — resumes at that page. finished=True only when Gmail reports no
    further page: that is the only thing that lets the caller advance."""
    q = _contact_query(group)
    tag = _query_fingerprint(q)
    token = None
    saved = account.gmail_backfill_page_token or ""
    if saved.startswith(f"{tag}:"):
        token = saved[len(tag) + 1 :]
    listed = stored = 0
    while True:
        await release_db(db)
        try:
            data = await _search_page(access, q, token)
        except GoogleError as exc:
            if token and "(400)" in str(exc):
                # Gmail no longer honours the saved token — start this
                # group's search over; the dedupe absorbs the repeats.
                log.info("gmail backfill page token rejected; restarting the group's search")
                token = None
                account.gmail_backfill_page_token = None
                continue
            raise
        ids = [m["id"] for m in data.get("messages", []) if m.get("id")]
        got, _ = await _store_messages(db, account, access, ids, contact_map, attempted)
        stored += got
        listed += len(ids)
        token = data.get("nextPageToken")
        if not token:
            return True, listed, stored
        position = f"{tag}:{token}"
        resumable = len(position) <= 1000
        if resumable:
            account.gmail_backfill_page_token = position
        await db.commit()  # checkpoint inside the group
        if listed >= budget and resumable:
            return False, listed, stored


async def _reset_given_up(db, user_id: uuid.UUID) -> None:
    await db.execute(
        update(GmailSyncFailure)
        .where(GmailSyncFailure.user_id == user_id, GmailSyncFailure.given_up.is_(True))
        .values(given_up=False, attempts=0)
    )


async def sync_gmail(
    db, cfg: GoogleIntegration, account: GoogleAccount, force_backfill: bool = False,
    progress: dict | None = None,
) -> int:
    """Targeted backfill (searches all history with known contacts, or the
    configured window), then the history feed for new mail. Only mail involving known People/Leads is
    stored. force_backfill re-runs the search — how newly added contacts get
    their history pulled.

    The backfill runs in bounded passes and is only ever marked done when the
    walk has gone past the last address with every search completed. When a
    pass stops only because its budget ran out, progress["backfill_paused"]
    is set so the loop knows to come back soon."""
    access = await ensure_access_token(db, cfg, account)
    contact_map = (await load_contact_index(db, account.org_id)).by_email
    if not contact_map:
        return 0
    stored_total = 0
    attempted: set[str] = set()

    if force_backfill:
        # An explicit re-run is also a fresh chance for anything given up on.
        await _reset_given_up(db, account.user_id)
        if account.gmail_backfill_done:
            # Restart the walk — how newly added contacts get their history.
            account.gmail_backfill_done = False
            account.gmail_backfill_cursor = 0
            account.gmail_backfill_after = None
            account.gmail_backfill_page_token = None

    # Messages earlier passes couldn't fetch come first, so a transient
    # failure heals on the next pass no matter where the checkpoints are.
    stored_total += await _retry_failed_messages(db, account, access, contact_map, attempted)

    if not account.gmail_backfill_done:
        # Snapshot the history cursor FIRST so mail arriving mid-backfill
        # isn't missed once the incremental feed takes over.
        if not account.gmail_history_id:
            await release_db(db)
            profile = await _get_json(f"{settings.google_gmail_base}/users/me/profile", access)
            account.gmail_history_id = str(profile.get("historyId") or "")
            await db.commit()
        addresses = _backfill_addresses(contact_map)
        # Stamped per pass (the list is rebuilt each time anyway) so the
        # status poll can report progress without sizing the walk itself.
        account.gmail_backfill_total = len(addresses)
        pos = backfill_position(addresses, account.gmail_backfill_after)
        account.gmail_backfill_cursor = pos
        address_failures = await _failure_rows(db, account.user_id, "address")
        budget = BACKFILL_MAX_IDS_PER_PASS
        single_until = 0  # addresses below this index are searched one by one
        while pos < len(addresses):
            if pos < single_until:
                group = [addresses[pos]]
            else:
                group = addresses[pos : pos + ADDRESSES_PER_QUERY]
            try:
                finished, listed, stored = await _walk_group(
                    db, account, access, group, contact_map, budget, attempted
                )
            except GoogleError as exc:
                if _is_rate_limit(exc):
                    # Quota pressure: keep the checkpoint, resume next pass.
                    account.sync_error = "Gmail rate limited — backfill resumes automatically"
                    await db.commit()
                    log.warning("gmail backfill rate limited at %s/%s", pos, len(addresses))
                    return stored_total
                if len(group) > 1:
                    # A combined query that fails any other way — usually a
                    # timeout on a large mailbox — degrades to one address at
                    # a time so a single heavy contact can't jam the rest.
                    log.warning("combined gmail search failed (%s); retrying per address", exc)
                    single_until = pos + len(group)
                    account.gmail_backfill_page_token = None
                    continue
                # One address's search failed. The walk does not move past
                # it: the failure is counted and the pass ends here, to try
                # again next time. Only after MAX_FAILURE_ATTEMPTS is the
                # address recorded as given up and stepped over.
                given_up = _note_failure(
                    db, address_failures, account.user_id, "address", group[0], exc
                )
                if not given_up:
                    account.sync_error = (
                        f"Gmail search failed for {group[0]} — the backfill will retry it"
                    )[:500]
                    await db.commit()
                    return stored_total
                finished, listed, stored = True, 0, 0
            else:
                if finished:
                    await _clear_failures(db, address_failures, group)
            stored_total += stored
            budget -= listed
            if finished:
                pos += len(group)
                account.gmail_backfill_after = group[-1]
                account.gmail_backfill_cursor = pos
                account.gmail_backfill_page_token = None
            account.sync_error = None
            await db.commit()  # checkpoint: finished work survives anything
            if not finished or (budget <= 0 and pos < len(addresses)):
                # Pass budget spent with history still to walk; the saved
                # position picks it up on the next pass.
                log.info("gmail backfill paused at %s/%s for %s", pos, len(addresses), account.user_id)
                if progress is not None:
                    progress["backfill_paused"] = True
                return stored_total
        account.gmail_backfill_done = True
        account.gmail_backfill_cursor = 0
        account.gmail_backfill_total = None
        account.gmail_backfill_after = None
        account.gmail_backfill_page_token = None
    elif account.gmail_history_id:
        ids = []
        page_token = None
        newest_history = account.gmail_history_id
        await release_db(db)
        try:
            while True:
                params = {
                    "startHistoryId": account.gmail_history_id,
                    "historyTypes": "messageAdded",
                    "maxResults": 500,
                }
                if page_token:
                    params["pageToken"] = page_token
                data = await _get_json(
                    f"{settings.google_gmail_base}/users/me/history", access, params
                )
                for h in data.get("history", []):
                    for added in h.get("messagesAdded", []):
                        mid = (added.get("message") or {}).get("id")
                        if mid:
                            ids.append(mid)
                newest_history = str(data.get("historyId") or newest_history)
                page_token = data.get("nextPageToken")
                if not page_token:
                    break
        except GoogleError as exc:
            if "(404)" in str(exc):
                # Cursor expired — re-run the backfill window; dedupe makes it cheap.
                account.gmail_backfill_done = False
                account.gmail_history_id = None
                account.gmail_backfill_cursor = 0
                account.gmail_backfill_after = None
                account.gmail_backfill_page_token = None
                return stored_total
            raise
        # A message that won't fetch is queued for retry inside, which is
        # what makes it safe to move the history id past it below.
        stored, _ = await _store_messages(db, account, access, ids, contact_map, attempted)
        stored_total += stored
        # Advanced only after every batch is in: an interruption re-walks
        # this history window next pass and the dedupe absorbs the repeats.
        account.gmail_history_id = newest_history

    return stored_total
