from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .state import read_json, remove_file, write_json


def _now() -> datetime:
    return datetime.now().astimezone()


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value)


@dataclass
class TimerSnapshot:
    exists: bool
    name: str | None
    timer_active: bool
    paused: bool
    completed: bool
    phase_name: str | None
    remaining_seconds: int | None
    action: dict[str, Any] | None
    completion_action: dict[str, Any] | None
    state: dict[str, Any] | None


def load_timer_state(path: Path) -> dict[str, Any] | None:
    payload = read_json(path, None)
    if not payload:
        return None
    return payload


def save_timer_state(path: Path, state: dict[str, Any]) -> None:
    write_json(path, state)


def clear_timer_state(path: Path) -> None:
    remove_file(path)


def routine_state(name: str, routine: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    current = now or _now()
    phases = []
    for phase in routine["phases"]:
        duration_seconds = phase.get("seconds")
        if duration_seconds is None:
            duration_seconds = float(phase["minutes"]) * 60.0
        phases.append(
            {
                "name": phase["name"],
                "duration_seconds": int(duration_seconds),
                "action": phase["action"],
            }
        )
    first_end = current + timedelta(seconds=phases[0]["duration_seconds"])
    return {
        "name": name,
        "kind": "routine",
        "paused": False,
        "phase_index": 0,
        "phase_started_at": _iso(current),
        "phase_ends_at": _iso(first_end),
        "remaining_seconds": None,
        "completed_at": None,
        "phases": phases,
        "completion_action": routine.get("completion_action"),
    }


def countdown_state(
    name: str,
    duration_seconds: int,
    active_action: dict[str, Any],
    completion_action: dict[str, Any],
    now: datetime | None = None,
) -> dict[str, Any]:
    current = now or _now()
    return {
        "name": name,
        "kind": "countdown",
        "paused": False,
        "phase_index": 0,
        "phase_started_at": _iso(current),
        "phase_ends_at": _iso(current + timedelta(seconds=duration_seconds)),
        "remaining_seconds": None,
        "completed_at": None,
        "phases": [
            {
                "name": name,
                "duration_seconds": int(duration_seconds),
                "action": active_action,
            }
        ],
        "completion_action": completion_action,
    }


def refresh_timer_state(path: Path, now: datetime | None = None) -> TimerSnapshot:
    current = now or _now()
    state = load_timer_state(path)
    if not state:
        return TimerSnapshot(False, None, False, False, False, None, None, None, None, None)

    changed = False
    if not state.get("paused") and not state.get("completed_at"):
        while True:
            end_time = _parse(state.get("phase_ends_at"))
            if end_time is None or current < end_time:
                break
            next_index = state["phase_index"] + 1
            if next_index >= len(state["phases"]):
                state["phase_index"] = len(state["phases"]) - 1
                state["phase_started_at"] = None
                state["phase_ends_at"] = None
                state["completed_at"] = _iso(current)
                changed = True
                break
            state["phase_index"] = next_index
            next_phase = state["phases"][next_index]
            state["phase_started_at"] = _iso(end_time)
            state["phase_ends_at"] = _iso(end_time + timedelta(seconds=next_phase["duration_seconds"]))
            changed = True

    if changed:
        save_timer_state(path, state)

    phase = state["phases"][state["phase_index"]]
    completed = bool(state.get("completed_at"))
    paused = bool(state.get("paused"))
    active = not paused and not completed
    if active:
        remaining = max(0, int((_parse(state["phase_ends_at"]) - current).total_seconds()))
    elif paused:
        remaining = int(state.get("remaining_seconds") or 0)
    else:
        remaining = 0

    phase_name = "completed" if completed else phase["name"]
    action = phase["action"] if active else None
    return TimerSnapshot(
        exists=True,
        name=state.get("name"),
        timer_active=active,
        paused=paused,
        completed=completed,
        phase_name=phase_name,
        remaining_seconds=remaining,
        action=action,
        completion_action=state.get("completion_action"),
        state=state,
    )


def pause_timer(path: Path, now: datetime | None = None) -> TimerSnapshot:
    current = now or _now()
    snapshot = refresh_timer_state(path, current)
    if not snapshot.exists or not snapshot.timer_active or not snapshot.state:
        return snapshot
    state = snapshot.state
    end_time = _parse(state["phase_ends_at"])
    state["paused"] = True
    state["remaining_seconds"] = max(0, int((end_time - current).total_seconds()))
    save_timer_state(path, state)
    return refresh_timer_state(path, current)


def resume_timer(path: Path, now: datetime | None = None) -> TimerSnapshot:
    current = now or _now()
    snapshot = refresh_timer_state(path, current)
    if not snapshot.exists or not snapshot.paused or not snapshot.state:
        return snapshot
    state = snapshot.state
    remaining = int(state.get("remaining_seconds") or 0)
    state["paused"] = False
    state["phase_started_at"] = _iso(current)
    state["phase_ends_at"] = _iso(current + timedelta(seconds=remaining))
    state["remaining_seconds"] = None
    save_timer_state(path, state)
    return refresh_timer_state(path, current)
