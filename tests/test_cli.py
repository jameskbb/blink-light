from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import io
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

import blink_light.cli as cli_module
from blink_light.cli import main
from blink_light.paths import build_paths
from blink_light.system_state import SystemSnapshot
from blink_light.state import read_json, write_json


class FakeController:
    available_serials = ["FAKE1234"]
    applied_actions: list[dict] = []

    def __init__(self, serial: str | None = None):
        self.serial = serial

    @classmethod
    def list_devices(cls) -> list[str]:
        return list(cls.available_serials)

    def status(self):
        selected = self.serial or self.available_serials[0]
        return SimpleNamespace(selected_serial=selected, available_serials=list(self.available_serials), version="999")

    def close(self) -> None:
        return None

    def solid(self, color: str, fade_ms: int = 0, led: int = 0) -> None:
        self.applied_actions.append({"color": color, "fade_ms": fade_ms, "led": led})

    def off(self) -> None:
        self.applied_actions.append({"off": True})

    def flash(self, **kwargs) -> None:
        self.applied_actions.append({"flash": kwargs})

    def pulse(self, **kwargs) -> None:
        self.applied_actions.append({"pulse": kwargs})

    def play_scene(self, scene: dict, persistent: bool) -> None:
        self.applied_actions.append({"scene": scene, "persistent": persistent})

    def apply_action(self, action: dict, scenes: dict, persistent: bool) -> None:
        self.applied_actions.append({"action": action, "persistent": persistent})

    def enable_watchdog(self, timeout_millis: int) -> None:
        self.applied_actions.append({"watchdog": timeout_millis})

    def disable_watchdog(self) -> None:
        self.applied_actions.append({"watchdog": 0})


