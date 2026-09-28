"""Filesystem locations and user settings.

Every path fleet writes to is resolved here, so the environment variables below
are the complete list of knobs for relocating state during testing.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict

from fleet.errors import FleetError

DEFAULT_STATE_DIR = "~/.local/state/fleet"
DEFAULT_CONFIG_DIR = "~/.config/fleet"


def _expand(path: str) -> str:
    return os.path.abspath(os.path.expanduser(path))


def state_dir() -> str:
    return _expand(os.environ.get("FLEET_STATE_DIR") or DEFAULT_STATE_DIR)


def config_dir() -> str:
    return _expand(os.environ.get("FLEET_CONFIG_DIR") or DEFAULT_CONFIG_DIR)


def registry_path() -> str:
    return os.path.join(state_dir(), "registry.json")


def lock_path() -> str:
    return os.path.join(state_dir(), "registry.lock")


def config_path() -> str:
    return os.path.join(config_dir(), "config.json")


def install_record_path() -> str:
    """Where fleet remembers what it wrote, so upgrades do not clobber edits."""
    return os.path.join(config_dir(), "installed.json")


def opencode_config_dir() -> str:
    override = os.environ.get("FLEET_OPENCODE_CONFIG_DIR")
    if override:
        return _expand(override)
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = _expand(xdg) if xdg else os.path.expanduser("~/.config")
    return os.path.join(base, "opencode")


DEFAULTS: Dict[str, Any] = {
    # Which OpenCode agent profile new sessions get.
    "default_agent": "build",
    # Which agent binary pane mode launches.
    "default_kind": "opencode",
    # Optional model override, as provider/model#variant.
    "default_model": None,
    # Where new agents work unless --dir says otherwise.
    "default_directory": None,
}


def load_config() -> Dict[str, Any]:
    """User settings layered over the defaults.

    A missing or unreadable config file is not an error. fleet should still run
    on a fresh machine with no configuration at all.
    """
    merged = dict(DEFAULTS)
    path = config_path()
    try:
        with open(path) as handle:
            stored = json.load(handle)
    except FileNotFoundError:
        return merged
    except json.JSONDecodeError as exc:
        raise FleetError(f"{path} is not valid JSON: {exc}") from exc
    except OSError as exc:
        raise FleetError(f"could not read {path}: {exc}") from exc

    if not isinstance(stored, dict):
        raise FleetError(f"{path} must contain a JSON object")
    unknown = set(stored) - set(DEFAULTS)
    if unknown:
        raise FleetError(f"{path} has unknown keys: {', '.join(sorted(unknown))}")
    merged.update(stored)
    return merged


def write_config(values: Dict[str, Any]) -> str:
    """Merge values into the config file, leaving other keys alone."""
    path = config_path()
    current: Dict[str, Any] = {}
    try:
        with open(path) as handle:
            current = json.load(handle)
    except (FileNotFoundError, json.JSONDecodeError):
        current = {}
    if not isinstance(current, dict):
        current = {}

    unknown = set(values) - set(DEFAULTS)
    if unknown:
        raise FleetError(f"unknown config keys: {', '.join(sorted(unknown))}")

    current.update(values)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w") as handle:
        json.dump(current, handle, indent=2, sort_keys=True)
        handle.write("\n")
    os.replace(tmp, path)
    return path


def target_path(kind: str, name: str) -> str:
    """Where a bundled template belongs inside the OpenCode config.

    `kind` is "agents" or "commands". Kept here so install and uninstall cannot
    disagree about the destination.
    """
    if kind not in ("agents", "commands"):
        raise FleetError(f"unknown template kind {kind!r}")
    return os.path.join(opencode_config_dir(), kind, name)
