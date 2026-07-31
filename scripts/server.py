"""Private MCP bridge for one or more explicitly allowed iCloud calendars."""

from __future__ import annotations

import argparse
import base64
import hashlib
import html
import hmac
import json
import os
import secrets
import time
from datetime import date, datetime, time as datetime_time, timedelta
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlencode, urlparse
from urllib.request import Request as URLRequest
from urllib.request import urlopen
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import caldav
from caldav.elements import cdav, dav
from caldav.lib import error as caldav_error
from icalendar import Calendar as ICalendar
from icalendar import Event
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import AnyHttpUrl
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


PUBLIC_URL = os.getenv("PUBLIC_URL", "https://codex-icloud-calendar.onrender.com").rstrip("/")
MCP_RESOURCE = f"{PUBLIC_URL}/mcp"
RESOURCE_ALIASES = {PUBLIC_URL, MCP_RESOURCE}
OAUTH_SCOPE = "icloud-calendar"
_used_authorization_codes: set[str] = set()
_used_refresh_tokens: set[str] = set()


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _sign(payload: dict[str, Any]) -> str:
    body = _b64encode(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    signature = hmac.new(
        _required("ICLOUD_OAUTH_SIGNING_KEY").encode(), body.encode(), hashlib.sha256
    ).digest()
    return f"{body}.{_b64encode(signature)}"


def _unsign(value: str, expected_type: str) -> dict[str, Any] | None:
    try:
        body, supplied_signature = value.split(".", 1)
        expected_signature = hmac.new(
            _required("ICLOUD_OAUTH_SIGNING_KEY").encode(), body.encode(), hashlib.sha256
        ).digest()
        if not hmac.compare_digest(_b64decode(supplied_signature), expected_signature):
            return None
        payload = json.loads(_b64decode(body))
        if payload.get("typ") != expected_type:
            return None
        if payload.get("exp") is not None and int(payload["exp"]) < int(time.time()):
            return None
        return payload
    except (ValueError, TypeError, KeyError, json.JSONDecodeError):
        return None


def _valid_redirect_uri(value: str) -> bool:
    parsed = urlparse(value)
    if parsed.scheme == "https" and parsed.netloc:
        return True
    return parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost"}


def _client_payload(client_id: str) -> dict[str, Any] | None:
    return _unsign(client_id, "client")


def _oauth_error(error: str, description: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"error": error, "error_description": description}, status_code=status)


class SignedTokenVerifier(TokenVerifier):
    async def verify_token(self, token: str) -> AccessToken | None:
        payload = _unsign(token, "access")
        if not payload or payload.get("resource") not in RESOURCE_ALIASES:
            return None
        return AccessToken(
            token=token,
            client_id=str(payload["client_id"]),
            scopes=list(payload.get("scopes", [])),
            expires_at=int(payload["exp"]),
            resource=str(payload["resource"]),
            subject="icloud-owner",
            claims={"iss": PUBLIC_URL},
        )


def _allowed_names() -> set[str]:
    value = _required("ICLOUD_ALLOWED_CALENDARS")
    return {item.strip() for item in value.split(",") if item.strip()}


def _parse_datetime(value: str, timezone_name: str | None = None) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Date-times must include a timezone offset, for example -04:00.")
    if timezone_name:
        try:
            timezone = ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"Unknown IANA timezone: {timezone_name}") from exc
        localized = parsed.astimezone(timezone)
        if localized.utcoffset() != parsed.utcoffset():
            raise ValueError(
                f"The offset in {value} does not match {timezone_name} at that date."
            )
        return localized
    return parsed


def _http_json(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    payload: dict[str, Any] | None = None,
    form: dict[str, str] | None = None,
) -> dict[str, Any]:
    request_headers = {"Accept": "application/json", **(headers or {})}
    body: bytes | None = None
    if payload is not None:
        body = json.dumps(payload).encode()
        request_headers["Content-Type"] = "application/json"
    elif form is not None:
        body = urlencode(form).encode()
        request_headers["Content-Type"] = "application/x-www-form-urlencoded"
    request = URLRequest(url, data=body, headers=request_headers, method=method)
    try:
        with urlopen(request, timeout=30) as response:
            raw = response.read()
            return json.loads(raw) if raw else {}
    except HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")[:1000]
        raise RuntimeError(f"Remote API returned HTTP {exc.code}: {raw}") from exc
    except URLError as exc:
        raise RuntimeError(f"Remote API is unavailable: {exc.reason}") from exc


def _zoom_access_token() -> str:
    client_id = _required("ZOOM_CLIENT_ID")
    client_secret = _required("ZOOM_CLIENT_SECRET")
    credentials = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    query = urlencode(
        {
            "grant_type": "account_credentials",
            "account_id": _required("ZOOM_ACCOUNT_ID"),
        }
    )
    result = _http_json(
        "POST",
        f"https://zoom.us/oauth/token?{query}",
        headers={"Authorization": f"Basic {credentials}"},
    )
    token = str(result.get("access_token", ""))
    if not token:
        raise RuntimeError("Zoom did not return an access token.")
    return token


