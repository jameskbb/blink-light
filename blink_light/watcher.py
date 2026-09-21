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
from .device import BlinkDeviceController, DeviceError
from .calendar_source import (
    CalendarCache,
    acknowledge_calendar_result,
    evaluate_calendar_action,
)
from .log_file import close_log, open_log
from .paths import AppPaths, ensure_runtime_dirs
from .rules import first_matching_rule, is_between_times
from .slot_lock import single_instance
from .startup import startup_status
from .state import is_process_running, read_json, remove_file, write_json
from .system_state import collect_system_snapshot
from .timers import TimerSnapshot, refresh_timer_state


LOGGER = logging.getLogger("blink_light.watcher")


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


def heartbeat_age_seconds(state: dict[str, Any], now: datetime | None = None) -> float | None:
    """How long since the watcher last wrote its state, or None if it never has.

    A monitor wants this even when the process is up: a watcher whose ticks
    have stalled is as blind as one that exited, and the pid alone cannot tell
    you which you have.
    """
    updated_at = state.get("updated_at")
    if not updated_at:
        return None
    try:
        updated = datetime.fromisoformat(updated_at)
    except ValueError:
        return None
    return round(((now or _now()) - updated).total_seconds(), 1)


def watch_paused(paths: AppPaths) -> bool:
    return paths.watcher_paused_path.exists()


def watch_status(paths: AppPaths, now: datetime | None = None) -> dict[str, Any]:
    state = read_json(paths.watcher_state_path, {})
    pid = None
    if paths.watcher_pid_path.exists():
        try:
            pid = int(paths.watcher_pid_path.read_text(encoding="utf-8").strip())
        except (ValueError, OSError):
            # Same race as the loop's: a watcher exiting deletes this while the
            # supervisor reads it, and Windows reports that as a sharing
            # violation. Unreadable means "no pid", not "crash the caller" -
            # and here the caller is the thing that restarts the watcher.
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
    return {
        "running": running,
        "pid": pid if running else None,
        "paused": watch_paused(paths),
        "heartbeat_age_seconds": heartbeat_age_seconds(state, now),
        "state": state,
    }


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
    """Run the watcher, but only if no other watcher is already running.

    A pid-file check cannot do this on its own, and one used to be all there
    was. Two starts that land together both read the pid file before either has
    written one, so both pass it and both go on to drive the same USB device.
    Two processes writing pattern lines to one blink(1) interleave, and a tick
    that fails on the contention skips re-arming the device watchdog at the end
    of the tick. The lock is the claim, so exactly one process can hold it
    however many try.

    A loser returns the running watcher's status rather than raising: it is not
    an error for a supervisor, a logon task and a hand-typed `watch start` to
    all decide the watcher should be up.
    """
    ensure_runtime_dirs(paths)
    with single_instance(paths.watcher_lock_path) as only_instance:
        if not only_instance:
            open_log(LOGGER, paths.log_path, "watcher")
            LOGGER.info("Watcher already running; this one is standing down")
            close_log(LOGGER)
            return watch_status(paths)
        return _run_watch_loop(
            config,
            paths,
            controller_cls=controller_cls,
            snapshot_factory=snapshot_factory,
            now_factory=now_factory,
            background=background,
        )


