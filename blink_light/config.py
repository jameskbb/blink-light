from __future__ import annotations

from copy import deepcopy
import math
from pathlib import Path
from typing import Any

from .defaults import default_config
from .env_file import ENV_FILE_NAME, apply_env_overrides, resolve_env
from .state import read_json


class ConfigError(ValueError):
    """Raised when blink-light config is invalid."""


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = deepcopy(value)
    return merged


def _merge_named_section(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = deepcopy(base)
    for key, value in override.items():
        merged[key] = deepcopy(value)
    return merged


def load_user_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    payload = read_json(path, {})
    if not isinstance(payload, dict):
        raise ConfigError("Config root must be a JSON object.")
    return payload


def build_effective_config(path: Path) -> dict[str, Any]:
    user = load_user_config(path)
    merged = merge_config(user)
    # Applied last, and never written back: credentials and account ids belong
    # in .env, not in blink-light.json - that file holds behaviour and gets
    # shared as a template.
    merged = apply_env_overrides(merged, resolve_env(path.parent / ENV_FILE_NAME))
    validate_config(merged)
    return merged


def merge_config(user: dict[str, Any], detected_serial: str | None = None) -> dict[str, Any]:
    base = default_config(serial=detected_serial)
    if "github" in user:
        if not isinstance(user["github"], dict):
            raise ConfigError("'github' must be an object.")
        base["github"] = _deep_merge(base["github"], user["github"])
    if "device" in user:
        if not isinstance(user["device"], dict):
            raise ConfigError("'device' must be an object.")
        base["device"] = _deep_merge(base["device"], user["device"])
    if "calendar" in user:
        if not isinstance(user["calendar"], dict):
            raise ConfigError("'calendar' must be an object.")
        base["calendar"] = _deep_merge(base["calendar"], user["calendar"])
    if "chime" in user:
        if not isinstance(user["chime"], dict):
            raise ConfigError("'chime' must be an object.")
        base["chime"] = _deep_merge(base["chime"], user["chime"])
    if "show" in user:
        if not isinstance(user["show"], dict):
            raise ConfigError("'show' must be an object.")
        base["show"] = _deep_merge(base["show"], user["show"])
    if "notify" in user:
        if not isinstance(user["notify"], dict):
            raise ConfigError("'notify' must be an object.")
        base["notify"] = _merge_named_section(base["notify"], user["notify"])
    if "alarms" in user:
        if not isinstance(user["alarms"], list):
            raise ConfigError("'alarms' must be a list.")
        # Replaced wholesale, like 'rules': a user listing alarms is stating the
        # complete set, and merging by name would make an alarm impossible to
        # delete without an explicit disable.
        base["alarms"] = deepcopy(user["alarms"])
    for section in ("presets", "scenes", "routines"):
        if section in user:
            if not isinstance(user[section], dict):
                raise ConfigError(f"'{section}' must be an object.")
            base[section] = _merge_named_section(base[section], user[section])
    if "rules" in user:
        if not isinstance(user["rules"], list):
            raise ConfigError("'rules' must be a list.")
        base["rules"] = deepcopy(user["rules"])
    if "settings" in user:
        if not isinstance(user["settings"], dict):
            raise ConfigError("'settings' must be an object.")
        base["settings"] = _deep_merge(base["settings"], user["settings"])
    return base


def validate_config(payload: dict[str, Any]) -> None:
    for key in (
        "device",
        "calendar",
        "github",
        "chime",
        "show",
        "alarms",
        "notify",
        "presets",
        "scenes",
        "routines",
        "rules",
        "settings",
    ):
        if key not in payload:
            raise ConfigError(f"Missing top-level key: {key}")

    if not isinstance(payload["device"], dict):
        raise ConfigError("'device' must be an object.")
    serial = payload["device"].get("serial")
    if serial is not None and not isinstance(serial, str):
        raise ConfigError("'device.serial' must be null or a string.")
    _validate_calendar(payload["calendar"])
    _validate_chime(payload["chime"])

    _validate_alarms(payload["alarms"])
    if not isinstance(payload["notify"], dict):
        raise ConfigError("'notify' must be an object.")
    for name, action in payload["notify"].items():
        _validate_action(action, f"notify.{name}")
    _validate_github(payload["github"], payload["notify"])
    if not isinstance(payload["presets"], dict):
        raise ConfigError("'presets' must be an object.")
    if not isinstance(payload["scenes"], dict):
        raise ConfigError("'scenes' must be an object.")
    if not isinstance(payload["routines"], dict):
        raise ConfigError("'routines' must be an object.")
    if not isinstance(payload["rules"], list):
        raise ConfigError("'rules' must be a list.")
    if not isinstance(payload["settings"], dict):
        raise ConfigError("'settings' must be an object.")

    for name, action in payload["presets"].items():
        _validate_action(action, f"presets.{name}")
    for name, scene in payload["scenes"].items():
        _validate_scene(scene, f"scenes.{name}")
    for name, routine in payload["routines"].items():
        _validate_routine(routine, f"routines.{name}")
    for index, rule in enumerate(payload["rules"]):
        location = f"rules[{index}]"
        if not isinstance(rule, dict):
            raise ConfigError(f"{location} must be an object.")
        if not isinstance(rule.get("name"), str):
            raise ConfigError(f"{location}.name must be a string.")
        if not isinstance(rule.get("when"), dict):
            raise ConfigError(f"{location}.when must be an object.")
        _validate_conditions(rule["when"], f"{location}.when")
        _validate_action(rule.get("action"), f"{location}.action")

    settings = payload["settings"]
    tick_seconds = settings.get("tick_seconds")
    watchdog_millis = settings.get("watchdog_millis")
    if not isinstance(tick_seconds, (int, float)) or tick_seconds <= 0:
        raise ConfigError("'settings.tick_seconds' must be a positive number.")
    if not isinstance(watchdog_millis, (int, float)) or watchdog_millis <= 0:
        raise ConfigError("'settings.watchdog_millis' must be a positive number.")
    if not isinstance(settings.get("stop_turns_light_off"), bool):
        raise ConfigError("'settings.stop_turns_light_off' must be a boolean.")
    _validate_action(settings.get("default_action"), "settings.default_action")
    quiet_hours = settings.get("quiet_hours")
    if not isinstance(quiet_hours, dict):
        raise ConfigError("'settings.quiet_hours' must be an object.")
    if not isinstance(quiet_hours.get("enabled"), bool):
        raise ConfigError("'settings.quiet_hours.enabled' must be a boolean.")
    for key in ("start", "end"):
        value = quiet_hours.get(key)
        if not isinstance(value, str) or len(value.split(":")) != 2:
            raise ConfigError(f"'settings.quiet_hours.{key}' must look like HH:MM.")
    _validate_action(quiet_hours.get("action"), "settings.quiet_hours.action")
    # Runs last: it measures a scene, so every scene must already be well formed.
    _validate_show(payload["show"], payload["scenes"])
    _validate_action_references(payload)


def _validate_action(action: Any, location: str) -> None:
    if not isinstance(action, dict):
        raise ConfigError(f"{location} must be an object.")
    action_keys = {"preset", "scene", "color", "off"}
    present = [key for key in action_keys if key in action]
    if len(present) != 1:
        raise ConfigError(f"{location} must define exactly one of preset, scene, color, or off.")
    if "preset" in action and not isinstance(action["preset"], str):
        raise ConfigError(f"{location}.preset must be a string.")
    if "scene" in action and not isinstance(action["scene"], str):
        raise ConfigError(f"{location}.scene must be a string.")
    if "color" in action and not isinstance(action["color"], str):
        raise ConfigError(f"{location}.color must be a string.")
    if "off" in action and action["off"] is not True:
        raise ConfigError(f"{location}.off must be true.")
    for number_key in ("fade_ms", "on_ms", "off_ms", "count"):
        if number_key in action and not isinstance(action[number_key], (int, float)):
            raise ConfigError(f"{location}.{number_key} must be numeric when present.")
    if "secondary_color" in action and not isinstance(action["secondary_color"], str):
        raise ConfigError(f"{location}.secondary_color must be a string.")


def _validate_scene(scene: Any, location: str) -> None:
    if not isinstance(scene, dict):
        raise ConfigError(f"{location} must be an object.")
    if not isinstance(scene.get("loop", False), bool):
        raise ConfigError(f"{location}.loop must be a boolean.")
    repeat = scene.get("repeat", 1)
    if not isinstance(repeat, int) or repeat < 1:
        raise ConfigError(f"{location}.repeat must be a positive integer.")
    steps = scene.get("steps")
    if not isinstance(steps, list) or not steps:
        raise ConfigError(f"{location}.steps must be a non-empty list.")
    for index, step in enumerate(steps):
        step_location = f"{location}.steps[{index}]"
        if not isinstance(step, dict):
            raise ConfigError(f"{step_location} must be an object.")
        if not isinstance(step.get("color"), str):
            raise ConfigError(f"{step_location}.color must be a string.")
        seconds = step.get("seconds")
        if not isinstance(seconds, (int, float)) or seconds < 0:
            raise ConfigError(f"{step_location}.seconds must be zero or greater.")
        led = step.get("led", 0)
        if not isinstance(led, int) or led not in (0, 1, 2):
            raise ConfigError(f"{step_location}.led must be 0, 1, or 2.")


def _validate_routine(routine: Any, location: str) -> None:
    if not isinstance(routine, dict):
        raise ConfigError(f"{location} must be an object.")
    phases = routine.get("phases")
    if not isinstance(phases, list) or not phases:
        raise ConfigError(f"{location}.phases must be a non-empty list.")
    for index, phase in enumerate(phases):
        phase_location = f"{location}.phases[{index}]"
        if not isinstance(phase, dict):
            raise ConfigError(f"{phase_location} must be an object.")
        if not isinstance(phase.get("name"), str):
            raise ConfigError(f"{phase_location}.name must be a string.")
        duration = phase.get("minutes", phase.get("seconds"))
        if not isinstance(duration, (int, float)) or duration <= 0:
            raise ConfigError(f"{phase_location} must include a positive minutes or seconds value.")
        _validate_action(phase.get("action"), f"{phase_location}.action")
    if "completion_action" in routine:
        _validate_action(routine["completion_action"], f"{location}.completion_action")


def _validate_conditions(conditions: dict[str, Any], location: str) -> None:
    valid = {
        "idle_seconds_gte",
        "process_running_any",
        "file_exists",
        "file_contains",
        "time_between",
        "battery_below_percent",
        "charging",
        "timer_active",
        "routine_phase",
    }
    for key, value in conditions.items():
        if key not in valid:
            raise ConfigError(f"{location}.{key} is not a supported condition.")
        if key == "idle_seconds_gte" and not isinstance(value, (int, float)):
            raise ConfigError(f"{location}.{key} must be numeric.")
        if key == "process_running_any" and not (
            isinstance(value, list) and all(isinstance(item, str) for item in value)
        ):
            raise ConfigError(f"{location}.{key} must be a list of process names.")
        if key == "file_exists" and not isinstance(value, str):
            raise ConfigError(f"{location}.{key} must be a file path string.")
        if key == "file_contains":
            if not isinstance(value, dict):
                raise ConfigError(f"{location}.{key} must be an object.")
            if not isinstance(value.get("path"), str) or not isinstance(value.get("text"), str):
                raise ConfigError(f"{location}.{key} must include string path and text fields.")
        if key == "time_between":
            if not isinstance(value, dict):
                raise ConfigError(f"{location}.{key} must be an object.")
            for subkey in ("start", "end"):
                if not isinstance(value.get(subkey), str):
                    raise ConfigError(f"{location}.{key}.{subkey} must be a string.")
        if key == "battery_below_percent" and not isinstance(value, (int, float)):
            raise ConfigError(f"{location}.{key} must be numeric.")
        if key == "charging" and not isinstance(value, bool):
            raise ConfigError(f"{location}.{key} must be a boolean.")
        if key == "timer_active" and not isinstance(value, bool):
            raise ConfigError(f"{location}.{key} must be a boolean.")
        if key == "routine_phase" and not isinstance(value, (str, list)):
            raise ConfigError(f"{location}.{key} must be a string or list of strings.")


def _validate_action_references(payload: dict[str, Any]) -> None:
    scenes = payload["scenes"]
    presets = payload["presets"]

    def walk(action: dict[str, Any], location: str, seen: set[str] | None = None) -> None:
        seen = seen or set()
        if "scene" in action and action["scene"] not in scenes:
            raise ConfigError(f"{location}.scene refers to unknown scene '{action['scene']}'.")
        if "preset" in action:
            preset_name = action["preset"]
            if preset_name not in presets:
                raise ConfigError(f"{location}.preset refers to unknown preset '{preset_name}'.")
            if preset_name in seen:
                raise ConfigError(f"{location}.preset creates a cycle at '{preset_name}'.")
            walk(presets[preset_name], f"presets.{preset_name}", seen | {preset_name})

    for name, action in presets.items():
        walk(action, f"presets.{name}", {name})
    for name, routine in payload["routines"].items():
        for index, phase in enumerate(routine["phases"]):
            walk(phase["action"], f"routines.{name}.phases[{index}].action")
        if "completion_action" in routine:
            walk(routine["completion_action"], f"routines.{name}.completion_action")
    for name, action in payload["notify"].items():
        walk(action, f"notify.{name}")
    for index, alarm in enumerate(payload["alarms"]):
        walk(alarm["action"], f"alarms[{index}].action")
    for index, rule in enumerate(payload["rules"]):
        walk(rule["action"], f"rules[{index}].action")
    walk(payload["settings"]["default_action"], "settings.default_action")
    walk(payload["settings"]["quiet_hours"]["action"], "settings.quiet_hours.action")


def _validate_github(github: Any, events: dict[str, Any]) -> None:
    if not isinstance(github, dict):
        raise ConfigError("'github' must be an object.")
    allowed = {"enabled", "poll_seconds", "respect_quiet_hours", "actions_failed_event"}
    if set(github) - allowed:
        raise ConfigError("Unknown github setting. Credentials belong in the gh login, not config.")
    for key in ("enabled", "respect_quiet_hours"):
        if not isinstance(github.get(key), bool):
            raise ConfigError(f"'github.{key}' must be a boolean.")
    interval = github.get("poll_seconds")
    if (
        isinstance(interval, bool)
        or not isinstance(interval, (int, float))
        or not math.isfinite(interval)
        or interval < 60
    ):
        raise ConfigError("'github.poll_seconds' must be a finite number of at least 60.")
    event = github.get("actions_failed_event")
    if not isinstance(event, str) or event not in events:
        raise ConfigError("'github.actions_failed_event' must name an event in notify.")


def _validate_chime(chime: Any) -> None:
    if not isinstance(chime, dict):
        raise ConfigError("'chime' must be an object.")
    for key in ("enabled", "respect_quiet_hours"):
        if not isinstance(chime.get(key), bool):
            raise ConfigError(f"'chime.{key}' must be a boolean.")
    for key in ("color", "secondary_color"):
        if not isinstance(chime.get(key), str):
            raise ConfigError(f"'chime.{key}' must be a string.")
    for key in ("on_ms", "off_ms", "count"):
        value = chime.get(key)
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
            raise ConfigError(f"'chime.{key}' must be a positive number.")
    minute = chime.get("minute")
    if not isinstance(minute, int) or isinstance(minute, bool) or not 0 <= minute <= 59:
        raise ConfigError("'chime.minute' must be an integer from 0 to 59.")
    window = chime.get("catch_up_window_seconds")
    if not isinstance(window, (int, float)) or isinstance(window, bool) or window < 0:
        raise ConfigError("'chime.catch_up_window_seconds' must be zero or greater.")


def _validate_alarms(alarms: Any) -> None:
    from .alarms import DAY_NAMES

    if not isinstance(alarms, list):
        raise ConfigError("'alarms' must be a list.")

    seen: set[str] = set()
    for index, alarm in enumerate(alarms):
        location = f"alarms[{index}]"
        if not isinstance(alarm, dict):
            raise ConfigError(f"{location} must be an object.")

        name = alarm.get("name")
        if not isinstance(name, str) or not name:
            raise ConfigError(f"{location}.name must be a non-empty string.")
        # Names key the dedupe state, so duplicates would silently share a slot
        # record and one alarm would swallow the other.
        if name in seen:
            raise ConfigError(f"{location}.name '{name}' is used more than once.")
        seen.add(name)

        at = alarm.get("at")
        if not isinstance(at, str) or len(at.split(":")) != 2:
            raise ConfigError(f"{location}.at must look like HH:MM.")
        try:
            hour, minute = (int(part) for part in at.split(":"))
        except ValueError as exc:
            raise ConfigError(f"{location}.at must look like HH:MM.") from exc
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ConfigError(f"{location}.at must be a real time of day.")

        _validate_action(alarm.get("action"), f"{location}.action")

        if "enabled" in alarm and not isinstance(alarm["enabled"], bool):
            raise ConfigError(f"{location}.enabled must be a boolean.")
        if "respect_quiet_hours" in alarm and not isinstance(alarm["respect_quiet_hours"], bool):
            raise ConfigError(f"{location}.respect_quiet_hours must be a boolean.")
        if "catch_up_window_seconds" in alarm:
            window = alarm["catch_up_window_seconds"]
            if not isinstance(window, (int, float)) or isinstance(window, bool) or window < 0:
                raise ConfigError(f"{location}.catch_up_window_seconds must be zero or greater.")

        if "days" in alarm:
            days = alarm["days"]
            if not isinstance(days, list):
                raise ConfigError(f"{location}.days must be a list of day names.")
            for day in days:
                if not isinstance(day, str) or str(day).strip().lower()[:3] not in DAY_NAMES:
                    raise ConfigError(
                        f"{location}.days entry '{day}' is not a day name "
                        f"(use {', '.join(sorted(DAY_NAMES))})."
                    )


def _validate_show(show: Any, scenes: dict[str, Any]) -> None:
    from .defaults import scene_duration_seconds

    if not isinstance(show, dict):
        raise ConfigError("'show' must be an object.")
    for key in ("enabled", "respect_quiet_hours"):
        if not isinstance(show.get(key), bool):
            raise ConfigError(f"'show.{key}' must be a boolean.")

    at = show.get("at")
    if not isinstance(at, str) or len(at.split(":")) != 2:
        raise ConfigError("'show.at' must look like HH:MM.")
    try:
        hour, minute = (int(part) for part in at.split(":"))
    except ValueError as exc:
        raise ConfigError("'show.at' must look like HH:MM.") from exc
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ConfigError("'show.at' must be a real time of day.")

    window = show.get("catch_up_window_seconds")
    if not isinstance(window, (int, float)) or isinstance(window, bool) or window < 0:
        raise ConfigError("'show.catch_up_window_seconds' must be zero or greater.")

    max_seconds = show.get("max_seconds")
    if not isinstance(max_seconds, (int, float)) or isinstance(max_seconds, bool) or max_seconds <= 0:
        raise ConfigError("'show.max_seconds' must be a positive number.")

    scene_name = show.get("scene")
    if not isinstance(scene_name, str):
        raise ConfigError("'show.scene' must be a string.")
    if scene_name not in scenes:
        raise ConfigError(f"'show.scene' refers to unknown scene '{scene_name}'.")

    scene = scenes[scene_name]
    if scene.get("loop", False):
        raise ConfigError(f"'show.scene' cannot be a looping scene ('{scene_name}' loops forever).")
    duration = scene_duration_seconds(scene)
    if duration > float(max_seconds):
        raise ConfigError(
            f"'show.scene' runs {duration:.2f}s, over the {max_seconds}s cap in show.max_seconds."
        )


def _validate_calendar(calendar: Any) -> None:
    if not isinstance(calendar, dict):
        raise ConfigError("'calendar' must be an object.")
    if not isinstance(calendar.get("enabled"), bool):
        raise ConfigError("'calendar.enabled' must be a boolean.")
    provider = calendar.get("provider")
    if not isinstance(provider, str):
        raise ConfigError("'calendar.provider' must be a string.")
    if provider not in ("graph", "outlook"):
        raise ConfigError(f"'calendar.provider' must be 'graph' or 'outlook', got '{provider}'.")
    graph = calendar.get("graph")
    if not isinstance(graph, dict):
        raise ConfigError("'calendar.graph' must be an object.")
    for key in ("client_id", "tenant_id"):
        if not isinstance(graph.get(key), str):
            raise ConfigError(f"'calendar.graph.{key}' must be a string.")
    # A missing client_id is deliberately not a config error: it surfaces at
    # poll time as an error snapshot instead, so an unconfigured Graph provider
    # degrades to "no events" rather than making every other command refuse to
    # run. `calendar status` is where you go to see it.
    if not isinstance(calendar.get("auto_watch_on_launch"), bool):
        raise ConfigError("'calendar.auto_watch_on_launch' must be a boolean.")
    for key in ("poll_seconds", "lookahead_minutes"):
        if not isinstance(calendar.get(key), (int, float)) or calendar[key] <= 0:
            raise ConfigError(f"'calendar.{key}' must be a positive number.")
    if not isinstance(calendar.get("ignore_all_day"), bool):
        raise ConfigError("'calendar.ignore_all_day' must be a boolean.")
    statuses = calendar.get("alert_statuses")
    if (
        not isinstance(statuses, list)
        or not statuses
        or not all(isinstance(item, int) and not isinstance(item, bool) for item in statuses)
    ):
        raise ConfigError("'calendar.alert_statuses' must be a non-empty list of integers.")
    for key in ("available_color", "busy_meeting_color"):
        if not isinstance(calendar.get(key), str):
            raise ConfigError(f"'calendar.{key}' must be a string.")
    for key in ("ten_minute_warning", "five_minute_warning"):
        _validate_calendar_warning(key, calendar.get(key))
    # A ten-minute warning needs the meeting to already be in the polling
    # window, or it can never fire.
    if float(calendar["lookahead_minutes"]) < 10:
        raise ConfigError("'calendar.lookahead_minutes' must be at least 10, or the ten-minute warning never fires.")


def _validate_calendar_warning(key: str, warning: Any) -> None:
    if not isinstance(warning, dict):
        raise ConfigError(f"'calendar.{key}' must be an object.")
    if not isinstance(warning.get("color"), str):
        raise ConfigError(f"'calendar.{key}.color' must be a string.")
    for field in ("on_ms", "off_ms", "count"):
        if not isinstance(warning.get(field), (int, float)) or warning[field] <= 0:
            raise ConfigError(f"'calendar.{key}.{field}' must be a positive number.")