class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        self.paths = build_paths(
            config_path=root / "blink-light.json",
            project_root=root,
            runtime_dir=root / "runtime",
            startup_dir=root / "startup",
        )
        FakeController.applied_actions = []

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def _run(self, argv, snapshot=None) -> tuple[int, str, str]:
        out = io.StringIO()
        err = io.StringIO()
        snapshot_factory = snapshot or (lambda now=None: SystemSnapshot(None, set(), None, None))
        exit_code = main(
            argv,
            paths=self.paths,
            controller_cls=FakeController,
            snapshot_factory=snapshot_factory,
            now_factory=lambda: datetime(2026, 4, 2, 12, 0, tzinfo=timezone.utc),
            out=out,
            err=err,
        )
        return exit_code, out.getvalue(), err.getvalue()

    def _disable_calendar(self) -> None:
        payload = read_json(self.paths.config_path, {})
        payload.setdefault("calendar", {})["enabled"] = False
        payload["calendar"]["auto_watch_on_launch"] = False
        write_json(self.paths.config_path, payload)

    def test_config_init_and_validate(self) -> None:
        exit_code, output, _ = self._run(["config", "init"])
        self.assertEqual(exit_code, 0)
        self.assertTrue(self.paths.config_path.exists())
        payload = json.loads(output)
        self.assertEqual(payload["config"]["device"]["serial"], "FAKE1234")

        exit_code, _, error = self._run(["config", "validate"])
        self.assertEqual(exit_code, 0)
        self.assertEqual(error, "")

    def test_no_args_prints_welcome(self) -> None:
        exit_code, output, error = self._run([])
        self.assertEqual(exit_code, 0)
        self.assertIn("Connected blink(1) devices: FAKE1234", output)
        self.assertIn("blink-light.bat status", output)
        self.assertEqual(error, "")

    def test_no_args_can_auto_start_watch_when_calendar_enabled(self) -> None:
        self._run(["config", "init"])
        original = cli_module.start_background_watch
        try:
            cli_module.start_background_watch = lambda paths: {"running": True, "pid": 1234, "state": {"source": "calendar"}}
            exit_code, output, error = self._run([])
        finally:
            cli_module.start_background_watch = original
        self.assertEqual(exit_code, 0)
        payload = json.loads(output)
        self.assertTrue(payload["launched_watch"])
        self.assertEqual(payload["watch"]["pid"], 1234)
        self.assertEqual(error, "")

    def test_override_set_and_clear(self) -> None:
        exit_code, _, _ = self._run(["override", "set", "--preset", "busy", "--expires-in", "30m"])
        self.assertEqual(exit_code, 0)
        override = read_json(self.paths.override_path, {})
        self.assertEqual(override["action"], {"preset": "busy"})
        self.assertIsNotNone(override["expires_at"])

        exit_code, _, _ = self._run(["override", "clear"])
        self.assertEqual(exit_code, 0)
        self.assertFalse(self.paths.override_path.exists())

    def test_timer_start_and_status(self) -> None:
        exit_code, _, _ = self._run(["timer", "start", "--minutes", "1", "--name", "tea"])
        self.assertEqual(exit_code, 0)
        timer_state = read_json(self.paths.timer_path, {})
        self.assertEqual(timer_state["name"], "tea")

        exit_code, output, _ = self._run(["timer", "status"])
        self.assertEqual(exit_code, 0)
        payload = json.loads(output)
        self.assertTrue(payload["timer"]["active"])
        self.assertEqual(payload["timer"]["name"], "tea")

    def test_watch_once_uses_highest_priority_rule(self) -> None:
        self._run(["config", "init"])
        # The calendar outranks rules, so it must be off for this test to be
        # about rule precedence rather than whatever Outlook says today.
        self._disable_calendar()

        def snapshot_factory(now=None):
            return SystemSnapshot(idle_seconds=900, process_names=set(), battery_percent=None, charging=None)

        exit_code, output, _ = self._run(["watch", "once"], snapshot=snapshot_factory)
        self.assertEqual(exit_code, 0)
        payload = json.loads(output)
        self.assertEqual(payload["source"], "rule")
        self.assertEqual(payload["detail"], "idle-away")
        self.assertEqual(FakeController.applied_actions[-1]["action"]["color"], "#2962FF")

    def test_chime_now_fires_once_and_then_no_ops(self) -> None:
        self._run(["config", "init"])
        exit_code, output, _ = self._run(["chime", "now"])
        self.assertEqual(exit_code, 0)
        payload = json.loads(output)
        self.assertTrue(payload["fired"])
        self.assertEqual(payload["action"]["pulse"]["color"], "#FFFFFF")
        self.assertEqual(payload["action"]["pulse"]["count"], 1)

        exit_code, output, _ = self._run(["chime", "now"])
        self.assertEqual(exit_code, 0)
        self.assertFalse(json.loads(output)["fired"])

    def test_chime_now_force_ignores_the_already_fired_guard(self) -> None:
        self._run(["config", "init"])
        self._run(["chime", "now"])
        exit_code, output, _ = self._run(["chime", "now", "--force"])
        self.assertEqual(exit_code, 0)
        self.assertTrue(json.loads(output)["fired"])

    def test_notify_run_fires_the_named_event(self) -> None:
        self._run(["config", "init"])
        exit_code, output, _ = self._run(["notify", "run", "agent_done"])
        self.assertEqual(exit_code, 0)
        payload = json.loads(output)
        self.assertTrue(payload["notified"])
        self.assertEqual(payload["action"], {"scene": "agent_done_scene"})
        self.assertFalse(FakeController.applied_actions[-1]["persistent"])

    def test_notify_run_rejects_an_unknown_event(self) -> None:
        self._run(["config", "init"])
        exit_code, _, error = self._run(["notify", "run", "nope"])
        self.assertEqual(exit_code, 1)
        self.assertIn("Unknown notify event", error)

    def test_notify_run_can_stay_quiet_about_unknown_events(self) -> None:
        self._run(["config", "init"])
        exit_code, output, error = self._run(["notify", "run", "nope", "--quiet-missing"])
        self.assertEqual(exit_code, 0)
        self.assertFalse(json.loads(output)["notified"])
        self.assertEqual(error, "")

    def test_notify_list_shows_resolved_actions(self) -> None:
        self._run(["config", "init"])
        exit_code, output, _ = self._run(["notify", "list"])
        self.assertEqual(exit_code, 0)
        events = json.loads(output)["events"]
        self.assertEqual(events["agent_done"], {"scene": "agent_done_scene"})

    def test_chime_status_reports_the_next_slot(self) -> None:
        self._run(["config", "init"])
        exit_code, output, _ = self._run(["chime", "status"])
        self.assertEqual(exit_code, 0)
        payload = json.loads(output)
        self.assertTrue(payload["enabled"])
        self.assertEqual(payload["minute"], 0)
        self.assertTrue(payload["next_slot"].startswith("2026-04-02T13:00:00"))

    def test_startup_enable_and_disable(self) -> None:
        exit_code, output, _ = self._run(["startup", "enable"])
        self.assertEqual(exit_code, 0)
        payload = json.loads(output)
        self.assertTrue(payload["enabled"])
        self.assertTrue(self.paths.startup_script_path.exists())

        exit_code, output, _ = self._run(["startup", "disable"])
        self.assertEqual(exit_code, 0)
        payload = json.loads(output)
        self.assertFalse(payload["enabled"])
        self.assertFalse(self.paths.startup_script_path.exists())


if __name__ == "__main__":
    unittest.main()
