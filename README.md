# crm-app

Self-hosted CRM. Companies, people, leads, opportunities across multiple
pipelines, tasks, notes, tags, custom fields, saved filters, and CSV import
compatible with Copper's export format. FastAPI + Postgres backend, React PWA
frontend.

The display name is config (`APP_NAME`), not code — identifiers stay generic.

## Layout

- `backend/` — FastAPI app (async SQLAlchemy, argon2 sessions, TOTP)
- `frontend/` — Vite + React single-page app, installable as a PWA

## Quick start (dev)

Backend (SQLite, no services needed):

```
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
DATABASE_URL="sqlite+aiosqlite:///./dev.db" .venv/bin/uvicorn app.main:app --port 8010
```

Frontend:

```
cd frontend
npm install
npm run dev        # http://localhost:5173, proxies /api to :8010
```

First visit walks through creating the initial admin account.

Postgres via compose instead: `docker compose up` (API on :8010).

## API

Everything lives under `/api/v1`; interactive docs at `/docs`. Auth is a
bearer session token from `POST /api/v1/auth/login` (TOTP two-step when
enabled). `/api/health` is the readiness check.

## Import

`POST /api/v1/imports/preview` (multipart CSV + type) parses Copper-format
exports for people, leads, companies, and opportunities — including repeated
`Tag` columns and `Name cf_NNNNNN` custom-field columns, which become real
custom fields. The preview flags likely duplicates (email, name+company,
domain) in the file and against existing records; `POST /imports/commit`
applies per-row create/skip/merge decisions.

## Integrations

- **Colloqui** (chat): connect from Settings → Integrations with a service-user
  API key — or paste an admin key and the CRM provisions the service user,
  space, and #tasks channel itself. Task assignments post to #tasks; due tasks
  DM the assignee's linked account.
- **Google Workspace**: the org registers its own OAuth client (setup guide in
  Settings); users connect their accounts for read-only Contacts import and
  Calendar sync. Events surface on People/Leads/Companies detail pages by
  attendee email/domain.

## Deployment

One image serves API + web app; `alembic upgrade head` runs at container
start. CI builds and publishes `ghcr.io/jwapps-app/crm-app` on every push to
main.

NAS/Portainer: use `docker-compose.portainer.yml` (postgres + api + nightly
pg_dump sidecar with retention). Create `/volume1/docker/colloquicrm/{postgres,backups}`
first, then set the stack env:

| Variable | Example |
|---|---|
| `POSTGRES_PASSWORD` | strong random |
| `SECRET_KEY` | `openssl rand -hex 32` |
| `APP_URL` | `https://crm.example.com` |
| `APP_PORT` | `3310` (host port) |
| `APP_NAME` | display name |

Point the reverse proxy / tunnel hostname at `<NAS-IP>:<APP_PORT>`.
