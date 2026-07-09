import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps import get_current_user
from app.models import Opportunity, Pipeline, Stage, User
from app.schemas import PipelineIn, PipelineUpdateIn, StageIn, StageUpdateIn

router = APIRouter()


def stage_out(s: Stage) -> dict:
    return {
        "id": str(s.id),
        "name": s.name,
        "position": s.position,
        "win_probability": s.win_probability,
    }


async def _get_pipeline(db: AsyncSession, user: User, pipeline_id: uuid.UUID) -> Pipeline:
    p = (
        await db.execute(
            select(Pipeline).where(Pipeline.id == pipeline_id, Pipeline.org_id == user.org_id)
        )
    ).scalar_one_or_none()
    if p is None:
        raise HTTPException(status_code=404, detail="Pipeline not found")
    return p


@router.get("")
async def list_pipelines(
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
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
    by_pipeline: dict[uuid.UUID, list[dict]] = {}
    for s in stages:
        by_pipeline.setdefault(s.pipeline_id, []).append(stage_out(s))
    return [
        {
            "id": str(p.id),
            "name": p.name,
            "position": p.position,
            "stages": by_pipeline.get(p.id, []),
        }
        for p in pipelines
    ]


@router.post("", status_code=201)
async def create_pipeline(
    body: PipelineIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    max_pos = (
        await db.execute(
            select(func.coalesce(func.max(Pipeline.position), -1)).where(
                Pipeline.org_id == user.org_id
            )
        )
    ).scalar_one()
    p = Pipeline(org_id=user.org_id, name=body.name, position=max_pos + 1)
    db.add(p)
    await db.flush()
    stages = []
    for i, s in enumerate(body.stages or []):
        stage = Stage(
            pipeline_id=p.id,
            name=s.name,
            position=s.position if s.position is not None else i,
            win_probability=s.win_probability,
        )
        db.add(stage)
        stages.append(stage)
    await db.flush()
    result = {
        "id": str(p.id),
        "name": p.name,
        "position": p.position,
        "stages": [stage_out(s) for s in stages],
    }
    await db.commit()  # visible before the client refetches
    return result


@router.patch("/{pipeline_id}")
async def update_pipeline(
    pipeline_id: uuid.UUID,
    body: PipelineUpdateIn,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    p = await _get_pipeline(db, user, pipeline_id)
    if body.name is not None:
        p.name = body.name
    if body.position is not None:
        p.position = body.position
    result = {"id": str(p.id), "name": p.name, "position": p.position}
    await db.commit()  # visible before the client refetches
    return result


@router.delete("/{pipeline_id}", status_code=204)
async def delete_pipeline(
    pipeline_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    p = await _get_pipeline(db, user, pipeline_id)
    in_use = (
        await db.execute(
            select(func.count()).select_from(Opportunity).where(
                Opportunity.pipeline_id == p.id, Opportunity.deleted_at.is_(None)
            )
        )
    ).scalar_one()
    if in_use:
        raise HTTPException(
            status_code=409, detail=f"Pipeline has {in_use} opportunities; move them first"
        )
    await db.delete(p)
    await db.commit()  # visible before the client refetches


@router.post("/{pipeline_id}/stages", status_code=201)
async def add_stage(
    pipeline_id: uuid.UUID,
    body: StageIn,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    p = await _get_pipeline(db, user, pipeline_id)
    max_pos = (
        await db.execute(
            select(func.coalesce(func.max(Stage.position), -1)).where(Stage.pipeline_id == p.id)
        )
    ).scalar_one()
    stage = Stage(
        pipeline_id=p.id,
        name=body.name,
        position=body.position if body.position is not None else max_pos + 1,
        win_probability=body.win_probability,
    )
    db.add(stage)
    await db.flush()
    result = stage_out(stage)
    await db.commit()  # visible before the client refetches
    return result


async def _get_stage(db: AsyncSession, user: User, stage_id: uuid.UUID) -> Stage:
    row = (
        await db.execute(
            select(Stage)
            .join(Pipeline, Pipeline.id == Stage.pipeline_id)
            .where(Stage.id == stage_id, Pipeline.org_id == user.org_id)
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Stage not found")
    return row


@router.patch("/stages/{stage_id}")
async def update_stage(
    stage_id: uuid.UUID,
    body: StageUpdateIn,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    s = await _get_stage(db, user, stage_id)
    if body.name is not None:
        s.name = body.name
    if body.win_probability is not None:
        s.win_probability = body.win_probability
    if body.position is not None:
        s.position = body.position
    result = stage_out(s)
    await db.commit()  # visible before the client refetches
    return result


@router.delete("/stages/{stage_id}", status_code=204)
async def delete_stage(
    stage_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    s = await _get_stage(db, user, stage_id)
    in_use = (
        await db.execute(
            select(func.count()).select_from(Opportunity).where(
                Opportunity.stage_id == s.id, Opportunity.deleted_at.is_(None)
            )
        )
    ).scalar_one()
    if in_use:
        raise HTTPException(
            status_code=409, detail=f"Stage has {in_use} opportunities; move them first"
        )
    await db.delete(s)
    await db.commit()  # visible before the client refetches
