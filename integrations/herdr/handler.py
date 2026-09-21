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
* **A screen-scraped state machine can flicker.** A short per-pane cooldown
  stops one pane's transitions from machine-gunning the light.
* **Agents finish in herds.** Kick off ten and they land together, and ten
  identical flashes tell you nothing three did not. A burst - a run of finishes
  with no real gap between them - is capped at three. The third is spent on
  `agent_done_more` when agents are still waiting behind it, so the cap
  announces itself instead of silently eating the rest.

Tunable by dropping a `config.json` in the plugin's config directory
(`herdr plugin config-dir blinklight.agent-status`):

    {"notify_focused": false, "cooldown_seconds": 8, "min_working_seconds": 0,
     "burst_window_seconds": 90, "burst_max_flashes": 3}
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
    "burst_window_seconds": 90.0,
    "burst_max_flashes": 3,
}

# Spent in place of the last finish a burst is allowed, when more agents are
# still waiting behind it. Configured in blink-light.json like any other event.
OVERFLOW_EVENT = "agent_done_more"


def _state_dir() -> Path:
    base = (
        os.environ.get("HERDR_PLUGIN_STATE_DIR")
        or os.environ.get("TEMP")
        or os.environ.get("TMPDIR")
        or "/tmp"
    )
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


def agent_snapshot() -> dict[str, dict]:
    """Ask Herdr for every agent pane it currently knows about, keyed by pane.

    `herdr agent list` already emits JSON on stdout; it has no `--json` flag and
    exits 2 if given one. That mattered more than it looks: the flag was here
    from the start, so every call failed, the focus filter silently passed
    nothing, and the light flashed for the pane you were sitting in front of.
    A non-zero exit is now logged rather than quietly swallowed.

    Only called when a flash is otherwise about to happen, so the subprocess
    cost is paid per notification rather than per status change.
    """
    herdr = os.environ.get("HERDR_BIN_PATH") or "herdr"
    try:
        completed = subprocess.run(
            [herdr, "agent", "list"],
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log(f"warn: could not query agents: {exc}")
        return {}
    if completed.returncode != 0:
        log(f"warn: herdr agent list exited {completed.returncode}: {(completed.stderr or '').strip()[:200]}")
        return {}
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        log("warn: herdr agent list did not return JSON")
        return {}

    agents = payload.get("result", {}).get("agents", [])
    return {
        agent["pane_id"]: agent
        for agent in agents
        if isinstance(agent, dict) and agent.get("pane_id")
    }


def focused_panes(snapshot: dict[str, dict] | None = None) -> set[str]:
    agents = agent_snapshot() if snapshot is None else snapshot
    return {pane for pane, agent in agents.items() if agent.get("focused")}


def queued_behind(pane: str, state: dict, snapshot: dict[str, dict]) -> int:
    """Count agents that have finished but whose flash this burst will not show.

    A pane counts when Herdr says it is finished and we have not flashed for
    that finish. That covers both of the ways a finish goes unshown:

    * Its event has not reached us yet. When ten agents land together Herdr is
      still working through the queue while this handler is already deciding,
      so our record still says `working` - or, for a pane we have never seen
      change state at all, says nothing.
    * We recorded the finish and never announced it, because the burst cap had
      already been spent.

    A pane that finished earlier and *was* announced is not waiting for
    anything, which is why `announced` is tracked rather than just status. It
    is also what keeps a deskful of long-idle agents from making every third
    flash claim there is more behind it.
    """
    waiting = 0
    for other, agent in snapshot.items():
        if other == pane or agent.get("agent_status") not in TERMINAL_STATUSES:
            continue
        entry = state.get(other)
        if not isinstance(entry, dict):
            waiting += 1
        elif entry.get("status") in ACTIVE_STATUSES or not entry.get("announced", True):
            waiting += 1
    return waiting


def _windows_launcher_path() -> str | None:
    """Translate the WSL-mount launcher path into the C:\\... path cmd.exe needs.

    Only called from Linux (see `notify`) - REPO_ROOT lives on the Windows
    filesystem either way (the plugin is linked from there), so `wslpath -w`
    always has something valid to translate.
    """
    try:
        completed = subprocess.run(
            ["wslpath", "-w", str(LAUNCHER)],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log(f"warn: wslpath failed: {exc}")
        return None
    if completed.returncode != 0:
        log(f"warn: wslpath exited {completed.returncode}: {completed.stderr.strip()}")
        return None
    return completed.stdout.strip() or None


def notify(event: str) -> bool:
    if not LAUNCHER.exists():
        log(f"skip: launcher missing at {LAUNCHER}")
        return False

    # blink-light.bat only ever runs on Windows. From a native Linux/WSL herdr
    # install we reach it through interop (cmd.exe), same as native Windows
    # reaches it directly - the launcher itself resolves its own directory via
    # %~dp0, so neither branch needs to worry about cwd.
    if sys.platform.startswith("win"):
        launcher_arg = str(LAUNCHER)
    else:
        launcher_arg = _windows_launcher_path()
        if launcher_arg is None:
            log("skip: could not resolve a Windows path for the launcher")
            return False

    command = ["cmd.exe", "/c", launcher_arg, "notify", "run", event, "--quiet-missing"]
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
    # `announced` starts false for a finish and is set once a flash goes out, so
    # a suppressed finish stays visibly outstanding to `queued_behind`.
    # `fired_at` is carried across, not rebuilt: it is what the per-pane
    # cooldown reads, and dropping it on every status change would leave the
    # cooldown looking at zero forever.
    state[pane] = {
        "status": status,
        "at": now,
        "announced": event is None,
        "fired_at": entry.get("fired_at", 0),
    }

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

    # The cooldown is per-pane. It was global, which read as "do not machine-gun
    # the light" but actually meant one agent finishing could swallow a
    # different agent's alert entirely - with nine panes open, the busiest one
    # muted the rest. Bursts across panes are the burst cap's job, below.
    cooldown = float(settings.get("cooldown_seconds") or 0)
    last_fired = float(entry.get("fired_at") or 0)
    if cooldown and now - last_fired < cooldown:
        write_state(state)
        log(f"skip: {pane} cooldown ({now - last_fired:.1f}s < {cooldown}s)")
        return 0

    snapshot = agent_snapshot()

    if not settings.get("notify_focused"):
        if pane in focused_panes(snapshot):
            write_state(state)
            log(f"skip: {pane} is focused")
            return 0

    # A burst is a run of finishes with no real gap between them. Ten agents
    # landing together say nothing ten flashes say better than three, so the
    # run is capped - and the last flash it is allowed carries the overflow
    # scene when anything is still waiting, so the cap shows rather than hides.
    window = float(settings.get("burst_window_seconds") or 0)
    cap = int(settings.get("burst_max_flashes") or 0)
    burst = state.get("__burst__") if isinstance(state.get("__burst__"), dict) else {}
    counts = burst.get("counts") if isinstance(burst.get("counts"), dict) else {}
    if window and now - float(burst.get("at") or 0) > window:
        counts = {}
    # Counted per event so a stampede of finishes cannot mute a blocked agent,
    # which is the one alert that is actually asking you for something.
    position = int(counts.get(event) or 0) + 1
    counts[event] = position
    state["__burst__"] = {"at": now, "counts": counts}

    if cap and position > cap:
        write_state(state)
        log(f"skip: {pane} burst cap ({event} #{position} > {cap})")
        return 0

    fired_event = event
    if cap and position == cap and event == "agent_done":
        waiting = queued_behind(pane, state, snapshot)
        if waiting:
            fired_event = OVERFLOW_EVENT
            log(f"overflow: {waiting} more agent(s) waiting behind {pane}")

    if notify(fired_event):
        state[pane]["announced"] = True
        state[pane]["fired_at"] = now
        log(f"fired: {fired_event} for {pane} ({previous} -> {status})")
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
