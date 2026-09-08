from __future__ import annotations

from datetime import datetime, time
from pathlib import Path
from typing import Any

from .system_state import SystemSnapshot
from .timers import TimerSnapshot


def _parse_time(value: str) -> time:
    hour_text, minute_text = value.split(":")
    return time(hour=int(hour_text), minute=int(minute_text))


def is_between_times(now: datetime, start_text: str, end_text: str) -> bool:
    current = now.timetz().replace(tzinfo=None)
    start = _parse_time(start_text)
    end = _parse_time(end_text)
    if start <= end:
        return start <= current <= end
    return current >= start or current <= end


def conditions_match(
    conditions: dict[str, Any],
    snapshot: SystemSnapshot,
    timer_snapshot: TimerSnapshot,
    now: datetime,
) -> bool:
    for key, value in conditions.items():
        if key == "idle_seconds_gte":
            if snapshot.idle_seconds is None or snapshot.idle_seconds < float(value):
                return False
        elif key == "process_running_any":
            expected = {item.lower() for item in value}
            if snapshot.process_names.isdisjoint(expected):
                return False
        elif key == "file_exists":
            if not Path(value).exists():
                return False
        elif key == "file_contains":
            path = Path(value["path"])
            if not path.exists():
                return False
            haystack = path.read_text(encoding="utf-8", errors="ignore")
            if value["text"] not in haystack:
                return False
        elif key == "time_between":
            if not is_between_times(now, value["start"], value["end"]):
                return False
        elif key == "battery_below_percent":
            if snapshot.battery_percent is None or snapshot.battery_percent > float(value):
                return False
        elif key == "charging":
            if snapshot.charging is None or snapshot.charging is not value:
                return False
        elif key == "timer_active":
            if timer_snapshot.timer_active is not value:
                return False
        elif key == "routine_phase":
            phases = value if isinstance(value, list) else [value]
            if timer_snapshot.phase_name not in phases:
                return False
        else:  # pragma: no cover - guarded by config validation
            return False
    return True


def first_matching_rule(
    rules: list[dict[str, Any]],
    snapshot: SystemSnapshot,
    timer_snapshot: TimerSnapshot,
    now: datetime,
) -> dict[str, Any] | None:
    for rule in rules:
        if rule.get("enabled", True) is False:
            continue
        if conditions_match(rule["when"], snapshot, timer_snapshot, now):
            return rule
    return None
