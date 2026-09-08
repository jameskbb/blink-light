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
    content = (
        "@echo off\r\n"
        f'cd /d "{paths.project_root}"\r\n'
        f'call "{launcher}" watch start\r\n'
    )
    write_text(paths.startup_script_path, content)
    return startup_status(paths)


def disable_startup(paths: AppPaths) -> dict:
    remove_file(paths.startup_script_path)
    return startup_status(paths)
