from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "Colloqui CRM"
    environment: str = "development"
    database_url: str = "postgresql+asyncpg://app:app@localhost:5432/app"
    secret_key: str = "dev-secret-change-me"
    allowed_origins: str = "http://localhost:5173"
    session_ttl_days: int = 30
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

    @property
    def cors_origins(self) -> list[str]:
        return [o.strip() for o in self.allowed_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
