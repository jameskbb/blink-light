from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_NAME = "blink-light.json"
STARTUP_SCRIPT_NAME = "BlinkLight Watcher.cmd"


@dataclass(frozen=True)
class AppPaths:
    project_root: Path
    config_path: Path
    runtime_dir: Path
    calendar_state_path: Path
    github_state_path: Path
    graph_token_path: Path
    chime_state_path: Path
    chime_pid_path: Path
    chime_stop_path: Path
    show_state_path: Path
    alarm_state_path: Path
    override_path: Path
    timer_path: Path
    watcher_pid_path: Path
    watcher_state_path: Path
    watcher_stop_path: Path
    log_path: Path
    startup_dir: Path
    startup_script_path: Path


def _local_appdata() -> Path:
    raw = os.environ.get("LOCALAPPDATA")
    if raw:
        return Path(raw)
    return Path.home() / "AppData" / "Local"


def _appdata() -> Path:
    raw = os.environ.get("APPDATA")
    if raw:
        return Path(raw)
    return Path.home() / "AppData" / "Roaming"


def build_paths(
    config_path: Path | None = None,
    project_root: Path | None = None,
    runtime_dir: Path | None = None,
    startup_dir: Path | None = None,
) -> AppPaths:
    root = (project_root or PROJECT_ROOT).resolve()
    resolved_config = (config_path or (root / DEFAULT_CONFIG_NAME)).resolve()
    runtime = (runtime_dir or (_local_appdata() / "BlinkLight")).resolve()
    startup = (
        startup_dir
        or (_appdata() / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup")
    ).resolve()
    return AppPaths(
        project_root=root,
        config_path=resolved_config,
        runtime_dir=runtime,
        calendar_state_path=runtime / "calendar-state.json",
        github_state_path=runtime / "github-state.json",
        # Refresh tokens live here. Outside the repo on purpose - the runtime
        # dir is not a git worktree, so this cannot be committed by accident.
        graph_token_path=runtime / "graph-token-cache.json",
        chime_state_path=runtime / "chime-state.json",
        chime_pid_path=runtime / "chime.pid",
        chime_stop_path=runtime / "chime.stop",
        show_state_path=runtime / "show-state.json",
        alarm_state_path=runtime / "alarm-state.json",
        override_path=runtime / "override.json",
        timer_path=runtime / "timer.json",
        watcher_pid_path=runtime / "watcher.pid",
        watcher_state_path=runtime / "watcher-state.json",
        watcher_stop_path=runtime / "watcher.stop",
        log_path=runtime / "blink-light.log",
        startup_dir=startup,
        startup_script_path=startup / STARTUP_SCRIPT_NAME,
    )


def ensure_runtime_dirs(paths: AppPaths) -> None:
    paths.runtime_dir.mkdir(parents=True, exist_ok=True)
