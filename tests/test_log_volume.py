"""The log should grow with what happened, not with how often something looked.

The watcher ticks every five seconds and used to log its action on every tick,
about 17,000 identical lines a day into the file the scheduler also writes, and
nothing ever trimmed that file. These pin one line per change, a rotation at
process start that can never take the process down, and the watcher lines that
were missing: which process wrote a line, why it stopped, and a calendar that
quietly stopped answering.
"""

from __future__ import annotations

from datetime import datetime, timezone
import logging
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from blink_light.calendar_source import CalendarSnapshot
from blink_light.chime import configure_loop_logging, release_loop_logging
from blink_light.defaults import default_config
from blink_light.log_file import close_log
from blink_light.paths import build_paths
from blink_light.state import LOG_MAX_BYTES, rotate_log, write_json
from blink_light.watcher import run_watch_loop


class QuietController:
    def __init__(self, serial: str | None = None):
        self.serial = serial

    def apply_action(self, action: dict, scenes: dict, persistent: bool) -> None:
        pass

    def enable_watchdog(self, millis: int) -> None:
        pass

    def disable_watchdog(self) -> None:
        pass

    def off(self) -> None:
        pass

    def close(self) -> None:
        pass


class CrashingController(QuietController):
    def apply_action(self, action: dict, scenes: dict, persistent: bool) -> None:
        raise RuntimeError("invented crash")


def _paths(root: Path):
    return build_paths(
        config_path=root / "blink-light.json",
        project_root=root,
        runtime_dir=root / "runtime",
        startup_dir=root / "startup",
    )


class WatcherLogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        # The watcher closes its own log, but a test that fails half-way must
        # not leave the file open and block the temp directory's cleanup.
        self.addCleanup(close_log, logging.getLogger("blink_light.watcher"))
        self.paths = _paths(Path(self.tempdir.name))
        self.config = default_config()
        self.config["calendar"]["enabled"] = False
        self.config["chime"]["enabled"] = False
        self.config["rules"] = []
        self.config["settings"]["quiet_hours"]["enabled"] = False

    def _log(self, ticks: int, on_tick=None, controller_cls=QuietController) -> str:
        count = {"ticks": 0}

        def fake_sleep(seconds: float) -> None:
            count["ticks"] += 1
            if on_tick:
                on_tick(count["ticks"])
            if count["ticks"] >= ticks:
                self.paths.watcher_stop_path.write_text("stop", encoding="utf-8")

        with patch("blink_light.watcher.time.sleep", side_effect=fake_sleep):
            run_watch_loop(
                self.config,
                self.paths,
                controller_cls=controller_cls,
                snapshot_factory=lambda now: None,
                now_factory=lambda: datetime(2026, 4, 2, 12, 0, tzinfo=timezone.utc),
            )
        return self.paths.log_path.read_text(encoding="utf-8")

    def _applied_lines(self, ticks: int, on_tick=None) -> list[str]:
        return [line for line in self._log(ticks, on_tick).splitlines() if "Applied action" in line]

    def test_an_unchanged_action_is_logged_once_not_every_tick(self) -> None:
        lines = self._applied_lines(ticks=6)

        self.assertEqual(len(lines), 1)
        self.assertIn("default:settings.default_action", lines[0])

    def test_a_change_of_action_is_logged_when_it_happens(self) -> None:
        def on_tick(tick: int) -> None:
            if tick == 2:
                write_json(self.paths.override_path, {"action": {"preset": "busy"}, "reason": "focus"})

        lines = self._applied_lines(ticks=5, on_tick=on_tick)

        self.assertEqual(len(lines), 2)
        self.assertIn("override:focus", lines[1])

    def test_watcher_lines_say_which_process_wrote_them_and_why_it_stopped(self) -> None:
        log = self._log(ticks=2)

        self.assertIn("[watcher] Watcher started (PID", log)
        self.assertIn("[watcher] Watcher stopped (stop-file)", log)

    def test_a_character_outside_the_windows_code_page_is_written_intact(self) -> None:
        # basicConfig wrote in the code page, so this line was dropped outright.
        write_json(self.paths.override_path, {"action": {"preset": "busy"}, "reason": "focus 🎧"})

        lines = self._applied_lines(ticks=2)

        self.assertIn("override:focus 🎧", lines[0])

    def test_a_crash_leaves_its_traceback_in_the_log(self) -> None:
        with self.assertRaises(RuntimeError):
            self._log(ticks=2, controller_cls=CrashingController)

        log = self.paths.log_path.read_text(encoding="utf-8")
        self.assertIn("Watcher exited on an unhandled error", log)
        self.assertIn("invented crash", log)
        self.assertIn("Watcher stopped (error)", log)

    def test_a_fault_that_persists_logs_one_traceback_not_one_per_tick(self) -> None:
        calls = {"count": 0}
        recovered = {"source": "default", "detail": "settings.default_action", "action": {"color": "#000000"}, "timer": {}}

        def flaky(*args, **kwargs):
            calls["count"] += 1
            if calls["count"] <= 4:
                raise ValueError("invented bad tick")
            return recovered

        with patch("blink_light.watcher.determine_action", side_effect=flaky):
            log = self._log(ticks=6)

        self.assertEqual(log.count("Traceback"), 1)
        self.assertEqual(log.count("Tick failed"), 1)
        self.assertIn("Ticks recovered after 4 failed", log)

    def test_a_calendar_outage_is_logged_when_it_starts_and_when_it_ends(self) -> None:
        self.config["calendar"]["enabled"] = True

        class FlakyCalendar:
            def __init__(self, config, poller=None, paths=None):
                self.calls = 0

            def get(self, now):
                self.calls += 1
                # A different message each poll, as Graph's request ids make it.
                error = f"Graph returned 401: request {self.calls}" if self.calls <= 3 else None
                return CalendarSnapshot(provider="graph", fetched_at=now, events=[], error=error)

        with patch("blink_light.watcher.CalendarCache", FlakyCalendar):
            log = self._log(ticks=6)

        self.assertEqual(log.count("Calendar unavailable"), 1)
        self.assertIn("meeting colours paused: Graph returned 401: request 1", log)
        self.assertEqual(log.count("Calendar available again"), 1)


class LogRotationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        self.log = self.root / "blink-light.log"
        self.rotated = self.root / "blink-light.log.1"

    def test_an_oversized_log_is_moved_aside(self) -> None:
        self.log.write_bytes(b"x" * 20)

        self.assertTrue(rotate_log(self.log, max_bytes=10))
        self.assertFalse(self.log.exists())
        self.assertEqual(self.rotated.read_bytes(), b"x" * 20)

    def test_a_log_under_the_limit_is_left_alone(self) -> None:
        self.log.write_bytes(b"x" * 5)

        self.assertFalse(rotate_log(self.log, max_bytes=10))
        self.assertTrue(self.log.exists())
        self.assertFalse(self.rotated.exists())

    def test_only_one_old_log_is_kept(self) -> None:
        self.rotated.write_bytes(b"older")
        self.log.write_bytes(b"x" * 20)

        rotate_log(self.log, max_bytes=10)

        self.assertEqual(self.rotated.read_bytes(), b"x" * 20)

    def test_a_log_another_process_holds_open_is_left_to_grow_rather_than_raising(self) -> None:
        # Windows refuses the rename while the other process has the file open.
        self.log.write_bytes(b"x" * 20)

        with patch("blink_light.state.os.replace", side_effect=PermissionError("in use")):
            self.assertFalse(rotate_log(self.log, max_bytes=10))
        self.assertTrue(self.log.exists())

    def test_a_missing_log_is_not_an_error(self) -> None:
        self.assertFalse(rotate_log(self.log, max_bytes=10))

    def test_the_scheduler_rotates_an_oversized_log_before_opening_it(self) -> None:
        paths = _paths(self.root)
        paths.log_path.parent.mkdir(parents=True, exist_ok=True)
        with paths.log_path.open("wb") as handle:
            handle.truncate(LOG_MAX_BYTES)

        configure_loop_logging(paths)
        release_loop_logging()

        self.assertTrue(paths.log_path.with_name("blink-light.log.1").exists())


if __name__ == "__main__":
    unittest.main()
