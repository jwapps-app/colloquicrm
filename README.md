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

## Running the tests

The backend suite needs a real PostgreSQL (the app uses Postgres-only SQL).
It creates and migrates its own `crm_test` database, and never talks to the
network — push, chat, Google and RingCentral are all faked.

```
docker run -d --rm --name crm-test-pg -e POSTGRES_PASSWORD=test -p 5494:5432 postgres:16
cd backend
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt
.venv/bin/python -m pytest
docker stop crm-test-pg
```

Point it at another server with
`TEST_DATABASE_URL=postgresql+asyncpg://user:pass@host:port/dbname` — that
database is dropped and recreated on every run.

`requirements.txt` is the hand-edited list of what the app needs;
`requirements.lock` pins the exact versions the suite was last run against,
with sha256 hashes, and is what the image installs
(`pip install --require-hashes -r requirements.lock`). Don't edit the lock by
hand — regenerate it with pip-tools (`pip-compile --generate-hashes`, the exact
command is in the comment at the top of the file), re-run the suite, commit
both files.

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
  attendee email/domain. With the Gmail scope granted, mail to and from known
  contacts is archived too (nothing else is stored).

**Gmail backfill.** A newly connected mailbox is searched for history with
every known address (People; Leads too with `GMAIL_BACKFILL_LEADS`), in sorted
order, ten addresses per query, falling back to one at a time if a combined
search fails. The position — last address finished, plus the page inside the
current search — is saved after every page, so a restart resumes where it
stopped. A pass lists at most 4,000 message ids and then *pauses*: nothing past
the budget is dropped, the next pass (a minute later while history remains,
otherwise every 30 minutes) carries on. A message that can't be fetched goes to
a retry queue and is tried first on each pass; an address whose search fails
holds the walk at that address. Either is given up after 5 attempts and stepped
over (a manual re-sync gives them a fresh chance). The backfill is "complete"
only when every address has been searched to its last page; after that the
Gmail history feed brings in new mail. `GMAIL_BACKFILL_DAYS=0` (the default)
means all history; a day count bounds the search. Reconnecting with a
*different* Google account resets all of this — history id, position, retry
queue — and backfills the new mailbox from the start. Mail already archived
from the old one stays; with `GMAIL_ARCHIVE_BODIES=false`, the full text of
those older messages can no longer be fetched (the API answers 410).

## Deployment

One image serves API + web app; `alembic upgrade head` runs at container
start. CI builds and publishes `ghcr.io/jwapps-app/colloquicrm` on every push to
main. The image is public — pull it without authentication, or build from this
repo with the included `Dockerfile`.

NAS/Portainer: use `docker-compose.portainer.yml` (postgres + api + nightly
pg_dump sidecar with retention). Create `/volume1/docker/colloquicrm/{postgres,backups,attachments}`
first, then set the stack env:

| Variable | Example |
|---|---|
| `POSTGRES_PASSWORD` | strong random, **URL-safe** — letters, digits, `-` `_` `.` `~` (e.g. `openssl rand -hex 24`). Compose pastes it straight into `DATABASE_URL`, where `@ : / ? # %` would be read as URL syntax |
| `SECRET_KEY` | `openssl rand -hex 32` |
| `APP_URL` | `https://crm.example.com` |
| `APP_PORT` | `3310` (host port) |
| `APP_BIND` | host address the port is published on; default `0.0.0.0` (all interfaces). `127.0.0.1` when a proxy on the same host is the only way in (optional) |
| `TRUSTED_PROXY_IPS` | default `172.16.0.0/12`; narrow to your proxy's address, e.g. `172.20.0.5/32` (optional — see below) |
| `APP_NAME` | display name |
| `BACKUP_RETENTION_DAYS` | `14` (optional) |

Point the reverse proxy / tunnel hostname at `<NAS-IP>:<APP_PORT>`.

**Behind a proxy or tunnel.** The login and public-form rate limits are keyed
by client address. A proxy in front of the API (cloudflared, nginx, …) hides
that address; the app reads the real one from `cf-connecting-ip` only when the
immediate peer is inside `TRUSTED_PROXY_IPS`, since anyone else could set the
header themselves and dodge the throttle. The Portainer compose sets it to
docker's `172.16.0.0/12` so a cloudflared container on the same host is
trusted — adjust it to your proxy's network if it runs elsewhere. Without it
every user arriving through the tunnel shares one throttle bucket (a burst of
bad logins from one visitor locks the address for everyone). Keep the set as
narrow as you can: docker's published port makes *every* direct connection
look like it came from the bridge gateway, so if the port is reachable
without the proxy, direct clients fall inside that range too.

Put plainly: **a published port plus broad proxy trust lets a direct caller
spoof their client address.** Anyone who can reach `<NAS-IP>:<APP_PORT>`
without going through the proxy can send their own `cf-connecting-ip`, pick a
fresh "address" per request, and walk around the per-IP login and form
throttles (the per-email login lock still holds). The defaults stay permissive
so existing stacks keep working; close the gap on one side or the other:

- **Proxy on the same host** (cloudflared/nginx container or service on the
  NAS): set `APP_BIND=127.0.0.1` so the port is published on loopback only and
  the proxy is the sole way in. Point the proxy at `127.0.0.1:<APP_PORT>` (or
  put both on one docker network and address the `api` service directly).
- **Either way**, narrow `TRUSTED_PROXY_IPS` from the whole docker range to
  the specific proxy address — the cloudflared container's IP as a `/32`, or
  your reverse proxy's LAN address. A spoofed header from any other peer is
  then ignored.

