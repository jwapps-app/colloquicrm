"""Automations engine: rules that make the CRM proactive.

Each enabled rule pairs one trigger with one action. The engine sweeps every
SWEEP_INTERVAL_SECONDS: for every rule it first RE-ARMS (deletes fire rows
whose record no longer matches the trigger, so the rule can fire again after
the next lapse), then evaluates the trigger with bounded SQL (never loads a
whole table — WHERE + NOT EXISTS fire-row + LIMIT), executes the action, and
records an automation_fires row. That row is both the idempotency guard
(unique per rule+record) and the visible audit log.

Design note — stage_entered is sweep-based state matching (opp.stage_id ==
config stage, no fire row yet) rather than hook- or Activity-driven. The
sweep loop must exist anyway for stale_record/task_overdue, hooks would have
to thread automation calls through the generic CRUD factory for every write
path (PATCH, bulk, imports, board drag), and Activity 'updated' payloads
only carry field names, not values. State matching in the same sweep is
uniform, catches every write path, and the ≤5-minute latency is fine for a
CRM. Re-arm deletes the fire row when the opp has moved away, so returning
to the stage fires again.

Failure philosophy matches the reminder loop: per-rule try/commit/rollback,
one bad rule (or one down chat server) can never kill the loop or block the
other rules.
"""

import asyncio
import logging
import uuid
from datetime import timedelta

from sqlalchemy import and_, case, delete, or_, select

from app.config import settings
from app.db import SessionLocal
from app.models import (
    AutomationFire,
    AutomationRule,
    Lead,
    Opportunity,
    Person,
    Task,
    User,
    utcnow,
)
from app.services import colloqui, push
from app.services.colloqui import ColloquiError
from app.services.common import add_tags

log = logging.getLogger("automations")

SWEEP_INTERVAL_SECONDS = 300
# Bound the work of any single rule in one pass; the rest fires next sweep.
MAX_FIRES_PER_RULE = 200

ENTITY_MODELS = {"lead": Lead, "person": Person, "opportunity": Opportunity, "task": Task}

# Trigger/action catalog — the single source of truth for validation (routes)
# and evaluation (here). trigger: allowed entity types + required config keys.
TRIGGERS: dict[str, dict] = {
    "stale_record": {"entities": {"lead", "person", "opportunity"}, "required": ["days"]},
    "stage_entered": {"entities": {"opportunity"}, "required": ["stage_id"]},
    "task_overdue": {"entities": {"task"}, "required": ["days"]},
    "record_created": {"entities": {"lead", "opportunity", "person"}, "required": []},
}
ACTIONS: dict[str, dict] = {
    "create_task": {"required": ["name"]},
    "notify": {"required": ["recipient", "message"]},
    "add_tag": {"required": ["tag"]},
}
# Tasks have no tags (has_extras=False); tagging a task is meaningless.
TASK_ALLOWED_ACTIONS = {"create_task", "notify"}

ENTITY_PATHS = {"lead": "/leads/{id}", "person": "/people/{id}",
                "opportunity": "/opportunities/{id}", "task": "/tasks"}


def record_label(entity_type: str, obj) -> str:
    """Display label for a record, used by {name} placeholders and fire logs."""
    if entity_type in ("opportunity", "task"):
        return obj.name or "(unnamed)"
    name = " ".join(filter(None, [obj.first_name, obj.last_name]))
    return name or getattr(obj, "company_name", None) or getattr(obj, "email", None) or "(unnamed)"


def _record_link(entity_type: str, entity_id: uuid.UUID) -> str:
    path = ENTITY_PATHS[entity_type].format(id=entity_id)
    return f"{settings.app_url.rstrip('/')}{path}"


def _touch_expr(model):
    """A record's last-touch time. For people, an interaction (call, email)
    counts as a touch even when nobody edited the record."""
    if model is Person:
        return case(
            (
                and_(
                    Person.last_contacted_at.is_not(None),
                    Person.last_contacted_at > Person.updated_at,
                ),
                Person.last_contacted_at,
            ),
            else_=Person.updated_at,
        )
    return model.updated_at


def _no_fire(rule: AutomationRule, entity_id_col):
    return ~(
        select(AutomationFire.id)
        .where(AutomationFire.rule_id == rule.id, AutomationFire.entity_id == entity_id_col)
        .exists()
    )


# ---- re-arm ----


