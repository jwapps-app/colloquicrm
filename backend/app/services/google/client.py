"""Talking to Google: the shared HTTP pool, bearer requests, and access tokens."""

import asyncio
import logging
from datetime import timedelta, timezone

import httpx

from app.config import settings
from app.models import (
    GoogleAccount,
    GoogleIntegration,
    utcnow,
)

log = logging.getLogger("google")


class GoogleError(Exception):
    """status_code is Google's HTTP status when it answered with an error,
    None when it could not be reached at all."""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


def _is_rate_limit(exc: "GoogleError") -> bool:
    s = str(exc)
    return "(429)" in s or "(403)" in s


# The combined contact search (and a full-format message fetch) is the
# heaviest request in the system; give it room before calling it a timeout.
SEARCH_TIMEOUT = 45.0


_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    # One keep-alive pool for every Google call. A backfill makes thousands
    # of requests and each fresh AsyncClient was a TLS handshake; the pool is
    # sized for the ten-wide message fetches. Per-call timeouts and the
    # bearer header are passed per request — nothing is baked in.
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            timeout=30.0,
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )
    return _client


async def aclose() -> None:
    """Drain the shared pool; called once from app shutdown."""
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


async def release_db(db) -> None:
    """End the session's transaction before waiting on Google.

    Every network call in the sync path sits behind one of these: the
    connection goes back to the pool and Postgres holds no snapshot while a
    page or a message batch is in flight (a backfill runs for minutes, and an
    idle-in-transaction session that long blocks vacuum and pins a pooled
    connection). What it commits is only finished work — rows the next
    checkpoint would have kept anyway, deduped on resume by gmail_id and
    rfc_message_id."""
    if db.in_transaction():
        await db.commit()


async def _post_form(url: str, data: dict) -> dict:
    # This is the OAuth token endpoint — it runs at the START of every sync to
    # refresh the access token, so a transient blip here aborts the whole pass
    # before anything else. Retry with backoff; a real rejection (4xx) below is
    # not retried.
    resp = None
    for attempt in range(3):
        try:
            resp = await _get_client().post(url, data=data, timeout=30.0)
            break
        except httpx.HTTPError as exc:
            if attempt < 2:
                await asyncio.sleep(1.5 * (attempt + 1))
                continue
            raise GoogleError(
                f"Cannot reach Google: {type(exc).__name__}: {exc}".rstrip(": ")
            ) from exc
    if resp.status_code >= 400:
        try:
            detail = resp.json().get("error_description") or resp.json().get("error")
        except Exception:
            detail = resp.text[:200]
        raise GoogleError(
            f"Google rejected the request ({resp.status_code}): {detail}",
            status_code=resp.status_code,
        )
    return resp.json()


async def _get_json(
    url: str, access_token: str, params: dict | None = None, timeout: float = 20.0
) -> dict:
    # Transport errors (timeouts, dropped connections) are transient — retry a
    # couple times with backoff before giving up. Gmail 4xx/5xx are handled
    # below and NOT retried here, so rate limits still surface as GoogleError.
    resp = None
    for attempt in range(3):
        try:
            resp = await _get_client().get(
                url,
                params=params,
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=timeout,
            )
            break
        except httpx.HTTPError as exc:
            if attempt < 2:
                await asyncio.sleep(1.5 * (attempt + 1))
                continue
            # str(timeout errors) is often empty — name the class so the
            # sync_error is legible.
            raise GoogleError(
                f"Cannot reach Google: {type(exc).__name__}: {exc}".rstrip(": ")
            ) from exc
    if resp.status_code >= 400:
        detail = ""
        try:
            detail = (resp.json().get("error") or {}).get("message", "")[:200]
        except Exception:
            pass
        raise GoogleError(
            f"Google GET {url.split('?')[0]} failed ({resp.status_code})"
            + (f": {detail}" if detail else ""),
            status_code=resp.status_code,
        )
    return resp.json()


async def get_userinfo(access_token: str) -> dict:
    return await _get_json(settings.google_userinfo_url, access_token)


async def ensure_access_token(db, cfg: GoogleIntegration, account: GoogleAccount) -> str:
    """Returns a valid access token, refreshing (and persisting) if expired."""
    if account.access_token and account.access_expires_at:
        expires = account.access_expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if expires > utcnow() + timedelta(minutes=2):
            return account.access_token
    await release_db(db)  # the refresh round-trip must not sit inside a transaction
    tokens = await _post_form(
        settings.google_token_url,
        {
            "client_id": cfg.client_id,
            "client_secret": cfg.client_secret,
            "refresh_token": account.refresh_token,
            "grant_type": "refresh_token",
        },
    )
    account.access_token = tokens["access_token"]
    account.access_expires_at = utcnow() + timedelta(seconds=int(tokens.get("expires_in", 3600)))
    await db.flush()
    return account.access_token


async def revoke(account: GoogleAccount) -> None:
    try:
        await _get_client().post(
            settings.google_revoke_url, params={"token": account.refresh_token}, timeout=10.0
        )
    except httpx.HTTPError:
        pass  # best effort — we delete our copy regardless
