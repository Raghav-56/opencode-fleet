"""Tests for the install lifecycle.

The behaviour that matters most here is restraint. A profile is a plain
Markdown file the user may edit, and an installer that silently overwrites such
a file destroys exactly the reasoning they added. So every test below is really
about whether fleet leaves the user's work alone.
"""

from __future__ import annotations

import json
import re
import os

import pytest

from fleet import config
from fleet import install as install_mod
from fleet.errors import FleetError


def agent_path(isolated_env, name="orchestrator.md"):
    return str(isolated_env["opencode"] / "agents" / name)


def command_path(isolated_env, name="orchestrate.md"):
    return str(isolated_env["opencode"] / "commands" / name)


class TestTemplates:
    def test_ships_the_expected_profiles(self):
        found = install_mod.template_list()
        assert "agents/orchestrator.md" in found
        assert "agents/worker.md" in found
        assert "agents/analyst.md" in found
        assert "commands/orchestrate.md" in found

    def test_templates_are_marked_as_install_managed(self):
        # A user who edits one should know it will be detected rather than
        # silently replaced.
        for relpath in install_mod.template_list():
            assert "fleet install" in install_mod.template_text(relpath)

    def test_no_template_uses_an_ask_permission(self):
        # An `ask` rule blocks forever in an unattended agent, which is how a
        # coordinator once hung for thirty minutes on three writes. Matched on
        # whole lines so the prose in each profile's comments does not trip it.
        pattern = re.compile(r"^\s*effect:\s*ask\s*$", re.MULTILINE)
        for relpath in install_mod.template_list():
            if not relpath.startswith("agents/"):
                continue
            assert not pattern.search(install_mod.template_text(relpath)), relpath

    def test_orchestrator_can_run_the_fleet_cli(self):
        text = install_mod.template_text("agents/orchestrator.md")
        assert 'resource: "fleet *"' in text
        assert "effect: allow" in text

    def test_analyst_cannot_edit(self):
        text = install_mod.template_text("agents/analyst.md")
        assert "edit" in text and "deny" in text

    def test_malformed_template_path_is_rejected(self):
        with pytest.raises(FleetError, match="malformed template path"):
            install_mod._relpath_to_kind("nonsense")
        with pytest.raises(FleetError, match="malformed template path"):
            install_mod._relpath_to_kind("nonsense/name.md")
        with pytest.raises(FleetError, match="malformed template path"):
            install_mod._relpath_to_kind("agents/")
        with pytest.raises(FleetError, match="unknown template kind"):
            config.target_path("nonsense", "x.md")


class TestInstall:
    def test_writes_profiles_and_records_them(self, isolated_env):
        results = install_mod.install()
        assert all(r.action == install_mod.INSTALLED for r in results)
        assert os.path.exists(agent_path(isolated_env))
        assert os.path.exists(command_path(isolated_env))

        record = json.load(open(config.install_record_path()))
        assert record["files"]["agents/orchestrator.md"]["sha256"]
        assert record["version"]

    def test_install_is_idempotent(self, isolated_env):
        install_mod.install()
        second = install_mod.install()
        assert all(r.action == install_mod.UNCHANGED for r in second)

    def test_only_limits_which_profiles_are_written(self, isolated_env):
        results = install_mod.install(only=["agents/worker.md"])
        assert [r.relpath for r in results] == ["agents/worker.md"]
        assert os.path.exists(agent_path(isolated_env, "worker.md"))
        assert not os.path.exists(agent_path(isolated_env, "orchestrator.md"))

    def test_leaves_a_preexisting_user_profile_alone(self, isolated_env):
        # Someone who wrote their own orchestrator.md before installing fleet
        # keeps it, and is told why.
        path = agent_path(isolated_env)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as handle:
            handle.write("my own profile\n")

        results = {r.relpath: r.action for r in install_mod.install()}
        assert results["agents/orchestrator.md"] == install_mod.KEPT_PREEXISTING
        with open(path) as handle:
            assert handle.read() == "my own profile\n"

    def test_force_overwrites_a_preexisting_profile(self, isolated_env):
        path = agent_path(isolated_env)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as handle:
            handle.write("my own profile\n")

        results = {r.relpath: r.action for r in install_mod.install(force=True)}
        assert results["agents/orchestrator.md"] == install_mod.UPDATED
        with open(path) as handle:
            assert handle.read() == install_mod.template_text("agents/orchestrator.md")


