"""Top-of-the-hour chime: one white pulse, once per hour.

The chime is deliberately independent of the watcher. It can be driven three
ways and all three share the same dedupe state file, so a chime never fires
twice for the same hour no matter how many drivers are active:

* ``blink-light.bat chime now``  - one-shot, used by the Windows scheduled task
* ``blink-light.bat chime run``  - foreground loop that sleeps until each slot
* the background watcher loop    - fires the chime between watcher ticks
"""

from __future__ import annotations

from datetime import datetime, timedelta
import os
import subprocess
from typing import Any, Callable

from .device import BlinkDeviceController
from .paths import AppPaths, ensure_runtime_dirs
from .rules import is_between_times
from .state import read_json, write_json

TASK_NAME = "BlinkLight Hourly Chime"
CHIME_SCRIPT_NAME = "blink-light-chime.vbs"


def _now() -> datetime:
    return datetime.now().astimezone()


def chime_settings(config: dict[str, Any]) -> dict[str, Any]:
    return config.get("chime", {})


def chime_enabled(config: dict[str, Any]) -> bool:
    return bool(chime_settings(config).get("enabled", False))


def chime_minute(config: dict[str, Any]) -> int:
    return int(chime_settings(config).get("minute", 0))


def chime_action(config: dict[str, Any]) -> dict[str, Any]:
    """Build the one-shot device action for a chime.

    A blink(1) "pulse" is a fade up to the chime color followed by a fade back
    to the secondary color, so a count of 1 reads as a single breath of light.
    """
    settings = chime_settings(config)
    return {
        "pulse": {
            "color": settings.get("color", "#FFFFFF"),
            "secondary_color": settings.get("secondary_color", "#000000"),
            "on_ms": int(settings.get("on_ms", 400)),
            "off_ms": int(settings.get("off_ms", 400)),
            "count": int(settings.get("count", 1)),
        }
    }


def current_slot(now: datetime, minute: int = 0) -> datetime:
    """The most recent scheduled chime moment at or before ``now``."""
    slot = now.replace(minute=minute, second=0, microsecond=0)
    if slot > now:
        slot -= timedelta(hours=1)
    return slot


def next_slot(now: datetime, minute: int = 0) -> datetime:
    """The next scheduled chime moment strictly after ``now``."""
    slot = now.replace(minute=minute, second=0, microsecond=0)
    if slot <= now:
        slot += timedelta(hours=1)
    return slot


def seconds_until_next_slot(now: datetime, minute: int = 0) -> float:
    return (next_slot(now, minute) - now).total_seconds()


def read_chime_state(paths: AppPaths) -> dict[str, Any]:
    payload = read_json(paths.chime_state_path, {})
    return payload if isinstance(payload, dict) else {}


def record_fire(paths: AppPaths, slot: datetime, now: datetime, source: str) -> dict[str, Any]:
    ensure_runtime_dirs(paths)
    payload = {
        "last_slot": slot.isoformat(),
        "last_fired_at": now.isoformat(),
        "source": source,
    }
    write_json(paths.chime_state_path, payload)
    return payload


def should_fire(
    config: dict[str, Any],
    paths: AppPaths,
    now: datetime | None = None,
) -> tuple[bool, str, datetime]:
    """Decide whether the chime is due, returning (due, reason, slot)."""
    current = now or _now()
    minute = chime_minute(config)
    slot = current_slot(current, minute)

    if not chime_enabled(config):
        return False, "disabled", slot

    settings = chime_settings(config)
    window = float(settings.get("catch_up_window_seconds", 300))
    late_by = (current - slot).total_seconds()
    if late_by > window:
        return False, "outside-catch-up-window", slot

    if settings.get("respect_quiet_hours", True):
        quiet_hours = config["settings"]["quiet_hours"]
        if quiet_hours.get("enabled") and is_between_times(current, quiet_hours["start"], quiet_hours["end"]):
            return False, "quiet-hours", slot

    if read_chime_state(paths).get("last_slot") == slot.isoformat():
        return False, "already-fired", slot

    return True, "due", slot


def fire_chime(
    config: dict[str, Any],
    paths: AppPaths,
    controller_cls=BlinkDeviceController,
    controller=None,
    now: datetime | None = None,
    slot: datetime | None = None,
    source: str = "manual",
    record: bool = True,
) -> dict[str, Any]:
    """Play the chime immediately, regardless of whether it is due."""
    current = now or _now()
    target_slot = slot or current_slot(current, chime_minute(config))
    action = chime_action(config)

    owned = controller is None
    device = controller or controller_cls(serial=config["device"].get("serial"))
    try:
        device.apply_action(action, config["scenes"], persistent=False)
    finally:
        if owned:
            device.close()

    state = record_fire(paths, target_slot, current, source) if record else {}
    return {"fired": True, "action": action, "slot": target_slot.isoformat(), "state": state}


