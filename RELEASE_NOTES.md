First release.

`fleet` runs and coordinates a fleet of OpenCode agents. One agent can spawn and
message others, and you can watch any worker in a real terminal when you want to.

```sh
pipx install opencode-fleet      # or: uv tool install opencode-fleet
fleet install                    # write the bundled agent profiles
fleet doctor                     # check the environment
```

`fleet install` is not optional decoration. Without the profiles, nothing tells
OpenCode an orchestrator exists, and the CLI has nothing to orchestrate.

## The agents

`orchestrator` splits a request into independent parts, spawns a worker for each,
collects the replies, and synthesizes one answer. It can only run `fleet` and
write to `/tmp`, so it coordinates rather than doing the work itself. Use
`/orchestrate` to hand a session over to it.

`worker` takes one delegated task and reports a complete answer. It can message
peers in the fleet, so two workers can talk to each other.

`analyst` investigates and reports without changing any file.

## Usage

```sh
fleet spawn reviewer --pane          # a watchable terminal
fleet spawn auditor --headless       # quiet, no terminal
fleet ask auditor "check the READMEs"
fleet collect auditor
fleet list
```

## Design notes worth knowing

**No `effect: ask` in any bundled profile.** An agent with `ask` in its permission
rules blocks forever when nobody answers the approval prompt, which is what an
unattended worker always is. All three profiles override the inherited `ask`
rules on `.env` reads and outside-workspace paths, replacing them with allow or
deny, so a blocked call fails fast and can be reported.

**Status comes from the message log.** The server leaves
`Session.Info.time.idle` unpopulated, so that field cannot say whether a session
is busy. `fleet` reads the message log instead: a streaming assistant message
means working, an `idle` marker after the last assistant message means settled.
Work queued with `resume: false` never reaches the log at all, so that is read
from the inbox.

**Your edits to the profiles are safe.** `fleet install` records a hash of what it
wrote, and `fleet upgrade` leaves any file that no longer matches alone rather
than overwriting your work. `fleet doctor` reports the drift so you find out
before upgrading rather than after.

**Nothing is a runtime dependency.** `fleet` shells out to `opencode api` rather
than speaking HTTP, so it inherits the same service discovery and authentication
the TUI uses, and works against a remote server without knowing anything about
it.

## Requirements

Python 3.9 or newer. The library is tested on 3.9, 3.12, and 3.13.
