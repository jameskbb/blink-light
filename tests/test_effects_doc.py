"""Keep docs/EFFECTS.md's scene blocks pasteable.

The doc's whole promise is that you can copy a block into `scenes` and it
works. A schema change - a renamed key, a tightened bound - would break that
promise silently, because prose does not fail a build. These run every block
through the real validator instead.

Same spirit as test_readme_schedule.py: assert the load-bearing facts, not the
wording.
"""

from __future__ import annotations

from pathlib import Path
import json
import re
import unittest

from blink_light.config import validate_config
from blink_light.defaults import default_config, scene_duration_seconds
from blink_light.device import BlinkDeviceController

EFFECTS = Path(__file__).resolve().parents[1] / "docs" / "EFFECTS.md"

JSON_BLOCK = re.compile(r"```json\n(.*?)```", re.DOTALL)


def documented_scenes() -> dict[str, dict]:
    """Every ```json block on the page, parsed as a `scenes` fragment."""
    text = EFFECTS.read_text(encoding="utf-8")
    scenes: dict[str, dict] = {}
    for block in JSON_BLOCK.findall(text):
        scenes.update(json.loads("{" + block + "}"))
    return scenes


class EffectsDocTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.scenes = documented_scenes()

    def test_the_page_actually_contains_scene_blocks(self) -> None:
        self.assertGreaterEqual(len(self.scenes), 5)

    def test_every_documented_scene_passes_config_validation(self) -> None:
        for name, scene in self.scenes.items():
            with self.subTest(scene=name):
                config = default_config()
                config["scenes"] = {**config["scenes"], name: scene}
                validate_config(config)

    def test_every_documented_scene_fits_the_device_pattern_memory(self) -> None:
        # The page tells the reader to design under 32 steps; it should not
        # then hand them a block that falls back to host playback.
        for name, scene in self.scenes.items():
            with self.subTest(scene=name):
                self.assertLessEqual(len(scene["steps"]), 32)

    def test_the_sunrise_ramp_is_short_enough_to_be_the_daily_show(self) -> None:
        # The page offers it as a drop-in for show.scene, so it has to survive
        # being pointed at.
        config = default_config()
        config["scenes"] = {**config["scenes"], "sunrise_show": self.scenes["sunrise_show"]}
        config["show"] = {**config["show"], "scene": "sunrise_show"}
        validate_config(config)
        self.assertLessEqual(
            scene_duration_seconds(self.scenes["sunrise_show"]),
            float(config["show"]["max_seconds"]),
        )

    def test_the_documented_step_limit_matches_the_devices_real_one(self) -> None:
        # The 32 in the doc's limits table comes from here, not from folklore.
        # Constructing the controller opens nothing; the device is lazy.
        device = BlinkDeviceController(serial=None)
        over = {"loop": False, "steps": [{"color": "#000000", "seconds": 0.01}] * 33}
        under = {"loop": False, "steps": [{"color": "#000000", "seconds": 0.01}] * 32}
        self.assertFalse(device.can_run_scene_on_device(over))
        self.assertTrue(device.can_run_scene_on_device(under))


if __name__ == "__main__":
    unittest.main()
