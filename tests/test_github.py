from __future__ import annotations

from datetime import datetime, timedelta, timezone
from email.message import Message
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from blink_light.chime import run_chime_loop
from blink_light.cli import main
from blink_light.config import ConfigError, merge_config, validate_config
from blink_light.defaults import default_config, scene_duration_seconds
from blink_light.device import DeviceError
from blink_light.github import (
    GitHubError, GitHubPoller, NOTIFICATIONS_URL, fetch_notifications,
    gh_token, github_status, read_github_state,
)
from blink_light.paths import build_paths
from blink_light.state import write_json


def notification(identifier="17", updated="2026-09-10T12:00:00Z", *, reason="ci_activity", failed=True):
    outcome = "failed" if failed else "succeeded"
    return {"id": identifier, "updated_at": updated, "reason": reason,
            "repository": {"full_name": "example/widget"},
            "subject": {"type": "CheckSuite", "url": None,
                        "title": f"Build workflow run {outcome} for main branch"}}


class FakeController:
    actions = []

    def __init__(self, serial=None):
        pass

    def apply_action(self, action, scenes, persistent):
        self.actions.append((action, persistent))

    def close(self):
        pass


class GitHubTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.paths = build_paths(project_root=root, runtime_dir=root / "runtime", startup_dir=root / "startup")
        self.config = default_config()
        self.config["github"]["enabled"] = True
        self.config["settings"]["quiet_hours"]["enabled"] = False
        self.now = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)
        self.fetcher = Mock(return_value=(200, {}, []))
        self.token = Mock(return_value="invented-credential")
        self.poller = GitHubPoller(self.config, self.paths, fetcher=self.fetcher, token_provider=self.token)
        FakeController.actions = []

    def poll(self, **kwargs):
        return self.poller.poll(now=kwargs.pop("now", self.now), force=True,
                                controller_cls=FakeController, **kwargs)

    def baseline(self):
        self.poll()
        self.fetcher.return_value = (200, {}, [notification()])

    def test_a_new_failed_run_flashes_once(self):
        self.baseline()
        result = self.poll()
        self.assertEqual(len(result["flashed"]), 1)
        self.assertEqual(FakeController.actions, [({"scene": "ci_failed_scene"}, False)])
        self.assertEqual(read_github_state(self.paths)["last_event_flashed"]["workflow"], "Build")

    def test_the_first_poll_records_a_baseline_and_flashes_nothing(self):
        self.fetcher.return_value = (200, {}, [notification()])
        result = self.poll()
        self.assertTrue(result["baseline"])
        self.assertEqual(len(result["matches"]), 1)
        self.assertEqual(result["would_flash"], [])
        self.assertEqual(FakeController.actions, [])
        self.assertTrue(read_github_state(self.paths)["initialized"])

    def test_the_same_notification_does_not_flash_twice(self):
        self.baseline()
        self.poll()
        self.poll()
        self.assertEqual(len(FakeController.actions), 1)
        self.token.assert_called_once()

    def test_a_restart_does_not_replay_a_notification(self):
        self.baseline()
        self.poll()
        restarted = GitHubPoller(self.config, self.paths, fetcher=self.fetcher, token_provider=self.token)
        self.assertEqual(restarted.poll(now=self.now, controller_cls=FakeController)["flashed"], [])

    def test_a_rerun_that_fails_again_flashes_again(self):
        self.baseline()
        self.poll()
        self.fetcher.return_value = (200, {}, [notification(updated="2026-09-10T12:01:00Z")])
        self.poll()
        self.assertEqual(len(FakeController.actions), 2)

    def test_a_successful_run_does_not_flash(self):
        self.baseline()
        self.fetcher.return_value = (200, {}, [notification(failed=False)])
        self.assertEqual(self.poll()["matches"], [])
        self.assertEqual(FakeController.actions, [])

    def test_other_notification_reasons_are_ignored(self):
        self.baseline()
        for reason in ("author", "subscribed", "mention", "review_requested"):
            with self.subTest(reason=reason):
                self.fetcher.return_value = (200, {}, [notification(reason=reason)])
                self.assertEqual(self.poll()["matches"], [])
        self.assertEqual(FakeController.actions, [])

    def test_an_unseen_item_at_the_watermark_is_not_lost(self):
        self.fetcher.return_value = (200, {}, [notification()])
        self.poll()
        self.fetcher.return_value = (200, {}, [notification("18"), notification()])
        self.assertEqual(len(self.poll()["flashed"]), 1)

    def test_items_older_than_the_watermark_never_replay(self):
        self.fetcher.return_value = (200, {}, [notification()])
        self.poll()
        self.fetcher.return_value = (200, {}, [notification("18", "2026-09-09T12:00:00Z")])
        self.assertEqual(self.poll()["flashed"], [])

    def test_the_dedupe_cache_keeps_only_the_most_recent_200_keys(self):
        for index in range(205):
            self.fetcher.return_value = (200, {}, [notification(str(index), failed=False)])
            self.poll()
        state = read_github_state(self.paths)
        self.assertEqual(len(state["seen"]), 200)
        self.assertNotIn("0@2026-09-10T12:00:00Z", state["seen"])
        self.assertIn("204@2026-09-10T12:00:00Z", state["seen"])

    def test_an_unchanged_feed_sends_if_modified_since_and_flashes_nothing(self):
        modified = "Thu, 10 Sep 2026 12:00:00 GMT"
        self.fetcher.return_value = (200, {"Last-Modified": modified}, [notification()])
        self.poll()
        self.fetcher.return_value = (304, {"X-Poll-Interval": "90"}, None)
        self.assertEqual(self.poll()["result"], 304)
        args, kwargs = self.fetcher.call_args
        self.assertEqual(args[0], NOTIFICATIONS_URL)
        self.assertEqual(args[1]["If-Modified-Since"], modified)
        self.assertEqual(args[1]["Authorization"], "Bearer invented-credential")
        self.assertEqual(kwargs["timeout"], 5)
        self.assertEqual(FakeController.actions, [])

    def test_a_changed_feed_without_last_modified_clears_the_old_validator(self):
        self.fetcher.return_value = (200, {"Last-Modified": "old-value"}, [])
        self.poll()
        self.fetcher.return_value = (200, {}, [])
        self.poll()
        self.poll()
        self.assertNotIn("If-Modified-Since", self.fetcher.call_args.args[1])

    def test_the_poll_waits_for_the_larger_of_the_config_and_x_poll_interval(self):
        for configured, server, expected in ((60, "120", 120), (180, "90", 180), (60, "nan", 60)):
            with self.subTest(configured=configured, server=server):
                write_json(self.paths.github_state_path, {})
                self.config["github"]["poll_seconds"] = configured
                self.fetcher.return_value = (200, {"X-Poll-Interval": server}, [])
                self.poll()
                self.assertEqual(self.poller.seconds_until_next_poll(self.now), expected)
                self.fetcher.reset_mock()
                self.assertEqual(self.poller.poll(now=self.now + timedelta(seconds=expected - 1))["reason"], "not-due")
                self.fetcher.assert_not_called()

    def test_an_error_retains_the_server_poll_interval(self):
        self.fetcher.return_value = (200, {"X-Poll-Interval": "300"}, [])
        self.poll()
        self.fetcher.side_effect = GitHubError("offline")
        self.poll()
        self.assertEqual(self.poller.seconds_until_next_poll(self.now), 300)

    def test_rate_limiting_obeys_retry_after(self):
        self.fetcher.return_value = (429, {"Retry-After": "600"}, None)
        self.assertEqual(self.poll()["result"], "error")
        self.assertEqual(self.poller.seconds_until_next_poll(self.now), 600)

    def test_quiet_hours_advance_the_watermark_without_flashing(self):
        self.baseline()
        self.config["settings"]["quiet_hours"] = {"enabled": True, "start": "17:00", "end": "07:00"}
        result = self.poll(now=self.now.replace(hour=3))
        self.assertTrue(result["quiet_hours"])
        self.assertEqual(self.poll()["flashed"], [])
        self.assertIsNotNone(read_github_state(self.paths)["watermark"])
        self.assertEqual(FakeController.actions, [])

    def test_quiet_hours_can_be_explicitly_ignored(self):
        self.baseline()
        self.config["settings"]["quiet_hours"]["enabled"] = True
        self.config["github"]["respect_quiet_hours"] = False
        self.assertEqual(len(self.poll(now=self.now.replace(hour=3))["flashed"]), 1)

    def test_an_absent_device_consumes_the_event_before_attempting_a_flash(self):
        self.baseline()
        with patch("blink_light.github.fire_notify", side_effect=DeviceError("not connected")):
            with self.assertRaises(DeviceError):
                self.poll()
        self.assertEqual(self.poll()["flashed"], [])
        self.assertNotIn("last_event_flashed", read_github_state(self.paths))

    def test_a_disabled_integration_makes_no_request(self):
        self.config["github"]["enabled"] = False
        self.assertEqual(self.poll()["reason"], "disabled")
        self.assertEqual(self.poll(dry_run=True)["reason"], "disabled")
        self.fetcher.assert_not_called()
        self.token.assert_not_called()
        self.assertFalse(self.paths.runtime_dir.exists())
        self.assertIsNone(self.poller.seconds_until_next_poll(self.now))

    def test_a_401_refetches_the_token_once_on_the_next_poll(self):
        self.fetcher.return_value = (401, {}, None)
        self.poll()
        self.assertEqual(self.fetcher.call_count, 1)
        self.assertEqual(self.token.call_count, 1)
        self.poll()
        self.assertEqual(self.fetcher.call_count, 2)
        self.assertEqual(self.token.call_count, 2)
        self.poll()
        self.assertEqual(self.token.call_count, 2)
        self.assertEqual(self.fetcher.call_count, 2)

    def test_a_success_after_token_refresh_keeps_the_existing_baseline(self):
        self.baseline()
        self.fetcher.return_value = (401, {}, None)
        self.poll()
        self.fetcher.return_value = (200, {}, [notification()])
        self.assertEqual(len(self.poll()["flashed"]), 1)
        self.assertEqual(self.token.call_count, 2)

    def test_dry_run_saves_nothing_and_flashes_nothing(self):
        self.baseline()
        before = self.paths.github_state_path.read_bytes()
        deadline = self.poller.next_poll_at
        result = self.poll(dry_run=True, now=self.now + timedelta(minutes=1))
        self.assertEqual(len(result["would_flash"]), 1)
        self.assertEqual(self.paths.github_state_path.read_bytes(), before)
        self.assertEqual(self.poller.next_poll_at, deadline)
        self.assertEqual(FakeController.actions, [])
        self.assertEqual(len(self.poll()["flashed"]), 1)

    def test_a_first_dry_run_lists_baseline_failures_without_creating_state(self):
        self.fetcher.return_value = (200, {}, [notification()])
        result = self.poll(dry_run=True)
        self.assertTrue(result["baseline"])
        self.assertEqual(len(result["matches"]), 1)
        self.assertEqual(result["would_flash"], [])
        self.assertFalse(self.paths.runtime_dir.exists())
        self.assertIsNone(self.poller._token)

    def test_a_failed_dry_run_does_not_change_state_or_deadline(self):
        self.baseline()
        before = self.paths.github_state_path.read_bytes()
        deadline = self.poller.next_poll_at
        self.fetcher.return_value = (401, {}, None)
        self.assertEqual(self.poll(dry_run=True)["result"], "error")
        self.assertEqual(self.paths.github_state_path.read_bytes(), before)
        self.assertFalse(self.poller._refreshed)
        self.assertEqual(self.poller.next_poll_at, deadline)

    def test_a_missing_gh_cli_is_reported_by_status_not_raised(self):
        with patch("blink_light.github.subprocess.run", side_effect=FileNotFoundError):
            result = github_status(self.config, self.paths)
        self.assertIn("missing", result["auth"])
        self.assertFalse(self.paths.runtime_dir.exists())

    def test_a_token_never_appears_in_saved_state_or_status(self):
        self.poll()
        status = github_status(self.config, self.paths, token_provider=self.token)
        self.assertNotIn("invented-credential", self.paths.github_state_path.read_text() + json.dumps(status))

    def test_invalid_config_is_rejected_without_echoing_credentials(self):
        for override in ({"enabled": "true"}, {"respect_quiet_hours": 1},
                         *({"poll_seconds": value} for value in (True, 59, "60", float("nan"), float("inf"))),
                         {"actions_failed_event": "missing"}, {"actions_failed_event": []},
                         {"token": "invented-credential"}):
            with self.subTest(override=override):
                with self.assertRaises(ConfigError) as raised:
                    validate_config(merge_config({"github": override}))
                self.assertNotIn("invented-credential", str(raised.exception))

    def test_existing_notify_overrides_keep_the_new_ci_event(self):
        config = merge_config({"github": {"enabled": True}, "notify": {"agent_done": {"off": True}}})
        validate_config(config)
        self.assertIn("ci_failed", config["notify"])

    def test_the_ci_scene_has_a_distinct_rhythm_and_fits_the_device(self):
        scene = self.config["scenes"]["ci_failed_scene"]
        blocked = self.config["scenes"]["agent_blocked_scene"]
        self.assertFalse(scene["loop"])
        self.assertLessEqual(len(scene["steps"]), 32)
        self.assertEqual(scene["repeat"], 2)
        self.assertNotEqual(scene["steps"][0]["color"], blocked["steps"][0]["color"])
        self.assertAlmostEqual(scene_duration_seconds(scene), 2.4)
        self.assertGreater(scene["steps"][-1]["seconds"], scene["steps"][0]["seconds"])

    def run_loop(self, iterations=4, *, fetcher=None):
        self.config["chime"]["enabled"] = False
        self.config["show"]["enabled"] = False
        self.config["alarms"] = []
        clock = [self.now]
        def sleep(seconds):
            clock[0] += timedelta(seconds=seconds)
        with patch("blink_light.github.fetch_notifications", fetcher or self.fetcher), \
             patch("blink_light.github.gh_token", self.token):
            summary = run_chime_loop(self.config, self.paths, controller_cls=FakeController,
                                     now_factory=lambda: clock[0], sleep=sleep, max_iterations=iterations)
        return summary, self.paths.log_path.read_text(encoding="utf-8")

    def test_an_offline_poll_is_logged_once_per_hour_not_as_a_traceback(self):
        self.fetcher.side_effect = GitHubError("offline")
        summary, log = self.run_loop(iterations=62)
        self.assertEqual(summary["iterations"], 62)
        self.assertEqual(log.count("GitHub: offline"), 2)
        self.assertNotIn("Traceback", log)

    def test_an_unexpected_poll_error_logs_a_traceback_and_the_loop_continues(self):
        self.fetcher.side_effect = ValueError("invented defect")
        summary, log = self.run_loop()
        self.assertEqual(summary["iterations"], 4)
        self.assertIn("Traceback", log)
        self.assertIn("GitHub failed", log)

    def test_an_unchanged_feed_is_logged_once_per_quiet_stretch_not_every_poll(self):
        responses = iter([(200, {}, []), (304, {}, None), (304, {}, None), (304, {}, None),
                          (200, {}, []), (304, {}, None), (304, {}, None)])
        fetcher = Mock(side_effect=lambda *args, **kwargs: next(responses, (304, {}, None)))
        summary, log = self.run_loop(iterations=10, fetcher=fetcher)
        self.assertGreaterEqual(fetcher.call_count, 7)
        self.assertEqual(log.count("GitHub: poll 304 (unchanged)"), 2)

    def test_the_scheduler_polls_once_per_due_wake_and_flashes_one_failure(self):
        self.fetcher.side_effect = [(200, {}, []), (200, {"X-Poll-Interval": "120"}, [notification()]),
                                    (304, {}, None)]
        summary, log = self.run_loop(iterations=4)
        self.assertEqual(summary["iterations"], 4)
        self.assertEqual(self.fetcher.call_count, 3)
        self.assertEqual(len(FakeController.actions), 1)
        self.assertEqual(log.count("GitHub: workflow run failed - example/widget / Build on main"), 1)

    def test_the_scheduler_uses_the_github_deadline_in_its_sleep_candidates(self):
        self.now = self.now.replace(minute=59, second=40)
        call_times = []
        clock = [self.now]
        self.config["chime"]["enabled"] = False
        self.config["show"]["enabled"] = False
        self.config["alarms"] = []
        def fetch(*args, **kwargs):
            call_times.append(clock[0])
            return 200, {}, []
        def sleep(seconds):
            clock[0] += timedelta(seconds=seconds)
        with patch("blink_light.github.fetch_notifications", fetch), patch("blink_light.github.gh_token", self.token):
            run_chime_loop(self.config, self.paths, now_factory=lambda: clock[0], sleep=sleep, max_iterations=3)
        self.assertEqual(len(call_times), 2)
        self.assertLessEqual((call_times[1] - call_times[0]).total_seconds(), 61)

    def test_cli_status_check_and_test_use_the_integration(self):
        write_json(self.paths.config_path, self.config)
        with patch("blink_light.github.fetch_notifications", self.fetcher), patch("blink_light.github.gh_token", self.token):
            for arguments in (["github", "status"], ["github", "check", "--dry-run"], ["github", "test"]):
                with self.subTest(arguments=arguments):
                    output = io.StringIO()
                    self.assertEqual(main(arguments, paths=self.paths, controller_cls=FakeController, out=output), 0)
                    self.assertNotIn("invented-credential", output.getvalue())
        self.assertFalse(self.paths.github_state_path.exists())
        self.assertEqual(len(FakeController.actions), 1)


