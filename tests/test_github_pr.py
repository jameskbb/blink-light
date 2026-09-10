from __future__ import annotations

from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from blink_light.chime import run_chime_loop
from blink_light.cli import main
from blink_light.config import ConfigError, merge_config, validate_config
from blink_light.defaults import (
    PR_COLOR,
    PR_DIM_COLOR,
    default_config,
    scene_duration_seconds,
)
from blink_light.device import DeviceError
from blink_light.github import (
    GitHubError,
    GitHubPoller,
    NOTIFICATIONS_URL,
    USER_URL,
    github_status,
    read_github_state,
)
from blink_light.paths import build_paths
from blink_light.state import write_json


class FakeController:
    actions = []

    def __init__(self, serial=None):
        pass

    def apply_action(self, action, scenes, persistent):
        self.actions.append((action, persistent))

    def close(self):
        pass


def pr_notification(
    identifier="41",
    updated="2026-09-10T12:05:00Z",
    *,
    reason="review_requested",
    number=7,
    subject_type="PullRequest",
    last_read_at=None,
    url=None,
):
    return {
        "id": identifier,
        "updated_at": updated,
        "reason": reason,
        "last_read_at": last_read_at,
        "repository": {"full_name": "example/widget"},
        "subject": {
            "type": subject_type,
            "url": url or f"https://api.github.com/repos/example/widget/pulls/{number}",
            "title": "Invented change",
        },
    }


def ci_notification(identifier="99", updated="2026-09-10T12:00:00Z"):
    return {
        "id": identifier,
        "updated_at": updated,
        "reason": "ci_activity",
        "repository": {"full_name": "example/widget"},
        "subject": {"type": "CheckSuite", "url": None,
                    "title": "Build workflow run failed for main branch"},
    }


def review(identifier, submitted, login="reviewer-one", user_type="User", state="COMMENTED"):
    return {"id": identifier, "user": {"login": login, "type": user_type},
            "state": state, "submitted_at": submitted}


class FakeClock:
    """A monotonic stand-in: reading it never advances it, only a call to
    FakeGitHub does, so tests can assert exactly which fetches cost time."""

    def __init__(self, start: float = 1000.0):
        self.value = start

    def __call__(self) -> float:
        return self.value


class FakeGitHub:
    """A fetcher fake that shares a clock with the poller under test.

    ``pages`` maps a URL to a ``(status, headers, body)`` tuple or an
    exception instance to raise - the same shape ``fetch_notifications``
    returns, so it slots into ``GitHubPoller(fetcher=...)`` directly.
    """

    def __init__(self, clock: FakeClock, feed=(200, {}, []), login="owner-one", latency=0.0):
        self.clock = clock
        self.feed = feed
        self.pages: dict[str, object] = {}
        self.login = login
        self.latency = latency
        self.calls: list[tuple] = []

    def __call__(self, url, headers, timeout=5):
        self.calls.append((url, dict(headers), timeout))
        self.clock.value += self.latency
        if url in self.pages:
            entry = self.pages[url]
            if isinstance(entry, BaseException):
                raise entry
            return entry
        if url == NOTIFICATIONS_URL:
            return self.feed
        if url == USER_URL:
            return 200, {}, {"login": self.login}
        return 404, {}, None


PR_SCENE_NAMES = ("pr_review_requested_scene", "pr_mentioned_scene", "pr_review_received_scene")
NOTIFICATION_SCENE_NAMES = ("agent_done_scene", "agent_blocked_scene", "ci_failed_scene")


def _rhythm(scene):
    return (int(scene.get("repeat", 1)), tuple((step["seconds"], step.get("led", 0)) for step in scene["steps"]))


