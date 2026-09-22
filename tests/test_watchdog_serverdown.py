"""A lapsed watchdog must play nothing, and must not leave the light dark.

Feeding the watchdog on every tick (see `test_watchdog_rearm.py`) only covers
the lapses this process can see. It cannot cover the ones it causes by being
slow: the calendar poll runs inside the tick with a 20-second HTTP timeout
while the watchdog is armed for eight, so one sluggish Graph call hands the
light to the firmware with nothing raised and nothing logged.

What the firmware then plays is whichever pattern is still in the device's
memory - after a top-of-hour chime, a white pulse - on a loop, for as long as
the tick is stuck. That is the "dozen white flashes nobody fired" this repo has
blamed on the watchdog twice without closing off.

So the fix is not to chase every possible stall. It is to make a lapse
harmless: point the firmware's serverdown sub-pattern at one line this repo
keeps permanently blank, and have the watcher notice the lapse afterwards and
repaint, so the light comes back at once instead of sitting dark until the
colour happens to change.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from blink_light.defaults import default_config
from blink_light.device import SERVERDOWN_PATTERN_LINE, BlinkDeviceController
from blink_light.log_file import close_log
from blink_light.paths import build_paths
from blink_light.watcher import run_watch_loop


class FakeBlink1:
    """Records the pattern lines written and how serverdown was armed."""

    opened: list["FakeBlink1"] = []

    @staticmethod
    def list() -> list[str]:
        return ["12ab34cd"]

    def __init__(self, serial: str):
        self.pattern_lines: list[tuple[int, str, int, int]] = []
        self.tickles: list[tuple] = []
        FakeBlink1.opened.append(self)

    def write_pattern_line(self, fade_millis, color, position, ledn=0) -> None:
        self.pattern_lines.append((fade_millis, color, position, ledn))

    def server_tickle(self, enable, timeout_millis=0, stay_lit=False, start_pos=0, end_pos=16) -> None:
        self.tickles.append((enable, timeout_millis, stay_lit, start_pos, end_pos))

    def fade_to_color(self, fade_millis, color, ledn=0) -> None:
        pass

    def play(self, start_pos, end_pos, count) -> None:
        pass

    def stop(self) -> None:
        pass

    def close(self) -> None:
        pass


class ServerdownPatternTests(unittest.TestCase):
    def setUp(self) -> None:
        FakeBlink1.opened = []
        patcher = patch.object(BlinkDeviceController, "_blink1_class", staticmethod(lambda: FakeBlink1))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.controller = BlinkDeviceController()

    def test_the_watchdog_aims_the_firmware_at_the_reserved_line(self) -> None:
        self.controller.enable_watchdog(8000)

        enable, millis, stay_lit, start_pos, end_pos = FakeBlink1.opened[0].tickles[-1]
        self.assertTrue(enable)
        self.assertEqual(millis, 8000)
        self.assertEqual(
            (start_pos, end_pos),
            (SERVERDOWN_PATTERN_LINE, SERVERDOWN_PATTERN_LINE),
            "the library defaults to lines 0-16, which is where scenes live",
        )

    def test_the_reserved_line_is_blank_before_the_watchdog_is_armed(self) -> None:
        self.controller.enable_watchdog(8000)

        device = FakeBlink1.opened[0]
        blanked = [line for line in device.pattern_lines if line[2] == SERVERDOWN_PATTERN_LINE]
        self.assertEqual(blanked, [(0, "#000000", SERVERDOWN_PATTERN_LINE, 0)])
        self.assertTrue(device.tickles, "arming must follow the blanking, not precede it")

    def test_blanking_the_reserved_line_is_not_repeated_on_every_feed(self) -> None:
        """It is fed every five seconds all day; one write settles it."""
        for _ in range(5):
            self.controller.enable_watchdog(8000)

        device = FakeBlink1.opened[0]
        self.assertEqual(len([line for line in device.pattern_lines if line[2] == SERVERDOWN_PATTERN_LINE]), 1)
        self.assertEqual(len(device.tickles), 5)

    def test_a_light_plugged_back_in_has_its_reserved_line_blanked_again(self) -> None:
        """Pattern memory is reloaded from flash on power-up, blank line and all."""
        self.controller.enable_watchdog(8000)
        self.controller._forget_device()

        self.controller.enable_watchdog(8000)

        self.assertEqual(len(FakeBlink1.opened), 2)
        second = FakeBlink1.opened[1]
        self.assertEqual(
            [line for line in second.pattern_lines if line[2] == SERVERDOWN_PATTERN_LINE],
            [(0, "#000000", SERVERDOWN_PATTERN_LINE, 0)],
        )

    def test_an_on_device_scene_can_never_reach_the_reserved_line(self) -> None:
        full = {"loop": False, "steps": [{"color": "#FFFFFF", "seconds": 0.01}] * SERVERDOWN_PATTERN_LINE}
        over = {"loop": False, "steps": [{"color": "#FFFFFF", "seconds": 0.01}] * (SERVERDOWN_PATTERN_LINE + 1)}

        self.assertTrue(self.controller.can_run_scene_on_device(full))
        self.assertFalse(self.controller.can_run_scene_on_device(over))

    def test_loading_a_scene_leaves_the_reserved_line_black(self) -> None:
        self.controller.play_scene(
            {"loop": False, "repeat": 1, "steps": [{"color": "#FFFFFF", "seconds": 0.0}]},
            persistent=True,
        )

        device = FakeBlink1.opened[0]
        reserved = [line for line in device.pattern_lines if line[2] == SERVERDOWN_PATTERN_LINE]
        self.assertEqual(reserved, [(0, "#000000", SERVERDOWN_PATTERN_LINE, 0)])


class StallingController:
    """A controller that arms without complaint, so only elapsed time tells."""

    instance: "StallingController | None" = None

    def __init__(self, serial=None):
        self.armed = 0
        self.painted: list[dict] = []
        StallingController.instance = self

    def apply_action(self, action, scenes, persistent=False) -> None:
        self.painted.append(action)

    def enable_watchdog(self, timeout_millis: int) -> None:
        self.armed += 1

    def disable_watchdog(self) -> None:
        pass

    def close(self) -> None:
        pass


class SlowTickLapseTests(unittest.TestCase):
    """A tick slower than the watchdog is a lapse, even though nothing raised."""

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
        StallingController.instance = None

    def _run(self, tick_gaps: list[float]) -> str:
        """Run one tick per gap, advancing the clock by that many seconds."""
        clock = {"now": datetime(2026, 4, 2, 12, 0, tzinfo=timezone.utc), "tick": 0}

        def now_factory() -> datetime:
            return clock["now"]

        def fake_sleep(seconds: float) -> None:
            index = clock["tick"]
            clock["tick"] += 1
            clock["now"] += timedelta(seconds=tick_gaps[index])
            if clock["tick"] >= len(tick_gaps):
                self.paths.watcher_stop_path.write_text("stop", encoding="utf-8")

        with patch("blink_light.watcher.time.sleep", side_effect=fake_sleep):
            run_watch_loop(
                self.config,
                self.paths,
                controller_cls=StallingController,
                snapshot_factory=lambda now: None,
                now_factory=now_factory,
            )
        return self.paths.log_path.read_text(encoding="utf-8")

    def test_ticks_inside_the_watchdog_window_are_not_called_a_lapse(self) -> None:
        log = self._run([5.0, 5.0, 5.0])

        self.assertNotIn("Device watchdog lapsed", log)
        self.assertEqual(len(StallingController.instance.painted), 1)

    def test_a_tick_slower_than_the_watchdog_is_reported_as_a_lapse(self) -> None:
        log = self._run([5.0, 20.0, 5.0])

        self.assertEqual(log.count("Device watchdog lapsed"), 1)

    def test_a_lapse_repaints_the_light_instead_of_leaving_it_dark(self) -> None:
        """Serverdown fades to the blank line, so the cached colour is a lie."""
        self._run([5.0, 20.0, 5.0])

        painted = StallingController.instance.painted
        self.assertEqual(len(painted), 2, "the tick after the lapse must repaint")
        self.assertEqual(painted[0], painted[1])


if __name__ == "__main__":
    unittest.main()