class GitHubTransportTests(unittest.TestCase):
    def test_auth_uses_a_hidden_process_with_a_five_second_timeout(self):
        result = subprocess.CompletedProcess([], 0, "invented-credential\n", "")
        with patch("blink_light.github.subprocess.run", return_value=result) as run:
            self.assertEqual(gh_token(), "invented-credential")
        self.assertEqual(run.call_args.kwargs["timeout"], 5)
        self.assertEqual(run.call_args.kwargs["creationflags"], getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.assertEqual(run.call_args.args[0], ["gh", "auth", "token", "--hostname", "github.com"])

    def test_auth_failures_never_include_subprocess_output(self):
        result = subprocess.CompletedProcess([], 1, "invented-credential", "private stderr")
        with patch("blink_light.github.subprocess.run", return_value=result):
            with self.assertRaises(GitHubError) as raised:
                gh_token()
        self.assertNotIn("invented-credential", str(raised.exception))
        self.assertNotIn("private stderr", str(raised.exception))

    def test_auth_timeouts_are_expected_errors(self):
        with patch("blink_light.github.subprocess.run", side_effect=subprocess.TimeoutExpired("gh", 5)):
            with self.assertRaisesRegex(GitHubError, "timed out"):
                gh_token()

    def test_invalid_token_output_is_rejected_without_echoing_it(self):
        result = subprocess.CompletedProcess([], 0, "invented-credential\nextra-line", "")
        with patch("blink_light.github.subprocess.run", return_value=result):
            with self.assertRaises(GitHubError) as raised:
                gh_token()
        self.assertNotIn("invented-credential", str(raised.exception))

    def test_urllib_304_is_a_normal_response(self):
        headers = Message()
        headers["X-Poll-Interval"] = "60"
        error = HTTPError(NOTIFICATIONS_URL, 304, "Not Modified", headers, io.BytesIO())
        with patch("blink_light.github.build_opener") as opener:
            opener.return_value.open.side_effect = error
            self.assertEqual(fetch_notifications(NOTIFICATIONS_URL, {}), (304, dict(headers), None))

    def test_urllib_network_errors_do_not_expose_request_details(self):
        with patch("blink_light.github.build_opener") as opener:
            opener.return_value.open.side_effect = URLError("private request details")
            with self.assertRaises(GitHubError) as raised:
                fetch_notifications(NOTIFICATIONS_URL, {})
        self.assertNotIn("private request details", str(raised.exception))

    def test_the_http_request_is_read_only_and_decodes_json(self):
        with patch("blink_light.github.build_opener") as opener:
            response = opener.return_value.open.return_value.__enter__.return_value
            response.status = 200
            response.headers = {"X-Poll-Interval": "60"}
            response.read.return_value = b"[]"
            result = fetch_notifications(NOTIFICATIONS_URL, {"User-Agent": "blink-light"})
            request = opener.return_value.open.call_args.args[0]
            self.assertEqual(request.get_method(), "GET")
            self.assertEqual(request.full_url, NOTIFICATIONS_URL)
            self.assertEqual(opener.return_value.open.call_args.kwargs["timeout"], 5)
            self.assertEqual(result, (200, {"X-Poll-Interval": "60"}, []))


if __name__ == "__main__":
    unittest.main()
