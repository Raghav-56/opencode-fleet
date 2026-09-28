"""Tests for the opencode api client, driven by the stub binary.

These lock in two behaviours of the real command that are easy to get wrong:
a failing call prints a valid JSON error object on stdout, and a successful
DELETE prints nothing at all.
"""

from __future__ import annotations

import pytest

from fleet import api
from fleet.errors import FleetError
from conftest import assistant, settled, synthetic, turn


class TestErrorHandling:
    def test_missing_session_raises_with_the_server_message(self, stub_on_path):
        stub_on_path(sessions={"ses_known": {"id": "ses_known"}})
        with pytest.raises(FleetError) as excinfo:
            api.api("get", "/api/session/ses_unknown")
        assert "Session not found" in str(excinfo.value)

    def test_error_object_on_stdout_is_not_mistaken_for_a_response(self, stub_on_path):
        # The error body is valid JSON, so only the exit code distinguishes a
        # failure from a real reply. Getting this wrong makes every dead session
        # look alive.
        stub_on_path(sessions={})
        with pytest.raises(FleetError):
            api.api("get", "/api/session/ses_anything")

    def test_get_session_returns_none_for_a_missing_session(self, stub_on_path):
        stub_on_path(sessions={})
        assert api.get_session("ses_gone") is None

    def test_get_session_returns_the_record_when_present(self, stub_on_path):
        stub_on_path(sessions={"ses_a": {"id": "ses_a", "cost": 0.5}})
        assert api.get_session("ses_a")["cost"] == 0.5


class TestDelete:
    def test_delete_reports_success(self, stub_on_path):
        stub_on_path(sessions={"ses_a": {"id": "ses_a"}})
        assert api.delete_session("ses_a") is True

    def test_delete_of_a_missing_session_is_false_not_an_error(self, stub_on_path):
        stub_on_path(sessions={})
        assert api.delete_session("ses_gone") is False

    def test_empty_reply_is_treated_as_success(self, stub_on_path):
        # A 204 carries no body. Treating an empty reply as a failure made
        # `fleet kill` report that it had cleaned nothing up, every time.
        stub_on_path(sessions={"ses_a": {"id": "ses_a"}})
        assert api.api("delete", "/api/session/ses_a") == {}


class TestSessions:
    def test_create_session_returns_the_new_id(self, stub_on_path):
        stub_on_path(sessions={})
        assert api.create_session("/tmp", "worker").startswith("ses_")

    def test_create_session_passes_the_location(self, stub_on_path):
        stub_on_path(sessions={})
        api.create_session("/home/ubuntu/raghav", "worker", agent="analyst")
        # The stub records the title; the important part is that it did not 400,
        # which it would if the location were malformed.
        assert api.create_session("/home/ubuntu/raghav", "w2", agent="analyst")

    def test_create_session_rejects_a_bad_location(self, stub_on_path):
        # The real server requires location.directory, not location.path. This
        # pins the shape fleet sends.
        stub_on_path(sessions={})
        with pytest.raises(FleetError, match="directory"):
            api.api("post", "/api/session", {"location": {"path": "/tmp"}})


class TestMessaging:
    def test_send_synthetic_returns_the_message_id(self, stub_on_path):
        stub_on_path(sessions={"ses_a": {"id": "ses_a"}})
        assert api.send_synthetic("ses_a", "hello")["id"].startswith("msg_")

    def test_messages_come_back_oldest_first(self, stub_on_path):
        stub_on_path(
            sessions={"ses_a": {"id": "ses_a"}},
            messages={"ses_a": turn(100) + turn(200, "second")},
        )
        found = api.messages("ses_a")
        assert [m["id"] for m in found][:2] == ["msg_in100", "msg_out101"]

    def test_pending_work_never_reaches_the_message_log(self, stub_on_path):
        # The server holds resume=False work in the inbox, so the log alone
        # cannot tell a queued agent from a finished one.
        stub_on_path(sessions={"ses_a": {"id": "ses_a"}})
        api.send_synthetic("ses_a", "later", resume=False)
        assert api.messages("ses_a") == []
        assert api.pending_inbox("ses_a") == 1

    def test_resumed_work_does_reach_the_log(self, stub_on_path):
        stub_on_path(sessions={"ses_a": {"id": "ses_a"}})
        api.send_synthetic("ses_a", "now")
        assert len(api.messages("ses_a")) == 1
        assert api.pending_inbox("ses_a") == 0

    def test_pending_inbox_of_a_missing_session_is_zero(self, stub_on_path):
        stub_on_path(sessions={})
        assert api.pending_inbox("ses_gone") == 0


class TestDependency:
    def test_missing_opencode_binary_is_a_clear_error(self, monkeypatch):
        monkeypatch.setenv("PATH", "/nonexistent")
        with pytest.raises(FleetError, match="not on PATH"):
            api.require_opencode()

    def test_explicit_binary_overrides_path(self, monkeypatch, tmp_path):
        # fleet is often installed as an isolated tool, where the user's shell
        # profile may not be in the environment.
        fake = tmp_path / "fake-opencode"
        fake.write_text("#!/bin/sh\n")
        fake.chmod(0o755)
        monkeypatch.setenv("PATH", "/nonexistent")
        monkeypatch.setenv("FLEET_OPENCODE_BIN", str(fake))
        assert api.require_opencode() == str(fake)

    def test_a_bad_override_is_reported(self, monkeypatch, tmp_path):
        monkeypatch.setenv("FLEET_OPENCODE_BIN", str(tmp_path / "nope"))
        with pytest.raises(FleetError, match="not an executable file"):
            api.require_opencode()
