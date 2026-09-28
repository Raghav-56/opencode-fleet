"""Argument parsing and the process entry point."""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, List

from fleet import __version__, commands, config
from fleet import messaging as msg
from fleet.errors import FleetError
from fleet.registry import Registry

EPILOG = """\
examples:
  fleet spawn reviewer --pane            start a watchable agent in a pane
  fleet spawn auditor --headless         start a quiet agent
  fleet ask auditor "check the READMEs"  send work, wait, print the reply
  fleet ask auditor --file brief.md      same, brief from a file
  fleet send auditor "start here"        send without waiting
  fleet wait auditor                     block until it is free again
  fleet collect auditor                  read the latest reply
  fleet list                             who exists, and their status
  fleet kill auditor --pane              clean up

agent profiles: run `fleet install` to add the orchestrator, worker and
analyst agents, then `/orchestrate` to hand a session to the coordinator.
"""


def _add_text_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("text", nargs="*", help="the message to send")
    parser.add_argument("--file", "-f", help="read the message from a file")
    parser.add_argument("--prefix", help="text prepended to the message, after a blank line")
    parser.add_argument("--queue", action="store_true",
                        help="wait for the running turn instead of steering it")
    parser.add_argument("--no-resume", action="store_true",
                        help="enqueue without waking the session (send only)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fleet",
        description="Run and coordinate a fleet of OpenCode agents.",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"fleet {__version__}")
    sub = parser.add_subparsers(dest="command", required=True, metavar="<command>")

    # -- agents --

    spawn = sub.add_parser("spawn", help="start a new agent")
    spawn.add_argument("name", help="lowercase, digits, - and _, starting with a letter")
    spawn.add_argument("--dir", help="working directory (default: the current one)")
    spawn.add_argument("--agent", help="OpenCode agent profile for the session")
    spawn.add_argument("--model", help="provider/model#variant")
    spawn.add_argument("--kind", help="agent binary for pane mode")
    mode = spawn.add_mutually_exclusive_group()
    mode.add_argument("--pane", action="store_true", help="run in a watchable herdr pane")
    mode.add_argument("--headless", action="store_true", help="run with no terminal (default)")
    spawn.add_argument("--startup", type=int, default=60, help="seconds to wait for pane readiness")
    spawn.set_defaults(func=commands.spawn, mode=None)

    send = sub.add_parser("send", help="send work without waiting for a reply")
    send.add_argument("name")
    _add_text_args(send)
    send.add_argument("--timeout", type=int, default=300, help="seconds, for pane fallback")
    send.set_defaults(func=commands.send)

    ask = sub.add_parser("ask", help="send work and print the reply")
    ask.add_argument("name")
    _add_text_args(ask)
    ask.add_argument("--timeout", type=int, default=600, help="seconds to wait for the turn to settle")
    ask.add_argument("--poll", type=float, default=1.5, help="seconds between status checks")
    ask.set_defaults(func=commands.ask)

    wait = sub.add_parser("wait", help="block until an agent reaches a state")
    wait.add_argument("name")
    wait.add_argument("--until", nargs="+", choices=list(msg.WAITABLE),
                      help="states to stop on (default: idle or failed)")
    wait.add_argument("--timeout", type=int, default=600)
    wait.add_argument("--poll", type=float, default=2.0)
    wait.set_defaults(func=commands.wait)

    collect = sub.add_parser("collect", help="print an agent's latest reply")
    collect.add_argument("name")
    collect.add_argument("--full", action="store_true", help="print the whole session")
    collect.add_argument("--limit", type=int, default=60)
    collect.set_defaults(func=commands.collect)

    listing = sub.add_parser("list", help="show every agent")
    listing.add_argument("--json", action="store_true", help="machine-readable output")
    listing.set_defaults(func=commands.list_agents)

    status = sub.add_parser("status", help="show one agent in detail")
    status.add_argument("name")
    status.set_defaults(func=commands.status)

    broad = sub.add_parser("broadcast", help="send the same message to several agents")
    _add_text_args(broad)
    broad.add_argument("--to", nargs="+", help="targets (default: every registered agent)")
    broad.set_defaults(func=commands.broadcast)

    kill = sub.add_parser("kill", help="delete one or more agents")
    kill.add_argument("names", nargs="+")
    kill.add_argument("--pane", action="store_true", help="also close its terminal pane")
    kill.set_defaults(func=commands.kill)

    prune = sub.add_parser("prune", help="forget agents whose session or pane is gone")
    prune.set_defaults(func=commands.prune)

    # -- installation --

    installer = sub.add_parser("install", help="write the bundled agent profiles")
    installer.add_argument("--force", action="store_true", help="overwrite files you changed")
    installer.add_argument("--only", nargs="+", help="limit to these template kinds or names")
    installer.set_defaults(func=commands.do_install)

    upgrade = sub.add_parser("upgrade", help="refresh the bundled agent profiles")
    upgrade.add_argument("--force", action="store_true", help="overwrite files you changed")
    upgrade.add_argument("--only", nargs="+", help="limit to these template kinds or names")
    upgrade.set_defaults(func=commands.do_upgrade)

    uninstaller = sub.add_parser("uninstall", help="remove the bundled agent profiles")
    uninstaller.add_argument("--force", action="store_true", help="remove files you changed")
    uninstaller.set_defaults(func=commands.do_uninstall)

    doctor = sub.add_parser("doctor", help="check everything fleet depends on")
    doctor.set_defaults(func=commands.do_doctor)

    templates = sub.add_parser("templates", help="show the bundled agent profiles")
    templates.add_argument("name", nargs="?", help="limit to profiles whose name ends with this")
    templates.add_argument("--paths", action="store_true", help="print install paths instead of content")
    templates.set_defaults(func=commands.do_templates)

    configer = sub.add_parser("config", help="show or change settings")
    configer.add_argument("key", nargs="?", choices=sorted(config.DEFAULTS))
    configer.add_argument("value", nargs="?")
    configer.set_defaults(func=_config)

    return parser


