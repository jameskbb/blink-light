from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from blink_light.config import ConfigError, merge_config, validate_config
from blink_light.defaults import (
    RAINBOW_SWIRL_STEPS,
    default_config,
    rainbow_swirl_scene,
    scene_duration_seconds,
)
from blink_light.paths import build_paths
from blink_light.show import (
    current_slot,
    fire_show,
    maybe_fire_show,
    next_slot,
    read_show_state,
    seconds_until_next_slot,
    should_fire,
    show_status,
    show_time,
)


class FakeController:
    def __init__(self, serial: str | None = None):
        self.serial = serial
        self.scenes: list[dict] = []
        self.closed = False

    def play_scene(self, scene: dict, persistent: bool) -> None:
        self.scenes.append({"scene": scene, "persistent": persistent})

    def close(self) -> None:
        self.closed = True


def at(hour: int, minute: int = 0, second: int = 0) -> datetime:
    return datetime(2026, 4, 2, hour, minute, second, tzinfo=timezone.utc)


class RainbowSwirlSceneTests(unittest.TestCase):
    def test_scene_fits_inside_the_ten_second_cap(self) -> None:
        duration = scene_duration_seconds(rainbow_swirl_scene())
        self.assertLessEqual(duration, 10.0)
        # Comfortably fills the window rather than fizzling out early.
        self.assertGreater(duration, 8.0)

    def test_scene_fits_the_devices_32_step_pattern_memory(self) -> None:
        self.assertLessEqual(len(rainbow_swirl_scene()["steps"]), 32)

    def test_scene_does_not_loop(self) -> None:
        self.assertFalse(rainbow_swirl_scene()["loop"])

    def test_it_swirls_across_both_leds(self) -> None:
        steps = rainbow_swirl_scene()["steps"]
        swirl = steps[:RAINBOW_SWIRL_STEPS]
        self.assertEqual({step["led"] for step in swirl}, {1, 2})
        # Alternating, so color appears to travel between the two LEDs.
        self.assertEqual([step["led"] for step in swirl[:4]], [1, 2, 1, 2])

    def test_it_never_goes_dark_mid_show(self) -> None:
        """A black step in the middle would read as a strobe, not a swirl."""
        swirl = rainbow_swirl_scene()["steps"][:RAINBOW_SWIRL_STEPS]
        for step in swirl:
            channels = [int(step["color"][i : i + 2], 16) for i in (1, 3, 5)]
            self.assertGreater(max(channels), 100, f"{step['color']} is too dark to read as color")

    def test_it_ends_dark_on_both_leds(self) -> None:
        last = rainbow_swirl_scene()["steps"][-1]
        self.assertEqual(last["color"], "#000000")
        self.assertEqual(last["led"], 0)

    def test_hues_actually_travel_around_the_wheel(self) -> None:
        swirl = rainbow_swirl_scene()["steps"][:RAINBOW_SWIRL_STEPS]
        distinct = {step["color"] for step in swirl}
        self.assertGreater(len(distinct), RAINBOW_SWIRL_STEPS // 2)


class ShowSlotTests(unittest.TestCase):
    def test_slot_rolls_back_a_day_before_the_scheduled_time(self) -> None:
        self.assertEqual(current_slot(at(9, 0), (17, 0)).day, 1)
        self.assertEqual(current_slot(at(18, 0), (17, 0)), at(17, 0))

    def test_next_slot_is_always_in_the_future(self) -> None:
        self.assertEqual(next_slot(at(17, 0, 0), (17, 0)).day, 3)
        self.assertEqual(next_slot(at(16, 59), (17, 0)), at(17, 0))
        self.assertEqual(seconds_until_next_slot(at(16, 59, 30), (17, 0)), 30.0)

    def test_default_show_time_is_five_pm(self) -> None:
        self.assertEqual(show_time(default_config()), (17, 0))


class ShowFiringTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        self.paths = build_paths(
            config_path=root / "blink-light.json",
            project_root=root,
            runtime_dir=root / "runtime",
            startup_dir=root / "startup",
        )
        self.config = default_config()
        self.config["settings"]["quiet_hours"]["enabled"] = False

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_fires_once_per_day(self) -> None:
        first = maybe_fire_show(self.config, self.paths, controller_cls=FakeController, now=at(17, 0, 2))
        self.assertTrue(first["fired"])
        self.assertEqual(first["scene"], "rainbow_swirl")

        second = maybe_fire_show(self.config, self.paths, controller_cls=FakeController, now=at(17, 3))
        self.assertFalse(second["fired"])
        self.assertEqual(second["reason"], "already-fired")

    def test_plays_the_scene_non_persistently(self) -> None:
        controller = FakeController()
        fire_show(self.config, self.paths, controller=controller, now=at(17, 0, 1))
        self.assertEqual(len(controller.scenes), 1)
        self.assertFalse(controller.scenes[0]["persistent"])
        self.assertFalse(controller.closed)

    def test_late_run_falls_outside_the_catch_up_window(self) -> None:
        due, reason, _ = should_fire(self.config, self.paths, at(17, 30))
        self.assertFalse(due)
        self.assertEqual(reason, "outside-catch-up-window")

    def test_disabled_show_never_fires(self) -> None:
        self.config["show"]["enabled"] = False
        result = maybe_fire_show(self.config, self.paths, controller_cls=FakeController, now=at(17, 0, 1))
        self.assertFalse(result["fired"])
        self.assertEqual(result["reason"], "disabled")

    def test_test_mode_does_not_consume_the_day(self) -> None:
        fire_show(self.config, self.paths, controller_cls=FakeController, now=at(17, 0, 1), record=False)
        self.assertEqual(read_show_state(self.paths), {})
        self.assertTrue(
            maybe_fire_show(self.config, self.paths, controller_cls=FakeController, now=at(17, 0, 5))["fired"]
        )

    def test_unknown_scene_is_rejected(self) -> None:
        self.config["show"]["scene"] = "does_not_exist"
        with self.assertRaises(ValueError):
            fire_show(self.config, self.paths, controller_cls=FakeController, now=at(17, 0, 1))

    def test_status_reports_the_scene_length(self) -> None:
        payload = show_status(self.config, self.paths, at(9, 0))
        self.assertEqual(payload["at"], "17:00")
        self.assertEqual(payload["scene"], "rainbow_swirl")
        self.assertLessEqual(payload["scene_seconds"], payload["max_seconds"])
        self.assertTrue(payload["next_slot"].startswith("2026-04-02T17:00:00"))


class ShowConfigTests(unittest.TestCase):
    def test_default_show_section_validates(self) -> None:
        validate_config(default_config())

    def test_user_show_overrides_merge(self) -> None:
        payload = merge_config({"show": {"at": "18:30", "enabled": False}})
        self.assertEqual(payload["show"]["at"], "18:30")
        self.assertFalse(payload["show"]["enabled"])
        self.assertEqual(payload["show"]["scene"], "rainbow_swirl")

    def test_a_scene_over_the_cap_is_rejected(self) -> None:
        payload = default_config()
        payload["scenes"]["rainbow_swirl"]["steps"][0]["seconds"] = 30
        with self.assertRaises(ConfigError) as caught:
            validate_config(payload)
        self.assertIn("over the 10s cap", str(caught.exception))

    def test_a_looping_scene_is_rejected(self) -> None:
        payload = default_config()
        payload["show"]["scene"] = "hydrate_due_scene"
        with self.assertRaises(ConfigError):
            validate_config(payload)

    def test_a_bad_time_is_rejected(self) -> None:
        payload = default_config()
        payload["show"]["at"] = "25:00"
        with self.assertRaises(ConfigError):
            validate_config(payload)

    def test_an_unknown_scene_is_rejected(self) -> None:
        payload = default_config()
        payload["show"]["scene"] = "nope"
        with self.assertRaises(ConfigError):
            validate_config(payload)


if __name__ == "__main__":
    unittest.main()
