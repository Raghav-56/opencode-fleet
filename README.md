# opencode-fleet

Run and coordinate a fleet of [OpenCode](https://opencode.ai) agents from the
command line. One agent spawns and messages others, and you can watch any worker
in a real terminal when you want to.

```sh
fleet spawn reviewer --pane          # a watchable terminal
fleet spawn auditor --headless       # quiet, no terminal
fleet ask auditor "check the READMEs"
fleet list
```

## Install

```sh
pipx install opencode-fleet          # or: uv tool install opencode-fleet
fleet install                        # write the agent profiles
fleet doctor                         # check the environment
```

`fleet install` writes three agent profiles and one slash command into your
OpenCode config. That step is not optional decoration: without the profiles,
nothing tells OpenCode an orchestrator exists, and the CLI has nothing to
orchestrate.

Requires Python 3.9 or newer. fleet itself imports only the standard library. It
shells out to `opencode`, and to `herdr` if you want pane mode.

## The three agents

`orchestrator` splits a request into independent parts, spawns a worker for each,
collects the replies, and synthesizes one answer. It can only run `fleet` and
write to `/tmp`, so it coordinates rather than doing the work itself.

`worker` takes one delegated task and reports a complete answer. It can message
peers in the fleet, so two workers can talk to each other.

`analyst` investigates and reports without changing any file.

Switch a session over with `/orchestrate`, or select `orchestrator` as the
session's agent.

## Commands

| Command | What it does |
| --- | --- |
| `fleet spawn <name>` | Start an agent. `--pane` for a watchable terminal, `--headless` (default) for quiet |
| `fleet ask <name> <text>` | Send work, wait for the turn to settle, print the reply |
| `fleet send <name> <text>` | Send work without waiting |
| `fleet wait <name>` | Block until an agent is free again |
| `fleet collect <name>` | Print an agent's latest reply |
| `fleet broadcast <text> --to a b` | Send the same message to several agents |
| `fleet list` | Every agent, its mode, status, and cost |
| `fleet status <name>` | One agent in detail, as JSON |
| `fleet kill <name>...` | Delete one or more agents |
| `fleet prune` | Forget agents whose session or pane died |
| `fleet install` / `uninstall` / `upgrade` | Manage the bundled agent profiles |
| `fleet doctor` | Check the environment |
| `fleet templates` | Show the bundled profiles |
| `fleet config [key [value]]` | Read or change settings |

Long messages belong in a file, which keeps quoting sane:

```sh
fleet ask reviewer --file /tmp/brief.md
cat brief.md | fleet ask reviewer
```

`fleet ask` waits for a real settle rather than for the server's `wait`
endpoint, which returns immediately for a session that has not picked the work
up yet. When a wait times out, fleet prints the partial reply and names any tool
calls that started and never returned, which is usually a blocked permission
prompt.

## Agents talking to each other

Any agent in the fleet can reach any other by name:

```sh
fleet send bob "check the schema first"
fleet collect bob
```

The `orchestrator` uses this to fan work out. Give workers the `worker` or
`analyst` profile and they can message each other, so a two-step conversation
between workers is just a `fleet send` followed by a `fleet collect`.

## Pane mode

`fleet spawn <name> --pane` splits your current pane and starts a real agent in
it. You get a terminal you can watch, scroll back through, and take over. The
session id is discovered from Herdr, so the rest of the commands work exactly as
they do for a headless agent.

Pane mode needs to run from inside a Herdr-managed pane. Herdr classifies agent
lifecycle states and can see approval prompts that the message log cannot, so
pane agents take their status from Herdr.

## Your edits are safe

The agent profiles are plain Markdown that you are entitled to edit. So `fleet
install` records a hash of what it wrote, and `fleet upgrade` leaves any file
that no longer matches alone rather than overwriting your work. It tells you
which files it kept, and `--force` overrides.

```sh
fleet upgrade
#   unchanged      agents/analyst.md
#   kept, you modified it   agents/worker.md
```

`fleet uninstall` follows the same rule: it removes what it wrote and keeps what
you changed.

## Configuration

```sh
fleet config                        # show everything
fleet config default_agent worker   # profile new sessions get
fleet config default_model opencode/longcat-2.5-preview-free
```

Settings live in `~/.config/fleet/config.json`. The agent registry lives in
`~/.local/state/fleet/registry.json`.

| Variable | Purpose |
| --- | --- |
| `FLEET_STATE_DIR` | Where the registry lives |
| `FLEET_CONFIG_DIR` | Where settings and the install record live |
| `FLEET_OPENCODE_CONFIG_DIR` | Where agent profiles are written |
| `FLEET_OPENCODE_BIN` | Full path to the `opencode` CLI, overriding PATH |
| `FLEET_DEBUG` | Re-raise unexpected exceptions instead of printing a message |

## Design notes

**No `effect: ask` in a bundled profile.** An agent with `ask` in its permission
rules blocks forever when nobody answers the approval prompt, which is what an
unattended worker always is. All three profiles override the inherited `ask`
rules on `.env` reads and outside-workspace paths, replacing them with allow or
deny so a blocked call fails fast and can be reported.

**Status comes from the message log.** The server leaves `Session.Info.time.idle`
unpopulated, so that field cannot say whether a session is busy. fleet reads the
message log instead: a streaming assistant message means working, an `idle`
marker after the last assistant message means settled.

**The registry lock is per operation, not per process.** A coordinator holds a
long `fleet ask` while the worker it is waiting on may itself call `fleet spawn`.
A process-lifetime lock deadlocks that pair.

**Nothing is a runtime dependency.** fleet shells out to `opencode api` rather
than speaking HTTP, so it inherits the same service discovery and authentication
the TUI uses and works with a remote server without knowing anything about it.

## Development

```sh
uv venv && uv pip install -e ".[dev]"
uv run pytest
```

The suite runs offline against a stub `opencode` binary, so it costs no model
calls. `tests/test_messaging.py` covers the message-log logic, which is where the
subtle bugs have been.

## Licence

MIT.
