from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from blink_light.calendar_source import (
    CalendarEvent,
    CalendarSnapshot,
    acknowledge_calendar_result,
    evaluate_calendar_action,
)
from blink_light.defaults import default_config
from blink_light.paths import build_paths


class CalendarTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        self.paths = build_paths(
            config_path=root / "blink-light.json",
            project_root=root,
            runtime_dir=root / "runtime",
            startup_dir=root / "startup",
        )
        self.now = datetime(2026, 4, 2, 12, 0, tzinfo=timezone.utc)
        self.config = default_config(serial="12ab34cd")

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def _snapshot(self, events):
        return CalendarSnapshot(provider="outlook", fetched_at=self.now, events=events, error=None)

    def test_busy_meeting_turns_red(self) -> None:
        event = CalendarEvent(
            entry_id="1",
            subject="Booked meeting",
            start=self.now - timedelta(minutes=5),
            end=self.now + timedelta(minutes=25),
            busy_status=2,
            is_all_day=False,
        )
        result = evaluate_calendar_action(self.config, self.paths, self.now, snapshot=self._snapshot([event]))
        self.assertEqual(result["detail"], "active_busy")
        self.assertEqual(result["action"]["color"], "#D50000")

    def test_free_meeting_turns_purple(self) -> None:
        event = CalendarEvent(
            entry_id="2",
            subject="Free slot",
            start=self.now - timedelta(minutes=1),
            end=self.now + timedelta(minutes=10),
            busy_status=0,
            is_all_day=False,
        )
        result = evaluate_calendar_action(self.config, self.paths, self.now, snapshot=self._snapshot([event]))
        self.assertEqual(result["detail"], "active_free")
        self.assertEqual(result["action"]["color"], "#8E24AA")

    def test_ten_minute_warning_only_fires_once(self) -> None:
        event = CalendarEvent(
            entry_id="3",
            subject="Soon meeting",
            start=self.now + timedelta(minutes=9),
            end=self.now + timedelta(minutes=39),
            busy_status=2,
            is_all_day=False,
        )
        snapshot = self._snapshot([event])
        result = evaluate_calendar_action(self.config, self.paths, self.now, snapshot=snapshot)
        self.assertEqual(result["detail"], "meeting_in_10m")
        self.assertEqual(result["action"]["flash"]["color"], "#FDD835")

        acknowledge_calendar_result(self.paths, result, now=self.now)
        follow_up = evaluate_calendar_action(self.config, self.paths, self.now, snapshot=snapshot)
        self.assertEqual(follow_up["detail"], "available")
        self.assertEqual(follow_up["action"]["color"], "#00C853")

    def test_two_minute_warning_uses_orange(self) -> None:
        event = CalendarEvent(
            entry_id="4",
            subject="Now-ish meeting",
            start=self.now + timedelta(minutes=1),
            end=self.now + timedelta(minutes=31),
            busy_status=2,
            is_all_day=False,
        )
        result = evaluate_calendar_action(self.config, self.paths, self.now, snapshot=self._snapshot([event]))
        self.assertEqual(result["detail"], "meeting_in_2m")
        self.assertEqual(result["action"]["flash"]["color"], "#FB8C00")


if __name__ == "__main__":
    unittest.main()
