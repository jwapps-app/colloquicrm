"""Public lead-capture pages: GET/POST /f/{slug}, no auth by construction —
this router carries no auth dependencies and is mounted before the SPA
catch-all. Pages are minimal server-rendered HTML (no JS): a plain
<form method="post"> that redirects to ?ok=1 on success (Post/Redirect/Get,
so refresh never resubmits).

Abuse controls, mirroring the login throttle's shape:
- hidden honeypot field named "website": any value -> pretend success, store
  nothing;
- per-IP in-memory rate limit, 10 submissions / 15 min, bounded memory;
- duplicate softening: a resubmission with an email that matches an open
  (non-converted) lead appends a note-line to that lead instead of creating
  a duplicate.

Embedding: these pages intentionally keep the app-wide X-Frame-Options DENY
header — no iframe support. External sites share the link or paste the plain
HTML snippet (Settings -> Forms) that posts here directly.
"""

import html
import re
import time
import uuid
from collections import defaultdict
from datetime import date

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import get_db
from app.models import Lead, LeadForm, utcnow
from app.routes.auth import _client_ip
from app.routes.forms import DEFAULT_SUCCESS, FORM_FIELD_DEFS
from app.services.common import log_activity

router = APIRouter()

# In-process throttle, single instance by design (same trade-off as the login
# throttle). Counts every POST to any form from one address.
_SUBMIT_WINDOW_SECONDS = 900
_SUBMIT_LIMIT = 10
_submissions: dict[str, list[float]] = defaultdict(list)

# Absolute per-form ceiling: how many accepted submissions one form takes in a
# single UTC day, regardless of source IP. In-memory like the rate limiter
# (resets on restart) — a blunt DoS cap, not an audit counter.
_form_day_counts: dict[uuid.UUID, tuple[date, int]] = {}

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _rate_limited(request: Request) -> bool:
    key = f"ip:{_client_ip(request)}"
    now = time.monotonic()
    recent = [t for t in _submissions[key] if now - t < _SUBMIT_WINDOW_SECONDS]
    if len(recent) >= _SUBMIT_LIMIT:
        _submissions[key] = recent
        return True
    recent.append(now)
    _submissions[key] = recent
    if len(_submissions) > 10_000:  # bound memory under address-spraying
        _submissions.clear()
    return False


def _body_too_large(request: Request) -> bool:
    # Reject before request.form() ever reads the stream: a lead form is a
    # handful of short text fields, so anything over the byte bound is abuse.
    raw = request.headers.get("content-length")
    if raw is None:
        return False
    try:
        return int(raw) > settings.form_max_body_bytes
    except ValueError:
        return True


def _form_cap_reached(form: LeadForm) -> bool:
    today = utcnow().date()
    day, count = _form_day_counts.get(form.id, (today, 0))
    if day != today:
        count = 0
    return count >= settings.form_daily_submission_cap


def _record_form_submission(form: LeadForm) -> None:
    today = utcnow().date()
    day, count = _form_day_counts.get(form.id, (today, 0))
    if day != today:
        count = 0
    if len(_form_day_counts) > 10_000:  # bound memory across many forms
        _form_day_counts.clear()
    _form_day_counts[form.id] = (today, count + 1)


# ---- rendering ----

