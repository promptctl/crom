---
name: crom
description: Drive a real Chrome through the crom CLI — a per-project profile on a CDP port that never moves. Use when the task needs a browser you control (navigate, click, screenshot, read a page behind a login, wire chrome-devtools-mcp, attach Playwright or Puppeteer over CDP), when the repo has a `.crom.toml`, when the user says crom, or when something already holds port 9222. Not for plain HTTP fetching — WebFetch is cheaper and does not need a browser.
---

# Driving Chrome with crom

crom hands a project its own Chrome: its own user-data-dir, its own flags, and a CDP
port assigned once and never moved. You get a browser you can reach at the same number
tomorrow, without fighting whatever else on this machine also wanted 9222.

Three words, and crom's `--help` uses them everywhere:

- **profile** — one Chrome user-data-dir plus the CDP port crom assigned it.
- **namespace** — the profiles belonging to one project, so two projects never collide.
- **ref** — how you name a profile. `dev` means dev in the namespace you are standing
  in; `myapp/dev` names it from anywhere on the machine.

Which namespace you are standing in is decided by the directory you run from: `user`,
unless a `.crom.toml` sits here or above. `crom config` always says which.

## The happy path

```bash
crom up              # bring up `default` here; idempotent, prints the CDP URL
crom mcp             # write .mcp.json wiring chrome-devtools-mcp at that profile
```

If `crom` is not on PATH, it is not installed — say so and point at
<https://github.com/promptctl/crom> rather than reaching for a different browser.

Writing `.mcp.json` does not load the server into the session you are already in.
Tell the user to restart Claude Code and approve it — do not loop trying to call a
browser tool that is not there yet.

For a non-MCP client, `crom port` prints the number alone and `eval "$(crom env)"`
puts `CROM_PORT` and `CROM_CDP_URL` in the environment.

A profile's first launch copies the user's real Chrome profile, so the browser arrives
with their logins and extensions. `--seed fresh` on `init` or `add` gets an empty one
instead — reach for it whenever the work should not be signed in as them.

`crom init` gives the project its own namespace, but it writes a file into their repo.
If you just need a browser for one task, the `user` namespace already has one and
`crom up` from anywhere brings it up. Run `init` when the project is going to keep a
browser, or when the user asked for it.

## Connect on `ready`, never on `running`

`running: true` says a process holds the profile directory. That is the lights being on.
`state: "ready"` says a browser answered on the port — that is someone opening the door.
`unreachable` is lights on and nobody answering: a browser still starting up, one
shutting down, one wedged, or a stranger holding the port. Silence reads the same
whichever it is, so crom does not pretend to tell them apart.

Connect to an `unreachable` profile and you hang. Not fail — hang, with no error to
read, at whatever turn you happen to be on.

You will meet this late and it will not announce itself. It will look like a
chrome-devtools-mcp call that never comes back, and the thought will be *"it must still
be starting — try that again."* That is the moment. Run `crom status <ref>` instead: it
prints what the port actually did or said, and it degrades honestly — a browser that
will not answer costs you the browser build, the websocket and the tab list, and costs
you none of the pids. Act on what it reports, not on a retry.

The port is the stable handle; that is the whole point of crom. The browser websocket
URL in `crom status` takes a new value every single restart, so read it at the moment
you connect and never store it. A saved websocket URL is right until the first restart
and silently wrong forever after.

## `crom up` converges, and convergence has a victim

`crom up` brings a profile onto whatever its config resolves to *right now*. A browser
running something else is stopped and started again. That is the feature: edit a flag in
`.crom.toml`, run `crom up`, and the edit is live.

The stop takes the tabs, the logins and the unsaved work of whoever was using that
browser — and none of that appears in any config file. crom converges the things it can
see; the whole cost lands on the things it cannot.

So if you did not launch the browser, or you are not certain the user is finished with
it, use `crom up --no-restart`. It names what drifted and leaves the session standing.

The temptation never arrives as *"let me kill their browser."* It arrives as *"something
is off with this profile — I'll just `crom up` to get it into a known state."* A known
state is exactly what you would be trading their open work for. Ask `crom config <ref>`
what actually differs first; it prints every flag with the layer that supplied it. Drift
you can name is usually drift you can leave alone.

## Four commands you do not reach for on your own initiative

- `crom down --all` sweeps **every namespace on the machine**, not the project you are
  standing in. A browser someone is using in another checkout is in range. Run
  `crom list --running --all` first — that listing is precisely what the sweep will
  take down.
