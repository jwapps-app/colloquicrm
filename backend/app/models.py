import uuid
from datetime import date, datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Org(Base):
    """Tenant. Single row per install today; the column exists everywhere so a
    multi-tenant SaaS deployment is a config change, not a migration."""

    __tablename__ = "orgs"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(120), default="Default")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class User(Base):
    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("org_id", "email"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orgs.id", ondelete="CASCADE"), index=True)
    email: Mapped[str] = mapped_column(String(255), index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    display_name: Mapped[str] = mapped_column(String(120))
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    totp_secret: Mapped[str | None] = mapped_column(String(64))
    totp_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    colloqui_user_id: Mapped[uuid.UUID | None] = mapped_column()
    colloqui_username: Mapped[str | None] = mapped_column(String(80))
    # Where this user's personal task notifications go: a Colloqui chat DM or
    # an APNs push to the CRM companion app — one channel, never both.
    notify_channel: Mapped[str] = mapped_column(String(20), default="colloqui_chat")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Session(Base):
    __tablename__ = "sessions"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    pending_totp: Mapped[bool] = mapped_column(Boolean, default=False)
    totp_attempts: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class DeviceToken(Base):
    """An APNs device registration from the iOS companion app. One row per
    device; re-registering an existing token re-points it at the current
    user (the device changed accounts)."""

    __tablename__ = "device_tokens"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orgs.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    platform: Mapped[str] = mapped_column(String(20), default="ios")
    # "sandbox" for Xcode dev builds, "production" for TestFlight/App Store —
    # routes the send to the matching APNs host.
    environment: Mapped[str] = mapped_column(String(20), default="production")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Pipeline(Base):
    __tablename__ = "pipelines"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orgs.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    position: Mapped[int] = mapped_column(Integer, default=0)


class Stage(Base):
    __tablename__ = "stages"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    pipeline_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("pipelines.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(120))
    position: Mapped[int] = mapped_column(Integer, default=0)
    win_probability: Mapped[int] = mapped_column(Integer, default=0)


class Company(Base):
    __tablename__ = "companies"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orgs.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(255), index=True)
    details: Mapped[str | None] = mapped_column(Text)
    email_domain: Mapped[str | None] = mapped_column(String(255), index=True)
    street: Mapped[str | None] = mapped_column(String(255))
    city: Mapped[str | None] = mapped_column(String(120))
    state: Mapped[str | None] = mapped_column(String(120))
    postal_code: Mapped[str | None] = mapped_column(String(30))
    country: Mapped[str | None] = mapped_column(String(120))
    contact_type: Mapped[str | None] = mapped_column(String(60))
    owner_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    work_phone: Mapped[str | None] = mapped_column(String(60))
    work_website: Mapped[str | None] = mapped_column(String(500))
    linkedin: Mapped[str | None] = mapped_column(String(500))
    facebook: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)


class Person(Base):
    __tablename__ = "people"
    __table_args__ = (Index("ix_people_org_last_name", "org_id", "last_name"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orgs.id", ondelete="CASCADE"), index=True)
    first_name: Mapped[str | None] = mapped_column(String(120))
    middle_name: Mapped[str | None] = mapped_column(String(120))
    last_name: Mapped[str | None] = mapped_column(String(120))
    prefix: Mapped[str | None] = mapped_column(String(30))
    suffix: Mapped[str | None] = mapped_column(String(30))
    title: Mapped[str | None] = mapped_column(String(255))
    details: Mapped[str | None] = mapped_column(Text)
    company_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("companies.id", ondelete="SET NULL"), index=True
    )
    contact_type: Mapped[str | None] = mapped_column(String(60))
    work_email: Mapped[str | None] = mapped_column(String(255), index=True)
    personal_email: Mapped[str | None] = mapped_column(String(255), index=True)
    work_phone: Mapped[str | None] = mapped_column(String(60))
    mobile_phone: Mapped[str | None] = mapped_column(String(60))
    street: Mapped[str | None] = mapped_column(String(255))
    city: Mapped[str | None] = mapped_column(String(120))
    state: Mapped[str | None] = mapped_column(String(120))
    postal_code: Mapped[str | None] = mapped_column(String(30))
    country: Mapped[str | None] = mapped_column(String(120))
    work_website: Mapped[str | None] = mapped_column(String(500))
    personal_website: Mapped[str | None] = mapped_column(String(500))
    linkedin: Mapped[str | None] = mapped_column(String(500))
    facebook: Mapped[str | None] = mapped_column(String(500))
    owner_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    last_contacted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    interaction_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)


class Lead(Base):
    """A prospect: contact info known, relationship not yet established.
    Converting creates a Person (+ optional Company and Opportunity)."""

    __tablename__ = "leads"
    __table_args__ = (Index("ix_leads_org_last_name", "org_id", "last_name"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orgs.id", ondelete="CASCADE"), index=True)
    first_name: Mapped[str | None] = mapped_column(String(120))
    middle_name: Mapped[str | None] = mapped_column(String(120))
    last_name: Mapped[str | None] = mapped_column(String(120))
    prefix: Mapped[str | None] = mapped_column(String(30))
    suffix: Mapped[str | None] = mapped_column(String(30))
    title: Mapped[str | None] = mapped_column(String(255))
    details: Mapped[str | None] = mapped_column(Text)
    value: Mapped[float | None] = mapped_column(Numeric(14, 2))
    currency: Mapped[str | None] = mapped_column(String(3), default="USD")
    company_name: Mapped[str | None] = mapped_column(String(255))
    source: Mapped[str | None] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(60), default="New", index=True)
    street: Mapped[str | None] = mapped_column(String(255))
    city: Mapped[str | None] = mapped_column(String(120))
    state: Mapped[str | None] = mapped_column(String(120))
    postal_code: Mapped[str | None] = mapped_column(String(30))
    country: Mapped[str | None] = mapped_column(String(120))
    work_phone: Mapped[str | None] = mapped_column(String(60))
    mobile_phone: Mapped[str | None] = mapped_column(String(60))
    email: Mapped[str | None] = mapped_column(String(255), index=True)
    work_website: Mapped[str | None] = mapped_column(String(500))
    personal_website: Mapped[str | None] = mapped_column(String(500))
    linkedin: Mapped[str | None] = mapped_column(String(500))
    facebook: Mapped[str | None] = mapped_column(String(500))
    owner_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    converted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    converted_person_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("people.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)


class Opportunity(Base):
    __tablename__ = "opportunities"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orgs.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(255), index=True)
    details: Mapped[str | None] = mapped_column(Text)
    company_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("companies.id", ondelete="SET NULL"), index=True
    )
    primary_person_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("people.id", ondelete="SET NULL"), index=True
    )
    status: Mapped[str] = mapped_column(String(20), default="open", index=True)
    priority: Mapped[str | None] = mapped_column(String(20))
    owner_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    close_date: Mapped[date | None] = mapped_column(Date)
    value: Mapped[float | None] = mapped_column(Numeric(14, 2))
    currency: Mapped[str | None] = mapped_column(String(3), default="USD")
    win_probability: Mapped[int | None] = mapped_column(Integer)
    pipeline_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("pipelines.id", ondelete="SET NULL"), index=True
    )
    stage_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("stages.id", ondelete="SET NULL"), index=True
    )
    source: Mapped[str | None] = mapped_column(String(120))
    loss_reason: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)


