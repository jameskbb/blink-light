"""Tests for the Herdr event handler.

The handler must tolerate an unconfirmed payload shape, so these pin the range
of shapes it accepts rather than one blessed schema.
"""

from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import time
from pathlib import Path
import unittest
from unittest.mock import patch

HANDLER_PATH = Path(__file__).resolve().parents[1] / "integrations" / "herdr" / "handler.py"


def _load_handler():
    spec = importlib.util.spec_from_file_location("herdr_handler", HANDLER_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


handler = _load_handler()


class LogRotationTests(unittest.TestCase):
    """Herdr writes a line per agent status change; the file must not grow forever."""

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        os.environ["HERDR_PLUGIN_STATE_DIR"] = self.tempdir.name
        self.addCleanup(os.environ.pop, "HERDR_PLUGIN_STATE_DIR", None)
        self.log = Path(self.tempdir.name) / "blink-light-herdr.log"
        self.rotated = Path(self.tempdir.name) / "blink-light-herdr.log.1"

    def test_an_oversized_log_is_moved_aside_before_the_next_line(self) -> None:
        self.log.write_bytes(b"x" * handler.LOG_MAX_BYTES)

        handler.log("fired: agent_done for w7:p1")

        self.assertEqual(self.rotated.stat().st_size, handler.LOG_MAX_BYTES)
        lines = self.log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        self.assertIn("fired: agent_done for w7:p1", lines[0])

    def test_a_refused_rename_still_writes_the_line(self) -> None:
        # Another handler holding the file open makes Windows refuse the move.
        self.log.write_bytes(b"x" * handler.LOG_MAX_BYTES)

        with patch.object(handler.os, "replace", side_effect=PermissionError("in use")):
            handler.log("fired: agent_done for w7:p1")

        self.assertTrue(self.log.read_text(encoding="utf-8").endswith("fired: agent_done for w7:p1\n"))


class StatusExtractionTests(unittest.TestCase):
    def test_flat_status_string(self) -> None:
        self.assertEqual(handler.find_status({"status": "done"}), "done")

    def test_alternate_key_names(self) -> None:
        for key in ("agent_status", "state", "agent_state", "new_status", "to"):
            with self.subTest(key=key):
                self.assertEqual(handler.find_status({key: "idle"}), "idle")

    def test_case_and_whitespace_are_normalised(self) -> None:
        self.assertEqual(handler.find_status({"status": "  Done "}), "done")

    def test_serde_style_enum_wrapper(self) -> None:
        self.assertEqual(handler.find_status({"status": {"Blocked": {}}}), "blocked")

    def test_tagged_enum(self) -> None:
        self.assertEqual(handler.find_status({"status": {"type": "done"}}), "done")

    def test_nested_one_level_down(self) -> None:
        self.assertEqual(handler.find_status({"pane": {"status": "done"}}), "done")

    def test_unrecognised_status_is_ignored(self) -> None:
        self.assertIsNone(handler.find_status({"status": "banana"}))

    def test_a_wrapper_that_is_not_a_state_is_not_mistaken_for_one(self) -> None:
        self.assertIsNone(handler.find_status({"status": {"payload": {}}}))

    def test_missing_status_is_none(self) -> None:
        self.assertIsNone(handler.find_status({"pane_id": "p1"}))

    def test_non_dict_is_none(self) -> None:
        self.assertIsNone(handler.find_status(["done"]))

    def test_working_is_recognised_but_not_notified(self) -> None:
        """Recognising 'working' stops the search falling through to a stray key."""
        self.assertEqual(handler.find_status({"status": "working"}), "working")
        self.assertIsNone(handler.TERMINAL_STATUSES.get("working"))


class EventMappingTests(unittest.TestCase):
    def test_finished_states_map_to_agent_done(self) -> None:
        self.assertEqual(handler.TERMINAL_STATUSES["done"], "agent_done")
        self.assertEqual(handler.TERMINAL_STATUSES["idle"], "agent_done")

    def test_blocked_maps_to_its_own_event(self) -> None:
        self.assertEqual(handler.TERMINAL_STATUSES["blocked"], "agent_blocked")

    def test_only_notify_worthy_states_are_mapped(self) -> None:
        self.assertEqual(set(handler.TERMINAL_STATUSES), {"done", "idle", "blocked"})


class RealPayloadTests(unittest.TestCase):
    """The payload shape captured from a real Herdr event."""

    REAL = {
        "event": "pane_agent_status_changed",
        "data": {
            "type": "pane_agent_status_changed",
            "pane_id": "w7:p1",
            "workspace_id": "w7",
            "agent_status": "idle",
            "agent": "claude",
        },
    }

    def test_status_is_found_under_data(self) -> None:
        self.assertEqual(handler.find_status(self.REAL), "idle")

    def test_pane_is_found_under_data(self) -> None:
        self.assertEqual(handler.find_pane(self.REAL), "w7:p1")


class TransitionTests(unittest.TestCase):
    """The fix for over-firing: only a working -> terminal change is a finish."""

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        os.environ["HERDR_PLUGIN_STATE_DIR"] = self.tempdir.name
        os.environ["HERDR_PLUGIN_EVENT"] = "pane.agent_status_changed"
        os.environ.pop("HERDR_PLUGIN_CONFIG_DIR", None)
        self.fired: list[str] = []
        self._notify = handler.notify
        self._snapshot = handler.agent_snapshot
        handler.notify = lambda event: (self.fired.append(event), True)[1]
        # One seam for everything the handler asks Herdr: focus and the agents
        # still waiting behind a capped burst come from the same query.
        handler.agent_snapshot = lambda: {}

    def tearDown(self) -> None:
        handler.notify = self._notify
        handler.agent_snapshot = self._snapshot
        os.environ.pop("HERDR_PLUGIN_EVENT_JSON", None)
        os.environ.pop("HERDR_PLUGIN_STATE_DIR", None)
        os.environ.pop("HERDR_PLUGIN_EVENT", None)
        self.tempdir.cleanup()

    def _event(self, status: str, pane: str = "w7:p1") -> None:
        os.environ["HERDR_PLUGIN_EVENT_JSON"] = json.dumps(
            {"event": "pane_agent_status_changed", "data": {"pane_id": pane, "agent_status": status}}
        )
        handler.main()

    def _settings(self, **values) -> None:
        config_dir = Path(self.tempdir.name) / "config"
        config_dir.mkdir(exist_ok=True)
        (config_dir / "config.json").write_text(json.dumps(values), encoding="utf-8")
        os.environ["HERDR_PLUGIN_CONFIG_DIR"] = str(config_dir)

    def _no_cooldown(self) -> None:
        """Isolate the transition rules from the rate limits."""
        self._settings(cooldown_seconds=0, burst_max_flashes=0)

    def test_working_then_idle_fires_once(self) -> None:
        self._event("working")
        self._event("idle")
        self.assertEqual(self.fired, ["agent_done"])

    def test_idle_then_done_does_not_double_fire(self) -> None:
        """The actual bug: one turn emitting both idle and done flashed twice."""
        self._event("working")
        self._event("idle")
        self._event("done")
        self.assertEqual(self.fired, ["agent_done"])

    def test_a_new_turn_fires_again(self) -> None:
        self._no_cooldown()
        self._event("working")
        self._event("idle")
        self._event("working")
        self._event("done")
        self.assertEqual(self.fired, ["agent_done", "agent_done"])

    def test_repeated_idle_is_ignored(self) -> None:
        self._event("working")
        self._event("idle")
        self._event("idle")
        self._event("idle")
        self.assertEqual(self.fired, ["agent_done"])

    def test_panes_are_tracked_independently(self) -> None:
        self._no_cooldown()
        self._event("working", pane="w7:p1")
        self._event("working", pane="w7:p2")
        self._event("idle", pane="w7:p1")
        self._event("idle", pane="w7:p2")
        self.assertEqual(self.fired, ["agent_done", "agent_done"])

    def test_blocked_after_working_fires_its_own_event(self) -> None:
        self._event("working")
        self._event("blocked")
        self.assertEqual(self.fired, ["agent_blocked"])

    def test_a_focused_pane_is_skipped(self) -> None:
        handler.agent_snapshot = lambda: {"w7:p1": {"focused": True, "agent_status": "idle"}}
        self._event("working")
        self._event("idle")
        self.assertEqual(self.fired, [])

    def test_the_focus_filter_can_be_disabled(self) -> None:
        self._settings(notify_focused=True)
        handler.agent_snapshot = lambda: {"w7:p1": {"focused": True, "agent_status": "idle"}}
        self._event("working")
        self._event("idle")
        self.assertEqual(self.fired, ["agent_done"])

    def test_a_second_pane_is_not_muted_by_the_first_ones_cooldown(self) -> None:
        """The cooldown is per-pane: a global one let the busiest agent mute the rest."""
        self._event("working", pane="w7:p1")
        self._event("working", pane="w7:p2")
        self._event("idle", pane="w7:p1")
        self._event("idle", pane="w7:p2")
        self.assertEqual(self.fired, ["agent_done", "agent_done"])

    def test_the_same_pane_is_still_held_off_by_its_own_cooldown(self) -> None:
        self._event("working")
        self._event("idle")
        self._event("working")
        self._event("done")
        self.assertEqual(self.fired, ["agent_done"])

    def test_first_ever_event_for_a_pane_still_fires(self) -> None:
        """No history must not mean no notification."""
        self._event("idle")
        self.assertEqual(self.fired, ["agent_done"])


class BurstCapTests(TransitionTests):
    """Ten agents landing together must not become ten identical flashes."""

    def _finish(self, pane: str) -> None:
        self._event("working", pane=pane)
        self._event("done", pane=pane)

    def test_a_herd_of_finishes_is_capped_at_three_flashes(self) -> None:
        for index in range(10):
            self._finish(f"w7:p{index}")
        self.assertEqual(len(self.fired), 3)

    def test_the_cap_is_configurable(self) -> None:
        self._settings(burst_max_flashes=5)
        for index in range(10):
            self._finish(f"w7:p{index}")
        self.assertEqual(len(self.fired), 5)

    def test_a_finish_after_the_window_starts_a_fresh_burst(self) -> None:
        self._settings(burst_window_seconds=0.05, cooldown_seconds=0)
        for index in range(4):
            self._finish(f"w7:p{index}")
        self.assertEqual(len(self.fired), 3)

        time.sleep(0.06)
        self._finish("w7:p9")
        self.assertEqual(len(self.fired), 4)

    def test_a_blocked_agent_is_not_muted_by_a_herd_of_finishes(self) -> None:
        """Blocked is the one alert actually asking you for something."""
        for index in range(10):
            self._finish(f"w7:p{index}")
        self._event("working", pane="w7:p20")
        self._event("blocked", pane="w7:p20")
        self.assertEqual(self.fired[-1], "agent_blocked")

    def test_the_last_allowed_flash_reports_the_agents_still_waiting(self) -> None:
        # Herdr already sees p8 and p9 finished; their events have not arrived.
        handler.agent_snapshot = lambda: {
            f"w7:p{index}": {"focused": False, "agent_status": "done"} for index in range(10)
        }
        for index in range(3):
            self._finish(f"w7:p{index}")
        self.assertEqual(self.fired, ["agent_done", "agent_done", "agent_done_more"])

    def test_the_last_allowed_flash_stays_ordinary_when_nothing_is_waiting(self) -> None:
        handler.agent_snapshot = lambda: {
            f"w7:p{index}": {"focused": False, "agent_status": "done"} for index in range(3)
        }
        for index in range(3):
            self._finish(f"w7:p{index}")
        self.assertEqual(self.fired, ["agent_done"] * 3)

    def test_a_suppressed_finish_counts_as_waiting(self) -> None:
        handler.agent_snapshot = lambda: {
            f"w7:p{index}": {"focused": False, "agent_status": "done"} for index in range(5)
        }
        # Four finishes arrive; the fourth is capped and left unannounced, so a
        # fresh burst's third flash should report it as still waiting.
        for index in range(4):
            self._finish(f"w7:p{index}")
        self.assertEqual(self.fired[-1], "agent_done_more")

    def test_an_announced_agent_is_not_counted_as_waiting(self) -> None:
        handler.agent_snapshot = lambda: {
            f"w7:p{index}": {"focused": False, "agent_status": "idle"} for index in range(3)
        }
        self._settings(burst_window_seconds=0.05, cooldown_seconds=0)
        self._finish("w7:p0")
        time.sleep(0.06)
        self._finish("w7:p1")
        time.sleep(0.06)
        self._finish("w7:p2")
        # Each finished in its own burst and each was announced, so none of them
        # is outstanding and no flash should claim there is more behind it.
        self.assertEqual(self.fired, ["agent_done"] * 3)


class OverflowSceneTests(unittest.TestCase):
    """The overflow flash has to be tellable apart from an ordinary finish."""

    def setUp(self) -> None:
        from blink_light.defaults import default_config, scene_duration_seconds

        config = default_config()
        self.scenes = config["scenes"]
        self.notify = config["notify"]
        self.duration = scene_duration_seconds

    def test_the_overflow_event_is_configured_like_any_other(self) -> None:
        self.assertEqual(self.notify[handler.OVERFLOW_EVENT], {"scene": "agent_done_more_scene"})

    def test_it_runs_a_quarter_longer_than_an_ordinary_finish(self) -> None:
        ordinary = self.duration(self.scenes["agent_done_scene"])
        overflow = self.duration(self.scenes["agent_done_more_scene"])
        self.assertAlmostEqual(overflow / ordinary, 1.25, places=2)

    def test_it_climbs_from_dim_to_bright_instead_of_repeating_a_breath(self) -> None:
        steps = self.scenes["agent_done_more_scene"]["steps"]
        brightness = [sum(int(s["color"].lstrip("#")[i : i + 2], 16) for i in (0, 2, 4)) for s in steps]
        rise = brightness[:-1]
        self.assertEqual(rise, sorted(rise), "the rise should be monotonic")
        self.assertGreater(rise[-1], rise[0], "it should actually get brighter")
        self.assertEqual(brightness[-1], 0, "and end back in the dark")

    def test_it_is_short_enough_to_run_on_the_device(self) -> None:
        scene = self.scenes["agent_done_more_scene"]
        self.assertFalse(scene["loop"])
        self.assertLessEqual(len(scene["steps"]), 32)


class HandlerWiringTests(unittest.TestCase):
    def test_launcher_path_resolves_into_the_repo(self) -> None:
        self.assertTrue(handler.LAUNCHER.exists(), f"{handler.LAUNCHER} should exist")
        self.assertEqual(handler.LAUNCHER.name, "blink-light.bat")

    def test_missing_payload_exits_quietly(self) -> None:
        import os

        saved = os.environ.pop("HERDR_PLUGIN_EVENT_JSON", None)
        try:
            self.assertEqual(handler.main(), 0)
        finally:
            if saved is not None:
                os.environ["HERDR_PLUGIN_EVENT_JSON"] = saved

    def test_malformed_payload_exits_quietly(self) -> None:
        import os

        os.environ["HERDR_PLUGIN_EVENT_JSON"] = "{not json"
        try:
            self.assertEqual(handler.main(), 0)
        finally:
            del os.environ["HERDR_PLUGIN_EVENT_JSON"]


class NotifyPlatformTests(unittest.TestCase):
    """How `notify` reaches a light that is always on the Windows side.

    Two paths, and which one is taken matters for latency rather than
    correctness: the launcher re-hashes requirements.txt with PowerShell on
    every call, which is about a second spent on every agent turn. The venv
    interpreter is used when it is there, the launcher when it is not.
    """

    MISSING_VENV = Path("/nonexistent/.venv/Scripts/python.exe")

    def _present_venv(self) -> Path:
        """Any real file will do - only `.exists()` is consulted."""
        return HANDLER_PATH

    def test_windows_runs_the_venv_interpreter_directly(self) -> None:
        venv = self._present_venv()
        with patch.object(handler.sys, "platform", "win32"), patch.object(
            handler, "VENV_PYTHON", venv
        ), patch.object(handler.subprocess, "run") as run:
            run.return_value.returncode = 0
            handler.notify("agent_done")

        self.assertEqual(
            run.call_args.args[0],
            [str(venv), "-m", "blink_light", "notify", "run", "agent_done", "--quiet-missing"],
        )
        self.assertEqual(run.call_args.kwargs["env"]["PYTHONPATH"], str(handler.REPO_ROOT))

    def test_linux_runs_the_same_interpreter_with_a_translated_pythonpath(self) -> None:
        """Interop starts the Windows binary; only what Windows Python reads is translated."""
        venv = self._present_venv()
        with patch.object(handler.sys, "platform", "linux"), patch.object(
            handler, "VENV_PYTHON", venv
        ), patch.object(handler, "_windows_path", return_value="C:\\blink-light"), patch.object(
            handler.subprocess, "run"
        ) as run:
            run.return_value.returncode = 0
            handler.notify("agent_blocked")

        self.assertEqual(
            run.call_args.args[0],
            [str(venv), "-m", "blink_light", "notify", "run", "agent_blocked", "--quiet-missing"],
        )
        self.assertEqual(run.call_args.kwargs["env"]["PYTHONPATH"], "C:\\blink-light")

    def test_without_a_venv_linux_falls_back_to_the_launcher_through_cmd(self) -> None:
        with patch.object(handler.sys, "platform", "linux"), patch.object(
            handler, "VENV_PYTHON", self.MISSING_VENV
        ), patch.object(
            handler, "_windows_path", return_value="C:\\blink-light\\blink-light.bat"
        ), patch.object(handler.subprocess, "run") as run:
            run.return_value.returncode = 0
            handler.notify("agent_blocked")

        self.assertEqual(
            run.call_args.args[0],
            [
                "cmd.exe",
                "/c",
                "C:\\blink-light\\blink-light.bat",
                "notify",
                "run",
                "agent_blocked",
                "--quiet-missing",
            ],
        )

    def test_without_a_venv_windows_falls_back_to_the_launcher(self) -> None:
        with patch.object(handler.sys, "platform", "win32"), patch.object(
            handler, "VENV_PYTHON", self.MISSING_VENV
        ), patch.object(handler.subprocess, "run") as run:
            run.return_value.returncode = 0
            handler.notify("agent_done")

        self.assertEqual(
            run.call_args.args[0],
            ["cmd.exe", "/c", str(handler.LAUNCHER), "notify", "run", "agent_done", "--quiet-missing"],
        )

    def test_linux_gives_up_quietly_when_wslpath_cannot_resolve_the_path(self) -> None:
        with patch.object(handler.sys, "platform", "linux"), patch.object(
            handler, "VENV_PYTHON", self.MISSING_VENV
        ), patch.object(handler, "_windows_path", return_value=None), patch.object(
            handler.subprocess, "run"
        ) as run:
            fired = handler.notify("agent_done")

        self.assertFalse(fired)
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()

