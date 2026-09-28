"""The operations behind each fleet subcommand."""

from __future__ import annotations

import os
import re
import sys
import time
from typing import Any, Dict, List

from fleet import api, config, herdr, install as install_mod
from fleet import messaging as msg
from fleet.errors import FleetError
from fleet.registry import Registry

# Herdr requires agent names to match this, so fleet uses the same rule and
# every name stays usable as a pane agent later.
NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")

EXIT_TIMEOUT = 124


# -- shared helpers ---------------------------------------------------------


def refresh(reg: Registry, name: str) -> Dict[str, Any]:
    """Reconcile a registry record with live state and classify it.

    Never call this while holding the registry lock: it talks to herdr and to
    the OpenCode service.
    """
    record = reg.require(name)

    if record.get("mode") == "pane":
        pane = record.get("pane")
        if not pane or not herdr.pane_alive(pane):
            record["status"] = msg.DEAD
            return record
        session = herdr.pane_session(pane)
        # A human may switch sessions inside the pane, so re-read the id every
        # time rather than trusting the one captured at spawn.
        if session and session != record.get("session"):
            record["session"] = session
            reg.update(name, session=session)

    session = record.get("session")
    if not session:
        record["status"] = msg.WAITING
        return record

    if api.get_session(session) is None:
        record["status"] = msg.DEAD
        return record

    if record.get("mode") == "pane":
        # Herdr already classifies the live agent, and it notices approval and
        # question prompts that the message log cannot see.
        record["status"] = msg.IDLE
        try:
            match = next((a for a in herdr.agents() if a.get("pane_id") == record.get("pane")), None)
            if match and match.get("agent_status") in ("working", "blocked", "failed"):
                record["status"] = match["agent_status"]
        except FleetError:
            pass
        return record

    messages = api.messages(session)
    _, state = msg.turn_state(messages, 0.0)
    if state == msg.PENDING:
        # Work admitted but not yet picked up is invisible in the message log.
        record["status"] = msg.WAITING if api.pending_inbox(session) else msg.live_state(messages)
    else:
        record["status"] = state
    return record


def _read_prompt(args: Any) -> str:
    if getattr(args, "file", None):
        try:
            with open(args.file, encoding="utf-8") as handle:
                return handle.read().strip()
        except OSError as exc:
            raise FleetError(f"could not read {args.file}: {exc}") from exc
    text = " ".join(getattr(args, "text", []) or []).strip()
    if text:
        return text
    if not sys.stdin.isatty():
        return sys.stdin.read().strip()
    return ""


def _prepare_text(args: Any) -> str:
    text = _read_prompt(args)
    if not text:
        raise FleetError("nothing to send. Pass text, --file PATH, or pipe it on stdin.")
    if getattr(args, "prefix", None):
        text = f"{args.prefix}\n\n{text}"
    return text


def _require_live(reg: Registry, name: str) -> Dict[str, Any]:
    record = refresh(reg, name)
    if record["status"] == msg.DEAD:
        raise FleetError(
            f"{name!r} is not running; its session or pane is gone. "
            f"Run `fleet kill {name}` and spawn it again."
        )
    return record


# -- spawn ------------------------------------------------------------------


def spawn(reg: Registry, args: Any) -> int:
    name = args.name
    if not NAME_RE.match(name):
        raise FleetError(
            f"invalid name {name!r}: use lowercase letters, digits, '-' and '_', "
            "at most 32 characters, starting with a letter"
        )

    settings = config.load_config()
    directory = os.path.abspath(args.dir or settings.get("default_directory") or os.getcwd())
    if not os.path.isdir(directory):
        raise FleetError(f"no such directory: {directory}")

    mode = args.mode
    if mode == "pane" and not herdr.in_herdr():
        raise FleetError(
            "pane mode needs a Herdr-managed pane. Run this from inside Herdr, or use --headless."
        )

    record = {
        "session": None,
        "mode": mode,
        "pane": None,
        "kind": args.kind or settings["default_kind"],
        "agent": args.agent or settings["default_agent"],
        "directory": directory,
        "title": name,
        "model": args.model or settings.get("default_model"),
        "created": int(time.time()),
        "status": msg.WAITING,
    }

    # Claim the name before creating anything, so two concurrent spawns of the
    # same name cannot both build a session and leave one unregistered.
    reg.insert(name, record)
    try:
        if mode == "pane":
            pane = herdr.split_pane(directory)
            record["pane"] = pane
            try:
                herdr.start_agent(name, record["kind"], pane, args.startup)
            except FleetError:
                # Do not leave a half-built pane for the user to clean up.
                herdr.close_pane(pane)
                raise
            session = herdr.pane_session(pane)
            record["session"] = session
            record["status"] = msg.IDLE if session else msg.WAITING
        else:
            record["session"] = api.create_session(
                directory, name, record["agent"], record["model"]
            )
            record["status"] = msg.IDLE
    except BaseException:
        reg.drop(name)
        if record.get("session"):
            api.delete_session(record["session"])
        raise

    reg.put(name, record)
    shown = record["session"] or "(waiting for first turn)"
    print(f"{name}\t{mode}\t{shown}\t{record['pane'] or '-'}")
    return 0


