from __future__ import annotations

from .paths import AppPaths
from .state import remove_file, write_text


def startup_status(paths: AppPaths) -> dict:
    return {
        "enabled": paths.startup_script_path.exists(),
        "path": str(paths.startup_script_path),
    }


def enable_startup(paths: AppPaths) -> dict:
    paths.startup_dir.mkdir(parents=True, exist_ok=True)
    launcher = paths.project_root / "blink-light.bat"
    # The exit code is passed on rather than swallowed, so anything that runs
    # this script other than the Startup folder - a task, a test, a shell - can
    # tell a watcher that came up from one that did not.
    content = (
        "@echo off\r\n"
        f'cd /d "{paths.project_root}"\r\n'
        f'call "{launcher}" watch start\r\n'
        "exit /b %errorlevel%\r\n"
    )
    write_text(paths.startup_script_path, content)
    return startup_status(paths)


def disable_startup(paths: AppPaths) -> dict:
    remove_file(paths.startup_script_path)
    return startup_status(paths)