def _create_zoom_meeting(title: str, start: datetime, end: datetime, timezone: str) -> dict[str, Any]:
    duration = int((end - start).total_seconds() // 60)
    if duration <= 0:
        raise ValueError("end must be later than start")
    return _http_json(
        "POST",
        "https://api.zoom.us/v2/users/me/meetings",
        headers={"Authorization": f"Bearer {_zoom_access_token()}"},
        payload={
            "topic": title,
            "type": 2,
            "start_time": start.isoformat(),
            "duration": duration,
            "timezone": timezone,
            "settings": {"waiting_room": True, "join_before_host": False},
        },
    )


def _delete_zoom_meeting(meeting_id: str) -> None:
    _http_json(
        "DELETE",
        f"https://api.zoom.us/v2/meetings/{quote(meeting_id, safe='')}",
        headers={"Authorization": f"Bearer {_zoom_access_token()}"},
    )


def _google_access_token() -> str:
    result = _http_json(
        "POST",
        "https://oauth2.googleapis.com/token",
        form={
            "client_id": _required("GOOGLE_CLIENT_ID"),
            "client_secret": _required("GOOGLE_CLIENT_SECRET"),
            "refresh_token": _required("GOOGLE_REFRESH_TOKEN"),
            "grant_type": "refresh_token",
        },
    )
    token = str(result.get("access_token", ""))
    if not token:
        raise RuntimeError("Google did not return an access token.")
    return token


def _google_calendar_map() -> dict[str, str]:
    raw = os.getenv("GOOGLE_ALLOWED_CALENDARS", "").strip()
    if not raw:
        return {"info@pierrelescarbeau.com": "primary"}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError("GOOGLE_ALLOWED_CALENDARS must be a JSON object.") from exc
    if not isinstance(value, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in value.items()):
        raise RuntimeError("GOOGLE_ALLOWED_CALENDARS must map calendar names to IDs.")
    return value


def _create_google_event(
    calendar_name: str,
    title: str,
    start: datetime,
    end: datetime,
    timezone: str,
    description: str,
    location: str,
    attendees: list[str],
) -> dict[str, Any]:
    calendars = _google_calendar_map()
    if calendar_name not in calendars:
        raise PermissionError(f"Google calendar '{calendar_name}' is not allowed.")
    calendar_id = calendars[calendar_name]
    token = _google_access_token()
    event = _http_json(
        "POST",
        f"https://www.googleapis.com/calendar/v3/calendars/{quote(calendar_id, safe='')}/events?sendUpdates=all",
        headers={"Authorization": f"Bearer {token}"},
        payload={
            "summary": title,
            "description": description,
            "location": location,
            "start": {"dateTime": start.isoformat(), "timeZone": timezone},
            "end": {"dateTime": end.isoformat(), "timeZone": timezone},
            "attendees": [{"email": email} for email in attendees],
        },
    )
    event_id = str(event.get("id", ""))
    if not event_id:
        raise RuntimeError("Google did not return an event ID.")
    return _http_json(
        "GET",
        f"https://www.googleapis.com/calendar/v3/calendars/{quote(calendar_id, safe='')}/events/{quote(event_id, safe='')}",
        headers={"Authorization": f"Bearer {token}"},
    )


def _calendar_name(calendar: Any) -> str:
    try:
        return str(calendar.get_display_name())
    except Exception:
        return str(calendar.name)


def _client() -> caldav.DAVClient:
    return caldav.DAVClient(
        url=os.getenv("ICLOUD_CALDAV_URL", "https://caldav.icloud.com/"),
        username=_required("ICLOUD_USERNAME"),
        password=_required("ICLOUD_APP_PASSWORD"),
    )


def _calendars(client: caldav.DAVClient) -> list[Any]:
    allowed = _allowed_names()
    calendars = client.principal().calendars()
    return [calendar for calendar in calendars if _calendar_name(calendar) in allowed]


def _calendar(client: caldav.DAVClient, name: str) -> Any:
    if name not in _allowed_names():
        raise PermissionError(f"Calendar '{name}' is not in ICLOUD_ALLOWED_CALENDARS.")
    for calendar in _calendars(client):
        if _calendar_name(calendar) == name:
            return calendar
    raise LookupError(f"Allowed calendar '{name}' was not found in iCloud.")


def _component_value(component: Any, key: str) -> Any:
    value = component.get(key)
    if value is None:
        return None
    decoded = value.dt if hasattr(value, "dt") else value
    return decoded.isoformat() if hasattr(decoded, "isoformat") else str(decoded)


def _event_result(resource: Any, calendar_name: str) -> dict[str, Any]:
    component = resource.get_icalendar_component()
    return {
        "uid": str(component.get("uid", "")),
        "calendar": calendar_name,
        "title": str(component.get("summary", "")),
        "start": _component_value(component, "dtstart"),
        "end": _component_value(component, "dtend"),
        "location": str(component.get("location", "")),
        "description": str(component.get("description", "")),
        "url": str(component.get("url", "")),
    }


def _as_reference_datetime(value: Any, reference: datetime) -> datetime | None:
    """Interpret floating iCloud values in the timezone supplied by the caller."""
    if isinstance(value, datetime):
        return value.replace(tzinfo=reference.tzinfo) if value.tzinfo is None else value
    if isinstance(value, date):
        return datetime.combine(value, datetime_time.min, tzinfo=reference.tzinfo)
    return None


def _component_overlaps(component: Any, start: datetime, end: datetime) -> bool:
    candidate_start = _as_reference_datetime(component.get("dtstart").dt, start)
    candidate_end_value = component.get("dtend")
    candidate_end = (
        _as_reference_datetime(candidate_end_value.dt, end)
        if candidate_end_value is not None
        else candidate_start
    )
    if candidate_start is None or candidate_end is None:
        return False
    return candidate_start < end and candidate_end > start


def _component_matches_exactly(
    component: Any, title: str, start: datetime, end: datetime
) -> bool:
    if str(component.get("summary", "")) != title:
        return False
    candidate_start = _as_reference_datetime(component.get("dtstart").dt, start)
    candidate_end_value = component.get("dtend")
    candidate_end = (
        _as_reference_datetime(candidate_end_value.dt, end)
        if candidate_end_value is not None
        else None
    )
    return candidate_start == start and candidate_end == end


def _calendar_resources_in_window(
    calendar: Any, start: datetime, end: datetime, *, expand: bool
) -> list[Any]:
    """Fetch a padded window so legacy floating events are not lost by iCloud."""
    try:
        candidates = calendar.search(
            event=True,
            start=start - timedelta(days=1),
            end=end + timedelta(days=1),
            expand=expand,
        )
    except Exception:
        candidates = calendar.events()
    resources = []
    for resource in candidates:
        try:
            if _component_overlaps(resource.get_icalendar_component(), start, end):
                resources.append(resource)
        except Exception:
            continue
    return resources


def _resource_delete_status(resource: Any) -> int:
    """Delete with the current iCloud precondition tags instead of a stale resource."""
    try:
        resource.get_properties([dav.GetEtag(), cdav.ScheduleTag()])
    except Exception:
        # A conditional wildcard still protects against deleting a missing resource.
        pass
    headers: dict[str, str] = {}
    if resource.etag:
        headers["if-match"] = str(resource.etag)
    else:
        headers["if-match"] = "*"
    if resource.schedule_tag:
        headers["if-schedule-tag-match"] = str(resource.schedule_tag)
    response = resource.client.request(str(resource.url), "DELETE", "", headers)
    if response.status not in (200, 204, 404, 412):
        raise RuntimeError(f"iCloud DELETE returned HTTP {response.status}.")
    return response.status


def _resource_by_uid(calendar: Any, uid: str) -> Any:
    """Use a plain collection listing when Apple's UID REPORT returns 412."""
    try:
        return calendar.get_event_by_uid(uid)
    except caldav_error.NotFoundError:
        raise
    except Exception:
        matches = []
        for resource in calendar.events():
            try:
                component_uid = str(
                    resource.get_icalendar_component().get("uid", "")
                )
                if component_uid == uid:
                    matches.append(resource)
            except Exception:
                continue
        if not matches:
            raise caldav_error.NotFoundError(f"{uid} not found on server")
        if len(matches) > 1:
            raise RuntimeError(f"More than one iCloud event has UID {uid}.")
        return matches[0]


def _event_exists(calendar_name: str, uid: str) -> bool:
    with _client() as client:
        calendar = _calendar(client, calendar_name)
        try:
            _resource_by_uid(calendar, uid)
        except caldav_error.NotFoundError:
            return False
        return True


def _delete_icloud_event(calendar_name: str, uid: str) -> dict[str, Any]:
    """Refresh preconditions, retry 412 responses, and verify actual absence."""
    last_status: int | None = None
    for attempt in range(3):
        if attempt:
            time.sleep(1)
        with _client() as client:
            calendar = _calendar(client, calendar_name)
            try:
                resource = _resource_by_uid(calendar, uid)
            except caldav_error.NotFoundError:
                return {
                    "uid": uid,
                    "calendar": calendar_name,
                    "deleted": attempt > 0,
                    "already_absent": attempt == 0,
                    "verified": True,
                }
            last_status = _resource_delete_status(resource)
        if not _event_exists(calendar_name, uid):
            return {
                "uid": uid,
                "calendar": calendar_name,
                "deleted": True,
                "already_absent": False,
                "verified": True,
            }
    return {
        "uid": uid,
        "calendar": calendar_name,
        "deleted": False,
        "already_absent": False,
        "verified": False,
        "http_status": last_status,
        "error": "iCloud still contains the event after three refreshed delete attempts.",
    }


mcp = FastMCP(
    "iCloud Calendar",
    host="0.0.0.0",
    instructions=(
        "Access only calendars named in ICLOUD_ALLOWED_CALENDARS. "
        "Use timezone-aware ISO 8601 date-times and verify writes by reading them back."
    ),
    stateless_http=True,
    json_response=True,
    token_verifier=SignedTokenVerifier(),
    auth=AuthSettings(
        issuer_url=AnyHttpUrl(PUBLIC_URL),
        resource_server_url=AnyHttpUrl(MCP_RESOURCE),
        required_scopes=[OAUTH_SCOPE],
        service_documentation_url=AnyHttpUrl(f"{PUBLIC_URL}/docs"),
    ),
)


@mcp.tool(
    title="List calendars available for Zoom events",
    annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False),
)
def list_calendars() -> list[dict[str, str]]:
    """List writable iCloud and Google calendars explicitly allowed for this connector."""
    with _client() as client:
        calendars = [
            {"name": _calendar_name(calendar), "provider": "icloud"}
            for calendar in _calendars(client)
        ]
    calendars.extend(
        {"name": name, "provider": "google"} for name in _google_calendar_map()
    )
    return calendars


