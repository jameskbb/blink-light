"""Daily light show: a scene played once a day at a fixed wall-clock time.

Deliberately the same shape as the chime - a slot, a catch-up window, and a
state file keyed by slot - so both effects can share one scheduler loop and
neither can double-fire. The only real differences are that the slot recurs
daily instead of hourly, and the payload is a scene rather than a pulse.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from .device import BlinkDeviceController
from .paths import AppPaths, ensure_runtime_dirs
from .rules import is_between_times
from .state import read_json, write_json


def _now() -> datetime:
    return datetime.now().astimezone()


def show_settings(config: dict[str, Any]) -> dict[str, Any]:
    return config.get("show", {})


def show_enabled(config: dict[str, Any]) -> bool:
    return bool(show_settings(config).get("enabled", False))


def show_time(config: dict[str, Any]) -> tuple[int, int]:
    raw = str(show_settings(config).get("at", "17:00"))
    hour_text, minute_text = raw.split(":")
    return int(hour_text), int(minute_text)


def current_slot(now: datetime, at: tuple[int, int]) -> datetime:
    """The most recent scheduled show moment at or before ``now``."""
    hour, minute = at
    slot = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if slot > now:
        slot -= timedelta(days=1)
    return slot


def next_slot(now: datetime, at: tuple[int, int]) -> datetime:
    """The next scheduled show moment strictly after ``now``."""
    hour, minute = at
    slot = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if slot <= now:
        slot += timedelta(days=1)
    return slot


def seconds_until_next_slot(now: datetime, at: tuple[int, int]) -> float:
    return (next_slot(now, at) - now).total_seconds()


def read_show_state(paths: AppPaths) -> dict[str, Any]:
    payload = read_json(paths.show_state_path, {})
    return payload if isinstance(payload, dict) else {}


def record_fire(paths: AppPaths, slot: datetime, now: datetime, source: str) -> dict[str, Any]:
    ensure_runtime_dirs(paths)
    payload = {
        "last_slot": slot.isoformat(),
        "last_fired_at": now.isoformat(),
        "source": source,
    }
    write_json(paths.show_state_path, payload)
    return payload


def should_fire(
    config: dict[str, Any],
    paths: AppPaths,
    now: datetime | None = None,
) -> tuple[bool, str, datetime]:
    """Decide whether today's show is due, returning (due, reason, slot)."""
    current = now or _now()
    at = show_time(config)
    slot = current_slot(current, at)

    if not show_enabled(config):
        return False, "disabled", slot

    settings = show_settings(config)
    window = float(settings.get("catch_up_window_seconds", 300))
    if (current - slot).total_seconds() > window:
        return False, "outside-catch-up-window", slot

    if settings.get("respect_quiet_hours", True):
        quiet_hours = config["settings"]["quiet_hours"]
        if quiet_hours.get("enabled") and is_between_times(current, quiet_hours["start"], quiet_hours["end"]):
            return False, "quiet-hours", slot

    if read_show_state(paths).get("last_slot") == slot.isoformat():
        return False, "already-fired", slot

    return True, "due", slot


def fire_show(
    config: dict[str, Any],
    paths: AppPaths,
    controller_cls=BlinkDeviceController,
    controller=None,
    now: datetime | None = None,
    slot: datetime | None = None,
    source: str = "manual",
    record: bool = True,
) -> dict[str, Any]:
    """Play the show immediately, regardless of whether it is due."""
    current = now or _now()
    target_slot = slot or current_slot(current, show_time(config))
    scene_name = show_settings(config).get("scene", "rainbow_swirl")
    scenes = config["scenes"]
    if scene_name not in scenes:
        raise ValueError(f"Unknown show scene '{scene_name}'.")

    owned = controller is None
    device = controller or controller_cls(serial=config["device"].get("serial"))
    try:
        device.play_scene(scenes[scene_name], persistent=False)
    finally:
        if owned:
            device.close()

    state = record_fire(paths, target_slot, current, source) if record else {}
    return {"fired": True, "scene": scene_name, "slot": target_slot.isoformat(), "state": state}


def maybe_fire_show(
    config: dict[str, Any],
    paths: AppPaths,
    controller_cls=BlinkDeviceController,
    controller=None,
    now: datetime | None = None,
    source: str = "scheduler",
) -> dict[str, Any]:
    """Fire the show only if today's slot is due and unfired."""
    current = now or _now()
    due, reason, slot = should_fire(config, paths, current)
    if not due:
        return {"fired": False, "reason": reason, "slot": slot.isoformat()}
    result = fire_show(
        config,
        paths,
        controller_cls=controller_cls,
        controller=controller,
        now=current,
        slot=slot,
        source=source,
    )
    result["reason"] = reason
    return result


def show_status(config: dict[str, Any], paths: AppPaths, now: datetime | None = None) -> dict[str, Any]:
    from .defaults import scene_duration_seconds

    current = now or _now()
    due, reason, slot = should_fire(config, paths, current)
    at = show_time(config)
    settings = show_settings(config)
    scene_name = settings.get("scene", "rainbow_swirl")
    scene = config["scenes"].get(scene_name)
    return {
        "enabled": show_enabled(config),
        "at": f"{at[0]:02d}:{at[1]:02d}",
        "scene": scene_name,
        "scene_seconds": round(scene_duration_seconds(scene), 2) if scene else None,
        "max_seconds": settings.get("max_seconds"),
        "due_now": due,
        "reason": reason,
        "current_slot": slot.isoformat(),
        "next_slot": next_slot(current, at).isoformat(),
        "seconds_until_next_slot": round(seconds_until_next_slot(current, at), 1),
        "last": read_show_state(paths),
    }
