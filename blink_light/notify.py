"""One-shot notifications for external tools (Herdr, CI, git hooks, anything).

Deliberately the opposite of the chime and show: **no slots and no dedupe.**
Those exist to fire once per scheduled moment. A notification fires when
something happened, and if it happens three times you want three flashes.

The named-event indirection matters for the caller. An integration hard-coding
`light flash "#00E5FF" --count 2` pins the appearance at the call site, so
restyling means editing the integration. Naming `agent_done` instead leaves
appearance in `blink-light.json` where it belongs.
"""

from __future__ import annotations

from typing import Any

from .device import BlinkDeviceController


class NotifyError(ValueError):
    """Raised for unknown or malformed notification events."""


def notify_events(config: dict[str, Any]) -> dict[str, Any]:
    return config.get("notify", {})


def list_events(config: dict[str, Any]) -> list[str]:
    return sorted(notify_events(config).keys())


def resolve_event(config: dict[str, Any], event: str) -> dict[str, Any]:
    """Look up an event's action, following a preset reference if present."""
    events = notify_events(config)
    if event not in events:
        known = ", ".join(list_events(config)) or "none configured"
        raise NotifyError(f"Unknown notify event '{event}'. Known events: {known}.")

    action = events[event]
    if "preset" in action:
        presets = config["presets"]
        preset_name = action["preset"]
        if preset_name not in presets:
            raise NotifyError(f"notify.{event}.preset refers to unknown preset '{preset_name}'.")
        return dict(presets[preset_name])
    return dict(action)


def fire_notify(
    config: dict[str, Any],
    event: str,
    controller_cls=BlinkDeviceController,
    controller=None,
) -> dict[str, Any]:
    """Play an event's action once and return.

    Always non-persistent: a notification is an interruption, not a new resting
    state, so the watcher repaints the underlying state on its next tick.
    """
    action = resolve_event(config, event)

    owned = controller is None
    device = controller or controller_cls(serial=config["device"].get("serial"))
    try:
        device.apply_action(action, config["scenes"], persistent=False)
    finally:
        if owned:
            device.close()

    return {"notified": True, "event": event, "action": action}