@mcp.tool(
    title="List iCloud calendar events",
    annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False),
)
def list_events(calendar_name: str, start: str, end: str) -> list[dict[str, Any]]:
    """List events overlapping a timezone-aware ISO 8601 interval."""
    start_dt = _parse_datetime(start)
    end_dt = _parse_datetime(end)
    if end_dt <= start_dt:
        raise ValueError("end must be later than start")
    with _client() as client:
        calendar = _calendar(client, calendar_name)
        resources = _calendar_resources_in_window(
            calendar, start_dt, end_dt, expand=True
        )
        return [_event_result(resource, calendar_name) for resource in resources]


@mcp.tool(
    title="Create an iCloud calendar event",
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False),
)
def create_event(
    calendar_name: str,
    title: str,
    start: str,
    end: str,
    description: str = "",
    location: str = "",
    url: str = "",
    timezone: str = "America/Toronto",
) -> dict[str, Any]:
    """Create an event, then read it back from iCloud to verify persistence."""
    start_dt = _parse_datetime(start, timezone)
    end_dt = _parse_datetime(end, timezone)
    if end_dt <= start_dt:
        raise ValueError("end must be later than start")

    uid = f"{secrets.token_hex(16)}@codex-icloud-calendar"
    event = Event()
    event.add("uid", uid)
    event.add("dtstamp", datetime.now().astimezone())
    event.add("dtstart", start_dt)
    event.add("dtend", end_dt)
    event.add("summary", title)
    if description:
        event.add("description", description)
    if location:
        event.add("location", location)
    if url:
        event.add("url", url)

    payload = ICalendar()
    payload.add("prodid", "-//Codex iCloud Calendar//EN")
    payload.add("version", "2.0")
    payload.add_component(event)

    def reconcile() -> dict[str, Any] | None:
        # iCloud can return HTTP 412 after accepting a CalDAV write. Re-open the
        # collection and search by our unique UID before treating it as failed.
        for attempt in range(3):
            if attempt:
                time.sleep(1)
            try:
                with _client() as verify_client:
                    verify_calendar = _calendar(verify_client, calendar_name)
                    resources = verify_calendar.search(
                        event=True,
                        start=start_dt - timedelta(minutes=1),
                        end=end_dt + timedelta(minutes=1),
                        expand=False,
                    )
                    for resource in resources:
                        candidate = _event_result(resource, calendar_name)
                        if candidate.get("uid") == uid:
                            candidate["verified"] = True
                            candidate["verification_method"] = "fresh_calendar_search"
                            return candidate
            except Exception:
                continue
        return None

    write_completed = False
    write_error: Exception | None = None
    try:
        with _client() as client:
            calendar = _calendar(client, calendar_name)
            calendar.add_event(payload.to_ical().decode("utf-8"))
            write_completed = True
            try:
                saved = calendar.get_event_by_uid(uid)
                result = _event_result(saved, calendar_name)
                result["verified"] = result["uid"] == uid
                result["verification_method"] = "direct_uid_lookup"
                return result
            except Exception:
                pass
    except Exception as exc:
        write_error = exc

    reconciled = reconcile()
    if reconciled is not None:
        return reconciled
    if write_completed or (write_error is not None and "412" in str(write_error)):
        return {
            "uid": uid,
            "calendar": calendar_name,
            "title": title,
            "start": start,
            "end": end,
            "verified": False,
            "write_status": "accepted_but_icloud_verification_unavailable",
            "do_not_retry": True,
            "warning": (
                "iCloud returned 412 during post-write verification. The event may already "
                "be visible in the calendar; do not retry automatically."
            ),
        }
    raise RuntimeError(
        "iCloud did not confirm the event after the CalDAV write. "
        "The event may still exist; check the calendar before retrying."
    )


