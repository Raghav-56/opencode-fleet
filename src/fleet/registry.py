"""The agent registry: which named agents exist and where they live.

Concurrency note, because this bit us. The lock is taken per
read-modify-write and released immediately. A coordinator runs `fleet ask`
against a worker for minutes at a time, and that worker may itself call `fleet
spawn`. Holding the lock for the life of the process deadlocks that pair, so no
method here may span a network call.
"""

from __future__ import annotations

import fcntl
import json
import os
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional

from fleet import config
from fleet.errors import FleetError


class Registry:
    """Named agents, keyed by name, guarded by a lock file."""

    def __init__(self) -> None:
        os.makedirs(config.state_dir(), exist_ok=True)

    @contextmanager
    def _locked(self) -> Iterator[None]:
        os.makedirs(config.state_dir(), exist_ok=True)
        handle = open(config.lock_path(), "a+")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
            handle.close()

    def _read(self) -> Dict[str, Any]:
        try:
            with open(config.registry_path()) as handle:
                data = json.load(handle)
        except FileNotFoundError:
            data = {}
        except json.JSONDecodeError as exc:
            raise FleetError(
                f"registry at {config.registry_path()} is corrupt: {exc}. "
                "Fix or delete the file, then run `fleet prune`."
            ) from exc
        except OSError as exc:
            raise FleetError(f"could not read the registry: {exc}") from exc
        if not isinstance(data, dict):
            raise FleetError(f"registry at {config.registry_path()} is not a JSON object")
        agents = data.get("agents", {})
        if not isinstance(agents, dict):
            raise FleetError(f"registry at {config.registry_path()} has a malformed 'agents' key")
        data["agents"] = agents
        return data

    def _write(self, data: Dict[str, Any]) -> None:
        path = config.registry_path()
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)

    # -- reads ------------------------------------------------------------

    def all(self) -> Dict[str, Any]:
        with self._locked():
            return self._read()["agents"]

    def names(self) -> List[str]:
        return sorted(self.all())

    def get(self, name: str) -> Optional[Dict[str, Any]]:
        with self._locked():
            return self._read()["agents"].get(name)

    def require(self, name: str) -> Dict[str, Any]:
        with self._locked():
            agents = self._read()["agents"]
            record = agents.get(name)
            if record is None:
                known = ", ".join(sorted(agents)) or "none yet"
                raise FleetError(f"no agent named {name!r}. registered: {known}")
            return record

    # -- writes -----------------------------------------------------------

    def put(self, name: str, record: Dict[str, Any]) -> None:
        with self._locked():
            data = self._read()
            data["agents"][name] = record
            self._write(data)

    def insert(self, name: str, record: Dict[str, Any]) -> None:
        """Register a new name, refusing to overwrite an existing one.

        Used to claim a name before any session or pane is created, so two
        concurrent spawns of the same name cannot both build one and leave the
        loser unregistered.
        """
        with self._locked():
            data = self._read()
            if name in data["agents"]:
                raise FleetError(f"{name!r} already exists. Run `fleet kill {name}` first.")
            data["agents"][name] = record
            self._write(data)

    def update(self, name: str, **fields: Any) -> Dict[str, Any]:
        with self._locked():
            data = self._read()
            record = data["agents"].get(name)
            if record is None:
                raise FleetError(f"no agent named {name!r}")
            record.update(fields)
            self._write(data)
            return record

    def drop(self, name: str) -> None:
        with self._locked():
            data = self._read()
            data["agents"].pop(name, None)
            self._write(data)