_CSS = """
:root{--accent:#7c3aed;--accent-dark:#6d28d9}
*{box-sizing:border-box}
body{margin:0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
  background:#f6f5fa;color:#1f2430;-webkit-font-smoothing:antialiased}
.wrap{max-width:520px;margin:0 auto;padding:32px 16px}
.card{background:#fff;border:1px solid #e6e3ef;border-radius:12px;padding:28px;
  box-shadow:0 1px 3px rgba(20,10,60,.06)}
.brand{color:var(--accent);font-weight:700;font-size:13px;letter-spacing:.06em;
  text-transform:uppercase;margin-bottom:6px}
h1{font-size:22px;margin:0 0 18px}
p{line-height:1.5}
label{display:block;margin:0 0 14px;font-size:14px;font-weight:600}
input,textarea{display:block;width:100%;margin-top:6px;padding:10px 12px;font:inherit;
  font-weight:400;border:1px solid #d5d0e2;border-radius:8px;background:#fff}
input:focus,textarea:focus{outline:2px solid var(--accent);outline-offset:1px}
button{width:100%;padding:12px;border:0;border-radius:8px;background:var(--accent);
  color:#fff;font:inherit;font-weight:600;cursor:pointer;margin-top:4px}
button:hover{background:var(--accent-dark)}
.error{background:#fdecec;color:#9f1d1d;border:1px solid #f5c2c2;border-radius:8px;
  padding:10px 12px;margin:0 0 16px;font-size:14px}
.center{text-align:center;padding:10px 0}
.mark{width:56px;height:56px;border-radius:50%;background:var(--accent);color:#fff;
  font-size:26px;line-height:56px;margin:0 auto 16px;text-align:center}
footer{text-align:center;color:#8b86a0;font-size:12px;margin-top:20px}
.hp{position:absolute;left:-9999px;top:-9999px;width:1px;height:1px;overflow:hidden}
"""


def _esc(value: str | None) -> str:
    return html.escape(value or "", quote=True)


def _page(title: str, body: str, status_code: int = 200) -> HTMLResponse:
    doc = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>{_esc(title)} — {_esc(settings.app_name)}</title>
<style>{_CSS}</style>
</head>
<body>
<main class="wrap">
<div class="card">
<div class="brand">{_esc(settings.app_name)}</div>
{body}
</div>
<footer>Powered by {_esc(settings.app_name)}</footer>
</main>
</body>
</html>"""
    return HTMLResponse(doc, status_code=status_code)


def _field_html(form: LeadForm, key: str, value: str) -> str:
    spec = FORM_FIELD_DEFS[key]
    required = key == "first_name" or (key == "email" and form.require_email)
    star = ' <span aria-hidden="true">*</span>' if required else ""
    req = " required" if required else ""
    if spec["widget"] == "textarea":
        control = (
            f'<textarea name="{key}" rows="4" maxlength="{spec["max"]}"{req}>'
            f"{_esc(value)}</textarea>"
        )
    else:
        control = (
            f'<input type="{spec["widget"]}" name="{key}" value="{_esc(value)}" '
            f'maxlength="{spec["max"]}"{req}>'
        )
    return f"<label>{_esc(spec['label'])}{star}{control}</label>"


def _form_page(
    form: LeadForm, values: dict | None = None, error: str = "", status_code: int = 200
) -> HTMLResponse:
    values = values or {}
    error_html = f'<div class="error">{_esc(error)}</div>' if error else ""
    fields = "\n".join(
        _field_html(form, key, values.get(key, ""))
        for key in (form.fields or [])
        if key in FORM_FIELD_DEFS
    )
    body = f"""<h1>{_esc(form.name)}</h1>
{error_html}
<form method="post" action="/f/{_esc(form.slug)}">
{fields}
<div class="hp" aria-hidden="true"><label>Website
<input type="text" name="website" value="" tabindex="-1" autocomplete="off"></label></div>
<button type="submit">Send</button>
</form>"""
    return _page(form.name, body, status_code=status_code)


def _success_page(form: LeadForm) -> HTMLResponse:
    message = form.success_message or DEFAULT_SUCCESS
    body = f"""<div class="center">
<div class="mark">✓</div>
<h1>Thank you</h1>
<p>{_esc(message)}</p>
</div>"""
    return _page(form.name, body)


def _not_found_page() -> HTMLResponse:
    body = """<h1>Form not available</h1>
<p>This form doesn't exist or is no longer accepting submissions.</p>"""
    return _page("Form not available", body, status_code=404)


def _throttled_page(form: LeadForm, status_code: int = 429) -> HTMLResponse:
    body = f"""<h1>{_esc(form.name)}</h1>
<p>You've sent quite a few submissions in a short time. Please wait a few
minutes and try again.</p>"""
    return _page(form.name, body, status_code=status_code)