class GitHubPullRequestConfigTests(unittest.TestCase):
    def test_invalid_pull_request_settings_are_rejected_without_echoing_values(self):
        cases = [
            {"pr": "invented-login-value"},
            {"pr": {"unknown_key": True}},
            {"pr": {"mentioned": "invented-login-value"}},
            {"pr": {"ignore_logins": "invented-login-value"}},
            {"pr": {"ignore_logins": [""]}},
            {"pr": {"ignore_logins": ["invented login value"]}},
            {"pr": {"ignore_logins": [12345]}},
            {"pr": {"ignore_logins": ["invented-login-value" * 10]}},
        ]
        for override in cases:
            with self.subTest(override=override):
                with self.assertRaises(ConfigError) as raised:
                    validate_config(merge_config({"github": override}))
                self.assertNotIn("invented-login-value", str(raised.exception))
                self.assertNotIn("unknown_key", str(raised.exception))

    def test_a_partial_pull_request_block_keeps_the_other_defaults(self):
        config = merge_config({"github": {"pr": {"mentioned": False}}})
        validate_config(config)
        self.assertTrue(config["github"]["pr"]["review_requested"])
        self.assertFalse(config["github"]["pr"]["mentioned"])
        self.assertTrue(config["github"]["pr"]["review_received"])
        self.assertEqual(config["github"]["pr"]["ignore_logins"], [])

    def test_existing_notify_overrides_keep_the_pull_request_events(self):
        config = merge_config({"notify": {"agent_done": {"off": True}}})
        validate_config(config)
        for event in ("pr_review_requested", "pr_mentioned", "pr_review_received"):
            with self.subTest(event=event):
                self.assertIn(event, config["notify"])


class GitHubPullRequestSceneTests(unittest.TestCase):
    def setUp(self):
        self.scenes = default_config()["scenes"]

    def test_pull_request_scenes_fit_the_device_and_differ_in_rhythm_from_every_notification_scene(self):
        notification_first_colors = {self.scenes[name]["steps"][0]["color"] for name in NOTIFICATION_SCENE_NAMES}
        notification_rhythms = {_rhythm(self.scenes[name]) for name in NOTIFICATION_SCENE_NAMES}
        pr_rhythms = []
        for name in PR_SCENE_NAMES:
            scene = self.scenes[name]
            with self.subTest(scene=name):
                self.assertFalse(scene["loop"])
                self.assertLessEqual(len(scene["steps"]), 32)
                self.assertLess(scene_duration_seconds(scene), 3.0)
                self.assertNotIn(scene["steps"][0]["color"], notification_first_colors)
                rhythm = _rhythm(scene)
                self.assertNotIn(rhythm, notification_rhythms)
                self.assertNotIn(rhythm, pr_rhythms)
                pr_rhythms.append(rhythm)
                for step in scene["steps"]:
                    self.assertIn(step["color"], (PR_COLOR, PR_DIM_COLOR, "#000000"))

    def test_pull_request_scenes_stay_at_notify_brightness(self):
        self.assertEqual(self.scenes["pr_review_requested_scene"]["steps"][0]["color"], PR_COLOR)
        self.assertEqual(self.scenes["pr_mentioned_scene"]["steps"][0]["color"], PR_COLOR)
        self.assertEqual(self.scenes["pr_review_received_scene"]["steps"][0]["color"], PR_DIM_COLOR)
        for name in PR_SCENE_NAMES:
            for step in self.scenes[name]["steps"]:
                self.assertIn(step["color"], (PR_COLOR, PR_DIM_COLOR, "#000000"))


