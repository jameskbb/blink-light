from __future__ import annotations

import unittest

from blink_light.config import ConfigError, merge_config, validate_config
from blink_light.defaults import default_config


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
