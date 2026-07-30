"""Private MCP bridge for one or more explicitly allowed iCloud calendars."""

from __future__ import annotations

import argparse
import hmac
import os
import secrets
from datetime import datetime
from typing import Any

import caldav
from icalendar import Calendar as ICalendar
from icalendar import Event
from mcp.server.fastmcp import FastMCP


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def _allowed_names() -> set[str]:
    value = _required("ICLOUD_ALLOWED_CALENDARS")
    return {item.strip() for item in value.split(",") if item.strip()}


def _parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Date-times must include a timezone offset, for example -04:00.")
    return parsed


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


mcp = FastMCP(
    "iCloud Calendar",
    instructions=(
        "Access only calendars named in ICLOUD_ALLOWED_CALENDARS. "
        "Use timezone-aware ISO 8601 date-times and verify writes by reading them back."
    ),
    stateless_http=True,
    json_response=True,
)


@mcp.tool()
def list_calendars() -> list[dict[str, str]]:
    """List only the iCloud calendars explicitly allowed for this connector."""
    with _client() as client:
        return [{"name": _calendar_name(calendar)} for calendar in _calendars(client)]


@mcp.tool()
def list_events(calendar_name: str, start: str, end: str) -> list[dict[str, Any]]:
    """List events overlapping a timezone-aware ISO 8601 interval."""
    start_dt = _parse_datetime(start)
    end_dt = _parse_datetime(end)
    if end_dt <= start_dt:
        raise ValueError("end must be later than start")
    with _client() as client:
        calendar = _calendar(client, calendar_name)
        resources = calendar.search(event=True, start=start_dt, end=end_dt, expand=True)
        return [_event_result(resource, calendar_name) for resource in resources]


@mcp.tool()
def create_event(
    calendar_name: str,
    title: str,
    start: str,
    end: str,
    description: str = "",
    location: str = "",
    url: str = "",
) -> dict[str, Any]:
    """Create an event, then read it back from iCloud to verify persistence."""
    start_dt = _parse_datetime(start)
    end_dt = _parse_datetime(end)
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

    with _client() as client:
        calendar = _calendar(client, calendar_name)
        calendar.add_event(payload.to_ical().decode("utf-8"))
        saved = calendar.get_event_by_uid(uid)
        result = _event_result(saved, calendar_name)
        result["verified"] = result["uid"] == uid
        return result


@mcp.tool()
def update_event(
    calendar_name: str,
    uid: str,
    title: str | None = None,
    start: str | None = None,
    end: str | None = None,
    description: str | None = None,
    location: str | None = None,
    url: str | None = None,
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
                component.add("dtstart", _parse_datetime(start))
            if end is not None:
                component.pop("dtend", None)
                component.add("dtend", _parse_datetime(end))
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


@mcp.tool()
def delete_event(calendar_name: str, uid: str) -> dict[str, Any]:
    """Delete an event by UID and verify that it is no longer retrievable."""
    with _client() as client:
        calendar = _calendar(client, calendar_name)
        resource = calendar.get_event_by_uid(uid)
        resource.delete()
        try:
            calendar.get_event_by_uid(uid)
        except Exception:
            return {"uid": uid, "calendar": calendar_name, "deleted": True, "verified": True}
        return {"uid": uid, "calendar": calendar_name, "deleted": True, "verified": False}


class BearerAuthMiddleware:
    """Small ASGI guard for hosted MCP deployments."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        expected = _required("ICLOUD_MCP_BEARER_TOKEN")
        headers = {key.decode().lower(): value.decode() for key, value in scope.get("headers", [])}
        supplied = headers.get("authorization", "")
        valid = supplied.startswith("Bearer ") and hmac.compare_digest(
            supplied.removeprefix("Bearer "), expected
        )
        if not valid:
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [(b"content-type", b"application/json")],
                }
            )
            await send({"type": "http.response.body", "body": b'{"error":"unauthorized"}'})
            return
        await self.app(scope, receive, send)


app = BearerAuthMiddleware(mcp.streamable_http_app())


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--stdio", action="store_true")
    args = parser.parse_args()
    if not args.stdio:
        raise SystemExit("Use uvicorn scripts.server:app for HTTP mode.")
    mcp.run(transport="stdio")
