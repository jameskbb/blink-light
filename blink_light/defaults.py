from __future__ import annotations

import colorsys
import math
from copy import deepcopy


RAINBOW_SWIRL_STEPS = 28
RAINBOW_SWIRL_STEP_SECONDS = 0.34
RAINBOW_SWIRL_FADE_OUT_SECONDS = 0.4

# The same rotation compressed to notification length. A notification is an
# interruption that has to be over before the next one arrives, and every other
# one on the light runs 1-2.6s; ten seconds of rainbow per event would hold the
# light through whatever came next.
RAINBOW_SWEEP_STEPS = 12
RAINBOW_SWEEP_STEP_SECONDS = 0.18
RAINBOW_SWEEP_FADE_OUT_SECONDS = 0.3


def _hsv_hex(hue: float, value: float) -> str:
    red, green, blue = colorsys.hsv_to_rgb(hue % 1.0, 1.0, max(0.0, min(1.0, value)))
    return "#{:02X}{:02X}{:02X}".format(round(red * 255), round(green * 255), round(blue * 255))


def scale_brightness(color: str, factor: float) -> str:
    """Scale a #RRGGBB color's brightness, preserving its hue.

    Scaling all three channels by the same factor keeps the ratios between them
    fixed, which is what keeps the hue recognisable - dimming by clamping or by
    blending toward grey would shift it.
    """
    text = color.lstrip("#")
    if len(text) != 6:
        raise ValueError(f"Expected a #RRGGBB color, got '{color}'.")
    factor = max(0.0, min(1.0, factor))
    channels = (int(text[index : index + 2], 16) for index in (0, 2, 4))
    return "#{:02X}{:02X}{:02X}".format(*(round(value * factor) for value in channels))


# Herdr's own light blue, so the desk light and the Herdr UI agree on what
# "complete" looks like. Dimmed for notifications: a light on the desk is in
# peripheral vision all day, and full brightness reads as an alarm.
HERDR_BLUE = "#4FC3F7"
NOTIFY_BRIGHTNESS = 0.5
# Full brightness for "the agent finished", alone among the notifications. It
# was dimmed twice over on the theory that the most frequent event should be
# the quietest. That theory was answering bursts of unexplained flashing which
# no notification had asked for - see the device watchdog note in watcher.py -
# so dimming treated a symptom that was never this event's to begin with, and
# cost the signal: at 12.5% it was easy to miss across a desk, which is the one
# thing this flash exists not to be.
AGENT_DONE_BRIGHTNESS = 1.0
AGENT_DONE_COLOR = scale_brightness(HERDR_BLUE, AGENT_DONE_BRIGHTNESS)
# Blocked shares the palette but sits redder, so the two are told apart by hue
# as well as by rhythm.
AGENT_BLOCKED_COLOR = scale_brightness("#FF3D00", NOTIFY_BRIGHTNESS)
CI_FAILED_COLOR = scale_brightness("#FF7800", NOTIFY_BRIGHTNESS)

# Violet is the one hue no notification, painted preset or calendar colour
# uses. The three pull-request events share it so they read as one family,
# told apart from each other by rhythm rather than hue - the way the two
# standup alerts share gold.
PR_VIOLET = "#8957E5"
PR_COLOR = scale_brightness(PR_VIOLET, NOTIFY_BRIGHTNESS)
PR_DIM_COLOR = scale_brightness(PR_VIOLET, NOTIFY_BRIGHTNESS * 0.4)

# Standup alerts. Full brightness, unlike the notification colours above: these
# are meant to catch your eye across the room, not sit quietly in the corner.
STANDUP_COLOR = "#FFD700"


def rainbow_scene(
    step_count: int = RAINBOW_SWIRL_STEPS,
    step_seconds: float = RAINBOW_SWIRL_STEP_SECONDS,
    fade_out_seconds: float = RAINBOW_SWIRL_FADE_OUT_SECONDS,
) -> dict:
    """A two-LED hue rotation that swells and settles.

    Built rather than hand-written so the timing stays provably inside the cap.
    Every step is a *fade* to the next color, and the two LEDs are held half a
    turn apart in hue, so the light reads as a rotating gradient rather than a
    strobe. Brightness never drops to zero mid-show - the envelope bottoms out
    at 45% - which is what keeps it pretty instead of blinky.

    The step count and duration are arguments because the same rotation serves
    two lengths: the daily show, and a notification-length sweep. Compressing
    it rather than writing a second scene keeps one rainbow on the light, so a
    short one still reads as "the rainbow, briefly".
    """
    steps = []
    for index in range(step_count):
        progress = index / (step_count - 1)
        led = 1 if index % 2 == 0 else 2
        # Each LED advances a full turn over the show; the odd LED trails by
        # half a rotation so the pair always shows complementary colors.
        hue = progress + (0.0 if led == 1 else 0.5)
        value = 0.45 + 0.55 * math.sin(math.pi * progress)
        steps.append(
            {
                "color": _hsv_hex(hue, value),
                "seconds": step_seconds,
                "led": led,
            }
        )
    steps.append({"color": "#000000", "seconds": fade_out_seconds, "led": 0})
    return {"loop": False, "repeat": 1, "steps": steps}


