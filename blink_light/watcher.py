from __future__ import annotations

from datetime import datetime
import json
import logging
import os
import signal
import subprocess
import sys
import time
from typing import Any, Callable

from .chime import maybe_fire_chime
from .device import BlinkDeviceController
from .calendar_source import (
    CalendarCache,
    acknowledge_calendar_result,
    evaluate_calendar_action,
)
from .paths import AppPaths, ensure_runtime_dirs
from .rules import first_matching_rule, is_between_times
from .state import is_process_running, read_json, remove_file, write_json
from .system_state import collect_system_snapshot
from .timers import TimerSnapshot, refresh_timer_state


def _now() -> datetime:
    return datetime.now().astimezone()


def _heartbeat_running(state: dict[str, Any], now: datetime | None = None, heartbeat_seconds: int = 15) -> tuple[bool, int | None]:
    current = now or _now()
    pid = state.get("pid")
    updated_at = state.get("updated_at")
    if not pid or not updated_at:
        return False, None
    try:
        updated = datetime.fromisoformat(updated_at)
    except ValueError:
        return False, None
    age = (current - updated).total_seconds()
    if age <= heartbeat_seconds:
        return True, int(pid)
    return False, int(pid)


def load_override(paths: AppPaths, now: datetime | None = None) -> dict[str, Any] | None:
    current = now or _now()
    payload = read_json(paths.override_path, None)
    if not payload:
        return None
    expires_at = payload.get("expires_at")
    if expires_at and datetime.fromisoformat(expires_at) <= current:
        remove_file(paths.override_path)
        return None
    return payload


def resolve_action(action: dict[str, Any], config: dict[str, Any], seen: set[str] | None = None) -> dict[str, Any]:
    seen = seen or set()
    if "preset" not in action:
        return dict(action)
    preset_name = action["preset"]
    if preset_name in seen:
        raise ValueError(f"Preset cycle detected at '{preset_name}'.")
    presets = config["presets"]
    if preset_name not in presets:
        raise ValueError(f"Unknown preset '{preset_name}'.")
    seen.add(preset_name)
    return resolve_action(presets[preset_name], config, seen)


def _timer_payload(snapshot: TimerSnapshot) -> dict[str, Any]:
    return {
        "exists": snapshot.exists,
        "name": snapshot.name,
        "active": snapshot.timer_active,
        "paused": snapshot.paused,
        "completed": snapshot.completed,
        "phase": snapshot.phase_name,
        "remaining_seconds": snapshot.remaining_seconds,
    }


def determine_action(
    config: dict[str, Any],
    paths: AppPaths,
    snapshot_factory: Callable[[datetime | None], Any] = collect_system_snapshot,
    now: datetime | None = None,
    calendar_snapshot=None,
    calendar_poller=None,
) -> dict[str, Any]:
    current = now or _now()
    override = load_override(paths, current)
    timer_snapshot = refresh_timer_state(paths.timer_path, current)

    if override:
        resolved = resolve_action(override["action"], config)
        return {
            "source": "override",
            "detail": override.get("reason") or "manual",
            "action": resolved,
            "timer": _timer_payload(timer_snapshot),
        }

    calendar_result = evaluate_calendar_action(
        config,
        paths,
        current,
        snapshot=calendar_snapshot,
        poller=calendar_poller,
    )
    if calendar_result is not None:
        calendar_result["timer"] = _timer_payload(timer_snapshot)
        return calendar_result

    if timer_snapshot.timer_active and timer_snapshot.action:
        resolved = resolve_action(timer_snapshot.action, config)
        return {
            "source": "timer",
            "detail": timer_snapshot.phase_name,
            "action": resolved,
            "timer": _timer_payload(timer_snapshot),
        }

    if timer_snapshot.completed and timer_snapshot.completion_action:
        resolved = resolve_action(timer_snapshot.completion_action, config)
        return {
            "source": "timer",
            "detail": "completed",
            "action": resolved,
            "timer": _timer_payload(timer_snapshot),
        }

    snapshot = snapshot_factory(current)
    matched_rule = first_matching_rule(config["rules"], snapshot, timer_snapshot, current)
    if matched_rule:
        resolved = resolve_action(matched_rule["action"], config)
        return {
            "source": "rule",
            "detail": matched_rule["name"],
            "action": resolved,
            "timer": _timer_payload(timer_snapshot),
        }

    quiet_hours = config["settings"]["quiet_hours"]
    if quiet_hours.get("enabled") and is_between_times(current, quiet_hours["start"], quiet_hours["end"]):
        resolved = resolve_action(quiet_hours["action"], config)
        return {
            "source": "quiet_hours",
            "detail": f'{quiet_hours["start"]}-{quiet_hours["end"]}',
            "action": resolved,
            "timer": _timer_payload(timer_snapshot),
        }

    resolved = resolve_action(config["settings"]["default_action"], config)
    return {
        "source": "default",
        "detail": "settings.default_action",
        "action": resolved,
        "timer": _timer_payload(timer_snapshot),
    }


def watch_status(paths: AppPaths) -> dict[str, Any]:
    state = read_json(paths.watcher_state_path, {})
    pid = None
    if paths.watcher_pid_path.exists():
        try:
            pid = int(paths.watcher_pid_path.read_text(encoding="utf-8").strip())
        except ValueError:
            pid = None
    if pid is None:
        state_pid = state.get("pid")
        if isinstance(state_pid, int):
            pid = state_pid
    running = is_process_running(pid)
    if not running:
        heartbeat_running, heartbeat_pid = _heartbeat_running(state)
        if heartbeat_running:
            running = True
            pid = heartbeat_pid
    if not running and pid:
        remove_file(paths.watcher_pid_path)
    return {"running": running, "pid": pid if running else None, "state": state}


