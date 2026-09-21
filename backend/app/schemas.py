import math
import uuid
from datetime import date, datetime, timezone
from typing import Annotated, Any

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    EmailStr,
    Field,
    StringConstraints,
    model_validator,
)


# ---- auth ----

class SetupIn(BaseModel):
    email: EmailStr
    password: str
    display_name: str


class LoginIn(BaseModel):
    email: EmailStr
    password: str


class TotpVerifyIn(BaseModel):
    pending_token: str
    code: str


class TotpCodeIn(BaseModel):
    code: str


class UserCreateIn(BaseModel):
    email: EmailStr
    password: str
    display_name: str
    is_admin: bool = False


class MeUpdateIn(BaseModel):
    display_name: str | None = None
    current_password: str | None = None
    new_password: str | None = None
    notify_channel: str | None = None


class DeviceIn(BaseModel):
    token: str
    platform: str = "ios"
    environment: str = "production"


class UserAdminUpdateIn(BaseModel):
    is_admin: bool | None = None
    is_active: bool | None = None


class ResetPasswordIn(BaseModel):
    new_password: str


# ---- CRM entities ----
# One schema per entity serves both create (POST) and partial update (PATCH,
# via exclude_unset). Required-field rules are enforced in the routes.

# Canonical lead statuses, Title-Case — what the model default, the public
# forms and the iOS app all use. Input is matched case-insensitively and
# stored in this casing; a status outside the list (imports bring their own,
# e.g. Copper's "Junk") is kept as typed rather than rejected.
LEAD_STATUSES = ["New", "Open", "Contacted", "Qualified", "Unqualified", "Converted"]
_LEAD_STATUS_BY_LOWER = {s.lower(): s for s in LEAD_STATUSES}

OPPORTUNITY_STATUSES = ("open", "won", "lost", "abandoned")
CLOSED_OPPORTUNITY_STATUSES = ("won", "lost", "abandoned")
PRIORITIES = ("none", "low", "medium", "high")

# Numeric(14, 2) tops out just under a trillion.
MAX_MONEY = 999_999_999_999.99
# Successor generation adds intervals to a due date; keep both far enough from
# datetime.max that the arithmetic can never overflow.
MIN_TASK_DATE = datetime(1970, 1, 1, tzinfo=timezone.utc)
MAX_TASK_DATE = datetime(2200, 1, 1, tzinfo=timezone.utc)


