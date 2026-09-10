"""Graph provider tests. No network, no msal, no Outlook - pure mapping."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from blink_light.calendar_source import (
    alert_statuses,
    alertable_events,
    evaluate_calendar_action,
    parse_events,
    poll_calendar,
)
from blink_light.config import ConfigError, validate_config
from blink_light.defaults import (
    OUTLOOK_BUSY,
    OUTLOOK_FREE,
    OUTLOOK_OUT_OF_OFFICE,
    OUTLOOK_TENTATIVE,
    OUTLOOK_WORKING_ELSEWHERE,
    default_config,
)
from blink_light.graph_calendar import graph_rows_to_items
from blink_light.paths import build_paths


def graph_row(**overrides):
    row = {
        "id": "AAMk123",
        "subject": "Quarterly planning",
        "start": {"dateTime": "2026-09-09T11:00:00.0000000", "timeZone": "Central Standard Time"},
        "end": {"dateTime": "2026-09-09T12:00:00.0000000", "timeZone": "Central Standard Time"},
        "showAs": "busy",
        "isAllDay": False,
        "isCancelled": False,
    }
    row.update(overrides)
    return row


class GraphMappingTests(unittest.TestCase):
    def test_show_as_maps_onto_outlook_busy_statuses(self) -> None:
        cases = {
            "free": OUTLOOK_FREE,
            "tentative": OUTLOOK_TENTATIVE,
            "busy": OUTLOOK_BUSY,
            "oof": OUTLOOK_OUT_OF_OFFICE,
            "workingElsewhere": OUTLOOK_WORKING_ELSEWHERE,
        }
        for show_as, expected in cases.items():
            with self.subTest(show_as=show_as):
                items = graph_rows_to_items([graph_row(showAs=show_as)])
                self.assertEqual(items[0]["busy_status"], expected)

    def test_unknown_availability_is_treated_as_busy(self) -> None:
        """A missed warning is worse than an extra one."""
        items = graph_rows_to_items([graph_row(showAs="unknown")])
        self.assertEqual(items[0]["busy_status"], OUTLOOK_BUSY)

    def test_cancelled_events_are_dropped(self) -> None:
        self.assertEqual(graph_rows_to_items([graph_row(isCancelled=True)]), [])

    def test_seven_digit_fractional_seconds_parse(self) -> None:
        """Graph pads to 7 digits; fromisoformat accepts at most 6."""
        events = parse_events(graph_rows_to_items([graph_row()]))
        self.assertEqual(events[0].start.hour, 11)
        self.assertIsNotNone(events[0].start.tzinfo)

    def test_utc_rows_keep_the_right_moment_in_time(self) -> None:
        # Shown in local time, like the Outlook provider, but the instant is
        # what the warnings are computed from - so that is what must survive.
        row = graph_row(
            start={"dateTime": "2026-09-09T16:00:00.0000000", "timeZone": "UTC"},
            end={"dateTime": "2026-09-09T17:00:00.0000000", "timeZone": "UTC"},
        )
        events = parse_events(graph_rows_to_items([row]))
        self.assertEqual(events[0].start, datetime(2026, 9, 9, 16, 0, tzinfo=timezone.utc))
        self.assertEqual(events[0].end, datetime(2026, 9, 9, 17, 0, tzinfo=timezone.utc))
        self.assertIsNotNone(events[0].start.tzinfo)

    def test_an_invitation_you_did_not_organise_still_counts(self) -> None:
        """The case that started this: invited, not organised, still Busy."""
        row = graph_row(responseStatus={"response": "notResponded", "time": "0001-01-01T00:00:00Z"})
        events = parse_events(graph_rows_to_items([row]))
        config = default_config()
        self.assertEqual(alertable_events(events, alert_statuses(config)), events)


class GraphProviderWiringTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        self.paths = build_paths(
            config_path=root / "blink-light.json",
            project_root=root,
            runtime_dir=root / "runtime",
            startup_dir=root / "startup",
        )
        self.now = datetime(2026, 9, 9, 10, 50, tzinfo=timezone.utc)
        self.config = default_config()
        self.config["calendar"]["provider"] = "graph"
        self.config["calendar"]["graph"]["client_id"] = "11111111-2222-3333-4444-555555555555"

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_an_unknown_provider_is_reported_not_raised(self) -> None:
        self.config["calendar"]["provider"] = "carrier-pigeon"
        snapshot = poll_calendar(self.config, self.now, self.paths)
        self.assertIn("carrier-pigeon", snapshot.error)
        self.assertEqual(snapshot.events, [])

    def test_a_signed_out_graph_poll_degrades_to_an_error_snapshot(self) -> None:
        """No token means no events - and a watcher that keeps running."""
        snapshot = poll_calendar(self.config, self.now, self.paths)
        self.assertIsNotNone(snapshot.error)
        self.assertEqual(snapshot.events, [])
        # An error snapshot must not resolve to an action at all.
        self.assertIsNone(
            evaluate_calendar_action(self.config, self.paths, self.now, snapshot=snapshot)
        )


class GraphConfigTests(unittest.TestCase):
    def test_a_missing_client_id_is_a_poll_error_not_a_config_error(self) -> None:
        """An unconfigured Graph provider must not break every other command."""
        config = default_config()
        config["calendar"]["provider"] = "graph"
        config["calendar"]["graph"]["client_id"] = ""
        validate_config(config)

        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            paths = build_paths(
                config_path=root / "blink-light.json",
                project_root=root,
                runtime_dir=root / "runtime",
                startup_dir=root / "startup",
            )
            snapshot = poll_calendar(config, datetime.now().astimezone(), paths)
        self.assertIn("client_id", snapshot.error)

    def test_an_unknown_provider_is_rejected(self) -> None:
        config = default_config()
        config["calendar"]["provider"] = "gcal"
        with self.assertRaises(ConfigError):
            validate_config(config)


if __name__ == "__main__":
    unittest.main()
