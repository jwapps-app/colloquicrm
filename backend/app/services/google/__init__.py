"""Google Workspace integration: per-user OAuth, Contacts import, Calendar and
Gmail sync.

Auth model: the org registers its own OAuth client (client_id/secret from
Google Cloud console, stored org-level); each user then connects their own
Google account through the authorization-code flow with offline access, and
we keep a per-user refresh token. Google is never a login method for the CRM
itself.

Everything is read-only on Google's side: contacts feed the existing import
machinery (same duplicate detection and commit path as CSV), calendar events
sync into a local table matched to CRM records by attendee email, and mail to
and from known contacts is archived.

Layout — one concern per module:

  client       HTTP pool, bearer requests, access tokens, GoogleError
  oauth        scopes, consent URL, signed state, code exchange
  people       Google Contacts -> import rows
  calendar     calendar event sync
  gmail        message parsing, storage, the retry queue, body fetch
  backfill     the Gmail sync pass: history feed + resumable backfill walk
  suggestions  suggested contacts from recent mail
  sync         per-account claims, error recording, manual and scheduled passes
  diagnostics  admin troubleshooting probes

Callers outside the package use the names re-exported here.
"""

from app.services.google import diagnostics
from app.services.google.backfill import (
    ADDRESSES_PER_QUERY,
    backfill_position,
    count_backfill_addresses,
    sync_gmail,
)
from app.services.google.calendar import sync_calendar
from app.services.google.client import (
    GoogleError,
    aclose,
    ensure_access_token,
    get_userinfo,
    release_db,
    revoke,
)
from app.services.google.gmail import (
    MAX_FAILURE_ATTEMPTS,
    MessageGone,
    failure_counts,
    fetch_message_body,
    reset_mailbox_state,
)
from app.services.google.oauth import (
    GMAIL_SCOPE,
    SCOPES,
    STATE_TTL_SECONDS,
    auth_url,
    check_state,
    exchange_code,
    has_gmail_scope,
    hash_nonce,
    make_state,
    redirect_uri,
)
from app.services.google.people import contact_to_row, fetch_contacts
from app.services.google.suggestions import scan_contact_suggestions
from app.services.google.sync import (
    recompute_all_person_metrics,
    release_sync,
    run_sync_pass,
    sync_account,
    sync_account_background,
    sync_in_progress,
    sync_loop,
    try_claim_sync,
)

__all__ = [
    "ADDRESSES_PER_QUERY",
    "GMAIL_SCOPE",
    "MAX_FAILURE_ATTEMPTS",
    "SCOPES",
    "STATE_TTL_SECONDS",
    "GoogleError",
    "MessageGone",
    "aclose",
    "auth_url",
    "backfill_position",
    "check_state",
    "diagnostics",
    "contact_to_row",
    "count_backfill_addresses",
    "ensure_access_token",
    "exchange_code",
    "failure_counts",
    "fetch_contacts",
    "fetch_message_body",
    "get_userinfo",
    "has_gmail_scope",
    "hash_nonce",
    "make_state",
    "recompute_all_person_metrics",
    "redirect_uri",
    "release_db",
    "release_sync",
    "reset_mailbox_state",
    "revoke",
    "run_sync_pass",
    "scan_contact_suggestions",
    "sync_account",
    "sync_account_background",
    "sync_calendar",
    "sync_gmail",
    "sync_in_progress",
    "sync_loop",
    "try_claim_sync",
]
