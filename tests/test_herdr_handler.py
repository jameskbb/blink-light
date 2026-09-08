"""Tests for the Herdr event handler.

The handler must tolerate an unconfirmed payload shape, so these pin the range
of shapes it accepts rather than one blessed schema.
"""

from __future__ import annotations

import importlib.util
import json
import os
import tempfile
from pathlib import Path
import unittest

HANDLER_PATH = Path(__file__).resolve().parents[1] / "integrations" / "herdr" / "handler.py"


def _load_handler():
    spec = importlib.util.spec_from_file_location("herdr_handler", HANDLER_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


handler = _load_handler()


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
        self._focused = handler.focused_panes
        handler.notify = lambda event: (self.fired.append(event), True)[1]
        handler.focused_panes = lambda: set()

    def tearDown(self) -> None:
        handler.notify = self._notify
        handler.focused_panes = self._focused
        os.environ.pop("HERDR_PLUGIN_EVENT_JSON", None)
        os.environ.pop("HERDR_PLUGIN_STATE_DIR", None)
        os.environ.pop("HERDR_PLUGIN_EVENT", None)
        self.tempdir.cleanup()

    def _event(self, status: str, pane: str = "w7:p1") -> None:
        os.environ["HERDR_PLUGIN_EVENT_JSON"] = json.dumps(
            {"event": "pane_agent_status_changed", "data": {"pane_id": pane, "agent_status": status}}
        )
        handler.main()

    def _no_cooldown(self) -> None:
        config_dir = Path(self.tempdir.name) / "config"
        config_dir.mkdir(exist_ok=True)
        (config_dir / "config.json").write_text('{"cooldown_seconds": 0}', encoding="utf-8")
        os.environ["HERDR_PLUGIN_CONFIG_DIR"] = str(config_dir)

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
        handler.focused_panes = lambda: {"w7:p1"}
        self._event("working")
        self._event("idle")
        self.assertEqual(self.fired, [])

    def test_the_focus_filter_can_be_disabled(self) -> None:
        config_dir = Path(self.tempdir.name) / "config"
        config_dir.mkdir(exist_ok=True)
        (config_dir / "config.json").write_text('{"notify_focused": true}', encoding="utf-8")
        os.environ["HERDR_PLUGIN_CONFIG_DIR"] = str(config_dir)
        handler.focused_panes = lambda: {"w7:p1"}
        self._event("working")
        self._event("idle")
        self.assertEqual(self.fired, ["agent_done"])

    def test_cooldown_suppresses_a_burst(self) -> None:
        self._event("working", pane="w7:p1")
        self._event("working", pane="w7:p2")
        self._event("idle", pane="w7:p1")
        self._event("idle", pane="w7:p2")
        # Second pane finished inside the default 8s cooldown.
        self.assertEqual(self.fired, ["agent_done"])

    def test_first_ever_event_for_a_pane_still_fires(self) -> None:
        """No history must not mean no notification."""
        self._event("idle")
        self.assertEqual(self.fired, ["agent_done"])


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


if __name__ == "__main__":
    unittest.main()

