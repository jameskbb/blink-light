from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from blink_light.defaults import default_config
from blink_light.timers import pause_timer, refresh_timer_state, resume_timer, routine_state, save_timer_state


class TimerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.timer_path = Path(self.tempdir.name) / "timer.json"
        self.base = datetime(2026, 4, 2, 12, 0, tzinfo=timezone.utc)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_routine_advances_to_next_phase_and_completion(self) -> None:
        config = default_config()
        state = routine_state("pomodoro", config["routines"]["pomodoro"], now=self.base)
        save_timer_state(self.timer_path, state)

        after_focus = refresh_timer_state(self.timer_path, self.base + timedelta(minutes=26))
        self.assertTrue(after_focus.timer_active)
        self.assertEqual(after_focus.phase_name, "short_break")

        completed = refresh_timer_state(self.timer_path, self.base + timedelta(minutes=31))
        self.assertTrue(completed.completed)
        self.assertEqual(completed.phase_name, "completed")

    def test_pause_and_resume_preserve_remaining_time(self) -> None:
        config = default_config()
        state = routine_state("short_break", config["routines"]["short_break"], now=self.base)
        save_timer_state(self.timer_path, state)

        paused = pause_timer(self.timer_path, self.base + timedelta(minutes=2))
        self.assertTrue(paused.paused)
        self.assertGreater(paused.remaining_seconds, 0)

        resumed = resume_timer(self.timer_path, self.base + timedelta(minutes=3))
        self.assertTrue(resumed.timer_active)
        self.assertEqual(resumed.phase_name, "short_break")


if __name__ == "__main__":
    unittest.main()