def apply_once(
    config: dict[str, Any],
    paths: AppPaths,
    controller_cls=BlinkDeviceController,
    snapshot_factory: Callable[[datetime | None], Any] = collect_system_snapshot,
    now: datetime | None = None,
    persistent: bool = True,
) -> dict[str, Any]:
    result = determine_action(config, paths, snapshot_factory=snapshot_factory, now=now)
    controller = controller_cls(serial=config["device"].get("serial"))
    try:
        controller.apply_action(result["action"], config["scenes"], persistent=persistent)
        acknowledge_calendar_result(paths, result, now=now)
    finally:
        controller.close()
    return result


def run_watch_loop(
    config: dict[str, Any],
    paths: AppPaths,
    controller_cls=BlinkDeviceController,
    snapshot_factory: Callable[[datetime | None], Any] = collect_system_snapshot,
    now_factory: Callable[[], datetime] = _now,
    background: bool = False,
) -> dict[str, Any]:
    del background
    ensure_runtime_dirs(paths)
    existing = watch_status(paths)
    if existing["running"] and existing["pid"] != os.getpid():
        raise RuntimeError(f"Watcher already running with PID {existing['pid']}.")

    logging.basicConfig(
        filename=paths.log_path,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    controller = controller_cls(serial=config["device"].get("serial"))
    calendar_cache = CalendarCache(config, paths=paths)
    paths.watcher_pid_path.write_text(str(os.getpid()), encoding="utf-8")
    remove_file(paths.watcher_stop_path)
    last_signature = None
    try:
        while True:
            if paths.watcher_stop_path.exists():
                break
            current = now_factory()
            try:
                result = determine_action(
                    config,
                    paths,
                    snapshot_factory=snapshot_factory,
                    now=current,
                    calendar_snapshot=calendar_cache.get(current),
                )
            except Exception:
                # One bad tick - a malformed calendar entry, a transient COM
                # failure - used to kill the loop and take the light with it.
                # Log it and try again next tick; the light keeps showing
                # whatever it was showing.
                logging.exception("Tick failed; keeping the previous action")
                time.sleep(float(config["settings"]["tick_seconds"]))
                continue
            signature = json.dumps(result["action"], sort_keys=True)
            if signature != last_signature:
                controller.apply_action(result["action"], config["scenes"], persistent=True)
                acknowledge_calendar_result(paths, result, now=current)
                last_signature = signature
            chime_result = maybe_fire_chime(config, paths, controller=controller, now=current)
            if chime_result["fired"]:
                # The chime leaves the LED wherever the pulse ended, so drop the
                # cached signature and let the next tick repaint the real state.
                last_signature = None
                logging.info("Fired hourly chime for slot %s", chime_result["slot"])
            controller.enable_watchdog(int(config["settings"]["watchdog_millis"]))
            write_json(
                paths.watcher_state_path,
                {
                    "pid": os.getpid(),
                    "updated_at": current.isoformat(),
                    "source": result["source"],
                    "detail": result["detail"],
                    "action": result["action"],
                    "calendar": result.get("calendar"),
                    "chime": chime_result,
                    "timer": result["timer"],
                },
            )
            logging.info("Applied action from %s:%s -> %s", result["source"], result["detail"], result["action"])
            time.sleep(float(config["settings"]["tick_seconds"]))
    except KeyboardInterrupt:
        logging.info("Watcher interrupted")
    finally:
        try:
            controller.disable_watchdog()
        except Exception:
            logging.exception("Failed disabling watchdog")
        if config["settings"].get("stop_turns_light_off", True):
            try:
                controller.off()
            except Exception:
                logging.exception("Failed turning blink(1) off")
        controller.close()
        remove_file(paths.watcher_pid_path)
        remove_file(paths.watcher_stop_path)
        write_json(
            paths.watcher_state_path,
            {
                "pid": None,
                "updated_at": now_factory().isoformat(),
                "running": False,
                "stopped": True,
            },
        )
    return watch_status(paths)


def start_background_watch(paths: AppPaths) -> dict[str, Any]:
    ensure_runtime_dirs(paths)
    status = watch_status(paths)
    if status["running"]:
        return status
    command = [
        sys.executable,
        "-m",
        "blink_light",
        "--config",
        str(paths.config_path),
        "watch",
        "run",
        "--background",
    ]
    creationflags = 0
    startupinfo = None
    if os.name == "nt":
        creationflags = (
            subprocess.CREATE_NEW_PROCESS_GROUP
            | subprocess.DETACHED_PROCESS
            | subprocess.CREATE_NO_WINDOW
        )
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    subprocess.Popen(
        command,
        cwd=str(paths.project_root),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        creationflags=creationflags,
        startupinfo=startupinfo,
    )
    for _ in range(20):
        time.sleep(0.25)
        status = watch_status(paths)
        if status["running"]:
            return status
    status["error"] = f"Watcher did not stay running. Check {paths.log_path} for details."
    return status


def stop_watch(paths: AppPaths, timeout_seconds: float = 8.0) -> dict[str, Any]:
    status = watch_status(paths)
    if not status["running"]:
        remove_file(paths.watcher_stop_path)
        return status
    paths.watcher_stop_path.write_text(_now().isoformat(), encoding="utf-8")
    deadline = time.time() + timeout_seconds
    pid = status["pid"]
    while time.time() < deadline:
        time.sleep(0.25)
        current = watch_status(paths)
        if not current["running"]:
            return current
    if pid:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    time.sleep(1.0)
    return watch_status(paths)
