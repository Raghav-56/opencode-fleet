"""Herdr integration, used only for pane mode.

Panes give a human something to watch and take over. Herdr already classifies
agent lifecycle states and can see approval prompts that the message log cannot,
so pane agents take their status from Herdr and headless agents take it from the
message log.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from typing import Any, Dict, List, Optional

from fleet.errors import FleetError

CALL_TIMEOUT = 60


def herdr_binary() -> Optional[str]:
    return shutil.which("herdr")


def in_herdr() -> bool:
    """True when this process runs inside a Herdr-managed pane."""
    return os.environ.get("HERDR_ENV") == "1" and bool(os.environ.get("HERDR_SOCKET_PATH"))


def require_herdr() -> str:
    path = herdr_binary()
    if not path:
        raise FleetError(
            "the herdr command is not on PATH. Pane mode needs it; use --headless otherwise."
        )
    return path


def _run(*args: str, check: bool = True) -> Dict[str, Any]:
    require_herdr()
    try:
        proc = subprocess.run(
            [require_herdr(), *args], capture_output=True, text=True, timeout=CALL_TIMEOUT
        )
    except subprocess.TimeoutExpired as exc:
        raise FleetError(f"herdr {args[0]} timed out after {CALL_TIMEOUT}s") from exc
    if check and proc.returncode != 0:
        raise FleetError((proc.stderr or proc.stdout or "herdr failed").strip()[:500])
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise FleetError(f"could not parse herdr output: {proc.stdout[:300]!r}") from exc


def agents() -> List[Dict[str, Any]]:
    return _run("agent", "list")["result"]["agents"]


def panes() -> List[Dict[str, Any]]:
    return _run("pane", "list", check=False)["result"].get("panes", [])


def pane_session(pane: str) -> Optional[str]:
    """The OpenCode session currently occupying a pane, if any."""
    for agent in agents():
        if agent.get("pane_id") == pane:
            value = (agent.get("agent_session") or {}).get("value")
            if isinstance(value, str) and value.startswith("ses_"):
                return value
    return None


def pane_alive(pane: Optional[str]) -> bool:
    if not pane:
        return False
    try:
        return any(p.get("pane_id") == pane for p in panes())
    except (FleetError, KeyError):
        return False


def split_direction() -> str:
    """A split direction that suits the calling pane's shape.

    A wide pane gets a neighbour to the right, a tall or narrow one gets one
    below. Repeated same-direction splits produce unusably thin columns.
    """
    try:
        width = shutil.get_terminal_size((120, 40)).columns
    except OSError:
        width = 120
    direction = "right" if width >= 200 else "down"

    pane_id = os.environ.get("HERDR_PANE_ID")
    if pane_id:
        try:
            layout = _run("pane", "layout", "--pane", pane_id)["result"]
            for key in ("layout", "direction", "orientation"):
                value = layout.get(key)
                if isinstance(value, str) and value in ("right", "down", "left", "up"):
                    return value
        except (FleetError, KeyError, OSError):
            pass
    return direction


def split_pane(directory: str) -> str:
    """Create a sibling pane in the calling pane's tab, without stealing focus."""
    result = _run(
        "pane", "split", "--current", "--direction", split_direction(),
        "--cwd", directory, "--no-focus",
    )
    return result["result"]["pane"]["pane_id"]


def start_agent(name: str, kind: str, pane: str, startup_seconds: int = 60) -> None:
    """Launch an agent in an existing pane and wait for it to be ready.

    The pane must already be at an interactive shell prompt. Nothing here creates
    or moves layout; that is `split_pane`'s job.
    """
    _run(
        "agent", "start", name, "--kind", kind, "--pane", pane,
        "--timeout", str(startup_seconds * 1000),
    )


def prompt(name: str, text: str, timeout_seconds: int = 300) -> Dict[str, Any]:
    """Type a message into a pane agent's terminal and wait for it to settle.

    Used for a pane agent that has no session yet, so there is no API to address.
    Once a session exists the API path is preferred, since it records the message
    as machine-sent.
    """
    return _run(
        "agent", "prompt", name, text, "--wait",
        "--timeout", str(timeout_seconds * 1000),
    )


def close_pane(pane: str) -> bool:
    try:
        _run("pane", "close", pane, check=False)
        return True
    except FleetError:
        return False
