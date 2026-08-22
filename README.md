# crom

crom gives a project its own Chrome — its own profile directory, its own flags, and a
CDP port that stays the same every time. Drop a `.crom.toml` in a repo and anything in
that repo can drive a browser without fighting whatever else on the machine also wanted
port 9222.

It exists because running several browser-driving tools at once is miserable. They all
default to `--remote-debugging-port=9222`, they all reach for `~/Library/.../Chrome`,
and the second one to start either fails or silently attaches to the first one's
browser. crom hands each project a namespace and assigns each profile a port once,
which is the whole trick.

## Install

```bash
uv tool install --force .     # or, to hack on it: uv sync && .venv/bin/crom
```

## Quick start

```bash
crom              # launch your default profile
crom list         # what exists, and what's running
crom down         # stop it
```

Your personal profiles live in `~/.config/crom/config.toml` and answer to `user/<name>`.
On first run crom writes a `default` profile there, seeded from your real Chrome profile
so your logins come along.

## Giving a project its own browser

```bash
cd ~/code/myapp
crom init          # writes .crom.toml with namespace = "myapp"
crom up            # launches myapp/default
crom port          # -> 9223
```

From here on, any command run inside `~/code/myapp` (or below it) resolves bare profile
names in the `myapp` namespace, so `crom up dev` means `myapp/dev`. Other projects get
other ports. And once crom has read this config, `myapp/dev` is addressable from
anywhere on the machine.

`crom init` writes a commented skeleton with a `default` profile. Add more by hand, or
with `crom add`, which appends to the same file and leaves your comments alone:

```toml
namespace = "myapp"

[defaults]
flags = ["--window-size=1280,900"]

[profiles.default]
seed = "fresh"
flags = []

[profiles.ci]
seed = "fresh"
flags = ["--headless=new"]
```

### Config reference

Both `~/.config/crom/config.toml` and a project's `.crom.toml` use this schema. The user
file is always the `user` namespace and must not declare one.

| Key | Where | What it does |
| --- | --- | --- |
| `namespace` | top level | Required in a project config. Keeps this project's ports and profile directories clear of every other project's. `user` is reserved. |
| `chrome_binary` | top level | Path to Chrome. Auto-detected per platform when absent. |
| `state_dir` | top level | Where profile directories go, relative to the config file. Defaults to `~/.local/state/crom/profiles`. Set it to `.crom/profiles` to keep a project's browser data inside the repo (and gitignore it). |
| `flags` | `[defaults]`, `[profiles.X]` | Extra Chrome switches. Namespace defaults come first, then the profile's own. |
| `env` | `[defaults]`, `[profiles.X]` | Environment variables for the Chrome process. |
| `seed` | `[defaults]`, `[profiles.X]` | Where a new profile's data comes from: `fresh`, `chrome`, `chrome:<Profile Name>`, or a path to an existing user-data-dir. Defaults to `fresh`. |
| `port` | `[profiles.X]` | Pin the CDP port. Leave it out and crom assigns a free one and remembers it. |

Flags and env values can interpolate `${CROM_PROFILE_DIR}`, `${CROM_CONFIG_DIR}`,
`${CROM_PORT}`, `${CROM_NAMESPACE}`, and `${CROM_PROFILE}` — so a project can load an
extension that lives next to its config:

```toml
[profiles.dev]
flags = ["--load-extension=${CROM_CONFIG_DIR}/ext/dist"]
```

crom rejects an unknown key, an unknown `${VARIABLE}`, and any attempt to set
`--user-data-dir` or `--remote-debugging-port` yourself. Those two are how crom knows
which browser is which.

Discovery walks up from the working directory and stops at the first hit: `.crom.toml`,
then `.crom/config.toml`. Ancestor configs are never merged into descendants — one
profile, one file, and `crom config` will tell you which file. `CROM_CONFIG=/path/to/file`
overrides discovery entirely.

