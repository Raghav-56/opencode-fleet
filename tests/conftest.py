"""Shared fixtures.

Tests run against a stub `opencode` on PATH and against temporary config and
state directories, so nothing touches the developer's real agents or sessions.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
STUB = ROOT / "tests" / "stub_opencode"

# Run against the source tree, so the suite works in a bare checkout without an
# editable install. An installed copy is exercised separately by the smoke test
# in the README.
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    """Point every path fleet uses at a temp directory."""
    state = tmp_path / "state"
    conf = tmp_path / "config"
    opencode_cfg = tmp_path / "opencode"
    state.mkdir()
    conf.mkdir()
    opencode_cfg.mkdir()

    monkeypatch.setenv("FLEET_STATE_DIR", str(state))
    monkeypatch.setenv("FLEET_CONFIG_DIR", str(conf))
    monkeypatch.setenv("FLEET_OPENCODE_CONFIG_DIR", str(opencode_cfg))
    monkeypatch.delenv("FLEET_DEBUG", raising=False)
    return {"state": state, "config": conf, "opencode": opencode_cfg}


@pytest.fixture
def fixture_path(tmp_path, isolated_env):
    """Path to a mutable stub fixture, and a setter for its contents."""
    path = tmp_path / "stub-fixture.json"

    def apply(sessions=None, messages=None, inbox=None, version="stub", auto_reply=None):
        data = {
            "sessions": sessions if sessions is not None else {},
            "messages": messages if messages is not None else {},
            "inbox": inbox if inbox is not None else {},
            "version": version,
            "auto_reply": auto_reply,
        }
        path.write_text(json.dumps(data))
        return path

    return apply


@pytest.fixture
def stub_on_path(fixture_path, isolated_env, monkeypatch):
    """Put the stub opencode first on PATH and return the fixture setter."""
    bindir = isolated_env["state"].parent / "bin"
    bindir.mkdir(exist_ok=True)
    target = bindir / "opencode"
    target.write_text(STUB.read_text())
    target.chmod(target.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    monkeypatch.setenv("FLEET_STUB_FIXTURE", str(fixture_path()))
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    return fixture_path


# -- message builders -------------------------------------------------------


def at(ms, completed=False):
    """A message timestamp: created, and optionally completed."""
    stamp = {"created": ms}
    if completed:
        stamp["completed"] = ms + 1
    return stamp


def synthetic(ms, text="hello", delivery="steer"):
    return {
        "id": f"msg_in{ms}",
        "type": "synthetic",
        "time": at(ms),
        "delivery": delivery,
        "payload": {"text": text},
    }


def assistant(ms, text="reply", completed=True, tool=None, executed=True, running=False):
    content = []
    if tool:
        content.append({
            "type": "tool",
            "name": tool,
            "executed": executed,
            "state": {"status": "running" if running else "completed", "input": {}},
        })
    if text:
        content.append({"type": "text", "text": text})
    return {
        "id": f"msg_out{ms}",
        "type": "assistant",
        "time": at(ms, completed and not running),
        "content": content,
    }


def settled(ms, outcome="succeeded"):
    return {"id": f"msg_idle{ms}", "type": "idle", "time": at(ms + 2), "outcome": outcome}


def turn(ms, text="reply", outcome="succeeded"):
    return [synthetic(ms), assistant(ms + 1, text), settled(ms + 1, outcome)]
