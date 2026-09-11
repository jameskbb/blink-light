"""The log should grow with what happened, not with how often something looked.

The watcher ticks every five seconds and used to log its action on every tick,
about 17,000 identical lines a day into the file the scheduler also writes, and
nothing ever trimmed that file. These pin one line per change, and a rotation
at process start that can never take the process down.
"""

from __future__ import annotations

from datetime import datetime, timezone
import logging
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from blink_light.chime import configure_loop_logging, release_loop_logging
from blink_light.defaults import default_config
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
        self.paths = _paths(Path(self.tempdir.name))
        self.config = default_config()
        self.config["calendar"]["enabled"] = False
        self.config["chime"]["enabled"] = False
        self.config["rules"] = []
        self.config["settings"]["quiet_hours"]["enabled"] = False

        # basicConfig does nothing once the root logger has a handler, and the
        # file handler it opens would block the temp directory's cleanup on
        # Windows, so hand the watcher a bare root logger and close up after.
        root = logging.getLogger()
        saved_handlers = root.handlers[:]
        saved_level = root.level
        root.handlers = []

        def restore() -> None:
            for handler in root.handlers:
                handler.close()
            root.handlers = saved_handlers
            root.setLevel(saved_level)

        self.addCleanup(restore)

    def _applied_lines(self, ticks: int, on_tick=None) -> list[str]:
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
                controller_cls=QuietController,
                snapshot_factory=lambda now: None,
                now_factory=lambda: datetime(2026, 4, 2, 12, 0, tzinfo=timezone.utc),
            )
        for handler in logging.getLogger().handlers:
            handler.flush()
        text = self.paths.log_path.read_text(encoding="utf-8")
        return [line for line in text.splitlines() if "Applied action" in line]

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
