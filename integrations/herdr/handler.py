"""Turn a Herdr pane.agent_status_changed event into a blink-light notification.

Herdr's published plugin docs specify the manifest and the injected environment
variables, but not the JSON payload shape for this particular event, and the
binary is closed-source. So this handler does two things deliberately:

* It reads the status **tolerantly** - any of several plausible key names, at the
  top level or one level down - rather than betting on one schema.
* It appends every payload it sees to a log, so the real shape can be confirmed
  from a single real event instead of guessed at.

It is also silent by construction. A notifier that fails loudly in the middle of
someone's terminal is worse than one that quietly does nothing, so every failure
path here logs and exits 0.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
LAUNCHER = REPO_ROOT / "blink-light.bat"

# Which agent states are worth interrupting someone for. Herdr's own states are
# idle / working / blocked / done; "working" and "unknown" are noise here.
EVENT_FOR_STATUS = {
    "done": "agent_done",
    "idle": "agent_done",
    "blocked": "agent_blocked",
}

# Keys that might carry the status, in preference order. `agent_status` is the
# confirmed one - it is what `herdr agent list` returns for each pane, with the
# values "idle" / "working" / "blocked". The rest are fallbacks in case the
# event payload names it differently from the query response.
STATUS_KEYS = ("agent_status", "status", "state", "agent_state", "new_status", "to")


def log_path() -> Path:
    base = os.environ.get("HERDR_PLUGIN_STATE_DIR") or os.environ.get("TEMP") or "."
    return Path(base) / "blink-light-herdr.log"


def log(message: str) -> None:
    try:
        with log_path().open("a", encoding="utf-8") as handle:
            handle.write(f"{datetime.now().isoformat(timespec='seconds')} {message}\n")
    except OSError:
        pass


KNOWN_STATUSES = ("done", "idle", "blocked", "working", "unknown")


def _coerce_status(value: object) -> str | None:
    """Pull a state name out of a string or an enum wrapper."""
    if isinstance(value, str):
        return value.strip().lower()
    if not isinstance(value, dict):
        return None

    # Tagged form first - {"type": "done"} would otherwise be misread as the
    # externally-tagged form below and yield "type".
    for key in ("type", "kind", "name", "value", "status"):
        nested = value.get(key)
        if isinstance(nested, str):
            return nested.strip().lower()

    # Externally-tagged enum: {"Done": {...}}. Only trust it when the single
    # key is actually a state name, so an unrelated wrapper is not mistaken
    # for one.
    if len(value) == 1:
        only_key = next(iter(value))
        if isinstance(only_key, str) and only_key.strip().lower() in KNOWN_STATUSES:
            return only_key.strip().lower()
    return None


def find_status(payload: object, depth: int = 0) -> str | None:
    """Search a payload for a recognised agent state.

    Bounded to two levels: enough for `{"status": ...}` and
    `{"pane": {"status": ...}}`, shallow enough that it cannot wander into an
    unrelated `status` field deep in some nested object.
    """
    if not isinstance(payload, dict) or depth > 2:
        return None

    for key in STATUS_KEYS:
        if key in payload:
            status = _coerce_status(payload[key])
            if status in KNOWN_STATUSES:
                return status

    for value in payload.values():
        if isinstance(value, dict):
            found = find_status(value, depth + 1)
            if found:
                return found
    return None


def notify(event: str) -> None:
    if not LAUNCHER.exists():
        log(f"skip: launcher missing at {LAUNCHER}")
        return
    # --quiet-missing so a config without this event is a no-op, not a failure.
    command = ["cmd.exe", "/c", str(LAUNCHER), "notify", "run", event, "--quiet-missing"]
    try:
        completed = subprocess.run(
            command,
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=45,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log(f"error: {event} failed to launch: {exc}")
        return
    if completed.returncode == 0:
        log(f"fired: {event}")
    else:
        log(f"error: {event} exited {completed.returncode}: {(completed.stderr or '').strip()[:300]}")


def main() -> int:
    raw = os.environ.get("HERDR_PLUGIN_EVENT_JSON", "")
    name = os.environ.get("HERDR_PLUGIN_EVENT", "<unset>")

    if not raw:
        log(f"skip: no HERDR_PLUGIN_EVENT_JSON (event={name})")
        return 0

    # Recorded verbatim so the payload shape can be confirmed from a real event.
    log(f"payload event={name} json={raw[:1200]}")

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        log(f"skip: payload is not JSON: {exc}")
        return 0

    status = find_status(payload)
    if status is None:
        log("skip: no recognisable status in payload")
        return 0

    event = EVENT_FOR_STATUS.get(status)
    if event is None:
        log(f"skip: status '{status}' is not notify-worthy")
        return 0

    notify(event)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as exc:  # never let a notifier take down the caller
        log(f"error: unhandled {type(exc).__name__}: {exc}")
        raise SystemExit(0)
