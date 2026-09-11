"""Read-only GitHub notifications, shared by the scheduler and one-shot CLI."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from http.client import HTTPException
import json
import logging
import math
import re
import subprocess
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .defaults import PR_EVENTS
from .device import BlinkDeviceController
from .notify import fire_notify
from .paths import AppPaths
from .rules import is_between_times
from .state import read_json, write_json


NOTIFICATIONS_URL = "https://api.github.com/notifications?all=true&per_page=50"
USER_URL = "https://api.github.com/user"
FAILURE_TITLE = re.compile(r"^(?P<workflow>.+) workflow run failed for (?P<branch>.+) branch$")
# Anchored, and built only from an owner/repo/number shape GitHub itself uses -
# the bearer token must never reach a URL taken from a Link header or any
# other response field.
PULL_URL = re.compile(
    r"^https://api\.github\.com/repos/"
    r"(?P<owner>[A-Za-z0-9._-]+)/(?P<repo>[A-Za-z0-9._-]+)/pulls/(?P<number>[1-9][0-9]{0,9})$"
)
LAST_PAGE = re.compile(r'<[^>]*[?&]page=(?P<page>[0-9]{1,4})[^>]*>\s*;\s*rel="last"')
# stop_chime_loop kills the loop after 8s, and every flash already blocks for
# its scene length, so the extra login and review requests in one poll must
# share a total budget rather than each getting their own timeout.
POLL_BUDGET_SECONDS = 5.0
MIN_REQUEST_SECONDS = 0.5
# GitHub takes roughly 20s to bump a pull request thread's updated_at after a
# review is submitted (measured against the owner's live feed); this widens
# the lower bound so a review just inside that lag is still caught.
REVIEW_LAG_SECONDS = 120
PR_REASON_FAMILIES = {"review_requested": "review_requested", "mention": "mentioned", "team_mention": "mentioned"}
LOGGER = logging.getLogger("blink_light.chime")


class GitHubError(RuntimeError):
    """An expected service/auth error with a message safe to log."""


def gh_token() -> str:
    try:
        result = subprocess.run(
            ["gh", "auth", "token", "--hostname", "github.com"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except FileNotFoundError:
        raise GitHubError("GitHub CLI (gh) is missing. Install gh and run gh auth login.") from None
    except subprocess.TimeoutExpired:
        raise GitHubError("GitHub CLI authentication timed out; run gh auth status.") from None
    except OSError:
        raise GitHubError("GitHub CLI could not start; check the gh installation.") from None
    if result.returncode or not result.stdout.strip():
        # stderr and subprocess exception text can include credentials.
        raise GitHubError("GitHub CLI is not logged in to github.com; run gh auth login.")
    token = result.stdout.strip()
    if not token.isascii() or any(character.isspace() or ord(character) < 32 for character in token):
        raise GitHubError("GitHub CLI returned an invalid credential; run gh auth login.")
    return token


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward the bearer token to a location supplied by a response.
        return None


def fetch_notifications(url: str, headers: dict[str, str], timeout: float = 5):
    """Return (status, headers, decoded body); HTTP errors never expose bodies."""
    try:
        with build_opener(_NoRedirect()).open(Request(url, headers=headers), timeout=timeout) as response:
            raw = response.read(2_000_001)
            if len(raw) > 2_000_000:
                raise GitHubError("GitHub returned an oversized notification feed.")
            try:
                body = json.loads(raw)
            except (ValueError, UnicodeError):
                raise GitHubError("GitHub returned an invalid notification feed.") from None
            return response.status, dict(response.headers), body
    except HTTPError as error:
        try:
            return error.code, dict(error.headers), None
        finally:
            error.close()
    except (URLError, OSError, HTTPException):
        raise GitHubError("GitHub is unavailable (network or timeout); polling will retry.") from None


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo else None
    except ValueError:
        return None


def read_github_state(paths: AppPaths) -> dict[str, Any]:
    try:
        state = read_json(paths.github_state_path, {})
    except (ValueError, UnicodeError):
        # A damaged cache must rebaseline, never replay the entire feed.
        return {}
    if not isinstance(state, dict) or not isinstance(state.get("seen", []), list):
        return {}
    return state


def _trim_threads(threads: dict[str, Any]) -> dict[str, Any]:
    """Keep the 200 pull-request threads with the newest parseable updated_at."""
    parsed = []
    for thread_id, info in threads.items():
        if not isinstance(info, dict):
            continue
        updated = _timestamp(info.get("updated_at")) or datetime.min.replace(tzinfo=timezone.utc)
        parsed.append((updated, thread_id, info))
    parsed.sort(key=lambda entry: entry[0], reverse=True)
    return {thread_id: info for _, thread_id, info in parsed[:200]}


class GitHubPoller:
    def __init__(self, config: dict[str, Any], paths: AppPaths, *, fetcher=None, token_provider=None, clock=None):
        self.config = config
        self.paths = paths
        self.fetcher = fetcher or fetch_notifications
        self.token_provider = token_provider or gh_token
        self.clock = clock or time.monotonic
        self._token: str | None = None
        self._login: str | None = None
        self._refreshed = False
        self._auth_rejected = False
        self.next_poll_at = 0.0
        self._last_error_at: float | None = None
        self._unchanged_logged = False

    def seconds_until_next_poll(self, now: datetime) -> float | None:
        if not self.config["github"]["enabled"]:
            return None
        return max(0.0, self.next_poll_at - now.timestamp())

    def poll(self, *, now: datetime | None = None, force=False, dry_run=False,
             controller_cls=BlinkDeviceController, stop_requested=None) -> dict[str, Any]:
        current = now or datetime.now().astimezone()
        if dry_run:
            # Preview is transactional even when used on a live poller object.
            preview = GitHubPoller(self.config, self.paths, fetcher=self.fetcher,
                                   token_provider=self.token_provider, clock=self.clock)
            preview._token = self._token
            preview._login = self._login
            return preview._poll(current, force, True, controller_cls, stop_requested)
        return self._poll(current, force, False, controller_cls, stop_requested)

    def _poll(self, now, force, dry_run, controller_cls, stop_requested=None):
        settings = self.config["github"]
        if not settings["enabled"]:
            return {"polled": False, "reason": "disabled", "flashed": [], "dry_run": dry_run}
        if not force and now.timestamp() < self.next_poll_at:
            return {"polled": False, "reason": "not-due", "flashed": []}

        stop_fn = stop_requested or (lambda: False)
        # Measured from poll start, before any I/O, so the whole poll - feed,
        # login, and every review read - shares one 5s allowance.
        deadline = self.clock() + POLL_BUDGET_SECONDS

        # Set the deadline before any I/O so even an unexpected failure backs off.
        interval = float(settings["poll_seconds"])
        self.next_poll_at = now.timestamp() + interval
        state = deepcopy(read_github_state(self.paths))
        interval = max(interval, self._interval(state.get("poll_interval"), interval))
        self.next_poll_at = now.timestamp() + interval
        state["last_poll_at"] = now.isoformat()
        try:
            if self._auth_rejected:
                raise GitHubError("GitHub rejected the refreshed login; run gh auth login and restart the loop.")
            if self._token is None:
                self._token = self.token_provider()
            headers = {
                "Authorization": f"Bearer {self._token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "blink-light",
            }
            # Skip the conditional header once when upgrading from an
            # Actions-only state, so pull-request thread memory is built from
            # a real read instead of baselining on nothing.
            if state.get("initialized") and state.get("pr_initialized") and state.get("last_modified"):
                headers["If-Modified-Since"] = state["last_modified"]
            status, response_headers, body = self.fetcher(NOTIFICATIONS_URL, headers, timeout=5)
            response_headers = {key.lower(): value for key, value in response_headers.items()}
            interval = max(float(settings["poll_seconds"]),
                           self._interval(response_headers.get("x-poll-interval"), interval))
            interval = max(interval, self._interval(response_headers.get("retry-after"), interval))
            state["poll_interval"] = interval
            self.next_poll_at = now.timestamp() + interval
            state["last_http_status"] = status
            if status == 401:
                if not self._refreshed:
                    self._token = None
                    self._refreshed = True
                    raise GitHubError("GitHub returned HTTP 401; authentication will refresh on the next poll.")
                self._auth_rejected = True
                raise GitHubError("GitHub returned HTTP 401 after refresh; run gh auth login and restart the loop.")
            if status not in (200, 304):
                raise GitHubError(f"GitHub returned HTTP {status}; check gh auth status and service availability.")
            if status == 200 and not isinstance(body, list):
                raise GitHubError("GitHub returned an invalid notification feed.")
        except GitHubError as error:
            state.update(last_result="error", error=str(error))
            if not dry_run:
                write_json(self.paths.github_state_path, state)
                if self._last_error_at is None or now.timestamp() - self._last_error_at >= 3600:
                    LOGGER.info("GitHub: %s", error)
                    self._last_error_at = now.timestamp()
                    self._unchanged_logged = False
            return {"polled": True, "result": "error", "error": str(error),
                    "flashed": [], "dry_run": dry_run}

        self._refreshed = False
        state.update(last_result=status, error=None)
        if response_headers.get("last-modified"):
            state["last_modified"] = response_headers["last-modified"]
        elif status == 200:
            state.pop("last_modified", None)
        if status == 304:
            if not dry_run:
                write_json(self.paths.github_state_path, state)
                # Nearly every poll is a 304, so a line each was ~1,440 a day
                # saying the same thing. One line proves polling works; a 200
                # or a logged error re-arms it. Re-arming on every error would
                # let a flaky network bring the flood back.
                if not self._unchanged_logged:
                    LOGGER.info("GitHub: poll 304 (unchanged)")
                    self._unchanged_logged = True
            return {"polled": True, "result": 304, "flashed": [], "dry_run": dry_run}

        baseline = not state.get("initialized", False)
        # A state upgraded from Actions-only, or a genuinely fresh one, must
        # not replay old pull-request activity the moment the toggles are on.
        pr_baseline = baseline or not state.get("pr_initialized", False)
        watermark = _timestamp(state.get("watermark"))
        seen = [key for key in state.get("seen", []) if isinstance(key, str)]
        seen_set = set(seen)
        threads = state.get("threads") if isinstance(state.get("threads"), dict) else {}
        seen_reviews = [value for value in state.get("seen_reviews", []) if isinstance(value, str)]
        seen_reviews_set = set(seen_reviews)
        # Computed once, ahead of the item loop, so a quiet-hours poll can
        # both skip review reads and still record thread memory.
        quiet = self.config["settings"]["quiet_hours"]
        suppressed = bool(settings["respect_quiet_hours"] and quiet.get("enabled")
                          and is_between_times(now, quiet["start"], quiet["end"]))
        matches, pr_matches, pending = [], [], []
        review_candidates = []
        items = []
        for item in body:
            if not isinstance(item, dict):
                continue
            updated = _timestamp(item.get("updated_at"))
            if updated is not None and isinstance(item.get("id"), str):
                items.append((updated, item))
        for updated, item in sorted(items, key=lambda pair: pair[0]):
            key = f"{item['id']}@{item['updated_at']}"
            already_seen = key in seen_set
            new = not already_seen and (watermark is None or updated >= watermark)
            if not already_seen:
                seen.append(key)
                seen_set.add(key)
            subject = item.get("subject")
            subject = subject if isinstance(subject, dict) else {}
            title = subject.get("title")
            match = FAILURE_TITLE.fullmatch(title) if isinstance(title, str) else None
            if item.get("reason") == "ci_activity" and match:
                repository = item.get("repository") or {}
                event = {"key": key, "kind": "actions_failed",
                         "repo": repository.get("full_name", "unknown")
                         if isinstance(repository, dict) else "unknown",
                         **match.groupdict(), "event": settings["actions_failed_event"]}
                matches.append(event)
                if new and not baseline:
                    pending.append(event)

            if subject.get("type") == "PullRequest":
                reason = item.get("reason")
                subject_url = subject.get("url")
                pull_match = PULL_URL.fullmatch(subject_url) if isinstance(subject_url, str) else None
                number = int(pull_match.group("number")) if pull_match else None
                repository = item.get("repository") or {}
                repo_name = repository.get("full_name", "unknown") if isinstance(repository, dict) else "unknown"
                thread_id = str(item.get("id"))
                previous = threads.get(thread_id)
                family = PR_REASON_FAMILIES.get(reason)
                if not already_seen and family is not None:
                    eligible = previous is None or previous.get("reason") != family
                    if not eligible:
                        last_read = _timestamp(item.get("last_read_at"))
                        previous_updated = _timestamp(previous.get("updated_at"))
                        eligible = (
                            last_read is not None
                            and previous_updated is not None
                            and last_read > previous_updated
                        )
                    if eligible and settings["pr"].get(family):
                        pr_event = {"key": key, "kind": family, "event": PR_EVENTS[family],
                                    "repo": repo_name, "number": number}
                        pr_matches.append(pr_event)
                        if new and not pr_baseline:
                            pending.append(pr_event)
                if (
                    reason == "author"
                    and new
                    and not pr_baseline
                    and not suppressed
                    and settings["pr"].get("review_received")
                ):
                    review_candidates.append({
                        "key": key, "repo": repo_name, "number": number,
                        "subject_url": subject_url, "updated": updated,
                    })
                # Every PullRequest thread is recorded whatever the toggles,
                # baseline or quiet hours say, so enabling a toggle or leaving
                # quiet hours never replays anything already read.
                previous_updated = _timestamp(previous.get("updated_at")) if previous else None
                if not already_seen and (previous_updated is None or updated >= previous_updated):
                    threads[thread_id] = {"reason": family or reason, "updated_at": item.get("updated_at")}
        if items:
            newest = max(updated for updated, _ in items)
            if watermark is None or newest > watermark:
                state["watermark"] = newest.isoformat()

        review_checks_skipped = 0
        review_skip_reasons: dict[str, int] = {}

        def record_skip(reason: str, count: int = 1) -> None:
            nonlocal review_checks_skipped
            review_checks_skipped += count
            review_skip_reasons[reason] = review_skip_reasons.get(reason, 0) + count

        if review_candidates:
            review_floor = _timestamp(state.get("review_floor"))
            bounds = []
            if watermark is not None:
                bounds.append(watermark - timedelta(seconds=REVIEW_LAG_SECONDS))
            if review_floor is not None:
                bounds.append(review_floor)
            lower_bound = max(bounds) if bounds else None
            if lower_bound is None:
                record_skip("no review floor", len(review_candidates))
            else:
                ignored = {login.casefold() for login in settings["pr"].get("ignore_logins", [])}
                # Once one candidate is skipped for a whole-poll reason (a stop
                # request, the budget, or an unreadable login), every candidate
                # after it is skipped for that same reason - counted once each,
                # not as one bulk count, so the running total never double-counts.
                stopped_reason: str | None = None
                for candidate in review_candidates:
                    if stopped_reason is not None:
                        record_skip(stopped_reason)
                        continue
                    if stop_fn():
                        stopped_reason = "stop requested"
                        record_skip(stopped_reason)
                        continue
                    subject_url = candidate["subject_url"]
                    pull_match = PULL_URL.fullmatch(subject_url) if isinstance(subject_url, str) else None
                    if not pull_match:
                        record_skip("unexpected pull request URL")
                        continue
                    if self._login is None:
                        if deadline - self.clock() < MIN_REQUEST_SECONDS:
                            stopped_reason = "time budget"
                            record_skip(stopped_reason)
                            continue
                        timeout = min(5.0, max(0.0, deadline - self.clock()))
                        try:
                            login_status, _, login_body = self.fetcher(USER_URL, headers, timeout=timeout)
                        except GitHubError:
                            login_status, login_body = None, None
                        login_value = login_body.get("login") if isinstance(login_body, dict) else None
                        if login_status != 200 or not isinstance(login_value, str) or not login_value:
                            stopped_reason = "login unavailable"
                            record_skip(stopped_reason)
                            continue
                        self._login = login_value
                    if stop_fn():
                        stopped_reason = "stop requested"
                        record_skip(stopped_reason)
                        continue
                    if deadline - self.clock() < MIN_REQUEST_SECONDS:
                        stopped_reason = "time budget"
                        record_skip(stopped_reason)
                        continue
                    timeout = min(5.0, max(0.0, deadline - self.clock()))
                    page_url = f"{subject_url}/reviews?per_page=100"
                    try:
                        page_status, page_headers, page_body = self.fetcher(page_url, headers, timeout=timeout)
                    except GitHubError:
                        record_skip("review read failed")
                        continue
                    if page_status != 200 or not isinstance(page_body, list):
                        record_skip("review read failed")
                        continue
                    reviews = list(page_body)
                    last_page_reason = None
                    if len(page_body) == 100:
                        lowered_headers = {key.lower(): value for key, value in (page_headers or {}).items()}
                        last_page_match = LAST_PAGE.search(lowered_headers.get("link") or "")
                        if last_page_match:
                            page_number = int(last_page_match.group("page"))
                            if 2 <= page_number <= 9999:
                                if stop_fn():
                                    last_page_reason = "stop requested"
                                elif deadline - self.clock() < MIN_REQUEST_SECONDS:
                                    last_page_reason = "time budget"
                                else:
                                    last_timeout = min(5.0, max(0.0, deadline - self.clock()))
                                    # Always built locally from an integer page number,
                                    # never from the Link header's own URL.
                                    last_url = f"{subject_url}/reviews?per_page=100&page={page_number}"
                                    try:
                                        last_status, _, last_body = self.fetcher(
                                            last_url, headers, timeout=last_timeout)
                                    except GitHubError:
                                        last_page_reason = "review read failed"
                                    else:
                                        if last_status == 200 and isinstance(last_body, list):
                                            reviews.extend(last_body)
                                        else:
                                            last_page_reason = "review read failed"
                    qualifying = []
                    for review in reviews:
                        if not isinstance(review, dict):
                            continue
                        submitted = _timestamp(review.get("submitted_at"))
                        if submitted is None:
                            continue
                        user = review.get("user")
                        if not isinstance(user, dict):
                            continue
                        login = user.get("login")
                        if not isinstance(login, str) or not login:
                            continue
                        review_id = review.get("id")
                        if isinstance(review_id, bool):
                            continue
                        if isinstance(review_id, int):
                            review_id_str = str(review_id)
                        elif isinstance(review_id, str) and review_id:
                            review_id_str = review_id
                        else:
                            continue
                        if not (lower_bound < submitted <= candidate["updated"]):
                            continue
                        if login.casefold() == self._login.casefold():
                            continue
                        if login.casefold() in ignored:
                            continue
                        if review_id_str in seen_reviews_set:
                            continue
                        qualifying.append((submitted, login, review_id_str))
                    if qualifying:
                        qualifying.sort(key=lambda entry: entry[0])
                        newest_login = qualifying[-1][1]
                        review_event = {
                            "key": candidate["key"], "kind": "review_received",
                            "event": PR_EVENTS["review_received"], "repo": candidate["repo"],
                            "number": candidate["number"], "reviewer": newest_login,
                        }
                        pr_matches.append(review_event)
                        pending.append(review_event)
                        for _, _, review_id_str in qualifying:
                            seen_reviews.append(review_id_str)
                            seen_reviews_set.add(review_id_str)
                    elif last_page_reason is not None:
                        record_skip(last_page_reason)

        state.update(initialized=True, seen=seen[-200:])
        state["threads"] = _trim_threads(threads)
        state["seen_reviews"] = seen_reviews[-200:]
        state["pr_initialized"] = True
        if pr_baseline or suppressed:
            floor_candidates = [now.astimezone(timezone.utc)]
            existing_floor = _timestamp(state.get("review_floor"))
            if existing_floor is not None:
                floor_candidates.append(existing_floor)
            state["review_floor"] = max(floor_candidates).isoformat()

        result = {
            "polled": True, "result": 200, "baseline": baseline, "pr_baseline": pr_baseline,
            "matches": matches, "pr_matches": pr_matches,
            "would_flash": [] if suppressed else pending,
            "quiet_hours": suppressed, "flashed": [], "dry_run": dry_run,
            "review_checks_skipped": review_checks_skipped,
            "review_skip_reasons": review_skip_reasons,
        }
        if dry_run:
            return result
        # Consume the whole batch before device access, including quiet/absent cases.
        write_json(self.paths.github_state_path, state)
        LOGGER.info(
            "GitHub: poll 200 (%s failures, %s pull request events, baseline=%s, quiet=%s)",
            len(matches), len(pr_matches), baseline, suppressed,
        )
        self._unchanged_logged = False
        if review_checks_skipped:
            LOGGER.info(
                "GitHub: %s pull request review checks skipped (%s)",
                review_checks_skipped, ", ".join(sorted(review_skip_reasons)),
            )
        for index, event in enumerate(result["would_flash"]):
            if index > 0 and stop_fn():
                LOGGER.info("GitHub: stop requested; %s flashes skipped", len(result["would_flash"]) - index)
                break
            fire_notify(self.config, event["event"], controller_cls=controller_cls)
            result["flashed"].append(event)
            state["last_event_flashed"] = {**event, "at": now.isoformat()}
            write_json(self.paths.github_state_path, state)
            kind = event.get("kind")
            number_display = event.get("number") if event.get("number") is not None else "?"
            if kind == "actions_failed":
                LOGGER.info("GitHub: workflow run failed - %s / %s on %s",
                            event["repo"], event["workflow"], event["branch"])
            elif kind == "review_requested":
                LOGGER.info("GitHub: review requested - %s #%s", event["repo"], number_display)
            elif kind == "mentioned":
                LOGGER.info("GitHub: mentioned - %s #%s", event["repo"], number_display)
            elif kind == "review_received":
                LOGGER.info("GitHub: review received - %s #%s from %s",
                            event["repo"], number_display, event["reviewer"])
        return result

    @staticmethod
    def _interval(value, default):
        try:
            number = float(value)
            return number if math.isfinite(number) and number >= 0 else default
        except (TypeError, ValueError):
            return default


def github_status(config: dict[str, Any], paths: AppPaths, *, token_provider=None) -> dict[str, Any]:
    try:
        (token_provider or gh_token)()
        auth = "available"
    except GitHubError as error:
        auth = str(error)
    state = read_github_state(paths)
    return {"enabled": config["github"]["enabled"], "auth_source": "gh auth token (github.com)",
            "auth": auth, "poll_seconds": config["github"]["poll_seconds"],
            "last_poll_at": state.get("last_poll_at"), "last_result": state.get("last_result"),
            "watermark": state.get("watermark"), "last_event_flashed": state.get("last_event_flashed"),
            "pull_requests_baselined": bool(state.get("pr_initialized")),
            "error": state.get("error")}