def rainbow_swirl_scene() -> dict:
    """The daily show's rainbow: a slow full rotation, under 10 seconds."""
    return rainbow_scene()


def rainbow_sweep_scene() -> dict:
    """The same rotation at notification length, around 2.5 seconds."""
    return rainbow_scene(
        RAINBOW_SWEEP_STEPS,
        RAINBOW_SWEEP_STEP_SECONDS,
        RAINBOW_SWEEP_FADE_OUT_SECONDS,
    )


def scene_duration_seconds(scene: dict) -> float:
    """Wall-clock length of one scene run, ignoring loop."""
    return sum(float(step["seconds"]) for step in scene.get("steps", [])) * int(scene.get("repeat", 1))


def swell_scene(color: str, seconds: float, rise_steps: int = 4) -> dict:
    """One slow climb out of the dark, then a drop back to it.

    The blink(1) fades to each pattern line over that line's own duration, so a
    ladder of brightnesses plays as a single continuous rise rather than as
    separate steps. That rise is the whole point: every other notification
    repeats a fixed breath, so a scene that *grows* is the one shape none of
    them can be mistaken for, which is what makes it legible as "and there is
    more behind this one" rather than as another finish.
    """
    fall_seconds = seconds * 0.2
    rise_seconds = (seconds - fall_seconds) / rise_steps
    steps = [
        {
            "color": scale_brightness(color, (index + 1) / rise_steps),
            "seconds": round(rise_seconds, 3),
        }
        for index in range(rise_steps)
    ]
    steps.append({"color": "#000000", "seconds": round(fall_seconds, 3)})
    return {"loop": False, "repeat": 1, "steps": steps}