class Task(Base):
    __tablename__ = "tasks"
    __table_args__ = (Index("ix_tasks_entity", "entity_type", "entity_id"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orgs.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(255))
    details: Mapped[str | None] = mapped_column(Text)
    entity_type: Mapped[str | None] = mapped_column(String(30))
    entity_id: Mapped[uuid.UUID | None] = mapped_column()
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    reminder_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    priority: Mapped[str | None] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(20), default="open", index=True)
    assignee_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    due_notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class ColloquiIntegration(Base):
    """Org-level connection to a self-hosted Colloqui server. The api_key is a
    colq_ key bound to a Colloqui service user (see Colloqui's
    INTEGRATION.md); the CRM provisions and reuses a dedicated space with a
    #tasks channel."""

    __tablename__ = "colloqui_integration"

    org_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("orgs.id", ondelete="CASCADE"), primary_key=True
    )
    base_url: Mapped[str] = mapped_column(String(500))
    api_key: Mapped[str] = mapped_column(String(255))
    space_id: Mapped[uuid.UUID | None] = mapped_column()
    tasks_channel_id: Mapped[uuid.UUID | None] = mapped_column()
    connected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(String(500))


class GoogleIntegration(Base):
    """Org-level Google OAuth client. Each self-hosted install registers its
    own OAuth client in Google Cloud console; users then connect their own
    accounts against it."""

    __tablename__ = "google_integration"

    org_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("orgs.id", ondelete="CASCADE"), primary_key=True
    )
    client_id: Mapped[str] = mapped_column(String(255))
    client_secret: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class GoogleAccount(Base):
    """One user's connected Google account (offline refresh token)."""

    __tablename__ = "google_accounts"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orgs.id", ondelete="CASCADE"), index=True)
    email: Mapped[str] = mapped_column(String(255))
    refresh_token: Mapped[str] = mapped_column(String(512))
    access_token: Mapped[str | None] = mapped_column(String(2048))
    access_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    scopes: Mapped[str | None] = mapped_column(String(500))
    connected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sync_error: Mapped[str | None] = mapped_column(String(500))
    gmail_history_id: Mapped[str | None] = mapped_column(String(40))
    gmail_backfill_done: Mapped[bool] = mapped_column(Boolean, default=False)
    # index into the sorted backfill address list — survives restarts/429s
    gmail_backfill_cursor: Mapped[int] = mapped_column(Integer, default=0)


