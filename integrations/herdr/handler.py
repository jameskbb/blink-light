"""Turn a Herdr pane.agent_status_changed event into a blink-light notification.

The confirmed payload, captured from a real event:

    {"event": "pane_agent_status_changed",
     "data": {"type": "pane_agent_status_changed", "pane_id": "w7:p1",
              "workspace_id": "w7", "agent_status": "idle", "agent": "claude"}}

Note what is *absent*: no previous status, and no focus flag. Both matter, and
their absence is why a naive handler flashes far too often:

* **Terminal states repeat.** Herdr has two finished-ish states, `idle` and
  `done`, and a single agent turn can emit both. Firing on each gives two
  flashes for one finish. So this handler tracks the previous status per pane and
  only fires on a real transition *into* a terminal state.
* **The focused pane needs no alert.** You are looking at it. Herdr's own sound
  only plays for background workspaces; this matches that by default.
* **A screen-scraped state machine can flicker.** A short cooldown stops a burst
  of transitions from machine-gunning the light.

Tunable by dropping a `config.json` in the plugin's config directory
(`herdr plugin config-dir blinklight.agent-status`):

    {"notify_focused": false, "cooldown_seconds": 8, "min_working_seconds": 0}
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
LAUNCHER = REPO_ROOT / "blink-light.bat"

# Which agent states count as "the agent stopped and wants you".
TERMINAL_STATUSES = {
    "done": "agent_done",
    "idle": "agent_done",
    "blocked": "agent_blocked",
}

# States that mean the agent is busy. A terminal status only counts as a finish
# if the pane was in one of these first.
ACTIVE_STATUSES = {"working"}

KNOWN_STATUSES = ("done", "idle", "blocked", "working", "unknown")

# `agent_status` is confirmed from both the event payload and `herdr agent list`.
# The rest are fallbacks in case the shape changes.
STATUS_KEYS = ("agent_status", "status", "state", "agent_state", "new_status", "to")
PANE_KEYS = ("pane_id", "paneId", "pane")

DEFAULTS = {
    "notify_focused": False,
    "cooldown_seconds": 8.0,
    "min_working_seconds": 0.0,
}


def _state_dir() -> Path:
    base = os.environ.get("HERDR_PLUGIN_STATE_DIR") or os.environ.get("TEMP") or "."
    return Path(base)


def log_path() -> Path:
    return _state_dir() / "blink-light-herdr.log"


def state_path() -> Path:
    return _state_dir() / "pane-state.json"


LOG_MAX_BYTES = 1024 * 1024


def _rotate_log(path: Path) -> None:
    """Keep one old log beside the live one.

    Every agent status change writes a line and nothing else ever trims the
    file. A rename refused because another handler has the file open just
    leaves it for the next call - it must not cost this call its own line.
    """
    try:
        if path.stat().st_size >= LOG_MAX_BYTES:
            os.replace(path, path.with_name(path.name + ".1"))
    except OSError:
        pass


def log(message: str) -> None:
    try:
        path = log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        _rotate_log(path)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"{datetime.now().isoformat(timespec='seconds')} {message}\n")
    except OSError:
        pass


def load_settings() -> dict:
    settings = dict(DEFAULTS)
    config_dir = os.environ.get("HERDR_PLUGIN_CONFIG_DIR")
    if not config_dir:
        return settings
    path = Path(config_dir) / "config.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return settings
    if isinstance(payload, dict):
        for key in DEFAULTS:
            if key in payload:
                settings[key] = payload[key]
    return settings


def read_state() -> dict:
    try:
        payload = json.loads(state_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def write_state(state: dict) -> None:
    """Write the pane state atomically.

    Panes fire concurrently, so two handlers can race here. A temp file plus
    replace means a loser overwrites rather than corrupts, which for this data
    is an acceptable trade - worst case is one extra or one missed flash.
    """
    path = state_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(f".{os.getpid()}.tmp")
        temp.write_text(json.dumps(state), encoding="utf-8")
        os.replace(temp, path)
    except OSError as exc:
        log(f"warn: could not persist pane state: {exc}")


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

    # Externally-tagged enum: {"Done": {...}}. Only trust it when the single key
    # is actually a state name.
    if len(value) == 1:
        only_key = next(iter(value))
        if isinstance(only_key, str) and only_key.strip().lower() in KNOWN_STATUSES:
            return only_key.strip().lower()
    return None


def _find(payload: object, keys: tuple[str, ...], validate=None, depth: int = 0):
    """Search a payload up to two levels down for one of ``keys``.

    Two levels covers the real shape - the interesting fields live under
    `data` - while staying shallow enough not to wander into an unrelated
    field deep in some nested object.
    """
    if not isinstance(payload, dict) or depth > 2:
        return None

    for key in keys:
        if key in payload:
            value = payload[key]
            resolved = validate(value) if validate else (value if isinstance(value, str) else None)
            if resolved is not None:
                return resolved

    for value in payload.values():
        if isinstance(value, dict):
            found = _find(value, keys, validate, depth + 1)
            if found is not None:
                return found
    return None


def find_status(payload: object, depth: int = 0) -> str | None:
    def validate(value):
        status = _coerce_status(value)
        return status if status in KNOWN_STATUSES else None

    return _find(payload, STATUS_KEYS, validate, depth)


def find_pane(payload: object) -> str | None:
    return _find(payload, PANE_KEYS)


def focused_panes() -> set[str]:
    """Ask Herdr which panes are focused.

    Only called when a flash is otherwise about to happen, so the subprocess
    cost is paid per notification rather than per status change.
    """
    herdr = os.environ.get("HERDR_BIN_PATH") or "herdr"
    try:
        completed = subprocess.run(
            [herdr, "agent", "list", "--json"],
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log(f"warn: could not query focus: {exc}")
        return set()
    if completed.returncode != 0:
        return set()
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return set()

    agents = payload.get("result", {}).get("agents", [])
    return {
        agent.get("pane_id")
        for agent in agents
        if isinstance(agent, dict) and agent.get("focused") and agent.get("pane_id")
    }


def notify(event: str) -> bool:
    if not LAUNCHER.exists():
        log(f"skip: launcher missing at {LAUNCHER}")
        return False
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
        return False
    if completed.returncode == 0:
        return True
    log(f"error: {event} exited {completed.returncode}: {(completed.stderr or '').strip()[:300]}")
    return False


def main() -> int:
    raw = os.environ.get("HERDR_PLUGIN_EVENT_JSON", "")
    name = os.environ.get("HERDR_PLUGIN_EVENT", "<unset>")

    if not raw:
        log(f"skip: no HERDR_PLUGIN_EVENT_JSON (event={name})")
        return 0

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        log(f"skip: payload is not JSON: {exc} raw={raw[:400]}")
        return 0

    status = find_status(payload)
    pane = find_pane(payload) or "<unknown-pane>"
    if status is None:
        # Logged verbatim so a shape change can be fixed in one edit.
        log(f"skip: no recognisable status in payload json={raw[:800]}")
        return 0

    settings = load_settings()
    state = read_state()
    entry = state.get(pane) if isinstance(state.get(pane), dict) else {}
    previous = entry.get("status")
    now = time.time()

    event = TERMINAL_STATUSES.get(status)

    # Record the new status before any early return, so the next event sees it.
    state[pane] = {"status": status, "at": now}

    if event is None:
        write_state(state)
        log(f"skip: {pane} -> {status} (not notify-worthy)")
        return 0

    # The core fix: only a transition *out of* an active state is a finish.
    # Without this, idle and then done for the same turn flash twice.
    if previous is not None and previous not in ACTIVE_STATUSES:
        write_state(state)
        log(f"skip: {pane} {previous} -> {status} (not a finish)")
        return 0

    min_working = float(settings.get("min_working_seconds") or 0)
    if min_working and previous in ACTIVE_STATUSES:
        worked_for = now - float(entry.get("at") or 0)
        if worked_for < min_working:
            write_state(state)
            log(f"skip: {pane} worked only {worked_for:.1f}s (< {min_working}s)")
            return 0

    if not settings.get("notify_focused"):
        if pane in focused_panes():
            write_state(state)
            log(f"skip: {pane} is focused")
            return 0

    cooldown = float(settings.get("cooldown_seconds") or 0)
    last_fired = float(state.get("__last_fired__") or 0)
    if cooldown and now - last_fired < cooldown:
        write_state(state)
        log(f"skip: cooldown ({now - last_fired:.1f}s < {cooldown}s)")
        return 0

    if notify(event):
        state["__last_fired__"] = now
        log(f"fired: {event} for {pane} ({previous} -> {status})")
    write_state(state)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as exc:  # never let a notifier take down the caller
        log(f"error: unhandled {type(exc).__name__}: {exc}")
        raise SystemExit(0)
