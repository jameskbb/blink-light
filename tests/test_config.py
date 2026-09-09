from __future__ import annotations

from pathlib import Path
import unittest

from blink_light.config import ConfigError, build_effective_config, merge_config, validate_config
from blink_light.defaults import default_config

REPO_ROOT = Path(__file__).resolve().parents[1]


class CommittedConfigTests(unittest.TestCase):
    """The light runs the merged config, not defaults.py.

    blink-light.json was generated from the defaults and pins every scene by
    value, so changing a colour in defaults.py alone leaves the real light on
    the old one - silently, because the rest of the suite only ever looks at
    default_config(). That happened: agent_done stayed dim orange for an hour
    after the blue landed. These compare the two.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.effective = build_effective_config(REPO_ROOT / "blink-light.json")
        cls.defaults = default_config()

    def test_no_scene_pins_a_stale_colour(self) -> None:
        for name, scene in self.defaults["scenes"].items():
            if name not in self.effective["scenes"]:
                continue
            with self.subTest(scene=name):
                self.assertEqual(
                    [step["color"] for step in self.effective["scenes"][name]["steps"]],
                    [step["color"] for step in scene["steps"]],
                    f"blink-light.json pins colours for '{name}' that defaults.py no longer uses",
                )

    def test_no_preset_pins_a_stale_colour(self) -> None:
        for name, preset in self.defaults["presets"].items():
            if "color" not in preset or name not in self.effective["presets"]:
                continue
            with self.subTest(preset=name):
                self.assertEqual(self.effective["presets"][name].get("color"), preset["color"])

    def test_the_committed_calendar_colours_match_the_defaults(self) -> None:
        for key in ("available_color", "busy_meeting_color"):
            with self.subTest(key=key):
                self.assertEqual(
                    self.effective["calendar"][key], self.defaults["calendar"][key]
                )
        for key in ("ten_minute_warning", "five_minute_warning"):
            with self.subTest(key=key):
                self.assertEqual(
                    self.effective["calendar"][key]["color"],
                    self.defaults["calendar"][key]["color"],
                )


class ConfigTests(unittest.TestCase):
    def test_default_config_is_valid(self) -> None:
        payload = default_config(serial="12ab34cd")
        validate_config(payload)

    def test_user_sections_merge_over_defaults(self) -> None:
        payload = merge_config(
            {
                "presets": {"available": {"color": "#123456", "fade_ms": 10}},
                "settings": {"tick_seconds": 5},
            }
        )
        self.assertEqual(payload["presets"]["available"]["color"], "#123456")
        self.assertEqual(payload["settings"]["tick_seconds"], 5)
        self.assertIn("busy", payload["presets"])

    def test_unknown_scene_reference_fails_validation(self) -> None:
        payload = default_config()
        payload["presets"]["bad"] = {"scene": "missing_scene"}
        with self.assertRaises(ConfigError):
            validate_config(payload)

    def test_preset_cycles_fail_validation(self) -> None:
        payload = default_config()
        payload["presets"]["a"] = {"preset": "b"}
        payload["presets"]["b"] = {"preset": "a"}
        with self.assertRaises(ConfigError):
            validate_config(payload)


if __name__ == "__main__":
    unittest.main()