# One slow breath in full Herdr blue - one finish, one blink. It repeated
# twice, which at a glance was indistinguishable from two agents finishing back
# to back. Counting flashes is only a signal if the count means something.
# Named before the scene table because the overflow scene is derived from it.
AGENT_DONE_SCENE = {
    "loop": False,
    "repeat": 1,
    "steps": [
        {"color": AGENT_DONE_COLOR, "seconds": 0.45},
        {"color": "#000000", "seconds": 0.45},
    ],
}
# A quarter longer than an ordinary finish. Long enough to register as "that
# one was different" in peripheral vision, short enough that it is still over
# before you have turned your head.
AGENT_DONE_MORE_SECONDS = scene_duration_seconds(AGENT_DONE_SCENE) * 1.25


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
    "rainbow_swirl": rainbow_swirl_scene(),
    # Full brightness, unlike the dimmed notification scenes: a queue that just
    # grew is meant to turn your head, and dimming a rainbow muddies the hues
    # that make it recognisable in the first place.
    "triage_request_scene": rainbow_sweep_scene(),
    # Paired thumps with a rest distinguish CI from three even agent breaths.
    "ci_failed_scene": {
        "loop": False,
        "repeat": 2,
        "steps": [
            {"color": CI_FAILED_COLOR, "seconds": 0.12},
            {"color": "#000000", "seconds": 0.12},
            {"color": CI_FAILED_COLOR, "seconds": 0.18},
            {"color": "#000000", "seconds": 0.18},
            {"color": "#000000", "seconds": 0.60},
        ],
    },
    # Notification scenes are short and non-looping: they must finish on their
    # own so the light returns to whatever the watcher was showing.
    #
    # Every step is a fade rather than a jump, so these read as breaths instead
    # of blinks. Done and blocked differ in both hue and rhythm: two slow
    # breaths versus three quick ones.
    "agent_done_scene": AGENT_DONE_SCENE,
    # Fired in place of the last finish a burst is allowed, when more agents
    # are still sitting unannounced behind it. See BUILTIN_NOTIFY.
    "agent_done_more_scene": swell_scene(AGENT_DONE_COLOR, AGENT_DONE_MORE_SECONDS),
    "agent_blocked_scene": {
        "loop": False,
        "repeat": 3,
        "steps": [
            {"color": AGENT_BLOCKED_COLOR, "seconds": 0.22},
            {"color": "#000000", "seconds": 0.20},
        ],
    },
    # The three pull-request scenes differ from the agent and CI scenes, and
    # from each other, by rhythm as well as hue: a relay across both LEDs, a
    # swell with a flicker, and a two-step climb.
    "pr_review_requested_scene": {
        "loop": False,
        "repeat": 2,
        "steps": [
            {"color": PR_COLOR, "seconds": 0.18, "led": 1},
            {"color": PR_DIM_COLOR, "seconds": 0.18, "led": 1},
            {"color": PR_COLOR, "seconds": 0.18, "led": 2},
            {"color": PR_DIM_COLOR, "seconds": 0.18, "led": 2},
            {"color": "#000000", "seconds": 0.40, "led": 0},
        ],
    },
    "pr_mentioned_scene": {
        "loop": False,
        "repeat": 2,
        "steps": [
            {"color": PR_COLOR, "seconds": 0.50},
            {"color": PR_DIM_COLOR, "seconds": 0.07},
            {"color": PR_COLOR, "seconds": 0.07},
            {"color": PR_DIM_COLOR, "seconds": 0.07},
            {"color": PR_COLOR, "seconds": 0.07},
            {"color": "#000000", "seconds": 0.50},
        ],
    },
    "pr_review_received_scene": {
        "loop": False,
        "repeat": 2,
        "steps": [
            {"color": PR_DIM_COLOR, "seconds": 0.15},
            {"color": PR_DIM_COLOR, "seconds": 0.12},
            {"color": PR_COLOR, "seconds": 0.15},
            {"color": PR_COLOR, "seconds": 0.12},
            {"color": "#000000", "seconds": 0.45},
        ],
    },
    # Standup alerts. The two are told apart by urgency - a short warning, then
    # an insistent one - rather than by colour, so they read as one pair.
    "standup_warning_scene": {
        "loop": False,
        "repeat": 3,
        "steps": [
            {"color": STANDUP_COLOR, "seconds": 0.16},
            {"color": "#000000", "seconds": 0.16},
        ],
    },
    "standup_now_scene": {
        "loop": False,
        "repeat": 6,
        "steps": [
            {"color": STANDUP_COLOR, "seconds": 0.12},
            {"color": "#000000", "seconds": 0.12},
        ],
    },
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
    # Three ticks, not the 8000 this was. Against a 5-second tick that left
    # 3 seconds of slack - less than one tick - so a single slow wake handed
    # the light to the blink(1)'s firmware. The margin is a multiple of the
    # tick on purpose: the two numbers are only meaningful together, and
    # `config validate` refuses a watchdog the tick could never beat.
    "watchdog_millis": 15000,
    "stop_turns_light_off": True,
    # The watcher was only ever started at logon, so one that died mid-session
    # took the calendar colours with it until somebody noticed the light had
    # gone plain - days, the first time it happened. The scheduler loop is
    # already always on, so it is the thing that can notice. An explicit
    # `watch stop` is still honoured; only an unplanned exit is restarted.
    "supervise_watcher": True,
    "default_action": {"preset": "available"},
    # Out of office from 17:00, so the light goes dark rather than chiming and
    # showing calendar colours to an empty desk. The 17:00 show opts out above.
    "quiet_hours": {
        "enabled": True,
        "start": "17:00",
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

# The event names are fixed; `notify` decides how each one looks.
PR_EVENTS = {
    "review_requested": "pr_review_requested",
    "mentioned": "pr_mentioned",
    "review_received": "pr_review_received",
}

BUILTIN_NOTIFY = {
    "ci_failed": {"scene": "ci_failed_scene"},
    "agent_done": {"scene": "agent_done_scene"},
    # When several agents finish at once the light would otherwise fire once
    # per agent, and a stream of identical flashes says nothing a single one
    # did not. The Herdr plugin caps a burst at three and spends the third on
    # this event instead, so the cap is visible rather than silent: you can
    # tell "three finished" from "three finished and there are more".
    "agent_done_more": {"scene": "agent_done_more_scene"},
    "agent_blocked": {"scene": "agent_blocked_scene"},
    "pr_review_requested": {"scene": "pr_review_requested_scene"},
    "pr_mentioned": {"scene": "pr_mentioned_scene"},
    "pr_review_received": {"scene": "pr_review_received_scene"},
    # For a queue an assistant or a bot feeds: "something new is waiting for
    # you". A rainbow because it belongs to none of the existing bands - not a
    # meeting, not a failure, not a pull request - and there is no mistaking it
    # for one of them at a glance.
    "triage_request": {"scene": "triage_request_scene"},
}

BUILTIN_GITHUB = {
    "enabled": False,
    "poll_seconds": 60,
    "respect_quiet_hours": True,
    "actions_failed_event": "ci_failed",
    "pr": {
        "review_requested": True,
        "mentioned": True,
        "review_received": True,
        "ignore_logins": [],
    },
}

BUILTIN_ALARMS = [
    {
        "name": "standup_warning",
        "at": "08:13",
        "action": {"scene": "standup_warning_scene"},
        "enabled": True,
        "catch_up_window_seconds": 120,
        "respect_quiet_hours": True,
    },
    {
        "name": "standup_now",
        "at": "08:15",
        "action": {"scene": "standup_now_scene"},
        "enabled": True,
        # Tighter than the default: a standup alert five minutes late is worse
        # than no alert, because it says the wrong thing about the time.
        "catch_up_window_seconds": 120,
        "respect_quiet_hours": True,
    },
    # A second run of the 17:00 rainbow, for the evenings you were heads-down
    # and worked straight through the first one. It is an alarm rather than a
    # second `show` block because that is what `alarms` is for: another
    # scheduled moment costs a config entry, not a code change. Opts out of
    # quiet hours for the same reason the show does - 17:15 is inside them.
    {
        "name": "rainbow_encore",
        "at": "17:15",
        "action": {"scene": "rainbow_swirl"},
        "enabled": True,
        "catch_up_window_seconds": 300,
        "respect_quiet_hours": False,
    },
]

BUILTIN_SHOW = {
    "enabled": True,
    "at": "17:00",
    "scene": "rainbow_swirl",
    "max_seconds": 10,
    "catch_up_window_seconds": 300,
    # Quiet hours open at 17:00 and the window is inclusive of its start, so a
    # show that respected them would never play again. It opts out instead:
    # the rainbow is the end-of-day marker, not something to be silenced by it.
    "respect_quiet_hours": False,
}

# Outlook's OlBusyStatus values, named so the config below reads as intent
# rather than as magic numbers.
OUTLOOK_FREE = 0
OUTLOOK_TENTATIVE = 1
OUTLOOK_BUSY = 2
OUTLOOK_OUT_OF_OFFICE = 3
OUTLOOK_WORKING_ELSEWHERE = 4

BUSY_STATUS_NAMES = {
    OUTLOOK_FREE: "Free",
    OUTLOOK_TENTATIVE: "Tentative",
    OUTLOOK_BUSY: "Busy",
    OUTLOOK_OUT_OF_OFFICE: "Out of Office",
    OUTLOOK_WORKING_ELSEWHERE: "Working Elsewhere",
}

BUILTIN_CALENDAR = {
    "enabled": True,
    # "graph" reads the server-side mailbox over Microsoft 365, which is what
    # the Outlook web app shows, and is the better provider once you have signed
    # in. The default stays "outlook" only because COM needs no setup at all:
    # a fresh clone works before anyone has registered an Entra app.
    "provider": "outlook",
    "graph": {
        # Your own Entra app registration. See README, "Signing in with
        # Microsoft 365" - there is no default, because an app id is per-tenant.
        "client_id": "",
        # "organizations" works for any work or school account; pin it to your
        # tenant id to refuse everything else.
        "tenant_id": "organizations",
    },
    "auto_watch_on_launch": True,
    "poll_seconds": 30,
    # Has to stay above the ten-minute warning, or a meeting is still invisible
    # when its first warning comes due.
    "lookahead_minutes": 15,
    "ignore_all_day": True,
    # Only a meeting you are actually expected at moves the light. A Free block
    # is informational and an Out of Office block is usually the whole day, so
    # neither one gets a colour or a warning.
    "alert_statuses": [OUTLOOK_TENTATIVE, OUTLOOK_BUSY],
    "available_color": "#00C853",
    "busy_meeting_color": "#D50000",
    # The two warnings differ in hue *and* rhythm, so which one just fired is
    # readable from across the room without counting blinks: two slow ones at
    # ten minutes out, four quick ones at five.
    #
    # Cyan and magenta rather than the yellow and orange they used to be. Six
    # different things were flashing in the red-to-yellow band - these two, both
    # standup alarms, a failed CI run and a blocked agent - and at a glance they
    # were one warm blink. These two are the only cyan and the only magenta on
    # the light, which is what makes them readable. Keep them out of that band
    # if you re-colour them.
    "ten_minute_warning": {"color": "#00E5FF", "on_ms": 220, "off_ms": 180, "count": 2},
    "five_minute_warning": {"color": "#FF00C8", "on_ms": 130, "off_ms": 110, "count": 4},
}


def default_config(serial: str | None = None) -> dict:
    payload = {
        "device": {"serial": serial},
        "calendar": deepcopy(BUILTIN_CALENDAR),
        "github": deepcopy(BUILTIN_GITHUB),
        "chime": deepcopy(BUILTIN_CHIME),
        "show": deepcopy(BUILTIN_SHOW),
        "alarms": deepcopy(BUILTIN_ALARMS),
        "notify": deepcopy(BUILTIN_NOTIFY),
        "presets": deepcopy(BUILTIN_PRESETS),
        "scenes": deepcopy(BUILTIN_SCENES),
        "routines": deepcopy(BUILTIN_ROUTINES),
        "rules": deepcopy(BUILTIN_RULES),
        "settings": deepcopy(BUILTIN_SETTINGS),
    }
    return payload
