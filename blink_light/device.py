from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import json
import threading
import time
from typing import Any


class DeviceError(RuntimeError):
    """Raised for device selection and communication failures."""


@dataclass(frozen=True)
class DeviceStatus:
    selected_serial: str | None
    available_serials: list[str]
    version: str | None


class BlinkDeviceController:
    def __init__(self, serial: str | None = None):
        self.serial = serial
        self._device = None
        self._lock = threading.RLock()
        self._scene_thread: threading.Thread | None = None
        self._scene_stop = threading.Event()
        self._current_signature: str | None = None

    @staticmethod
    def _blink1_class():
        try:
            from blink1.blink1 import Blink1
        except ImportError as exc:  # pragma: no cover - exercised only on missing dependency
            raise DeviceError("The 'blink1' package is not installed.") from exc
        return Blink1

    @classmethod
    def list_devices(cls) -> list[str]:
        blink1_cls = cls._blink1_class()
        return list(blink1_cls.list())

    @classmethod
    def select_serial(cls, configured_serial: str | None = None) -> str:
        serials = cls.list_devices()
        if configured_serial:
            if configured_serial not in serials:
                raise DeviceError(f"Configured blink(1) serial '{configured_serial}' is not connected.")
            return configured_serial
        if not serials:
            raise DeviceError("No blink(1) devices are connected.")
        if len(serials) > 1:
            raise DeviceError("Multiple blink(1) devices are connected. Set device.serial in blink-light.json.")
        return serials[0]

    def _ensure_device(self):
        with self._lock:
            if self._device is None:
                serial = self.select_serial(self.serial)
                blink1_cls = self._blink1_class()
                self._device = blink1_cls(serial)
                self.serial = serial
            return self._device

    def status(self) -> DeviceStatus:
        serials = self.list_devices()
        selected = None
        version = None
        try:
            device = self._ensure_device()
            selected = self.serial
            version = device.get_version()
        except DeviceError:
            selected = self.serial if self.serial in serials else None
        return DeviceStatus(selected_serial=selected, available_serials=serials, version=version)

    def close(self) -> None:
        self.stop_scene()
        with self._lock:
            if self._device is not None:
                try:
                    self._device.close()
                finally:
                    self._device = None

    def enable_watchdog(self, timeout_millis: int) -> None:
        with self._lock:
            device = self._ensure_device()
            device.server_tickle(True, int(timeout_millis), stay_lit=False)

    def disable_watchdog(self) -> None:
        with self._lock:
            if self._device is not None:
                self._device.server_tickle(False, 0, stay_lit=False)

    def stop_scene(self) -> None:
        thread = self._scene_thread
        if thread and thread.is_alive():
            self._scene_stop.set()
            thread.join(timeout=2.0)
        self._scene_thread = None
        self._scene_stop = threading.Event()
        with self._lock:
            if self._device is not None:
                self._device.stop()

    def off(self) -> None:
        self.stop_scene()
        with self._lock:
            self._ensure_device().off()
        self._current_signature = json.dumps({"off": True}, sort_keys=True)

    def solid(self, color: str, fade_ms: int = 0, led: int = 0) -> None:
        self.stop_scene()
        with self._lock:
            self._ensure_device().fade_to_color(int(fade_ms), color, ledn=led)
        self._current_signature = json.dumps({"color": color, "fade_ms": fade_ms, "led": led}, sort_keys=True)

    def flash(
        self,
        color: str,
        secondary_color: str = "#000000",
        on_ms: int = 250,
        off_ms: int = 250,
        count: int = 3,
    ) -> None:
        scene = {
            "loop": count == 0,
            "repeat": max(1, count),
            "steps": [
                {"color": color, "seconds": max(on_ms, 1) / 1000.0},
                {"color": secondary_color, "seconds": max(off_ms, 1) / 1000.0},
            ],
        }
        self.play_scene(scene, persistent=False)

    def pulse(
        self,
        color: str,
        secondary_color: str = "#000000",
        on_ms: int = 350,
        off_ms: int = 900,
        count: int = 6,
    ) -> None:
        scene = {
            "loop": count == 0,
            "repeat": max(1, count),
            "steps": [
                {"color": color, "seconds": max(on_ms, 1) / 1000.0},
                {"color": secondary_color, "seconds": max(off_ms, 1) / 1000.0},
            ],
        }
        self.play_scene(scene, persistent=False)

    @staticmethod
    def scene_signature(scene: dict[str, Any]) -> str:
        return json.dumps(scene, sort_keys=True)

    def can_run_scene_on_device(self, scene: dict[str, Any]) -> bool:
        return not scene.get("loop", False) and len(scene.get("steps", [])) <= 32

    def _load_scene_to_device(self, scene: dict[str, Any]) -> float:
        with self._lock:
            device = self._ensure_device()
            steps = scene["steps"]
            total_seconds = 0.0
            for index, step in enumerate(steps):
                millis = int(float(step["seconds"]) * 1000)
                total_seconds += float(step["seconds"])
                device.write_pattern_line(millis, step["color"], index, step.get("led", 0))
            for index in range(len(steps), 32):
                device.write_pattern_line(0, "#000000", index, 0)
            device.play(count=int(scene.get("repeat", 1)))
            return total_seconds * int(scene.get("repeat", 1))

    def _play_scene_host(self, scene: dict[str, Any], stop_event: threading.Event) -> None:
        loop = bool(scene.get("loop", False))
        repeat = int(scene.get("repeat", 1))
        iteration = 0
        with self._lock:
            self._ensure_device().stop()
        while not stop_event.is_set() and (loop or iteration < repeat):
            for step in scene["steps"]:
                if stop_event.is_set():
                    return
                duration_seconds = float(step["seconds"])
                with self._lock:
                    self._ensure_device().fade_to_color(
                        int(duration_seconds * 1000),
                        step["color"],
                        ledn=int(step.get("led", 0)),
                    )
                if stop_event.wait(duration_seconds):
                    return
            iteration += 1

    def play_scene(self, scene: dict[str, Any], persistent: bool) -> None:
        scene_copy = deepcopy(scene)
        signature = self.scene_signature(scene_copy)
        if persistent and signature == self._current_signature:
            return

        self.stop_scene()
        if self.can_run_scene_on_device(scene_copy):
            total_seconds = self._load_scene_to_device(scene_copy)
            self._current_signature = signature
            if not persistent:
                time.sleep(total_seconds)
            return

        if persistent:
            self._scene_stop = threading.Event()
            self._scene_thread = threading.Thread(
                target=self._play_scene_host,
                args=(scene_copy, self._scene_stop),
                daemon=True,
            )
            self._scene_thread.start()
            self._current_signature = signature
            return

        self._play_scene_host(scene_copy, threading.Event())
        self._current_signature = signature

    def apply_action(self, action: dict[str, Any], scenes: dict[str, dict[str, Any]], persistent: bool) -> None:
        if action.get("off"):
            self.off()
            return
        if "flash" in action:
            payload = action["flash"]
            self.flash(
                color=payload["color"],
                secondary_color=payload.get("secondary_color", "#000000"),
                on_ms=int(payload.get("on_ms", 250)),
                off_ms=int(payload.get("off_ms", 250)),
                count=int(payload.get("count", 1)),
            )
            return
        if "pulse" in action:
            payload = action["pulse"]
            self.pulse(
                color=payload["color"],
                secondary_color=payload.get("secondary_color", "#000000"),
                on_ms=int(payload.get("on_ms", 350)),
                off_ms=int(payload.get("off_ms", 900)),
                count=int(payload.get("count", 1)),
            )
            return
        if "color" in action:
            self.solid(action["color"], int(action.get("fade_ms", 0)))
            return
        if "scene" in action:
            if action["scene"] not in scenes:
                raise DeviceError(f"Unknown scene '{action['scene']}'.")
            self.play_scene(scenes[action["scene"]], persistent=persistent)
            return
        raise DeviceError(f"Unsupported action: {action}")
