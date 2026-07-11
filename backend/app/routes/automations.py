"""Automation rules API. Any user can read the rules and their fire log;
only admins create, edit, delete, or run them — automations act org-wide
(create tasks for others, DM people), like the other admin-only settings."""

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps import get_current_user, require_admin
from app.models import (
    AutomationFire,
    AutomationRule,
    Pipeline,
    Stage,
    User,
)
from app.schemas import AutomationRuleIn, AutomationRuleUpdateIn
from app.services.automations import (
    ACTIONS,
    ENTITY_MODELS,
    TASK_ALLOWED_ACTIONS,
    TRIGGERS,
    record_label,
    run_sweep,
)

router = APIRouter()


def _bad(msg: str):
    return HTTPException(status_code=422, detail=msg)


def _int_config(cfg: dict, key: str, lo: int, hi: int) -> None:
    try:
        v = int(cfg[key])
    except (KeyError, TypeError, ValueError):
        raise _bad(f"trigger_config.{key} must be an integer")
    if not lo <= v <= hi:
        raise _bad(f"trigger_config.{key} must be between {lo} and {hi}")
    cfg[key] = v


async def _validate_user_ref(db, org_id, spec, field: str) -> None:
    if spec in (None, "", "owner"):
        return
    try:
        user_id = uuid.UUID(str(spec))
    except ValueError:
        raise _bad(f"{field} must be 'owner' or a user id")
    ok = (
        await db.execute(select(User.id).where(User.id == user_id, User.org_id == org_id))
    ).scalar_one_or_none()
    if ok is None:
        raise _bad(f"{field}: no such user")


async def _validate_rule(
    db: AsyncSession,
    org_id: uuid.UUID,
    entity_type: str,
    trigger_type: str,
    trigger_config: dict,
    action_type: str,
    action_config: dict,
) -> None:
    if entity_type not in ENTITY_MODELS:
        raise _bad(f"entity_type must be one of {sorted(ENTITY_MODELS)}")
    trig = TRIGGERS.get(trigger_type)
    if trig is None:
        raise _bad(f"Unknown trigger_type {trigger_type!r}")
    if entity_type not in trig["entities"]:
        raise _bad(f"Trigger {trigger_type!r} does not apply to {entity_type} records")
    for key in trig["required"]:
        if trigger_config.get(key) in (None, ""):
            raise _bad(f"trigger_config.{key} is required for {trigger_type}")

    if trigger_type in ("stale_record", "task_overdue"):
        _int_config(trigger_config, "days", 1, 3650)
    if trigger_type == "stale_record":
        statuses = trigger_config.get("status")
        if statuses is not None:
            if not isinstance(statuses, list) or not all(isinstance(s, str) for s in statuses):
                raise _bad("trigger_config.status must be a list of strings")
            if not statuses:
                trigger_config.pop("status")
        if trigger_config.get("pipeline_id"):
            if entity_type != "opportunity":
                raise _bad("trigger_config.pipeline_id only applies to opportunities")
            try:
                pid = uuid.UUID(str(trigger_config["pipeline_id"]))
            except ValueError:
                raise _bad("trigger_config.pipeline_id must be a UUID")
            ok = (
                await db.execute(
                    select(Pipeline.id).where(Pipeline.id == pid, Pipeline.org_id == org_id)
                )
            ).scalar_one_or_none()
            if ok is None:
                raise _bad("trigger_config.pipeline_id: no such pipeline")
    if trigger_type == "stage_entered":
        try:
            stage_id = uuid.UUID(str(trigger_config["stage_id"]))
        except ValueError:
            raise _bad("trigger_config.stage_id must be a UUID")
        ok = (
            await db.execute(
                select(Stage.id)
                .join(Pipeline, Pipeline.id == Stage.pipeline_id)
                .where(Stage.id == stage_id, Pipeline.org_id == org_id)
            )
        ).scalar_one_or_none()
        if ok is None:
            raise _bad("trigger_config.stage_id: no such stage")

    act = ACTIONS.get(action_type)
    if act is None:
        raise _bad(f"Unknown action_type {action_type!r}")
    if entity_type == "task" and action_type not in TASK_ALLOWED_ACTIONS:
        # Tasks have no tags; only create_task/notify make sense.
        raise _bad(f"Task rules only support {sorted(TASK_ALLOWED_ACTIONS)} actions")
    for key in act["required"]:
        value = action_config.get(key)
        if value in (None, "") or (isinstance(value, str) and not value.strip()):
            raise _bad(f"action_config.{key} is required for {action_type}")

    if action_type == "create_task":
        if action_config.get("due_in_days") not in (None, ""):
            try:
                action_config["due_in_days"] = int(action_config["due_in_days"])
            except (TypeError, ValueError):
                raise _bad("action_config.due_in_days must be an integer")
            if not 0 <= action_config["due_in_days"] <= 3650:
                raise _bad("action_config.due_in_days must be between 0 and 3650")
        await _validate_user_ref(db, org_id, action_config.get("assignee"), "action_config.assignee")
    elif action_type == "notify":
        await _validate_user_ref(
            db, org_id, action_config.get("recipient"), "action_config.recipient"
        )


