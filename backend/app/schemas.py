import uuid
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, EmailStr


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

class ExtrasIn(BaseModel):
    tags: list[str] | None = None
    custom_fields: dict[str, Any] | None = None


class CompanyIn(ExtrasIn):
    name: str | None = None
    details: str | None = None
    email_domain: str | None = None
    street: str | None = None
    city: str | None = None
    state: str | None = None
    postal_code: str | None = None
    country: str | None = None
    contact_type: str | None = None
    owner_id: uuid.UUID | None = None
    work_phone: str | None = None
    work_website: str | None = None
    linkedin: str | None = None
    facebook: str | None = None


class PersonIn(ExtrasIn):
    first_name: str | None = None
    middle_name: str | None = None
    last_name: str | None = None
    prefix: str | None = None
    suffix: str | None = None
    title: str | None = None
    details: str | None = None
    company_id: uuid.UUID | None = None
    contact_type: str | None = None
    work_email: str | None = None
    personal_email: str | None = None
    work_phone: str | None = None
    mobile_phone: str | None = None
    street: str | None = None
    city: str | None = None
    state: str | None = None
    postal_code: str | None = None
    country: str | None = None
    work_website: str | None = None
    personal_website: str | None = None
    linkedin: str | None = None
    facebook: str | None = None
    owner_id: uuid.UUID | None = None
    last_contacted_at: datetime | None = None


class LeadIn(ExtrasIn):
    first_name: str | None = None
    middle_name: str | None = None
    last_name: str | None = None
    prefix: str | None = None
    suffix: str | None = None
    title: str | None = None
    details: str | None = None
    value: float | None = None
    currency: str | None = None
    company_name: str | None = None
    source: str | None = None
    status: str | None = None
    street: str | None = None
    city: str | None = None
    state: str | None = None
    postal_code: str | None = None
    country: str | None = None
    work_phone: str | None = None
    mobile_phone: str | None = None
    email: str | None = None
    work_website: str | None = None
    personal_website: str | None = None
    linkedin: str | None = None
    facebook: str | None = None
    owner_id: uuid.UUID | None = None


class OpportunityIn(ExtrasIn):
    name: str | None = None
    details: str | None = None
    company_id: uuid.UUID | None = None
    primary_person_id: uuid.UUID | None = None
    status: str | None = None
    priority: str | None = None
    owner_id: uuid.UUID | None = None
    close_date: date | None = None
    value: float | None = None
    currency: str | None = None
    win_probability: int | None = None
    pipeline_id: uuid.UUID | None = None
    stage_id: uuid.UUID | None = None
    source: str | None = None
    loss_reason: str | None = None


class TaskIn(BaseModel):
    name: str | None = None
    details: str | None = None
    entity_type: str | None = None
    entity_id: uuid.UUID | None = None
    due_at: datetime | None = None
    reminder_at: datetime | None = None
    priority: str | None = None
    assignee_id: uuid.UUID | None = None


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
    pipeline_id: uuid.UUID | None = None
    opportunity_name: str | None = None
    opportunity_value: float | None = None


class StageIn(BaseModel):
    name: str
    win_probability: int = 0
    position: int | None = None


class StageUpdateIn(BaseModel):
    name: str | None = None
    win_probability: int | None = None
    position: int | None = None


class PipelineIn(BaseModel):
    name: str
    stages: list[StageIn] | None = None


class PipelineUpdateIn(BaseModel):
    name: str | None = None
    position: int | None = None


class CustomFieldIn(BaseModel):
    entity_type: str
    name: str
    field_type: str = "text"
    options: list[str] | None = None
    position: int | None = None


class CustomFieldUpdateIn(BaseModel):
    name: str | None = None
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
