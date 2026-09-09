"""`.env` loading and the map from environment variables into config.

Credentials and account-specific ids live here rather than in
`blink-light.json`, because that file is committed and shared. Nothing in this
module ever writes a value back to disk.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

ENV_FILE_NAME = ".env"
ENV_TEMPLATE_NAME = ".env.template"

# Environment variable -> where it lands in the config tree. Explicit rather
# than derived: a typo in a variable name should do nothing, not silently
# create a new config key.
ENV_CONFIG_MAP: dict[str, tuple[str, ...]] = {
    "BLINK_LIGHT_GRAPH_CLIENT_ID": ("calendar", "graph", "client_id"),
    "BLINK_LIGHT_GRAPH_TENANT_ID": ("calendar", "graph", "tenant_id"),
    "BLINK_LIGHT_CALENDAR_PROVIDER": ("calendar", "provider"),
    "BLINK_LIGHT_DEVICE_SERIAL": ("device", "serial"),
}


def parse_env_text(text: str) -> dict[str, str]:
    """Parse KEY=VALUE lines. Comments, blanks and `export ` are tolerated."""
    values: dict[str, str] = {}
    # PowerShell's `Set-Content -Encoding utf8` and Notepad both write a BOM,
    # which would otherwise become part of the first key name and make the
    # whole file look like it was ignored.
    for raw_line in text.lstrip("﻿").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, separator, value = line.partition("=")
        if not separator:
            continue
        key = key.strip()
        value = value.strip()
        # Only strip quotes that actually wrap the value, so a password
        # containing a quote survives.
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def load_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    # utf-8-sig so a BOM is consumed by the decoder rather than the parser.
    return parse_env_text(path.read_text(encoding="utf-8-sig"))


def resolve_env(env_path: Path, environ: dict[str, str] | None = None) -> dict[str, str]:
    """`.env` values, with real environment variables taking precedence.

    That order is what lets a scheduled task or a CI run override the file
    without editing it.
    """
    current = os.environ if environ is None else environ
    resolved = load_env_file(env_path)
    for key in ENV_CONFIG_MAP:
        value = current.get(key)
        if value:
            resolved[key] = value
    return resolved


def apply_env_overrides(config: dict[str, Any], values: dict[str, str]) -> dict[str, Any]:
    """Write mapped values into the config tree. Empty values are ignored."""
    for key, path in ENV_CONFIG_MAP.items():
        value = values.get(key, "").strip()
        if not value:
            continue
        target = config
        for segment in path[:-1]:
            target = target.setdefault(segment, {})
        target[path[-1]] = value
    return config
