from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
import inspect
import json
from pathlib import Path
import subprocess
from typing import Any

from .defaults import OUTLOOK_BUSY, OUTLOOK_TENTATIVE
from .paths import AppPaths, build_paths
from .state import read_json, write_json


# The two pre-meeting warnings. Keys are what gets written to the dedupe state,
# so renaming one re-arms it for every meeting already on the calendar.
TEN_MINUTE_KEY = "ten_minute"
FIVE_MINUTE_KEY = "five_minute"
TEN_MINUTE_SECONDS = 600
FIVE_MINUTE_SECONDS = 300


@dataclass(frozen=True)
class CalendarEvent:
    entry_id: str
    subject: str
    start: datetime
    end: datetime
    busy_status: int
    is_all_day: bool

    @property
    def event_key(self) -> str:
        return f"{self.entry_id}|{self.start.isoformat()}"

    def to_payload(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["start"] = self.start.isoformat()
        payload["end"] = self.end.isoformat()
        return payload


@dataclass(frozen=True)
class CalendarSnapshot:
    provider: str
    fetched_at: datetime
    events: list[CalendarEvent]
    error: str | None = None


class CalendarCache:
    def __init__(self, config: dict[str, Any], poller=None, paths: AppPaths | None = None):
        self.config = config
        self.poller = poller or poll_calendar
        self.paths = paths
        self._snapshot: CalendarSnapshot | None = None
        self._next_refresh_at: datetime | None = None

    def get(self, now: datetime) -> CalendarSnapshot | None:
        if not calendar_enabled(self.config):
            return None
        if self._snapshot is None or self._next_refresh_at is None or now >= self._next_refresh_at:
            self._snapshot = _call_poller(self.poller, self.config, now, self.paths)
            refresh_after = int(self.config["calendar"].get("poll_seconds", 30))
            self._next_refresh_at = now + timedelta(seconds=refresh_after)
        return self._snapshot


def calendar_enabled(config: dict[str, Any]) -> bool:
    return bool(config.get("calendar", {}).get("enabled"))


def _call_poller(poller, config: dict[str, Any], now: datetime, paths: AppPaths | None):
    """Call a poller, handing it `paths` only if it accepts them.

    The Graph poller needs paths to find the token cache; the simple fakes in
    the tests take (config, now). Inspecting the signature - rather than
    catching TypeError - keeps a genuine TypeError inside a poller visible.
    """
    takes_paths = "paths" in inspect.signature(poller).parameters
    return poller(config, now, paths) if takes_paths else poller(config, now)


def poll_calendar(
    config: dict[str, Any],
    now: datetime | None = None,
    paths: AppPaths | None = None,
) -> CalendarSnapshot:
    current = now or datetime.now().astimezone()
    calendar_config = config["calendar"]
    provider = calendar_config.get("provider", "outlook")
    if provider not in ("outlook", "graph"):
        return CalendarSnapshot(
            provider=provider,
            fetched_at=current,
            events=[],
            error=f"Unsupported provider '{provider}'. Use 'graph' or 'outlook'.",
        )

    try:
        if provider == "graph":
            events = _poll_graph_events(config, paths or build_paths(), current)
        else:
            events = _poll_outlook_events(calendar_config)
        return CalendarSnapshot(provider=provider, fetched_at=current, events=events, error=None)
    except Exception as exc:
        # A snapshot carrying an error, rather than a raise: the watcher keeps
        # the light on its last state instead of dying because Wi-Fi dropped.
        return CalendarSnapshot(provider=provider, fetched_at=current, events=[], error=str(exc))


def _poll_graph_events(config: dict[str, Any], paths: AppPaths, now: datetime) -> list[CalendarEvent]:
    from .graph_calendar import fetch_graph_events, graph_rows_to_items

    calendar_config = config["calendar"]
    rows = fetch_graph_events(
        config,
        paths.graph_token_path,
        now,
        lookahead_minutes=int(calendar_config.get("lookahead_minutes", 15)),
    )
    events = parse_events(graph_rows_to_items(rows))
    if calendar_config.get("ignore_all_day", True):
        events = [event for event in events if not event.is_all_day]
    return events


def _poll_outlook_events(calendar_config: dict[str, Any]) -> list[CalendarEvent]:
    lookahead_minutes = int(calendar_config.get("lookahead_minutes", 15))
    ignore_all_day = "$true" if calendar_config.get("ignore_all_day", True) else "$false"
    script = f"""
$ErrorActionPreference = 'Stop'
$lookbackMinutes = 5
$lookaheadMinutes = {lookahead_minutes}
$ignoreAllDay = {ignore_all_day}
$outlook = New-Object -ComObject Outlook.Application
$namespace = $outlook.GetNamespace('MAPI')
$calendar = $namespace.GetDefaultFolder(9)
$items = $calendar.Items
$items.IncludeRecurrences = $true
$items.Sort('[Start]')
$windowStart = (Get-Date).AddMinutes(-$lookbackMinutes)
$windowEnd = (Get-Date).AddMinutes($lookaheadMinutes)
$fmt = 'MM/dd/yyyy hh:mm tt'
$filter = "[Start] <= '{{0}}' AND [End] >= '{{1}}'" -f $windowEnd.ToString($fmt), $windowStart.ToString($fmt)
$restricted = @($items.Restrict($filter))
$result = @()
foreach ($item in $restricted) {{
  if ($null -eq $item.Start -or $null -eq $item.End) {{ continue }}
  if ($ignoreAllDay -and $item.AllDayEvent) {{ continue }}
  $result += [pscustomobject]@{{
    entry_id = [string]$item.EntryID
    subject = [string]$item.Subject
    start = ([datetime]$item.Start).ToString('o')
    end = ([datetime]$item.End).ToString('o')
    busy_status = [int]$item.BusyStatus
    is_all_day = [bool]$item.AllDayEvent
  }}
}}
$result | Sort-Object start | ConvertTo-Json -Depth 4 -Compress
"""
    completed = subprocess.run(
        ["powershell", "-NoProfile", "-Command", script],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    if completed.returncode != 0:
        stderr = completed.stderr.strip() or completed.stdout.strip() or "Unknown Outlook calendar error."
        raise RuntimeError(stderr)
    raw = completed.stdout.strip()
    if not raw:
        return []
    decoded = json.loads(raw)
    items = decoded if isinstance(decoded, list) else [decoded]
    return parse_events(items)


def parse_events(items: list[dict[str, Any]]) -> list[CalendarEvent]:
    events: list[CalendarEvent] = []
    for item in items:
        events.append(
            CalendarEvent(
                entry_id=item.get("entry_id") or item.get("subject") or "unknown",
                subject=item.get("subject") or "(untitled)",
                start=_as_aware(datetime.fromisoformat(item["start"])),
                end=_as_aware(datetime.fromisoformat(item["end"])),
                busy_status=int(item.get("busy_status", 2)),
                is_all_day=bool(item.get("is_all_day", False)),
            )
        )
    return events


def _as_aware(value: datetime) -> datetime:
    """Give a naive Outlook timestamp the local timezone.

    Outlook COM reports local wall-clock times with no offset, while the
    watcher's clock is timezone-aware. Comparing the two raises TypeError, so
    normalising here - at the one place events enter the program - is what
    keeps every comparison downstream safe.
    """
    return value if value.tzinfo is not None else value.astimezone()


def alert_statuses(config: dict[str, Any]) -> set[int]:
    """Outlook busy statuses the light reacts to. Default: Tentative and Busy."""
    configured = config.get("calendar", {}).get("alert_statuses", [OUTLOOK_TENTATIVE, OUTLOOK_BUSY])
    return {int(value) for value in configured}


def alertable_events(events: list[CalendarEvent], statuses: set[int]) -> list[CalendarEvent]:
    """Drop the events that should never move the light - Free, Out of Office.

    Filtering once, here, is what keeps the rest of this module honest: every
    decision below is made over a list that already contains only meetings you
    are expected at, so there is no path that can colour or flash for a Free or
    an Out-of-Office block.
    """
    return [event for event in events if event.busy_status in statuses]


def choose_active_event(events: list[CalendarEvent], now: datetime) -> CalendarEvent | None:
    active = [event for event in events if event.start <= now < event.end]
    if not active:
        return None
    active.sort(key=lambda event: (event.start, event.end))
    return active[0]


def choose_next_event(events: list[CalendarEvent], now: datetime) -> CalendarEvent | None:
    future = [event for event in events if event.start > now]
    if not future:
        return None
    future.sort(key=lambda event: event.start)
    return future[0]


def evaluate_calendar_action(
    config: dict[str, Any],
    paths: AppPaths,
    now: datetime,
    snapshot: CalendarSnapshot | None = None,
    poller=None,
) -> dict[str, Any] | None:
    if not calendar_enabled(config):
        return None

    current_snapshot = snapshot or _call_poller(poller or poll_calendar, config, now, paths)
    if current_snapshot.error:
        return None

    events = alertable_events(current_snapshot.events, alert_statuses(config))
    current_event = choose_active_event(events, now)
    if current_event is not None:
        return {
            "source": "calendar",
            "detail": "active_busy",
            "action": {"color": config["calendar"]["busy_meeting_color"], "fade_ms": 150},
            "calendar": _calendar_payload(current_snapshot, current_event),
        }

    next_event = choose_next_event(events, now)
    if next_event is None:
        return {
            "source": "calendar",
            "detail": "available",
            "action": {"color": config["calendar"]["available_color"], "fade_ms": 150},
            "calendar": _calendar_payload(current_snapshot, None),
        }

    alert_state = _load_calendar_state(paths.calendar_state_path)
    seconds_until = max(0, int((next_event.start - now).total_seconds()))
    # Each warning owns a window, not just an upper bound. Without the lower
    # bound on the ten-minute one, a watcher that starts four minutes before a
    # meeting would fire the five-minute warning and then, on the next tick,
    # fire the ten-minute warning too - saying something that is no longer true.
    if seconds_until <= FIVE_MINUTE_SECONDS and not _alert_seen(alert_state, next_event, FIVE_MINUTE_KEY):
        return {
            "source": "calendar",
            "detail": "meeting_in_5m",
            "action": _warning_action(config["calendar"]["five_minute_warning"]),
            "calendar": _calendar_payload(current_snapshot, next_event),
            "_calendar_ack": {"event": next_event.to_payload(), "alert": FIVE_MINUTE_KEY},
        }
    if (
        FIVE_MINUTE_SECONDS < seconds_until <= TEN_MINUTE_SECONDS
        and not _alert_seen(alert_state, next_event, TEN_MINUTE_KEY)
    ):
        return {
            "source": "calendar",
            "detail": "meeting_in_10m",
            "action": _warning_action(config["calendar"]["ten_minute_warning"]),
            "calendar": _calendar_payload(current_snapshot, next_event),
            "_calendar_ack": {"event": next_event.to_payload(), "alert": TEN_MINUTE_KEY},
        }
    return {
        "source": "calendar",
        "detail": "available",
        "action": {"color": config["calendar"]["available_color"], "fade_ms": 150},
        "calendar": _calendar_payload(current_snapshot, next_event),
    }


def acknowledge_calendar_result(paths: AppPaths, result: dict[str, Any], now: datetime | None = None) -> None:
    metadata = result.get("_calendar_ack")
    if not metadata:
        return
    current = now or datetime.now().astimezone()
    state = _load_calendar_state(paths.calendar_state_path)
    event_payload = metadata["event"]
    event_key = f'{event_payload["entry_id"]}|{event_payload["start"]}'
    events = state.setdefault("events", {})
    event_state = events.setdefault(
        event_key,
        {"subject": event_payload["subject"], "end": event_payload["end"], "alerts": {}},
    )
    event_state["alerts"][metadata["alert"]] = current.isoformat()
    _prune_calendar_state(state, current)
    write_json(paths.calendar_state_path, state)


def _warning_action(warning: dict[str, Any]) -> dict[str, Any]:
    return {
        "flash": {
            "color": warning["color"],
            "secondary_color": "#000000",
            "on_ms": int(warning["on_ms"]),
            "off_ms": int(warning["off_ms"]),
            "count": int(warning["count"]),
        }
    }


def _calendar_payload(snapshot: CalendarSnapshot, event: CalendarEvent | None) -> dict[str, Any]:
    payload = {
        "provider": snapshot.provider,
        "fetched_at": snapshot.fetched_at.isoformat(),
        "error": snapshot.error,
    }
    if event is not None:
        payload["event"] = event.to_payload()
    return payload


def _load_calendar_state(path: Path) -> dict[str, Any]:
    payload = read_json(path, {"events": {}})
    if not isinstance(payload, dict):
        return {"events": {}}
    payload.setdefault("events", {})
    return payload


def _alert_seen(state: dict[str, Any], event: CalendarEvent, alert_key: str) -> bool:
    event_state = state.get("events", {}).get(event.event_key, {})
    alerts = event_state.get("alerts", {})
    return alert_key in alerts


def _prune_calendar_state(state: dict[str, Any], now: datetime) -> None:
    events = state.get("events", {})
    remove_keys = []
    for key, payload in events.items():
        end_text = payload.get("end")
        if not end_text:
            continue
        try:
            end_time = datetime.fromisoformat(end_text)
        except ValueError:
            remove_keys.append(key)
            continue
        if end_time < now - timedelta(hours=12):
            remove_keys.append(key)
    for key in remove_keys:
        events.pop(key, None)