def maybe_fire_chime(
    config: dict[str, Any],
    paths: AppPaths,
    controller_cls=BlinkDeviceController,
    controller=None,
    now: datetime | None = None,
    source: str = "watcher",
) -> dict[str, Any]:
    """Fire the chime only if this hour's slot is due and unfired."""
    current = now or _now()
    due, reason, slot = should_fire(config, paths, current)
    if not due:
        return {"fired": False, "reason": reason, "slot": slot.isoformat()}
    result = fire_chime(
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


def chime_status(config: dict[str, Any], paths: AppPaths, now: datetime | None = None) -> dict[str, Any]:
    current = now or _now()
    due, reason, slot = should_fire(config, paths, current)
    minute = chime_minute(config)
    return {
        "enabled": chime_enabled(config),
        "action": chime_action(config),
        "minute": minute,
        "due_now": due,
        "reason": reason,
        "current_slot": slot.isoformat(),
        "next_slot": next_slot(current, minute).isoformat(),
        "seconds_until_next_slot": round(seconds_until_next_slot(current, minute), 1),
        "last": read_chime_state(paths),
        "scheduled_task": scheduled_task_status(),
    }


def run_chime_loop(
    config: dict[str, Any],
    paths: AppPaths,
    controller_cls=BlinkDeviceController,
    now_factory: Callable[[], datetime] = _now,
    sleep: Callable[[float], None] | None = None,
    max_iterations: int | None = None,
    stop_check: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Foreground runner: sleep until each slot, chime, repeat.

    Sleeps are capped at 60 seconds so Ctrl+C stays responsive and a clock
    change (sleep/resume, DST) is picked up within a minute.
    """
    import time as _time

    sleep_fn = sleep or _time.sleep
    ensure_runtime_dirs(paths)
    minute = chime_minute(config)
    fired = 0
    iterations = 0
    try:
        while True:
            if stop_check and stop_check():
                break
            if max_iterations is not None and iterations >= max_iterations:
                break
            iterations += 1
            current = now_factory()
            result = maybe_fire_chime(
                config,
                paths,
                controller_cls=controller_cls,
                now=current,
                source="chime-run",
            )
            if result["fired"]:
                fired += 1
            remaining = seconds_until_next_slot(now_factory(), minute)
            sleep_fn(max(1.0, min(60.0, remaining + 0.5)))
    except KeyboardInterrupt:
        pass
    return {"iterations": iterations, "fired": fired}


def _chime_script_path(paths: AppPaths):
    return paths.project_root / CHIME_SCRIPT_NAME


def _run_schtasks(arguments: list[str]) -> subprocess.CompletedProcess:
    if os.name != "nt":
        raise RuntimeError("Scheduled task management requires Windows.")
    return subprocess.run(
        ["schtasks", *arguments],
        capture_output=True,
        text=True,
        check=False,
    )


def scheduled_task_status() -> dict[str, Any]:
    if os.name != "nt":
        return {"supported": False, "installed": False, "task_name": TASK_NAME}
    try:
        completed = _run_schtasks(["/Query", "/TN", TASK_NAME])
    except (OSError, RuntimeError) as exc:  # pragma: no cover - environment dependent
        return {"supported": False, "installed": False, "task_name": TASK_NAME, "error": str(exc)}
    return {
        "supported": True,
        "installed": completed.returncode == 0,
        "task_name": TASK_NAME,
    }


def install_scheduled_task(config: dict[str, Any], paths: AppPaths) -> dict[str, Any]:
    script = _chime_script_path(paths)
    if not script.exists():
        raise RuntimeError(f"Missing chime launcher at {script}.")
    minute = chime_minute(config)
    completed = _run_schtasks(
        [
            "/Create",
            "/TN",
            TASK_NAME,
            "/TR",
            f'wscript.exe "{script}"',
            "/SC",
            "HOURLY",
            "/ST",
            f"00:{minute:02d}",
            "/F",
        ]
    )
    if completed.returncode != 0:
        raise RuntimeError((completed.stderr or completed.stdout).strip() or "schtasks /Create failed.")
    return {
        "installed": True,
        "task_name": TASK_NAME,
        "runs_at_minute": minute,
        "action": f'wscript.exe "{script}"',
    }


def uninstall_scheduled_task() -> dict[str, Any]:
    completed = _run_schtasks(["/Delete", "/TN", TASK_NAME, "/F"])
    if completed.returncode != 0 and "cannot find" not in (completed.stderr or "").lower():
        raise RuntimeError((completed.stderr or completed.stdout).strip() or "schtasks /Delete failed.")
    return {"installed": False, "task_name": TASK_NAME}
