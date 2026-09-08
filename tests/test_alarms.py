from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from blink_light.alarms import (
    alarm_days,
    alarm_status,
    fire_alarm,
    fire_due_alarms,
    find_alarm,
    parse_at,
    read_state,
    seconds_until_next_alarm,
    should_fire,
)
from blink_light.config import ConfigError, merge_config, validate_config
from blink_light.defaults import default_config, scene_duration_seconds
from blink_light.paths import build_paths


class FakeController:
    def __init__(self, serial: str | None = None):
        self.serial = serial
        self.applied: list[dict] = []
        self.closed = False

    def apply_action(self, action: dict, scenes: dict, persistent: bool) -> None:
        self.applied.append({"action": action, "persistent": persistent})

    def close(self) -> None:
        self.closed = True


# 2026-04-02 is a Thursday; 2026-04-04 is a Saturday.
def at(hour: int, minute: int = 0, second: int = 0, day: int = 2) -> datetime:
    return datetime(2026, 4, day, hour, minute, second, tzinfo=timezone.utc)


class StandupAlarmTests(unittest.TestCase):
    """The two alarms this was built for."""

    def setUp(self) -> None:
        self.config = default_config()
        self.config["settings"]["quiet_hours"]["enabled"] = False

    def test_both_standup_alarms_exist(self) -> None:
        names = [alarm["name"] for alarm in self.config["alarms"]]
        self.assertEqual(names, ["standup_warning", "standup_now"])

    def test_they_are_at_0813_and_0815(self) -> None:
        self.assertEqual(find_alarm(self.config, "standup_warning")["at"], "08:13")
        self.assertEqual(find_alarm(self.config, "standup_now")["at"], "08:15")

    def test_both_are_red(self) -> None:
        scenes = self.config["scenes"]
        for name in ("standup_warning_scene", "standup_now_scene"):
            with self.subTest(scene=name):
                self.assertEqual(scenes[name]["steps"][0]["color"], "#FF0000")

    def test_the_two_alerts_differ_in_urgency(self) -> None:
        scenes = self.config["scenes"]
        warning = scene_duration_seconds(scenes["standup_warning_scene"])
        now = scene_duration_seconds(scenes["standup_now_scene"])
        self.assertGreater(now, warning)

    def test_neither_alert_loops(self) -> None:
        for name in ("standup_warning_scene", "standup_now_scene"):
            with self.subTest(scene=name):
                self.assertFalse(self.config["scenes"][name]["loop"])

    def test_unknown_alarm_lists_what_exists(self) -> None:
        with self.assertRaises(ValueError) as caught:
            find_alarm(self.config, "nope")
        self.assertIn("standup_now", str(caught.exception))


class AlarmFiringTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        self.paths = build_paths(
            config_path=root / "blink-light.json",
            project_root=root,
            runtime_dir=root / "runtime",
            startup_dir=root / "startup",
        )
        self.config = default_config()
        self.config["settings"]["quiet_hours"]["enabled"] = False

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_only_the_due_alarm_fires(self) -> None:
        results = fire_due_alarms(self.config, self.paths, controller_cls=FakeController, now=at(8, 13, 2))
        self.assertEqual([result["alarm"] for result in results], ["standup_warning"])

    def test_the_second_alarm_fires_two_minutes_later(self) -> None:
        results = fire_due_alarms(self.config, self.paths, controller_cls=FakeController, now=at(8, 15, 1))
        self.assertEqual([result["alarm"] for result in results], ["standup_now"])

    def test_alarms_two_minutes_apart_do_not_share_a_slot(self) -> None:
        """Each alarm dedupes on its own key, so neither swallows the other."""
        fire_due_alarms(self.config, self.paths, controller_cls=FakeController, now=at(8, 13, 2))
        results = fire_due_alarms(self.config, self.paths, controller_cls=FakeController, now=at(8, 15, 1))
        self.assertEqual([result["alarm"] for result in results], ["standup_now"])
        self.assertEqual(set(read_state(self.paths)), {"standup_warning", "standup_now"})

    def test_an_alarm_fires_once_per_day(self) -> None:
        first = fire_due_alarms(self.config, self.paths, controller_cls=FakeController, now=at(8, 13, 2))
        self.assertEqual(len(first), 1)
        again = fire_due_alarms(self.config, self.paths, controller_cls=FakeController, now=at(8, 13, 40))
        self.assertEqual(again, [])

    def test_it_fires_again_the_next_day(self) -> None:
        fire_due_alarms(self.config, self.paths, controller_cls=FakeController, now=at(8, 13, 2))
        tomorrow = fire_due_alarms(
            self.config, self.paths, controller_cls=FakeController, now=at(8, 13, 2, day=3)
        )
        self.assertEqual([result["alarm"] for result in tomorrow], ["standup_warning"])

    def test_nothing_fires_at_an_unrelated_time(self) -> None:
        self.assertEqual(
            fire_due_alarms(self.config, self.paths, controller_cls=FakeController, now=at(11, 0)),
            [],
        )

    def test_a_late_alarm_is_skipped(self) -> None:
        alarm = find_alarm(self.config, "standup_warning")
        due, reason, _ = should_fire(self.config, self.paths, alarm, at(8, 20))
        self.assertFalse(due)
        self.assertEqual(reason, "outside-catch-up-window")

    def test_a_disabled_alarm_never_fires(self) -> None:
        self.config["alarms"][0]["enabled"] = False
        results = fire_due_alarms(self.config, self.paths, controller_cls=FakeController, now=at(8, 13, 2))
        self.assertEqual(results, [])

    def test_quiet_hours_can_suppress_an_alarm(self) -> None:
        self.config["settings"]["quiet_hours"].update({"enabled": True, "start": "08:00", "end": "09:00"})
        alarm = find_alarm(self.config, "standup_warning")
        due, reason, _ = should_fire(self.config, self.paths, alarm, at(8, 13, 2))
        self.assertFalse(due)
        self.assertEqual(reason, "quiet-hours")

    def test_firing_is_non_persistent(self) -> None:
        controller = FakeController()
        alarm = find_alarm(self.config, "standup_now")
        fire_alarm(self.config, self.paths, alarm, controller=controller, now=at(8, 15, 1))
        self.assertEqual(len(controller.applied), 1)
        self.assertFalse(controller.applied[0]["persistent"])
        self.assertFalse(controller.closed)

    def test_test_mode_does_not_consume_the_day(self) -> None:
        alarm = find_alarm(self.config, "standup_now")
        fire_alarm(
            self.config, self.paths, alarm, controller_cls=FakeController, now=at(8, 15, 1), record=False
        )
        self.assertEqual(read_state(self.paths), {})

    def test_a_preset_action_is_resolved(self) -> None:
        self.config["alarms"] = [
            {"name": "via_preset", "at": "08:13", "action": {"preset": "busy"}}
        ]
        controller = FakeController()
        fire_alarm(
            self.config, self.paths, self.config["alarms"][0], controller=controller, now=at(8, 13, 1)
        )
        self.assertEqual(controller.applied[0]["action"], self.config["presets"]["busy"])


class AlarmDayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        self.paths = build_paths(
            config_path=root / "blink-light.json",
            project_root=root,
            runtime_dir=root / "runtime",
            startup_dir=root / "startup",
        )
        self.config = default_config()
        self.config["settings"]["quiet_hours"]["enabled"] = False
        self.config["alarms"] = [
            {
                "name": "weekday_standup",
                "at": "08:13",
                "days": ["mon", "tue", "wed", "thu", "fri"],
                "action": {"scene": "standup_warning_scene"},
            }
        ]

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_it_fires_on_a_weekday(self) -> None:
        results = fire_due_alarms(self.config, self.paths, controller_cls=FakeController, now=at(8, 13, 2))
        self.assertEqual(len(results), 1)

    def test_it_stays_quiet_at_the_weekend(self) -> None:
        alarm = self.config["alarms"][0]
        due, reason, _ = should_fire(self.config, self.paths, alarm, at(8, 13, 2, day=4))
        self.assertFalse(due)
        self.assertEqual(reason, "wrong-day")

    def test_day_names_are_parsed_leniently(self) -> None:
        self.assertEqual(alarm_days({"days": ["Monday", "TUE", " wed "]}), {0, 1, 2})

    def test_no_days_means_every_day(self) -> None:
        self.assertIsNone(alarm_days({}))

    def test_the_next_slot_skips_days_it_cannot_fire_on(self) -> None:
        """A weekday alarm must not pull the scheduler awake early on Saturday."""
        # Saturday 09:00 -> the next firing is Monday 08:13, not Sunday.
        remaining = seconds_until_next_alarm(self.config, at(9, 0, 0, day=4))
        upcoming = at(9, 0, 0, day=4).timestamp() + remaining
        self.assertEqual(datetime.fromtimestamp(upcoming, tz=timezone.utc).weekday(), 0)


class AlarmScheduleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = default_config()

    def test_seconds_until_next_picks_the_soonest(self) -> None:
        # 08:00 -> the 08:13 warning is 13 minutes out.
        self.assertEqual(seconds_until_next_alarm(self.config, at(8, 0, 0)), 13 * 60)

    def test_after_the_first_it_points_at_the_second(self) -> None:
        self.assertEqual(seconds_until_next_alarm(self.config, at(8, 14, 0)), 60)

    def test_after_both_it_wraps_to_tomorrow(self) -> None:
        remaining = seconds_until_next_alarm(self.config, at(9, 0, 0))
        self.assertAlmostEqual(remaining, 23 * 3600 + 13 * 60, delta=1)

    def test_no_alarms_means_none(self) -> None:
        self.config["alarms"] = []
        self.assertIsNone(seconds_until_next_alarm(self.config, at(8, 0)))

    def test_disabled_alarms_are_not_scheduled(self) -> None:
        for alarm in self.config["alarms"]:
            alarm["enabled"] = False
        self.assertIsNone(seconds_until_next_alarm(self.config, at(8, 0)))

    def test_parse_at(self) -> None:
        self.assertEqual(parse_at("08:13"), (8, 13))
        self.assertEqual(parse_at("17:00"), (17, 0))


class AlarmStatusTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        self.paths = build_paths(
            config_path=root / "blink-light.json",
            project_root=root,
            runtime_dir=root / "runtime",
            startup_dir=root / "startup",
        )
        self.config = default_config()

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_status_lists_every_alarm(self) -> None:
        payload = alarm_status(self.config, self.paths, at(8, 0))
        self.assertEqual([entry["name"] for entry in payload["alarms"]], ["standup_warning", "standup_now"])
        self.assertEqual(payload["seconds_until_next"], 13 * 60)

    def test_status_shows_the_next_slot(self) -> None:
        payload = alarm_status(self.config, self.paths, at(8, 0))
        self.assertTrue(payload["alarms"][0]["next_slot"].startswith("2026-04-02T08:13:00"))


class AlarmConfigTests(unittest.TestCase):
    def test_default_alarms_validate(self) -> None:
        validate_config(default_config())

    def test_user_alarms_replace_the_defaults(self) -> None:
        payload = merge_config({"alarms": [{"name": "solo", "at": "09:00", "action": {"off": True}}]})
        self.assertEqual([alarm["name"] for alarm in payload["alarms"]], ["solo"])

    def test_an_empty_list_disables_all_alarms(self) -> None:
        payload = merge_config({"alarms": []})
        self.assertEqual(payload["alarms"], [])
        validate_config(payload)

    def test_duplicate_names_are_rejected(self) -> None:
        payload = default_config()
        payload["alarms"] = [
            {"name": "dup", "at": "08:13", "action": {"off": True}},
            {"name": "dup", "at": "08:15", "action": {"off": True}},
        ]
        with self.assertRaises(ConfigError) as caught:
            validate_config(payload)
        self.assertIn("more than once", str(caught.exception))

    def test_a_bad_time_is_rejected(self) -> None:
        payload = default_config()
        payload["alarms"][0]["at"] = "25:00"
        with self.assertRaises(ConfigError):
            validate_config(payload)

    def test_a_missing_name_is_rejected(self) -> None:
        payload = default_config()
        payload["alarms"] = [{"at": "08:13", "action": {"off": True}}]
        with self.assertRaises(ConfigError):
            validate_config(payload)

    def test_a_bad_day_name_is_rejected(self) -> None:
        payload = default_config()
        payload["alarms"][0]["days"] = ["funday"]
        with self.assertRaises(ConfigError):
            validate_config(payload)

    def test_an_unknown_scene_is_rejected(self) -> None:
        payload = default_config()
        payload["alarms"][0]["action"] = {"scene": "missing"}
        with self.assertRaises(ConfigError):
            validate_config(payload)

    def test_alarms_must_be_a_list(self) -> None:
        with self.assertRaises(ConfigError):
            merge_config({"alarms": {"name": "nope"}})


if __name__ == "__main__":
    unittest.main()
