"""Google Contacts: fetch a user's contacts and map them onto import rows."""

import logging

from app.config import settings
from app.services.google.client import _get_json

log = logging.getLogger("google")


async def fetch_contacts(access_token: str) -> list[dict]:
    """All of the user's Google contacts, paginated."""
    out: list[dict] = []
    page_token = None
    while True:
        params = {
            "personFields": "names,emailAddresses,phoneNumbers,organizations,urls,addresses",
            "pageSize": 200,
        }
        if page_token:
            params["pageToken"] = page_token
        data = await _get_json(
            f"{settings.google_people_base}/v1/people/me/connections", access_token, params
        )
        out.extend(data.get("connections", []))
        page_token = data.get("nextPageToken")
        if not page_token:
            return out


def contact_to_row(contact: dict) -> dict | None:
    """Map a People API person to the same row shape the CSV importer uses,
    so preview/dedupe/commit are shared."""
    names = (contact.get("names") or [{}])[0]
    emails = contact.get("emailAddresses") or []
    phones = contact.get("phoneNumbers") or []
    orgs = (contact.get("organizations") or [{}])[0]
    addresses = (contact.get("addresses") or [{}])[0]

    data: dict = {}
    if names.get("givenName"):
        data["first_name"] = names["givenName"]
    if names.get("familyName"):
        data["last_name"] = names["familyName"]
    if not data and not emails:
        return None  # nothing identifying

    for e in emails:
        kind = (e.get("type") or "").lower()
        key = "personal_email" if kind == "home" else "work_email"
        data.setdefault(key, e.get("value"))
    for p in phones:
        kind = (p.get("type") or "").lower()
        key = "mobile_phone" if kind == "mobile" else "work_phone"
        data.setdefault(key, p.get("value"))
    if orgs.get("name"):
        data["company_name"] = orgs["name"]
    if orgs.get("title"):
        data["title"] = orgs["title"]
    if addresses.get("streetAddress"):
        data["street"] = addresses["streetAddress"]
    if addresses.get("city"):
        data["city"] = addresses["city"]
    if addresses.get("region"):
        data["state"] = addresses["region"]
    if addresses.get("postalCode"):
        data["postal_code"] = addresses["postalCode"]
    if addresses.get("country"):
        data["country"] = addresses["country"]

    return {"data": data, "tags": ["Google Contacts"], "custom_fields": {}}
