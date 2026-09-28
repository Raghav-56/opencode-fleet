"""Tests for the registry's locking and conflict behaviour.

The lock is per read-modify-write, not per process, because a coordinator holds a
long-running `fleet ask` while the worker it is waiting on may itself call
`fleet spawn`. A process-lifetime lock deadlocks that pair.
"""

from __future__ import annotations

import json
import multiprocessing
import os
import time

import pytest

from fleet import config
from fleet.errors import FleetError
from fleet.registry import Registry


def _child_insert(args):
    """Spawn a same-name agent in a separate process, reporting success."""
    name, start_at = args
    time.sleep(max(0.0, start_at - time.time()))
    from fleet.registry import Registry as R

    try:
        R().insert(name, {"session": None, "mode": "headless"})
        return name, True
    except FleetError:
        return name, False


class TestBasics:
    def test_empty_registry_has_no_agents(self):
        assert Registry().names() == []

    def test_insert_then_read_back(self):
        reg = Registry()
        reg.insert("alpha", {"session": "ses_1", "mode": "headless"})
        assert reg.require("alpha")["session"] == "ses_1"

    def test_insert_refuses_to_overwrite(self):
        reg = Registry()
        reg.insert("alpha", {"session": "ses_1"})
        with pytest.raises(FleetError, match="already exists"):
            reg.insert("alpha", {"session": "ses_2"})

    def test_require_names_the_alternatives(self):
        reg = Registry()
        reg.insert("alpha", {})
        reg.insert("beta", {})
        with pytest.raises(FleetError) as excinfo:
            reg.require("gamma")
        assert "alpha, beta" in str(excinfo.value)

    def test_require_on_empty_registry_says_none_yet(self):
        with pytest.raises(FleetError, match="none yet"):
            Registry().require("ghost")

    def test_update_merges_fields(self):
        reg = Registry()
        reg.insert("alpha", {"session": "ses_1", "status": "waiting"})
        reg.update("alpha", status="idle")
        assert reg.require("alpha") == {"session": "ses_1", "status": "idle"}

    def test_update_of_missing_name_raises(self):
        with pytest.raises(FleetError, match="no agent named"):
            Registry().update("ghost", status="idle")

    def test_drop_is_forgiving(self):
        reg = Registry()
        reg.insert("alpha", {})
        reg.drop("alpha")
        reg.drop("alpha")  # must not raise
        assert reg.names() == []

    def test_corrupt_registry_explains_itself(self, isolated_env):
        path = config.registry_path()
        with open(path, "w") as handle:
            handle.write("{not json")
        with pytest.raises(FleetError, match="corrupt"):
            Registry().names()


class TestAtomicity:
    def test_only_one_concurrent_spawn_of_a_name_wins(self):
        """The reason insert claims the name before building anything.

        Two concurrent spawns must not both create a session and leave one of
        them unregistered.
        """
        reg = Registry()
        start = time.time() + 0.5
        with multiprocessing.Pool(2) as pool:
            results = pool.map(_child_insert, [("twin", start), ("twin", start)])
        assert sorted(ok for _, ok in results) == [False, True]
        assert reg.names() == ["twin"]

    def test_concurrent_spawns_of_different_names_all_succeed(self):
        reg = Registry()
        start = time.time() + 0.5
        with multiprocessing.Pool(4) as pool:
            results = pool.map(
                _child_insert, [(f"agent{i}", start) for i in range(4)]
            )
        assert all(ok for _, ok in results)
        assert reg.names() == ["agent0", "agent1", "agent2", "agent3"]

    def test_registry_file_is_never_left_partially_written(self, isolated_env):
        reg = Registry()
        for i in range(20):
            reg.insert(f"a{i}", {"session": f"ses_{i}"})
        with open(config.registry_path()) as handle:
            data = json.load(handle)
        assert len(data["agents"]) == 20

    def test_no_temp_files_survive(self, isolated_env):
        reg = Registry()
        for i in range(5):
            reg.insert(f"a{i}", {})
        leftovers = [n for n in os.listdir(config.state_dir()) if n.endswith(".tmp")]
        assert leftovers == []
