"""Reading the message log: what an agent said, and whether it is still working.

This is pure logic over message dicts, with no I/O, because it is where the
tricky parts live. The server leaves `time.idle` unpopulated, so the message log
is the only trustworthy signal of whether a session is busy.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

# Lifecycle states, as reported by `fleet list`. These are fleet's own names for
# agent liveness, not the server's vocabulary.
WAITING = "waiting"
WORKING = "working"
IDLE = "idle"
PENDING = "pending"
FAILED = "failed"
DEAD = "dead"
UNKNOWN = "unknown"

# States a caller can wait for.
WAITABLE = (IDLE, WORKING, FAILED, DEAD, WAITING)


def _created(message: Dict[str, Any]) -> float:
    return message.get("time", {}).get("created", 0)


def _completed(message: Dict[str, Any]) -> bool:
    return bool(message.get("time", {}).get("completed"))


def text_of(message: Dict[str, Any]) -> str:
    """The readable text of a message, ignoring reasoning and tool parts."""
    kind = message.get("type")
    if kind == "assistant":
        return "\n".join(
            part.get("text", "")
            for part in message.get("content", [])
            if part.get("type") == "text"
        ).strip()
    if kind in ("synthetic", "user"):
        payload = message.get("payload") or {}
        return payload.get("text") or message.get("text") or ""
    return ""


def turn_state(messages: List[Dict[str, Any]], after: float) -> Tuple[List[Dict[str, Any]], str]:
    """Assistant messages newer than a timestamp, plus what the turn is doing.

    An assistant message still streaming, with no `completed` stamp, means work
    is in flight. An `idle` message after the last assistant message means the
    loop ran out of things to do. This is what lets fleet wait for a real
    settle rather than for the server's `wait` endpoint, which returns
    immediately for a session that has not picked the work up yet.

    Returns one of `pending`, `working`, `idle` or `failed`. `pending` means
    nothing has started answering yet, which is not the same as being busy.
    """
    fresh = [m for m in messages if _created(m) > after]
    assistants = [m for m in fresh if m.get("type") == "assistant"]
    if not assistants:
        return [], PENDING

    last = assistants[-1]
    if not _completed(last):
        return assistants, WORKING

    for message in reversed(fresh):
        if message.get("type") == "idle" and _created(message) > _created(last):
            return assistants, (FAILED if message.get("outcome") == "failed" else IDLE)
    return assistants, WORKING


def live_state(messages: List[Dict[str, Any]]) -> str:
    """Classify a whole session from its message log.

    `turn_state` reports `pending` when no assistant message exists at all, which
    covers both a brand new session and one whose first turn has been admitted
    but not yet written. Those want opposite answers. The first is idle, the
    second is in flight, and mistaking the second for the first would let a
    caller interrupt a running turn believing it had finished.
    """
    if not messages:
        return IDLE

    _, state = turn_state(messages, 0.0)
    if state != PENDING:
        return state

    if any(m.get("type") == "idle" for m in messages):
        # Settled with no assistant text, so the turn died before replying.
        return FAILED
    if any(m.get("type") in ("synthetic", "user") for m in messages):
        return WORKING
    return IDLE


def last_turn(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Assistant messages belonging to the most recent inbound message."""
    anchor = 0.0
    for message in reversed(messages):
        if message.get("type") in ("synthetic", "user"):
            anchor = _created(message)
            break
    return [m for m in messages if m.get("type") == "assistant" and _created(m) > anchor]


def stuck_tool_calls(messages: List[Dict[str, Any]], after: float = 0.0) -> List[str]:
    """Names of tool calls that started and never reported back.

    A call left in `running` with `executed` false is what a blocked permission
    prompt looks like from the outside. An unattended agent has nobody to answer
    the prompt, so it sits there forever, and naming the tool is the difference
    between a useful diagnosis and a bare timeout.
    """
    stuck: List[str] = []
    for message in messages:
        if message.get("type") != "assistant" or _created(message) <= after:
            continue
        for part in message.get("content", []):
            if part.get("type") != "tool":
                continue
            state = part.get("state", {})
            if state.get("status") == "running" and not part.get("executed"):
                stuck.append(part.get("name", "tool"))
    return stuck


HANG_HINT = (
    "those tool calls started and never returned, which is what a blocked "
    "permission prompt looks like from outside. An unattended agent has nobody "
    "to answer it, so look for an 'ask' effect in the agent's permission rules."
)


def join_reply(assistants: List[Dict[str, Any]]) -> str:
    """Concatenate the text of an assistant turn."""
    return "\n".join(filter(None, (text_of(m) for m in assistants)))


def describe_status(status: Optional[str]) -> str:
    """A short human gloss for a lifecycle state."""
    return {
        WAITING: "work queued, not started",
        WORKING: "running",
        IDLE: "free",
        FAILED: "last turn failed",
        DEAD: "session or pane is gone",
    }.get(status or "", "unknown")
