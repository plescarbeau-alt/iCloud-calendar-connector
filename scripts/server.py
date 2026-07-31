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
from datetime import datetime
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import caldav
from icalendar import Calendar as ICalendar
from icalendar import Event
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import AnyHttpUrl
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.routing import Mount, Route


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
    token_verifier=SignedTokenVerifier(),
    auth=AuthSettings(
        issuer_url=AnyHttpUrl(PUBLIC_URL),
        resource_server_url=AnyHttpUrl(MCP_RESOURCE),
        required_scopes=[OAUTH_SCOPE],
        service_documentation_url=AnyHttpUrl(f"{PUBLIC_URL}/docs"),
    ),
)


@mcp.tool(
    title="List iCloud calendars",
    annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False),
)
def list_calendars() -> list[dict[str, str]]:
    """List only the iCloud calendars explicitly allowed for this connector."""
    with _client() as client:
        return [{"name": _calendar_name(calendar)} for calendar in _calendars(client)]


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
        resources = calendar.search(event=True, start=start_dt, end=end_dt, expand=True)
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


@mcp.tool(
    title="Delete an iCloud calendar event",
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=False),
)
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


mcp_http_app = mcp.streamable_http_app()

app = Starlette(
    routes=[
        Route("/", health),
        Route("/docs", documentation),
        Route("/privacy", privacy),
        Route("/terms", terms),
        Route("/.well-known/oauth-authorization-server", oauth_metadata),
        Route("/.well-known/openid-configuration", oauth_metadata),
        Route("/.well-known/oauth-protected-resource", protected_resource_metadata),
        Route("/.well-known/oauth-protected-resource/mcp", protected_resource_metadata),
        Route("/register", register, methods=["POST"]),
        Route("/authorize", authorize, methods=["GET", "POST"]),
        Route("/token", token, methods=["POST"]),
        Mount("/", app=mcp_http_app),
    ],
    lifespan=mcp_http_app.lifespan,
)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--stdio", action="store_true")
    args = parser.parse_args()
    if not args.stdio:
        raise SystemExit("Use uvicorn scripts.server:app for HTTP mode.")
    mcp.run(transport="stdio")
