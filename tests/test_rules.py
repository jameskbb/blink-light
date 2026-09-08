from __future__ import annotations

from datetime import datetime, timezone
import unittest

from blink_light.rules import first_matching_rule, is_between_times
from blink_light.system_state import SystemSnapshot
from blink_light.timers import TimerSnapshot


class RuleTests(unittest.TestCase):
    def test_time_between_wraps_midnight(self) -> None:
        current = datetime(2026, 4, 2, 23, 30, tzinfo=timezone.utc)
        self.assertTrue(is_between_times(current, "22:00", "07:00"))
        self.assertFalse(is_between_times(current, "07:30", "21:00"))

    def test_first_matching_rule_returns_highest_priority_rule(self) -> None:
        snapshot = SystemSnapshot(
            idle_seconds=900,
            process_names={"python.exe"},
            battery_percent=35.0,
            charging=False,
        )
        timer_snapshot = TimerSnapshot(
            exists=False,
            name=None,
            timer_active=False,
            paused=False,
            completed=False,
            phase_name=None,
            remaining_seconds=None,
            action=None,
            completion_action=None,
            state=None,
        )
        now = datetime(2026, 4, 2, 12, 0, tzinfo=timezone.utc)
        rules = [
            {"name": "idle-away", "when": {"idle_seconds_gte": 600}, "action": {"preset": "away"}},
            {
                "name": "python-busy",
                "when": {"process_running_any": ["python.exe"]},
                "action": {"preset": "busy"},
            },
        ]
        matched = first_matching_rule(rules, snapshot, timer_snapshot, now)
        self.assertIsNotNone(matched)
        self.assertEqual(matched["name"], "idle-away")


if __name__ == "__main__":
    unittest.main()
