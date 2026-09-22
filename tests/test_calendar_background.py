"""The calendar poll must not be able to stall the tick that asked for it.

`CalendarCache.get()` used to fetch inline, which put a Graph request - 20
second HTTP timeout, plus whatever MSAL spends refreshing a token - inside the
watcher's five-second tick. One slow call and the tick blew past the device
watchdog, and the blink(1) firmware took the light over with nothing raised and
nothing logged. That is the whole mechanism behind the flashing this repo spent
two rounds blaming on notification scenes.

The serverdown fix made a lapse harmless. This removes the cause: refreshes run
on their own thread and the tick reads whatever snapshot is ready, so how long
Graph takes stops being the watcher's problem. The synchronous cache stays the
default, because a one-shot `calendar status` wants an answer, not a stale one.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import threading
import time
import unittest

from blink_light.calendar_source import CalendarCache, CalendarSnapshot, evaluate_calendar_action
from blink_light.defaults import default_config
from blink_light.paths import build_paths
from blink_light.watcher import determine_action

NOON = datetime(2026, 4, 2, 12, 0, tzinfo=timezone.utc)


def snapshot(at: datetime, provider: str = "graph") -> CalendarSnapshot:
    return CalendarSnapshot(provider=provider, fetched_at=at, events=[], error=None)


def wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


class BlockingPoller:
    """A poller that will not return until the test lets it."""

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls = 0

    def __call__(self, config, now, paths=None) -> CalendarSnapshot:
        self.calls += 1
        self.entered.set()
        self.release.wait(timeout=5)
        return snapshot(now)


class BackgroundCalendarTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = default_config()
        self.config["calendar"]["enabled"] = True
        self.config["calendar"]["poll_seconds"] = 30

    def _cache(self, poller) -> CalendarCache:
        cache = CalendarCache(self.config, poller=poller, paths=None, background=True)
        self.addCleanup(cache.close)
        return cache

    def test_a_poll_that_has_not_finished_does_not_hold_up_the_tick(self) -> None:
        poller = BlockingPoller()
        cache = self._cache(poller)

        self.assertIsNone(cache.get(NOON), "nothing has been fetched yet")
        self.assertTrue(poller.entered.wait(timeout=5), "the refresh must be under way")
        self.assertIsNone(
            cache.get(NOON + timedelta(seconds=5)),
            "and the next tick must not wait for it either",
        )

        poller.release.set()

    def test_the_snapshot_appears_once_the_poll_finishes(self) -> None:
        poller = BlockingPoller()
        cache = self._cache(poller)

        cache.get(NOON)
        poller.entered.wait(timeout=5)
        poller.release.set()

        self.assertTrue(wait_until(lambda: cache.get(NOON + timedelta(seconds=5)) is not None))

    def test_ticks_during_a_slow_poll_do_not_pile_up_more_polls(self) -> None:
        """Thirty seconds of ticks against one 20-second request is six threads."""
        poller = BlockingPoller()
        cache = self._cache(poller)

        cache.get(NOON)
        poller.entered.wait(timeout=5)
        for tick in range(1, 8):
            cache.get(NOON + timedelta(seconds=5 * tick))

        self.assertEqual(poller.calls, 1)
        poller.release.set()

    def test_a_poller_that_raises_becomes_a_snapshot_carrying_the_error(self) -> None:
        """Off the tick thread, a raise would otherwise die unseen in a thread."""

        def exploding(config, now, paths=None):
            raise RuntimeError("wifi dropped")

        cache = self._cache(exploding)
        cache.get(NOON)

        later = NOON + timedelta(seconds=5)
        self.assertTrue(wait_until(lambda: cache.get(later) is not None))
        result = cache.get(later)
        self.assertIn("wifi dropped", result.error)
        self.assertEqual(result.events, [])
        self.assertEqual(result.provider, self.config["calendar"]["provider"])

    def test_the_poll_interval_restarts_when_the_snapshot_lands(self) -> None:
        """Counting from dispatch would leave a slow poll due again on arrival."""
        calls = {"n": 0}

        def counting(config, now, paths=None):
            calls["n"] += 1
            return snapshot(now)

        cache = self._cache(counting)
        cache.get(NOON)
        self.assertTrue(wait_until(lambda: cache.get(NOON + timedelta(seconds=1)) is not None))

        # The tick at +1 is the one that saw it land, so the window runs to +31.
        cache.get(NOON + timedelta(seconds=30))
        self.assertEqual(calls["n"], 1, "not due yet")

        cache.get(NOON + timedelta(seconds=31))
        self.assertTrue(wait_until(lambda: calls["n"] == 2))

    def test_a_disabled_calendar_starts_no_poll_at_all(self) -> None:
        self.config["calendar"]["enabled"] = False
        poller = BlockingPoller()
        cache = self._cache(poller)

        self.assertIsNone(cache.get(NOON))
        self.assertEqual(poller.calls, 0)

    def test_closing_the_cache_waits_for_the_refresh_to_let_go(self) -> None:
        """A watcher shutting down must not leave a poll writing behind it."""
        poller = BlockingPoller()
        cache = CalendarCache(self.config, poller=poller, paths=None, background=True)
        cache.get(NOON)
        poller.entered.wait(timeout=5)
        poller.release.set()

        cache.close()

        self.assertFalse(cache.refreshing)


class NoSnapshotYetTests(unittest.TestCase):
    """The tick before the first refresh lands must still not touch the network.

    `evaluate_calendar_action` falls back to polling inline when it is handed no
    snapshot, which was dead code while the cache always fetched before
    returning. A background cache returns None until its first refresh arrives,
    so that fallback would fire on exactly the tick where the poll is slowest -
    a cold token - and put the blocking call straight back into the tick.
    """

    def setUp(self) -> None:
        self.config = default_config()
        self.config["calendar"]["enabled"] = True
        # The default rules read a system snapshot this test does not build.
        self.config["rules"] = []
        self.paths = build_paths(
            config_path=Path("blink-light.json"),
            project_root=Path("."),
            runtime_dir=Path("runtime"),
            startup_dir=Path("startup"),
        )

    def _forbidden(self, config, now, paths=None):
        raise AssertionError("the tick must not poll the calendar itself")

    def test_an_explicit_none_snapshot_does_not_fall_back_to_polling(self) -> None:
        result = evaluate_calendar_action(
            self.config, self.paths, NOON, snapshot=None, poller=self._forbidden
        )

        self.assertIsNone(result)

    def test_a_caller_that_supplies_no_snapshot_at_all_still_polls(self) -> None:
        """`watch once` and the CLI have no cache; they must keep fetching."""
        calls = {"n": 0}

        def counting(config, now, paths=None):
            calls["n"] += 1
            return snapshot(now)

        evaluate_calendar_action(self.config, self.paths, NOON, poller=counting)

        self.assertEqual(calls["n"], 1)

    def test_the_watcher_tick_passes_the_missing_snapshot_through_as_missing(self) -> None:
        result = determine_action(
            self.config,
            self.paths,
            snapshot_factory=lambda now: None,
            now=NOON,
            calendar_snapshot=None,
            calendar_poller=self._forbidden,
        )

        self.assertNotEqual(result["source"], "calendar")


class SynchronousCalendarTests(unittest.TestCase):
    """The default is unchanged: one-shot callers still get a fresh answer."""

    def setUp(self) -> None:
        self.config = default_config()
        self.config["calendar"]["enabled"] = True
        self.config["calendar"]["poll_seconds"] = 30

    def test_the_first_get_fetches_before_it_returns(self) -> None:
        cache = CalendarCache(self.config, poller=lambda config, now: snapshot(now), paths=None)

        result = cache.get(NOON)

        self.assertIsNotNone(result)
        self.assertEqual(result.fetched_at, NOON)

    def test_it_refetches_only_once_the_poll_interval_is_up(self) -> None:
        calls = {"n": 0}

        def counting(config, now):
            calls["n"] += 1
            return snapshot(now)

        cache = CalendarCache(self.config, poller=counting, paths=None)
        cache.get(NOON)
        cache.get(NOON + timedelta(seconds=29))
        cache.get(NOON + timedelta(seconds=30))

        self.assertEqual(calls["n"], 2)


if __name__ == "__main__":
    unittest.main()