async def _rearm(db, rule: AutomationRule, now) -> None:
    """Delete fire rows whose record no longer matches the trigger, so the
    rule can fire on that record again. record_created never re-arms."""
    cfg = rule.trigger_config or {}
    model = ENTITY_MODELS[rule.entity_type]
    if rule.trigger_type == "stale_record":
        stmt = (
            select(AutomationFire.id)
            .join(model, model.id == AutomationFire.entity_id)
            .where(AutomationFire.rule_id == rule.id, _touch_expr(model) > AutomationFire.fired_at)
        )
    elif rule.trigger_type == "stage_entered":
        stage_id = uuid.UUID(str(cfg["stage_id"]))
        stmt = (
            select(AutomationFire.id)
            .join(Opportunity, Opportunity.id == AutomationFire.entity_id)
            .where(
                AutomationFire.rule_id == rule.id,
                or_(Opportunity.stage_id.is_(None), Opportunity.stage_id != stage_id),
            )
        )
    elif rule.trigger_type == "task_overdue":
        cutoff = now - timedelta(days=int(cfg["days"]))
        stmt = (
            select(AutomationFire.id)
            .join(Task, Task.id == AutomationFire.entity_id)
            .where(
                AutomationFire.rule_id == rule.id,
                or_(Task.status != "open", Task.due_at.is_(None), Task.due_at >= cutoff),
            )
        )
    else:  # record_created — once ever per record
        return
    ids = [fid for (fid,) in await db.execute(stmt)]
    if ids:
        await db.execute(delete(AutomationFire).where(AutomationFire.id.in_(ids)))
        await db.commit()


# ---- trigger evaluation ----


async def _candidates(db, rule: AutomationRule, now) -> list:
    cfg = rule.trigger_config or {}
    model = ENTITY_MODELS[rule.entity_type]

    if rule.trigger_type == "stale_record":
        cutoff = now - timedelta(days=int(cfg["days"]))
        stmt = select(model).where(
            model.org_id == rule.org_id,
            model.deleted_at.is_(None),
            _touch_expr(model) < cutoff,
            _no_fire(rule, model.id),
        )
        if model is Lead:
            # A converted lead lives on as a Person; nagging about it is noise.
            stmt = stmt.where(Lead.converted_at.is_(None))
        statuses = cfg.get("status")
        if statuses and hasattr(model, "status"):
            stmt = stmt.where(model.status.in_(list(statuses)))
        if cfg.get("pipeline_id") and model is Opportunity:
            stmt = stmt.where(Opportunity.pipeline_id == uuid.UUID(str(cfg["pipeline_id"])))
    elif rule.trigger_type == "stage_entered":
        stmt = select(Opportunity).where(
            Opportunity.org_id == rule.org_id,
            Opportunity.deleted_at.is_(None),
            Opportunity.stage_id == uuid.UUID(str(cfg["stage_id"])),
            _no_fire(rule, Opportunity.id),
        )
    elif rule.trigger_type == "task_overdue":
        cutoff = now - timedelta(days=int(cfg["days"]))
        stmt = select(Task).where(
            Task.org_id == rule.org_id,
            Task.status == "open",
            Task.due_at.is_not(None),
            Task.due_at < cutoff,
            _no_fire(rule, Task.id),
        )
    elif rule.trigger_type == "record_created":
        # Only records created AFTER the rule — a new rule must never
        # retro-fire across thousands of imported records.
        stmt = select(model).where(
            model.org_id == rule.org_id,
            model.deleted_at.is_(None),
            model.created_at > rule.created_at,
            _no_fire(rule, model.id),
        )
    else:
        log.warning("Rule %s has unknown trigger %r; skipping", rule.id, rule.trigger_type)
        return []
    return list((await db.execute(stmt.limit(MAX_FIRES_PER_RULE))).scalars().all())


# ---- actions ----


async def _resolve_user(db, org_id, spec, obj, rule) -> User | None:
    """'owner' -> the record's owner (task: assignee), falling back to the
    rule's creator; otherwise an explicit user id."""
    user_id = None
    if spec in (None, "", "owner"):
        user_id = (
            getattr(obj, "owner_id", None) or getattr(obj, "assignee_id", None) or rule.created_by
        )
    else:
        user_id = uuid.UUID(str(spec))
    if user_id is None:
        return None
    return (
        await db.execute(select(User).where(User.id == user_id, User.org_id == org_id))
    ).scalar_one_or_none()


async def _act_create_task(db, rule: AutomationRule, obj, label: str) -> dict:
    cfg = rule.action_config or {}
    name = str(cfg.get("name") or "Follow up with {name}").replace("{name}", label)[:255]
    due_at = None
    if cfg.get("due_in_days") not in (None, ""):
        due_at = utcnow() + timedelta(days=int(cfg["due_in_days"]))
    assignee = await _resolve_user(db, rule.org_id, cfg.get("assignee"), obj, rule)
    # For task triggers, link the new task to the overdue task's record (a
    # task pointing at another task helps nobody).
    if rule.entity_type == "task":
        link_type, link_id = obj.entity_type, obj.entity_id
    else:
        link_type, link_id = rule.entity_type, obj.id
    task = Task(
        org_id=rule.org_id,
        name=name,
        entity_type=link_type,
        entity_id=link_id,
        due_at=due_at,
        status="open",
        assignee_id=assignee.id if assignee else None,
        created_by=rule.created_by,
    )
    db.add(task)
    await db.flush()
    return {
        "action": "create_task",
        "task_id": str(task.id),
        "task_name": name,
        "assignee": assignee.display_name if assignee else None,
    }