def _config(reg: Registry, args: Any) -> int:
    if not args.key:
        print(json.dumps(config.load_config(), indent=2, sort_keys=True))
        return 0
    if args.value is None:
        current = config.load_config().get(args.key)
        print("" if current is None else current)
        return 0
    value: Any = args.value
    if value.lower() in ("none", "null", ""):
        value = None
    elif value.lower() in ("true", "false"):
        value = value.lower() == "true"
    path = config.write_config({args.key: value})
    print(f"{args.key} = {value}   (written to {path})")
    return 0


def _resolve_mode(args: argparse.Namespace) -> None:
    if args.command != "spawn":
        return
    if args.pane:
        args.mode = "pane"
    else:
        args.mode = "headless"


def main(argv: List[str] = None) -> int:
    parser = build_parser()

    # argparse cannot match a greedy nargs="*" positional that follows an
    # option, so plain words trailing a flag are folded back into the message.
    # Anything starting with a dash is left alone, so a mistyped flag still
    # fails loudly instead of being swallowed into the prompt.
    args, extra = parser.parse_known_args(argv)
    leftover = [w for w in extra if not w.startswith("-")]
    unknown = [w for w in extra if w.startswith("-")]
    if unknown:
        parser.error(f"unrecognized arguments: {' '.join(unknown)}")
    if leftover:
        if not hasattr(args, "text"):
            parser.error(f"unexpected arguments: {' '.join(leftover)}")
        args.text = list(args.text) + leftover

    _resolve_mode(args)

    needs_registry = args.command not in (
        "install", "uninstall", "upgrade", "doctor", "templates", "config",
    )
    reg = Registry() if needs_registry else None
    try:
        return args.func(reg, args)
    except FleetError as exc:
        print(f"fleet: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:  # `fleet list | head`
        try:
            sys.stdout.close()
        finally:
            os._exit(0)
    except Exception as exc:  # a tool an agent calls should fail with a message
        if os.environ.get("FLEET_DEBUG"):
            raise
        print(f"fleet: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
