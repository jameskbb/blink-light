from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

import blink_light.chime as chime_module
from blink_light.chime import (
    chime_action,
    current_slot,
    fire_chime,
    maybe_fire_chime,
    next_slot,
    read_chime_state,
    run_chime_loop,
    seconds_until_next_slot,
    should_fire,
)
from blink_light.config import ConfigError, merge_config, validate_config
from blink_light.defaults import default_config
from blink_light.paths import build_paths


class FakeController:
    def __init__(self, serial: str | None = None):
        self.serial = serial
        self.applied: list[dict] = []
        self.closed = False

    def apply_action(self, action: dict, scenes: dict, persistent: bool) -> None:
        self.applied.append({"action": action, "persistent": persistent})

    def close(self) -> None:
        self.closed = True


def at(hour: int, minute: int = 0, second: int = 0) -> datetime:
    return datetime(2026, 4, 2, hour, minute, second, tzinfo=timezone.utc)


class ChimeSlotTests(unittest.TestCase):
    def test_current_slot_is_the_top_of_this_hour(self) -> None:
        self.assertEqual(current_slot(at(12, 0, 3)), at(12))
        self.assertEqual(current_slot(at(12, 59, 59)), at(12))

    def test_current_slot_rolls_back_when_minute_offset_is_ahead(self) -> None:
        self.assertEqual(current_slot(at(12, 10), minute=30), at(11, 30))

    def test_next_slot_is_always_in_the_future(self) -> None:
        self.assertEqual(next_slot(at(12, 0, 0)), at(13))
        self.assertEqual(next_slot(at(12, 30)), at(13))
        self.assertEqual(seconds_until_next_slot(at(12, 59, 30)), 30.0)

    def test_chime_action_is_a_single_white_pulse(self) -> None:
        action = chime_action(default_config())
        self.assertEqual(action["pulse"]["color"], "#FFFFFF")
        self.assertEqual(action["pulse"]["count"], 1)


class ChimeFiringTests(unittest.TestCase):
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
        # Quiet hours would otherwise suppress chimes during the test window.
        self.config["settings"]["quiet_hours"]["enabled"] = False

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_fires_once_per_hour(self) -> None:
        first = maybe_fire_chime(self.config, self.paths, controller_cls=FakeController, now=at(12, 0, 2))
        self.assertTrue(first["fired"])

        second = maybe_fire_chime(self.config, self.paths, controller_cls=FakeController, now=at(12, 3))
        self.assertFalse(second["fired"])
        self.assertEqual(second["reason"], "already-fired")

        third = maybe_fire_chime(self.config, self.paths, controller_cls=FakeController, now=at(13, 0, 1))
        self.assertTrue(third["fired"])

    def test_late_ticks_fall_outside_the_catch_up_window(self) -> None:
        due, reason, _ = should_fire(self.config, self.paths, at(12, 30))
        self.assertFalse(due)
        self.assertEqual(reason, "outside-catch-up-window")

    def test_disabled_chime_never_fires(self) -> None:
        self.config["chime"]["enabled"] = False
        result = maybe_fire_chime(self.config, self.paths, controller_cls=FakeController, now=at(12, 0, 1))
        self.assertFalse(result["fired"])
        self.assertEqual(result["reason"], "disabled")

    def test_quiet_hours_suppress_the_chime(self) -> None:
        self.config["settings"]["quiet_hours"].update({"enabled": True, "start": "22:30", "end": "07:00"})
        result = maybe_fire_chime(self.config, self.paths, controller_cls=FakeController, now=at(23, 0, 1))
        self.assertFalse(result["fired"])
        self.assertEqual(result["reason"], "quiet-hours")

    def test_quiet_hours_can_be_ignored_by_config(self) -> None:
        self.config["settings"]["quiet_hours"].update({"enabled": True, "start": "22:30", "end": "07:00"})
        self.config["chime"]["respect_quiet_hours"] = False
        result = maybe_fire_chime(self.config, self.paths, controller_cls=FakeController, now=at(23, 0, 1))
        self.assertTrue(result["fired"])

    def test_fire_chime_applies_a_non_persistent_pulse(self) -> None:
        controller = FakeController()
        fire_chime(self.config, self.paths, controller=controller, now=at(12, 0, 1))
        self.assertEqual(len(controller.applied), 1)
        self.assertEqual(controller.applied[0]["persistent"], False)
        self.assertEqual(controller.applied[0]["action"]["pulse"]["color"], "#FFFFFF")
        # A borrowed controller is left open for the caller to reuse.
        self.assertFalse(controller.closed)

    def test_test_mode_does_not_consume_the_hour(self) -> None:
        fire_chime(self.config, self.paths, controller_cls=FakeController, now=at(12, 0, 1), record=False)
        self.assertEqual(read_chime_state(self.paths), {})
        follow_up = maybe_fire_chime(self.config, self.paths, controller_cls=FakeController, now=at(12, 0, 5))
        self.assertTrue(follow_up["fired"])

    def test_run_loop_chimes_then_sleeps_until_the_next_slot(self) -> None:
        clock = iter([at(12, 0, 1), at(12, 0, 1), at(12, 0, 2), at(12, 0, 2)])
        slept: list[float] = []
        summary = run_chime_loop(
            self.config,
            self.paths,
            controller_cls=FakeController,
            now_factory=lambda: next(clock),
            sleep=slept.append,
            max_iterations=2,
        )
        self.assertEqual(summary["fired"], 1)
        self.assertEqual(summary["iterations"], 2)
        self.assertEqual(slept, [60.0, 60.0])