@mcp.tool(
    title="Create a verified Zoom event in iCloud or Google Calendar",
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True),
)
def create_zoom_event(
    calendar_name: str,
    title: str,
    start: str,
    end: str,
    timezone: str = "America/Toronto",
    provider: str | None = None,
    description: str = "",
    location: str = "",
    attendees: list[str] | None = None,
) -> dict[str, Any]:
    """Create a real Zoom meeting, save it in an allowed iCloud or Google calendar, and verify it."""
    start_dt = _parse_datetime(start, timezone)
    end_dt = _parse_datetime(end, timezone)
    if end_dt <= start_dt:
        raise ValueError("end must be later than start")

    if provider is None:
        if calendar_name in _allowed_names():
            provider = "icloud"
        elif calendar_name in _google_calendar_map():
            provider = "google"
        else:
            raise LookupError(f"Calendar '{calendar_name}' is not configured.")
    provider = provider.lower()
    if provider not in {"icloud", "google"}:
        raise ValueError("provider must be 'icloud' or 'google'.")

    meeting = _create_zoom_meeting(title, start_dt, end_dt, timezone)
    meeting_id = str(meeting.get("id", ""))
    join_url = str(meeting.get("join_url", ""))
    if not meeting_id or not join_url.startswith("https://") or "zoom.us/" not in join_url:
        raise RuntimeError("Zoom did not return a valid meeting ID and join URL.")

    event_description = f"{description.strip()}\n\nZoom: {join_url}".strip()
    event_location = location or "Zoom"
    try:
        if provider == "icloud":
            event = create_event(
                calendar_name=calendar_name,
                title=title,
                start=start,
                end=end,
                description=event_description,
                location=event_location,
                url=join_url,
                timezone=timezone,
            )
        else:
            event = _create_google_event(
                calendar_name=calendar_name,
                title=title,
                start=start_dt,
                end=end_dt,
                timezone=timezone,
                description=event_description,
                location=event_location,
                attendees=attendees or [],
            )
    except Exception as exc:
        cleanup_error = ""
        try:
            _delete_zoom_meeting(meeting_id)
        except Exception as cleanup_exc:
            cleanup_error = f" Zoom cleanup also failed: {cleanup_exc}"
        raise RuntimeError(f"Calendar creation failed; the Zoom meeting was rolled back.{cleanup_error}") from exc

    event_verified = bool(event.get("verified", False))
    return {
        "provider": provider,
        "calendar": calendar_name,
        "title": title,
        "start": start,
        "end": end,
        "timezone": timezone,
        "zoom_meeting_id": meeting_id,
        "zoom_join_url": join_url,
        "event": event,
        "verified": event_verified,
        "calendar_write_status": (
            "verified" if event_verified else "accepted_but_icloud_verification_unavailable"
        ),
        "do_not_retry": not event_verified,
    }