class CalendarEvent(Base):
    __tablename__ = "calendar_events"
    __table_args__ = (UniqueConstraint("org_id", "google_event_id"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orgs.id", ondelete="CASCADE"), index=True)
    google_event_id: Mapped[str] = mapped_column(String(255))
    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    summary: Mapped[str | None] = mapped_column(String(500))
    location: Mapped[str | None] = mapped_column(String(500))
    starts_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    all_day: Mapped[bool] = mapped_column(Boolean, default=False)
    html_link: Mapped[str | None] = mapped_column(String(1000))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class EmailMessage(Base):
    """A synced email that involves at least one known Person or Lead.
    Non-matching mail is never stored. Deduped org-wide on the RFC Message-ID
    header so a thread synced from two mailboxes lands once."""

    __tablename__ = "email_messages"
    __table_args__ = (
        UniqueConstraint("org_id", "rfc_message_id"),
        Index("ix_email_messages_org_sent", "org_id", "sent_at"),
        Index("ix_email_messages_owner_gmail", "owner_user_id", "gmail_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orgs.id", ondelete="CASCADE"), index=True)
    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    gmail_id: Mapped[str] = mapped_column(String(32))
    gmail_thread_id: Mapped[str | None] = mapped_column(String(32))
    rfc_message_id: Mapped[str] = mapped_column(String(255))
    subject: Mapped[str | None] = mapped_column(String(500))
    snippet: Mapped[str | None] = mapped_column(String(500))
    from_email: Mapped[str | None] = mapped_column(String(255))
    from_name: Mapped[str | None] = mapped_column(String(255))
    is_outgoing: Mapped[bool] = mapped_column(Boolean, default=False)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Full content is fetched lazily on first view and cached here.
    body_text: Mapped[str | None] = mapped_column(Text)
    body_html: Mapped[str | None] = mapped_column(Text)
    body_fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class EmailParticipant(Base):
    __tablename__ = "email_participants"

    email_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("email_messages.id", ondelete="CASCADE"), primary_key=True
    )
    email: Mapped[str] = mapped_column(String(255), primary_key=True, index=True)
    kind: Mapped[str] = mapped_column(String(8), default="to")
    # True when this participant actually engaged: they authored the message,
    # or the mailbox owner sent it to them. Passive co-recipients of a third
    # party's mail stay False and never surface as interactions.
    direct: Mapped[bool] = mapped_column(Boolean, default=False)
    display_name: Mapped[str | None] = mapped_column(String(255))


class CalendarEventAttendee(Base):
    __tablename__ = "calendar_event_attendees"

    event_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("calendar_events.id", ondelete="CASCADE"), primary_key=True
    )
    email: Mapped[str] = mapped_column(String(255), primary_key=True, index=True)
    display_name: Mapped[str | None] = mapped_column(String(255))


