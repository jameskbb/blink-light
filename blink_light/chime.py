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
import logging
import os
from pathlib import Path
import signal
import subprocess
import tempfile
from typing import Any, Callable

from .device import BlinkDeviceController, DeviceError
from .paths import AppPaths, ensure_runtime_dirs
from .rules import is_between_times
from .state import is_process_running, read_json, remove_file, write_json

TASK_NAME = "BlinkLight Hourly Chime"
AUTOSTART_TASK_NAME = "BlinkLight Autostart"
CHIME_SCRIPT_NAME = "blink-light-chime.vbs"
AUTOSTART_SCRIPT_NAME = "blink-light-autostart.vbs"


LOGGER = logging.getLogger("blink_light.chime")


def _now() -> datetime:
    return datetime.now().astimezone()


def configure_loop_logging(paths: AppPaths) -> logging.Handler:
    """Send loop output to the shared log file.

    The loop runs detached behind wscript with no console, so an unhandled
    error would otherwise surface only as a scheduled-task result code. Anyone
    debugging a silent runner should be able to read what happened instead.

    Returns the handler so the caller can release the file when it is done;
    a handler left attached keeps the log open and would stack up if the loop
    were ever run more than once in one process.
    """
    ensure_runtime_dirs(paths)
    release_loop_logging()
    handler = logging.FileHandler(paths.log_path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [chime] %(message)s"))
    LOGGER.addHandler(handler)
    LOGGER.setLevel(logging.INFO)
    return handler


def release_loop_logging() -> None:
    for handler in list(LOGGER.handlers):
        LOGGER.removeHandler(handler)
        handler.close()


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
        "autostart": autostart_status(paths),
    }


STOP_POLL_SECONDS = 2.0


def _sleep_in_slices(
    paths: AppPaths,
    seconds: float,
    sleep_fn: Callable[[float], None],
    stop_check: Callable[[], bool] | None,
    slice_seconds: float = STOP_POLL_SECONDS,
) -> bool:
    """Sleep ``seconds``, checking for a stop request every slice.

    Returns True if a stop was requested. Without this the loop could sit in a
    60-second sleep while ``stop_chime_loop`` gave up waiting after 8 and
    resorted to killing it, which skips the loop's own cleanup.
    """
    remaining = float(seconds)
    while remaining > 0:
        if paths.chime_stop_path.exists():
            return True
        if stop_check and stop_check():
            return True
        chunk = min(slice_seconds, remaining)
        sleep_fn(chunk)
        remaining -= chunk
    return False


def loop_status(paths: AppPaths) -> dict[str, Any]:
    """Is a chime loop alive? Read from its PID file."""
    pid = None
    if paths.chime_pid_path.exists():
        try:
            pid = int(paths.chime_pid_path.read_text(encoding="utf-8").strip())
        except ValueError:
            pid = None
    running = is_process_running(pid)
    if not running and pid:
        remove_file(paths.chime_pid_path)
    return {"running": running, "pid": pid if running else None}