@mcp.tool(
    title="Update an iCloud calendar event",
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False),
)
def update_event(
    calendar_name: str,
    uid: str,
    title: str | None = None,
    start: str | None = None,
    end: str | None = None,
    description: str | None = None,
    location: str | None = None,
    url: str | None = None,
    timezone: str = "America/Toronto",
) -> dict[str, Any]:
    """Update an event by UID, then read it back to verify persistence."""
    with _client() as client:
        calendar = _calendar(client, calendar_name)
        resource = calendar.get_event_by_uid(uid)
        with resource.edit_icalendar_component() as component:
            if title is not None:
                component["summary"] = title
            if start is not None:
                component.pop("dtstart", None)
                component.add("dtstart", _parse_datetime(start, timezone))
            if end is not None:
                component.pop("dtend", None)
                component.add("dtend", _parse_datetime(end, timezone))
            if description is not None:
                component["description"] = description
            if location is not None:
                component["location"] = location
            if url is not None:
                component["url"] = url
        resource.save()
        saved = calendar.get_event_by_uid(uid)
        result = _event_result(saved, calendar_name)
        result["verified"] = result["uid"] == uid
        return result


@mcp.tool(
    title="Delete an iCloud calendar event",
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=False),
)
def delete_event(calendar_name: str, uid: str) -> dict[str, Any]:
    """Delete an event by UID and verify that it is no longer retrievable."""
    return _delete_icloud_event(calendar_name, uid)