class GitHubPullRequestPollTests(unittest.TestCase):
    def setUp(self):
        self._fresh_fixture()

    def _fresh_fixture(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.paths = build_paths(project_root=root, runtime_dir=root / "runtime", startup_dir=root / "startup")
        self.config = default_config()
        self.config["github"]["enabled"] = True
        self.config["settings"]["quiet_hours"]["enabled"] = False
        self.clock = FakeClock()
        self.github_fetcher = FakeGitHub(self.clock)
        self.token = Mock(return_value="invented-credential")
        self.poller = GitHubPoller(self.config, self.paths, fetcher=self.github_fetcher,
                                   token_provider=self.token, clock=self.clock)
        self.now = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)
        FakeController.actions = []

    def poll(self, **kwargs):
        now = kwargs.pop("now", self.now)
        return self.poller.poll(now=now, force=True, controller_cls=FakeController, **kwargs)

    def baseline(self):
        self.github_fetcher.feed = (200, {}, [])
        self.poll()
        self.github_fetcher.calls = []

    def review_url(self, number=7):
        return f"https://api.github.com/repos/example/widget/pulls/{number}/reviews?per_page=100"

    def run_loop(self, iterations=4, *, fetcher=None):
        self.config["chime"]["enabled"] = False
        self.config["show"]["enabled"] = False
        self.config["alarms"] = []
        clock_dt = [self.now]

        def sleep(seconds):
            clock_dt[0] += timedelta(seconds=seconds)

        with patch("blink_light.github.fetch_notifications", fetcher or self.github_fetcher), \
             patch("blink_light.github.gh_token", self.token):
            summary = run_chime_loop(self.config, self.paths, controller_cls=FakeController,
                                     now_factory=lambda: clock_dt[0], sleep=sleep, max_iterations=iterations)
        return summary, self.paths.log_path.read_text(encoding="utf-8")

    # -- Reason rules ----------------------------------------------------

    def test_a_new_review_request_flashes_once_without_an_extra_request(self):
        self.baseline()
        self.github_fetcher.feed = (200, {}, [pr_notification(reason="review_requested")])
        result = self.poll()
        self.assertEqual(len(result["flashed"]), 1)
        self.assertEqual(result["flashed"][0]["event"], "pr_review_requested")
        self.assertEqual([call[0] for call in self.github_fetcher.calls], [NOTIFICATIONS_URL])

    def test_a_new_mention_or_team_mention_flashes_pr_mentioned(self):
        for reason in ("mention", "team_mention"):
            with self.subTest(reason=reason):
                self._fresh_fixture()
                self.baseline()
                self.github_fetcher.feed = (200, {}, [pr_notification(reason=reason)])
                result = self.poll()
                self.assertEqual(len(result["flashed"]), 1)
                self.assertEqual(result["flashed"][0]["event"], "pr_mentioned")

    def test_further_activity_on_an_unread_thread_with_the_same_reason_does_not_flash_again(self):
        self.baseline()
        self.github_fetcher.feed = (200, {}, [pr_notification(reason="review_requested")])
        self.poll()
        self.github_fetcher.feed = (
            200, {}, [pr_notification(updated="2026-09-10T12:06:00Z", reason="review_requested")])
        result = self.poll()
        self.assertEqual(result["flashed"], [])

    def test_activity_after_you_read_the_thread_flashes_again(self):
        self.baseline()
        self.github_fetcher.feed = (200, {}, [pr_notification(reason="review_requested",
                                                                updated="2026-09-10T12:05:00Z")])
        self.poll()
        self.github_fetcher.feed = (200, {}, [pr_notification(
            reason="review_requested", updated="2026-09-10T12:10:00Z", last_read_at="2026-09-10T12:06:00Z")])
        result = self.poll()
        self.assertEqual(len(result["flashed"]), 1)

    def test_a_thread_whose_reason_changes_to_a_mention_flashes(self):
        self.baseline()
        self.github_fetcher.feed = (200, {}, [pr_notification(reason="author", updated="2026-09-10T12:05:00Z")])
        self.poll()
        self.github_fetcher.feed = (200, {}, [pr_notification(reason="mention", updated="2026-09-10T12:06:00Z")])
        result = self.poll()
        self.assertEqual(len(result["flashed"]), 1)
        self.assertEqual(result["flashed"][0]["event"], "pr_mentioned")

    def test_each_disabled_pull_request_reason_stays_silent(self):
        for toggle, reason in (("review_requested", "review_requested"),
                               ("mentioned", "mention"),
                               ("review_received", "author")):
            with self.subTest(toggle=toggle):
                self._fresh_fixture()
                self.config["github"]["pr"][toggle] = False
                self.baseline()
                self.github_fetcher.feed = (200, {}, [pr_notification(reason=reason)])
                result = self.poll()
                self.assertEqual(result["flashed"], [])
                if toggle == "review_received":
                    urls = [call[0] for call in self.github_fetcher.calls]
                    self.assertNotIn(USER_URL, urls)
                    self.assertFalse(any("/reviews" in url for url in urls))

    def test_other_reasons_and_non_pull_request_subjects_never_flash(self):
        self.baseline()
        cases = [
            pr_notification(identifier="s1", reason="subscribed"),
            pr_notification(identifier="s2", reason="state_change"),
            pr_notification(identifier="s3", reason="assign"),
            pr_notification(identifier="s4", reason="comment"),
            pr_notification(identifier="s5", reason="mention", subject_type="Issue"),
        ]
        for case in cases:
            with self.subTest(case=case["id"]):
                self.github_fetcher.feed = (200, {}, [case])
                result = self.poll()
                self.assertEqual(result["flashed"], [])
        self.assertNotIn(USER_URL, [call[0] for call in self.github_fetcher.calls])

    # -- Upgrade -----------------------------------------------------------

    def test_upgrading_an_actions_only_state_rereads_the_feed_and_baselines_pull_requests_silently(self):
        write_json(self.paths.github_state_path, {
            "initialized": True, "last_modified": "Thu, 10 Sep 2026 11:00:00 GMT",
            "watermark": "2026-09-10T11:00:00Z", "seen": [],
        })
        self.github_fetcher.feed = (200, {}, [
            pr_notification(reason="review_requested"),
            ci_notification(),
        ])
        result = self.poll()
        self.assertEqual(len(self.github_fetcher.calls), 1)
        self.assertNotIn("If-Modified-Since", self.github_fetcher.calls[0][1])
        self.assertTrue(result["pr_baseline"])
        self.assertEqual(len(result["flashed"]), 1)
        self.assertEqual(result["flashed"][0]["kind"], "actions_failed")

    # -- Quiet hours ---------------------------------------------------------

    def test_quiet_hours_record_pull_request_activity_without_flashing_or_reading_reviews(self):
        self.baseline()
        self.config["settings"]["quiet_hours"] = {"enabled": True, "start": "17:00", "end": "07:00"}
        self.github_fetcher.feed = (200, {}, [pr_notification(reason="author", updated="2026-09-10T03:05:00Z")])
        result = self.poll(now=self.now.replace(hour=3))
        self.assertTrue(result["quiet_hours"])
        self.assertEqual(result["flashed"], [])
        self.assertNotIn(USER_URL, [call[0] for call in self.github_fetcher.calls])
        state = read_github_state(self.paths)
        self.assertIn("41", state["threads"])
        self.assertIsNotNone(state.get("review_floor"))

    # -- Memory bounds --------------------------------------------------------

    def test_thread_and_review_memory_stay_bounded(self):
        self.baseline()
        base = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)
        for index in range(205):
            updated = (base + timedelta(minutes=index)).strftime("%Y-%m-%dT%H:%M:%SZ")
            self.github_fetcher.feed = (200, {}, [pr_notification(
                identifier=str(index), updated=updated, reason="review_requested")])
            self.poll(now=base + timedelta(minutes=index, seconds=1))
        state = read_github_state(self.paths)
        self.assertLessEqual(len(state["threads"]), 200)
        self.assertLessEqual(len(state.get("seen_reviews", [])), 200)
        self.assertNotIn("0", state["threads"])
        self.assertIn("204", state["threads"])

    # -- Review received --------------------------------------------------

    def test_someone_elses_review_on_your_pull_request_flashes_once(self):
        self.baseline()
        self.github_fetcher.pages[self.review_url()] = (200, {}, [review("501", "2026-09-10T12:04:40Z")])
        self.github_fetcher.feed = (200, {}, [pr_notification(reason="author", updated="2026-09-10T12:05:00Z")])
        result = self.poll()
        self.assertEqual(len(result["flashed"]), 1)
        self.assertEqual(result["flashed"][0]["event"], "pr_review_received")
        self.assertEqual(result["flashed"][0]["reviewer"], "reviewer-one")
        user_calls = [call for call in self.github_fetcher.calls if call[0] == USER_URL]
        self.assertEqual(len(user_calls), 1)
        self.assertNotIn("If-Modified-Since", user_calls[0][1])
        self.assertEqual(user_calls[0][1]["Authorization"], "Bearer invented-credential")
        state = read_github_state(self.paths)
        self.assertIn("501", state["seen_reviews"])

    def test_a_plain_comment_on_your_pull_request_does_not_flash(self):
        self.baseline()
        self.github_fetcher.pages[self.review_url()] = (200, {}, [])
        self.github_fetcher.feed = (200, {}, [pr_notification(reason="author")])
        result = self.poll()
        self.assertEqual(result["flashed"], [])
        self.assertEqual(result["review_checks_skipped"], 0)

    def test_your_own_review_replies_do_not_count(self):
        self.baseline()
        self.github_fetcher.pages[self.review_url()] = (200, {}, [review("601", "2026-09-10T12:04:40Z",
                                                                          login="owner-one")])
        self.github_fetcher.feed = (200, {}, [pr_notification(reason="author", updated="2026-09-10T12:05:00Z")])
        result = self.poll()
        self.assertEqual(result["flashed"], [])

    def test_ignored_logins_are_muted_case_insensitively(self):
        self.baseline()
        self.config["github"]["pr"]["ignore_logins"] = ["muted-bot"]
        self.github_fetcher.pages[self.review_url()] = (200, {}, [review("602", "2026-09-10T12:04:40Z",
                                                                          login="MUTED-BOT")])
        self.github_fetcher.feed = (200, {}, [pr_notification(reason="author", updated="2026-09-10T12:05:00Z")])
        result = self.poll()
        self.assertEqual(result["flashed"], [])

    def test_bot_reviews_count_as_reviews(self):
        self.baseline()
        self.github_fetcher.pages[self.review_url()] = (200, {}, [review(
            "701", "2026-09-10T12:04:40Z", login="code-review-bot[bot]", user_type="Bot")])
        self.github_fetcher.feed = (200, {}, [pr_notification(reason="author", updated="2026-09-10T12:05:00Z")])
        result = self.poll()
        self.assertEqual(len(result["flashed"]), 1)
        self.assertEqual(result["flashed"][0]["reviewer"], "code-review-bot[bot]")

    def test_a_comment_after_a_flashed_review_does_not_flash_that_review_again(self):
        self.baseline()
        self.github_fetcher.pages[self.review_url()] = (200, {}, [review("801", "2026-09-10T12:04:40Z")])
        self.github_fetcher.feed = (200, {}, [pr_notification(reason="author", updated="2026-09-10T12:05:00Z")])
        self.poll()
        self.github_fetcher.feed = (200, {}, [pr_notification(reason="author", updated="2026-09-10T12:05:30Z")])
        result = self.poll(now=self.now + timedelta(seconds=30))
        self.assertEqual(result["flashed"], [])

    def test_a_review_submitted_just_before_the_previous_watermark_is_still_caught(self):
        self.baseline()
        self.github_fetcher.feed = (200, {}, [pr_notification(reason="mention", updated="2026-09-10T12:05:00Z")])
        self.poll()
        self.github_fetcher.pages[self.review_url()] = (200, {}, [review("901", "2026-09-10T12:04:55Z")])
        self.github_fetcher.feed = (200, {}, [pr_notification(reason="author", updated="2026-09-10T12:06:00Z")])
        result = self.poll(now=self.now + timedelta(minutes=1))
        self.assertEqual(len(result["flashed"]), 1)

    def test_reviews_from_before_the_baseline_never_flash(self):
        self.baseline()
        self.github_fetcher.pages[self.review_url()] = (200, {}, [review("1001", "2026-09-10T11:59:00Z")])
        self.github_fetcher.feed = (200, {}, [pr_notification(reason="author", updated="2026-09-10T12:05:00Z")])
        result = self.poll()
        self.assertEqual(result["flashed"], [])

    def test_your_login_is_read_once_per_poller(self):
        self.baseline()
        self.github_fetcher.pages[self.review_url()] = (200, {}, [review("1101", "2026-09-10T12:04:40Z")])
        self.github_fetcher.feed = (200, {}, [pr_notification(reason="author", updated="2026-09-10T12:05:00Z")])
        self.poll()
        self.github_fetcher.pages[self.review_url()] = (200, {}, [review("1102", "2026-09-10T12:06:40Z")])
        self.github_fetcher.feed = (200, {}, [pr_notification(reason="author", updated="2026-09-10T12:07:00Z")])
        self.poll(now=self.now + timedelta(minutes=2))
        user_calls = [call for call in self.github_fetcher.calls if call[0] == USER_URL]
        self.assertEqual(len(user_calls), 1)

    def test_a_full_first_page_reads_the_last_page_from_a_locally_built_url(self):
        self.baseline()
        page1_url = self.review_url()
        old_reviews = [review(f"page1-{index}", "2026-09-10T00:00:00Z") for index in range(100)]
        foreign_url = "https://api.github.com/repositories/1/pulls/7/reviews?page=3"
        self.github_fetcher.pages[page1_url] = (
            200, {"Link": f'<{foreign_url}>; rel="last"'}, old_reviews,
        )
        last_url = self.review_url() + "&page=3"
        self.github_fetcher.pages[last_url] = (200, {}, [review("1201", "2026-09-10T12:04:40Z")])
        self.github_fetcher.feed = (200, {}, [pr_notification(reason="author", updated="2026-09-10T12:05:00Z")])
        result = self.poll()
        self.assertEqual(len(result["flashed"]), 1)
        urls = [call[0] for call in self.github_fetcher.calls]
        self.assertIn(last_url, urls)
        self.assertNotIn(foreign_url, urls)

    def test_a_subject_url_outside_the_github_api_is_never_requested(self):
        self.baseline()
        self.github_fetcher.feed = (200, {}, [pr_notification(
            reason="author", url="https://example.com/not-github")])
        result = self.poll()
        self.assertEqual(result["flashed"], [])
        self.assertEqual(result["review_checks_skipped"], 1)
        self.assertEqual(result["review_skip_reasons"], {"unexpected pull request URL": 1})
        self.assertNotIn(USER_URL, [call[0] for call in self.github_fetcher.calls])

    def test_review_reads_share_the_five_second_budget(self):
        self.baseline()
        self.github_fetcher.latency = 2.0
        self.github_fetcher.pages[self.review_url(7)] = (200, {}, [review("1301", "2026-09-10T12:04:40Z")])
        self.github_fetcher.pages[self.review_url(8)] = (200, {}, [review("1302", "2026-09-10T12:04:40Z")])
        self.github_fetcher.feed = (200, {}, [
            pr_notification(identifier="41", number=7, reason="author", updated="2026-09-10T12:05:00Z"),
            pr_notification(identifier="42", number=8, reason="author", updated="2026-09-10T12:05:00Z"),
        ])
        result = self.poll()
        self.assertEqual(len(result["flashed"]), 1)
        self.assertEqual(result["review_skip_reasons"].get("time budget"), 1)
        timeouts = [call[2] for call in self.github_fetcher.calls]
        self.assertEqual(timeouts, [5, 3.0, 1.0])

    def test_a_failed_review_read_skips_only_that_pull_request(self):
        self.baseline()
        self.github_fetcher.pages[self.review_url(7)] = GitHubError("boom")
        self.github_fetcher.pages[self.review_url(8)] = (200, {}, [review("1401", "2026-09-10T12:04:40Z")])
        self.github_fetcher.feed = (200, {}, [
            pr_notification(identifier="41", number=7, reason="author", updated="2026-09-10T12:05:00Z"),
            pr_notification(identifier="42", number=8, reason="author", updated="2026-09-10T12:05:00Z"),
        ])
        result = self.poll()
        self.assertEqual(len(result["flashed"]), 1)
        self.assertEqual(result["review_skip_reasons"].get("review read failed"), 1)

    def test_a_stop_request_skips_review_reads_and_later_flashes(self):
        self.baseline()
        self.github_fetcher.feed = (200, {}, [
            pr_notification(identifier="41", reason="review_requested", updated="2026-09-10T12:05:00Z"),
            pr_notification(identifier="42", reason="mention", updated="2026-09-10T12:05:00Z"),
            pr_notification(identifier="43", number=9, reason="author", updated="2026-09-10T12:05:00Z"),
        ])
        result = self.poll(stop_requested=lambda: True)
        self.assertEqual(len(result["flashed"]), 1)
        self.assertEqual(result["flashed"][0]["event"], "pr_review_requested")
        self.assertGreaterEqual(result["review_checks_skipped"], 1)
        self.assertIn("stop requested", result["review_skip_reasons"])
        self.assertNotIn(USER_URL, [call[0] for call in self.github_fetcher.calls])
        self.github_fetcher.feed = (200, {}, [])
        result2 = self.poll(now=self.now + timedelta(minutes=1))
        self.assertEqual(result2["flashed"], [])

    def test_dry_run_reads_reviews_but_saves_flashes_and_caches_nothing(self):
        self.baseline()
        self.github_fetcher.pages[self.review_url()] = (200, {}, [review("1501", "2026-09-10T12:04:40Z")])
        self.github_fetcher.feed = (200, {}, [pr_notification(reason="author", updated="2026-09-10T12:05:00Z")])
        before = self.paths.github_state_path.read_bytes()
        result = self.poll(dry_run=True)
        self.assertEqual(len(result["would_flash"]), 1)
        self.assertEqual(self.paths.github_state_path.read_bytes(), before)
        self.assertIsNone(self.poller._login)
        result2 = self.poll()
        self.assertEqual(len(result2["flashed"]), 1)

    def test_an_absent_device_consumes_pull_request_events_before_flashing(self):
        self.baseline()
        self.github_fetcher.pages[self.review_url()] = (200, {}, [review("1601", "2026-09-10T12:04:40Z")])
        self.github_fetcher.feed = (200, {}, [pr_notification(reason="author", updated="2026-09-10T12:05:00Z")])
        with patch("blink_light.github.fire_notify", side_effect=DeviceError("not connected")):
            with self.assertRaises(DeviceError):
                self.poll()
        state = read_github_state(self.paths)
        self.assertIn("1601", state["seen_reviews"])
        self.assertEqual(self.poll()["flashed"], [])

    # -- Scheduler ----------------------------------------------------------

    def test_the_scheduler_stop_request_reaches_the_github_poll(self):
        write_json(self.paths.github_state_path, {
            "initialized": True, "pr_initialized": True, "seen": [], "threads": {},
            "watermark": "2026-09-10T11:00:00Z", "review_floor": "2026-09-10T11:00:00Z",
            "last_modified": "Thu, 10 Sep 2026 11:00:00 GMT",
        })

        def fetch(url, headers, timeout=5):
            if url == NOTIFICATIONS_URL:
                self.paths.chime_stop_path.parent.mkdir(parents=True, exist_ok=True)
                self.paths.chime_stop_path.write_text("stop", encoding="utf-8")
                return 200, {}, [pr_notification(reason="author", updated="2026-09-10T12:05:00Z")]
            return 200, {}, {"login": "owner-one"}

        summary, log = self.run_loop(iterations=5, fetcher=fetch)
        self.assertEqual(summary["stopped_by"], "stop-file")
        self.assertIn("pull request review checks skipped (stop requested)", log)

    # -- CLI ------------------------------------------------------------------

    def test_github_test_plays_each_pull_request_event_and_status_reports_the_baseline(self):
        write_json(self.paths.config_path, self.config)
        with patch("blink_light.github.fetch_notifications", self.github_fetcher), \
             patch("blink_light.github.gh_token", self.token):
            for event in ("review_requested", "mentioned", "review_received"):
                output = io.StringIO()
                self.assertEqual(
                    main(["github", "test", "--event", event], paths=self.paths,
                         controller_cls=FakeController, out=output),
                    0,
                )
            self.baseline()
            output = io.StringIO()
            self.assertEqual(
                main(["github", "status"], paths=self.paths, controller_cls=FakeController, out=output), 0)
            payload = json.loads(output.getvalue())
            self.assertTrue(payload["pull_requests_baselined"])
        self.assertEqual(len(FakeController.actions), 3)


if __name__ == "__main__":
    unittest.main()
