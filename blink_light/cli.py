from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timedelta
import io
import json
from pathlib import Path
import sys
from typing import Any, Callable

from .config import ConfigError, build_effective_config
from .calendar_source import (
    alert_statuses,
    alertable_events,
    calendar_enabled,
    choose_active_event,
    choose_next_event,
    poll_calendar,
)
from .chime import (
    autostart_status,
    chime_status,
    fire_chime,
    install_autostart_task,
    install_scheduled_task,
    loop_status,
    stop_chime_loop,
    uninstall_autostart_task,
    maybe_fire_chime,
    run_chime_loop,
    uninstall_scheduled_task,
)
from .alarms import alarm_status, fire_alarm, fire_due_alarms, find_alarm
from .defaults import BUSY_STATUS_NAMES, default_config
from .notify import NotifyError, fire_notify, list_events, resolve_event
from .github import GitHubPoller, github_status
from .show import fire_show, maybe_fire_show, show_status
from .device import BlinkDeviceController, DeviceError
from .paths import AppPaths, build_paths, ensure_runtime_dirs
from .startup import disable_startup, enable_startup, startup_status
from .state import read_json, remove_file, write_json
from .timers import (
    TimerSnapshot,
    clear_timer_state,
    countdown_state,
    pause_timer,
    refresh_timer_state,
    resume_timer,
    routine_state,
    save_timer_state,
)
from .watcher import (
    apply_once,
    determine_action,
    load_override,
    resolve_action,
    run_watch_loop,
    start_background_watch,
    stop_watch,
    watch_status,
)


def _now() -> datetime:
    return datetime.now().astimezone()


def _print_json(stream: io.TextIOBase, payload: Any) -> None:
    json.dump(payload, stream, indent=2, sort_keys=True)
    stream.write("\n")


def _print_welcome(stream: io.TextIOBase, controller_cls=BlinkDeviceController) -> None:
    serials: list[str]
    try:
        serials = controller_cls.list_devices()
    except Exception:
        serials = []
    stream.write("blink-light\n")
    stream.write("\n")
    if serials:
        stream.write(f"Connected blink(1) devices: {', '.join(serials)}\n")
    else:
        stream.write("Connected blink(1) devices: none detected\n")
    stream.write("\n")
    stream.write("Common commands:\n")
    stream.write("  blink-light.bat status\n")
    stream.write("  blink-light.bat preset list\n")
    stream.write("  blink-light.bat preset run focus\n")
    stream.write("  blink-light.bat timer start pomodoro\n")
    stream.write("  blink-light.bat chime install\n")
    stream.write("  blink-light.bat autostart enable\n")
    stream.write("  blink-light.bat show test\n")
    stream.write("  blink-light.bat alarm status\n")
    stream.write("  blink-light.bat watch start\n")
    stream.write("  blink-light.bat override set --preset busy --expires-in 30m\n")
    stream.write("\n")
    stream.write("Use 'blink-light.bat -h' for full help.\n")


def _handle_no_args(
    resolved_paths: AppPaths,
    stream: io.TextIOBase,
    controller_cls=BlinkDeviceController,
) -> int:
    if resolved_paths.config_path.exists():
        try:
            config = _load_config(resolved_paths)
        except Exception:
            config = None
        if config and calendar_enabled(config) and config["calendar"].get("auto_watch_on_launch", False):
            status = start_background_watch(resolved_paths)
            _print_json(
                stream,
                {
                    "launched_watch": True,
                    "calendar_provider": config["calendar"]["provider"],
                    "watch": status,
                },
            )
            return 0
    _print_welcome(stream, controller_cls=controller_cls)
    return 0


def _parse_duration(text: str, bare_unit: str = "seconds") -> int:
    value = text.strip().lower()
    units = {"s": 1, "m": 60, "h": 3600}
    if not value:
        raise ValueError("Duration cannot be empty.")
    suffix = value[-1]
    if suffix in units:
        amount = float(value[:-1])
        return int(amount * units[suffix])
    amount = float(value)
    if bare_unit == "minutes":
        return int(amount * 60)
    return int(amount)


