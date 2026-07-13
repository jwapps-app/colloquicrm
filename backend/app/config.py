from functools import lru_cache
from ipaddress import ip_network

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "Colloqui CRM"
    environment: str = "development"
    database_url: str = "postgresql+asyncpg://app:app@localhost:5432/app"
    secret_key: str = "dev-secret-change-me"
    # Fernet key (urlsafe base64 of 32 bytes) for encrypting sensitive secret
    # columns at rest — Google/RingCentral tokens, the Colloqui api_key, TOTP
    # seeds. Empty = derive deterministically from secret_key (works with no
    # extra config). Setting this explicitly is recommended in production so
    # encryption is decoupled from SECRET_KEY rotation. Generate with:
    #   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    secret_encryption_key: str = ""
    allowed_origins: str = "http://localhost:5173"
    session_ttl_days: int = 30
    # Comma-separated IPs/CIDRs of reverse proxies we trust to set
    # cf-connecting-ip. Only when the immediate peer (request.client.host)
    # falls inside this set do we believe that header for throttle keying;
    # otherwise the peer address itself is used, so a public client can't spoof
    # its way past the login/form rate limits. Defaults to loopback + the
    # private ranges a docker/cloudflared front-end sits in. Tighten to your
    # proxy's exact address if the app is otherwise reachable.
    trusted_proxy_ips: str = "127.0.0.1,::1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16"
    # Public lead form abuse ceilings. A single form only accepts this many
    # submissions per UTC day (an absolute DoS cap on top of the per-IP
    # limiter), and a submission whose Content-Length exceeds this byte bound
    # is rejected before the body is parsed.
    form_daily_submission_cap: int = 500
    form_max_body_bytes: int = 65536
    # Public URL of this CRM's web app, used in links posted to chat and as
    # the base of the Google OAuth redirect URI.
    app_url: str = "http://localhost:5173"

    # Directory holding the built frontend; empty = auto-detect ../frontend/dist.
    static_dir: str = ""

    # Google endpoint bases — overridable for tests; defaults are production.
    google_auth_url: str = "https://accounts.google.com/o/oauth2/v2/auth"
    google_token_url: str = "https://oauth2.googleapis.com/token"
    google_revoke_url: str = "https://oauth2.googleapis.com/revoke"
    google_userinfo_url: str = "https://openidconnect.googleapis.com/v1/userinfo"
    google_people_base: str = "https://people.googleapis.com"
    google_calendar_base: str = "https://www.googleapis.com/calendar/v3"
    google_gmail_base: str = "https://gmail.googleapis.com/gmail/v1"
    ringcentral_base: str = "https://platform.ringcentral.com"
    gravatar_base: str = "https://en.gravatar.com"
    # RingCentral defaults to ~1 week when no dateFrom is sent, so always send
    # an explicit window. A year is their typical retention.
    ringcentral_backfill_days: int = 365
    # 0 = no window: sync all history with known contacts. Set a day count to
    # bound the initial backfill on very large installs.
    gmail_backfill_days: int = 0
    # Backfill searches People's addresses by default. Leads too can mean tens
    # of thousands of extra Gmail queries — opt in deliberately.
    gmail_backfill_leads: bool = False
    # How long deleted records stay recoverable in Trash before the daily
    # purge removes them permanently. There is no manual empty by design.
    trash_retention_days: int = 60
    # Store each synced email's full body at sync time, so the CRM is a true
    # archive that survives deletion in Gmail. Off = bodies fetched lazily on
    # first view only (lighter storage, not a permanent record).
    gmail_archive_bodies: bool = True
    # Push for the iOS companion app, via the shared push relay — the same
    # pattern as every other app in the fleet: the relay holds the Apple .p8;
    # this server holds only a relay key scoped to its bundle id. All three
    # must be set for push to be active; unset = feature dark, chat DMs keep
    # working.
    push_relay_url: str = ""
    push_relay_api_key: str = ""
    apns_topic: str = "com.jworthington.colloquicrm"
    # When a task has a due time but no explicit reminder, notify this many
    # minutes BEFORE it's due (a reminder at the due moment is too late, and
    # nobody wants to enter two datetimes). An explicit reminder_at overrides.
    task_reminder_lead_minutes: int = 15

    @property
    def cors_origins(self) -> list[str]:
        return [o.strip() for o in self.allowed_origins.split(",") if o.strip()]

    @property
    def trusted_proxy_networks(self) -> list:
        nets = []
        for raw in self.trusted_proxy_ips.split(","):
            raw = raw.strip()
            if not raw:
                continue
            try:
                nets.append(ip_network(raw, strict=False))
            except ValueError:
                continue
        return nets


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
