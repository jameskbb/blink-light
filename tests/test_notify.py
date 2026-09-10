from __future__ import annotations

import unittest

from blink_light.config import ConfigError, merge_config, validate_config
from blink_light.defaults import (
    AGENT_BLOCKED_COLOR,
    AGENT_DONE_COLOR,
    HERDR_BLUE,
    default_config,
    scale_brightness,
    scene_duration_seconds,
)
from blink_light.notify import NotifyError, fire_notify, list_events, resolve_event


class FakeController:
    def __init__(self, serial: str | None = None):
        self.serial = serial
        self.applied: list[dict] = []
        self.closed = False

    def apply_action(self, action: dict, scenes: dict, persistent: bool) -> None:
        self.applied.append({"action": action, "persistent": persistent})

    def close(self) -> None:
        self.closed = True


class NotifyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = default_config()

    def test_default_events_are_present(self) -> None:
        self.assertEqual(list_events(self.config), ["agent_blocked", "agent_done", "ci_failed"])

    def test_events_resolve_to_scenes(self) -> None:
        self.assertEqual(resolve_event(self.config, "agent_done"), {"scene": "agent_done_scene"})

    def test_a_preset_reference_is_followed(self) -> None:
        self.config["notify"]["via_preset"] = {"preset": "focus"}
        self.assertEqual(resolve_event(self.config, "via_preset"), self.config["presets"]["focus"])

    def test_a_preset_reference_to_nothing_is_rejected(self) -> None:
        self.config["notify"]["broken"] = {"preset": "nope"}
        with self.assertRaises(NotifyError):
            resolve_event(self.config, "broken")

    def test_unknown_event_lists_what_is_available(self) -> None:
        with self.assertRaises(NotifyError) as caught:
            resolve_event(self.config, "nope")
        self.assertIn("agent_done", str(caught.exception))

    def test_firing_is_always_non_persistent(self) -> None:
        """A notification is an interruption, not a new resting state."""
        controller = FakeController()
        result = fire_notify(self.config, "agent_done", controller=controller)
        self.assertTrue(result["notified"])
        self.assertEqual(len(controller.applied), 1)
        self.assertFalse(controller.applied[0]["persistent"])
        self.assertFalse(controller.closed)

    def test_an_owned_controller_is_closed(self) -> None:
        created: list[FakeController] = []

        def factory(serial=None):
            controller = FakeController(serial)
            created.append(controller)
            return controller

        fire_notify(self.config, "agent_done", controller_cls=factory)
        self.assertTrue(created[0].closed)

    def test_repeat_calls_all_fire(self) -> None:
        """Unlike the chime, notify has no dedupe."""
        controller = FakeController()
        for _ in range(3):
            fire_notify(self.config, "agent_done", controller=controller)
        self.assertEqual(len(controller.applied), 3)


class NotifySceneTests(unittest.TestCase):
    def test_notification_scenes_are_short_and_do_not_loop(self) -> None:
        scenes = default_config()["scenes"]
        for name in ("agent_done_scene", "agent_blocked_scene"):
            with self.subTest(scene=name):
                scene = scenes[name]
                # A looping notification would never hand the light back.
                self.assertFalse(scene["loop"])
                self.assertLess(scene_duration_seconds(scene), 3.0)

    def test_notification_scenes_fit_on_the_device(self) -> None:
        scenes = default_config()["scenes"]
        for name in ("agent_done_scene", "agent_blocked_scene"):
            with self.subTest(scene=name):
                self.assertLessEqual(len(scenes[name]["steps"]), 32)


class BrightnessTests(unittest.TestCase):
    def test_full_brightness_is_the_identity(self) -> None:
        self.assertEqual(scale_brightness("#DE7356", 1.0), "#DE7356")

    def test_zero_brightness_is_black(self) -> None:
        self.assertEqual(scale_brightness("#DE7356", 0.0), "#000000")

    def test_half_brightness_halves_every_channel(self) -> None:
        self.assertEqual(scale_brightness("#DE7356", 0.5), "#6F3A2B")

    def test_hue_ratios_are_preserved(self) -> None:
        """Dimming must not shift the colour, only its intensity."""
        original = (0xDE, 0x73, 0x56)
        dimmed_hex = scale_brightness("#DE7356", 0.5).lstrip("#")
        dimmed = tuple(int(dimmed_hex[i : i + 2], 16) for i in (0, 2, 4))
        for index in range(3):
            self.assertAlmostEqual(dimmed[index] / original[index], 0.5, delta=0.01)

    def test_a_factor_out_of_range_is_clamped(self) -> None:
        self.assertEqual(scale_brightness("#DE7356", 5.0), "#DE7356")
        self.assertEqual(scale_brightness("#DE7356", -1.0), "#000000")

    def test_a_malformed_colour_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            scale_brightness("#FFF", 0.5)

    def test_agent_done_is_herdr_blue_at_half(self) -> None:
        self.assertEqual(AGENT_DONE_COLOR, scale_brightness(HERDR_BLUE, 0.5))
        self.assertEqual(
            default_config()["scenes"]["agent_done_scene"]["steps"][0]["color"],
            AGENT_DONE_COLOR,
        )

    def test_done_and_blocked_are_visually_distinct(self) -> None:
        self.assertNotEqual(AGENT_DONE_COLOR, AGENT_BLOCKED_COLOR)


class NotifyConfigTests(unittest.TestCase):
    def test_default_notify_section_validates(self) -> None:
        validate_config(default_config())

    def test_user_events_merge_alongside_the_defaults(self) -> None:
        payload = merge_config({"notify": {"build_failed": {"color": "#FF0000"}}})
        self.assertEqual(payload["notify"]["build_failed"], {"color": "#FF0000"})
        self.assertIn("agent_done", payload["notify"])

    def test_an_event_pointing_at_an_unknown_scene_fails_validation(self) -> None:
        payload = default_config()
        payload["notify"]["agent_done"] = {"scene": "missing"}
        with self.assertRaises(ConfigError):
            validate_config(payload)

    def test_an_event_pointing_at_an_unknown_preset_fails_validation(self) -> None:
        payload = default_config()
        payload["notify"]["agent_done"] = {"preset": "missing"}
        with self.assertRaises(ConfigError):
            validate_config(payload)

    def test_a_malformed_event_fails_validation(self) -> None:
        payload = default_config()
        payload["notify"]["agent_done"] = {"color": "#FFF", "off": True}
        with self.assertRaises(ConfigError):
            validate_config(payload)


if __name__ == "__main__":
    unittest.main()
