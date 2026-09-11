"""An undocked laptop should be a note in the log, not a pile of tracebacks.

The loop retries a missed slot once a minute for the whole catch-up window, so
before this the one fact "the light is not plugged in" arrived as five
identical stack traces an hour - enough noise to hide a real fault. These pin
the quiet behaviour, and pin that everything *other* than an absent device
still gets its traceback.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from blink_light.chime import run_chime_loop
from blink_light.defaults import default_config
from blink_light.device import DeviceError
from blink_light.paths import build_paths


class AbsentController:
    """Stands in for a blink(1) that is not plugged in."""

    def __init__(self, serial: str | None = None):
        self.serial = serial

    def apply_action(self, action: dict, scenes: dict, persistent: bool) -> None:
        raise DeviceError("Configured blink(1) serial '12ab34cd' is not connected.")

    def close(self) -> None:
        pass


class BrokenController:
    def __init__(self, serial: str | None = None):
        self.serial = serial

    def apply_action(self, action: dict, scenes: dict, persistent: bool) -> None:
        raise ValueError("something genuinely unexpected")

    def close(self) -> None:
        pass


class AbsentDeviceLoggingTests(unittest.TestCase):
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

    def _log(self) -> str:
        """What actually lands in blink-light.log.

        The loop owns the logger's handlers - it strips whatever is attached
        and points it at the log file - so assertLogs cannot see it. Reading
        the file is both the only option and the honest one: it is the artifact
        the runbook tells you to open.
        """
        if not self.paths.log_path.exists():
            return ""
        return self.paths.log_path.read_text(encoding="utf-8")

    def _run(self, controller_cls, start: datetime, iterations: int):
        clock = {"now": start}

        def now_factory() -> datetime:
            current = clock["now"]
            clock["now"] = current + timedelta(minutes=1)
            return current

        return run_chime_loop(
            self.config,
            self.paths,
            controller_cls=controller_cls,
            now_factory=now_factory,
            sleep=lambda seconds: None,
            max_iterations=iterations,
        )

    def test_a_disconnected_light_is_reported_once_per_hour_not_once_per_retry(self) -> None:
        start = datetime(2026, 4, 2, 12, 0, 1, tzinfo=timezone.utc)
        summary = self._run(AbsentController, start, iterations=5)

        self.assertEqual(summary["fired"], 0)
        skipped = [line for line in self._log().splitlines() if "device not connected" in line]
        self.assertEqual(len(skipped), 1)
        self.assertIn("12ab34cd", skipped[0])

    def test_the_absent_device_note_carries_no_stack_trace(self) -> None:
        start = datetime(2026, 4, 2, 12, 0, 1, tzinfo=timezone.utc)
        self._run(AbsentController, start, iterations=3)

        self.assertNotIn("Traceback", self._log())

    def test_a_later_hour_gets_its_own_note(self) -> None:
        # Undocked across two slots is two facts, not one - the second hour
        # really did miss its chime.
        first = datetime(2026, 4, 2, 12, 0, 1, tzinfo=timezone.utc)
        self._run(AbsentController, first, iterations=3)
        self._run(AbsentController, first + timedelta(hours=1), iterations=3)

        skipped = [line for line in self._log().splitlines() if "device not connected" in line]
        self.assertEqual(len(skipped), 2)

    def test_a_missed_alarm_gets_its_own_note_after_the_chime_already_had_one(self) -> None:
        # One note per hour used to cover every effect, so the 08:00 chime's
        # note hid both standup alarms that missed later in the same hour.
        # Two minutes pass per wake, so nine wakes reach both alarm windows.
        start = datetime(2026, 4, 2, 8, 0, 1, tzinfo=timezone.utc)
        self._run(AbsentController, start, iterations=9)

        skipped = [line for line in self._log().splitlines() if "device not connected" in line]
        self.assertEqual(len(skipped), 3)
        self.assertIn("Chime skipped", skipped[0])
        self.assertIn("Alarm standup_warning skipped", skipped[1])
        self.assertIn("Alarm standup_now skipped", skipped[2])

    def test_a_real_failure_still_logs_its_traceback(self) -> None:
        start = datetime(2026, 4, 2, 12, 0, 1, tzinfo=timezone.utc)
        self._run(BrokenController, start, iterations=1)

        joined = self._log()
        self.assertIn("Chime failed", joined)
        self.assertIn("Traceback", joined)
        self.assertIn("something genuinely unexpected", joined)

    def test_an_absent_device_does_not_stop_the_loop(self) -> None:
        start = datetime(2026, 4, 2, 12, 0, 1, tzinfo=timezone.utc)
        summary = self._run(AbsentController, start, iterations=4)

        self.assertEqual(summary["stopped_by"], "max-iterations")
        self.assertEqual(summary["iterations"], 4)


if __name__ == "__main__":
    unittest.main()