def canonical_lead_status(value) -> str | None:
    """'new' / ' NEW ' -> 'New'. Unknown statuses pass through trimmed; blank
    -> None (callers decide what an absent status means)."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return _LEAD_STATUS_BY_LOWER.get(text.lower(), text)


def _lead_status(value):
    # The column is NOT NULL: an explicit null/blank (the web's inline editor
    # sends null when a select is cleared) falls back to the default.
    status = canonical_lead_status(value) or "New"
    if len(status) > 60:
        raise ValueError("status must be at most 60 characters")
    return status


def _lower_choice(name: str, allowed: tuple[str, ...], blank):
    def check(value):
        if value is None:
            return blank
        text = str(value).strip().lower()
        if not text:
            return blank
        if text not in allowed:
            raise ValueError(f"{name} must be one of: {', '.join(allowed)}")
        return text

    return check


def _currency(value):
    if value is None:
        return None
    text = str(value).strip().upper()
    if not text:
        return None
    if len(text) != 3 or not text.isascii() or not text.isalpha():
        raise ValueError("currency must be a 3-letter code, like USD")
    return text


def _money(value: float | None) -> float | None:
    if value is None:
        return None
    if not math.isfinite(value):
        raise ValueError("must be a finite number")
    if value < 0:
        raise ValueError("must not be negative")
    if value > MAX_MONEY:
        raise ValueError("is too large")
    return value


def _task_date(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    aware = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    if not MIN_TASK_DATE <= aware <= MAX_TASK_DATE:
        raise ValueError("date is out of range")
    return value


def _str(max_length: int):
    return Annotated[str, StringConstraints(max_length=max_length)]


# Widths mirror the column sizes in models.py, so an oversized value is a 422
# here rather than a DataError from Postgres.
Str30 = _str(30)
Str60 = _str(60)
Str120 = _str(120)
Str255 = _str(255)
Str500 = _str(500)
LongText = _str(100_000)

Money = Annotated[float, AfterValidator(_money)]
Currency = Annotated[str | None, BeforeValidator(_currency)]
# `str | None` so the field can default to None (= not sent); the validators
# never actually return None for the two NOT NULL status columns.
LeadStatus = Annotated[str | None, BeforeValidator(_lead_status)]
OpportunityStatus = Annotated[
    str | None, BeforeValidator(_lower_choice("status", OPPORTUNITY_STATUSES, "open"))
]
Priority = Annotated[str | None, BeforeValidator(_lower_choice("priority", PRIORITIES, None))]
Percent = Annotated[int, Field(ge=0, le=100)]
TaskDate = Annotated[datetime, AfterValidator(_task_date)]


class ExtrasIn(BaseModel):
    tags: list[Str120] | None = None
    custom_fields: dict[str, Any] | None = None


class CompanyIn(ExtrasIn):
    name: Str255 | None = None
    details: LongText | None = None
    email_domain: Str255 | None = None
    street: Str255 | None = None
    city: Str120 | None = None
    state: Str120 | None = None
    postal_code: Str30 | None = None
    country: Str120 | None = None
    contact_type: Str60 | None = None
    owner_id: uuid.UUID | None = None
    work_phone: Str60 | None = None
    work_website: Str500 | None = None
    linkedin: Str500 | None = None
    facebook: Str500 | None = None


class PersonIn(ExtrasIn):
    first_name: Str120 | None = None
    middle_name: Str120 | None = None
    last_name: Str120 | None = None
    prefix: Str30 | None = None
    suffix: Str30 | None = None
    title: Str255 | None = None
    details: LongText | None = None
    company_id: uuid.UUID | None = None
    contact_type: Str60 | None = None
    work_email: Str255 | None = None
    personal_email: Str255 | None = None
    work_phone: Str60 | None = None
    mobile_phone: Str60 | None = None
    street: Str255 | None = None
    city: Str120 | None = None
    state: Str120 | None = None
    postal_code: Str30 | None = None
    country: Str120 | None = None
    work_website: Str500 | None = None
    personal_website: Str500 | None = None
    linkedin: Str500 | None = None
    facebook: Str500 | None = None
    owner_id: uuid.UUID | None = None
    last_contacted_at: datetime | None = None


class LeadIn(ExtrasIn):
    first_name: Str120 | None = None
    middle_name: Str120 | None = None
    last_name: Str120 | None = None
    prefix: Str30 | None = None
    suffix: Str30 | None = None
    title: Str255 | None = None
    details: LongText | None = None
    value: Money | None = None
    currency: Currency = None
    company_name: Str255 | None = None
    source: Str120 | None = None
    status: LeadStatus = None
    street: Str255 | None = None
    city: Str120 | None = None
    state: Str120 | None = None
    postal_code: Str30 | None = None
    country: Str120 | None = None
    work_phone: Str60 | None = None
    mobile_phone: Str60 | None = None
    email: Str255 | None = None
    work_website: Str500 | None = None
    personal_website: Str500 | None = None
    linkedin: Str500 | None = None
    facebook: Str500 | None = None
    owner_id: uuid.UUID | None = None


class OpportunityIn(ExtrasIn):
    name: Str255 | None = None
    details: LongText | None = None
    company_id: uuid.UUID | None = None
    primary_person_id: uuid.UUID | None = None
    status: OpportunityStatus = None
    priority: Priority = None
    owner_id: uuid.UUID | None = None
    close_date: date | None = None
    value: Money | None = None
    currency: Currency = None
    win_probability: Percent | None = None
    pipeline_id: uuid.UUID | None = None
    stage_id: uuid.UUID | None = None
    source: Str120 | None = None
    loss_reason: Str255 | None = None


class TaskIn(BaseModel):
    name: Str255 | None = None
    details: LongText | None = None
    entity_type: Str30 | None = None
    entity_id: uuid.UUID | None = None
    due_at: TaskDate | None = None
    reminder_at: TaskDate | None = None
    priority: Priority = None
    assignee_id: uuid.UUID | None = None
    # Recurrence — every N day/week/month/year(s). Always sent as a pair
    # (both null clears it), so a PATCH can't leave half a schedule behind.
    repeat_every: int | None = None
    repeat_unit: str | None = None

    @model_validator(mode="after")
    def _check_recurrence(self):
        # Judge the pair by what was actually SENT, not by the default Nones:
        # {"repeat_every": null} alone would otherwise pass (None == None)
        # and exclude_unset would clear just that half of the schedule.
        sent = self.model_fields_set & {"repeat_every", "repeat_unit"}
        if len(sent) == 1:
            raise ValueError("repeat_every and repeat_unit must be sent together")
        if (self.repeat_every is None) != (self.repeat_unit is None):
            raise ValueError("repeat_every and repeat_unit go together")
        if self.repeat_every is not None:
            if not 1 <= self.repeat_every <= 365:
                raise ValueError("repeat_every must be between 1 and 365")
            if self.repeat_unit not in ("day", "week", "month", "year"):
                raise ValueError("repeat_unit must be day, week, month, or year")
        return self


class NoteAttachIn(BaseModel):
    phone_event_id: uuid.UUID | None = None


class NoteIn(BaseModel):
    entity_type: str
    entity_id: uuid.UUID
    body: str
    phone_event_id: uuid.UUID | None = None
    # Log this note as a manual call (calls that didn't go through RingCentral).
    log_call: bool = False
    call_direction: str | None = None  # inbound | outbound


class ConvertIn(BaseModel):
    create_company: bool = False
    # Link the new person to this existing company. Wins over create_company;
    # the way out when several companies share the lead's company name.
    company_id: uuid.UUID | None = None
    pipeline_id: uuid.UUID | None = None
    opportunity_name: Str255 | None = None
    opportunity_value: Money | None = None


class StageIn(BaseModel):
    name: Str120
    win_probability: Percent = 0
    position: int | None = None


class StageUpdateIn(BaseModel):
    name: Str120 | None = None
    win_probability: Percent | None = None
    position: int | None = None


class PipelineIn(BaseModel):
    name: Str120
    stages: list[StageIn] | None = None


class PipelineUpdateIn(BaseModel):
    name: Str120 | None = None
    position: int | None = None


class CustomFieldIn(BaseModel):
    entity_type: str
    name: Str120
    field_type: str = "text"
    options: list[str] | None = None
    position: int | None = None


class CustomFieldUpdateIn(BaseModel):
    name: Str120 | None = None
    field_type: str | None = None
    options: list[str] | None = None
    position: int | None = None


class SavedFilterIn(BaseModel):
    entity_type: str
    name: str
    filters: dict = {}
    is_public: bool = False


class SavedFilterUpdateIn(BaseModel):
    name: str | None = None
    filters: dict | None = None
    is_public: bool | None = None


class AutomationRuleIn(BaseModel):
    name: str
    entity_type: str
    trigger_type: str
    trigger_config: dict[str, Any] = {}
    action_type: str
    action_config: dict[str, Any] = {}
    enabled: bool = True


class AutomationRuleUpdateIn(BaseModel):
    name: str | None = None
    enabled: bool | None = None
    entity_type: str | None = None
    trigger_type: str | None = None
    trigger_config: dict[str, Any] | None = None
    action_type: str | None = None
    action_config: dict[str, Any] | None = None


class LeadFormIn(BaseModel):
    name: str
    fields: list[str] = []
    require_email: bool = True
    source: str | None = None
    success_message: str | None = None
    enabled: bool = True


class LeadFormUpdateIn(BaseModel):
    name: str | None = None
    fields: list[str] | None = None
    require_email: bool | None = None
    source: str | None = None
    success_message: str | None = None
    enabled: bool | None = None


class GoogleConfigIn(BaseModel):
    client_id: str
    client_secret: str


class RingCentralConnectIn(BaseModel):
    client_id: str
    client_secret: str
    jwt: str


class ColloquiConnectIn(BaseModel):
    base_url: str
    api_key: str


class ColloquiLinkIn(BaseModel):
    colloqui_user_id: uuid.UUID
    colloqui_username: str | None = None


class ImportRowIn(BaseModel):
    action: str = "create"  # create | skip | merge
    merge_id: uuid.UUID | None = None
    data: dict[str, Any] = {}
    tags: list[str] = []
    custom_fields: dict[str, Any] = {}


class ImportCommitIn(BaseModel):
    type: str
    rows: list[ImportRowIn]
