"""Tests for the command line surface.

These exercise the end-to-end path against the stub opencode, which is the only
way to check argument handling and the spawn/ask/kill lifecycle without spending
model calls.
"""

from __future__ import annotations

import json

import pytest

from fleet import cli
from conftest import assistant, settled, synthetic, turn


def run(argv, capsys=None):
    code = cli.main(argv)
    out = capsys.readouterr() if capsys else None
    return code, (out.out if out else ""), (out.err if out else "")


def _register(name, session, mode="headless"):
    from fleet.registry import Registry

    Registry().insert(name, {"session": session, "mode": mode, "pane": None})


def _last_text(stub_on_path):
    """The text of the most recent message the stub recorded."""
    import os

    with open(os.environ["FLEET_STUB_FIXTURE"]) as handle:
        fixture = json.load(handle)
    for log in fixture["messages"].values():
        for message in reversed(log):
            payload = message.get("payload") or {}
            if message.get("type") == "synthetic":
                return payload.get("text")
    return None


class TestArgumentHandling:
    def test_missing_command_is_an_error(self):
        with pytest.raises(SystemExit):
            cli.main([])

    def test_text_after_a_flag_is_folded_into_the_message(self, stub_on_path, capsys):
        # argparse cannot match a greedy nargs="*" positional that follows an
        # option, so fleet recovers plain words. Both orders must work.
        stub_on_path(sessions={"ses_a": {"id": "ses_a"}}, auto_reply="ok")
        _register("a", "ses_a")
        code, _, _ = run(["send", "a", "--prefix", "sys", "do the thing"], capsys)
        assert code == 0
        assert _last_text(stub_on_path) == "sys\n\ndo the thing"

    def test_text_before_a_flag_also_works(self, stub_on_path, capsys):
        stub_on_path(sessions={"ses_a": {"id": "ses_a"}}, auto_reply="ok")
        _register("a", "ses_a")
        code, _, _ = run(["send", "a", "do the thing", "--prefix", "sys"], capsys)
        assert code == 0
        assert _last_text(stub_on_path) == "sys\n\ndo the thing"

    def test_a_mistyped_flag_still_fails_loudly(self):
        # Folding trailing words must not swallow a typo into the prompt, or a
        # wrong timeout would quietly become part of the brief.
        with pytest.raises(SystemExit):
            cli.main(["ask", "a", "hi", "--timout", "30"])

    def test_version_is_reported(self, capsys):
        with pytest.raises(SystemExit) as excinfo:
            cli.main(["--version"])
        assert excinfo.value.code == 0


class TestSpawn:
    def test_spawns_a_headless_agent(self, stub_on_path, capsys):
        stub_on_path(sessions={})
        code, out, _ = run(["spawn", "alpha"], capsys)
        assert code == 0
        assert out.startswith("alpha\theadless\t")

    def test_rejects_an_invalid_name(self, stub_on_path, capsys):
        stub_on_path(sessions={})
        code, _, err = run(["spawn", "Bad Name"], capsys)
        assert code == 1
        assert "invalid name" in err

    def test_rejects_a_duplicate_name(self, stub_on_path, capsys):
        stub_on_path(sessions={})
        run(["spawn", "alpha"], capsys)
        code, _, err = run(["spawn", "alpha"], capsys)
        assert code == 1
        assert "already exists" in err

    def test_rejects_a_missing_directory(self, stub_on_path, capsys):
        stub_on_path(sessions={})
        code, _, err = run(["spawn", "alpha", "--dir", "/nope/not/here"], capsys)
        assert code == 1
        assert "no such directory" in err

    def test_pane_mode_needs_herdr(self, stub_on_path, capsys, monkeypatch):
        stub_on_path(sessions={})
        monkeypatch.delenv("HERDR_ENV", raising=False)
        code, _, err = run(["spawn", "alpha", "--pane"], capsys)
        assert code == 1
        assert "Herdr" in err

    def test_a_failed_spawn_leaves_no_registry_entry(self, stub_on_path, capsys, monkeypatch):
        # A reservation that is not cleaned up would block the name forever.
        stub_on_path(sessions={})
        monkeypatch.delenv("HERDR_ENV", raising=False)
        run(["spawn", "alpha", "--pane"], capsys)
        code, out, _ = run(["spawn", "alpha"], capsys)
        assert code == 0
        assert "alpha" in out


