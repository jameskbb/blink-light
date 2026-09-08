from __future__ import annotations

from dataclasses import dataclass
import ctypes
from ctypes import wintypes
from pathlib import Path
from datetime import datetime


try:
    import psutil  # type: ignore
except ImportError:  # pragma: no cover - exercised on machines without psutil
    psutil = None


@dataclass(frozen=True)
class SystemSnapshot:
    idle_seconds: float | None
    process_names: set[str]
    battery_percent: float | None
    charging: bool | None


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


def get_idle_seconds() -> float | None:
    try:
        last_input = LASTINPUTINFO()
        last_input.cbSize = ctypes.sizeof(LASTINPUTINFO)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(last_input)):
            return None
        elapsed_millis = ctypes.windll.kernel32.GetTickCount() - last_input.dwTime
        return float(elapsed_millis) / 1000.0
    except Exception:
        return None


def _collect_process_names() -> set[str]:
    if psutil is None:
        return set()
    names: set[str] = set()
    for process in psutil.process_iter(["name", "exe"]):
        try:
            for candidate in (process.info.get("name"), process.info.get("exe")):
                if candidate:
                    names.add(Path(candidate).name.lower())
        except (psutil.Error, OSError):
            continue
    return names


def _collect_battery() -> tuple[float | None, bool | None]:
    if psutil is None:
        return None, None
    try:
        battery = psutil.sensors_battery()
    except Exception:
        return None, None
    if battery is None:
        return None, None
    return float(battery.percent), bool(battery.power_plugged)


def collect_system_snapshot(now: datetime | None = None) -> SystemSnapshot:
    del now
    battery_percent, charging = _collect_battery()
    return SystemSnapshot(
        idle_seconds=get_idle_seconds(),
        process_names=_collect_process_names(),
        battery_percent=battery_percent,
        charging=charging,
    )