class AutostartTaskTests(unittest.TestCase):
    """The schtasks calls are stubbed; these pin the arguments we build."""

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.paths = build_paths(
            config_path=self.root / "blink-light.json",
            project_root=self.root,
            runtime_dir=self.root / "runtime",
            startup_dir=self.root / "startup",
        )
        (self.root / chime_module.AUTOSTART_SCRIPT_NAME).write_text("' stub", encoding="utf-8")
        self.calls: list[list[str]] = []
        self._original = chime_module._run_schtasks

        def fake_schtasks(arguments):
            self.calls.append(list(arguments))
            return SimpleNamespace(returncode=0, stdout="Status: Ready", stderr="")

        chime_module._run_schtasks = fake_schtasks

    def tearDown(self) -> None:
        chime_module._run_schtasks = self._original
        self.tempdir.cleanup()

    def test_enable_registers_a_logon_task_and_starts_it(self) -> None:
        result = chime_module.install_autostart_task(self.paths)
        create = self.calls[0]
        self.assertIn("/Create", create)
        self.assertEqual(create[create.index("/SC") + 1], "ONLOGON")
        self.assertEqual(create[create.index("/TN") + 1], "BlinkLight Autostart")
        self.assertIn(chime_module.AUTOSTART_SCRIPT_NAME, create[create.index("/TR") + 1])
        self.assertIn("/F", create)
        self.assertTrue(result["installed"])
        self.assertTrue(result["started_now"])
        self.assertIn(["/Run", "/TN", "BlinkLight Autostart"], self.calls)

    def test_enable_with_no_start_only_registers(self) -> None:
        result = chime_module.install_autostart_task(self.paths, start_now=False)
        self.assertFalse(result["started_now"])
        self.assertNotIn(["/Run", "/TN", "BlinkLight Autostart"], self.calls)

    def test_enable_does_not_restart_an_already_running_task(self) -> None:
        def running_schtasks(arguments):
            self.calls.append(list(arguments))
            return SimpleNamespace(returncode=0, stdout="Status: Running", stderr="")

        chime_module._run_schtasks = running_schtasks
        result = chime_module.install_autostart_task(self.paths)
        self.assertFalse(result["started_now"])
        self.assertNotIn(["/Run", "/TN", "BlinkLight Autostart"], self.calls)

    def test_disable_ends_the_run_before_deleting(self) -> None:
        result = chime_module.uninstall_autostart_task()
        self.assertEqual(self.calls[0], ["/End", "/TN", "BlinkLight Autostart"])
        self.assertIn("/Delete", self.calls[1])
        self.assertFalse(result["installed"])

    def test_missing_launcher_script_is_reported(self) -> None:
        (self.root / chime_module.AUTOSTART_SCRIPT_NAME).unlink()
        with self.assertRaises(RuntimeError):
            chime_module.install_autostart_task(self.paths)

    def test_delete_tolerates_a_task_that_is_not_there(self) -> None:
        chime_module._run_schtasks = lambda arguments: SimpleNamespace(
            returncode=1, stdout="", stderr="ERROR: The system cannot find the file specified."
        )
        self.assertFalse(chime_module.uninstall_autostart_task()["installed"])


class ChimeConfigTests(unittest.TestCase):
    def test_default_chime_section_validates(self) -> None:
        validate_config(default_config())

    def test_user_chime_overrides_merge(self) -> None:
        payload = merge_config({"chime": {"color": "#FF0000", "minute": 30}})
        self.assertEqual(payload["chime"]["color"], "#FF0000")
        self.assertEqual(payload["chime"]["minute"], 30)
        self.assertTrue(payload["chime"]["enabled"])

    def test_minute_out_of_range_is_rejected(self) -> None:
        payload = default_config()
        payload["chime"]["minute"] = 60
        with self.assertRaises(ConfigError):
            validate_config(payload)

    def test_non_positive_duration_is_rejected(self) -> None:
        payload = default_config()
        payload["chime"]["on_ms"] = 0
        with self.assertRaises(ConfigError):
            validate_config(payload)


if __name__ == "__main__":
    unittest.main()
