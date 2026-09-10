"""Microsoft Graph calendar provider.

Why this exists alongside the Outlook COM provider: COM reads whatever the
classic Outlook desktop profile happens to have cached locally. If you live in
the Outlook PWA, that cache can be stale or incomplete - meetings you were
invited to but have not organised are the usual casualty. Graph reads the
server-side mailbox, so it sees what the web app sees.

Auth is a delegated device-code or interactive sign-in against your own Entra
app registration, with the refresh token cached under the runtime directory.
Read-only: the only scope requested is Calendars.Read.
"""

from __future__ import annotations

from datetime import datetime, timedelta
import json
from pathlib import Path
from typing import Any, Callable
import urllib.error
import urllib.parse
import urllib.request

from .defaults import (
    OUTLOOK_BUSY,
    OUTLOOK_FREE,
    OUTLOOK_OUT_OF_OFFICE,
    OUTLOOK_TENTATIVE,
    OUTLOOK_WORKING_ELSEWHERE,
)

GRAPH_SCOPES = ["Calendars.Read"]
GRAPH_CALENDAR_VIEW = "https://graph.microsoft.com/v1.0/me/calendarView"
AUTHORITY_TEMPLATE = "https://login.microsoftonline.com/{tenant}"

# Graph reports availability as a word; the rest of the program speaks Outlook's
# OlBusyStatus integers. Mapping here - at the edge - means alert_statuses stays
# one concept no matter which provider fetched the event.
SHOW_AS_TO_BUSY_STATUS = {
    "free": OUTLOOK_FREE,
    "tentative": OUTLOOK_TENTATIVE,
    "busy": OUTLOOK_BUSY,
    "oof": OUTLOOK_OUT_OF_OFFICE,
    "workingElsewhere": OUTLOOK_WORKING_ELSEWHERE,
    # Graph returns "unknown" for some invitations. Treating it as Busy is the
    # safe default: a missed warning is worse than an extra one.
    "unknown": OUTLOOK_BUSY,
}


class GraphAuthError(RuntimeError):
    """Raised when there is no usable token and no way to get one silently."""


def graph_config(config: dict[str, Any]) -> dict[str, Any]:
    return config.get("calendar", {}).get("graph", {})


def _import_msal():
    try:
        import msal  # noqa: PLC0415 - optional dependency, only needed for Graph
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise GraphAuthError(
            "The 'msal' package is required for the Graph provider. "
            "Run blink-light.bat once to reinstall requirements, or: pip install msal"
        ) from exc
    return msal


def _authority(settings: dict[str, Any]) -> str:
    return AUTHORITY_TEMPLATE.format(tenant=settings.get("tenant_id") or "organizations")


def _load_cache(msal, token_path: Path):
    cache = msal.SerializableTokenCache()
    if token_path.exists():
        cache.deserialize(token_path.read_text(encoding="utf-8"))
    return cache


def _save_cache(cache, token_path: Path) -> None:
    if not cache.has_state_changed:
        return
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(cache.serialize(), encoding="utf-8")


def build_app(config: dict[str, Any], token_path: Path):
    """An MSAL public client wired to the on-disk token cache."""
    msal = _import_msal()
    settings = graph_config(config)
    client_id = settings.get("client_id")
    if not client_id:
        raise GraphAuthError(
            "'calendar.graph.client_id' is not set. Register an Entra app "
            "(see README, 'Signing in with Microsoft 365') and put its "
            "Application (client) ID there."
        )
    cache = _load_cache(msal, token_path)
    app = msal.PublicClientApplication(
        client_id,
        authority=_authority(settings),
        token_cache=cache,
    )
    return app, cache


def acquire_token_silent(config: dict[str, Any], token_path: Path) -> str:
    """A token from the cache, refreshing it if needed. Never prompts."""
    app, cache = build_app(config, token_path)
    accounts = app.get_accounts()
    if not accounts:
        raise GraphAuthError("Not signed in. Run: blink-light.bat calendar login")
    result = app.acquire_token_silent(GRAPH_SCOPES, account=accounts[0])
    _save_cache(cache, token_path)
    if not result or "access_token" not in result:
        raise GraphAuthError(
            "Cached sign-in has expired. Run: blink-light.bat calendar login"
        )
    return result["access_token"]


def sign_in(
    config: dict[str, Any],
    token_path: Path,
    prompt: Callable[[str], None] = print,
    interactive: bool = True,
) -> dict[str, Any]:
    """Interactive sign-in, falling back to device code.

    Interactive is smoother on a desktop with a browser; device code is what
    works over RDP or when no browser can be launched.
    """
    app, cache = build_app(config, token_path)
    result: dict[str, Any] | None = None

    if interactive:
        try:
            result = app.acquire_token_interactive(GRAPH_SCOPES)
        except Exception:
            result = None

    if not result or "access_token" not in result:
        flow = app.initiate_device_flow(scopes=GRAPH_SCOPES)
        if "user_code" not in flow:
            raise GraphAuthError(
                f"Could not start device sign-in: {flow.get('error_description', flow)}"
            )
        prompt(flow["message"])
        result = app.acquire_token_by_device_flow(flow)

    _save_cache(cache, token_path)
    if not result or "access_token" not in result:
        raise GraphAuthError(
            f"Sign-in failed: {result.get('error_description', result) if result else 'no token returned'}"
        )
    return result


