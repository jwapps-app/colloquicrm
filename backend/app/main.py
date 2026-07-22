import asyncio
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select, text

from app.config import settings
from app.db import Base, SessionLocal, engine
from app.models import Org, Pipeline, Stage
from app.routes import (
    activities,
    attachments,
    auth,
    automations,
    duplicates,
    feed,
    ringcentral as ringcentral_routes,
    colloqui as colloqui_routes,
    companies,
    devices,
    forms,
    google as google_routes,
    imports,
    leads,
    meta,
    notes,
    opportunities,
    people,
    pipelines,
    public_forms,
    reports,
    suggestions,
    tasks,
    users,
)
from app.services.automations import automations_loop
from app.services.colloqui import reminder_loop
from app.services.google import sync_loop as google_sync_loop
from app.services.maintenance import purge_loop
from app.services.ringcentral import sync_loop as ringcentral_sync_loop

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
    from app.services.importer import resume_interrupted_imports

    await resume_interrupted_imports()
    background = [
        asyncio.create_task(reminder_loop()),
        asyncio.create_task(google_sync_loop()),
        asyncio.create_task(ringcentral_sync_loop()),
        asyncio.create_task(purge_loop()),
        asyncio.create_task(automations_loop()),
    ]
    yield
    for task in background:
        task.cancel()
    for task in background:
        with suppress(asyncio.CancelledError):
            await task
    await engine.dispose()


# Interactive docs and the OpenAPI schema are a dev convenience; in production
# they needlessly advertise the whole API surface, so turn them off.
_docs_kwargs = (
    {"docs_url": None, "redoc_url": None, "openapi_url": None}
    if settings.environment == "production"
    else {}
)
app = FastAPI(title=settings.app_name, lifespan=lifespan, **_docs_kwargs)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# CSP for the SPA + API. The app loads only its own hashed bundles ('self'),
# may carry inline styles, renders avatars/QR from data: and remote https
# images, and previews emails in a srcdoc iframe (frame-src 'self'). No
# framing of the app itself (frame-ancestors 'none').
_SPA_CSP = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: https:; "
    "font-src 'self' data:; "
    "connect-src 'self'; "
    "frame-src 'self'; "
    "base-uri 'self'; "
    "form-action 'self'; "
    "frame-ancestors 'none'"
)
# The public /f/{slug} lead pages are self-contained server-rendered HTML with
# one inline <style> and a form that posts to itself — nothing else is allowed.
_FORM_CSP = (
    "default-src 'none'; "
    "style-src 'unsafe-inline'; "
    "form-action 'self'; "
    "base-uri 'none'; "
    "frame-ancestors 'none'"
)


@app.middleware("http")
async def static_and_security_headers(request, call_next):
    response = await call_next(request)
    path = request.url.path
    if path.startswith("/assets/"):
        # Hashed filenames: cache forever, a new build means new names.
        response.headers.setdefault("Cache-Control", "public, max-age=31536000, immutable")
    elif not path.startswith("/api/"):
        # index.html and sw.js must always revalidate — a heuristically cached
        # shell keeps serving bundles from an old deploy.
        response.headers.setdefault("Cache-Control", "no-cache")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    if path.startswith("/f/"):
        response.headers.setdefault("Content-Security-Policy", _FORM_CSP)
    else:
        response.headers.setdefault("Content-Security-Policy", _SPA_CSP)
    return response


@app.get("/api/health")
async def health():
    import os

    version = os.environ.get("GIT_SHA", "unknown")[:12]
    try:
        async with SessionLocal() as db:
            await db.execute(text("SELECT 1"))
    except Exception:
        from fastapi.responses import JSONResponse

        return JSONResponse(
            status_code=503,
            content={"status": "degraded", "database": "down", "version": version},
        )
    return {"status": "ok", "version": version}


API = "/api/v1"
app.include_router(auth.router, prefix=f"{API}/auth", tags=["auth"])
app.include_router(users.router, prefix=f"{API}/users", tags=["users"])
app.include_router(devices.router, prefix=f"{API}/devices", tags=["devices"])
app.include_router(companies.router, prefix=f"{API}/companies", tags=["companies"])
app.include_router(people.router, prefix=f"{API}/people", tags=["people"])
app.include_router(leads.router, prefix=f"{API}/leads", tags=["leads"])
app.include_router(opportunities.router, prefix=f"{API}/opportunities", tags=["opportunities"])
app.include_router(pipelines.router, prefix=f"{API}/pipelines", tags=["pipelines"])
app.include_router(reports.router, prefix=f"{API}/reports", tags=["reports"])
app.include_router(tasks.router, prefix=f"{API}/tasks", tags=["tasks"])
app.include_router(notes.router, prefix=f"{API}/notes", tags=["notes"])
app.include_router(activities.router, prefix=f"{API}/activities", tags=["activities"])
app.include_router(attachments.router, prefix=f"{API}/attachments", tags=["attachments"])
app.include_router(duplicates.router, prefix=f"{API}/duplicates", tags=["duplicates"])
app.include_router(automations.router, prefix=f"{API}/automations", tags=["automations"])
app.include_router(forms.router, prefix=f"{API}/forms", tags=["forms"])
app.include_router(meta.tags_router, prefix=f"{API}/tags", tags=["tags"])
app.include_router(meta.options_router, prefix=f"{API}/options", tags=["options"])
app.include_router(meta.custom_fields_router, prefix=f"{API}/custom-fields", tags=["custom-fields"])
app.include_router(meta.saved_filters_router, prefix=f"{API}/saved-filters", tags=["saved-filters"])
app.include_router(imports.router, prefix=f"{API}/imports", tags=["imports"])
app.include_router(
    suggestions.router, prefix=f"{API}/contact-suggestions", tags=["suggestions"]
)
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
app.include_router(feed.router, prefix=f"{API}/feed", tags=["feed"])
app.include_router(
    ringcentral_routes.router, prefix=f"{API}/integrations/ringcentral", tags=["integrations"]
)
# Public lead-capture pages (/f/{slug}) — no auth by construction (the router
# has no auth dependencies). Must register before the SPA catch-all below so
# /f/ paths never fall through to index.html.
app.include_router(public_forms.router, tags=["public-forms"])

# Serve the built frontend when present (single-process deployment). API routes
# above always win; anything else falls back to the SPA's index.html.
_dist = (
    Path(settings.static_dir)
    if settings.static_dir
    else Path(__file__).resolve().parent.parent.parent / "frontend" / "dist"
)
if _dist.is_dir():
    app.mount("/assets", StaticFiles(directory=_dist / "assets"), name="assets")

    # In production the docs/schema routes are unregistered; without this they
    # would fall through to the SPA shell (a 200) instead of a clean 404.
    _DISABLED_DOCS = {"openapi.json", "docs", "redoc"}

    @app.get("/{path:path}", include_in_schema=False)
    async def spa(path: str):
        if path.startswith("api/") or path == "api":
            # Unknown API route: a JSON 404 beats silently serving the SPA.
            raise HTTPException(status_code=404, detail="Not found")
        if settings.environment == "production" and path in _DISABLED_DOCS:
            raise HTTPException(status_code=404, detail="Not found")
        candidate = (_dist / path).resolve()
        if path and candidate.is_file() and candidate.is_relative_to(_dist):
            return FileResponse(candidate)
        return FileResponse(_dist / "index.html")
