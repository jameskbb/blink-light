"""A slot belongs to whoever claims it first, and claiming never blocks.

The hourly chime once fired twice in the same second: the scheduled task and
the loop both read an unfired slot before either had written one back, and
their overlapping pattern writes left the light stuck on. These pin that the
claim is taken before the light is touched, that a driver which cannot get the
lock stands down instead of firing on top of the holder, that a claim whose
effect never reached the light is handed back, and - because a lock is its own
way to wedge a schedule - that nothing here waits longer than its deadline.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import tempfile
import threading
import time
import unittest

import blink_light.slot_lock as slot_lock_module
from blink_light.alarms import fire_due_alarms
from blink_light.chime import maybe_fire_chime, read_chime_state
from blink_light.defaults import default_config
from blink_light.device import DeviceError
from blink_light.paths import build_paths
from blink_light.show import maybe_fire_show, read_show_state
from blink_light.slot_lock import slot_lock


def at(hour: int, minute: int = 0, second: int = 0) -> datetime:
    return datetime(2026, 4, 2, hour, minute, second, tzinfo=timezone.utc)


class HeldElsewhere:
    """Holds the claim lock from another thread, standing in for another process.

    Another thread and not this one on purpose: the lock is re-entrant within a
    thread, so a claim taken here would be handed straight back to the code
    under test and nothing would be proved.
    """

    def __init__(self, path: Path):
        self.path = path
        self._acquired = threading.Event()
        self._release = threading.Event()
        self._thread = threading.Thread(target=self._hold, daemon=True)

    def _hold(self) -> None:
        with slot_lock(self.path) as acquired:
            if acquired:
                self._acquired.set()
            self._release.wait(10)

    def __enter__(self) -> "HeldElsewhere":
        self._thread.start()
        if not self._acquired.wait(5):
            raise AssertionError("the holder thread never acquired the lock")
        return self

    def __exit__(self, *_exc) -> None:
        self._release.set()
        self._thread.join(5)


class SlotLockTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.path = Path(self.tempdir.name) / "runtime" / "slot.lock"

    def test_a_second_holder_is_turned_away_rather_than_queued(self) -> None:
        with HeldElsewhere(self.path):
            started = time.monotonic()
            with slot_lock(self.path, timeout_seconds=0.1) as acquired:
                self.assertFalse(acquired)
            self.assertLess(time.monotonic() - started, 2.0)

    def test_the_lock_is_free_again_once_the_holder_leaves_the_block(self) -> None:
        with HeldElsewhere(self.path):
            pass
        with slot_lock(self.path, timeout_seconds=0.1) as acquired:
            self.assertTrue(acquired)

    def test_re_entering_the_lock_on_one_thread_costs_nothing(self) -> None:
        started = time.monotonic()
        with slot_lock(self.path) as outer:
            with slot_lock(self.path) as inner:
                self.assertTrue(outer)
                self.assertTrue(inner)
        self.assertLess(time.monotonic() - started, 2.0)
        # And the re-entry did not leave the real lock held.
        with slot_lock(self.path, timeout_seconds=0.1) as again:
            self.assertTrue(again)

    def test_an_unusable_lock_path_still_lets_the_effect_fire(self) -> None:
        """A runtime dir we cannot write is no reason to skip the hour."""
        blocked = Path(self.tempdir.name) / "blocked"
        blocked.write_text("not a directory", encoding="utf-8")
        with slot_lock(blocked / "slot.lock", timeout_seconds=0.1) as acquired:
            self.assertTrue(acquired)


class ClaimBeforePlayTests(unittest.TestCase):
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
        # A held lock must not cost the suite the full production deadline.
        original = slot_lock_module.LOCK_TIMEOUT_SECONDS
        slot_lock_module.LOCK_TIMEOUT_SECONDS = 0.1
        self.addCleanup(setattr, slot_lock_module, "LOCK_TIMEOUT_SECONDS", original)

    def test_a_chime_that_arrives_mid_pulse_finds_the_hour_already_claimed(self) -> None:
        """The real race: the second driver asks while the first is still playing."""
        second: list[dict] = []
        reached: list[dict] = []
        paths, config = self.paths, self.config

        class ReentrantController:
            """Runs a second driver's attempt from inside the first one's pulse."""

            def __init__(self, serial: str | None = None):
                self.applied: list[dict] = []

            def apply_action(self, action: dict, scenes: dict, persistent: bool) -> None:
                self.applied.append(action)
                thread = threading.Thread(
                    target=lambda: second.append(
                        maybe_fire_chime(config, paths, controller_cls=Watched, now=at(12, 0, 3))
                    )
                )
                thread.start()
                thread.join(5)

            def close(self) -> None:
                pass

        class Watched:
            """The second driver's light: anything reaching it is the old bug."""

            def __init__(self, serial: str | None = None):
                pass

            def apply_action(self, action: dict, scenes: dict, persistent: bool) -> None:
                reached.append(action)

            def close(self) -> None:
                pass

        first = maybe_fire_chime(config, paths, controller_cls=ReentrantController, now=at(12, 0, 2))

        self.assertTrue(first["fired"])
        self.assertEqual(reached, [])
        self.assertFalse(second[0]["fired"])
        self.assertEqual(second[0]["reason"], "already-fired")

    def test_a_driver_that_cannot_get_the_lock_stands_down(self) -> None:
        with HeldElsewhere(self.paths.slot_lock_path):
            result = maybe_fire_chime(self.config, self.paths, controller_cls=Unreachable, now=at(12, 0, 2))

        self.assertFalse(result["fired"])
        self.assertEqual(result["reason"], "claimed-elsewhere")
        # Standing down must not consume the hour: nobody claimed it.
        self.assertEqual(read_chime_state(self.paths), {})

    def test_a_chime_whose_pulse_failed_hands_the_hour_back(self) -> None:
        with self.assertRaises(DeviceError):
            maybe_fire_chime(self.config, self.paths, controller_cls=Absent, now=at(12, 0, 2))

        self.assertEqual(read_chime_state(self.paths), {})
        retry = maybe_fire_chime(self.config, self.paths, controller_cls=Quiet, now=at(12, 1, 2))
        self.assertTrue(retry["fired"])

    def test_a_show_whose_scene_failed_hands_the_day_back(self) -> None:
        self.config["show"]["at"] = "12:00"
        with self.assertRaises(DeviceError):
            maybe_fire_show(self.config, self.paths, controller_cls=Absent, now=at(12, 0, 2))

        self.assertEqual(read_show_state(self.paths), {})
        retry = maybe_fire_show(self.config, self.paths, controller_cls=Quiet, now=at(12, 1, 2))
        self.assertTrue(retry["fired"])

    def test_an_alarm_is_claimed_before_it_plays(self) -> None:
        self.config["alarms"] = [
            {"name": "standup_now", "at": "08:15", "enabled": True, "action": {"color": "#FF0000"}}
        ]
        seen: list[dict] = []
        errors: list[BaseException] = []
        paths, config = self.paths, self.config

        class ReentrantController:
            def __init__(self, serial: str | None = None):
                pass

            def apply_action(self, action: dict, scenes: dict, persistent: bool) -> None:
                thread = threading.Thread(
                    target=lambda: seen.extend(
                        fire_due_alarms(
                            config,
                            paths,
                            controller_cls=Unreachable,
                            now=at(8, 15, 3),
                            on_error=lambda alarm, error: errors.append(error),
                        )
                    )
                )
                thread.start()
                thread.join(5)

            def close(self) -> None:
                pass

        fired = fire_due_alarms(config, paths, controller_cls=ReentrantController, now=at(8, 15, 1))

        self.assertEqual([result["alarm"] for result in fired], ["standup_now"])
        self.assertEqual(seen, [])
        self.assertEqual(errors, [])


class Quiet:
    def __init__(self, serial: str | None = None):
        pass

    def apply_action(self, action: dict, scenes: dict, persistent: bool) -> None:
        pass

    def play_scene(self, scene: dict, persistent: bool) -> None:
        pass

    def close(self) -> None:
        pass


class Absent:
    """A blink(1) that is not plugged in."""

    def __init__(self, serial: str | None = None):
        pass

    def apply_action(self, action: dict, scenes: dict, persistent: bool) -> None:
        raise DeviceError("Configured blink(1) serial '12ab34cd' is not connected.")

    def play_scene(self, scene: dict, persistent: bool) -> None:
        raise DeviceError("Configured blink(1) serial '12ab34cd' is not connected.")

    def close(self) -> None:
        pass


class Unreachable:
    def __init__(self, serial: str | None = None):
        pass

    def apply_action(self, action: dict, scenes: dict, persistent: bool) -> None:
        raise AssertionError("this driver must not reach the light")

    def play_scene(self, scene: dict, persistent: bool) -> None:
        raise AssertionError("this driver must not reach the light")

    def close(self) -> None:
        pass


if __name__ == "__main__":
    unittest.main()