def sign_out(config: dict[str, Any], token_path: Path) -> int:
    """Forget every cached account. Returns how many were removed."""
    app, cache = build_app(config, token_path)
    accounts = app.get_accounts()
    for account in accounts:
        app.remove_account(account)
    _save_cache(cache, token_path)
    if not accounts and token_path.exists():
        token_path.unlink()
    return len(accounts)


def signed_in_account(config: dict[str, Any], token_path: Path) -> str | None:
    try:
        app, _ = build_app(config, token_path)
    except GraphAuthError:
        return None
    accounts = app.get_accounts()
    return accounts[0].get("username") if accounts else None


def fetch_graph_events(
    config: dict[str, Any],
    token_path: Path,
    now: datetime,
    lookahead_minutes: int,
    lookback_minutes: int = 5,
    fetcher: Callable[[str, dict[str, str]], dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Raw calendarView rows for the polling window.

    calendarView is the endpoint that expands recurring series server-side, so
    a daily standup arrives as today's occurrence rather than as a master to
    expand here.
    """
    token = acquire_token_silent(config, token_path)
    window_start = now - timedelta(minutes=lookback_minutes)
    window_end = now + timedelta(minutes=lookahead_minutes)
    query = urllib.parse.urlencode(
        {
            "startDateTime": window_start.astimezone().isoformat(),
            "endDateTime": window_end.astimezone().isoformat(),
            "$select": "id,subject,start,end,showAs,isAllDay,isCancelled,responseStatus",
            "$orderby": "start/dateTime",
            "$top": "50",
        }
    )
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        # Always UTC. Graph wants a Windows zone ID here ("Central Standard
        # Time"), but Python on Windows only exposes the display name, which
        # flips to "Central Daylight Time" from March to November - and Graph
        # rejects that with a 400, so the calendar went silent all summer.
        # UTC is accepted everywhere; _graph_datetime converts back to local.
        "Prefer": 'outlook.timezone="UTC"',
    }
    payload = (fetcher or _get_json)(f"{GRAPH_CALENDAR_VIEW}?{query}", headers)
    return payload.get("value", [])


def _get_json(url: str, headers: dict[str, str]) -> dict[str, Any]:
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:  # pragma: no cover - network path
        detail = exc.read().decode("utf-8", errors="replace")[:400]
        raise RuntimeError(f"Graph returned {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:  # pragma: no cover - network path
        raise RuntimeError(f"Could not reach Microsoft Graph: {exc.reason}") from exc


def graph_rows_to_items(rows: list[dict[str, Any]], include_cancelled: bool = False) -> list[dict[str, Any]]:
    """Graph rows -> the same dicts the Outlook COM path produces.

    Both providers converge here, so everything downstream - the status filter,
    the warning windows, the dedupe state - has exactly one shape to handle.
    """
    items: list[dict[str, Any]] = []
    for row in rows:
        if row.get("isCancelled") and not include_cancelled:
            continue
        show_as = row.get("showAs") or "unknown"
        items.append(
            {
                "entry_id": row.get("id") or row.get("subject") or "unknown",
                "subject": row.get("subject") or "(untitled)",
                "start": _graph_datetime(row.get("start")),
                "end": _graph_datetime(row.get("end")),
                "busy_status": SHOW_AS_TO_BUSY_STATUS.get(show_as, OUTLOOK_BUSY),
                "is_all_day": bool(row.get("isAllDay", False)),
            }
        )
    return items


def _graph_datetime(payload: dict[str, Any] | None) -> str:
    """Graph's {dateTime, timeZone} pair as an ISO string the parser accepts."""
    if not payload:
        raise ValueError("Graph event is missing a start or end time.")
    text = payload["dateTime"]
    # Graph pads to 7 fractional digits; fromisoformat takes at most 6.
    if "." in text:
        head, _, fraction = text.partition(".")
        text = f"{head}.{fraction[:6]}"
    zone = payload.get("timeZone", "")
    if zone.upper() == "UTC":
        # fetch_graph_events always asks for UTC. Converting to local keeps
        # Graph events in the same shape as the Outlook provider's, so
        # `calendar upcoming` reads in the zone you are actually in.
        if not text.endswith("Z"):
            text = f"{text}+00:00"
        return datetime.fromisoformat(text).astimezone().isoformat()
    return text
