"""Tests for the message-log logic.

This is where the subtle bugs lived: a session's `time.idle` is never populated
by the server, and a session that has just been given work looks identical to one
that has never run anything. Both mistakes are covered here.
"""

from __future__ import annotations

from fleet import messaging as msg
from conftest import assistant, settled, synthetic, turn


class TestTextOf:
    def test_assistant_text_joins_text_parts(self):
        message = {
            "type": "assistant",
            "content": [
                {"type": "reasoning", "text": "thinking hard"},
                {"type": "text", "text": "line one"},
                {"type": "tool", "name": "bash", "state": {}},
                {"type": "text", "text": "line two"},
            ],
        }
        assert msg.text_of(message) == "line one\nline two"

    def test_synthetic_reads_payload(self):
        assert msg.text_of(synthetic(1, "do the thing")) == "do the thing"

    def test_user_reads_plain_text(self):
        assert msg.text_of({"type": "user", "text": "hi"}) == "hi"

    def test_idle_message_has_no_text(self):
        assert msg.text_of(settled(1)) == ""


class TestTurnState:
    def test_no_messages_is_pending_not_busy(self):
        # The distinction that matters: a session that never ran is idle, and
        # reporting it as busy makes `fleet list` lie about every fresh agent.
        _, state = msg.turn_state([], 0.0)
        assert state == msg.PENDING

    def test_streaming_assistant_is_working(self):
        messages = [synthetic(1), assistant(2, completed=False)]
        _, state = msg.turn_state(messages, 0.0)
        assert state == msg.WORKING

    def test_completed_assistant_without_settle_marker_is_working(self):
        messages = [synthetic(1), assistant(2, completed=True)]
        _, state = msg.turn_state(messages, 0.0)
        assert state == msg.WORKING

    def test_settle_marker_makes_it_idle(self):
        _, state = msg.turn_state(turn(100), 0.0)
        assert state == msg.IDLE

    def test_failed_settle_marker_reports_failed(self):
        messages = turn(100, outcome="failed")
        _, state = msg.turn_state(messages, 0.0)
        assert state == msg.FAILED

    def test_only_messages_after_the_marker_count(self):
        older = turn(100, "old answer")
        newer_in = synthetic(200)
        _, state = msg.turn_state(older + [newer_in], 200)
        assert state == msg.PENDING

    def test_multiple_assistants_in_one_turn(self):
        # Tool use splits a turn across several assistant messages. The turn is
        # only done when a settle marker follows the last of them.
        messages = [synthetic(1), assistant(2, "step one"), assistant(4, "step two"), settled(4)]
        assistants, state = msg.turn_state(messages, 0.0)
        assert state == msg.IDLE
        assert len(assistants) == 2


class TestLiveState:
    def test_untouched_session_is_idle(self):
        assert msg.live_state([]) == msg.IDLE

    def test_admitted_but_unanswered_work_counts_as_working(self):
        # The regression that made `fleet send` then `fleet list` show idle,
        # which invites a caller to interrupt a turn that is still running.
        assert msg.live_state([synthetic(1)]) == msg.WORKING

    def test_settled_turn_is_idle(self):
        assert msg.live_state(turn(100)) == msg.IDLE

    def test_answered_work_still_streaming_is_working(self):
        assert msg.live_state([synthetic(1), assistant(2, completed=False)]) == msg.WORKING

    def test_settle_with_no_assistant_text_is_failed(self):
        # A turn that died before producing a reply leaves a settle marker and
        # nothing else. Treating that as idle would report a broken agent as
        # ready for more work.
        assert msg.live_state([synthetic(1), settled(1)]) == msg.FAILED


class TestLastTurn:
    def test_returns_only_the_final_turn(self):
        messages = turn(100, "first") + turn(200, "second")
        picked = msg.last_turn(messages)
        assert [msg.text_of(m) for m in picked] == ["second"]

    def test_no_inbound_message_returns_everything(self):
        messages = [assistant(1, "orphan")]
        assert msg.last_turn(messages) == messages


class TestStuckToolCalls:
    def test_running_and_unexecuted_tool_is_stuck(self):
        # What a blocked permission prompt looks like from outside: the call
        # started, never executed, and never returned.
        messages = [synthetic(1), assistant(2, tool="write", executed=False, running=True)]
        assert msg.stuck_tool_calls(messages) == ["write"]

    def test_finished_tool_is_not_stuck(self):
        messages = [synthetic(1), assistant(2, tool="bash", executed=True)]
        assert msg.stuck_tool_calls(messages) == []

    def test_ignores_calls_from_before_the_marker(self):
        messages = [assistant(2, tool="write", executed=False, running=True), synthetic(100)]
        assert msg.stuck_tool_calls(messages, 100) == []

    def test_reports_each_stuck_call(self):
        stuck = assistant(2, tool=None, executed=False, running=True)
        stuck["content"].insert(0, {
            "type": "tool", "name": "write", "executed": False, "state": {"status": "running"},
        })
        assert msg.stuck_tool_calls([synthetic(1), stuck]) == ["write"]


class TestJoinReply:
    def test_skips_empty_assistants(self):
        messages = [assistant(1, "one"), assistant(2, text=""), assistant(3, "three")]
        assert msg.join_reply(messages) == "one\nthree"
