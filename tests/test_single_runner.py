"""One watcher, one chime loop, however many things try to start them.

Both loops guarded themselves by reading a pid file and standing down if it
named a live process, which cannot hold: two starts that land together both read
it before either has written one, so both pass. The cost if they do is two
processes driving one USB device, two supervising the watcher, and `chime stop`
able to signal only whichever one the pid file happens to name - the other left
running with nothing tracking it.

These pin the replacement - a lock held for the loop's whole life, so the check
*is* the claim - and the properties that make it safe to hold for hours: a
loser never waits, the lock dies with its holder, it does not collide with the
shared slot lock, and a runtime dir that cannot be written does not cost the
light its watcher.
"""

from __future__ import annotations

from datetime import datetime
import logging
import os
from pathlib import Path
import tempfile
import unittest

from blink_light.chime import run_chime_loop
from blink_light.defaults import default_config
from blink_light.log_file import close_log
from blink_light.paths import build_paths
from blink_light.slot_lock import single_instance, slot_lock
from blink_light.state import write_json
from blink_light.watcher import run_watch_loop


class ExplodingController:
    """Any use at all is a failure: a loser must not reach the device."""

    def __init__(self, *args, **kwargs):
        raise AssertionError("a stood-down runner must never open the device")


class SingleInstanceLockTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.path = Path(self.tempdir.name) / "runtime" / "watcher.lock"

    def test_a_second_holder_is_refused_rather_than_queued(self) -> None:
        with single_instance(self.path) as first:
            self.assertTrue(first)
            with single_instance(self.path) as second:
                self.assertFalse(second)

    def test_the_lock_is_free_again_once_the_holder_leaves(self) -> None:
        with single_instance(self.path) as first:
            self.assertTrue(first)
        with single_instance(self.path) as second:
            self.assertTrue(second)

    def test_it_does_not_collide_with_the_shared_slot_lock(self) -> None:
        """Separate files on purpose - a byte-range lock would fight otherwise."""
        slot_path = self.path.with_name("slot.lock")
        with single_instance(self.path) as held:
            self.assertTrue(held)
            with slot_lock(slot_path, timeout_seconds=0.1) as claimed:
                self.assertTrue(claimed)

    def test_an_unusable_lock_path_still_lets_the_loop_run(self) -> None:
        """Losing the guard must not be worse than the duplicate it prevents."""
        blocked = Path(self.tempdir.name) / "blocked"
        blocked.write_text("not a directory", encoding="utf-8")
        with single_instance(blocked / "watcher.lock") as acquired:
            self.assertTrue(acquired)


class StandDownTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        # Before the tempdir cleanup, so it runs after it: a watcher that fails
        # part-way leaves the log handler attached, and Windows will not delete
        # a directory holding an open file.
        self.addCleanup(self.tempdir.cleanup)
        self.addCleanup(close_log, logging.getLogger("blink_light.watcher"))
        root = Path(self.tempdir.name)
        self.paths = build_paths(project_root=root, runtime_dir=root / "runtime")
        self.config = default_config()

    def test_a_second_watcher_stands_down_instead_of_running(self) -> None:
        with single_instance(self.paths.watcher_lock_path) as held:
            self.assertTrue(held)
            status = run_watch_loop(
                self.config,
                self.paths,
                controller_cls=ExplodingController,
            )
        self.assertFalse(status["running"])

    def test_a_second_chime_loop_stands_down_instead_of_running(self) -> None:
        with single_instance(self.paths.chime_lock_path) as held:
            self.assertTrue(held)
            result = run_chime_loop(
                self.config,
                self.paths,
                controller_cls=ExplodingController,
            )
        self.assertEqual(result, {"ran": False, "reason": "already-running"})

    def test_a_replacement_starts_even_while_the_dead_watchers_heartbeat_looks_fresh(self) -> None:
        """Stop then start immediately used to refuse itself for 15 seconds.

        `watch_status` calls a watcher running until its heartbeat is 15s old,
        and a killed watcher never writes a final one, so the state it left
        behind described a process that was already gone. Holding the lock is
        the proof it is gone - reaching the device is what proves we got past
        the check.
        """
        reached_device = RuntimeError("reached the device")

        def exploding(*_args, **_kwargs):
            raise reached_device

        write_json(
            self.paths.watcher_state_path,
            {"pid": 999999, "updated_at": datetime.now().astimezone().isoformat()},
        )
        with self.assertRaises(RuntimeError) as caught:
            run_watch_loop(self.config, self.paths, controller_cls=exploding)
        self.assertIs(caught.exception, reached_device)

    def test_standing_down_leaves_the_holders_pid_file_alone(self) -> None:
        """A loser reports; it does not tidy up after the watcher that won.

        The pid is this process's, so it is genuinely alive - a stale one is
        swept by `watch_status` on purpose, and sweeping that is not what this
        is about.
        """
        live_pid = str(os.getpid())
        self.paths.watcher_pid_path.parent.mkdir(parents=True, exist_ok=True)
        self.paths.watcher_pid_path.write_text(live_pid, encoding="utf-8")
        with single_instance(self.paths.watcher_lock_path):
            status = run_watch_loop(self.config, self.paths, controller_cls=ExplodingController)
        self.assertEqual(self.paths.watcher_pid_path.read_text(encoding="utf-8"), live_pid)
        self.assertTrue(status["running"], "it should report the watcher that holds the lock")


if __name__ == "__main__":
    unittest.main()