# -- messaging --------------------------------------------------------------


def send(reg: Registry, args: Any) -> int:
    record = _require_live(reg, args.name)
    text = _prepare_text(args)

    session = record.get("session")
    if not session:
        # A pane agent that has not run yet has no session to address, so fall
        # back to typing into its terminal.
        if record["mode"] != "pane":
            raise FleetError(f"{args.name!r} has no session id")
        herdr.prompt(args.name, text, args.timeout)
        reg.update(args.name, last_prompt=int(time.time()))
        print(f"sent to {args.name} via pane")
        return 0

    admitted = api.send_synthetic(session, text, args.queue, not args.no_resume)
    reg.update(
        args.name,
        last_sent=int(admitted.get("time", {}).get("created", time.time() * 1000) / 1000),
        last_msg=admitted.get("id"),
    )
    print(admitted.get("id", ""))
    return 0


def ask(reg: Registry, args: Any) -> int:
    record = _require_live(reg, args.name)
    text = _prepare_text(args)
    if args.no_resume:
        # Enqueueing without waking means nothing will ever reply, so waiting
        # here would only burn the timeout.
        raise FleetError(
            "ask cannot use --no-resume, since the session would never wake. Use `fleet send`."
        )

    session = record.get("session")
    if not session:
        raise FleetError(
            f"{args.name!r} has no session yet. Run `fleet send` to start it, or use a headless agent."
        )

    admitted = api.send_synthetic(session, text, args.queue, True)
    start = admitted.get("time", {}).get("created", 0)
    deadline = time.time() + args.timeout

    while True:
        messages = api.messages(session)
        assistants, state = msg.turn_state(messages, start)
        if assistants and state in (msg.IDLE, msg.FAILED):
            break

        if time.time() > deadline:
            partial = msg.join_reply(assistants)
            stuck = msg.stuck_tool_calls(messages, start)
            if partial:
                print(partial)
                print(
                    f"\n[fleet: {args.name} did not settle within {args.timeout}s, "
                    "partial reply above]",
                    file=sys.stderr,
                )
            if stuck:
                print(f"[fleet: started and never returned: {', '.join(stuck)}]", file=sys.stderr)
                print(f"[fleet: {msg.HANG_HINT}]", file=sys.stderr)
                return EXIT_TIMEOUT
            if partial:
                return EXIT_TIMEOUT
            raise FleetError(f"{args.name} did not start working within {args.timeout}s")

        if record["mode"] == "pane" and not herdr.pane_alive(record.get("pane")):
            raise FleetError(f"the pane for {args.name} closed mid-turn")
        time.sleep(args.poll)

    reply = msg.join_reply(assistants)
    if reply:
        print(reply)

    if state == msg.FAILED:
        print(
            f"[fleet: the {args.name} turn failed; read its session log for the error]",
            file=sys.stderr,
        )
        return 1

    reg.update(args.name, status=msg.IDLE, last_turn_at=int(time.time()))
    return 0


def wait(reg: Registry, args: Any) -> int:
    record = _require_live(reg, args.name)
    until = set(args.until) if args.until else {msg.IDLE, msg.FAILED}
    deadline = time.time() + args.timeout

    while True:
        record = refresh(reg, args.name)
        if record.get("status") in until:
            print(record["status"])
            return 1 if record["status"] == msg.FAILED else 0
        if time.time() > deadline:
            print(
                f"fleet: {args.name} was {record.get('status')} after {args.timeout}s, still running",
                file=sys.stderr,
            )
            return EXIT_TIMEOUT
        time.sleep(args.poll)


def broadcast(reg: Registry, args: Any) -> int:
    text = _prepare_text(args)
    names = args.to or reg.names()
    if not names:
        print("no agents registered.")
        return 0

    failures = 0
    for name in names:
        try:
            record = refresh(reg, name)
        except FleetError as exc:
            # One bad target should not abandon the rest of the list.
            print(f"{name}\tSKIP\t{exc}")
            failures += 1
            continue
        if not record.get("session"):
            print(f"{name}\tSKIP\tno session yet")
            failures += 1
            continue
        admitted = api.send_synthetic(record["session"], text, True, True)
        print(f"{name}\t{admitted.get('id')}")
    return 1 if failures else 0