def _action_from_args(args) -> dict[str, Any]:
    if getattr(args, "preset_name", None):
        return {"preset": args.preset_name}
    if getattr(args, "scene_name", None):
        return {"scene": args.scene_name}
    if getattr(args, "color_name", None):
        payload = {"color": args.color_name}
        if getattr(args, "fade_ms", None) is not None:
            payload["fade_ms"] = int(args.fade_ms)
        return payload
    if getattr(args, "off", False):
        return {"off": True}
    raise ValueError("An action is required.")


def _device_status(config: dict[str, Any], controller_cls=BlinkDeviceController) -> dict[str, Any]:
    controller = controller_cls(serial=config["device"].get("serial"))
    try:
        status = controller.status()
        return {
            "selected_serial": status.selected_serial,
            "available_serials": status.available_serials,
            "version": status.version,
        }
    finally:
        controller.close()


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


def _calendar_status(
    config: dict[str, Any],
    now: datetime,
    paths: AppPaths | None = None,
) -> dict[str, Any] | None:
    if not calendar_enabled(config):
        return None
    snapshot = poll_calendar(config, now, paths)
    events = alertable_events(snapshot.events, alert_statuses(config))
    active_event = choose_active_event(events, now)
    next_event = choose_next_event(events, now)
    return {
        "provider": snapshot.provider,
        "fetched_at": snapshot.fetched_at.isoformat(),
        "error": snapshot.error,
        # Two counts, because "3 events, none of them alertable" is the answer
        # to most "why is my light still green?" questions.
        "event_count": len(snapshot.events),
        "alertable_event_count": len(events),
        "active_event": active_event.to_payload() if active_event else None,
        "next_event": next_event.to_payload() if next_event else None,
    }


def _run_calendar_command(
    args: argparse.Namespace,
    config: dict[str, Any],
    paths: AppPaths,
    stream,
    now: datetime,
) -> int:
    from .graph_calendar import GraphAuthError, sign_in, sign_out, signed_in_account

    ensure_runtime_dirs(paths)

    if args.calendar_command == "login":
        result = sign_in(config, paths.graph_token_path, prompt=lambda message: print(message, file=stream))
        claims = result.get("id_token_claims", {})
        _print_json(
            stream,
            {
                "signed_in": True,
                "account": claims.get("preferred_username") or claims.get("name"),
                "token_cache": str(paths.graph_token_path),
            },
        )
        return 0

    if args.calendar_command == "logout":
        removed = sign_out(config, paths.graph_token_path)
        _print_json(stream, {"signed_out": True, "accounts_removed": removed})
        return 0

    if args.calendar_command == "status":
        try:
            account = signed_in_account(config, paths.graph_token_path)
        except GraphAuthError as exc:
            account = None
            _print_json(stream, {"provider": config["calendar"]["provider"], "error": str(exc)})
            return 1
        payload = {
            "provider": config["calendar"]["provider"],
            "account": account,
            "alert_statuses": config["calendar"]["alert_statuses"],
        }
        payload.update(_calendar_status(config, now, paths) or {})
        _print_json(stream, payload)
        return 0

    if args.calendar_command == "upcoming":
        _print_json(stream, _calendar_upcoming(config, paths, now, hours=args.hours))
        return 0

    return 1


