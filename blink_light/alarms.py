"""Named daily alarms: fire an action at a wall-clock time, once per day.

The daily show already did this for exactly one moment. Rather than bolt on a
second and third hard-coded section, this generalises it: `alarms` is a list, so
"another scheduled moment" costs a config entry instead of a code change.

Slot arithmetic is shared with `show` rather than reimplemented, and each alarm
dedupes on its own key inside one state file, so two alarms two minutes apart do
not interfere.

Weekday filtering exists because most daily alarms are really workday alarms.
Omitting `days` means every day.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Callable

from .device import BlinkDeviceController
from .paths import AppPaths, ensure_runtime_dirs
from .rules import is_between_times
from .show import current_slot, next_slot
from .state import read_json, write_json

# Monday is 0, matching datetime.weekday().
DAY_NAMES = {
    "mon": 0,
    "tue": 1,
    "wed": 2,
    "thu": 3,
    "fri": 4,
    "sat": 5,
    "sun": 6,
}
WEEKDAYS = ["mon", "tue", "wed", "thu", "fri"]


def _now() -> datetime:
    return datetime.now().astimezone()


def alarm_list(config: dict[str, Any]) -> list[dict[str, Any]]:
    alarms = config.get("alarms", [])
    return alarms if isinstance(alarms, list) else []


def enabled_alarms(config: dict[str, Any]) -> list[dict[str, Any]]:
    return [alarm for alarm in alarm_list(config) if alarm.get("enabled", True)]


def find_alarm(config: dict[str, Any], name: str) -> dict[str, Any]:
    for alarm in alarm_list(config):
        if alarm.get("name") == name:
            return alarm
    known = ", ".join(alarm.get("name", "?") for alarm in alarm_list(config)) or "none configured"
    raise ValueError(f"Unknown alarm '{name}'. Known alarms: {known}.")


def parse_at(raw: str) -> tuple[int, int]:
    hour_text, minute_text = str(raw).split(":")
    return int(hour_text), int(minute_text)


def alarm_days(alarm: dict[str, Any]) -> set[int] | None:
    """The weekdays an alarm may fire on, or None for every day."""
    days = alarm.get("days")
    if not days:
        return None
    resolved = set()
    for entry in days:
        key = str(entry).strip().lower()[:3]
        if key in DAY_NAMES:
            resolved.add(DAY_NAMES[key])
    return resolved or None


def read_state(paths: AppPaths) -> dict[str, Any]:
    payload = read_json(paths.alarm_state_path, {})
    return payload if isinstance(payload, dict) else {}


def record_fire(paths: AppPaths, name: str, slot: datetime, now: datetime, source: str) -> dict[str, Any]:
    ensure_runtime_dirs(paths)
    state = read_state(paths)
    state[name] = {
        "last_slot": slot.isoformat(),
        "last_fired_at": now.isoformat(),
        "source": source,
    }
    write_json(paths.alarm_state_path, state)
    return state[name]


def should_fire(
    config: dict[str, Any],
    paths: AppPaths,
    alarm: dict[str, Any],
    now: datetime | None = None,
) -> tuple[bool, str, datetime]:
    """Decide whether one alarm is due, returning (due, reason, slot)."""
    current = now or _now()
    at = parse_at(alarm["at"])
    slot = current_slot(current, at)

    if not alarm.get("enabled", True):
        return False, "disabled", slot

    days = alarm_days(alarm)
    # Checked against the slot, not "now": a 00:05 alarm evaluated at 00:06 must
    # be judged on the day the slot belongs to.
    if days is not None and slot.weekday() not in days:
        return False, "wrong-day", slot

    window = float(alarm.get("catch_up_window_seconds", 300))
    if (current - slot).total_seconds() > window:
        return False, "outside-catch-up-window", slot

    if alarm.get("respect_quiet_hours", True):
        quiet_hours = config["settings"]["quiet_hours"]
        if quiet_hours.get("enabled") and is_between_times(current, quiet_hours["start"], quiet_hours["end"]):
            return False, "quiet-hours", slot

    entry = read_state(paths).get(alarm["name"])
    if isinstance(entry, dict) and entry.get("last_slot") == slot.isoformat():
        return False, "already-fired", slot

    return True, "due", slot


def resolve_action(config: dict[str, Any], alarm: dict[str, Any]) -> dict[str, Any]:
    action = alarm.get("action", {})
    if "preset" in action:
        presets = config["presets"]
        name = action["preset"]
        if name not in presets:
            raise ValueError(f"Alarm '{alarm.get('name')}' refers to unknown preset '{name}'.")
        return dict(presets[name])
    return dict(action)


def fire_alarm(
    config: dict[str, Any],
    paths: AppPaths,
    alarm: dict[str, Any],
    controller_cls=BlinkDeviceController,
    controller=None,
    now: datetime | None = None,
    slot: datetime | None = None,
    source: str = "manual",
    record: bool = True,
) -> dict[str, Any]:
    """Play one alarm immediately, regardless of whether it is due."""
    current = now or _now()
    target_slot = slot or current_slot(current, parse_at(alarm["at"]))
    action = resolve_action(config, alarm)

    owned = controller is None
    device = controller or controller_cls(serial=config["device"].get("serial"))
    try:
        device.apply_action(action, config["scenes"], persistent=False)
    finally:
        if owned:
            device.close()

    state = record_fire(paths, alarm["name"], target_slot, current, source) if record else {}
    return {
        "fired": True,
        "alarm": alarm["name"],
        "action": action,
        "slot": target_slot.isoformat(),
        "state": state,
    }


def fire_due_alarms(
    config: dict[str, Any],
    paths: AppPaths,
    controller_cls=BlinkDeviceController,
    controller=None,
    now: datetime | None = None,
    source: str = "scheduler",
    on_error: Callable[[dict[str, Any], BaseException], None] | None = None,
) -> list[dict[str, Any]]:
    """Fire every alarm that is currently due.

    Returns one result per alarm that actually fired. With ``on_error``, a
    failure on one alarm is handed to it and the rest are still attempted - the
    scheduler needs that, or an unplugged light on the first alarm leaves the
    next one untried and unreported. Without it the first failure raises, so a
    one-shot ``alarm now`` still surfaces the error.
    """
    current = now or _now()
    results = []
    for alarm in alarm_list(config):
        due, _reason, slot = should_fire(config, paths, alarm, current)
        if not due:
            continue
        try:
            result = fire_alarm(
                config,
                paths,
                alarm,
                controller_cls=controller_cls,
                controller=controller,
                now=current,
                slot=slot,
                source=source,
            )
        except Exception as error:
            if on_error is None:
                raise
            on_error(alarm, error)
            continue
        results.append(result)
    return results


def seconds_until_next_alarm(config: dict[str, Any], now: datetime | None = None) -> float | None:
    """Seconds to the soonest upcoming enabled alarm, or None if there are none.

    Day filters are honoured by walking forward a week; an alarm restricted to
    weekdays should not pull the scheduler awake early on a Saturday.
    """
    current = now or _now()
    best = None
    for alarm in enabled_alarms(config):
        try:
            at = parse_at(alarm["at"])
        except (KeyError, ValueError):
            continue
        days = alarm_days(alarm)
        candidate = next_slot(current, at)
        if days is not None:
            for _ in range(7):
                if candidate.weekday() in days:
                    break
                candidate = next_slot(candidate, at)
            else:
                continue
        remaining = (candidate - current).total_seconds()
        if best is None or remaining < best:
            best = remaining
    return best


def alarm_status(config: dict[str, Any], paths: AppPaths, now: datetime | None = None) -> dict[str, Any]:
    current = now or _now()
    state = read_state(paths)
    entries = []
    for alarm in alarm_list(config):
        due, reason, slot = should_fire(config, paths, alarm, current)
        at = parse_at(alarm["at"])
        entries.append(
            {
                "name": alarm.get("name"),
                "at": alarm.get("at"),
                "days": alarm.get("days") or "every day",
                "enabled": alarm.get("enabled", True),
                "action": resolve_action(config, alarm),
                "due_now": due,
                "reason": reason,
                "current_slot": slot.isoformat(),
                "next_slot": next_slot(current, at).isoformat(),
                "last": state.get(alarm.get("name")),
            }
        )
    return {"alarms": entries, "seconds_until_next": seconds_until_next_alarm(config, current)}
