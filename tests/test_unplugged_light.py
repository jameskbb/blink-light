"""Undocking takes the light with it; the watcher has to outlive that.

The watcher holds one controller for hours. Pulling the light out left that
controller holding a dead handle whose errors were not DeviceError, the watcher
died on the first one, and plugging the light back in changed nothing until
someone restarted it. These pin that the controller reports the loss as a
missing device and reconnects, and that the watcher notes it once and repaints
on return.
"""

from __future__ import annotations

from datetime import datetime, timezone
import logging
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from blink1.blink1 import Blink1ConnectionFailed

from blink_light.defaults import default_config
from blink_light.device import BlinkDeviceController, DeviceError
from blink_light.log_file import close_log
from blink_light.paths import build_paths
from blink_light.watcher import run_watch_loop


class FakeDesk:
    """One USB port with a blink(1) that can be pulled out and put back."""

    def __init__(self) -> None:
        self.plugged = True
        self.opened = 0
        self.colors: list[str] = []

    def blink1_class(self):
        desk = self

        class FakeBlink1:
            @staticmethod
            def list() -> list[str]:
                return ["12ab34cd"] if desk.plugged else []

            def __init__(self, serial: str):
                if not desk.plugged:
                    raise Blink1ConnectionFailed("could not open")
                desk.opened += 1

            def _write(self) -> None:
                # What the real library raises on a handle whose light has gone.
                if not desk.plugged:
                    raise Blink1ConnectionFailed("write returned -1 instead of 9")

            def fade_to_color(self, fade_milliseconds, color, ledn=0) -> None:
                self._write()
                desk.colors.append(color)

            def server_tickle(self, enable, timeout_millis=0, stay_lit=False) -> None:
                self._write()

            def stop(self) -> None:
                self._write()

            def off(self) -> None:
                self.fade_to_color(0, "#000000")

            def close(self) -> None:
                pass

        return FakeBlink1


class UnpluggedControllerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.desk = FakeDesk()
        patcher = patch.object(BlinkDeviceController, "_blink1_class", staticmethod(self.desk.blink1_class))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.controller = BlinkDeviceController()

    def test_a_light_pulled_out_mid_use_is_reported_as_a_missing_device(self) -> None:
        self.controller.solid("#FF0000")
        self.desk.plugged = False

        with self.assertRaises(DeviceError):
            self.controller.solid("#00FF00")

    def test_a_light_plugged_back_in_is_found_again(self) -> None:
        self.controller.solid("#FF0000")
        self.desk.plugged = False
        with self.assertRaises(DeviceError):
            self.controller.enable_watchdog(8000)

        self.desk.plugged = True
        self.controller.solid("#0000FF")

        self.assertEqual(self.desk.opened, 2)
        self.assertEqual(self.desk.colors[-1], "#0000FF")

    def test_closing_the_controller_of_a_light_that_has_gone_does_not_raise(self) -> None:
        self.controller.solid("#FF0000")
        self.desk.plugged = False

        self.controller.close()


class UnpluggedWatcherTests(unittest.TestCase):
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
        self.desk = FakeDesk()
        patcher = patch.object(BlinkDeviceController, "_blink1_class", staticmethod(self.desk.blink1_class))
        patcher.start()
        self.addCleanup(patcher.stop)

    def _run(self, ticks: int, on_tick) -> str:
        count = {"ticks": 0}

        def fake_sleep(seconds: float) -> None:
            count["ticks"] += 1
            on_tick(count["ticks"])
            if count["ticks"] >= ticks:
                self.paths.watcher_stop_path.write_text("stop", encoding="utf-8")

        with patch("blink_light.watcher.time.sleep", side_effect=fake_sleep):
            run_watch_loop(
                self.config,
                self.paths,
                snapshot_factory=lambda now: None,
                now_factory=lambda: datetime(2026, 4, 2, 12, 0, tzinfo=timezone.utc),
            )
        return self.paths.log_path.read_text(encoding="utf-8")

    def test_the_watcher_outlives_an_unplugged_light_and_repaints_it_on_return(self) -> None:
        def on_tick(tick: int) -> None:
            if tick == 1:
                self.desk.plugged = False
            if tick == 3:
                self.desk.plugged = True

        log = self._run(ticks=5, on_tick=on_tick)

        self.assertIn("Watcher stopped (stop-file)", log)
        self.assertEqual(log.count("Light not connected"), 1)
        self.assertEqual(log.count("Light connected again"), 1)
        self.assertEqual(log.count("Applied action"), 2)
        self.assertEqual(self.desk.opened, 2)
        self.assertNotIn("Traceback", log)

    def test_stopping_while_the_light_is_unplugged_leaves_no_traceback(self) -> None:
        def on_tick(tick: int) -> None:
            if tick == 1:
                self.desk.plugged = False

        log = self._run(ticks=3, on_tick=on_tick)

        self.assertIn("Watcher stopped (stop-file)", log)
        self.assertNotIn("Traceback", log)


if __name__ == "__main__":
    unittest.main()