- `crom rm <ref>` stops the profile, undeclares it, releases its port, and deletes its
  data. The logins go with it. `--keep-data` undeclares and leaves the directory.
- `crom release <key>` hands a port back. It goes to the next profile that asks for one
  and does not come back.
- `crom clean <path>` deletes a staging directory. A seed running *right now* leaves
  evidence identical to an abandoned one, and crom cannot tell the two apart — the
  prompt names the size so a person can.

Each of these is a request, not a repair. Do them when the user asked for them; propose
them otherwise. And `--yes` on `rm` and `clean` skips the prompt that is the last thing
standing between a mistyped ref and someone's logins, so type it only for the specific
removal the user asked for.

## Do not launch Chrome yourself

The moment crom feels like it is in the way — a flag it refuses, a port you would rather
pick — the thought is *"I'll just run Chrome with `--remote-debugging-port=9222` for
this one thing."* That is the exact collision crom exists to prevent, and the machine it
breaks is the user's: whatever else was on 9222 either dies or silently hands you its
browser, and you will not be able to tell which.

crom owns `--user-data-dir`, `--remote-debugging-port`, `--remote-debugging-pipe`,
`--enable-features` and `--disable-features`, and refuses them in a config's `flags`.
Everything else is yours. Put switches under `[profiles.<name>].flags`, remove an
inherited one with `drop_flags = ["--disable-sync"]`, toggle a Chrome feature with
`features = { SomeFeature = false }`. For a throwaway, declare a profile rather than
reaching around the tool:

```bash
crom add scratch --seed fresh --flag '--headless=new'
```

**`crom config --help` is the full reference for every key a config file may set.**
Read it there rather than guessing at the schema — it is current and this file is not.

## Reading crom from a script

`--json` is available on `up`, `down`, `restart`, `show`, `list`, `status`, `config`
and `doctor`. The other commands answer in prose only. Exit codes are a contract: `0`
success, `1` failure, `2` bad usage, `3` no such profile or namespace, `4` a port or
declaration conflict.

A failed `--json` command also puts one envelope on stdout:
`{"error": {"code", "kind", "reason", "fields", "message"}}`. Branch on `reason`, fall
back to `kind` when you meet one you do not know — `kind` is coarse on purpose. `fields`
carries what crom looked up on the way to refusing, as data rather than as English.

`message` is the one key not to parse. It is written for a person and free to be
reworded; everything a script needs is already in `reason` and `fields`. A regex over
`message` is a test that passes today and breaks on a rewrite nobody thought was a
breaking change.

Two shapes that are not the envelope. `crom list --json` returns an element carrying
`error` for a declaration it could not resolve, so check for `error` before reading
`port` — one broken declaration is reported rather than sinking a listing you are
running *because* something is wrong. And `crom down --all --json` answers with its
array of rows even when it failed; the reason travels on the row that earned it, under
`failure`.

## The surface

| What you want to know | Command |
| --- | --- |
| What exists here, on what port, in what state | `crom list` (`--all` every namespace, `--running` only live ones) |
| Bring a profile up | `crom up [ref]` — idempotent; `--no-restart` to spare a live session |
| Wire chrome-devtools-mcp at one | `crom mcp [ref]` — writes `.mcp.json` here |
| Just the port | `crom port [ref]` |
| Shell exports | `eval "$(crom env [ref])"` |
| What is *actually* on the port right now | `crom status [ref]` — pids, browser build, websocket, open tabs |
| What `crom up` will run, flag by flag | `crom config [ref]` |
| Give this project a namespace | `crom init [--seed fresh]` |
| Declare another profile | `crom add <name> [--seed …] [--flag …] [--port …]` |
| Stop one, or bring its window forward | `crom down [ref]` / `crom show [ref]` |
| Where crom's own state has leaked | `crom doctor` |

Every command asks for a state rather than a change, so asking twice is not an error:
`init` in a project that already has a `.crom.toml`, `add` of a profile already
declared, and `up` on a browser already running this config all report what is there and
exit 0. Only a request for something *different* is refused.

`--no-probe` — on `up`, `restart`, `show`, `list`, `status` and `config` — skips asking
the port and reports `unprobed` instead of `ready` or `unreachable`. Use it when a
wedged browser's reply timeout would cost more than the answer is worth, and remember
that `unprobed` is not a promise that anything is reachable.

## Before you act

Three lines that will still be true long after this file has scrolled out of reach:

- Connect on `state: "ready"`. `running: true` is only the lights being on.
- `crom up` on a browser you did not launch costs someone their tabs. `--no-restart`.
- `down --all`, `rm`, `release`, `clean` are requests, not repairs.
