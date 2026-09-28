"""Installing, upgrading and removing the agent profiles fleet ships.

The profiles in `templates/` are the point of the package. Without them the CLI
is inert, because nothing tells OpenCode that an orchestrator exists. So fleet
owns those files rather than leaving the user to copy them by hand.

The hard part is not writing them, it is not destroying work. A profile is a
plain Markdown file that a user is entitled to edit, and a naive upgrade would
silently overwrite exactly the reasoning they added. So every write records a
hash, and any file that no longer matches its recorded hash is treated as the
user's and left alone unless they pass --force.
"""

from __future__ import annotations

import hashlib
import json
import os
from typing import Any, Dict, List, NamedTuple, Optional

from fleet import api, config, herdr
from fleet.errors import FleetError

TEMPLATE_ROOT = "templates"

# What each install action did to one file, for reporting.
INSTALLED = "installed"
UPDATED = "updated"
UNCHANGED = "unchanged"
KEPT_MODIFIED = "kept, you modified it"
KEPT_PREEXISTING = "kept, already there before fleet"
REMOVED = "removed"
KEPT_MODIFIED_REMOVE = "kept, you modified it"


class FileResult(NamedTuple):
    relpath: str
    path: str
    action: str


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _read_if_present(path: str) -> Optional[str]:
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise FleetError(f"could not read {path}: {exc}") from exc


def template_text(relpath: str) -> str:
    """Read a bundled template by its path under templates/, such as
    "agents/orchestrator.md"."""
    from importlib import resources

    node = resources.files("fleet").joinpath(TEMPLATE_ROOT, relpath)
    return node.read_bytes().decode("utf-8")


def template_list() -> List[str]:
    """Every bundled template, as paths relative to templates/."""
    from importlib import resources

    root = resources.files("fleet").joinpath(TEMPLATE_ROOT)
    found: List[str] = []
    for kind in ("agents", "commands"):
        folder = root.joinpath(kind)
        if not folder.is_dir():
            continue
        for entry in folder.iterdir():
            if entry.name.endswith(".md"):
                found.append(f"{kind}/{entry.name}")
    return sorted(found)


def _load_record() -> Dict[str, Any]:
    try:
        with open(config.install_record_path(), encoding="utf-8") as handle:
            data = json.load(handle)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {"version": None, "files": {}}
    if not isinstance(data, dict) or not isinstance(data.get("files", {}), dict):
        return {"version": None, "files": {}}
    data.setdefault("version", None)
    return data


def _save_record(record: Dict[str, Any]) -> None:
    path = config.install_record_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(record, handle, indent=2, sort_keys=True)
        handle.write("\n")
    os.replace(tmp, path)


def _write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.replace(tmp, path)


def _relpath_to_kind(relpath: str) -> tuple:
    kind, _, name = relpath.partition("/")
    if kind not in ("agents", "commands") or not name:
        raise FleetError(f"malformed template path {relpath!r}")
    return kind, name


def install(force: bool = False, only: Optional[List[str]] = None) -> List[FileResult]:
    """Write any bundled template that is missing or safe to replace.

    A file is safe to replace when fleet wrote it and the user has not touched
    it since. Everything else is reported and left alone.
    """
    from fleet import __version__

    record = _load_record()
    results: List[FileResult] = []

    for relpath in template_list():
        if only and not any(relpath.startswith(prefix) for prefix in only):
            continue
        kind, name = _relpath_to_kind(relpath)
        path = config.target_path(kind, name)
        shipped = template_text(relpath)
        shipped_hash = _sha256(shipped)
        entry = record["files"].get(relpath, {})
        recorded_hash = entry.get("sha256")
        current = _read_if_present(path)

        if current is None:
            _write(path, shipped)
            action = INSTALLED
        elif _sha256(current) == shipped_hash:
            action = UNCHANGED
        elif recorded_hash is None and not force:
            # Present but fleet never wrote it, so it is the user's own file.
            action = KEPT_PREEXISTING
        elif recorded_hash is not None and _sha256(current) != recorded_hash and not force:
            action = KEPT_MODIFIED
        else:
            # Either fleet wrote this file and the user has not touched it, or
            # --force was passed. Both are safe to replace.
            _write(path, shipped)
            action = UPDATED

        if action not in (KEPT_PREEXISTING, KEPT_MODIFIED):
            record["files"][relpath] = {"sha256": shipped_hash, "version": __version__}
        results.append(FileResult(relpath, path, action))

    record["version"] = __version__
    _save_record(record)
    return results


