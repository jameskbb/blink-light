from __future__ import annotations

from copy import deepcopy


BUILTIN_PRESETS = {
    "available": {"color": "#00C853", "fade_ms": 150},
    "busy": {"color": "#D50000", "fade_ms": 150},
    "calendar_free": {"color": "#8E24AA", "fade_ms": 150},
    "away": {"color": "#2962FF", "fade_ms": 200},
    "focus": {"color": "#FFB300", "fade_ms": 250},
    "break": {"color": "#00ACC1", "fade_ms": 250},
    "timer_done": {"scene": "timer_done_scene"},
    "hydrate_due": {"scene": "hydrate_due_scene"},
    "meeting_soon": {"scene": "meeting_soon_scene"},
}

BUILTIN_SCENES = {
    "timer_done_scene": {
        "loop": True,
        "steps": [
            {"color": "#FFFFFF", "seconds": 0.15},
            {"color": "#00C853", "seconds": 0.15},
            {"color": "#000000", "seconds": 0.18},
            {"color": "#FFFFFF", "seconds": 0.15},
            {"color": "#00C853", "seconds": 0.15},
            {"color": "#000000", "seconds": 1.00},
        ],
    },
    "hydrate_due_scene": {
        "loop": True,
        "steps": [
            {"color": "#26C6DA", "seconds": 0.35},
            {"color": "#000000", "seconds": 1.40},
        ],
    },
    "meeting_soon_scene": {
        "loop": True,
        "steps": [
            {"color": "#FF8F00", "seconds": 0.18},
            {"color": "#000000", "seconds": 0.15},
            {"color": "#FF8F00", "seconds": 0.18},
            {"color": "#000000", "seconds": 1.40},
        ],
    },
}

BUILTIN_ROUTINES = {
    "pomodoro": {
        "description": "25 minutes of focus followed by a 5 minute break.",
        "phases": [
            {"name": "focus", "minutes": 25, "action": {"preset": "focus"}},
            {"name": "short_break", "minutes": 5, "action": {"preset": "break"}},
        ],
        "completion_action": {"preset": "timer_done"},
    },
    "short_break": {
        "description": "Quick 5 minute reset.",
        "phases": [
            {"name": "short_break", "minutes": 5, "action": {"preset": "break"}},
        ],
        "completion_action": {"preset": "timer_done"},
    },
    "long_break": {
        "description": "Longer 15 minute reset.",
        "phases": [
            {"name": "long_break", "minutes": 15, "action": {"preset": "break"}},
        ],
        "completion_action": {"preset": "timer_done"},
    },
    "hydrate": {
        "description": "45 minute hydration reminder.",
        "phases": [
            {"name": "hydrate_wait", "minutes": 45, "action": {"preset": "available"}},
        ],
        "completion_action": {"preset": "hydrate_due"},
    },
    "stand": {
        "description": "60 minute stand-and-stretch reminder.",
        "phases": [
            {"name": "stand_wait", "minutes": 60, "action": {"preset": "available"}},
        ],
        "completion_action": {"preset": "meeting_soon"},
    },
}

BUILTIN_RULES = [
    {
        "name": "timer-completed",
        "when": {"routine_phase": "completed"},
        "action": {"preset": "timer_done"},
    },
    {
        "name": "idle-away",
        "when": {"idle_seconds_gte": 600},
        "action": {"preset": "away"},
    },
]

BUILTIN_SETTINGS = {
    "tick_seconds": 5,
    "watchdog_millis": 8000,
    "stop_turns_light_off": True,
    "default_action": {"preset": "available"},
    "quiet_hours": {
        "enabled": True,
        "start": "22:30",
        "end": "07:00",
        "action": {"off": True},
    },
}

BUILTIN_CHIME = {
    "enabled": True,
    "color": "#FFFFFF",
    "secondary_color": "#000000",
    "on_ms": 400,
    "off_ms": 400,
    "count": 1,
    "minute": 0,
    "catch_up_window_seconds": 300,
    "respect_quiet_hours": True,
}

BUILTIN_CALENDAR = {
    "enabled": True,
    "provider": "outlook",
    "auto_watch_on_launch": True,
    "poll_seconds": 30,
    "lookahead_minutes": 15,
    "ignore_all_day": True,
    "free_statuses": [0],
    "available_color": "#00C853",
    "free_meeting_color": "#8E24AA",
    "busy_meeting_color": "#D50000",
    "ten_minute_warning_color": "#FDD835",
    "two_minute_warning_color": "#FB8C00",
    "warning_on_ms": 180,
    "warning_off_ms": 120,
    "warning_count": 1,
}


def default_config(serial: str | None = None) -> dict:
    payload = {
        "device": {"serial": serial},
        "calendar": deepcopy(BUILTIN_CALENDAR),
        "chime": deepcopy(BUILTIN_CHIME),
        "presets": deepcopy(BUILTIN_PRESETS),
        "scenes": deepcopy(BUILTIN_SCENES),
        "routines": deepcopy(BUILTIN_ROUTINES),
        "rules": deepcopy(BUILTIN_RULES),
        "settings": deepcopy(BUILTIN_SETTINGS),
    }
    return payload
