"""A thin client for the OpenCode session API.

Every call shells out to `opencode api` rather than speaking HTTP directly, so
fleet inherits the same service discovery and authentication the TUI uses. That
means fleet works with a remote or explicitly configured server without knowing
anything about it.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from typing import Any, Dict, List, Optional

from fleet.errors import FleetError

# How long a single `opencode api` call may take. Generous, because the first
# call in a cold process can pay for service discovery or starting a server.
CALL_TIMEOUT = 120


def opencode_binary() -> Optional[str]:
    """Locate the opencode CLI.

    FLEET_OPENCODE_BIN wins over PATH, which matters when fleet is installed as
    an isolated tool and the user's shell profile is not in the environment.
    """
    override = os.environ.get("FLEET_OPENCODE_BIN")
    if override:
        if os.path.isfile(override) and os.access(override, os.X_OK):
            return override
        raise FleetError(f"FLEET_OPENCODE_BIN is set to {override!r}, which is not an executable file")
    return shutil.which("opencode")


def require_opencode() -> str:
    path = opencode_binary()
    if not path:
        raise FleetError(
            "the opencode command is not on PATH. Install OpenCode, or set FLEET_OPENCODE_BIN to its path."
        )
    return path


def api(method: str, path: str, data: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, str]] = None) -> Any:
    """Call one API endpoint and return the decoded body.

    The `opencode api` command reports failure two ways at once: an error object
    on stdout and a status line on stderr, with a non-zero exit. The error object
    is valid JSON, so the exit code is the only reliable signal that a reply is
    not a real one.

    A 204 carries no body. That is a success, so an empty reply returns `{}`
    rather than being mistaken for a failure.
    """
    require_opencode()
    command = [require_opencode(), "api", method, path]
    if data is not None:
        command += ["--data", json.dumps(data)]
    for key, value in (params or {}).items():
        command += ["--param", f"{key}={value}"]

    try:
        proc = subprocess.run(command, capture_output=True, text=True, timeout=CALL_TIMEOUT)
    except subprocess.TimeoutExpired as exc:
        raise FleetError(f"{method} {path} timed out after {CALL_TIMEOUT}s") from exc

    raw = proc.stdout.strip()
    parsed = None
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
            break
        except json.JSONDecodeError:
            continue

    if proc.returncode != 0:
        if isinstance(parsed, dict):
            detail = parsed.get("message") or parsed.get("_tag") or json.dumps(parsed)
        else:
            detail = (proc.stderr or raw or "opencode api failed").strip()
        raise FleetError(f"{method} {path}: {detail}")

    return parsed if parsed is not None else {}


# -- sessions ---------------------------------------------------------------


def create_session(directory: str, title: str, agent: Optional[str] = None,
                   model: Optional[str] = None) -> str:
    body: Dict[str, Any] = {"title": title, "location": {"directory": directory}}
    if agent:
        body["agent"] = agent
    if model:
        body["model"] = model
    return api("post", "/api/session", body)["data"]["id"]


def get_session(session: str) -> Optional[Dict[str, Any]]:
    """Session info, or None when the session does not exist.

    Note the server leaves `time.idle` unpopulated, so this cannot answer
    whether a session is busy. Use fleet.messaging for that.
    """
    try:
        return api("get", f"/api/session/{session}")["data"]
    except (FleetError, KeyError, TypeError):
        return None


def delete_session(session: str) -> bool:
    """Delete a session. False means it was already gone."""
    try:
        api("delete", f"/api/session/{session}")
        return True
    except FleetError:
        return False


def list_sessions(limit: int = 50) -> List[Dict[str, Any]]:
    return api("get", "/api/session", params={"limit": str(limit), "order": "desc"})["data"]


# -- messaging --------------------------------------------------------------


def send_synthetic(session: str, text: str, queue: bool = False, resume: bool = True) -> Dict[str, Any]:
    """Admit one message into a session.

    `synthetic` rather than `prompt`, so the server records it as machine-sent
    and it is never mistaken for a human turn.

    `queue` waits for the running turn to finish; otherwise `steer` interrupts
    the current turn. `resume=False` enqueues without waking the session, which
    is how work is staged ahead of time.
    """
    body: Dict[str, Any] = {"text": text, "delivery": "queue" if queue else "steer"}
    if not resume:
        body["resume"] = False
    return api("post", f"/api/session/{session}/synthetic", body)["data"]


def messages(session: str, limit: int = 60) -> List[Dict[str, Any]]:
    """The message log, oldest first."""
    found = api("get", f"/api/session/{session}/message",
                params={"limit": str(limit), "order": "desc"})["data"]
    return list(reversed(found))


def pending_inbox(session: str) -> int:
    """How much work is admitted but not yet picked up by the agent loop.

    Messages sent with `resume=False` wait here and never reach the message log,
    so the log alone cannot tell a queued agent from a finished one.
    """
    try:
        return len(api("get", f"/api/session/{session}/inbox")["data"])
    except (FleetError, KeyError, TypeError):
        return 0