def _calendar_upcoming(
    config: dict[str, Any],
    paths: AppPaths,
    now: datetime,
    hours: float,
) -> dict[str, Any]:
    """Every event in the window, each labelled with what the light will do.

    Deliberately runs through the same filter the watcher uses, so this answers
    "will it fire for that meeting?" rather than "is that meeting on my
    calendar?" - which are different questions, and only the first one matters.
    """
    widened = deepcopy(config)
    widened["calendar"]["lookahead_minutes"] = max(int(hours * 60), 1)
    snapshot = poll_calendar(widened, now, paths)
    statuses = alert_statuses(config)
    ignore_all_day = bool(config["calendar"].get("ignore_all_day", True))

    events = []
    for event in sorted(snapshot.events, key=lambda item: item.start):
        if event.end <= now:
            continue
        skipped_all_day = ignore_all_day and event.is_all_day
        fires = event.busy_status in statuses and not skipped_all_day
        payload = {
            "subject": event.subject,
            "start": event.start.isoformat(),
            "end": event.end.isoformat(),
            "busy_status": event.busy_status,
            "status_name": BUSY_STATUS_NAMES.get(event.busy_status, str(event.busy_status)),
            "fires": fires,
        }
        if fires:
            payload["ten_minute_warning_at"] = (event.start - timedelta(minutes=10)).isoformat()
            payload["five_minute_warning_at"] = (event.start - timedelta(minutes=5)).isoformat()
        else:
            payload["ignored_because"] = "all-day event" if skipped_all_day else payload["status_name"]
        events.append(payload)

    return {
        "provider": snapshot.provider,
        "error": snapshot.error,
        "window_hours": hours,
        "fetched_at": snapshot.fetched_at.isoformat(),
        "event_count": len(events),
        "firing_count": sum(1 for event in events if event["fires"]),
        "events": events,
    }


def _load_config(paths: AppPaths) -> dict[str, Any]:
    return build_effective_config(paths.config_path)


def _write_default_config(paths: AppPaths, controller_cls=BlinkDeviceController) -> dict[str, Any]:
    serial = None
    try:
        serials = controller_cls.list_devices()
        if len(serials) == 1:
            serial = serials[0]
    except Exception:
        serial = None
    payload = default_config(serial=serial)
    write_json(paths.config_path, payload)
    return payload


def _set_override(paths: AppPaths, action: dict[str, Any], reason: str | None, expires_in: str | None) -> dict[str, Any]:
    ensure_runtime_dirs(paths)
    expires_at = None
    if expires_in:
        expires_at = (_now() + timedelta(seconds=_parse_duration(expires_in))).isoformat()
    payload = {
        "action": action,
        "reason": reason or "manual",
        "created_at": _now().isoformat(),
        "expires_at": expires_at,
    }
    write_json(paths.override_path, payload)
    return payload