@mcp.tool(
    title="Delete a Zoom calendar event by title and time",
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=True),
)
def delete_zoom_event(
    calendar_name: str,
    title: str,
    start: str,
    end: str,
    zoom_meeting_id: str = "",
) -> dict[str, Any]:
    """Delete one matching iCloud event and, when supplied, its Zoom meeting."""
    start_dt = _parse_datetime(start)
    end_dt = _parse_datetime(end)
    if end_dt <= start_dt:
        raise ValueError("end must be later than start")

    matches: list[Any] = []
    with _client() as client:
        calendar = _calendar(client, calendar_name)
        candidates = _calendar_resources_in_window(
            calendar, start_dt, end_dt, expand=False
        )
        for resource in candidates:
            try:
                component = resource.get_icalendar_component()
                if _component_matches_exactly(component, title, start_dt, end_dt):
                    matches.append(resource)
            except Exception:
                continue
        if len(matches) > 1:
            raise RuntimeError("More than one matching iCloud event was found; nothing was deleted.")
        calendar_uid = (
            str(matches[0].get_icalendar_component().get("uid", "")) if matches else ""
        )

    if matches:
        calendar_result = _delete_icloud_event(calendar_name, calendar_uid)
    else:
        calendar_result = {
            "deleted": False,
            "already_absent": True,
            "verified": True,
        }

    zoom_deleted = False
    zoom_already_absent = False
    if zoom_meeting_id.strip():
        try:
            _delete_zoom_meeting(zoom_meeting_id.strip())
            zoom_deleted = True
        except Exception as exc:
            if "HTTP 404" in str(exc):
                zoom_already_absent = True
            else:
                raise

    return {
        "calendar": calendar_name,
        "title": title,
        "calendar_event_found": bool(matches),
        "calendar_event_uid": calendar_uid,
        "calendar_deleted": bool(calendar_result["deleted"]),
        "calendar_already_absent": bool(calendar_result["already_absent"]),
        "calendar_deletion_verified": bool(calendar_result["verified"]),
        "zoom_deleted": zoom_deleted,
        "zoom_already_absent": zoom_already_absent,
        "do_not_retry": bool(matches) and not bool(calendar_result["verified"]),
        **({"calendar_error": calendar_result["error"]} if "error" in calendar_result else {}),
    }


async def oauth_metadata(_: Request) -> JSONResponse:
    return JSONResponse(
        {
            "issuer": PUBLIC_URL,
            "authorization_endpoint": f"{PUBLIC_URL}/authorize",
            "token_endpoint": f"{PUBLIC_URL}/token",
            "registration_endpoint": f"{PUBLIC_URL}/register",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["none"],
            "scopes_supported": [OAUTH_SCOPE],
        }
    )


async def protected_resource_metadata(_: Request) -> JSONResponse:
    return JSONResponse(
        {
            "resource": MCP_RESOURCE,
            "authorization_servers": [PUBLIC_URL],
            "scopes_supported": [OAUTH_SCOPE],
            "bearer_methods_supported": ["header"],
            "resource_documentation": f"{PUBLIC_URL}/docs",
        }
    )


async def register(request: Request) -> JSONResponse:
    try:
        metadata = await request.json()
    except (json.JSONDecodeError, ValueError):
        return _oauth_error("invalid_client_metadata", "A JSON request body is required.")
    redirect_uris = metadata.get("redirect_uris", [])
    if not redirect_uris or not all(_valid_redirect_uri(uri) for uri in redirect_uris):
        return _oauth_error("invalid_redirect_uri", "Only HTTPS or localhost redirect URIs are allowed.")
    now = int(time.time())
    client_id = _sign(
        {
            "typ": "client",
            "redirect_uris": redirect_uris,
            "client_name": str(metadata.get("client_name", "MCP client"))[:100],
            "iat": now,
        }
    )
    return JSONResponse(
        {
            **metadata,
            "client_id": client_id,
            "client_id_issued_at": now,
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "scope": OAUTH_SCOPE,
        },
        status_code=201,
    )


def _authorization_request(params: dict[str, str]) -> tuple[dict[str, Any] | None, str | None]:
    required = ("client_id", "redirect_uri", "code_challenge")
    if any(not params.get(key) for key in required):
        return None, "client_id, redirect_uri and code_challenge are required."
    client = _client_payload(params["client_id"])
    if not client or params["redirect_uri"] not in client.get("redirect_uris", []):
        return None, "Unknown client or unregistered redirect URI."
    if params.get("response_type", "code") != "code":
        return None, "Only response_type=code is supported."
    if params.get("code_challenge_method") != "S256":
        return None, "PKCE with code_challenge_method=S256 is required."
    resource = params.get("resource", MCP_RESOURCE)
    if resource not in RESOURCE_ALIASES:
        return None, "The requested resource is not this MCP server."
    scopes = params.get("scope", OAUTH_SCOPE).split()
    if any(scope != OAUTH_SCOPE for scope in scopes):
        return None, "An unsupported scope was requested."
    return {
        "client_id": params["client_id"],
        "redirect_uri": params["redirect_uri"],
        "code_challenge": params["code_challenge"],
        "state": params.get("state", ""),
        "scope": OAUTH_SCOPE,
        "resource": MCP_RESOURCE,
    }, None


