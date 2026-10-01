"""Build the printable desk reference card, docs/blink-light-reference-card.pdf.

Every colour, time and duration on the card is read out of
`blink_light.defaults` rather than typed in, for the same reason
`tests/test_readme_schedule.py` exists: a printout pinned above a desk is worse
than no printout once it disagrees with the light. Prose - what an effect
*means* - is written here, because nothing in the config knows that.

Run it after changing a default:

    .venv\\Scripts\\python.exe tools/build_reference_card.py
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from blink_light import defaults as D  # noqa: E402
from pdfwriter import (  # noqa: E402
    COURIER,
    COURIER_BOLD,
    HELVETICA,
    HELVETICA_BOLD,
    PAGE_HEIGHT,
    PAGE_WIDTH,
    Document,
    Page,
    text_width,
    wrap,
)

OUTPUT = REPO_ROOT / "docs" / "blink-light-reference-card.pdf"

# Print-first palette. The README's artwork is white-on-navy, which is right on
# a screen and wrong on paper - a full-bleed dark page is unreadable from most
# office printers and empties a toner cartridge. The navy survives as one
# header band and as the ink colour; the page itself stays white.
NAVY = "#0B1430"
INK = "#11182C"
MUTED = "#626E85"
FAINT = "#9AA4B8"
RULE = "#D5DAE4"
STRIPE = "#F4F6FA"
BAND_SUB = "#9FB3D9"
BAND_EYEBROW = "#6E7FA8"

MARGIN = 44.0
CONTENT_WIDTH = PAGE_WIDTH - 2 * MARGIN

# Column geometry, shared by the two event tables so the eye can run straight
# down the colour chips from one table into the next.
COL_WHEN = (MARGIN, 94.0)
COL_WHAT = (MARGIN + 100, 190.0)
COL_CHIP = (MARGIN + 298, 14.0)
COL_COLOUR = (MARGIN + 318, 118.0)
COL_LENGTH = (MARGIN + 444, CONTENT_WIDTH - 444)


def seconds(scene_name: str) -> str:
    return f"{D.scene_duration_seconds(D.BUILTIN_SCENES[scene_name]):.2f}s"


def solid(color: str, label: str) -> dict:
    return {"kind": "solid", "hex": color.upper(), "label": label}


def gradient(start: str, end: str, label: str) -> dict:
    return {"kind": "gradient", "hex": f"{start.upper()} to {end.upper()}", "label": label,
            "stops": [start, end]}


def hue_ramp(count: int) -> list[str]:
    """A smooth spectrum.

    The scene's own step colours alternate between the two LEDs and are half a
    rotation apart, so printed side by side they read as noise rather than as a
    rainbow. The swatch shows the rotation the pair traces instead.
    """
    return [D._hsv_hex(index / count, 0.92) for index in range(count)]


def rainbow(label: str) -> dict:
    first = D.BUILTIN_SCENES["rainbow_swirl"]["steps"][0]["color"]
    return {"kind": "rainbow", "hex": f"from {first}", "label": label, "stops": hue_ramp(18)}


def alarm_time(name: str) -> str:
    return next(alarm["at"] for alarm in D.BUILTIN_ALARMS if alarm["name"] == name)


def chime_length() -> str:
    chime = D.BUILTIN_CHIME
    return f"{chime['count'] * (chime['on_ms'] + chime['off_ms']) / 1000:.2f}s"


def warning_length(warning: dict) -> str:
    return f"{warning['count'] * (warning['on_ms'] + warning['off_ms']) / 1000:.2f}s"


SCHEDULE_ROWS = [
    (
        f"{alarm_time('standup_warning')} daily",
        "Standup in 2 minutes. Three flashes.",
        solid(D.STANDUP_COLOR, "gold"),
        seconds("standup_warning_scene"),
    ),
    (
        f"{alarm_time('standup_now')} daily",
        "Standup now. Six faster flashes.",
        solid(D.STANDUP_COLOR, "gold"),
        seconds("standup_now_scene"),
    ),
    (
        f":{D.BUILTIN_CHIME['minute']:02d} every hour",
        "One white breath. Another hour has gone.",
        solid(D.BUILTIN_CHIME["color"], "white"),
        chime_length(),
    ),
    (
        f"{D.BUILTIN_SHOW['at']} daily",
        "Rainbow swirl across both LEDs. End of the working day.",
        rainbow(f"{len(D.BUILTIN_SCENES['rainbow_swirl']['steps']) - 1} hues"),
        seconds("rainbow_swirl"),
    ),
    (
        f"{alarm_time('rainbow_encore')} daily",
        "The rainbow again, for the evenings you worked through the first one.",
        rainbow(f"{len(D.BUILTIN_SCENES['rainbow_swirl']['steps']) - 1} hues"),
        seconds("rainbow_swirl"),
    ),
]

_DONE_MORE_START = D.BUILTIN_SCENES["agent_done_more_scene"]["steps"][0]["color"]

EVENT_ROWS = [
    (
        "AI agent finishes",
        "One slow breath. Come back and review it.",
        solid(D.AGENT_DONE_COLOR, "Herdr blue, full"),
        seconds("agent_done_scene"),
    ),
    (
        "...and more are waiting",
        "One slow climb out of the dark. The burst cap is showing.",
        gradient(_DONE_MORE_START, D.AGENT_DONE_COLOR, "Herdr blue, climbing"),
        seconds("agent_done_more_scene"),
    ),
    (
        "AI agent blocked",
        "Three quicker breaths. It needs an answer from you.",
        solid(D.AGENT_BLOCKED_COLOR, "dim red"),
        seconds("agent_blocked_scene"),
    ),
    (
        "GitHub Actions run fails",
        "Two pairs of heartbeat thumps, with a rest between.",
        solid(D.CI_FAILED_COLOR, "dim orange-red"),
        seconds("ci_failed_scene"),
    ),
    (
        "PR review requested",
        "A pulse handed from the top LED to the bottom, twice.",
        solid(D.PR_COLOR, "dim violet"),
        seconds("pr_review_requested_scene"),
    ),
    (
        "@mentioned on a PR",
        "A slow swell with a flicker at its peak, twice.",
        solid(D.PR_COLOR, "dim violet"),
        seconds("pr_mentioned_scene"),
    ),
    (
        "Your PR was reviewed",
        "A two-step climb, twice.",
        solid(D.PR_DIM_COLOR, "dimmer violet"),
        seconds("pr_review_received_scene"),
    ),
    (
        "New request in a queue",
        "A rainbow sweep across both LEDs.",
        rainbow(f"{len(D.BUILTIN_SCENES['triage_request_scene']['steps']) - 1} hues"),
        seconds("triage_request_scene"),
    ),
]

_CAL = D.BUILTIN_CALENDAR

RESTING_ROWS = [
    (
        "Nothing scheduled",
        "Steady. The light is just on.",
        solid(_CAL["available_color"], "green"),
        "held",
    ),
    (
        "Meeting in 10 minutes",
        "Two slow blinks. Time to wrap up.",
        solid(_CAL["ten_minute_warning"]["color"], "cyan"),
        warning_length(_CAL["ten_minute_warning"]),
    ),
    (
        "Meeting in 5 minutes",
        "Four quick blinks.",
        solid(_CAL["five_minute_warning"]["color"], "magenta"),
        warning_length(_CAL["five_minute_warning"]),
    ),
    (
        "In a Busy or Tentative meeting",
        "Steady. Visible to anyone walking up.",
        solid(_CAL["busy_meeting_color"], "red"),
        "held",
    ),
    (
        f"Idle {D.BUILTIN_RULES[1]['when']['idle_seconds_gte'] // 60}+ minutes",
        "Steady. You stepped away.",
        solid(D.BUILTIN_PRESETS["away"]["color"], "blue"),
        "held",
    ),
]

# The bands from the README's "Telling them apart". One LED carries a dozen
# meanings, so the card ends on the grouping rather than on another list of
# effects: this is the part you read when the light has already flashed.
BAND_ROWS = [
    ([_CAL["ten_minute_warning"]["color"], _CAL["five_minute_warning"]["color"]],
     "Cyan, then magenta",
     "A meeting is coming",
     "10 minutes out, then 5"),
    ([_CAL["busy_meeting_color"]],
     "Red, steady",
     "You are in the meeting",
     "Busy or Tentative"),
    ([D.STANDUP_COLOR, D.CI_FAILED_COLOR, D.AGENT_BLOCKED_COLOR],
     "Gold, orange, dim red",
     "Something wants you",
     "Standup, failed CI, blocked agent"),
    ([D.PR_COLOR, D.PR_DIM_COLOR],
     "Dim violet",
     "Pull requests",
     "Review asked, @mention, review in"),
    ([_CAL["available_color"], D.BUILTIN_PRESETS["away"]["color"], D.AGENT_DONE_COLOR],
     "Green, blue",
     "Your own state",
     "Free, idle, agent finished"),
    ([D.BUILTIN_CHIME["color"]],
     "White",
     "The hour",
     "The chime, on its own"),
]

# The hand-set presets. These are the only colours on the light you choose
# rather than something choosing for you, which is why they get a strip of
# their own rather than a row in a table of events.
PRESET_SWATCHES = [
    ("available", "free, the default"),
    ("busy", "in a meeting"),
    ("calendar_free", "a Free block"),
    ("away", "stepped away"),
    ("focus", "heads-down"),
    ("break", "on a break"),
]

PREVIEW_COMMANDS = [
    ("alarm test standup_warning", "the 2-minute standup warning"),
    ("chime test", "the hourly white breath"),
    ("show test", "the end-of-day rainbow"),
    ("notify run agent_done", "an AI agent finishing"),
    ("notify run agent_blocked", "an AI agent stuck"),
    ("notify run pr_mentioned", "an @mention on a pull request"),
]


def draw_band(page: Page) -> float:
    """The title band, and the rainbow strip that closes it."""
    height = 104.0
    page.rect(0, 0, PAGE_WIDTH, height, NAVY)
    page.text(MARGIN, 36, "WINDOWS / PYTHON / BLINK(1)", HELVETICA, 7.4, BAND_EYEBROW, 2.4)
    page.text(MARGIN, 68, "BLINK LIGHT", HELVETICA_BOLD, 27, "#FFFFFF", 1.4)
    page.text(MARGIN, 88, "YOUR DESK, IN COLOUR", HELVETICA, 11, BAND_SUB, 3.2)

    note = "WHAT EACH COLOUR MEANS"
    page.text(
        PAGE_WIDTH - MARGIN - text_width(note, HELVETICA_BOLD, 8.2, 1.8),
        68,
        note,
        HELVETICA_BOLD,
        8.2,
        BAND_SUB,
        1.8,
    )
    sub = "the shipped defaults"
    page.text(
        PAGE_WIDTH - MARGIN - text_width(sub, HELVETICA, 8.2, 0.6),
        82,
        sub,
        HELVETICA,
        8.2,
        BAND_EYEBROW,
        0.6,
    )

    stops = hue_ramp(72)
    segment = PAGE_WIDTH / len(stops)
    for index, color in enumerate(stops):
        page.rect(index * segment, height, segment + 0.5, 5.0, color)
    return height + 5.0


def section(page: Page, y: float, title: str, subtitle: str = "") -> float:
    """A section rule with its heading sitting on top of it."""
    page.text(MARGIN, y + 9, title.upper(), HELVETICA_BOLD, 10.0, NAVY, 1.9)
    if subtitle:
        page.text(
            MARGIN + text_width(title.upper(), HELVETICA_BOLD, 10.0, 1.9) + 10,
            y + 9,
            subtitle,
            HELVETICA,
            8.4,
            MUTED,
            0.2,
        )
    page.line(MARGIN, y + 15, CONTENT_WIDTH, RULE, 0.7)
    page.rect(MARGIN, y + 15, 34, 2.0, NAVY)
    return y + 24


def chip(page: Page, x: float, y: float, spec: dict, width: float = 14.0, height: float = 14.0) -> None:
    """A colour swatch. Gradients and rainbows are drawn as their own stops."""
    stops = spec.get("stops")
    if stops:
        if spec["kind"] == "gradient":
            start, end = stops
            bands = 14
            step = width / bands
            for index in range(bands):
                blend = index / (bands - 1)
                mixed = "#" + "".join(
                    f"{round(int(start.lstrip('#')[pair:pair + 2], 16) * (1 - blend) + int(end.lstrip('#')[pair:pair + 2], 16) * blend):02X}"
                    for pair in (0, 2, 4)
                )
                page.rect(x + index * step, y, step + 0.4, height, mixed)
        else:
            step = width / len(stops)
            for index, color in enumerate(stops):
                page.rect(x + index * step, y, step + 0.4, height, color)
    else:
        page.rect(x, y, width, height, spec["hex"])
    outline(page, x, y, width, height)


def outline(page: Page, x: float, y: float, width: float, height: float) -> None:
    """A hairline box.

    White is a colour the light actually uses, and an unoutlined white swatch
    is an empty space on the page. The same box keeps the near-black violets
    from reading as a printing fault.
    """
    page.line(x, y, width, RULE, 0.5)
    page.line(x, y + height - 0.5, width, RULE, 0.5)
    page.rect(x, y, 0.5, height, RULE)
    page.rect(x + width - 0.5, y, 0.5, height, RULE)


def effect_table(page: Page, y: float, rows: list[tuple], first_header: str = "WHEN") -> float:
    header_y = y
    for (x, _), label in (
        (COL_WHEN, first_header),
        (COL_WHAT, "WHAT THE LIGHT DOES"),
        (COL_COLOUR, "COLOUR"),
        (COL_LENGTH, "LENGTH"),
    ):
        page.text(x, header_y + 7, label, HELVETICA_BOLD, 6.8, FAINT, 1.1)
    y = header_y + 12

    for index, (when, what, spec, length) in enumerate(rows):
        when_lines = wrap(when, HELVETICA_BOLD, 8.6, COL_WHEN[1])
        what_lines = wrap(what, HELVETICA, 8.6, COL_WHAT[1])
        body = max(len(when_lines), len(what_lines))
        height = max(body * 10.4 + 9.0, 25.0)
        if index % 2 == 0:
            page.rect(MARGIN - 5, y, CONTENT_WIDTH + 10, height, STRIPE)

        top = y + 11.5
        for line_index, line in enumerate(when_lines):
            page.text(COL_WHEN[0], top + line_index * 10.4, line, HELVETICA_BOLD, 8.6, INK)
        for line_index, line in enumerate(what_lines):
            page.text(COL_WHAT[0], top + line_index * 10.4, line, HELVETICA, 8.6, INK)

        chip(page, COL_CHIP[0], y + 5.0, spec)
        page.text(COL_COLOUR[0], top, spec["hex"], COURIER, 7.4, INK)
        page.text(COL_COLOUR[0], top + 9.4, spec["label"], HELVETICA, 7.4, MUTED)
        page.text(COL_LENGTH[0], top, length, HELVETICA, 8.6, MUTED)
        y += height
    page.line(MARGIN, y + 1, CONTENT_WIDTH, RULE, 0.5)
    return y + 12


def band_table(page: Page, y: float) -> float:
    for x, label in ((MARGIN, "BAND"), (MARGIN + 150, "MEANS"), (MARGIN + 300, "WHICH EFFECTS")):
        page.text(x, y + 7, label, HELVETICA_BOLD, 6.8, FAINT, 1.1)
    y += 12

    for index, (stops, name, means, effects) in enumerate(BAND_ROWS):
        height = 24.0
        if index % 2 == 0:
            page.rect(MARGIN - 5, y, CONTENT_WIDTH + 10, height, STRIPE)
        for stop_index, color in enumerate(stops):
            page.rect(MARGIN + stop_index * 13, y + 6, 11, 11, color)
            outline(page, MARGIN + stop_index * 13, y + 6, 11, 11)
        page.text(MARGIN + len(stops) * 13 + 6, y + 15, name, HELVETICA, 8.4, INK)
        page.text(MARGIN + 150, y + 15, means, HELVETICA_BOLD, 8.4, INK)
        page.text(MARGIN + 300, y + 15, effects, HELVETICA, 8.4, MUTED)
        y += height
    page.line(MARGIN, y + 1, CONTENT_WIDTH, RULE, 0.5)
    return y + 12


def preset_strip(page: Page, y: float) -> float:
    """The hand-set presets, as a palette rather than as rows."""
    cell = CONTENT_WIDTH / len(PRESET_SWATCHES)
    for index, (name, meaning) in enumerate(PRESET_SWATCHES):
        x = MARGIN + index * cell
        color = D.BUILTIN_PRESETS[name]["color"]
        page.rect(x, y, cell - 8, 15, color)
        outline(page, x, y, cell - 8, 15)
        page.text(x, y + 26, name, HELVETICA_BOLD, 7.8, INK)
        page.text(x, y + 35.5, color.upper(), COURIER, 6.8, MUTED)
        page.text(x, y + 45, meaning, HELVETICA, 7.0, MUTED)
    y += 62
    caption = (
        "Timer routines paint these same presets: pomodoro is focus then break. Every routine"
        " ends on timer_done - white and green, until cleared."
    )
    for line in wrap(caption, HELVETICA, 7.8, CONTENT_WIDTH):
        page.text(MARGIN, y, line, HELVETICA, 7.8, MUTED)
        y += 10.0
    return y + 2


def note_box(page: Page, y: float, title: str, lines: list[str]) -> float:
    height = 20.0 + len(lines) * 11.0
    page.rect(MARGIN - 5, y, CONTENT_WIDTH + 10, height, STRIPE)
    page.rect(MARGIN - 5, y, 2.4, height, NAVY)
    page.text(MARGIN + 6, y + 14, title.upper(), HELVETICA_BOLD, 8.0, NAVY, 1.3)
    for index, line in enumerate(lines):
        page.text(MARGIN + 6, y + 27 + index * 11.0, line, HELVETICA, 8.4, INK)
    return y + height + 12


def footer(page: Page, number: int, total: int) -> None:
    y = PAGE_HEIGHT - 34
    page.line(MARGIN, y, CONTENT_WIDTH, RULE, 0.5)
    page.text(
        MARGIN,
        y + 13,
        "blink-light · defaults from blink_light/defaults.py · github.com/jameskbb/blink-light",
        HELVETICA,
        7.2,
        FAINT,
        0.2,
    )
    label = f"{number} / {total}"
    page.text(
        PAGE_WIDTH - MARGIN - text_width(label, HELVETICA_BOLD, 7.2, 0.8),
        y + 13,
        label,
        HELVETICA_BOLD,
        7.2,
        FAINT,
        0.8,
    )


def running_head(page: Page, text: str) -> float:
    page.rect(0, 0, PAGE_WIDTH, 5.0, NAVY)
    page.text(MARGIN, 30, text.upper(), HELVETICA_BOLD, 8.0, FAINT, 2.0)
    return 44.0


def build() -> bytes:
    document = Document()

    first = document.page()
    y = draw_band(first) + 20
    y = section(first, y, "On a schedule", "fires whether or not the watcher is running")
    y = effect_table(first, y, SCHEDULE_ROWS)
    y = section(first, y, "When something happens", "one-shots played over whatever is showing")
    y = effect_table(first, y, EVENT_ROWS)
    y = section(first, y, "Set it yourself", "blink-light.bat preset <name>")
    y = preset_strip(first, y)
    y = note_box(
        first,
        y,
        "Quiet hours",
        [
            f"{D.BUILTIN_SETTINGS['quiet_hours']['start']} to {D.BUILTIN_SETTINGS['quiet_hours']['end']}"
            " the light is off and the chime and alarms stay silent.",
            "Both rainbows opt out: they are the end-of-day marker, not something the end of day silences.",
            "Standup sits outside the window, so it is unaffected.",
        ],
    )
    footer(first, 1, 2)

    second = document.page()
    y = running_head(second, "Blink Light · what each colour means")
    y = section(second, y, "The resting colour", "watcher only - run watch start")
    y = effect_table(second, y, RESTING_ROWS, first_header="SITUATION")
    y = note_box(
        second,
        y,
        "Which meetings count",
        [
            "Only Tentative and Busy move the light. Free, Out of Office and Working Elsewhere are ignored",
            "entirely - no colour, no warning. All-day events are ignored too.",
        ],
    )
    y = section(second, y, "Telling them apart", "one LED, a dozen meanings")
    y = band_table(second, y)
    y = note_box(
        second,
        y,
        "Why cyan and magenta",
        [
            "The meeting warnings were yellow and orange until five different flashes ended up in that warm",
            "band at once. Cyan and magenta are used by nothing else. Keep them out of that band if you recolour.",
        ],
    )

    y = section(second, y, "Preview any of it", "blink-light.bat <command>")
    for index, (command, meaning) in enumerate(PREVIEW_COMMANDS):
        if index % 2 == 0:
            second.rect(MARGIN - 5, y, CONTENT_WIDTH + 10, 15.0, STRIPE)
        second.text(MARGIN, y + 10.5, command, COURIER_BOLD, 8.0, NAVY)
        second.text(MARGIN + 190, y + 10.5, meaning, HELVETICA, 8.4, MUTED)
        y += 15.0
    second.line(MARGIN, y + 1, CONTENT_WIDTH, RULE, 0.5)
    y += 14

    y = note_box(
        second,
        y,
        "If nothing is flashing",
        [
            "devices - is the light seen at all? It hangs off the dock, so undocking unplugs it.",
            "status - what the light would do right now, and the reason when it would do nothing.",
            "watch status - the calendar colours above need the watcher running; the scheduler restarts a dead one.",
            "Quiet hours silence everything that has not opted out. Both rainbows have.",
        ],
    )

    second.text(
        MARGIN,
        y + 8,
        "Your own blink-light.json overrides any of this. Change a default, then rerun"
        " tools/build_reference_card.py.",
        HELVETICA,
        8.0,
        MUTED,
    )
    footer(second, 2, 2)

    return document.render()


def main() -> int:
    OUTPUT.write_bytes(build())
    print(f"wrote {OUTPUT.relative_to(REPO_ROOT)} ({OUTPUT.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