class ContactSuggestion(Base):
    """A frequent email correspondent who isn't a CRM contact yet — surfaced
    for one-click Add or a persistent Ignore."""

    __tablename__ = "contact_suggestions"
    __table_args__ = (UniqueConstraint("org_id", "email"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orgs.id", ondelete="CASCADE"), index=True)
    email: Mapped[str] = mapped_column(String(255))
    display_name: Mapped[str | None] = mapped_column(String(255))
    message_count: Mapped[int] = mapped_column(Integer, default=0)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)  # pending|ignored|added
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class Note(Base):
    __tablename__ = "notes"
    __table_args__ = (Index("ix_notes_entity", "entity_type", "entity_id"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orgs.id", ondelete="CASCADE"), index=True)
    entity_type: Mapped[str] = mapped_column(String(30))
    entity_id: Mapped[uuid.UUID] = mapped_column()
    body: Mapped[str] = mapped_column(Text)
    # Optional link to a synced call/text this note documents.
    phone_event_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("phone_events.id", ondelete="SET NULL"), index=True
    )
    author_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Activity(Base):
    """Append-only audit/feed log. entity_type/entity_id are null for
    org-level events."""

    __tablename__ = "activities"
    __table_args__ = (Index("ix_activities_entity", "entity_type", "entity_id"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orgs.id", ondelete="CASCADE"), index=True)
    entity_type: Mapped[str | None] = mapped_column(String(30))
    entity_id: Mapped[uuid.UUID | None] = mapped_column()
    kind: Mapped[str] = mapped_column(String(40))
    payload: Mapped[dict | None] = mapped_column(JSON)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class Tag(Base):
    __tablename__ = "tags"
    __table_args__ = (UniqueConstraint("org_id", "name"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orgs.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(120))


class EntityTag(Base):
    __tablename__ = "entity_tags"
    __table_args__ = (Index("ix_entity_tags_entity", "entity_type", "entity_id"),)

    tag_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tags.id", ondelete="CASCADE"), primary_key=True
    )
    entity_type: Mapped[str] = mapped_column(String(30), primary_key=True)
    entity_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)


class CustomField(Base):
    """User-defined field definition, scoped to one entity type.
    external_key preserves the source system's id (Copper cf_NNNNNN) so
    re-imports map to the same field."""

    __tablename__ = "custom_fields"
    __table_args__ = (UniqueConstraint("org_id", "entity_type", "name"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orgs.id", ondelete="CASCADE"), index=True)
    entity_type: Mapped[str] = mapped_column(String(30))
    name: Mapped[str] = mapped_column(String(120))
    field_type: Mapped[str] = mapped_column(String(20), default="text")
    options: Mapped[list | None] = mapped_column(JSON)
    position: Mapped[int] = mapped_column(Integer, default=0)
    external_key: Mapped[str | None] = mapped_column(String(60))


class CustomFieldValue(Base):
    __tablename__ = "custom_field_values"
    __table_args__ = (Index("ix_cfv_entity", "entity_type", "entity_id"),)

    field_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("custom_fields.id", ondelete="CASCADE"), primary_key=True
    )
    entity_type: Mapped[str] = mapped_column(String(30))
    entity_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    value: Mapped[str | None] = mapped_column(Text)


class RingCentralIntegration(Base):
    """Org-level RingCentral app credentials (JWT auth flow)."""

    __tablename__ = "ringcentral_integration"

    org_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("orgs.id", ondelete="CASCADE"), primary_key=True
    )
    client_id: Mapped[str] = mapped_column(String(255))
    client_secret: Mapped[str] = mapped_column(String(255))
    jwt: Mapped[str] = mapped_column(Text)
    access_token: Mapped[str | None] = mapped_column(Text)
    access_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    own_numbers: Mapped[list | None] = mapped_column(JSON)  # E.164 numbers of the account
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sync_error: Mapped[str | None] = mapped_column(String(500))
    connected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PhoneEvent(Base):
    """A call or text that involves a known Person or Lead, matched by
    normalized phone number. kind: call | sms."""

    __tablename__ = "phone_events"
    __table_args__ = (
        UniqueConstraint("org_id", "rc_id"),
        Index("ix_phone_events_org_at", "org_id", "happened_at"),
        Index("ix_phone_events_number", "other_number"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orgs.id", ondelete="CASCADE"), index=True)
    rc_id: Mapped[str] = mapped_column(String(64))  # RingCentral id, or manual:<uuid>
    kind: Mapped[str] = mapped_column(String(8))  # call | sms
    # Manually logged events carry a direct record link (no phone matching).
    entity_type: Mapped[str | None] = mapped_column(String(30))
    entity_id: Mapped[uuid.UUID | None] = mapped_column()
    direction: Mapped[str] = mapped_column(String(10))  # inbound | outbound
    other_number: Mapped[str] = mapped_column(String(24))  # E.164 of the contact side
    other_name: Mapped[str | None] = mapped_column(String(255))
    happened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_seconds: Mapped[int | None] = mapped_column(Integer)
    result: Mapped[str | None] = mapped_column(String(60))  # Answered, Missed, Voicemail…
    text: Mapped[str | None] = mapped_column(Text)  # SMS body
    recording_id: Mapped[str | None] = mapped_column(String(64))  # future playback
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ImportJob(Base):
    """A committed import, processed in the background in chunks. The parsed
    rows live in payload so a restart can resume from the processed offset
    instead of losing minutes of work (or duplicating it)."""

    __tablename__ = "import_jobs"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orgs.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    import_type: Mapped[str] = mapped_column(String(30))
    status: Mapped[str] = mapped_column(String(20), default="running", index=True)
    payload: Mapped[list] = mapped_column(JSON, default=list)
    total: Mapped[int] = mapped_column(Integer, default=0)
    processed: Mapped[int] = mapped_column(Integer, default=0)
    created_count: Mapped[int] = mapped_column(Integer, default=0)
    merged_count: Mapped[int] = mapped_column(Integer, default=0)
    skipped_count: Mapped[int] = mapped_column(Integer, default=0)
    fields_created: Mapped[list] = mapped_column(JSON, default=list)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class AutomationRule(Base):
    """A proactive rule: when <trigger_type> matches a record of entity_type,
    run <action_type>. The engine sweeps periodically; see
    services/automations.py for the trigger/action catalog."""

    __tablename__ = "automation_rules"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orgs.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    entity_type: Mapped[str] = mapped_column(String(30))  # lead|opportunity|person|task
    trigger_type: Mapped[str] = mapped_column(String(30))
    trigger_config: Mapped[dict] = mapped_column(JSON, default=dict)
    action_type: Mapped[str] = mapped_column(String(30))
    action_config: Mapped[dict] = mapped_column(JSON, default=dict)
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class AutomationFire(Base):
    """One rule execution against one record. The unique constraint is the
    idempotency guard (a rule fires once per record until re-armed); the rows
    double as the visible audit log."""

    __tablename__ = "automation_fires"
    __table_args__ = (UniqueConstraint("rule_id", "entity_type", "entity_id"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orgs.id", ondelete="CASCADE"), index=True)
    rule_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("automation_rules.id", ondelete="CASCADE"), index=True
    )
    entity_type: Mapped[str] = mapped_column(String(30))
    entity_id: Mapped[uuid.UUID] = mapped_column()
    fired_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    detail: Mapped[dict | None] = mapped_column(JSON)


class LeadForm(Base):
    """A public lead-capture form: an admin picks the fields, the app serves
    the form at /f/{slug} with no auth, and submissions create Leads."""

    __tablename__ = "lead_forms"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orgs.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    # URL-safe handle with a random suffix so slugs can't be guessed from the
    # form name alone. Immutable after creation — it's a published URL.
    slug: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # Ordered list of field keys to render; always includes first/last name.
    fields: Mapped[list] = mapped_column(JSON, default=list)
    require_email: Mapped[bool] = mapped_column(Boolean, default=True)
    # Stamped onto created leads' source column; defaults to the form name.
    source: Mapped[str | None] = mapped_column(String(120))
    success_message: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    submission_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class SavedFilter(Base):
    __tablename__ = "saved_filters"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orgs.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    entity_type: Mapped[str] = mapped_column(String(30))
    name: Mapped[str] = mapped_column(String(120))
    filters: Mapped[dict] = mapped_column(JSON, default=dict)
    is_public: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