class TestLifecycle:
    def _spawn(self, stub_on_path, capsys, name="alpha"):
        stub_on_path(sessions={})
        run(["spawn", name], capsys)
        from fleet import config
        record = json.load(open(config.registry_path()))["agents"][name]
        return record["session"]

    def test_list_reports_a_registered_agent(self, stub_on_path, capsys):
        self._spawn(stub_on_path, capsys)
        code, out, _ = run(["list"], capsys)
        assert code == 0
        assert "alpha" in out and "headless" in out

    def test_list_json_is_machine_readable(self, stub_on_path, capsys):
        self._spawn(stub_on_path, capsys)
        code, out, _ = run(["list", "--json"], capsys)
        assert code == 0
        payload = json.loads(out)
        assert payload[0]["name"] == "alpha"
        assert payload[0]["mode"] == "headless"

    def test_list_with_no_agents_says_how_to_start_one(self, stub_on_path, capsys):
        code, out, _ = run(["list"], capsys)
        assert code == 0
        assert "fleet spawn" in out

    def test_ask_prints_the_reply(self, stub_on_path, capsys):
        session = self._spawn(stub_on_path, capsys)
        stub_on_path(sessions={session: {"id": session}}, auto_reply="the answer")
        code, out, _ = run(["ask", "alpha", "the question", "--poll", "0.01"], capsys)
        assert code == 0
        assert "the answer" in out

    def test_ask_reports_a_failed_turn(self, stub_on_path, capsys):
        session = self._spawn(stub_on_path, capsys)
        stub_on_path(sessions={session: {"id": session}},
                     auto_reply={"text": "partial", "outcome": "failed"})
        code, _, err = run(["ask", "alpha", "hi", "--poll", "0.01"], capsys)
        assert code == 1
        assert "turn failed" in err

    def test_ask_times_out_when_the_agent_never_answers(self, stub_on_path, capsys):
        session = self._spawn(stub_on_path, capsys)
        stub_on_path(sessions={session: {"id": session}}, auto_reply=None)
        code, _, err = run(["ask", "alpha", "hi", "--timeout", "1", "--poll", "0.01"], capsys)
        assert code == 1
        assert "did not start working" in err

    def test_ask_names_a_stuck_tool_call(self, stub_on_path, capsys):
        # A blocked permission prompt is otherwise indistinguishable from a slow
        # agent, and naming the tool turns a dead end into a diagnosis.
        session = self._spawn(stub_on_path, capsys)
        stub_on_path(sessions={session: {"id": session}},
                     auto_reply={"text": "working on it", "stuck_tool": "write"})
        code, out, err = run(["ask", "alpha", "hi", "--timeout", "1", "--poll", "0.01"], capsys)
        assert code == 124
        assert "working on it" in out
        assert "write" in err
        assert "permission prompt" in err

    def test_ask_rejects_no_resume(self, stub_on_path, capsys):
        self._spawn(stub_on_path, capsys)
        code, _, err = run(["ask", "alpha", "hi", "--no-resume"], capsys)
        assert code == 1
        assert "never wake" in err

    def test_wait_returns_once_the_turn_settles(self, stub_on_path, capsys):
        session = self._spawn(stub_on_path, capsys)
        stub_on_path(sessions={session: {"id": session}}, auto_reply="done")
        run(["send", "alpha", "hi"], capsys)
        code, out, _ = run(["wait", "alpha", "--poll", "0.01"], capsys)
        assert code == 0
        assert out.strip() == "idle"

    def test_collect_prints_the_last_turn(self, stub_on_path, capsys):
        session = self._spawn(stub_on_path, capsys)
        stub_on_path(sessions={session: {"id": session}},
                     messages={session: turn(100, "older") + turn(200, "newest")})
        code, out, _ = run(["collect", "alpha"], capsys)
        assert code == 0
        assert "newest" in out and "older" not in out

    def test_kill_removes_the_agent(self, stub_on_path, capsys):
        self._spawn(stub_on_path, capsys)
        code, out, _ = run(["kill", "alpha"], capsys)
        assert code == 0
        assert "killed" in out
        code, out, _ = run(["list"], capsys)
        assert "no agents registered" in out

    def test_kill_survives_a_bad_name_in_the_list(self, stub_on_path, capsys):
        # One unknown name must not strand the agents after it.
        self._spawn(stub_on_path, capsys, "alpha")
        self._spawn(stub_on_path, capsys, "beta")
        code, out, _ = run(["kill", "alpha", "ghost", "beta"], capsys)
        assert code == 1
        assert "ghost\tSKIP" in out
        assert "alpha killed" in out and "beta killed" in out

    def test_dead_session_shows_as_dead(self, stub_on_path, capsys):
        self._spawn(stub_on_path, capsys)
        stub_on_path(sessions={})
        code, out, _ = run(["status", "alpha"], capsys)
        assert code == 1
        assert '"status": "dead"' in out

    def test_prune_forgets_dead_agents(self, stub_on_path, capsys):
        self._spawn(stub_on_path, capsys)
        stub_on_path(sessions={})
        code, out, _ = run(["prune"], capsys)
        assert code == 0
        assert "pruned 1" in out

    def test_asking_an_unknown_agent_lists_the_alternatives(self, stub_on_path, capsys):
        self._spawn(stub_on_path, capsys, "alpha")
        code, _, err = run(["ask", "ghost", "hi"], capsys)
        assert code == 1
        assert "no agent named" in err and "alpha" in err

    def test_broadcast_skips_an_unknown_target_and_continues(self, stub_on_path, capsys):
        session = self._spawn(stub_on_path, capsys, "alpha")
        stub_on_path(sessions={session: {"id": session}}, auto_reply="ok")
        code, out, _ = run(["broadcast", "hello", "--to", "alpha", "ghost"], capsys)
        assert code == 1
        assert "ghost\tSKIP" in out
        assert "alpha\tmsg_" in out

    def test_one_agent_can_message_another(self, stub_on_path, capsys):
        # The path the whole tool exists for: a message from one agent reaching
        # another, and the reply read back.
        alpha = self._spawn(stub_on_path, capsys, "alpha")
        stub_on_path(sessions={alpha: {"id": alpha}, "ses_beta": {"id": "ses_beta"}},
                     auto_reply="bob got it")
        _register("beta", "ses_beta")

        code, _, _ = run(["send", "beta", "are you there"], capsys)
        assert code == 0
        code, out, _ = run(["collect", "beta"], capsys)
        assert code == 0
        assert "bob got it" in out


