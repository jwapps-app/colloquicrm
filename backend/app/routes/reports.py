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


DEFAULT_CURRENCY = "USD"
UNASSIGNED = "unassigned"


def _currency_expr():
    """An opportunity's currency as a grouping key: upper-cased, blank/NULL
    read as the column default."""
    return func.upper(
        func.coalesce(func.nullif(func.trim(Opportunity.currency), ""), DEFAULT_CURRENCY)
    )


async def _dominant_currency(db: AsyncSession, org_id: uuid.UUID) -> str:
    """The currency most of the org's deals are in. Money in different
    currencies can't be added, so headline totals are stated in this one and
    everything else is reported alongside, per currency — never summed in."""
    cur = _currency_expr()
    row = (
        await db.execute(
            select(cur, func.count())
            .where(Opportunity.org_id == org_id, Opportunity.deleted_at.is_(None))
            .group_by(cur)
            .order_by(func.count().desc(), cur)
            .limit(1)
        )
    ).first()
    return row[0] if row else DEFAULT_CURRENCY


class _Bucket:
    """Open deals in one slot of the pipeline report, kept per currency."""

    def __init__(self):
        self.by_currency: dict[str, dict] = {}

    def add(self, currency: str, count: int, value, weighted) -> None:
        cur = self.by_currency.setdefault(
            currency, {"count": 0, "total_value": 0.0, "weighted_value": 0.0}
        )
        cur["count"] += count
        cur["total_value"] = round(cur["total_value"] + _money(value), 2)
        cur["weighted_value"] = round(cur["weighted_value"] + _money(weighted), 2)

    @property
    def count(self) -> int:
        return sum(c["count"] for c in self.by_currency.values())

    def value(self, currency: str) -> float:
        return self.by_currency.get(currency, {}).get("total_value", 0.0)

    def weighted(self, currency: str) -> float:
        return self.by_currency.get(currency, {}).get("weighted_value", 0.0)


class _Totals:
    def __init__(self, currency: str):
        self.currency = currency
        self.open_count = 0
        self.by_currency: dict[str, dict] = {}

    def add(self, bucket: _Bucket) -> None:
        self.open_count += bucket.count
        for currency, c in bucket.by_currency.items():
            t = self.by_currency.setdefault(
                currency, {"open_count": 0, "open_value": 0.0, "weighted_forecast": 0.0}
            )
            t["open_count"] += c["count"]
            t["open_value"] = round(t["open_value"] + c["total_value"], 2)
            t["weighted_forecast"] = round(t["weighted_forecast"] + c["weighted_value"], 2)

    def merge(self, other: "_Totals") -> None:
        self.open_count += other.open_count
        for currency, c in other.by_currency.items():
            t = self.by_currency.setdefault(
                currency, {"open_count": 0, "open_value": 0.0, "weighted_forecast": 0.0}
            )
            for key, amount in c.items():
                t[key] = t[key] + amount if key == "open_count" else round(t[key] + amount, 2)

    def out(self) -> dict:
        mine = self.by_currency.get(self.currency, {})
        return {
            # Every open deal, whatever its currency.
            "open_count": self.open_count,
            # Money: deals in `currency` only. The rest is in by_currency.
            "open_value": mine.get("open_value", 0.0),
            "weighted_forecast": mine.get("weighted_forecast", 0.0),
            "by_currency": self.by_currency,
        }


def _stage_row(stage_id: str, name: str, win_probability, bucket: _Bucket, currency: str) -> dict:
    return {
        "stage_id": stage_id,
        "name": name,
        "win_probability": win_probability,
        "count": bucket.count,
        "total_value": bucket.value(currency),
        "weighted_value": bucket.weighted(currency),
        "by_currency": bucket.by_currency,
    }


def _effective_probability(bucket: _Bucket, currency: str) -> int:
    """The value-weighted win % of a bucket that has no stage to borrow one
    from — what its deals' own probabilities amount to."""
    value = bucket.value(currency)
    return round(bucket.weighted(currency) / value * 100) if value else 0


