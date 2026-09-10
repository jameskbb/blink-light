"""Read-only GitHub notifications, shared by the scheduler and one-shot CLI."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from http.client import HTTPException
import json
import logging
import math
import re
import subprocess
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .device import BlinkDeviceController
from .notify import fire_notify
from .paths import AppPaths
from .rules import is_between_times
from .state import read_json, write_json


NOTIFICATIONS_URL = "https://api.github.com/notifications?all=true&per_page=50"
FAILURE_TITLE = re.compile(r"^(?P<workflow>.+) workflow run failed for (?P<branch>.+) branch$")
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


class GitHubPoller:
    def __init__(self, config: dict[str, Any], paths: AppPaths, *, fetcher=None, token_provider=None):
        self.config = config
        self.paths = paths
        self.fetcher = fetcher or fetch_notifications
        self.token_provider = token_provider or gh_token
        self._token: str | None = None
        self._refreshed = False
        self._auth_rejected = False
        self.next_poll_at = 0.0
        self._last_error_at: float | None = None

    def seconds_until_next_poll(self, now: datetime) -> float | None:
        if not self.config["github"]["enabled"]:
            return None
        return max(0.0, self.next_poll_at - now.timestamp())

    def poll(self, *, now: datetime | None = None, force=False, dry_run=False,
             controller_cls=BlinkDeviceController) -> dict[str, Any]:
        current = now or datetime.now().astimezone()
        if dry_run:
            # Preview is transactional even when used on a live poller object.
            preview = GitHubPoller(self.config, self.paths, fetcher=self.fetcher,
                                   token_provider=self.token_provider)
            preview._token = self._token
            return preview._poll(current, force, True, controller_cls)
        return self._poll(current, force, False, controller_cls)

    def _poll(self, now, force, dry_run, controller_cls):
        settings = self.config["github"]
        if not settings["enabled"]:
            return {"polled": False, "reason": "disabled", "flashed": [], "dry_run": dry_run}
        if not force and now.timestamp() < self.next_poll_at:
            return {"polled": False, "reason": "not-due", "flashed": []}

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
            if state.get("initialized") and state.get("last_modified"):
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
                LOGGER.info("GitHub: poll 304 (unchanged)")
            return {"polled": True, "result": 304, "flashed": [], "dry_run": dry_run}

        baseline = not state.get("initialized", False)
        watermark = _timestamp(state.get("watermark"))
        seen = [key for key in state.get("seen", []) if isinstance(key, str)]
        seen_set = set(seen)
        matches, pending = [], []
        items = []
        for item in body:
            if not isinstance(item, dict):
                continue
            updated = _timestamp(item.get("updated_at"))
            if updated is not None and isinstance(item.get("id"), str):
                items.append((updated, item))
        for updated, item in sorted(items, key=lambda pair: pair[0]):
            key = f"{item['id']}@{item['updated_at']}"
            new = key not in seen_set and (watermark is None or updated >= watermark)
            if key not in seen_set:
                seen.append(key)
                seen_set.add(key)
            subject = item.get("subject") or {}
            title = subject.get("title") if isinstance(subject, dict) else None
            match = FAILURE_TITLE.fullmatch(title) if isinstance(title, str) else None
            if item.get("reason") == "ci_activity" and match:
                repository = item.get("repository") or {}
                event = {"key": key, "repo": repository.get("full_name", "unknown")
                         if isinstance(repository, dict) else "unknown",
                         **match.groupdict(), "event": settings["actions_failed_event"]}
                matches.append(event)
                if new and not baseline:
                    pending.append(event)
        if items:
            newest = max(updated for updated, _ in items)
            if watermark is None or newest > watermark:
                state["watermark"] = newest.isoformat()
        state.update(initialized=True, seen=seen[-200:])
        quiet = self.config["settings"]["quiet_hours"]
        suppressed = bool(settings["respect_quiet_hours"] and quiet.get("enabled")
                          and is_between_times(now, quiet["start"], quiet["end"]))
        result = {"polled": True, "result": 200, "baseline": baseline,
                  "matches": matches, "would_flash": [] if suppressed else pending,
                  "quiet_hours": suppressed, "flashed": [], "dry_run": dry_run}
        if dry_run:
            return result
        # Consume the whole batch before device access, including quiet/absent cases.
        write_json(self.paths.github_state_path, state)
        LOGGER.info("GitHub: poll 200 (%s failures, baseline=%s, quiet=%s)", len(matches), baseline, suppressed)
        for event in result["would_flash"]:
            fire_notify(self.config, event["event"], controller_cls=controller_cls)
            result["flashed"].append(event)
            state["last_event_flashed"] = {**event, "at": now.isoformat()}
            write_json(self.paths.github_state_path, state)
            LOGGER.info("GitHub: workflow run failed - %s / %s on %s",
                        event["repo"], event["workflow"], event["branch"])
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
            "error": state.get("error")}