def uninstall(force: bool = False) -> List[FileResult]:
    """Remove templates fleet wrote, leaving modified ones unless forced."""
    record = _load_record()
    results: List[FileResult] = []

    for relpath in sorted(record["files"]):
        kind, name = _relpath_to_kind(relpath)
        path = config.target_path(kind, name)
        current = _read_if_present(path)
        if current is None:
            continue
        recorded_hash = record["files"][relpath].get("sha256")
        if recorded_hash and _sha256(current) != recorded_hash and not force:
            results.append(FileResult(relpath, path, KEPT_MODIFIED_REMOVE))
            continue
        try:
            os.remove(path)
        except OSError as exc:
            raise FleetError(f"could not remove {path}: {exc}") from exc
        record["files"].pop(relpath, None)
        results.append(FileResult(relpath, path, REMOVED))

    _save_record(record)
    return results


def drift() -> List[FileResult]:
    """Report how each installed file relates to what fleet ships now.

    UNCHANGED means the file on disk matches the shipped template. UPDATED means
    the user edited it, so it is stale relative to any newer fleet release.
    """
    record = _load_record()
    seen: List[FileResult] = []
    for relpath in template_list():
        kind, name = _relpath_to_kind(relpath)
        path = config.target_path(kind, name)
        current = _read_if_present(path)
        shipped = template_text(relpath)
        if current is None:
            seen.append(FileResult(relpath, path, "missing"))
        elif current == shipped:
            seen.append(FileResult(relpath, path, UNCHANGED))
        elif record["files"].get(relpath, {}).get("sha256") == _sha256(current):
            seen.append(FileResult(relpath, path, "stale, safe to update"))
        else:
            seen.append(FileResult(relpath, path, KEPT_MODIFIED))
    return seen


# -- doctor -----------------------------------------------------------------


class Check:
    def __init__(self, name: str, ok: bool, detail: str = "", required: bool = True) -> None:
        self.name = name
        self.ok = ok
        self.detail = detail
        self.required = required

    def line(self) -> str:
        mark = "ok  " if self.ok else ("FAIL" if self.required else "warn")
        return f"  [{mark}] {self.name}" + (f": {self.detail}" if self.detail else "")


def doctor() -> List[Check]:
    """Check everything fleet depends on, and report what is off."""
    from fleet import __version__

    checks: List[Check] = [Check("fleet version", True, __version__)]

    binary = api.opencode_binary()
    checks.append(Check(
        "opencode on PATH",
        bool(binary),
        binary or "not found; install OpenCode or set FLEET_OPENCODE_BIN",
    ))

    if binary:
        try:
            info = api.api("get", "/api/info")
            checks.append(Check("opencode service", True, "reachable"))
            version = (info.get("data") or {}).get("version")
            if version:
                checks.append(Check("opencode version", True, str(version), required=False))
        except FleetError as exc:
            checks.append(Check("opencode service", False, str(exc)))

    herdr_path = herdr.herdr_binary()
    checks.append(Check(
        "herdr on PATH",
        bool(herdr_path),
        herdr_path or "not found; pane mode is unavailable, headless still works",
        required=False,
    ))
    checks.append(Check(
        "inside a herdr pane",
        herdr.in_herdr(),
        "yes" if herdr.in_herdr() else "no; pane mode needs this",
        required=False,
    ))

    state = config.state_dir()
    try:
        os.makedirs(state, exist_ok=True)
        probe = os.path.join(state, ".write-probe")
        with open(probe, "w", encoding="utf-8") as handle:
            handle.write("")
        os.remove(probe)
        checks.append(Check("state directory", True, state))
    except OSError as exc:
        checks.append(Check("state directory", False, f"{state}: {exc}"))

    for result in drift():
        detail = {
            UNCHANGED: "up to date",
            "stale, safe to update": "older than this fleet release, run `fleet upgrade`",
            KEPT_MODIFIED: "you modified it, upgrade will not touch it",
            "missing": "not installed, run `fleet install`",
        }.get(result.action, result.action)
        checks.append(Check(f"template {result.relpath}", result.action == UNCHANGED, detail,
                           required=False))

    try:
        from fleet.registry import Registry

        count = len(Registry().names())
        checks.append(Check("registry", True, f"{count} agent{'s' if count != 1 else ''} registered"))
    except FleetError as exc:
        checks.append(Check("registry", False, str(exc)))

    return checks


def doctor_exit_code(checks: List[Check]) -> int:
    return 1 if any(not c.ok and c.required for c in checks) else 0