@router.get("/pipeline")
async def pipeline_report(
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    """Current open-pipeline snapshot; the range param does not apply.

    Every open deal is counted somewhere: in its stage; in a "No stage" row
    of its pipeline; or, with neither, in a trailing "Unassigned" pipeline.
    A deal is weighted by ITS OWN win_probability when it has one (that is
    the number its card shows), else by its stage's. Monetary totals cover
    the org's dominant currency only — `currency` names it, `by_currency`
    carries the rest, and `mixed_currencies` says whether there is a rest."""
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
    currency = await _dominant_currency(db, user.org_id)
    cur = _currency_expr()
    probability = func.coalesce(Opportunity.win_probability, Stage.win_probability, 0)
    agg = await db.execute(
        select(
            Opportunity.pipeline_id,
            Opportunity.stage_id,
            cur,
            func.count(Opportunity.id),
            func.coalesce(func.sum(Opportunity.value), 0),
            func.coalesce(func.sum(Opportunity.value * probability), 0),
        )
        .outerjoin(Stage, Stage.id == Opportunity.stage_id)
        .where(
            Opportunity.org_id == user.org_id,
            Opportunity.deleted_at.is_(None),
            Opportunity.status == "open",
        )
        .group_by(Opportunity.pipeline_id, Opportunity.stage_id, cur)
    )
    known_stages = {s.id for s in stages}
    known_pipelines = {p.id for p in pipelines}
    by_stage: dict = {}
    unstaged: dict = {}  # pipeline id -> open deals with a pipeline but no stage
    unassigned = _Bucket()  # neither (or pointing at something that's gone)
    seen_currencies: set[str] = set()
    for pid, sid, row_currency, count, value, weighted_x100 in agg:
        seen_currencies.add(row_currency)
        if sid is not None and sid in known_stages:
            bucket = by_stage.setdefault(sid, _Bucket())
        elif pid is not None and pid in known_pipelines:
            bucket = unstaged.setdefault(pid, _Bucket())
        else:
            bucket = unassigned
        bucket.add(row_currency, count, value, float(weighted_x100 or 0) / 100)

    out = []
    grand = _Totals(currency)
    for p in pipelines:
        stage_rows = []
        totals = _Totals(currency)
        for s in stages:
            if s.pipeline_id != p.id:
                continue
            bucket = by_stage.get(s.id, _Bucket())
            stage_rows.append(_stage_row(str(s.id), s.name, s.win_probability, bucket, currency))
            totals.add(bucket)
        loose = unstaged.get(p.id)
        if loose is not None and loose.count:
            row = _stage_row(
                f"unstaged:{p.id}", "No stage", _effective_probability(loose, currency),
                loose, currency,
            )
            row["unassigned"] = True
            stage_rows.append(row)
            totals.add(loose)
        grand.merge(totals)
        out.append(
            {"id": str(p.id), "name": p.name, "stages": stage_rows, "totals": totals.out()}
        )
    if unassigned.count:
        totals = _Totals(currency)
        totals.add(unassigned)
        grand.merge(totals)
        row = _stage_row(
            UNASSIGNED, "No pipeline or stage", _effective_probability(unassigned, currency),
            unassigned, currency,
        )
        row["unassigned"] = True
        out.append(
            {
                "id": UNASSIGNED,
                "name": "Unassigned",
                "unassigned": True,
                "stages": [row],
                "totals": totals.out(),
            }
        )
    return {
        "pipelines": out,
        "totals": grand.out(),
        "currency": currency,
        "mixed_currencies": bool(seen_currencies - {currency}),
    }


@router.get("/sales")
async def sales_report(
    range: str = "90d",
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Won/lost analysis. A win/loss is dated by closed_at — stamped when the
    deal left "open", untouched by later edits. (Rows closed before closed_at
    existed were backfilled by the migration; the close_date/updated_at
    fallback only covers a row some other writer closed without stamping.)

    Counts and rates cover every deal; money covers the org's dominant
    currency only, with the full per-currency picture in by_currency."""
    unit, since = _parse_range(range)
    cur = _currency_expr()
    q = select(
        Opportunity.status,
        Opportunity.value,
        cur,
        Opportunity.closed_at,
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
                Opportunity.closed_at >= since,
                and_(
                    Opportunity.closed_at.is_(None),
                    or_(
                        Opportunity.close_date >= since.date(),
                        and_(Opportunity.close_date.is_(None), Opportunity.updated_at >= since),
                    ),
                ),
            )
        )
    rows = (await db.execute(q)).all()
    currency = await _dominant_currency(db, user.org_id)

    today = utcnow().date()
    effective: list[tuple[date, str, float, str, date | None]] = []
    for status, value, row_currency, closed_at, close_date, created_at, updated_at in rows:
        eff = _to_date(closed_at) or close_date or _to_date(updated_at) or today
        effective.append((eff, status, float(value or 0), row_currency, _to_date(created_at)))

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
    by_currency: dict[str, dict] = {}
    close_days: list[int] = []
    for eff, status, value, row_currency, created in effective:
        b = index.get(_bucket_key(unit, eff))
        per = by_currency.setdefault(
            row_currency,
            {"won_count": 0, "won_value": 0.0, "lost_count": 0, "lost_value": 0.0},
        )
        prefix = "won" if status == "won" else "lost"
        per[f"{prefix}_count"] += 1
        per[f"{prefix}_value"] = round(per[f"{prefix}_value"] + value, 2)
        if status == "won":
            won_count += 1
            if created is not None:
                close_days.append(max(0, (eff - created).days))
        else:
            lost_count += 1
        if b is not None:
            series[b][f"{prefix}_count"] += 1
            if row_currency == currency:
                series[b][f"{prefix}_value"] = round(series[b][f"{prefix}_value"] + value, 2)

    mine = by_currency.get(currency, {})
    won_value = mine.get("won_value", 0.0)
    decided = won_count + lost_count
    summary = {
        "won_count": won_count,
        "won_value": won_value,
        "lost_count": lost_count,
        "lost_value": mine.get("lost_value", 0.0),
        "win_rate": round(won_count / decided, 4) if decided else None,
        # Average of the deals the money total covers, not of all wins.
        "avg_deal_size": round(won_value / mine["won_count"], 2)
        if mine.get("won_count")
        else None,
        "avg_days_to_close": round(sum(close_days) / len(close_days), 1)
        if close_days
        else None,
    }
    return {
        "range": range,
        "unit": unit,
        "summary": summary,
        "series": series,
        "currency": currency,
        "mixed_currencies": bool(set(by_currency) - {currency}),
        "by_currency": by_currency,
    }


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
    # An event with notes from several users is credited ONCE, to the author
    # of the earliest note — otherwise one call counts for everybody who
    # documented it and the Team remainder goes negative.
    phone_where = [PhoneEvent.org_id == user.org_id]
    if since is not None:
        phone_where.append(PhoneEvent.happened_at >= since)
    note_rows = await db.execute(
        select(Note.author_id, PhoneEvent.id, PhoneEvent.kind)
        .join(PhoneEvent, Note.phone_event_id == PhoneEvent.id)
        .where(Note.author_id.is_not(None), *phone_where)
        .order_by(Note.created_at, Note.id)
    )
    credited: dict[uuid.UUID, tuple[uuid.UUID, str]] = {}
    for author_id, event_id, kind in note_rows:
        credited.setdefault(event_id, (author_id, kind))
    calls: dict[uuid.UUID, int] = {}
    texts: dict[uuid.UUID, int] = {}
    attributed_by_kind = {"call": 0, "sms": 0}
    for author_id, kind in credited.values():
        target = calls if kind == "call" else texts
        target[author_id] = target.get(author_id, 0) + 1
        attributed_by_kind[kind] = attributed_by_kind.get(kind, 0) + 1
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
    # Each event is attributed at most once, so the remainder can't go negative.
    team_calls = totals_by_kind.get("call", 0) - attributed_by_kind.get("call", 0)
    team_texts = totals_by_kind.get("sms", 0) - attributed_by_kind.get("sms", 0)

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
    """Lead funnel. Two different questions, kept apart:

    - FLOW: how many leads arrived in the period (new_leads) and how many
      conversions happened in it (converted / converted_in_period) — the
      latter includes leads that arrived long before.
    - COHORT: of the leads that ARRIVED in the period, how many have
      converted by now (cohort_converted). conversion_rate is this cohort
      ratio, so it can never exceed 100%; dividing the two flow counts (the
      old behaviour) could, and did."""
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
    cohort_by_source = {
        k: v
        for k, v in (
            await db.execute(
                select(Lead.source, func.count())
                .where(*new_where, Lead.converted_at.is_not(None))
                .group_by(Lead.source)
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
    cohort_converted = sum(cohort_by_source.values())
    by_source = []
    for source in sorted(
        set(new_by_source) | set(conv_by_source),
        key=lambda s: -(new_by_source.get(s, 0) + conv_by_source.get(s, 0)),
    ):
        n = new_by_source.get(source, 0)
        c = conv_by_source.get(source, 0)
        cohort = cohort_by_source.get(source, 0)
        by_source.append(
            {
                "source": source or "No source",
                "new_count": n,
                "converted_count": c,  # conversions that happened in the period
                "cohort_converted": cohort,  # of new_count, converted so far
                "conversion_rate": round(cohort / n, 4) if n else None,
            }
        )

    return {
        "range": range,
        "unit": unit,
        "summary": {
            "new_leads": new_leads,
            "converted": converted,
            "converted_in_period": converted,  # same number, named for what it is
            "cohort_converted": cohort_converted,
            "conversion_rate": round(cohort_converted / new_leads, 4) if new_leads else None,
            "avg_days_to_convert": round(sum(convert_days) / len(convert_days), 1)
            if convert_days
            else None,
        },
        "series": series,
        "by_source": by_source,
    }
