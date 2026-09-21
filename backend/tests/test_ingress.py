"""Request-body ceilings and early authentication on the upload endpoints."""

import json
import os

from app.config import settings
from app.models import Attachment, LeadForm

from .conftest import add, make_person

_MB = 1024 * 1024


class CountingBody:
    """An async request body that records how much of it the server pulled."""

    def __init__(self, total: int, chunk: int = 64 * 1024, prefix: bytes = b""):
        self.total = total
        self.chunk = chunk
        self.prefix = prefix
        self.sent = 0

    async def __aiter__(self):
        if self.prefix:
            self.sent += len(self.prefix)
            yield self.prefix
        while self.sent < self.total:
            piece = b"x" * min(self.chunk, self.total - self.sent)
            self.sent += len(piece)
            yield piece


def _multipart_headers(length: int | None) -> dict:
    headers = {"Content-Type": "multipart/form-data; boundary=testboundary"}
    if length is not None:
        headers["Content-Length"] = str(length)
    return headers


async def test_unauthenticated_attachment_upload_is_refused_before_body_is_read(client):
    body = CountingBody(2 * _MB)
    resp = await client.post(
        "/api/v1/attachments", content=body, headers=_multipart_headers(2 * _MB)
    )
    assert resp.status_code == 401
    assert resp.json() == {"detail": "Not authenticated"}
    assert body.sent == 0


async def test_unauthenticated_import_preview_is_refused_before_body_is_read(client):
    body = CountingBody(2 * _MB)
    resp = await client.post(
        "/api/v1/imports/preview",
        content=body,
        headers={**_multipart_headers(2 * _MB), "Authorization": "Bearer not-a-session"},
    )
    assert resp.status_code == 401
    assert resp.json() == {"detail": "Invalid session"}
    assert body.sent == 0


async def test_chunked_oversized_public_form_submission_gets_413(client, world):
    form = await add(
        LeadForm(
            org_id=world.org.id,
            name="Contact",
            slug="contact-abc123",
            fields=["first_name", "last_name", "email", "details"],
        )
    )
    limit = settings.form_max_body_bytes
    # No Content-Length at all: the per-route header check has nothing to see.
    body = CountingBody(4 * limit, chunk=16 * 1024, prefix=b"first_name=A&details=")
    resp = await client.post(
        f"/f/{form.slug}",
        content=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert "content-length" not in resp.request.headers
    assert resp.status_code == 413
    # Cut off at the ceiling, not after the whole body was taken in.
    assert body.sent <= limit + 32 * 1024


async def test_chunked_oversized_import_commit_gets_413(client, world):
    from app.routes.imports import MAX_COMMIT_BYTES

    prefix = json.dumps({"type": "people", "rows": []})[:-2].encode()  # unterminated on purpose
    body = CountingBody(MAX_COMMIT_BYTES + 2 * _MB, chunk=_MB, prefix=prefix)
    resp = await client.post(
        "/api/v1/imports/commit",
        content=body,
        headers={**world.member_auth, "Content-Type": "application/json"},
    )
    assert "content-length" not in resp.request.headers
    assert resp.status_code == 413
    assert resp.json() == {"detail": "Import payload too large (50MB max)"}
    assert body.sent <= MAX_COMMIT_BYTES + 2 * _MB - _MB  # stopped before the tail


async def test_authenticated_upload_returns_201_with_the_documented_shape(client, world):
    person = await make_person(world.org)
    resp = await client.post(
        "/api/v1/attachments",
        headers=world.member_auth,
        data={"entity_type": "person", "entity_id": str(person.id)},
        files={"file": ("notes.txt", b"hello attachment", "text/plain")},
    )
    assert resp.status_code == 201, resp.text
    out = resp.json()
    assert set(out) == {
        "id",
        "filename",
        "content_type",
        "size_bytes",
        "entity_type",
        "entity_id",
        "uploaded_by",
        "uploaded_by_name",
        "created_at",
    }
    assert out["filename"] == "notes.txt"
    assert out["content_type"] == "text/plain"
    assert out["size_bytes"] == len(b"hello attachment")
    assert out["entity_type"] == "person"
    assert out["entity_id"] == str(person.id)
    assert out["uploaded_by"] == str(world.member.id)
    assert out["uploaded_by_name"] == "Max Member"
    assert len(os.listdir(settings.attachments_dir)) == 1


async def test_oversized_file_gets_413_and_leaves_nothing_behind(client, world, monkeypatch):
    monkeypatch.setattr(settings, "attachment_max_mb", 1)
    before = set(os.listdir(settings.attachments_dir)) if os.path.isdir(
        settings.attachments_dir
    ) else set()
    person = await make_person(world.org)
    resp = await client.post(
        "/api/v1/attachments",
        headers=world.member_auth,
        data={"entity_type": "person", "entity_id": str(person.id)},
        files={"file": ("big.bin", b"\0" * (_MB + _MB // 2), "application/octet-stream")},
    )
    assert resp.status_code == 413
    assert resp.json() == {"detail": "File exceeds the 1 MB limit"}
    after = set(os.listdir(settings.attachments_dir)) if os.path.isdir(
        settings.attachments_dir
    ) else set()
    assert after == before


async def test_upload_past_the_org_quota_gets_507(client, world, monkeypatch):
    monkeypatch.setattr(settings, "attachment_quota_mb", 1)
    person = await make_person(world.org)
    await add(
        Attachment(
            org_id=world.org.id,
            entity_type="person",
            entity_id=person.id,
            filename="existing.bin",
            content_type="application/octet-stream",
            size_bytes=_MB - 10,
            stored_name="0" * 32,
        )
    )
    resp = await client.post(
        "/api/v1/attachments",
        headers=world.member_auth,
        data={"entity_type": "person", "entity_id": str(person.id)},
        files={"file": ("small.txt", b"y" * 100, "text/plain")},
    )
    assert resp.status_code == 507
    assert resp.json() == {"detail": "Attachment storage is full (limit 1 MB)"}
