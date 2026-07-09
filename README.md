# colloquicrm

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

`POST /api/v1/imports/preview` (multipart CSV + type) parses both of Copper's
export shapes for people, leads, companies, and opportunities: the per-list
CSV export (named columns, repeated `Tag` columns) and the full data export
(`Email`/`Phone Number`/`Social`/`Website` value+type column pairs, one
comma-separated `Tags` column). `Name cf_NNNNNN` columns become real custom
fields either way. The preview flags likely duplicates (email, name+company,
domain) in the file and against existing records; `POST /imports/commit`
applies per-row create/skip/merge decisions and processes them as a background
job with a progress endpoint.

Copper's full export arrives as `.xlsx` — `scripts/copper_xlsx_to_csv.py`
(needs `openpyxl`) converts `people/leads/companies/opportunities.xlsx` from
`~/Downloads` into import-ready CSVs, dropping the hundreds of padded columns
the export includes.

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
start. CI builds and publishes `ghcr.io/jwapps-app/colloquicrm` on every push to
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
| `BACKUP_RETENTION_DAYS` | `14` (optional) |

Point the reverse proxy / tunnel hostname at `<NAS-IP>:<APP_PORT>`.

**Finish setup before exposing the hostname.** The first visitor to a fresh
instance gets the "create admin account" screen — do that yourself before
wiring up the public tunnel/proxy, or anyone who finds the URL first owns the
instance.

### All backend settings

Everything the app reads (from environment or `backend/.env`); the compose
file already sets the required ones.

| Variable | Default | Notes |
|---|---|---|
| `DATABASE_URL` | sqlite dev db | asyncpg format: `postgresql+asyncpg://user:pass@host:5432/db` |
| `SECRET_KEY` | dev placeholder | production refuses to boot with a weak/placeholder value |
| `ENVIRONMENT` | `development` | `production` arms the SECRET_KEY check |
| `APP_NAME` | `Colloqui CRM` | display name everywhere, incl. the TOTP issuer |
| `APP_URL` | `http://localhost:5173` | public URL; OAuth redirect base and chat links |
| `ALLOWED_ORIGINS` | localhost dev | comma-separated CORS allow-list |
| `SESSION_TTL_DAYS` | `30` | login session lifetime |
| `STATIC_DIR` | auto-detect | where the built frontend lives (baked into the image) |
| `GMAIL_BACKFILL_DAYS` | `0` | `0` = all history with known contacts; set a day count to bound it |
| `GMAIL_BACKFILL_LEADS` | `false` | backfill searches People only unless enabled — leads can mean thousands of extra Gmail queries |
| `GMAIL_ARCHIVE_BODIES` | `true` | store each synced email's full body so the CRM is a permanent archive; `false` fetches bodies lazily on first view only |
| `RINGCENTRAL_BACKFILL_DAYS` | `365` | call/SMS history window |

### Upgrades

Re-pull the image and restart the stack — migrations run automatically at
container start. Before upgrading, take a manual backup (below). Rolling back
means restoring that dump and pinning the previous image digest; a downgraded
app against an upgraded schema is not supported.

### Backups and restore

The `backup` sidecar writes a nightly `pg_dump -Fc` to
`/volume1/docker/colloquicrm/backups` (`app-YYYY-MM-DD.dump`), prunes past
`BACKUP_RETENTION_DAYS`, and touches `backups/last-success` on every good run —
if that file goes stale, backups are failing (see `backups/failures.log`).

Dumps contain **all CRM data plus live integration credentials** (Google
refresh tokens, RingCentral JWT, Colloqui API key) unencrypted — treat the
backups directory like the database itself.

Restore (into a scratch or fresh stack):

```
# stop the api container first so nothing writes
docker exec -i <postgres-container> dropdb -U app --if-exists app
docker exec -i <postgres-container> createdb -U app app
docker exec -i <postgres-container> pg_restore -U app -d app --no-owner < app-YYYY-MM-DD.dump
# start the api container; alembic sees the restored version table and is a no-op
```

Drill this once against a scratch stack before you need it for real.

### Account recovery

- Forgotten password: any admin → Settings → Users → **Reset password**.
- Lost authenticator: any admin → Settings → Users → **Reset 2FA**.
- **Sole admin locked out of 2FA** (no other admin to help): disable it
  directly in the database, then log in and re-enroll:

  ```
  docker exec -it <postgres-container> psql -U app -d app \
    -c "UPDATE users SET totp_enabled=false, totp_secret=NULL WHERE email='you@example.com';"
  ```