**Finish setup before exposing the hostname.** The first visitor to a fresh
instance gets the "create admin account" screen — do that yourself before
wiring up the public tunnel/proxy, or anyone who finds the URL first owns the
instance.

### All backend settings

Everything the app reads (from environment or `backend/.env`); the compose
file already sets the required ones.

| Variable | Default | Notes |
|---|---|---|
| `DATABASE_URL` | local Postgres (`postgresql+asyncpg://app:app@localhost:5432/app`) | asyncpg format: `postgresql+asyncpg://user:pass@host:5432/db`. Percent-encode reserved characters in the password (`@` → `%40`, `:` → `%3A`, `/` → `%2F`, `%` → `%25`). For a no-services dev run use SQLite: `sqlite+aiosqlite:///./dev.db` |
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
| `SECRET_ENCRYPTION_KEY` | derived from `SECRET_KEY` | Fernet key encrypting integration tokens + TOTP secrets at rest. Set it explicitly to keep it independent of `SECRET_KEY` — rotating `SECRET_KEY` otherwise orphans the encrypted secrets. Generate: `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` |
| `PUSH_RELAY_URL` | unset | push-relay base URL for the iOS companion app. Unset = push off; task reminders go to Colloqui chat only |
| `PUSH_RELAY_API_KEY` | unset | relay key scoped to the app's bundle id |
| `APNS_TOPIC` | companion-app bundle id | topic the relay routes push on |
| `TRUSTED_PROXY_IPS` | `127.0.0.1/32,::1/128` | proxy IPs/CIDRs trusted to set `cf-connecting-ip`; any other peer is keyed by its own address for rate limiting. Behind cloudflared/a reverse proxy set this to the proxy's network (the compose files use `172.16.0.0/12`) — see "Behind a proxy" above |
| `TASK_REMINDER_LEAD_MINUTES` | `15` | default reminder lead before a task's due time when no explicit reminder is set |
| `ATTACHMENTS_DIR` | `./data/attachments` | where uploaded file attachments are stored on disk; both compose files mount a volume at `/data/attachments` and point this there |
| `ATTACHMENT_MAX_MB` | `25` | per-file upload size cap; larger uploads are rejected with 413. If a proxy in front has its own body limit, keep it at or above this (+1 MB of multipart framing) |
| `ATTACHMENT_QUOTA_MB` | `5120` | whole-org attachment budget (sum of stored files); an upload that would exceed it is rejected with 507. Uploads are also limited to 60 per user per hour |
| `FORM_DAILY_SUBMISSION_CAP` | `500` | max public lead-form submissions per form per UTC day |
| `FORM_MAX_BODY_BYTES` | `65536` | max public lead-form request body, counted on the bytes actually received (chunked requests included) |

### Upgrades

The server runs as an unprivileged user (uid/gid `10001`), not root. The
container still *starts* as root so the entrypoint can hand the attachments
directory to that user, then drops privileges before the migrations and the
server run. Upgrading from an image that ran as root needs nothing from you:
the first start chowns the existing attachments (one log line), and they show
up as owner `10001` on the host afterwards. If the storage refuses — a
filesystem or ACL that rejects chown, a read-only mount — the container logs
`entrypoint: WARNING: … running as root, as before` and does exactly that;
it never refuses to start over file ownership. Seeing that warning on every
start means the hardening isn't in effect: `chown -R 10001:10001` the directory
on the host. Don't add `user:` to the stack unless the directory is already
owned by that uid — a container started non-root can't fix ownership and just
runs as it is.

Re-pull the image and restart the stack — migrations run automatically at
container start. Before upgrading, take a manual backup (below). Rolling back
means restoring that dump and pinning the previous image digest; a downgraded
app against an upgraded schema is not supported.

### Backups and restore

The `backup` sidecar writes a nightly `pg_dump -Fc` to
`/volume1/docker/colloquicrm/backups` (`app-YYYY-MM-DD.dump`), prunes past
`BACKUP_RETENTION_DAYS`, and touches `backups/last-success` on every good run —
if that file goes stale, backups are failing (see `backups/failures.log`).
Each dump is written under a temporary name and renamed into place only when
`pg_dump` succeeds, so a failed re-run never clobbers that day's good dump.
**The sidecar backs up the database only** — uploaded files live in the
attachments directory and need their own copy job (below).

Dumps contain **all CRM data**. Integration credentials (Google refresh
tokens, RingCentral JWT, Colloqui API key) and TOTP secrets are stored
**encrypted at rest** (see `SECRET_ENCRYPTION_KEY`), so those fields in a dump
are ciphertext — a restore only decrypts them if `SECRET_ENCRYPTION_KEY` (or
the `SECRET_KEY` it was derived from) matches the source instance. Everything
else in the dump is plaintext, so still treat the backups directory like the
database itself.

File attachments are **not** in the database — only their metadata is. The
bytes live in the attachments volume (`/volume1/docker/colloquicrm/attachments`
on the NAS stack), so include that directory in backups alongside the dumps; a
restore without it brings back attachment rows whose files are gone. Copy it
on the same schedule as the dumps (NAS snapshot/Hyper Backup, `rsync`, …), and
restore both from the same night: a stray file with no matching row (a crash
mid-upload) is deleted by the app's daily cleanup once it is over an hour old.
The cleanup stands down when most files have no row — a restore in progress —
but restore the database before starting the api container all the same.

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
