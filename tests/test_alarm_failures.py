"""A missing light on one alarm must not hide the next alarm's miss.

The scheduler used to stop at the first alarm that raised, so a second alarm
due in the same wake was never tried and never reported. These pin that the
scheduler's error hook sees every due alarm, that a failed alarm stays unfired
so its catch-up window can still land it, and that callers without the hook
still get the exception.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from blink_light.alarms import fire_due_alarms
from blink_light.defaults import default_config
from blink_light.device import DeviceError
from blink_light.paths import build_paths


class WarningUnpluggedController:
    """Fails only the standup warning, so the test can see the next alarm still run."""

    def __init__(self, serial: str | None = None):
        self.serial = serial

    def apply_action(self, action: dict, scenes: dict, persistent: bool) -> None:
        if action.get("scene") == "standup_warning_scene":
            raise DeviceError("Configured blink(1) serial '12ab34cd' is not connected.")

    def close(self) -> None:
        pass


class PluggedController:
    def __init__(self, serial: str | None = None):
        self.serial = serial

    def apply_action(self, action: dict, scenes: dict, persistent: bool) -> None:
        pass

    def close(self) -> None:
        pass


class AlarmFailureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        root = Path(self.tempdir.name)
        self.paths = build_paths(
            config_path=root / "blink-light.json",
            project_root=root,
            runtime_dir=root / "runtime",
            startup_dir=root / "startup",
        )
        self.config = default_config()
        self.config["settings"]["quiet_hours"]["enabled"] = False
        # Both due in the same wake, the warning first.
        for alarm in self.config["alarms"]:
            alarm["at"] = "08:13"
        self.now = datetime(2026, 4, 2, 8, 13, 2, tzinfo=timezone.utc)

    def test_with_an_error_hook_a_failing_alarm_does_not_stop_the_next_one(self) -> None:
        failures = []
        results = fire_due_alarms(
            self.config,
            self.paths,
            controller_cls=WarningUnpluggedController,
            now=self.now,
            on_error=lambda alarm, error: failures.append((alarm["name"], error)),
        )

        self.assertEqual([result["alarm"] for result in results], ["standup_now"])
        self.assertEqual([name for name, _error in failures], ["standup_warning"])
        self.assertIsInstance(failures[0][1], DeviceError)

    def test_a_failed_alarm_stays_unfired_so_plugging_back_in_still_catches_it(self) -> None:
        fire_due_alarms(
            self.config,
            self.paths,
            controller_cls=WarningUnpluggedController,
            now=self.now,
            on_error=lambda alarm, error: None,
        )
        again = fire_due_alarms(
            self.config,
            self.paths,
            controller_cls=PluggedController,
            now=self.now + timedelta(seconds=30),
            on_error=lambda alarm, error: None,
        )

        self.assertEqual([result["alarm"] for result in again], ["standup_warning"])

    def test_without_an_error_hook_the_first_failure_still_raises(self) -> None:
        with self.assertRaises(DeviceError):
            fire_due_alarms(
                self.config,
                self.paths,
                controller_cls=WarningUnpluggedController,
                now=self.now,
            )


if __name__ == "__main__":
    unittest.main()