def _run_watch_loop(
    config: dict[str, Any],
    paths: AppPaths,
    controller_cls=BlinkDeviceController,
    snapshot_factory: Callable[[datetime | None], Any] = collect_system_snapshot,
    now_factory: Callable[[], datetime] = _now,
    background: bool = False,
) -> dict[str, Any]:
    del background
    ensure_runtime_dirs(paths)
    open_log(LOGGER, paths.log_path, "watcher")
    # No pid check here any more. Holding the lock is the proof that this is the
    # only watcher, and the old check actively got in the way: `watch_status`
    # calls a watcher running for 15 seconds after its last heartbeat, so a
    # stop immediately followed by a start refused itself for a quarter of a
    # minute - and a killed watcher, which writes no final heartbeat, refused
    # its own replacement. The state it left behind describes a process that is
    # provably gone, because we hold what it would have been holding.

    controller = controller_cls(serial=config["device"].get("serial"))
    calendar_cache = CalendarCache(config, paths=paths)
    paths.watcher_pid_path.write_text(str(os.getpid()), encoding="utf-8")
    remove_file(paths.watcher_stop_path)
    # However it was launched, a watcher that is running is not paused.
    remove_file(paths.watcher_paused_path)
    LOGGER.info("Watcher started (PID %s)", os.getpid())
    last_signature = None
    stopped_by = None
    # A fault that persists fails every 5-second tick, and a traceback per tick
    # is the same flood as an action line per tick. Keep the traceback that
    # starts a run of failures, one an hour while it lasts - so a different
    # fault later is not hidden for the day - and one line when it clears.
    failed_ticks = 0
    tick_error_logged_at: datetime | None = None
    # A calendar that cannot be read is not an exception: the snapshot carries
    # the error and the light quietly falls back to rules and the default, so
    # meeting colours stopped with nothing in the log. Graph error bodies carry
    # a request id and date, so the message differs every poll; log the outage
    # starting and ending, not each message.
    calendar_down_since: datetime | None = None
    light_missing_since: datetime | None = None
    # A lapsed watchdog is not a quiet failure: the blink(1) firmware answers one
    # by playing the pattern still loaded in its memory, which is whichever
    # notification scene was written last. The light then repeats a flash nothing
    # fired, and nothing in any log says why. Both of the tick's failure paths
    # used to skip the re-arm - a bad calendar read `continue`d past it, and a
    # DeviceError jumped over it - so the fault that stopped a tick also handed
    # the light to the firmware. Re-arming is now its own step that runs whatever
    # else failed, and a tick that genuinely cannot arm it says so.
    watchdog_lapsed_since: datetime | None = None

    def rearm_watchdog(current: datetime) -> DeviceError | None:
        """Feed the watchdog, and report the device error if it could not be fed.

        This call is also the tick's proof that the light is still there: it is
        the one device call that happens on every tick, where the repaint only
        happens when the colour changes. The caller decides what a failure
        means, because an unplugged light and a light that is present but
        unreachable read the same here and want different log lines.
        """
        nonlocal watchdog_lapsed_since
        try:
            controller.enable_watchdog(int(config["settings"]["watchdog_millis"]))
        except DeviceError as error:
            return error
        if watchdog_lapsed_since is not None:
            seconds = round((current - watchdog_lapsed_since).total_seconds())
            LOGGER.info("Device watchdog re-armed after %ss unarmed", seconds)
            watchdog_lapsed_since = None
        return None

    try:
        while True:
            if paths.watcher_stop_path.exists():
                stopped_by = "stop-file"
                break
            current = now_factory()
            try:
                calendar_snapshot = calendar_cache.get(current)
                result = determine_action(
                    config,
                    paths,
                    snapshot_factory=snapshot_factory,
                    now=current,
                    calendar_snapshot=calendar_snapshot,
                )
            except Exception:
                # One bad tick - a malformed calendar entry, a transient COM
                # failure - used to kill the loop and take the light with it.
                # Log it and try again next tick; the light keeps showing
                # whatever it was showing.
                failed_ticks += 1
                if tick_error_logged_at is None or (current - tick_error_logged_at).total_seconds() >= 3600:
                    tick_error_logged_at = current
                    LOGGER.exception("Tick failed; keeping the previous action")
                # The light is still showing something and still needs the
                # watchdog fed, whatever the calendar just did.
                rearm_watchdog(current)
                time.sleep(float(config["settings"]["tick_seconds"]))
                continue
            if failed_ticks:
                LOGGER.info("Ticks recovered after %s failed", failed_ticks)
                failed_ticks = 0
                tick_error_logged_at = None
            calendar_error = calendar_snapshot.error if calendar_snapshot is not None else None
            if calendar_error and calendar_down_since is None:
                calendar_down_since = current
                LOGGER.info("Calendar unavailable, meeting colours paused: %s", calendar_error)
            elif not calendar_error and calendar_down_since is not None:
                minutes = round((current - calendar_down_since).total_seconds() / 60)
                LOGGER.info("Calendar available again after %s min", minutes)
                calendar_down_since = None
            signature = json.dumps(result["action"], sort_keys=True)
            chime_result = {"fired": False, "reason": "light-not-connected"}
            try:
                if signature != last_signature:
                    controller.apply_action(result["action"], config["scenes"], persistent=True)
                    acknowledge_calendar_result(paths, result, now=current)
                    last_signature = signature
                    # Only on a change: at a 5-second tick, a line per tick was
                    # ~17,000 identical lines a day burying the scheduler's own.
                    LOGGER.info("Applied action from %s:%s -> %s", result["source"], result["detail"], result["action"])
                chime_result = maybe_fire_chime(config, paths, controller=controller, now=current)
                if chime_result["fired"]:
                    # The chime leaves the LED wherever the pulse ended, so drop the
                    # cached signature and let the next tick repaint the real state.
                    last_signature = None
                    LOGGER.info("Fired hourly chime for slot %s", chime_result["slot"])
            except DeviceError as error:
                device_error = error
            else:
                device_error = None

            # Runs whether or not the repaint did, because a lapsed watchdog is
            # what hands the light to the firmware, and because on a tick that
            # painted nothing this is the only thing that touches the device.
            watchdog_error = rearm_watchdog(current)
            device_error = device_error or watchdog_error

            if device_error is not None:
                # Undocking takes the light with it. That used to end the
                # watcher, so re-docking left the light dark until someone
                # restarted it. Keep ticking, say so once, and forget what was
                # painted so the light is repainted the moment it is back.
                last_signature = None
                if light_missing_since is None:
                    light_missing_since = current
                    LOGGER.info("Light not connected; will repaint when it is back: %s", device_error)
            else:
                if light_missing_since is not None:
                    minutes = round((current - light_missing_since).total_seconds() / 60)
                    LOGGER.info("Light connected again after %s min", minutes)
                    light_missing_since = None

            # Logged separately from "light not connected" even though an unplug
            # trips both, because they are different facts: one says the light
            # is gone, this one says the firmware is now free to play whatever
            # pattern it still holds. Once per run of failures, not per tick.
            if watchdog_error is not None and watchdog_lapsed_since is None:
                watchdog_lapsed_since = current
                LOGGER.warning(
                    "Device watchdog not re-armed; the light may replay its stored pattern: %s",
                    watchdog_error,
                )
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
                    "light_connected": light_missing_since is None,
                },
            )
            time.sleep(float(config["settings"]["tick_seconds"]))
    except KeyboardInterrupt:
        stopped_by = "interrupt"
    except Exception:
        # The background watcher runs with stderr discarded, so a crash - a
        # light unplugged while it runs - used to leave nothing behind, while
        # start_background_watch tells you to read this very log.
        stopped_by = "error"
        LOGGER.exception("Watcher exited on an unhandled error")
        raise
    finally:
        # A light that is not plugged in has no watchdog to disarm and is
        # already dark, so there is nothing to report for either step.
        try:
            controller.disable_watchdog()
        except DeviceError:
            pass
        except Exception:
            LOGGER.exception("Failed disabling watchdog")
        if config["settings"].get("stop_turns_light_off", True):
            try:
                controller.off()
            except DeviceError:
                pass
            except Exception:
                LOGGER.exception("Failed turning blink(1) off")
        try:
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
        finally:
            LOGGER.info("Watcher stopped (%s)", stopped_by)
            close_log(LOGGER)
    return watch_status(paths)