def collect(reg: Registry, args: Any) -> int:
    record = _require_live(reg, args.name)
    if not record.get("session"):
        raise FleetError(f"{args.name!r} has no session to read yet")
    messages = api.messages(record["session"], limit=args.limit)
    picks = messages if args.full else msg.last_turn(messages)
    if not picks:
        print(f"[fleet: {args.name} has no assistant reply yet]", file=sys.stderr)
        return 1
    for message in picks:
        body = msg.text_of(message)
        if body:
            print(body)
    return 0


# -- inspection -------------------------------------------------------------


def _enrich(reg: Registry, name: str) -> Dict[str, Any]:
    record = dict(refresh(reg, name))
    info = api.get_session(record["session"]) if record.get("session") else None
    record["name"] = name
    record["cost"] = (info or {}).get("cost")
    record["tokens"] = (info or {}).get("tokens")
    return record


def list_agents(reg: Registry, args: Any) -> int:
    names = reg.names()
    if not names:
        print("no agents registered. Start one with `fleet spawn <name>`.")
        return 0

    records = []
    for name in names:
        record = _enrich(reg, name)
        keep = {k: record[k] for k in ("status", "session", "cost", "tokens")
                if record.get(k) is not None}
        reg.update(name, **keep)
        records.append(record)

    if args.json:
        import json

        print(json.dumps(records, indent=2, sort_keys=True))
        return 0

    headers = ("NAME", "MODE", "STATUS", "COST", "SESSION")
    rows = [headers]
    for record in records:
        cost = record.get("cost")
        rows.append((
            record["name"],
            record.get("mode", "?"),
            record.get("status", msg.UNKNOWN),
            f"${cost:.4f}" if isinstance(cost, (int, float)) and cost else "-",
            (record.get("session") or "-")[:24],
        ))
    widths = [max(len(row[i]) for row in rows) for i in range(len(headers))]
    for row in rows:
        print("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip())
    return 0


def status(reg: Registry, args: Any) -> int:
    record = _enrich(reg, args.name)
    reg.update(args.name, **{k: record[k] for k in ("status", "session")
                             if record.get(k) is not None})
    import json

    record["description"] = msg.describe_status(record.get("status"))
    print(json.dumps(record, indent=2, sort_keys=True))
    return 0 if record.get("status") not in (msg.DEAD, msg.FAILED) else 1


def prune(reg: Registry, args: Any) -> int:
    dropped = []
    for name in reg.names():
        if refresh(reg, name).get("status") == msg.DEAD:
            reg.drop(name)
            dropped.append(name)
    print(f"pruned {len(dropped)}: {', '.join(dropped) or 'none'}")
    return 0


def kill(reg: Registry, args: Any) -> int:
    failures = 0
    for name in args.names:
        try:
            record = reg.require(name)
        except FleetError as exc:
            # One bad name should not strand the rest of the list.
            print(f"{name}\tSKIP\t{exc}")
            failures += 1
            continue
        did = []
        if record.get("session"):
            did.append("session" if api.delete_session(record["session"]) else "session(already gone)")
        if args.pane and record.get("pane"):
            did.append("pane" if herdr.close_pane(record["pane"]) else "pane(kept)")
        reg.drop(name)
        print(f"{name} killed: {', '.join(did) or 'nothing to clean up'}")
    return 1 if failures else 0


# -- install lifecycle ------------------------------------------------------


def _print_results(results: List[install_mod.FileResult]) -> int:
    if not results:
        print("nothing to do")
        return 0
    changed = 0
    for result in results:
        print(f"  {result.action:<28} {result.relpath}  ->  {result.path}")
        if result.action in (install_mod.INSTALLED, install_mod.UPDATED, install_mod.REMOVED):
            changed += 1
    kept = [r for r in results if "kept" in r.action]
    if kept:
        print(f"\n  {len(kept)} file(s) left alone because you changed them. "
              "Re-run with --force to overwrite.")
    return 0


def do_install(reg: Registry, args: Any) -> int:
    return _print_results(install_mod.install(force=args.force, only=args.only))


def do_upgrade(reg: Registry, args: Any) -> int:
    return _print_results(install_mod.install(force=args.force, only=args.only))


def do_uninstall(reg: Registry, args: Any) -> int:
    return _print_results(install_mod.uninstall(force=args.force))


def do_doctor(reg: Registry, args: Any) -> int:
    checks = install_mod.doctor()
    for check in checks:
        print(check.line())
    failed = [c for c in checks if not c.ok and c.required]
    if failed:
        print(f"\n  {len(failed)} required check(s) failed.")
    return install_mod.doctor_exit_code(checks)


def do_templates(reg: Registry, args: Any) -> int:
    for relpath in install_mod.template_list():
        if args.name and not relpath.endswith(args.name):
            continue
        if args.paths:
            print(config.target_path(*install_mod._relpath_to_kind(relpath)))
        else:
            print(f"### {relpath}")
            print(install_mod.template_text(relpath))
            print()
    return 0