async def authorize(request: Request) -> HTMLResponse | RedirectResponse:
    if request.method == "GET":
        params = dict(request.query_params)
        form: dict[str, str] = {}
    else:
        form = {key: values[-1] for key, values in parse_qs((await request.body()).decode()).items()}
        params = {key: value for key, value in form.items() if key != "connector_password"}
    auth_request, error = _authorization_request(params)
    if error:
        return HTMLResponse(f"<h1>Demande refusée</h1><p>{html.escape(error)}</p>", status_code=400)
    assert auth_request is not None
    if request.method == "POST":
        supplied = form.get("connector_password", "")
        if hmac.compare_digest(supplied, _required("ICLOUD_MCP_BEARER_TOKEN")):
            now = int(time.time())
            code = _sign({"typ": "code", **auth_request, "iat": now, "exp": now + 300})
            redirect_params = {"code": code}
            if auth_request["state"]:
                redirect_params["state"] = auth_request["state"]
            separator = "&" if "?" in auth_request["redirect_uri"] else "?"
            return RedirectResponse(
                f'{auth_request["redirect_uri"]}{separator}{urlencode(redirect_params)}',
                status_code=302,
            )
        error_message = "<p style='color:#b42318'>Mot de passe incorrect.</p>"
    else:
        error_message = ""
    hidden = "".join(
        f'<input type="hidden" name="{html.escape(key)}" value="{html.escape(str(value), quote=True)}">'
        for key, value in params.items()
    )
    return HTMLResponse(
        f"""<!doctype html><html lang="fr"><meta name="viewport" content="width=device-width">
        <title>Autoriser iCloud Calendar</title>
        <style>body{{font:16px system-ui;background:#f5f5f7;margin:0;padding:32px}}
        main{{max-width:480px;margin:8vh auto;background:white;padding:32px;border-radius:18px;
        box-shadow:0 8px 30px #0002}}input,button{{box-sizing:border-box;width:100%;padding:13px;
        margin-top:12px;border-radius:10px;border:1px solid #bbb}}button{{background:#111;color:white;
        cursor:pointer}}small{{color:#666}}</style><main><h1>Autoriser iCloud Calendar</h1>
        <p>Cette application demande l’accès aux calendriers iCloud que vous avez autorisés sur Render.</p>
        {error_message}<form method="post">{hidden}
        <label>Mot de passe du connecteur</label>
        <input name="connector_password" type="password" autocomplete="current-password" required>
        <button type="submit">Autoriser</button></form>
        <p><small>Utilisez la valeur secrète ICLOUD_MCP_BEARER_TOKEN enregistrée sur Render.</small></p>
        </main></html>"""
    )


async def token(request: Request) -> JSONResponse:
    form = {key: values[-1] for key, values in parse_qs((await request.body()).decode()).items()}
    grant_type = form.get("grant_type")
    client_id = form.get("client_id", "")
    client = _client_payload(client_id)
    if not client:
        return _oauth_error("invalid_client", "Unknown client.")
    now = int(time.time())
    if grant_type == "authorization_code":
        raw_code = form.get("code", "")
        code_hash = hashlib.sha256(raw_code.encode()).hexdigest()
        code = _unsign(raw_code, "code")
        if (
            not code
            or code_hash in _used_authorization_codes
            or code.get("client_id") != client_id
            or code.get("redirect_uri") != form.get("redirect_uri")
        ):
            return _oauth_error("invalid_grant", "Invalid authorization code.")
        verifier = form.get("code_verifier", "")
        challenge = _b64encode(hashlib.sha256(verifier.encode()).digest())
        if not verifier or not hmac.compare_digest(challenge, str(code.get("code_challenge", ""))):
            return _oauth_error("invalid_grant", "PKCE verification failed.")
        _used_authorization_codes.add(code_hash)
        resource = code["resource"]
        scopes = [OAUTH_SCOPE]
    elif grant_type == "refresh_token":
        raw_refresh = form.get("refresh_token", "")
        refresh_hash = hashlib.sha256(raw_refresh.encode()).hexdigest()
        refresh = _unsign(raw_refresh, "refresh")
        if (
            not refresh
            or refresh_hash in _used_refresh_tokens
            or refresh.get("client_id") != client_id
        ):
            return _oauth_error("invalid_grant", "Invalid refresh token.")
        _used_refresh_tokens.add(refresh_hash)
        resource = refresh["resource"]
        scopes = list(refresh["scopes"])
    else:
        return _oauth_error("unsupported_grant_type", "Use authorization_code or refresh_token.")
    access_token = _sign(
        {
            "typ": "access",
            "client_id": client_id,
            "scopes": scopes,
            "resource": resource,
            "iat": now,
            "exp": now + 3600,
        }
    )
    refresh_token = _sign(
        {
            "typ": "refresh",
            "client_id": client_id,
            "scopes": scopes,
            "resource": resource,
            "iat": now,
            "exp": now + 30 * 86400,
        }
    )
    return JSONResponse(
        {
            "access_token": access_token,
            "token_type": "Bearer",
            "expires_in": 3600,
            "refresh_token": refresh_token,
            "scope": " ".join(scopes),
            "resource": resource,
        },
        headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
    )


