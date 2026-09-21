FROM node:22-alpine AS web
WORKDIR /web
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.13-slim
WORKDIR /srv

# Install the pinned, tested dependency set — requirements.txt stays the
# human-edited source of truth; the lock is what production actually runs.
# --require-hashes: every download must match a sha256 recorded in the lock.
COPY backend/requirements.lock .
RUN pip install --no-cache-dir --require-hashes -r requirements.lock

# The server runs as this user, not root. The uid is fixed because it ends up
# as the owner of files on bind mounts, where only the number is visible. The
# container still STARTS as root (no USER line, on purpose): entrypoint.sh
# hands the attachments directory over and then drops to this user — or stays
# root, loudly, when the storage won't allow that. Both attachment locations
# (the ATTACHMENTS_DIR default and the path the compose files use) exist
# app-owned up front, so a fresh named volume inherits the right owner.
RUN groupadd --system --gid 10001 app \
 && useradd --system --uid 10001 --gid app --home-dir /srv --no-create-home --shell /usr/sbin/nologin app \
 && mkdir -p /srv/data/attachments /data/attachments \
 && chown -R app:app /srv/data /data

COPY backend/app ./app
COPY backend/alembic ./alembic
COPY backend/alembic.ini backend/entrypoint.sh ./
# Bytecode is compiled now, as root: the app user can't write __pycache__.
RUN chmod +x entrypoint.sh && python -m compileall -q app alembic

COPY --from=web /web/dist ./frontend-dist
ENV STATIC_DIR=/srv/frontend-dist

# Stamped by CI so /api/health can prove which commit is running.
ARG GIT_SHA=unknown
ENV GIT_SHA=${GIT_SHA}

EXPOSE 8000
CMD ["./entrypoint.sh"]
