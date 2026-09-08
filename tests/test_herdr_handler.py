"""Tests for the Herdr event handler.

The handler must tolerate an unconfirmed payload shape, so these pin the range
of shapes it accepts rather than one blessed schema.
"""

from __future__ import annotations

import importlib.util
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
        self.assertIsNone(handler.EVENT_FOR_STATUS.get("working"))


class EventMappingTests(unittest.TestCase):
    def test_finished_states_map_to_agent_done(self) -> None:
        self.assertEqual(handler.EVENT_FOR_STATUS["done"], "agent_done")
        self.assertEqual(handler.EVENT_FOR_STATUS["idle"], "agent_done")

    def test_blocked_maps_to_its_own_event(self) -> None:
        self.assertEqual(handler.EVENT_FOR_STATUS["blocked"], "agent_blocked")

    def test_only_notify_worthy_states_are_mapped(self) -> None:
        self.assertEqual(set(handler.EVENT_FOR_STATUS), {"done", "idle", "blocked"})


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