def _clear_override(paths: AppPaths) -> dict[str, Any]:
    remove_file(paths.override_path)
    return {"cleared": True}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="blink-light", description="blink(1) Windows utility and watcher")
    parser.add_argument("--config", type=Path, default=None, help="Path to the JSON config file.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("devices", help="List connected blink(1) devices.")
    subparsers.add_parser("status", help="Show overall status.")

    light_parser = subparsers.add_parser("light", help="Run one-shot light actions.")
    light_sub = light_parser.add_subparsers(dest="light_command", required=True)
    color_parser = light_sub.add_parser("color", help="Set a steady color.")
    color_parser.add_argument("color_name")
    color_parser.add_argument("--fade-ms", type=int, default=150)
    light_sub.add_parser("off", help="Turn the light off.")
    flash_parser = light_sub.add_parser("flash", help="Flash between two colors.")
    flash_parser.add_argument("color_name")
    flash_parser.add_argument("--secondary-color", default="#000000")
    flash_parser.add_argument("--on-ms", type=int, default=250)
    flash_parser.add_argument("--off-ms", type=int, default=250)
    flash_parser.add_argument("--count", type=int, default=4)
    pulse_parser = light_sub.add_parser("pulse", help="Pulse between two colors.")
    pulse_parser.add_argument("color_name")
    pulse_parser.add_argument("--secondary-color", default="#000000")
    pulse_parser.add_argument("--on-ms", type=int, default=350)
    pulse_parser.add_argument("--off-ms", type=int, default=900)
    pulse_parser.add_argument("--count", type=int, default=6)
    scene_parser = light_sub.add_parser("scene", help="Run a named scene.")
    scene_parser.add_argument("scene_name")

    preset_parser = subparsers.add_parser("preset", help="Manage presets.")
    preset_sub = preset_parser.add_subparsers(dest="preset_command", required=True)
    preset_sub.add_parser("list", help="List available presets.")
    preset_run = preset_sub.add_parser("run", help="Run a preset.")
    preset_run.add_argument("preset_name")

    override_parser = subparsers.add_parser("override", help="Manage persistent watcher overrides.")
    override_sub = override_parser.add_subparsers(dest="override_command", required=True)
    override_set = override_sub.add_parser("set", help="Set an override action.")
    action_group = override_set.add_mutually_exclusive_group(required=True)
    action_group.add_argument("--preset", dest="preset_name")
    action_group.add_argument("--scene", dest="scene_name")
    action_group.add_argument("--color", dest="color_name")
    action_group.add_argument("--off", action="store_true")
    override_set.add_argument("--fade-ms", type=int)
    override_set.add_argument("--reason")
    override_set.add_argument("--expires-in", help="Expiry like 30m, 2h, or 900.")
    override_sub.add_parser("clear", help="Clear the current override.")
    override_sub.add_parser("status", help="Show the current override.")

    timer_parser = subparsers.add_parser("timer", help="Manage countdowns and routines.")
    timer_sub = timer_parser.add_subparsers(dest="timer_command", required=True)
    timer_start = timer_sub.add_parser("start", help="Start a built-in routine or countdown.")
    timer_start.add_argument("target", nargs="?")
    timer_start.add_argument("--duration", help="Countdown duration like 25m or 90s.")
    timer_start.add_argument("--minutes", type=float, help="Countdown duration in minutes.")
    timer_start.add_argument("--name", help="Custom timer name for countdowns.")
    timer_start.add_argument("--preset", default="focus", help="Active preset for countdown timers.")
    timer_start.add_argument("--completion-preset", default="timer_done")
    timer_sub.add_parser("pause", help="Pause the active timer.")
    timer_sub.add_parser("resume", help="Resume a paused timer.")
    timer_sub.add_parser("stop", help="Clear the active timer.")
    timer_sub.add_parser("status", help="Show timer status.")

    watch_parser = subparsers.add_parser("watch", help="Manage the background watcher.")
    watch_sub = watch_parser.add_subparsers(dest="watch_command", required=True)
    watch_run = watch_sub.add_parser("run", help="Run the watcher in the foreground.")
    watch_run.add_argument("--background", action="store_true", help=argparse.SUPPRESS)
    watch_sub.add_parser("start", help="Start the watcher in the background.")
    watch_sub.add_parser("stop", help="Stop the watcher.")
    watch_sub.add_parser("status", help="Show watcher status.")
    watch_sub.add_parser("once", help="Evaluate watcher logic once and apply it.")

    chime_parser = subparsers.add_parser("chime", help="Top-of-the-hour white pulse.")
    chime_sub = chime_parser.add_subparsers(dest="chime_command", required=True)
    chime_now = chime_sub.add_parser("now", help="Chime if this hour's pulse is still due.")
    chime_now.add_argument(
        "--force",
        action="store_true",
        help="Pulse regardless of quiet hours, the catch-up window, or a chime already fired this hour.",
    )
    chime_sub.add_parser("test", help="Pulse once immediately without touching chime state.")
    chime_sub.add_parser("run", help="Run the hourly chime loop in the foreground.")
    chime_sub.add_parser("stop", help="Ask a running chime loop to exit.")
    chime_sub.add_parser("status", help="Show chime config, last fire, and next slot.")
    chime_sub.add_parser("install", help="Register the hourly Windows scheduled task.")
    chime_sub.add_parser("uninstall", help="Remove the hourly Windows scheduled task.")

    alarm_parser = subparsers.add_parser("alarm", help="Named daily alarms (standup alerts).")
    alarm_sub = alarm_parser.add_subparsers(dest="alarm_command", required=True)
    alarm_sub.add_parser("status", help="Show every alarm, when it next fires, and when it last did.")
    alarm_sub.add_parser("now", help="Fire any alarm that is currently due.")
    alarm_test = alarm_sub.add_parser("test", help="Play one alarm now without consuming today's slot.")
    alarm_test.add_argument("name")
    alarm_run = alarm_sub.add_parser("run", help="Fire one alarm now and consume today's slot.")
    alarm_run.add_argument("name")

    notify_parser = subparsers.add_parser(
        "notify",
        help="Fire a named one-shot notification. The integration entry point.",
    )
    notify_sub = notify_parser.add_subparsers(dest="notify_command", required=True)
    notify_sub.add_parser("list", help="List configured notify events.")
    notify_run = notify_sub.add_parser("run", help="Fire a notify event now.")
    notify_run.add_argument("event")
    notify_run.add_argument(
        "--quiet-missing",
        action="store_true",
        help="Exit 0 when the event is unknown. For integrations that must not fail loudly.",
    )

    show_parser = subparsers.add_parser("show", help="Daily light show (rainbow swirl at 17:00).")
    show_sub = show_parser.add_subparsers(dest="show_command", required=True)
    show_now = show_sub.add_parser("now", help="Play the show if today's slot is still due.")
    show_now.add_argument(
        "--force",
        action="store_true",
        help="Play regardless of quiet hours, the catch-up window, or a show already played today.",
    )
    show_sub.add_parser("test", help="Play the show immediately without consuming today's slot.")
    show_sub.add_parser("status", help="Show the schedule, scene length, and last run.")

    autostart_parser = subparsers.add_parser(
        "autostart",
        help="Start the chime runner automatically at logon.",
    )
    autostart_sub = autostart_parser.add_subparsers(dest="autostart_command", required=True)
    autostart_enable = autostart_sub.add_parser("enable", help="Register the logon task and start it now.")
    autostart_enable.add_argument(
        "--no-start",
        action="store_true",
        help="Register the task but wait for the next logon instead of starting it now.",
    )
    autostart_sub.add_parser("disable", help="Stop the runner and remove the logon task.")
    autostart_sub.add_parser("status", help="Show whether the logon task is registered and running.")

    github_parser = subparsers.add_parser("github", help="GitHub Actions failure notifications.")
    github_sub = github_parser.add_subparsers(dest="github_command", required=True)
    github_sub.add_parser("status", help="Show authentication and the last GitHub poll.")
    github_check = github_sub.add_parser("check", help="Poll now, ignoring the interval.")
    github_check.add_argument("--dry-run", action="store_true", help="Preview without saving or flashing.")
    github_sub.add_parser("test", help="Play the configured CI failure event once.")

    calendar_parser = subparsers.add_parser("calendar", help="Microsoft 365 / Outlook calendar.")
    calendar_sub = calendar_parser.add_subparsers(dest="calendar_command", required=True)
    calendar_sub.add_parser("login", help="Sign in to Microsoft 365 and cache the token.")
    calendar_sub.add_parser("logout", help="Forget the cached Microsoft 365 sign-in.")
    calendar_sub.add_parser("status", help="Show the provider, the signed-in account, and the next event.")
    calendar_upcoming = calendar_sub.add_parser(
        "upcoming",
        help="List upcoming events and exactly which ones will move the light.",
    )
    calendar_upcoming.add_argument(
        "--hours",
        type=float,
        default=48.0,
        help="How far ahead to look. Default 48.",
    )

    config_parser = subparsers.add_parser("config", help="Manage blink-light.json.")
    config_sub = config_parser.add_subparsers(dest="config_command", required=True)
    init_parser = config_sub.add_parser("init", help="Write a starter config file.")
    init_parser.add_argument("--force", action="store_true")
    config_sub.add_parser("validate", help="Validate the current config.")
    config_sub.add_parser("show", help="Print the effective config.")

    startup_parser = subparsers.add_parser("startup", help="Manage login startup registration.")
    startup_sub = startup_parser.add_subparsers(dest="startup_command", required=True)
    startup_sub.add_parser("enable", help="Enable watcher startup at login.")
    startup_sub.add_parser("disable", help="Disable watcher startup at login.")
    startup_sub.add_parser("status", help="Show startup registration status.")
    return parser


def main(
    argv: list[str] | None = None,
    *,
    paths: AppPaths | None = None,
    controller_cls=BlinkDeviceController,
    snapshot_factory=None,
    now_factory: Callable[[], datetime] = _now,
    out: io.TextIOBase | None = None,
    err: io.TextIOBase | None = None,
) -> int:
    parser = _build_parser()
    actual_argv = sys.argv[1:] if argv is None else argv
    if len(actual_argv) == 0:
        stream = out or sys.stdout
        resolved_paths = paths or build_paths()
        return _handle_no_args(resolved_paths, stream, controller_cls=controller_cls)
    args = parser.parse_args(actual_argv)
    stream = out or sys.stdout
    error_stream = err or sys.stderr
    resolved_paths = paths or build_paths(config_path=args.config)
    if snapshot_factory is None:
        from .system_state import collect_system_snapshot

        snapshot_factory = collect_system_snapshot

    try:
        if args.command == "devices":
            _print_json(stream, {"serials": controller_cls.list_devices()})
            return 0

        if args.command == "config":
            if args.config_command == "init":
                if resolved_paths.config_path.exists() and not args.force:
                    raise RuntimeError(f"Config already exists at {resolved_paths.config_path}. Use --force to overwrite.")
                payload = _write_default_config(resolved_paths, controller_cls=controller_cls)
                _print_json(stream, {"path": str(resolved_paths.config_path), "config": payload})
                return 0
            effective = _load_config(resolved_paths)
            if args.config_command == "validate":
                _print_json(stream, {"valid": True, "path": str(resolved_paths.config_path)})
                return 0
            if args.config_command == "show":
                _print_json(stream, effective)
                return 0

        config = _load_config(resolved_paths)

        if args.command == "github":
            if args.github_command == "status":
                payload = github_status(config, resolved_paths)
            elif args.github_command == "test":
                payload = fire_notify(config, config["github"]["actions_failed_event"], controller_cls=controller_cls)
            else:
                payload = GitHubPoller(config, resolved_paths).poll(
                    now=now_factory(), force=True, dry_run=args.dry_run, controller_cls=controller_cls)
            _print_json(stream, payload)
            return 1 if payload.get("result") == "error" else 0

        if args.command == "calendar":
            return _run_calendar_command(args, config, resolved_paths, stream, now_factory())

        if args.command == "status":
            current = now_factory()
            payload = {
                "config_path": str(resolved_paths.config_path),
                "calendar": _calendar_status(config, current, resolved_paths),
                "device": _device_status(config, controller_cls=controller_cls),
                "override": load_override(resolved_paths, current),
                "timer": _timer_payload(refresh_timer_state(resolved_paths.timer_path, current)),
                "watch": watch_status(resolved_paths),
                "resolved_action": determine_action(
                    config,
                    resolved_paths,
                    snapshot_factory=snapshot_factory,
                    now=current,
                ),
            }
            _print_json(stream, payload)
            return 0

        if args.command == "light":
            controller = controller_cls(serial=config["device"].get("serial"))
            try:
                if args.light_command == "color":
                    controller.solid(args.color_name, fade_ms=args.fade_ms)
                    _print_json(stream, {"applied": {"color": args.color_name, "fade_ms": args.fade_ms}})
                    return 0
                if args.light_command == "off":
                    controller.off()
                    _print_json(stream, {"applied": {"off": True}})
                    return 0
                if args.light_command == "flash":
                    controller.flash(
                        color=args.color_name,
                        secondary_color=args.secondary_color,
                        on_ms=args.on_ms,
                        off_ms=args.off_ms,
                        count=args.count,
                    )
                    _print_json(
                        stream,
                        {
                            "applied": {
                                "mode": "flash",
                                "color": args.color_name,
                                "secondary_color": args.secondary_color,
                                "count": args.count,
                            }
                        },
                    )
                    return 0
                if args.light_command == "pulse":
                    controller.pulse(
                        color=args.color_name,
                        secondary_color=args.secondary_color,
                        on_ms=args.on_ms,
                        off_ms=args.off_ms,
                        count=args.count,
                    )
                    _print_json(
                        stream,
                        {
                            "applied": {
                                "mode": "pulse",
                                "color": args.color_name,
                                "secondary_color": args.secondary_color,
                                "count": args.count,
                            }
                        },
                    )
                    return 0
                if args.light_command == "scene":
                    controller.play_scene(config["scenes"][args.scene_name], persistent=False)
                    _print_json(stream, {"applied": {"scene": args.scene_name}})
                    return 0
            finally:
                controller.close()

        if args.command == "preset":
            if args.preset_command == "list":
                _print_json(stream, {"presets": sorted(config["presets"].keys())})
                return 0
            resolved = resolve_action({"preset": args.preset_name}, config)
            controller = controller_cls(serial=config["device"].get("serial"))
            try:
                controller.apply_action(resolved, config["scenes"], persistent=False)
            finally:
                controller.close()
            _print_json(stream, {"applied": resolved, "preset": args.preset_name})
            return 0

        if args.command == "override":
            if args.override_command == "set":
                payload = _set_override(
                    resolved_paths,
                    action=_action_from_args(args),
                    reason=args.reason,
                    expires_in=args.expires_in,
                )
                _print_json(stream, payload)
                return 0
            if args.override_command == "clear":
                _print_json(stream, _clear_override(resolved_paths))
                return 0
            if args.override_command == "status":
                _print_json(stream, {"override": load_override(resolved_paths, now_factory())})
                return 0

        if args.command == "timer":
            ensure_runtime_dirs(resolved_paths)
            if args.timer_command == "start":
                target = args.target
                if target and target in config["routines"] and not args.duration and args.minutes is None:
                    state = routine_state(target, config["routines"][target], now=now_factory())
                else:
                    if args.duration:
                        duration_seconds = _parse_duration(args.duration, bare_unit="seconds")
                    elif args.minutes is not None:
                        duration_seconds = int(args.minutes * 60)
                    elif target:
                        duration_seconds = _parse_duration(target, bare_unit="minutes")
                    else:
                        raise RuntimeError("Provide a routine name or a countdown duration.")
                    timer_name = args.name or target or f"{duration_seconds // 60}m timer"
                    state = countdown_state(
                        timer_name,
                        duration_seconds=duration_seconds,
                        active_action={"preset": args.preset},
                        completion_action={"preset": args.completion_preset},
                        now=now_factory(),
                    )
                save_timer_state(resolved_paths.timer_path, state)
                _print_json(stream, {"timer": _timer_payload(refresh_timer_state(resolved_paths.timer_path, now_factory()))})
                return 0
            if args.timer_command == "pause":
                _print_json(stream, {"timer": _timer_payload(pause_timer(resolved_paths.timer_path, now_factory()))})
                return 0
            if args.timer_command == "resume":
                _print_json(stream, {"timer": _timer_payload(resume_timer(resolved_paths.timer_path, now_factory()))})
                return 0
            if args.timer_command == "stop":
                clear_timer_state(resolved_paths.timer_path)
                _print_json(stream, {"cleared": True})
                return 0
            if args.timer_command == "status":
                _print_json(stream, {"timer": _timer_payload(refresh_timer_state(resolved_paths.timer_path, now_factory()))})
                return 0

        if args.command == "watch":
            if args.watch_command == "run":
                run_watch_loop(
                    config,
                    resolved_paths,
                    controller_cls=controller_cls,
                    snapshot_factory=snapshot_factory,
                    now_factory=now_factory,
                    background=args.background,
                )
                return 0
            if args.watch_command == "start":
                _print_json(stream, start_background_watch(resolved_paths))
                return 0
            if args.watch_command == "stop":
                _print_json(stream, stop_watch(resolved_paths))
                return 0
            if args.watch_command == "status":
                _print_json(stream, watch_status(resolved_paths))
                return 0
            if args.watch_command == "once":
                payload = apply_once(
                    config,
                    resolved_paths,
                    controller_cls=controller_cls,
                    snapshot_factory=snapshot_factory,
                    now=now_factory(),
                )
                _print_json(stream, payload)
                return 0

        if args.command == "chime":
            if args.chime_command == "status":
                _print_json(stream, chime_status(config, resolved_paths, now_factory()))
                return 0
            if args.chime_command == "install":
                _print_json(stream, install_scheduled_task(config, resolved_paths))
                return 0
            if args.chime_command == "uninstall":
                _print_json(stream, uninstall_scheduled_task())
                return 0
            if args.chime_command == "now":
                if args.force:
                    payload = fire_chime(
                        config,
                        resolved_paths,
                        controller_cls=controller_cls,
                        now=now_factory(),
                        source="chime-now-forced",
                    )
                else:
                    payload = maybe_fire_chime(
                        config,
                        resolved_paths,
                        controller_cls=controller_cls,
                        now=now_factory(),
                        source="chime-now",
                    )
                _print_json(stream, payload)
                return 0
            if args.chime_command == "test":
                payload = fire_chime(
                    config,
                    resolved_paths,
                    controller_cls=controller_cls,
                    now=now_factory(),
                    source="chime-test",
                    record=False,
                )
                _print_json(stream, payload)
                return 0
            if args.chime_command == "stop":
                _print_json(stream, stop_chime_loop(resolved_paths))
                return 0
            if args.chime_command == "run":
                _print_json(
                    stream,
                    run_chime_loop(
                        config,
                        resolved_paths,
                        controller_cls=controller_cls,
                        now_factory=now_factory,
                    ),
                )
                return 0

        if args.command == "alarm":
            if args.alarm_command == "status":
                _print_json(stream, alarm_status(config, resolved_paths, now_factory()))
                return 0
            if args.alarm_command == "now":
                results = fire_due_alarms(
                    config,
                    resolved_paths,
                    controller_cls=controller_cls,
                    now=now_factory(),
                    source="alarm-now",
                )
                _print_json(stream, {"fired": [result["alarm"] for result in results], "results": results})
                return 0
            if args.alarm_command in ("test", "run"):
                alarm = find_alarm(config, args.name)
                _print_json(
                    stream,
                    fire_alarm(
                        config,
                        resolved_paths,
                        alarm,
                        controller_cls=controller_cls,
                        now=now_factory(),
                        source=f"alarm-{args.alarm_command}",
                        record=args.alarm_command == "run",
                    ),
                )
                return 0

        if args.command == "notify":
            if args.notify_command == "list":
                _print_json(
                    stream,
                    {
                        "events": {
                            name: resolve_event(config, name) for name in list_events(config)
                        }
                    },
                )
                return 0
            if args.notify_command == "run":
                try:
                    payload = fire_notify(config, args.event, controller_cls=controller_cls)
                except NotifyError:
                    # An integration firing an event this config does not define
                    # should be able to stay silent rather than spam failures.
                    if args.quiet_missing:
                        _print_json(stream, {"notified": False, "event": args.event, "reason": "unknown-event"})
                        return 0
                    raise
                _print_json(stream, payload)
                return 0

        if args.command == "show":
            if args.show_command == "status":
                _print_json(stream, show_status(config, resolved_paths, now_factory()))
                return 0
            if args.show_command == "now":
                if args.force:
                    payload = fire_show(
                        config,
                        resolved_paths,
                        controller_cls=controller_cls,
                        now=now_factory(),
                        source="show-now-forced",
                    )
                else:
                    payload = maybe_fire_show(
                        config,
                        resolved_paths,
                        controller_cls=controller_cls,
                        now=now_factory(),
                        source="show-now",
                    )
                _print_json(stream, payload)
                return 0
            if args.show_command == "test":
                _print_json(
                    stream,
                    fire_show(
                        config,
                        resolved_paths,
                        controller_cls=controller_cls,
                        now=now_factory(),
                        source="show-test",
                        record=False,
                    ),
                )
                return 0

        if args.command == "autostart":
            if args.autostart_command == "enable":
                _print_json(
                    stream,
                    install_autostart_task(resolved_paths, start_now=not args.no_start),
                )
                return 0
            if args.autostart_command == "disable":
                _print_json(stream, uninstall_autostart_task(resolved_paths))
                return 0
            if args.autostart_command == "status":
                _print_json(stream, autostart_status(resolved_paths))
                return 0

        if args.command == "startup":
            if args.startup_command == "enable":
                _print_json(stream, enable_startup(resolved_paths))
                return 0
            if args.startup_command == "disable":
                _print_json(stream, disable_startup(resolved_paths))
                return 0
            if args.startup_command == "status":
                _print_json(stream, startup_status(resolved_paths))
                return 0

        raise RuntimeError(f"Unsupported command: {args.command}")
    except (ConfigError, DeviceError, NotifyError, RuntimeError, ValueError, KeyError) as exc:
        error_stream.write(f"error: {exc}\n")
        return 1
