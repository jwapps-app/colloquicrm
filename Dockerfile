FROM node:22-alpine AS web
WORKDIR /web
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.13-slim
WORKDIR /srv

COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/app ./app
COPY backend/alembic ./alembic
COPY backend/alembic.ini backend/entrypoint.sh ./
RUN chmod +x entrypoint.sh

COPY --from=web /web/dist ./frontend-dist
ENV STATIC_DIR=/srv/frontend-dist

# Stamped by CI so /api/health can prove which commit is running.
ARG GIT_SHA=unknown
ENV GIT_SHA=${GIT_SHA}

EXPOSE 8000
CMD ["./entrypoint.sh"]