async def _act_notify(db, rule: AutomationRule, obj, label: str) -> dict:
    """Route exactly like due-task reminders: crm_push users get an APNs push
    (with the attention badge), colloqui_chat users get a DM, broken DM links
    fall back to the #tasks channel — and if no channel is available at all
    the fire still logs, it just delivers nowhere."""
    cfg = rule.action_config or {}
    message = str(cfg.get("message") or "{name} needs attention").replace("{name}", label)
    recipient = await _resolve_user(db, rule.org_id, cfg.get("recipient"), obj, rule)
    link = _record_link(rule.entity_type, obj.id)
    detail = {
        "action": "notify",
        "message": message,
        "recipient": recipient.display_name if recipient else None,
        "delivered": None,
    }
    if recipient is None:
        log.warning("Rule %s: no resolvable recipient; notify undelivered", rule.id)
        return detail

    if colloqui._wants_push(recipient):
        sent = await push.send_to_user(
            db,
            recipient.id,
            "Automation",
            message,
            {"kind": "automation", "entity_type": rule.entity_type, "entity_id": str(obj.id)},
            badge=await colloqui._attention_count(db, recipient.id),
        )
        if sent:
            detail["delivered"] = "push"
            return detail
        # No working device — fall through to chat so it isn't lost.

    row = await colloqui.get_integration(db, rule.org_id)
    if not colloqui.is_enabled(row):
        log.warning("Rule %s: no push devices and no chat integration; notify undelivered", rule.id)
        return detail
    client = colloqui._client_for(row)
    content = f"⚙️ {message}\n{link}"
    if recipient.colloqui_user_id:
        try:
            dm = await client.open_dm(str(recipient.colloqui_user_id))
            await client.send_message(dm["id"], content)
            detail["delivered"] = "dm"
            return detail
        except ColloquiError as exc:
            log.warning("Rule %s: DM failed (%s); posting to #tasks", rule.id, exc)
    await client.send_message(
        str(row.tasks_channel_id), f"⚙️ {colloqui._mention(recipient)} — {message}\n{link}"
    )
    detail["delivered"] = "channel"
    return detail


async def _act_add_tag(db, rule: AutomationRule, obj, label: str) -> dict:
    tag = str((rule.action_config or {}).get("tag") or "").strip()
    if tag:
        await add_tags(db, rule.org_id, rule.entity_type, obj.id, [tag])
    return {"action": "add_tag", "tag": tag}


async def _execute_action(db, rule: AutomationRule, obj) -> dict:
    label = record_label(rule.entity_type, obj)
    if rule.action_type == "create_task":
        detail = await _act_create_task(db, rule, obj, label)
    elif rule.action_type == "notify":
        detail = await _act_notify(db, rule, obj, label)
    elif rule.action_type == "add_tag":
        detail = await _act_add_tag(db, rule, obj, label)
    else:
        raise ValueError(f"unknown action {rule.action_type!r}")
    detail["record"] = label
    return detail


# ---- the sweep ----


async def _process_rule(db, rule: AutomationRule, now) -> int:
    await _rearm(db, rule, now)
    fired = 0
    for obj in await _candidates(db, rule, now):
        detail = await _execute_action(db, rule, obj)
        db.add(
            AutomationFire(
                org_id=rule.org_id,
                rule_id=rule.id,
                entity_type=rule.entity_type,
                entity_id=obj.id,
                detail=detail,
            )
        )
        # Commit per fire: the action already happened out in the world
        # (push/DM sent); losing the fire row to a later record's failure
        # would re-fire it next sweep.
        await db.commit()
        fired += 1
        log.info("Rule %r fired on %s %s: %s", rule.name, rule.entity_type, obj.id, detail)
    return fired


async def run_sweep() -> int:
    """One pass over all enabled rules. Returns total fires."""
    async with SessionLocal() as db:
        rules = list(
            (
                await db.execute(
                    select(AutomationRule)
                    .where(AutomationRule.enabled.is_(True))
                    .order_by(AutomationRule.created_at)
                )
            )
            .scalars()
            .all()
        )
    now = utcnow()
    total = 0
    for rule in rules:
        # Fresh session per rule so one bad rule's poisoned transaction can't
        # bleed into the next.
        async with SessionLocal() as db:
            try:
                total += await _process_rule(db, rule, now)
            except Exception:
                log.exception("Automation rule %s (%r) failed", rule.id, rule.name)
                await db.rollback()
    return total


async def automations_loop() -> None:
    while True:
        try:
            await run_sweep()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Automations sweep failed")
        await asyncio.sleep(SWEEP_INTERVAL_SECONDS)
