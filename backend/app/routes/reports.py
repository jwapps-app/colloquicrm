"""Read-only reporting aggregates.

All endpoints are org-scoped, bearer-authed, and exclude soft-deleted rows.
Time-series bucketing happens in Python from narrow (date-only) row sets so
the SQL stays dialect-safe (Postgres and the SQLite dev database) — counts
and sums that don't need per-bucket dates use SQL aggregation.
"""

import uuid
from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends
from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps import get_current_user
from app.models import (
    EmailMessage,
    EmailParticipant,
    Lead,
    Note,
    Opportunity,
    PhoneEvent,
    Pipeline,
    Stage,
    Task,
    User,
    utcnow,
)

router = APIRouter()

# range name -> (bucket unit, lookback days). None days = no lower bound.
RANGES = {
    "30d": ("day", 30),
    "90d": ("week", 90),
    "12m": ("month", 365),
    "all": ("month", None),
}


def _parse_range(range_: str) -> tuple[str, datetime | None]:
    unit, days = RANGES.get(range_, RANGES["90d"])
    since = utcnow() - timedelta(days=days) if days else None
    return unit, since


def _to_date(v) -> date | None:
    """datetime (naive SQLite or tz-aware Postgres) or date -> date."""
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.date()
    return v


def _bucket_key(unit: str, d: date) -> date:
    if unit == "day":
        return d
    if unit == "week":
        return d - timedelta(days=d.weekday())  # Monday
    return d.replace(day=1)


def _bucket_label(unit: str, d: date) -> str:
    if unit == "month":
        return f"{d.strftime('%b')} {d.year}"
    return f"{d.strftime('%b')} {d.day}"


def _next_bucket(unit: str, d: date) -> date:
    if unit == "day":
        return d + timedelta(days=1)
    if unit == "week":
        return d + timedelta(days=7)
    return (d.replace(day=1) + timedelta(days=32)).replace(day=1)


def _build_buckets(unit: str, start: date | None, end: date) -> list[date]:
    """Contiguous bucket keys covering start..end (capped defensively)."""
    if start is None or start > end:
        return []
    keys = []
    cur = _bucket_key(unit, start)
    last = _bucket_key(unit, end)
    while cur <= last and len(keys) < 400:
        keys.append(cur)
        cur = _next_bucket(unit, cur)
    return keys


def _money(v) -> float:
    return round(float(v or 0), 2)


