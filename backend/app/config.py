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
    # 0 = as far back as RingCentral retains call/message history.
    ringcentral_backfill_days: int = 0
    # 0 = no window: sync all history with known contacts. Set a day count to
    # bound the initial backfill on very large installs.
    gmail_backfill_days: int = 0

    @property
    def cors_origins(self) -> list[str]:
        return [o.strip() for o in self.allowed_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
