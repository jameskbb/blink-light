"""The device watchdog has to be fed on every tick, including the bad ones.

The blink(1) answers a lapsed watchdog by playing the pattern still loaded in
its memory - whichever notification scene was written last. So a watcher that
stops feeding it does not leave the light alone; it hands the light to the
firmware, which repeats a flash nobody fired and writes nothing anywhere.

Both of the tick's failure paths used to skip the re-arm: a calendar read that
threw `continue`d past it, and a device error during the repaint jumped over
it. That made the two most likely causes of a lapse also the two that hid it.
These pin that the re-arm survives both, and that a lapse it cannot avoid is
said out loud.
"""

from __future__ import annotations

from datetime import datetime, timezone
import logging
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from blink_light.defaults import default_config
from blink_light.device import DeviceError
from blink_light.log_file import close_log
from blink_light.paths import build_paths
from blink_light.watcher import run_watch_loop


class RecordingController:
    """Counts watchdog feeds, and can refuse them without the light going away."""

    instance: "RecordingController | None" = None

    def __init__(self, serial=None):
        self.armed = 0
        self.refuse_arm = False
        self.painted: list[dict] = []
        RecordingController.instance = self

    def apply_action(self, action, scenes, persistent=False) -> None:
        self.painted.append(action)

    def enable_watchdog(self, timeout_millis: int) -> None:
        if self.refuse_arm:
            raise DeviceError("device busy")
        self.armed += 1

    def disable_watchdog(self) -> None:
        pass

    def close(self) -> None:
        pass


class WatchdogRearmTests(unittest.TestCase):
    def setUp(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        self.addCleanup(close_log, logging.getLogger("blink_light.watcher"))
        root = Path(tempdir.name)
        self.paths = build_paths(
            config_path=root / "blink-light.json",
            project_root=root,
            runtime_dir=root / "runtime",
            startup_dir=root / "startup",
        )
        self.config = default_config()
        self.config["calendar"]["enabled"] = False
        self.config["chime"]["enabled"] = False
        self.config["rules"] = []
        self.config["settings"]["quiet_hours"]["enabled"] = False
        self.config["settings"]["default_action"] = {"color": "#00C853"}
        RecordingController.instance = None

    def _run(self, ticks: int, snapshot_factory=None, on_tick=None) -> str:
        count = {"ticks": 0}

        def fake_sleep(seconds: float) -> None:
            count["ticks"] += 1
            if on_tick is not None:
                on_tick(count["ticks"], RecordingController.instance)
            if count["ticks"] >= ticks:
                self.paths.watcher_stop_path.write_text("stop", encoding="utf-8")

        with patch("blink_light.watcher.time.sleep", side_effect=fake_sleep):
            run_watch_loop(
                self.config,
                self.paths,
                controller_cls=RecordingController,
                snapshot_factory=snapshot_factory or (lambda now: None),
                now_factory=lambda: datetime(2026, 4, 2, 12, 0, tzinfo=timezone.utc),
            )
        return self.paths.log_path.read_text(encoding="utf-8")

    def test_every_tick_feeds_the_watchdog_even_with_nothing_to_repaint(self) -> None:
        """The colour only changes on the first tick; the rest must still feed it."""
        self._run(ticks=4)

        controller = RecordingController.instance
        self.assertEqual(controller.armed, 4)
        self.assertEqual(len(controller.painted), 1, "only the first tick changes colour")

    def test_a_tick_whose_calendar_read_throws_still_feeds_the_watchdog(self) -> None:
        """This path used to `continue` straight past the re-arm."""
        calls = {"n": 0}

        def exploding_snapshot(now):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("calendar blew up")
            return None

        log = self._run(ticks=3, snapshot_factory=exploding_snapshot)

        self.assertIn("Tick failed; keeping the previous action", log)
        self.assertEqual(RecordingController.instance.armed, 3, "including the failed tick")

    def test_a_watchdog_it_cannot_feed_is_reported_once_not_every_tick(self) -> None:
        def on_tick(tick, controller):
            if tick == 1:
                controller.refuse_arm = True

        log = self._run(ticks=4, on_tick=on_tick)

        self.assertEqual(log.count("Device watchdog not re-armed"), 1)
        self.assertIn("the light may replay its stored pattern", log)

    def test_it_says_when_the_watchdog_is_armed_again(self) -> None:
        def on_tick(tick, controller):
            if tick == 1:
                controller.refuse_arm = True
            if tick == 3:
                controller.refuse_arm = False

        log = self._run(ticks=5, on_tick=on_tick)

        self.assertEqual(log.count("Device watchdog not re-armed"), 1)
        self.assertEqual(log.count("Device watchdog re-armed after"), 1)


if __name__ == "__main__":
    unittest.main()