def _rule_out(rule: AutomationRule, fire_count: int = 0, last_fired_at=None) -> dict:
    return {
        "id": str(rule.id),
        "name": rule.name,
        "enabled": rule.enabled,
        "entity_type": rule.entity_type,
        "trigger_type": rule.trigger_type,
        "trigger_config": rule.trigger_config or {},
        "action_type": rule.action_type,
        "action_config": rule.action_config or {},
        "created_by": str(rule.created_by) if rule.created_by else None,
        "created_at": rule.created_at.isoformat() if rule.created_at else None,
        "fire_count": fire_count,
        "last_fired_at": last_fired_at.isoformat() if last_fired_at else None,
    }


@router.get("")
async def list_rules(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    agg = (
        select(
            AutomationFire.rule_id,
            func.count().label("fire_count"),
            func.max(AutomationFire.fired_at).label("last_fired_at"),
        )
        .group_by(AutomationFire.rule_id)
        .subquery()
    )
    rows = await db.execute(
        select(AutomationRule, agg.c.fire_count, agg.c.last_fired_at)
        .outerjoin(agg, agg.c.rule_id == AutomationRule.id)
        .where(AutomationRule.org_id == user.org_id)
        .order_by(AutomationRule.created_at)
    )
    return {"items": [_rule_out(r, c or 0, last) for r, c, last in rows]}


async def _get_rule(db, user, rule_id: uuid.UUID) -> AutomationRule:
    rule = (
        await db.execute(
            select(AutomationRule).where(
                AutomationRule.id == rule_id, AutomationRule.org_id == user.org_id
            )
        )
    ).scalar_one_or_none()
    if rule is None:
        raise HTTPException(status_code=404, detail="Automation not found")
    return rule


@router.post("", status_code=201)
async def create_rule(
    body: AutomationRuleIn,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    name = body.name.strip()
    if not name:
        raise _bad("name is required")
    await _validate_rule(
        db,
        user.org_id,
        body.entity_type,
        body.trigger_type,
        body.trigger_config,
        body.action_type,
        body.action_config,
    )
    rule = AutomationRule(
        org_id=user.org_id,
        name=name[:120],
        enabled=body.enabled,
        entity_type=body.entity_type,
        trigger_type=body.trigger_type,
        trigger_config=body.trigger_config,
        action_type=body.action_type,
        action_config=body.action_config,
        created_by=user.id,
    )
    db.add(rule)
    await db.flush()
    result = _rule_out(rule)
    await db.commit()  # visible before the client refetches
    return result


@router.patch("/{rule_id}")
async def update_rule(
    rule_id: uuid.UUID,
    body: AutomationRuleUpdateIn,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    rule = await _get_rule(db, user, rule_id)
    data = body.model_dump(exclude_unset=True)
    merged = {
        "entity_type": data.get("entity_type", rule.entity_type),
        "trigger_type": data.get("trigger_type", rule.trigger_type),
        "trigger_config": data.get("trigger_config", rule.trigger_config or {}),
        "action_type": data.get("action_type", rule.action_type),
        "action_config": data.get("action_config", rule.action_config or {}),
    }
    await _validate_rule(db, user.org_id, **merged)
    if "name" in data:
        name = (data["name"] or "").strip()
        if not name:
            raise _bad("name is required")
        rule.name = name[:120]
    if "enabled" in data:
        rule.enabled = bool(data["enabled"])
    rule.entity_type = merged["entity_type"]
    rule.trigger_type = merged["trigger_type"]
    rule.trigger_config = merged["trigger_config"]
    rule.action_type = merged["action_type"]
    rule.action_config = merged["action_config"]
    result = _rule_out(rule)
    await db.commit()  # visible before the client refetches
    return result


@router.delete("/{rule_id}", status_code=204)
async def delete_rule(
    rule_id: uuid.UUID,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    rule = await _get_rule(db, user, rule_id)
    # Explicit fire cleanup: the FK cascade covers Postgres, but SQLite dev
    # runs without PRAGMA foreign_keys.
    await db.execute(delete(AutomationFire).where(AutomationFire.rule_id == rule.id))
    await db.delete(rule)
    await db.commit()


@router.get("/{rule_id}/fires")
async def list_fires(
    rule_id: uuid.UUID,
    limit: int = 50,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    rule = await _get_rule(db, user, rule_id)
    limit = min(max(1, limit), 200)
    fires = (
        (
            await db.execute(
                select(AutomationFire)
                .where(AutomationFire.rule_id == rule.id)
                .order_by(AutomationFire.fired_at.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    # Resolve current record labels (like the trash list does); fall back to
    # the label captured in detail for records deleted since.
    labels: dict[uuid.UUID, str] = {}
    ids = [f.entity_id for f in fires]
    if ids:
        model = ENTITY_MODELS.get(rule.entity_type)
        if model is not None:
            for obj in (await db.execute(select(model).where(model.id.in_(ids)))).scalars():
                labels[obj.id] = record_label(rule.entity_type, obj)
    return {
        "items": [
            {
                "id": str(f.id),
                "entity_type": f.entity_type,
                "entity_id": str(f.entity_id),
                "entity_label": labels.get(f.entity_id)
                or (f.detail or {}).get("record")
                or "(deleted)",
                "fired_at": f.fired_at.isoformat() if f.fired_at else None,
                "detail": f.detail or {},
            }
            for f in fires
        ]
    }


@router.post("/run-now")
async def run_now(user: User = Depends(require_admin)):
    """Run one sweep immediately instead of waiting for the interval."""
    fired = await run_sweep()
    return {"fired": fired}