def start_background_watch(paths: AppPaths) -> dict[str, Any]:
    """Launch the watcher detached, and wait to see it actually come up.

    The return value is the answer to "is it running now?", never "the launch
    was issued" - a detached process with its output discarded can die before
    its first tick and say nothing. Callers should treat a false ``running``
    as a failure; the CLI turns it into a non-zero exit.
    """
    ensure_runtime_dirs(paths)
    remove_file(paths.watcher_paused_path)
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


class WatcherSupervisor:
    """Restarts a watcher that went away without being asked to.

    Lives in the scheduler loop because that is the only process already
    running all day. The logon script was the sole way the watcher ever
    started, so anything that killed it mid-session - an undock the device
    layer could not survive, a crash, a kill - left the calendar dark until a
    human noticed and logged back in.

    Deliberately dumb: it does not diagnose, it re-runs the same start the CLI
    runs, no more than once every ``retry_seconds`` so a watcher that cannot
    start is not respawned every wake.

    It acts only where the watcher was meant to be running in the first place:
    the logon script installed (`startup enable`) and no pause marker from a
    deliberate `watch stop`. Supervising a watcher nobody asked for would mean
    a bare checkout that runs the scheduler starts spawning watchers.
    """

    def __init__(
        self,
        config: dict[str, Any],
        paths: AppPaths,
        starter: Callable[[AppPaths], dict[str, Any]] = start_background_watch,
        retry_seconds: float = 300.0,
        logger: logging.Logger | None = None,
    ):
        self.config = config
        self.paths = paths
        self.starter = starter
        self.retry_seconds = retry_seconds
        self.logger = logger or LOGGER
        self._last_attempt: datetime | None = None
        self._failing = False

    def enabled(self) -> bool:
        if not self.config["settings"].get("supervise_watcher", True):
            return False
        return startup_status(self.paths)["enabled"]

    def check(self, now: datetime | None = None) -> dict[str, Any]:
        current = now or _now()
        if not self.enabled():
            return {"action": "disabled"}
        if watch_paused(self.paths):
            return {"action": "paused"}
        status = watch_status(self.paths, now=current)
        if status["running"]:
            if self._failing:
                self.logger.info("Watcher is running again")
                self._failing = False
            self._last_attempt = None
            return {"action": "running", "pid": status["pid"]}
        if self._last_attempt is not None and (current - self._last_attempt).total_seconds() < self.retry_seconds:
            return {"action": "waiting"}

        self._last_attempt = current
        started = self.starter(self.paths)
        if started.get("running"):
            self.logger.info("Watcher was not running; restarted it (PID %s)", started.get("pid"))
            self._failing = False
            self._last_attempt = None
            return {"action": "restarted", "pid": started.get("pid")}
        if not self._failing:
            # Once, not once per retry: a watcher that cannot start usually
            # cannot start for a while, and the reason is already in this log.
            self._failing = True
            self.logger.error(
                "Watcher is not running and would not start; retrying every %ss",
                int(self.retry_seconds),
            )
        return {"action": "failed", "error": started.get("error")}


def stop_watch(paths: AppPaths, timeout_seconds: float = 8.0) -> dict[str, Any]:
    """Ask the watcher to exit, and record that the silence is deliberate.

    The pause marker outlives the watcher so the supervisor does not undo a
    stop the moment it is made. `watch start` - including the one the logon
    script runs - clears it.
    """
    ensure_runtime_dirs(paths)
    paths.watcher_paused_path.write_text(_now().isoformat(), encoding="utf-8")
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