async def health(_: Request) -> JSONResponse:
    return JSONResponse({"status": "ok", "authentication": "oauth"})


async def documentation(_: Request) -> HTMLResponse:
    return HTMLResponse(
        """<!doctype html><html lang="fr"><meta name="viewport" content="width=device-width">
        <title>iCloud Calendar pour ChatGPT</title><style>
        body{font:16px system-ui;line-height:1.55;max-width:720px;margin:48px auto;padding:0 20px}
        h1,h2{line-height:1.2}</style><h1>iCloud Calendar pour ChatGPT</h1>
        <p>Connecteur privé permettant à son propriétaire de consulter et gérer uniquement les
        calendriers iCloud explicitement autorisés dans Render.</p>
        <h2>Données traitées</h2><p>Noms de calendriers, événements, dates, lieux, descriptions et
        liens nécessaires aux actions demandées. Les identifiants Apple et mots de passe
        d’application restent des secrets Render et ne sont jamais envoyés à ChatGPT.</p>
        <h2>Conservation</h2><p>Le connecteur ne crée pas de copie durable des calendriers.
        Les données sont lues ou modifiées directement dans iCloud via CalDAV.</p>
        <h2>Accès</h2><p>L’accès exige OAuth 2.1 avec PKCE et le mot de passe privé du connecteur.
        Le propriétaire peut révoquer l’accès en renouvelant les secrets Render.</p>
        <p><a href="/privacy">Politique de confidentialité</a> ·
        <a href="/terms">Conditions d’utilisation</a></p></html>"""
    )


async def privacy(_: Request) -> HTMLResponse:
    return HTMLResponse(
        """<!doctype html><html lang="fr"><meta name="viewport" content="width=device-width">
        <title>Confidentialité — iCloud Calendar</title><body style="font:16px system-ui;
        line-height:1.55;max-width:720px;margin:48px auto;padding:0 20px">
        <h1>Politique de confidentialité</h1><p>Cette app privée traite uniquement les données
        nécessaires pour exécuter les demandes de son propriétaire dans les calendriers iCloud
        autorisés. Elle ne vend, ne partage et ne conserve pas durablement ces données.</p>
        <p>Les secrets Apple sont stockés dans Render et ne sont pas exposés aux clients MCP.</p>
        </body></html>"""
    )


async def terms(_: Request) -> HTMLResponse:
    return HTMLResponse(
        """<!doctype html><html lang="fr"><meta name="viewport" content="width=device-width">
        <title>Conditions — iCloud Calendar</title><body style="font:16px system-ui;
        line-height:1.55;max-width:720px;margin:48px auto;padding:0 20px">
        <h1>Conditions d’utilisation</h1><p>Cette app est destinée à l’usage privé de son
        propriétaire. Toute création, modification ou suppression d’événement doit correspondre à
        une demande explicite de l’utilisateur et peut être révoquée depuis Render ou Apple.</p>
        </body></html>"""
    )


mcp.custom_route("/", methods=["GET"])(health)
mcp.custom_route("/docs", methods=["GET"])(documentation)
mcp.custom_route("/privacy", methods=["GET"])(privacy)
mcp.custom_route("/terms", methods=["GET"])(terms)
mcp.custom_route("/.well-known/oauth-authorization-server", methods=["GET"])(oauth_metadata)
mcp.custom_route("/.well-known/openid-configuration", methods=["GET"])(oauth_metadata)
mcp.custom_route("/.well-known/oauth-protected-resource", methods=["GET"])(
    protected_resource_metadata
)
mcp.custom_route("/.well-known/oauth-protected-resource/mcp", methods=["GET"])(
    protected_resource_metadata
)
mcp.custom_route("/register", methods=["POST"])(register)
mcp.custom_route("/authorize", methods=["GET", "POST"])(authorize)
mcp.custom_route("/token", methods=["POST"])(token)

app = mcp.streamable_http_app()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--stdio", action="store_true")
    args = parser.parse_args()
    if not args.stdio:
        raise SystemExit("Use uvicorn scripts.server:app for HTTP mode.")
    mcp.run(transport="stdio")