def _too_large_page(form: LeadForm) -> HTMLResponse:
    body = f"""<h1>{_esc(form.name)}</h1>
<p>That submission was too large. Please shorten your message and try again.</p>"""
    return _page(form.name, body, status_code=413)


async def _lookup(db: AsyncSession, slug: str) -> LeadForm | None:
    return (
        await db.execute(
            select(LeadForm).where(LeadForm.slug == slug, LeadForm.enabled.is_(True))
        )
    ).scalar_one_or_none()


# ---- routes ----


@router.get("/f/{slug}", include_in_schema=False)
async def show_form(slug: str, request: Request, db: AsyncSession = Depends(get_db)):
    form = await _lookup(db, slug)
    if form is None:
        return _not_found_page()
    if request.query_params.get("ok"):
        return _success_page(form)
    return _form_page(form)


@router.post("/f/{slug}", include_in_schema=False)
async def submit_form(slug: str, request: Request, db: AsyncSession = Depends(get_db)):
    form = await _lookup(db, slug)
    if form is None:
        return _not_found_page()
    if _body_too_large(request):
        return _too_large_page(form)
    if _rate_limited(request):
        return _throttled_page(form)
    # Absolute daily ceiling for this form, on top of the per-IP limiter.
    if _form_cap_reached(form):
        return _throttled_page(form)

    data = await request.form()
    success = RedirectResponse(f"/f/{form.slug}?ok=1", status_code=303)

    # Honeypot: humans never see the field; a filled value is a bot. Pretend
    # success so the bot learns nothing, store nothing.
    if str(data.get("website") or "").strip():
        return success

    values = {
        key: str(data.get(key) or "").strip()
        for key in (form.fields or [])
        if key in FORM_FIELD_DEFS
    }

    errors: list[str] = []
    if not values.get("first_name"):
        errors.append("First name is required.")
    email = values.get("email", "")
    if form.require_email and not email:
        errors.append("Email is required.")
    elif email and not _EMAIL_RE.match(email):
        errors.append("Enter a valid email address.")
    for key, value in values.items():
        if len(value) > FORM_FIELD_DEFS[key]["max"]:
            errors.append(f"{FORM_FIELD_DEFS[key]['label']} is too long.")
    if errors:
        return _form_page(form, values=values, error=" ".join(errors), status_code=422)

    # Duplicate softening: an open lead with this email already exists ->
    # append a note-line to it instead of minting a duplicate record.
    existing = None
    if email:
        existing = (
            (
                await db.execute(
                    select(Lead)
                    .where(
                        Lead.org_id == form.org_id,
                        func.lower(Lead.email) == email.lower(),
                        Lead.converted_at.is_(None),
                        Lead.deleted_at.is_(None),
                    )
                    .order_by(Lead.created_at.desc())
                    .limit(1)
                )
            )
            .scalars()
            .first()
        )

    if existing is not None:
        stamp = utcnow().strftime("%Y-%m-%d %H:%M UTC")
        line = f"Form {form.name} resubmission {stamp}"
        if values.get("details"):
            line += f": {values['details']}"
        existing.details = f"{existing.details}\n{line}" if existing.details else line
        existing.updated_at = utcnow()
        await log_activity(
            db, form.org_id, "lead", existing.id, "updated", None,
            {"fields": ["details"], "form": form.name},
        )
    else:
        lead = Lead(
            org_id=form.org_id,
            status="New",
            source=form.source or form.name,
            **{key: (value or None) for key, value in values.items()},
        )
        db.add(lead)
        await db.flush()
        await log_activity(
            db, form.org_id, "lead", lead.id, "created", None, {"form": form.name}
        )

    # SQL-side increment: two concurrent submissions with a Python += would
    # each write stale-value+1 and lose a count.
    form.submission_count = LeadForm.submission_count + 1
    _record_form_submission(form)
    # Commit before the redirect lands: the visitor (or the admin watching the
    # Leads list) may look immediately.
    await db.commit()
    return success