## Using crom from an app

`crom up` is idempotent: it launches if nothing is running and reports the live port
either way, so a script can call it unconditionally.

```bash
eval "$(crom env dev)"        # CROM_PORT, CROM_CDP_URL, CROM_PROFILE_DIR
crom port dev                 # just the number
crom up dev --json            # the full record, for anything that parses
crom mcp dev                  # writes .mcp.json pointing chrome-devtools-mcp at it
```

`--json` gives you this, and `list` gives you an array of the same shape:

```json
{
  "namespace": "myapp",
  "profile": "dev",
  "ref": "myapp/dev",
  "port": 9223,
  "cdp_url": "http://127.0.0.1:9223",
  "profile_dir": "/Users/you/.local/state/crom/profiles/myapp/dev",
  "chrome_binary": "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
  "source": "/Users/you/code/myapp/.crom.toml",
  "running": true,
  "pids": [13550]
}
```

Exit codes are a contract: `0` success, `1` failure, `2` bad usage, `3` no such profile
or namespace, `4` a port or declaration conflict.

## Commands

```
crom                    launch the default profile
crom up [REF]           launch, or report the running browser
crom down [REF]         stop it
crom list [--all]       profiles addressable from here; --all covers every namespace
crom add NAME           declare a profile in the config governing this directory
crom rm REF             undeclare it, release its port, delete its data
crom init [NAMESPACE]   write a .crom.toml here
crom config [REF]       which config is in effect, and the exact Chrome command line
crom port [REF]         print the port
crom env [REF]          print shell exports
crom mcp [REF]          write .mcp.json
crom forget NAMESPACE   drop a namespace whose config is gone
```

`REF` is `name` (resolved in the ambient namespace) or `namespace/name`. It defaults to
`default`.

## How collisions are avoided

Ports come from one ledger at `~/.local/state/crom/registry.json`, shared by every
project on the machine. A profile is assigned a free port the first time it resolves and
keeps it forever, so a checked-in `.mcp.json` or an app's `CDP_URL` stays correct. Before
handing out a port crom binds it, which catches the unrelated dev server that grabbed
9222 an hour ago. Two projects that pin the same port get an error naming the config file
on the other side, not a mysterious launch failure. Every read-modify-write of the ledger
takes an exclusive lock, because several agents each bringing up a browser at once is
the case crom is for.

Profile data is namespaced the same way: `<state_dir>/<namespace>/<name>`. Two projects
can both have a `dev` profile and never see each other's cookies.

Liveness is not tracked at all. crom reads the process table and matches on
`--user-data-dir`, so a browser you quit by closing the window is simply gone — there is
no pidfile to go stale.

## A note on seeds

Copying your real Chrome profile is expensive: a working profile runs to hundreds of
megabytes and carries every cookie you have. That is why `seed = "fresh"` is the default
for project profiles and `chrome` is opt-in. The seed applies once, when the directory is
created; after that the profile owns its own state and crom never overwrites it.

## Where things live

```
~/.config/crom/config.toml                    your profiles (the `user` namespace)
~/.local/state/crom/registry.json             port assignments + known namespaces
~/.local/state/crom/profiles/<ns>/<name>/     Chrome user-data-dirs
```

`XDG_CONFIG_HOME` and `XDG_STATE_HOME` are honored.

## Upgrading from the flat layout

Earlier versions kept ports in `~/.config/crom/profiles.json` and profile directories at
`~/.local/state/crom/<name>`. crom migrates that automatically on the next run: every
profile joins the `user` namespace and keeps the exact port it had, so anything already
pointing at it still works. The old registry is preserved as `profiles.json.migrated`.

Quit your crom-managed Chrome windows first. Migration moves profile directories, and
crom refuses to move one out from under a running browser — it would leave a process it
could no longer find or stop.

## Development

```bash
uv sync
.venv/bin/python -m unittest discover -s tests
```