class TestDriftProtection:
    def test_a_user_edit_survives_upgrade(self, isolated_env):
        """The core guarantee. Upgrade must not eat the user's work."""
        install_mod.install()
        path = agent_path(isolated_env, "worker.md")
        with open(path, "a") as handle:
            handle.write("\nMy important note about permissions.\n")

        results = {r.relpath: r.action for r in install_mod.install()}
        assert results["agents/worker.md"] == install_mod.KEPT_MODIFIED

        with open(path) as handle:
            assert "My important note" in handle.read()

    def test_a_modified_profile_is_still_recorded(self, isolated_env):
        # The record tracks what fleet wrote, so the user's later edits stay
        # distinguishable from fleet's.
        install_mod.install()
        path = agent_path(isolated_env, "worker.md")
        with open(path, "a") as handle:
            handle.write("\nedited\n")
        install_mod.install()

        record = json.load(open(config.install_record_path()))
        assert record["files"]["agents/worker.md"]["sha256"]

    def test_force_overwrites_a_user_edit(self, isolated_env):
        install_mod.install()
        path = agent_path(isolated_env, "worker.md")
        with open(path, "a") as handle:
            handle.write("\nedited\n")

        results = {r.relpath: r.action for r in install_mod.install(force=True)}
        assert results["agents/worker.md"] == install_mod.UPDATED
        with open(path) as handle:
            assert "edited\n" not in handle.read()

    def test_unmodified_profiles_are_updated_after_a_shipped_change(self, isolated_env, monkeypatch):
        # The real scenario for a safe update: a newer fleet release ships a
        # changed profile, and the file on disk still matches what the previous
        # release wrote, so the user never touched it.
        install_mod.install()
        original = install_mod.template_text("agents/analyst.md")
        monkeypatch.setattr(
            install_mod, "template_text",
            lambda relpath: original + "\nA newer release added this line.\n"
            if relpath == "agents/analyst.md" else original,
        )
        results = {r.relpath: r.action for r in install_mod.install()}
        assert results["agents/analyst.md"] == install_mod.UPDATED
        with open(agent_path(isolated_env, "analyst.md")) as handle:
            assert "A newer release added this line." in handle.read()

    def test_drift_reports_each_case(self, isolated_env):
        install_mod.install()
        with open(agent_path(isolated_env, "worker.md"), "a") as handle:
            handle.write("\nedited\n")

        report = {r.relpath: r.action for r in install_mod.drift()}
        assert report["agents/worker.md"] == install_mod.KEPT_MODIFIED
        assert report["agents/analyst.md"] == install_mod.UNCHANGED

    def test_drift_reports_a_missing_profile(self, isolated_env):
        report = {r.relpath: r.action for r in install_mod.drift()}
        assert report["agents/orchestrator.md"] == "missing"


class TestUninstall:
    def test_removes_profiles_it_installed(self, isolated_env):
        install_mod.install()
        results = install_mod.uninstall()
        assert all(r.action == install_mod.REMOVED for r in results)
        assert not os.path.exists(agent_path(isolated_env))
        assert not os.path.exists(command_path(isolated_env))

    def test_keeps_a_profile_the_user_edited(self, isolated_env):
        install_mod.install()
        with open(agent_path(isolated_env, "worker.md"), "a") as handle:
            handle.write("\nmine now\n")

        results = {r.relpath: r.action for r in install_mod.uninstall()}
        assert results["agents/worker.md"] == install_mod.KEPT_MODIFIED_REMOVE
        assert os.path.exists(agent_path(isolated_env, "worker.md"))

    def test_force_removes_an_edited_profile(self, isolated_env):
        install_mod.install()
        with open(agent_path(isolated_env, "worker.md"), "a") as handle:
            handle.write("\nmine now\n")
        install_mod.uninstall(force=True)
        assert not os.path.exists(agent_path(isolated_env))

    def test_leaves_preexisting_profiles_alone(self, isolated_env):
        path = agent_path(isolated_env)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as handle:
            handle.write("mine\n")
        install_mod.install()
        install_mod.uninstall()
        assert os.path.exists(path)

    def test_uninstall_is_safe_when_nothing_is_installed(self):
        assert install_mod.uninstall() == []


class TestConfig:
    def test_defaults_apply_without_a_config_file(self):
        assert config.load_config()["default_agent"] == "build"

    def test_written_values_round_trip(self):
        config.write_config({"default_agent": "analyst"})
        assert config.load_config()["default_agent"] == "analyst"

    def test_writing_preserves_other_keys(self):
        config.write_config({"default_agent": "analyst"})
        config.write_config({"default_kind": "codex"})
        loaded = config.load_config()
        assert loaded["default_agent"] == "analyst"
        assert loaded["default_kind"] == "codex"

    def test_unknown_key_is_rejected(self):
        with pytest.raises(FleetError, match="unknown config keys"):
            config.write_config({"nonsense": 1})

    def test_malformed_config_is_reported_clearly(self, isolated_env):
        os.makedirs(config.config_dir(), exist_ok=True)
        with open(config.config_path(), "w") as handle:
            handle.write("{not json")
        with pytest.raises(FleetError, match="not valid JSON"):
            config.load_config()

    def test_target_path_rejects_unknown_kinds(self):
        with pytest.raises(FleetError, match="unknown template kind"):
            config.target_path("nonsense", "x.md")