@router.get("/pipeline")
async def pipeline_report(
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    """Current open-pipeline snapshot; the range param does not apply."""
    pipelines = (
        (
            await db.execute(
                select(Pipeline)
                .where(Pipeline.org_id == user.org_id)
                .order_by(Pipeline.position, Pipeline.name)
            )
        )
        .scalars()
        .all()
    )
    stages = (
        (
            await db.execute(
                select(Stage)
                .where(Stage.pipeline_id.in_([p.id for p in pipelines] or [uuid.uuid4()]))
                .order_by(Stage.position)
            )
        )
        .scalars()
        .all()
    )
    agg = await db.execute(
        select(
            Opportunity.pipeline_id,
            Opportunity.stage_id,
            func.count(Opportunity.id),
            func.coalesce(func.sum(Opportunity.value), 0),
        )
        .where(
            Opportunity.org_id == user.org_id,
            Opportunity.deleted_at.is_(None),
            Opportunity.status == "open",
        )
        .group_by(Opportunity.pipeline_id, Opportunity.stage_id)
    )
    by_stage: dict = {}
    extra_by_pipeline: dict = {}  # open opps with a pipeline but no stage
    for pid, sid, count, value in agg:
        if sid is not None:
            by_stage[sid] = (count, value)
        elif pid is not None:
            prev = extra_by_pipeline.get(pid, (0, 0))
            extra_by_pipeline[pid] = (prev[0] + count, float(prev[1]) + float(value or 0))

    out = []
    grand = {"open_count": 0, "open_value": 0.0, "weighted_forecast": 0.0}
    for p in pipelines:
        stage_rows = []
        totals = {"open_count": 0, "open_value": 0.0, "weighted_forecast": 0.0}
        for s in stages:
            if s.pipeline_id != p.id:
                continue
            count, value = by_stage.get(s.id, (0, 0))
            value = _money(value)
            weighted = round(value * (s.win_probability or 0) / 100, 2)
            stage_rows.append(
                {
                    "stage_id": str(s.id),
                    "name": s.name,
                    "win_probability": s.win_probability,
                    "count": count,
                    "total_value": value,
                    "weighted_value": weighted,
                }
            )
            totals["open_count"] += count
            totals["open_value"] = round(totals["open_value"] + value, 2)
            totals["weighted_forecast"] = round(totals["weighted_forecast"] + weighted, 2)
        extra_count, extra_value = extra_by_pipeline.get(p.id, (0, 0))
        totals["open_count"] += extra_count
        totals["open_value"] = round(totals["open_value"] + _money(extra_value), 2)
        grand["open_count"] += totals["open_count"]
        grand["open_value"] = round(grand["open_value"] + totals["open_value"], 2)
        grand["weighted_forecast"] = round(
            grand["weighted_forecast"] + totals["weighted_forecast"], 2
        )
        out.append(
            {"id": str(p.id), "name": p.name, "stages": stage_rows, "totals": totals}
        )
    return {"pipelines": out, "totals": grand}


@router.get("/sales")
async def sales_report(
    range: str = "90d",
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Won/lost analysis. The effective date of a win/loss is close_date when
    set, else updated_at (the status flip touches updated_at)."""
    unit, since = _parse_range(range)
    q = select(
        Opportunity.status,
        Opportunity.value,
        Opportunity.close_date,
        Opportunity.created_at,
        Opportunity.updated_at,
    ).where(
        Opportunity.org_id == user.org_id,
        Opportunity.deleted_at.is_(None),
        Opportunity.status.in_(["won", "lost"]),
    )
    if since is not None:
        q = q.where(
            or_(
                Opportunity.close_date >= since.date(),
                and_(Opportunity.close_date.is_(None), Opportunity.updated_at >= since),
            )
        )
    rows = (await db.execute(q)).all()

    today = utcnow().date()
    effective: list[tuple[date, str, float, date | None]] = []
    for status, value, close_date, created_at, updated_at in rows:
        eff = close_date or _to_date(updated_at) or today
        effective.append((eff, status, float(value or 0), _to_date(created_at)))

    start = since.date() if since else (min(e[0] for e in effective) if effective else None)
    end = max([today] + [e[0] for e in effective]) if effective else today
    keys = _build_buckets(unit, start, end)
    index = {k: i for i, k in enumerate(keys)}
    series = [
        {"bucket_label": _bucket_label(unit, k), "won_count": 0, "won_value": 0.0,
         "lost_count": 0, "lost_value": 0.0}
        for k in keys
    ]

    won_count = lost_count = 0
    won_value = lost_value = 0.0
    close_days: list[int] = []
    for eff, status, value, created in effective:
        b = index.get(_bucket_key(unit, eff))
        if status == "won":
            won_count += 1
            won_value += value
            if b is not None:
                series[b]["won_count"] += 1
                series[b]["won_value"] = round(series[b]["won_value"] + value, 2)
            if created is not None:
                close_days.append(max(0, (eff - created).days))
        else:
            lost_count += 1
            lost_value += value
            if b is not None:
                series[b]["lost_count"] += 1
                series[b]["lost_value"] = round(series[b]["lost_value"] + value, 2)

    decided = won_count + lost_count
    summary = {
        "won_count": won_count,
        "won_value": round(won_value, 2),
        "lost_count": lost_count,
        "lost_value": round(lost_value, 2),
        "win_rate": round(won_count / decided, 4) if decided else None,
        "avg_deal_size": round(won_value / won_count, 2) if won_count else None,
        "avg_days_to_close": round(sum(close_days) / len(close_days), 1)
        if close_days
        else None,
    }
    return {"range": range, "unit": unit, "summary": summary, "series": series}


@router.get("/activity")
async def activity_report(
    range: str = "90d",
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Per-user activity counts.

    Phone attribution: calls/texts are credited to the author of a linked
    note (manual call logs create one). Synced RingCentral events carry no
    per-user identity, so unattributed events aggregate into a single
    org-wide "Team" row instead of being force-assigned to a record owner.
    """
    _, since = _parse_range(range)

    async def grouped(col, *where) -> dict[uuid.UUID, int]:
        q = select(col, func.count()).where(col.is_not(None), *where).group_by(col)
        return {k: v for k, v in (await db.execute(q))}

    sent_where = [
        EmailMessage.org_id == user.org_id,
        EmailMessage.is_outgoing.is_(True),
    ]
    if since is not None:
        sent_where.append(EmailMessage.sent_at >= since)
    emails_sent = await grouped(EmailMessage.owner_user_id, *sent_where)

    engaged = exists().where(
        (EmailParticipant.email_id == EmailMessage.id) & EmailParticipant.direct.is_(True)
    )
    recv_where = [
        EmailMessage.org_id == user.org_id,
        EmailMessage.is_outgoing.is_(False),
        engaged,
    ]
    if since is not None:
        recv_where.append(EmailMessage.sent_at >= since)
    emails_received = await grouped(EmailMessage.owner_user_id, *recv_where)

    note_where = [Note.org_id == user.org_id]
    if since is not None:
        note_where.append(Note.created_at >= since)
    notes = await grouped(Note.author_id, *note_where)

    task_where = [Task.org_id == user.org_id, Task.completed_at.is_not(None)]
    if since is not None:
        task_where.append(Task.completed_at >= since)
    tasks_completed = await grouped(Task.assignee_id, *task_where)

    # Phone events: credit the linked note's author; leftovers -> "Team".
    phone_where = [PhoneEvent.org_id == user.org_id]
    if since is not None:
        phone_where.append(PhoneEvent.happened_at >= since)
    attributed = await db.execute(
        select(
            Note.author_id,
            PhoneEvent.kind,
            func.count(func.distinct(PhoneEvent.id)),
        )
        .join(PhoneEvent, Note.phone_event_id == PhoneEvent.id)
        .where(Note.author_id.is_not(None), *phone_where)
        .group_by(Note.author_id, PhoneEvent.kind)
    )
    calls: dict[uuid.UUID, int] = {}
    texts: dict[uuid.UUID, int] = {}
    attributed_by_kind = {"call": 0, "sms": 0}
    for author_id, kind, count in attributed:
        target = calls if kind == "call" else texts
        target[author_id] = target.get(author_id, 0) + count
        attributed_by_kind[kind] = attributed_by_kind.get(kind, 0) + count
    totals_by_kind = {
        k: v
        for k, v in (
            await db.execute(
                select(PhoneEvent.kind, func.count()).where(*phone_where).group_by(
                    PhoneEvent.kind
                )
            )
        )
    }
    team_calls = max(0, totals_by_kind.get("call", 0) - attributed_by_kind.get("call", 0))
    team_texts = max(0, totals_by_kind.get("sms", 0) - attributed_by_kind.get("sms", 0))

    users = (
        (
            await db.execute(
                select(User).where(User.org_id == user.org_id, User.is_active.is_(True))
            )
        )
        .scalars()
        .all()
    )
    known_ids = {u.id for u in users}
    rows = []
    for u in users:
        row = {
            "user_id": str(u.id),
            "display_name": u.display_name,
            "emails_sent": emails_sent.get(u.id, 0),
            "emails_received": emails_received.get(u.id, 0),
            "calls": calls.get(u.id, 0),
            "texts": texts.get(u.id, 0),
            "notes": notes.get(u.id, 0),
            "tasks_completed": tasks_completed.get(u.id, 0),
        }
        row["total"] = sum(
            row[k]
            for k in ("emails_sent", "emails_received", "calls", "texts", "notes",
                      "tasks_completed")
        )
        rows.append(row)
    # Counts credited to deactivated/removed users still belong to the org's
    # totals — fold them into the Team row rather than dropping them.
    orphan = {"emails_sent": 0, "emails_received": 0, "calls": 0, "texts": 0,
              "notes": 0, "tasks_completed": 0}
    for key, bucket in (
        ("emails_sent", emails_sent),
        ("emails_received", emails_received),
        ("calls", calls),
        ("texts", texts),
        ("notes", notes),
        ("tasks_completed", tasks_completed),
    ):
        for uid, count in bucket.items():
            if uid not in known_ids:
                orphan[key] += count
    orphan["calls"] += team_calls
    orphan["texts"] += team_texts
    team_total = sum(orphan.values())
    if team_total:
        rows.append(
            {"user_id": None, "display_name": "Team (unattributed)", **orphan,
             "total": team_total}
        )
    rows.sort(key=lambda r: (-r["total"], r["display_name"] or ""))
    return {
        "range": range,
        "rows": rows,
        "attribution_note": (
            "Calls & texts are credited to the author of the linked note; "
            "synced events without one appear under Team (unattributed)."
        ),
    }


@router.get("/leads")
async def leads_report(
    range: str = "90d",
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    unit, since = _parse_range(range)
    base = [Lead.org_id == user.org_id, Lead.deleted_at.is_(None)]

    new_where = list(base)
    conv_where = list(base) + [Lead.converted_at.is_not(None)]
    if since is not None:
        new_where.append(Lead.created_at >= since)
        conv_where.append(Lead.converted_at >= since)

    new_by_source = {
        k: v
        for k, v in (
            await db.execute(
                select(Lead.source, func.count()).where(*new_where).group_by(Lead.source)
            )
        )
    }
    conv_by_source = {
        k: v
        for k, v in (
            await db.execute(
                select(Lead.source, func.count()).where(*conv_where).group_by(Lead.source)
            )
        )
    }

    # Narrow date rows for the series + avg-days math (bucketed in Python so
    # the SQL stays dialect-safe).
    new_dates = [
        _to_date(d)
        for (d,) in (await db.execute(select(Lead.created_at).where(*new_where)))
        if d is not None
    ]
    conv_rows = (
        await db.execute(select(Lead.created_at, Lead.converted_at).where(*conv_where))
    ).all()
    conv_dates = [_to_date(c) for _, c in conv_rows if c is not None]

    today = utcnow().date()
    all_dates = new_dates + conv_dates
    start = since.date() if since else (min(all_dates) if all_dates else None)
    end = max([today] + all_dates) if all_dates else today
    keys = _build_buckets(unit, start, end)
    index = {k: i for i, k in enumerate(keys)}
    series = [
        {"bucket_label": _bucket_label(unit, k), "new_count": 0, "converted_count": 0}
        for k in keys
    ]
    for d in new_dates:
        b = index.get(_bucket_key(unit, d))
        if b is not None:
            series[b]["new_count"] += 1
    for d in conv_dates:
        b = index.get(_bucket_key(unit, d))
        if b is not None:
            series[b]["converted_count"] += 1

    convert_days = []
    for created_at, converted_at in conv_rows:
        c, v = _to_date(created_at), _to_date(converted_at)
        if c is not None and v is not None:
            convert_days.append(max(0, (v - c).days))

    new_leads = sum(new_by_source.values())
    converted = sum(conv_by_source.values())
    by_source = []
    for source in sorted(
        set(new_by_source) | set(conv_by_source),
        key=lambda s: -(new_by_source.get(s, 0) + conv_by_source.get(s, 0)),
    ):
        n = new_by_source.get(source, 0)
        c = conv_by_source.get(source, 0)
        by_source.append(
            {
                "source": source or "No source",
                "new_count": n,
                "converted_count": c,
                "conversion_rate": round(c / n, 4) if n else None,
            }
        )

    return {
        "range": range,
        "unit": unit,
        "summary": {
            "new_leads": new_leads,
            "converted": converted,
            "conversion_rate": round(converted / new_leads, 4) if new_leads else None,
            "avg_days_to_convert": round(sum(convert_days) / len(convert_days), 1)
            if convert_days
            else None,
        },
        "series": series,
        "by_source": by_source,
    }