class TestInstallCommands:
    def test_install_then_templates(self, stub_on_path, capsys, isolated_env):
        code, out, _ = run(["install"], capsys)
        assert code == 0
        assert "installed" in out
        code, out, _ = run(["templates", "--paths"], capsys)
        assert str(isolated_env["opencode"]) in out

    def test_install_reports_what_it_kept(self, stub_on_path, capsys, isolated_env):
        run(["install"], capsys)
        import os
        path = isolated_env["opencode"] / "agents" / "worker.md"
        with open(path, "a") as handle:
            handle.write("\nedited\n")
        code, out, _ = run(["upgrade"], capsys)
        assert "left alone" in out

    def test_uninstall_removes_them(self, stub_on_path, capsys, isolated_env):
        run(["install"], capsys)
        code, _, _ = run(["uninstall"], capsys)
        assert code == 0
        assert not (isolated_env["opencode"] / "agents" / "worker.md").exists()

    def test_doctor_runs_and_reports(self, stub_on_path, capsys):
        code, out, _ = run(["doctor"], capsys)
        assert "fleet version" in out
        assert "opencode on PATH" in out

    def test_config_get_and_set(self, stub_on_path, capsys):
        code, out, _ = run(["config", "default_agent"], capsys)
        assert out.strip() == "build"
        code, out, _ = run(["config", "default_agent", "analyst"], capsys)
        assert "analyst" in out
        code, out, _ = run(["config"], capsys)
        assert json.loads(out)["default_agent"] == "analyst"

    def test_config_rejects_an_unknown_key(self, stub_on_path, capsys):
        with pytest.raises(SystemExit):
            run(["config", "nonsense", "x"], capsys)
