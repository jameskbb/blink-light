from __future__ import annotations

from pathlib import Path
import unittest

from blink_light.config import ConfigError, build_effective_config, merge_config, validate_config
from blink_light.defaults import default_config
from blink_light.state import read_json

REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = REPO_ROOT / "blink-light.example.json"
LOCAL_CONFIG = REPO_ROOT / "blink-light.json"


class PinnedColourChecks:
    """The light runs the merged config, not defaults.py.

    A config written by `config init` pins every scene by value, so changing a
    colour in defaults.py alone leaves the real light on the old one -
    silently, because the rest of the suite only ever looks at
    default_config(). That happened: agent_done stayed dim orange for an hour
    after the blue landed. These compare the two.
    """

    config_path: Path

    @classmethod
    def setUpClass(cls) -> None:
        cls.effective = build_effective_config(cls.config_path)
        cls.defaults = default_config()

    def test_no_scene_pins_a_stale_colour(self) -> None:
        for name, scene in self.defaults["scenes"].items():
            if name not in self.effective["scenes"]:
                continue
            with self.subTest(scene=name):
                self.assertEqual(
                    [step["color"] for step in self.effective["scenes"][name]["steps"]],
                    [step["color"] for step in scene["steps"]],
                    f"{self.config_path.name} pins colours for '{name}' that defaults.py no longer uses",
                )

    def test_no_preset_pins_a_stale_colour(self) -> None:
        for name, preset in self.defaults["presets"].items():
            if "color" not in preset or name not in self.effective["presets"]:
                continue
            with self.subTest(preset=name):
                self.assertEqual(self.effective["presets"][name].get("color"), preset["color"])

    def test_the_calendar_colours_match_the_defaults(self) -> None:
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


class TemplateConfigTests(PinnedColourChecks, unittest.TestCase):
    config_path = TEMPLATE

    def test_the_template_is_exactly_what_config_init_writes_with_no_device(self) -> None:
        # Pinned to default_config() so the template can never become a second,
        # hand-edited source of truth that quietly disagrees with the code.
        # After changing a default, regenerate it with write_json.
        self.assertEqual(read_json(TEMPLATE, None), default_config())

    def test_the_template_names_no_device_and_no_account(self) -> None:
        template = read_json(TEMPLATE, None)
        self.assertIsNone(template["device"]["serial"])
        self.assertEqual(template["calendar"]["graph"]["client_id"], "")


@unittest.skipUnless(LOCAL_CONFIG.exists(), "no local blink-light.json on this machine")
class LocalConfigTests(PinnedColourChecks, unittest.TestCase):
    """The same stale-colour check, against the config this machine really runs."""

    config_path = LOCAL_CONFIG


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
