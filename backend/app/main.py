import asyncio
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select, text

from app.config import settings
from app.db import Base, SessionLocal, engine
from app.models import Org, Pipeline, Stage
from app.routes import (
    activities,
    auth,
    colloqui as colloqui_routes,
    companies,
    google as google_routes,
    imports,
    leads,
    meta,
    notes,
    opportunities,
    people,
    pipelines,
    tasks,
    users,
)
from app.services.colloqui import reminder_loop
from app.services.google import sync_loop as google_sync_loop

DEFAULT_PIPELINES = [
    ("Sales", [("Lead", 10), ("Qualified", 25), ("Proposal", 50), ("Negotiation", 75)]),
    ("Business Development", [("Prospecting", 10), ("Discussion", 30), ("Agreement", 70)]),
]


def _require_strong_secret() -> None:
    if settings.environment == "production" and (
        len(settings.secret_key) < 32 or "change-me" in settings.secret_key
    ):
        raise RuntimeError("SECRET_KEY is weak or a placeholder; refusing to start in production")


async def _seed() -> None:
    async with SessionLocal() as db:
        org = (await db.execute(select(Org))).scalars().first()
        if org is None:
            org = Org(name="Default")
            db.add(org)
            await db.flush()
        has_pipelines = (await db.execute(select(Pipeline))).scalars().first()
        if has_pipelines is None:
            for pos, (name, stages) in enumerate(DEFAULT_PIPELINES):
                p = Pipeline(org_id=org.id, name=name, position=pos)
                db.add(p)
                await db.flush()
                for spos, (sname, prob) in enumerate(stages):
                    db.add(
                        Stage(pipeline_id=p.id, name=sname, position=spos, win_probability=prob)
                    )
        await db.commit()


@asynccontextmanager
async def lifespan(app: FastAPI):
    _require_strong_secret()
    if settings.database_url.startswith("sqlite"):
        # Dev convenience only; real deployments run `alembic upgrade head`
        # at container start so schema state is tracked.
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    await _seed()
    background = [
        asyncio.create_task(reminder_loop()),
        asyncio.create_task(google_sync_loop()),
    ]
    yield
    for task in background:
        task.cancel()
    for task in background:
        with suppress(asyncio.CancelledError):
            await task
    await engine.dispose()


app = FastAPI(title=settings.app_name, lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health")
async def health():
    try:
        async with SessionLocal() as db:
            await db.execute(text("SELECT 1"))
    except Exception:
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=503, content={"status": "degraded", "database": "down"})
    return {"status": "ok"}


API = "/api/v1"
app.include_router(auth.router, prefix=f"{API}/auth", tags=["auth"])
app.include_router(users.router, prefix=f"{API}/users", tags=["users"])
app.include_router(companies.router, prefix=f"{API}/companies", tags=["companies"])
app.include_router(people.router, prefix=f"{API}/people", tags=["people"])
app.include_router(leads.router, prefix=f"{API}/leads", tags=["leads"])
app.include_router(opportunities.router, prefix=f"{API}/opportunities", tags=["opportunities"])
app.include_router(pipelines.router, prefix=f"{API}/pipelines", tags=["pipelines"])
app.include_router(tasks.router, prefix=f"{API}/tasks", tags=["tasks"])
app.include_router(notes.router, prefix=f"{API}/notes", tags=["notes"])
app.include_router(activities.router, prefix=f"{API}/activities", tags=["activities"])
app.include_router(meta.tags_router, prefix=f"{API}/tags", tags=["tags"])
app.include_router(meta.custom_fields_router, prefix=f"{API}/custom-fields", tags=["custom-fields"])
app.include_router(meta.saved_filters_router, prefix=f"{API}/saved-filters", tags=["saved-filters"])
app.include_router(imports.router, prefix=f"{API}/imports", tags=["imports"])
app.include_router(
    colloqui_routes.router, prefix=f"{API}/integrations/colloqui", tags=["integrations"]
)
app.include_router(
    google_routes.router, prefix=f"{API}/integrations/google", tags=["integrations"]
)
app.include_router(
    google_routes.calendar_router, prefix=f"{API}/calendar-events", tags=["integrations"]
)
app.include_router(google_routes.emails_router, prefix=f"{API}/emails", tags=["integrations"])

# Serve the built frontend when present (single-process deployment). API routes
# above always win; anything else falls back to the SPA's index.html.
_dist = (
    Path(settings.static_dir)
    if settings.static_dir
    else Path(__file__).resolve().parent.parent.parent / "frontend" / "dist"
)
if _dist.is_dir():
    app.mount("/assets", StaticFiles(directory=_dist / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    async def spa(path: str):
        candidate = (_dist / path).resolve()
        if path and candidate.is_file() and candidate.is_relative_to(_dist):
            return FileResponse(candidate)
        return FileResponse(_dist / "index.html")
