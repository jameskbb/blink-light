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
    parse_events,
)
from blink_light.defaults import (
    OUTLOOK_BUSY,
    OUTLOOK_FREE,
    OUTLOOK_OUT_OF_OFFICE,
    OUTLOOK_TENTATIVE,
    OUTLOOK_WORKING_ELSEWHERE,
    default_config,
)
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

    def _event(self, busy_status: int, *, starts_in_minutes: int, minutes: int = 30, entry_id: str = "1"):
        start = self.now + timedelta(minutes=starts_in_minutes)
        return CalendarEvent(
            entry_id=entry_id,
            subject="Meeting",
            start=start,
            end=start + timedelta(minutes=minutes),
            busy_status=busy_status,
            is_all_day=False,
        )

    def _evaluate(self, events):
        return evaluate_calendar_action(self.config, self.paths, self.now, snapshot=self._snapshot(events))

    def test_busy_meeting_turns_red(self) -> None:
        result = self._evaluate([self._event(OUTLOOK_BUSY, starts_in_minutes=-5)])
        self.assertEqual(result["detail"], "active_busy")
        self.assertEqual(result["action"]["color"], "#D50000")

    def test_tentative_meeting_also_turns_red(self) -> None:
        result = self._evaluate([self._event(OUTLOOK_TENTATIVE, starts_in_minutes=-5)])
        self.assertEqual(result["detail"], "active_busy")
        self.assertEqual(result["action"]["color"], "#D50000")

    def test_ten_minute_warning_only_fires_once(self) -> None:
        events = [self._event(OUTLOOK_BUSY, starts_in_minutes=9)]
        result = self._evaluate(events)
        self.assertEqual(result["detail"], "meeting_in_10m")
        self.assertEqual(result["action"]["flash"]["color"], "#FDD835")
        self.assertEqual(result["action"]["flash"]["count"], 2)

        acknowledge_calendar_result(self.paths, result, now=self.now)
        follow_up = self._evaluate(events)
        self.assertEqual(follow_up["detail"], "available")
        self.assertEqual(follow_up["action"]["color"], "#00C853")

    def test_five_minute_warning_uses_a_faster_orange_pattern(self) -> None:
        result = self._evaluate([self._event(OUTLOOK_BUSY, starts_in_minutes=4)])
        self.assertEqual(result["detail"], "meeting_in_5m")
        flash = result["action"]["flash"]
        self.assertEqual(flash["color"], "#FB8C00")
        self.assertEqual(flash["count"], 4)
        ten_minute = self.config["calendar"]["ten_minute_warning"]
        self.assertLess(flash["on_ms"], ten_minute["on_ms"])

    def test_both_warnings_fire_for_one_meeting(self) -> None:
        event = self._event(OUTLOOK_BUSY, starts_in_minutes=9)
        first = self._evaluate([event])
        self.assertEqual(first["detail"], "meeting_in_10m")
        acknowledge_calendar_result(self.paths, first, now=self.now)

        # Same meeting, five minutes later: the second warning is still due.
        self.now += timedelta(minutes=5)
        second = self._evaluate([event])
        self.assertEqual(second["detail"], "meeting_in_5m")

    def test_a_late_start_does_not_fire_the_ten_minute_warning_afterwards(self) -> None:
        """Starting the watcher inside the five-minute window must not backfire the ten."""
        event = self._event(OUTLOOK_BUSY, starts_in_minutes=4)
        first = self._evaluate([event])
        self.assertEqual(first["detail"], "meeting_in_5m")
        acknowledge_calendar_result(self.paths, first, now=self.now)

        follow_up = self._evaluate([event])
        self.assertEqual(follow_up["detail"], "available")

    def test_free_and_out_of_office_events_trigger_nothing(self) -> None:
        for status in (OUTLOOK_FREE, OUTLOOK_OUT_OF_OFFICE, OUTLOOK_WORKING_ELSEWHERE):
            with self.subTest(busy_status=status):
                active = self._evaluate([self._event(status, starts_in_minutes=-5)])
                self.assertEqual(active["detail"], "available")
                self.assertEqual(active["action"]["color"], "#00C853")

                for minutes in (9, 4):
                    upcoming = self._evaluate([self._event(status, starts_in_minutes=minutes)])
                    self.assertEqual(upcoming["detail"], "available")
                    self.assertNotIn("flash", upcoming["action"])
                    self.assertNotIn("_calendar_ack", upcoming)

    def test_a_busy_meeting_is_seen_past_a_free_one(self) -> None:
        """A Free block covering the same slot must not hide the real meeting."""
        events = [
            self._event(OUTLOOK_FREE, starts_in_minutes=-10, entry_id="free"),
            self._event(OUTLOOK_BUSY, starts_in_minutes=-1, entry_id="busy"),
        ]
        result = self._evaluate(events)
        self.assertEqual(result["detail"], "active_busy")
        self.assertEqual(result["calendar"]["event"]["entry_id"], "busy")

    def test_a_free_event_does_not_delay_the_next_busy_warning(self) -> None:
        events = [
            self._event(OUTLOOK_FREE, starts_in_minutes=2, entry_id="free"),
            self._event(OUTLOOK_BUSY, starts_in_minutes=9, entry_id="busy"),
        ]
        result = self._evaluate(events)
        self.assertEqual(result["detail"], "meeting_in_10m")
        self.assertEqual(result["calendar"]["event"]["entry_id"], "busy")


class EventParsingTests(unittest.TestCase):
    """Outlook COM reports local wall-clock times with no offset."""

    def test_naive_outlook_timestamps_become_timezone_aware(self) -> None:
        events = parse_events(
            [
                {
                    "entry_id": "abc",
                    "subject": "Standup",
                    "start": "2026-09-10T08:30:00.0000000",
                    "end": "2026-09-10T08:45:00.0000000",
                    "busy_status": 2,
                    "is_all_day": False,
                }
            ]
        )
        self.assertIsNotNone(events[0].start.tzinfo)
        self.assertIsNotNone(events[0].end.tzinfo)

    def test_parsed_events_compare_against_an_aware_clock(self) -> None:
        """The comparison that used to raise TypeError and kill the watcher."""
        events = parse_events(
            [
                {
                    "entry_id": "abc",
                    "subject": "Standup",
                    "start": "2026-09-10T08:30:00.0000000",
                    "end": "2026-09-10T08:45:00.0000000",
                    "busy_status": 2,
                    "is_all_day": False,
                }
            ]
        )
        # Would raise "can't compare offset-naive and offset-aware datetimes".
        self.assertIsInstance(events[0].start > datetime.now().astimezone(), bool)

    def test_an_offset_bearing_timestamp_is_left_alone(self) -> None:
        events = parse_events(
            [
                {
                    "entry_id": "abc",
                    "subject": "Standup",
                    "start": "2026-09-10T08:30:00+02:00",
                    "end": "2026-09-10T08:45:00+02:00",
                    "busy_status": 2,
                    "is_all_day": False,
                }
            ]
        )
        self.assertEqual(events[0].start.utcoffset(), timedelta(hours=2))


if __name__ == "__main__":
    unittest.main()
