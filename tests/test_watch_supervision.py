"""A watcher that dies should not stay dead until somebody notices the light.

The watcher only ever started at logon, and nothing checked afterwards. One
that was killed mid-session left the calendar colours off for days: the light
still chimed, so nothing looked broken. These pin that the always-on scheduler
notices and restarts it, that a deliberate `watch stop` is not undone, that a
watcher which will not start is retried on a timer rather than every wake, and
that `watch start` reports a failed launch with a non-zero exit instead of a
cheerful JSON blob.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import tempfile
import unittest

import blink_light.cli as cli_module
from blink_light.cli import main
from blink_light.defaults import default_config
from blink_light.paths import build_paths
from blink_light.startup import enable_startup
from blink_light.watcher import WatcherSupervisor, stop_watch, watch_status


def at(minute: int = 0, second: int = 0) -> datetime:
    return datetime(2026, 4, 2, 12, minute, second, tzinfo=timezone.utc)


class RecordingStarter:
    """Stands in for `start_background_watch`, which launches a real process."""

    def __init__(self, running: bool = True):
        self.running = running
        self.calls = 0

    def __call__(self, paths) -> dict:
        self.calls += 1
        if self.running:
            return {"running": True, "pid": 4242}
        return {"running": False, "pid": None, "error": "Watcher did not stay running."}


class SupervisionTests(unittest.TestCase):
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
        # Supervision follows the logon script: it restarts what was asked for.
        enable_startup(self.paths)

    def _supervisor(self, starter: RecordingStarter, retry_seconds: float = 300.0) -> WatcherSupervisor:
        return WatcherSupervisor(self.config, self.paths, starter=starter, retry_seconds=retry_seconds)

    def test_a_watcher_that_went_missing_is_started_again(self) -> None:
        starter = RecordingStarter()
        result = self._supervisor(starter).check(now=at())

        self.assertEqual(result["action"], "restarted")
        self.assertEqual(starter.calls, 1)

    def test_a_running_watcher_is_left_alone(self) -> None:
        self.paths.watcher_pid_path.parent.mkdir(parents=True, exist_ok=True)
        # Our own pid: a process that is certainly alive.
        import os

        self.paths.watcher_pid_path.write_text(str(os.getpid()), encoding="utf-8")
        starter = RecordingStarter()

        result = self._supervisor(starter).check(now=at())

        self.assertEqual(result["action"], "running")
        self.assertEqual(starter.calls, 0)

    def test_a_pid_file_that_cannot_be_read_does_not_stop_the_supervisor(self) -> None:
        """A watcher exiting deletes this file while the supervisor reads it.

        Windows reports that in-flight read as a sharing violation rather than
        a missing file, and the raise would land in the one place that exists
        to bring the watcher back. A directory stands in for the unreadable
        file.
        """
        self.paths.watcher_pid_path.mkdir(parents=True)
        starter = RecordingStarter()

        result = self._supervisor(starter).check(now=at())

        self.assertEqual(result["action"], "restarted")

    def test_a_watcher_stopped_on_purpose_stays_stopped(self) -> None:
        stop_watch(self.paths)
        starter = RecordingStarter()

        result = self._supervisor(starter).check(now=at())

        self.assertEqual(result["action"], "paused")
        self.assertEqual(starter.calls, 0)

    def test_starting_the_watcher_again_lifts_the_pause(self) -> None:
        stop_watch(self.paths)
        self.assertTrue(watch_status(self.paths)["paused"])

        exit_code, output, _ = _run_cli(["watch", "start"], self.paths, RecordingStarter())

        self.assertEqual(exit_code, 0)
        self.assertFalse(watch_status(self.paths)["paused"])
        self.assertTrue(json.loads(output)["running"])

    def test_supervision_can_be_turned_off_in_config(self) -> None:
        self.config["settings"]["supervise_watcher"] = False
        starter = RecordingStarter()

        result = self._supervisor(starter).check(now=at())

        self.assertEqual(result["action"], "disabled")
        self.assertEqual(starter.calls, 0)

    def test_a_watcher_nobody_asked_to_autostart_is_not_spawned(self) -> None:
        self.paths.startup_script_path.unlink()
        starter = RecordingStarter()

        result = self._supervisor(starter).check(now=at())

        self.assertEqual(result["action"], "disabled")
        self.assertEqual(starter.calls, 0)

    def test_a_start_that_fails_is_retried_on_a_timer_not_every_wake(self) -> None:
        starter = RecordingStarter(running=False)
        supervisor = self._supervisor(starter, retry_seconds=300.0)

        with self.assertLogs("blink_light.watcher", level="ERROR"):
            first = supervisor.check(now=at())
            second = supervisor.check(now=at(1))
            third = supervisor.check(now=at(6))

        self.assertEqual(first["action"], "failed")
        self.assertEqual(second["action"], "waiting")
        self.assertEqual(third["action"], "failed")
        self.assertEqual(starter.calls, 2)

    def test_a_watcher_that_will_not_start_is_logged_once_not_once_a_retry(self) -> None:
        starter = RecordingStarter(running=False)
        supervisor = self._supervisor(starter, retry_seconds=0.0)

        with self.assertLogs("blink_light.watcher", level="ERROR") as captured:
            supervisor.check(now=at())
            supervisor.check(now=at(1))
            supervisor.check(now=at(2))

        self.assertEqual(len(captured.records), 1)

    def test_the_recovery_is_said_out_loud(self) -> None:
        starter = RecordingStarter(running=False)
        supervisor = self._supervisor(starter, retry_seconds=0.0)
        with self.assertLogs("blink_light.watcher", level="ERROR"):
            supervisor.check(now=at())

        starter.running = True
        with self.assertLogs("blink_light.watcher", level="INFO") as captured:
            supervisor.check(now=at(1))

        self.assertIn("restarted", captured.output[0])


class WatchStartExitCodeTests(unittest.TestCase):
    """`watch start` is what the logon script and any scheduler call."""

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

    def test_a_watcher_that_comes_up_exits_zero(self) -> None:
        exit_code, output, _ = _run_cli(["watch", "start"], self.paths, RecordingStarter())

        self.assertEqual(exit_code, 0)
        self.assertTrue(json.loads(output)["running"])

    def test_a_watcher_that_never_comes_up_exits_non_zero(self) -> None:
        exit_code, output, _ = _run_cli(["watch", "start"], self.paths, RecordingStarter(running=False))

        self.assertEqual(exit_code, 1)
        payload = json.loads(output)
        self.assertFalse(payload["running"])
        self.assertIn("error", payload)


def _run_cli(argv, paths, starter) -> tuple[int, str, str]:
    """Run the CLI with the process-launching start replaced.

    The real one detaches a python process; a test that let it would leave
    watchers behind on the machine running the suite.
    """
    out = io.StringIO()
    err = io.StringIO()
    original = cli_module.start_background_watch
    cli_module.start_background_watch = lambda resolved: _start(starter, resolved)
    try:
        exit_code = main(argv, paths=paths, out=out, err=err)
    finally:
        cli_module.start_background_watch = original
    return exit_code, out.getvalue(), err.getvalue()


def _start(starter, paths) -> dict:
    """What the real start does around the launch, minus the launch."""
    from blink_light.state import remove_file

    remove_file(paths.watcher_paused_path)
    return starter(paths)


if __name__ == "__main__":
    unittest.main()
