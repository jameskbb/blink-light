"""Keep the README's schedule table honest.

A table of times, colours and durations is exactly the kind of documentation
that rots: someone retimes a scene and the README quietly starts lying. These
assert the numbers in the table still match what the defaults actually produce.

Deliberately narrow - only the load-bearing facts a reader would act on, so
prose edits do not break the build.
"""

from __future__ import annotations

from pathlib import Path
import re
import unittest

from blink_light.defaults import default_config, scene_duration_seconds

README = Path(__file__).resolve().parents[1] / "README.md"


def readme_text() -> str:
    return README.read_text(encoding="utf-8")


class ScheduleTableTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = readme_text()
        cls.config = default_config()

    def _row_for(self, needle: str) -> str:
        for line in self.text.splitlines():
            if needle in line and line.strip().startswith("|"):
                return line
        self.fail(f"No README table row mentions {needle!r}")

    def test_alarm_times_match_the_config(self) -> None:
        for alarm in self.config["alarms"]:
            with self.subTest(alarm=alarm["name"]):
                row = self._row_for(f"alarm test {alarm['name']}")
                self.assertIn(f"**{alarm['at']}**", row)

    def test_alarm_durations_match_their_scenes(self) -> None:
        for alarm in self.config["alarms"]:
            with self.subTest(alarm=alarm["name"]):
                scene = self.config["scenes"][alarm["action"]["scene"]]
                row = self._row_for(f"alarm test {alarm['name']}")
                self.assertIn(f"{scene_duration_seconds(scene):.2f}s", row)

    def test_alarm_colour_matches_its_scene(self) -> None:
        for alarm in self.config["alarms"]:
            with self.subTest(alarm=alarm["name"]):
                scene = self.config["scenes"][alarm["action"]["scene"]]
                colour = scene["steps"][0]["color"]
                row = self._row_for(f"alarm test {alarm['name']}")
                self.assertIn(colour, row)

    def test_show_time_and_duration_match(self) -> None:
        row = self._row_for("show test")
        self.assertIn(f"**{self.config['show']['at']}**", row)
        scene = self.config["scenes"][self.config["show"]["scene"]]
        self.assertIn(f"{scene_duration_seconds(scene):.2f}s", row)

    def test_chime_colour_matches(self) -> None:
        row = self._row_for("chime test")
        self.assertIn(self.config["chime"]["color"], row)

    def test_notify_colours_and_durations_match(self) -> None:
        for event, action in self.config["notify"].items():
            with self.subTest(event=event):
                scene = self.config["scenes"][action["scene"]]
                row = self._row_for(f"notify run {event}")
                self.assertIn(scene["steps"][0]["color"], row)
                self.assertIn(f"{scene_duration_seconds(scene):.2f}s", row)

    def test_quiet_hours_window_matches(self) -> None:
        quiet = self.config["settings"]["quiet_hours"]
        self.assertIn(f"{quiet['start']} – {quiet['end']}", self.text)

    def test_calendar_colours_match(self) -> None:
        calendar = self.config["calendar"]
        for key in (
            "available_color",
            "busy_meeting_color",
            "free_meeting_color",
            "ten_minute_warning_color",
            "two_minute_warning_color",
        ):
            with self.subTest(key=key):
                self.assertIn(calendar[key], self.text)

    def test_every_scheduled_thing_is_listed(self) -> None:
        """A new alarm must not be able to slip in undocumented."""
        for alarm in self.config["alarms"]:
            with self.subTest(alarm=alarm["name"]):
                self.assertIn(alarm["name"], self.text)

    def test_no_stale_hex_colours_are_advertised(self) -> None:
        """Every hex in the two event tables must exist in the config somewhere."""
        in_use = {
            step["color"].upper()
            for scene in self.config["scenes"].values()
            for step in scene["steps"]
        }
        in_use |= {value.upper() for value in self.config["calendar"].values() if isinstance(value, str)}
        in_use |= {action["color"].upper() for action in self.config["presets"].values() if "color" in action}
        in_use.add(self.config["chime"]["color"].upper())

        start = self.text.index("## What the light does")
        end = self.text.index("## Hourly Chime")
        for match in re.finditer(r"`(#[0-9A-Fa-f]{6})`", self.text[start:end]):
            colour = match.group(1).upper()
            with self.subTest(colour=colour):
                self.assertIn(colour, in_use, f"README advertises {colour}, which no config value uses")


if __name__ == "__main__":
    unittest.main()