def stop_chime_loop(paths: AppPaths, timeout_seconds: float = 8.0) -> dict[str, Any]:
    """Ask a running loop to exit, then make sure it actually did.

    Killing the scheduled task's launcher is not enough: schtasks /End stops
    wscript but leaves the cmd and python descendants orphaned. The loop watches
    for a stop file so it can retire itself cleanly instead.
    """
    import time as _time

    status = loop_status(paths)
    if not status["running"]:
        remove_file(paths.chime_stop_path)
        return status

    ensure_runtime_dirs(paths)
    paths.chime_stop_path.write_text(_now().isoformat(), encoding="utf-8")
    deadline = _time.time() + timeout_seconds
    pid = status["pid"]
    while _time.time() < deadline:
        _time.sleep(0.25)
        current = loop_status(paths)
        if not current["running"]:
            remove_file(paths.chime_stop_path)
            return current

    if pid:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    _time.sleep(1.0)
    remove_file(paths.chime_stop_path)
    return loop_status(paths)


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

    Sleeps are capped at 60 seconds so Ctrl+C stays responsive, a clock change
    (sleep/resume, DST) is picked up within a minute, and a stop request is
    honored promptly. Each wake does only date arithmetic and one small JSON
    read unless the slot is actually due.
    """
    import time as _time

    from .alarms import fire_due_alarms, seconds_until_next_alarm
    from .show import maybe_fire_show, seconds_until_next_slot as show_seconds_until, show_time

    sleep_fn = sleep or _time.sleep
    ensure_runtime_dirs(paths)

    configure_loop_logging(paths)

    existing = loop_status(paths)
    if existing["running"] and existing["pid"] != os.getpid():
        LOGGER.error("Refusing to start: loop already running with PID %s", existing["pid"])
        # Bailing out before the main try/finally, so release the log here or
        # the file stays open for the life of the process.
        release_loop_logging()
        raise RuntimeError(f"Chime loop already running with PID {existing['pid']}.")

    remove_file(paths.chime_stop_path)
    paths.chime_pid_path.write_text(str(os.getpid()), encoding="utf-8")
    LOGGER.info("Loop started (PID %s)", os.getpid())
    minute = chime_minute(config)
    at = show_time(config)
    fired = 0
    shows = 0
    alarms = 0
    iterations = 0
    stopped_by = None
    # An undocked laptop is not a fault - the light is simply not there. Each
    # missed slot is retried once a minute through the catch-up window, so a
    # stack trace per attempt buried the real failures under five identical
    # copies. Report an absent device once per hour instead, and keep the full
    # traceback for everything else.
    absent_reported_for: str | None = None

    def log_effect_failure(label: str, error: BaseException, at: datetime) -> None:
        nonlocal absent_reported_for
        if isinstance(error, DeviceError):
            hour = at.replace(minute=0, second=0, microsecond=0).isoformat()
            if absent_reported_for != hour:
                absent_reported_for = hour
                LOGGER.info("%s skipped, device not connected: %s", label, error)
            return
        LOGGER.error("%s failed", label, exc_info=error)

    try:
        while True:
            if paths.chime_stop_path.exists():
                stopped_by = "stop-file"
                break
            if stop_check and stop_check():
                stopped_by = "stop-check"
                break
            if max_iterations is not None and iterations >= max_iterations:
                stopped_by = "max-iterations"
                break
            iterations += 1
            current = now_factory()

            # Both effects are cheap no-ops away from their slot: date math plus
            # one small JSON read each. Adding a third would cost the same.
            # A device error on one effect must not take the loop down; it would
            # take the rest of the day's schedule with it. Log and carry on.
            try:
                if maybe_fire_chime(
                    config,
                    paths,
                    controller_cls=controller_cls,
                    now=current,
                    source="chime-run",
                )["fired"]:
                    fired += 1
                    LOGGER.info("Fired chime")
            except Exception as error:
                log_effect_failure("Chime", error, current)
            try:
                if maybe_fire_show(
                    config,
                    paths,
                    controller_cls=controller_cls,
                    now=current,
                    source="chime-run",
                )["fired"]:
                    shows += 1
                    LOGGER.info("Played show")
            except Exception as error:
                log_effect_failure("Show", error, current)
            try:
                for result in fire_due_alarms(
                    config,
                    paths,
                    controller_cls=controller_cls,
                    now=current,
                    source="chime-run",
                ):
                    alarms += 1
                    LOGGER.info("Fired alarm %s", result["alarm"])
            except Exception as error:
                log_effect_failure("Alarm", error, current)

            after = now_factory()
            candidates = [
                seconds_until_next_slot(after, minute),
                show_seconds_until(after, at),
            ]
            next_alarm = seconds_until_next_alarm(config, after)
            if next_alarm is not None:
                candidates.append(next_alarm)
            remaining = min(candidates)
            # Sleep in slices so a stop request is noticed within a couple of
            # seconds rather than up to a minute later. Only the stop file is
            # re-checked per slice; the effects are still evaluated once per
            # wake, so this costs no extra file reads on the hot path.
            if _sleep_in_slices(
                paths,
                max(1.0, min(60.0, remaining + 0.5)),
                sleep_fn,
                stop_check,
            ):
                stopped_by = "stop-file"
                break
    except KeyboardInterrupt:
        stopped_by = "interrupt"
    except Exception:
        stopped_by = "error"
        LOGGER.exception("Loop exited on an unhandled error")
        raise
    finally:
        remove_file(paths.chime_pid_path)
        remove_file(paths.chime_stop_path)
        LOGGER.info(
            "Loop stopped (%s) after %s wakes, %s chimes, %s shows, %s alarms",
            stopped_by,
            iterations,
            fired,
            shows,
            alarms,
        )
        release_loop_logging()
    return {
        "iterations": iterations,
        "fired": fired,
        "shows": shows,
        "alarms": alarms,
        "stopped_by": stopped_by,
    }


def _script_path(paths: AppPaths, name: str):
    script = paths.project_root / name
    if not script.exists():
        raise RuntimeError(f"Missing launcher script at {script}.")
    return script


def _run_schtasks(arguments: list[str]) -> subprocess.CompletedProcess:
    if os.name != "nt":
        raise RuntimeError("Scheduled task management requires Windows.")
    return subprocess.run(
        ["schtasks", *arguments],
        capture_output=True,
        text=True,
        check=False,
    )


def _allow_task_on_battery(task_name: str) -> bool:
    """Clear the two power conditions schtasks turns on by default.

    A task created with `schtasks /Create` refuses to start on battery and
    stops if the machine unplugs. That is exactly backwards here: undocking is
    when the light is most likely to be missed, and the task simply sits in
    `Queued` forever with no error anywhere. `schtasks` has no flag for either
    setting, so the task is round-tripped through its own XML.

    Best-effort - a failure here costs a chime on battery, not the install.
    """
    exported = _run_schtasks(["/Query", "/TN", task_name, "/XML"])
    if exported.returncode != 0 or not exported.stdout.strip():
        return False
    xml = exported.stdout
    for element in ("DisallowStartIfOnBatteries", "StopIfGoingOnBatteries"):
        xml = xml.replace(f"<{element}>true</{element}>", f"<{element}>false</{element}>")

    with tempfile.NamedTemporaryFile(
        "w", suffix=".xml", encoding="utf-16", delete=False, newline=""
    ) as handle:
        handle.write(xml)
        path = handle.name
    try:
        return _run_schtasks(["/Create", "/TN", task_name, "/XML", path, "/F"]).returncode == 0
    finally:
        remove_file(Path(path))


def _task_status(task_name: str) -> dict[str, Any]:
    if os.name != "nt":
        return {"supported": False, "installed": False, "task_name": task_name}
    try:
        completed = _run_schtasks(["/Query", "/TN", task_name])
    except (OSError, RuntimeError) as exc:  # pragma: no cover - environment dependent
        return {"supported": False, "installed": False, "task_name": task_name, "error": str(exc)}
    return {
        "supported": True,
        "installed": completed.returncode == 0,
        "task_name": task_name,
    }


def _delete_task(task_name: str) -> dict[str, Any]:
    completed = _run_schtasks(["/Delete", "/TN", task_name, "/F"])
    if completed.returncode != 0 and "cannot find" not in (completed.stderr or "").lower():
        raise RuntimeError((completed.stderr or completed.stdout).strip() or "schtasks /Delete failed.")
    return {"installed": False, "task_name": task_name}


def scheduled_task_status() -> dict[str, Any]:
    return _task_status(TASK_NAME)


def autostart_status(paths: AppPaths | None = None) -> dict[str, Any]:
    """Status of the logon-triggered task that keeps the chime loop running.

    The task and the loop are tracked separately on purpose: an orphaned loop
    with no task, or a task whose loop has died, are both states worth seeing.
    """
    payload = _task_status(AUTOSTART_TASK_NAME)
    payload["task_running"] = _autostart_running()
    payload["loop"] = loop_status(paths) if paths is not None else {"running": None, "pid": None}
    return payload


def _autostart_running() -> bool:
    if os.name != "nt":
        return False
    try:
        completed = _run_schtasks(["/Query", "/TN", AUTOSTART_TASK_NAME, "/FO", "LIST"])
    except (OSError, RuntimeError):  # pragma: no cover - environment dependent
        return False
    if completed.returncode != 0:
        return False
    for line in completed.stdout.splitlines():
        if line.lower().startswith("status:"):
            return line.split(":", 1)[1].strip().lower() == "running"
    return False


def _await_loop(paths: AppPaths, timeout_seconds: float = 20.0) -> dict[str, Any]:
    """Wait for a freshly launched loop to write its PID file.

    The chain is wscript -> cmd -> the venv bootstrap -> python, and a cold
    .venv install makes that slow, so this needs a generous window.
    """
    import time as _time

    deadline = _time.time() + timeout_seconds
    while _time.time() < deadline:
        status = loop_status(paths)
        if status["running"]:
            return status
        _time.sleep(0.5)
    return loop_status(paths)


def install_autostart_task(paths: AppPaths, start_now: bool = True) -> dict[str, Any]:
    """Register a logon-triggered task that runs the chime loop for the session.

    Idempotent: any loop already running is retired first, so re-running enable
    replaces the runner instead of stacking a second one beside it. The loop's
    own PID file is the real duplicate guard - Task Scheduler's "do not start a
    new instance" policy only covers launches it knows about, and an orphaned
    loop is by definition one it has lost track of.
    """
    script = _script_path(paths, AUTOSTART_SCRIPT_NAME)

    was_running = loop_status(paths)["running"]
    stop_chime_loop(paths)
    _run_schtasks(["/End", "/TN", AUTOSTART_TASK_NAME])

    completed = _run_schtasks(
        [
            "/Create",
            "/TN",
            AUTOSTART_TASK_NAME,
            "/TR",
            f'wscript.exe "{script}"',
            "/SC",
            "ONLOGON",
            "/F",
        ]
    )
    if completed.returncode != 0:
        raise RuntimeError((completed.stderr or completed.stdout).strip() or "schtasks /Create failed.")
    on_battery = _allow_task_on_battery(AUTOSTART_TASK_NAME)

    started = False
    loop = {"running": False, "pid": None}
    if start_now:
        # schtasks returning 0 only means the launch was accepted. Whether a
        # loop actually came up is a separate question, and the one that
        # matters - reporting success on a runner that died immediately is
        # worse than reporting the failure.
        if _run_schtasks(["/Run", "/TN", AUTOSTART_TASK_NAME]).returncode == 0:
            loop = _await_loop(paths)
            started = loop["running"]

    return {
        "installed": True,
        "task_name": AUTOSTART_TASK_NAME,
        "trigger": "at logon",
        "action": f'wscript.exe "{script}"',
        "started_now": started,
        "runs_on_battery": on_battery,
        "loop": loop,
        "replaced_running_loop": was_running,
    }


def uninstall_autostart_task(paths: AppPaths | None = None) -> dict[str, Any]:
    """Stop the loop first, then drop the task.

    Order matters. schtasks /End only kills the launcher it started; the cmd and
    python descendants survive it. Asking the loop to retire itself through the
    stop file is what actually leaves nothing behind.
    """
    loop = None
    if paths is not None:
        loop = stop_chime_loop(paths)
    _run_schtasks(["/End", "/TN", AUTOSTART_TASK_NAME])
    payload = _delete_task(AUTOSTART_TASK_NAME)
    payload["loop"] = loop if loop is not None else {"running": None, "pid": None}
    return payload


def install_scheduled_task(config: dict[str, Any], paths: AppPaths) -> dict[str, Any]:
    script = _script_path(paths, CHIME_SCRIPT_NAME)
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
        "runs_on_battery": _allow_task_on_battery(TASK_NAME),
        "action": f'wscript.exe "{script}"',
    }


def uninstall_scheduled_task() -> dict[str, Any]:
    return _delete_task(TASK_NAME)
