"""Graph must always be asked for UTC.

The Prefer header used to carry Python's name for the local zone. On Windows
that is a display name - "Central Daylight Time" from March to November - and
Graph only accepts zone IDs, so every summer poll came back as a 400 and the
calendar provider quietly reported no meetings at all. Nothing caught it
because nothing had talked to real Graph until the first sign-in.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import unittest
from unittest import mock

from blink_light import graph_calendar


class GraphTimezoneHeaderTests(unittest.TestCase):
    def _headers_sent(self, now: datetime) -> dict[str, str]:
        captured: dict[str, str] = {}

        def fetcher(url: str, headers: dict[str, str]) -> dict:
            captured.update(headers)
            return {"value": []}

        with mock.patch.object(graph_calendar, "acquire_token_silent", return_value="token"):
            graph_calendar.fetch_graph_events({}, Path("unused"), now, lookahead_minutes=60, fetcher=fetcher)
        return captured

    def test_graph_is_always_asked_for_utc(self) -> None:
        headers = self._headers_sent(datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc))
        self.assertEqual(headers["Prefer"], 'outlook.timezone="UTC"')

    def test_the_header_is_the_same_in_summer_and_winter(self) -> None:
        # The original bug was seasonal, so pin both sides of the change.
        summer = self._headers_sent(datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc))
        winter = self._headers_sent(datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc))
        self.assertEqual(summer["Prefer"], winter["Prefer"])
        self.assertNotIn("Daylight", summer["Prefer"])

    def test_a_utc_time_from_graph_comes_back_with_an_explicit_offset(self) -> None:
        # A naive string would be read as local time downstream and shift every
        # meeting - and every warning - by the UTC offset.
        text = graph_calendar._graph_datetime({"dateTime": "2026-07-01T15:00:00.0000000", "timeZone": "UTC"})
        parsed = datetime.fromisoformat(text)
        self.assertIsNotNone(parsed.tzinfo)
        self.assertEqual(parsed, datetime(2026, 7, 1, 15, 0, tzinfo=timezone.utc))


if __name__ == "__main__":
    unittest.main()
