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

macOS and Linux. crom answers "is this profile running" by reading the process table
with `ps` and serializes its ledger with `flock`, so Windows is not supported.

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

`crom init` writes a commented skeleton with a `default` profile, and `--seed` decides
what goes into its `[defaults].seed`. Add more profiles by hand, or with `crom add`,
which appends to the same file and leaves your comments alone:

```toml
namespace = "myapp"

[defaults]
seed = "default"
flags = ["--window-size=1280,900"]

[profiles.default]
flags = []

[profiles.ci]
flags = ["--headless=new"]
```

Neither profile declares a `seed`, so both inherit `[defaults].seed` — a copy of your
real Chrome profile. `crom init --seed fresh` writes an empty one there instead, and
`crom add ci --seed fresh` overrides it for a single profile.

### Config reference

Both `~/.config/crom/config.toml` and a project's `.crom.toml` use this schema. The user
file is always the `user` namespace and must not declare one.

| Key | Where | What it does |
| --- | --- | --- |
| `namespace` | top level | Required in a project config. Keeps this project's ports and profile directories clear of every other project's. `user` is reserved. |
| `chrome_binary` | top level | Path to Chrome. Auto-detected per platform when absent. |
| `state_dir` | top level | Where profile directories go, relative to the config file. Defaults to `~/.local/state/crom/profiles`. Set it to `.crom/profiles` to keep a project's browser data inside the repo (and gitignore it). |
| `flags` | `[defaults]`, `[profiles.X]` | Extra Chrome switches. Namespace defaults come first, then the profile's own. |
| `drop_flags` | `[defaults]`, `[profiles.X]` | Switch names — no values — removed from what this stanza inherits, whether it came from `[defaults]` or from crom's own launch policy. `drop_flags = ["--disable-sync"]` in a profile launches that profile with sync available, with no edit to crom. A layer below can set the switch again. Dropping a switch nothing supplies is fine; naming one twice, giving one a value, naming a switch crom owns, or both setting and dropping the same switch in one stanza is refused. `crom config` prints what was dropped under the command line. |
| `features` | `[defaults]`, `[profiles.X]` | Chrome features to turn on or off, as `FeatureName = true`/`false`. A name no layer mentions is left alone. crom folds every layer's table into a single `--enable-features` and `--disable-features`, so a feature you turn off joins crom's own rather than replacing them — which is why `flags` may not name those two switches itself. Feature names are literal: unlike `flags` and `env`, they do not interpolate `${VARIABLE}`. |
| `env` | `[defaults]`, `[profiles.X]` | Environment variables for the Chrome process. |
| `seed` | `[defaults]`, `[profiles.X]` | Where a new profile's data comes from: `fresh`, `default`, `chrome:<Profile Name>`, or a path to an existing user-data-dir. A profile with no `seed` key inherits `[defaults].seed`, which is `default` unless the config says otherwise. |
| `port` | `[profiles.X]` | Pin the CDP port. Leave it out and crom assigns a free one and remembers it. |

Flags and env values can interpolate `${CROM_PROFILE_DIR}`, `${CROM_CONFIG_DIR}`,
`${CROM_PORT}`, `${CROM_NAMESPACE}`, and `${CROM_PROFILE}` — so a project can load an
extension that lives next to its config:

```toml
[profiles.dev]
flags = ["--load-extension=${CROM_CONFIG_DIR}/ext/dist"]
```

crom rejects an unknown key, an unknown `${VARIABLE}`, and any attempt to set
`--user-data-dir`, `--remote-debugging-port`, or `--remote-debugging-pipe` yourself.
Those are how crom knows which browser is which and how to reach it. `flags` also may
not name `--enable-features` or `--disable-features`; use the `features` table, which
crom composes into those switches for you.

Discovery walks up from the working directory and stops at the first hit: `.crom.toml`,
then `.crom/config.toml`. Ancestor configs are never merged into descendants — one
profile, one file, and `crom config` will tell you which file. `CROM_CONFIG=/path/to/file`
overrides discovery entirely.

## Using crom from an app

`crom up` is idempotent: it launches if nothing is running and reports the live port
either way, so a script can call it unconditionally.

```bash
eval "$(crom env dev)"        # see below for what it exports
crom port dev                 # just the number
crom up dev --json            # the full record, for anything that parses
crom mcp dev                  # writes .mcp.json pointing chrome-devtools-mcp at it
```

`crom env` exports `CROM_NAMESPACE`, `CROM_PROFILE`, `CROM_REF`, `CROM_PORT`,
`CROM_CDP_URL`, and `CROM_PROFILE_DIR`. The names it shares with the `${VARIABLE}`
vocabulary above mean the same thing in both places — `CROM_PROFILE` is the profile name
in a config and in your shell, and `CROM_REF` is the joined `namespace/name`.

`--json` gives you this:

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

`crom list --json` gives an array, but not every element has that shape. A profile that
could not be resolved appears as `{"namespace", "profile", "ref", "error"}`, and with
`--all` a namespace whose config file is gone appears as `{"namespace", "error"}`. That
is deliberate: one broken declaration is reported rather than sinking the whole listing,
since `crom list` is what you run *because* something is wrong. Check for `error` before
reading `port` and friends.

Exit codes are a contract: `0` success, `1` failure, `2` bad usage, `3` no such profile
or namespace, `4` a port or declaration conflict.

## Commands

```
crom                          launch the default profile
crom up [REF]                 launch, or report the running browser
crom down [REF]               stop it
crom list [--all]             profiles addressable from here; --all covers every namespace
crom add NAME [--seed SEED]   declare a profile in the config governing this directory
crom rm REF                   stop it if running, undeclare it, release its port, delete its data
crom init [NS] [--seed SEED]  write a .crom.toml here
crom config [REF]             which config is in effect, and the exact Chrome command line
crom port [REF]               print the port
crom env [REF]                print shell exports
crom mcp [REF]                write .mcp.json
crom forget NAMESPACE         drop a namespace deliberately, releasing its ports
```

`REF` is `name` (resolved in the ambient namespace) or `namespace/name`. It defaults to
`default`, with two exceptions: `crom config` without a REF reports the ambient scope
alone (which config is in effect, and what it declares) rather than resolving a profile,
and `crom rm` requires a REF — it will not guess which profile you meant to delete.

## How collisions are avoided

Ports come from one ledger at `~/.local/state/crom/registry.json`, shared by every
project on the machine. A profile is assigned a free port the first time it resolves and
keeps it forever, so a checked-in `.mcp.json` or an app's `CDP_URL` stays correct. Before
handing out an *assigned* port crom binds it, which catches the unrelated dev server that
grabbed 9222 an hour ago. A port you pin yourself skips that search, so its availability
is checked at launch instead — `crom up` names the process holding it rather than timing
out. Two projects that pin the same port get an error naming the config file on the other
side, not a mysterious launch failure. Every read-modify-write of the ledger takes an
exclusive lock, because several agents each bringing up a browser at once is the case
crom is for — and the config files crom edits and the profile directories it seeds are
locked the same way, for the same reason.

Profile data is namespaced the same way: `<state_dir>/<namespace>/<name>`. Two projects
can both have a `dev` profile and never see each other's cookies.

Liveness is not tracked at all. crom reads the process table and matches on
`--user-data-dir`, so a browser you quit by closing the window is simply gone — there is
no pidfile to go stale.

## Repairs crom makes for you

A crom command names an end state and converges to it, so crom never tells you to run a
different crom command first. If the only thing between a command and its job is another
crom command, crom runs it and says so.

Asking for a state you are already in is the simplest case of that. `crom init` in a
project that already has a `.crom.toml` reports the namespace and seed that file
declares and exits 0 without touching it; `crom add ci` for an already-declared `ci`
reports the seed, port, and directory `ci` resolves to and exits 0 the same way.
`crom up` on a running browser and `crom down` on a stopped one always behaved like
this, and now every command does.

But converging is not ignoring. crom compares the facts you actually state — so a bare
`crom add ci` states only the name and always converges — and any stated fact that
differs from the file is an error naming the difference: `crom add ci --port 9500`
against a `ci` pinned to 9224 stops with `port: declared 9224, you asked for 9500`, and
`crom init other` in a project whose namespace is `proj` stops the same way. Neither
touches the file. The comparison is on what the config effectively means, not on which
key it happens to spell things in, so `crom add ci --seed fresh` is fine when `ci`
inherits `fresh` from `[defaults]`. `--port` is the one exception: an assigned port is
remembered in the machine-local ledger and written nowhere in the file, so pinning the
number crom happened to hand out is a real change to a checkout on any other machine,
and crom asks you to make it yourself. Exiting 0 having quietly dropped your `--port 9500`
would be crom claiming work it did not do and you finding out at launch. crom does not
rewrite a config it did not just create: edit the file, or `crom rm <ref>` and add the
profile back.

A profile you refer to but never declared gets declared, exactly as `crom add <name>`
with no options would declare it — a bare stanza, so `[defaults]` still governs its seed.
That covers `crom up`, `crom port`, `crom env`, `crom mcp`, and `crom config <ref>`, and
the case it fires in most is a bare `crom up` in a project whose `.crom.toml` declares no
profiles at all, which used to exit 3 for want of the `default` all of those commands
default to. The cost is that a mistyped ref no longer errors: it writes a declaration into
a version-controlled `.crom.toml` and reserves a port, and under `crom up` copies a few
hundred megabytes of Chrome profile as well. `crom rm <name>` undoes all of it.

`crom down` and `crom rm` are the deliberate exception. They converge a profile toward
*not* running and *not* existing, so declaring one on the way would be crom creating the
thing it was asked to take away. `crom rm typo` is still an error, and leaves no profile
named `typo` behind.

A config file that will not tokenize as TOML at all is reset to the default crom would have
written, and the file it replaced is renamed beside it as `.crom.toml.broken` — then
`.broken-2`, `.broken-3`, because an earlier reset's copy is never overwritten. Nothing is
deleted. crom's parser is strict, so one unterminated string used to take out every command
in the project, including `crom init` and `crom rm`, the two you would reach for to repair
it; there was no command crom could have named as the fix.

Every other way a config can be wrong keeps its precise message naming the file and the
exact key, and leaves the file completely alone: two profiles pinning the same port, an
unknown key, a typo'd `chrome_binary`, an unrecognised seed keyword, `state_dir = ""`. When
crom can still read the file it can still see your other declarations, and resetting would
destroy four good ones to punish one bad line. Only a file that will not tokenize holds
nothing crom can act on. The trigger is a question about bytes, so nothing about the state
of the machine — whether Chrome is installed, where it is — can cause a reset.

The reset usually keeps your namespace. The registry records which config file owns which
namespace, so a project crom has loaded successfully at least once keeps its name, and with
it its ports and its profile directories. But the registry only learns that name *after* a
successful load, so a `.crom.toml` that arrived already broken — a fresh clone, a
hand-written file crom has never read — falls back to the directory name. That is a real
rename, onto a fresh set of ports and profile directories, and it is why crom reports the
namespace it chose instead of leaving you to work it out.

What gets reset is the config governing the directory you are standing in, plus your own
user config. A foreign project's config — one reached by name, as `crom up otherproject/dev`
does, or swept up by `crom list --all` — is reported and never rewritten from here, because
one `crom list --all` that repaired every registered project on the machine would drop
declarations belonging to work you weren't even doing. A config is repaired by the project
standing in it.

Two smaller repairs follow the same rule. `crom add` recreates a project config deleted
after crom read it — a `git clean`, another agent resetting the workspace — rather than
sending you back to `crom init`. And when a registered namespace's config file is gone, or
it has renamed its namespace in place, crom drops its record of *where that project lives*
and reports the namespace unknown, which is what it now is — but keeps the ports reserved
under it. An absent file is not proof the project is gone: an unmounted volume or a
mid-flight `git checkout` looks identical from here, and a released port is irreversible,
handed straight to another profile while every checked-in `.mcp.json` and `CDP_URL` pointing
at the old number breaks. If the project comes back it re-registers itself and every profile
resolves to the port it always had. `crom forget` is the only thing that releases those
ports, run deliberately about a project you know is gone.

Every one of these is reported on stderr as it happens. stdout still carries only the
answer, so a script parsing `crom port` sees exactly what it saw before.

## A note on seeds

A new profile is a copy of your real Chrome profile by default, because a browser with
no logins and no extensions cannot do the work crom exists for — you would spend the
first ten minutes of every new profile signing back into everything. An empty one is a
word away: `--seed fresh` on `crom init` or `crom add`.

The copy is not free. A working Chrome profile runs to a few hundred megabytes, and
that gets copied once per profile, when crom first creates its directory. After that
the profile owns its own state and crom never overwrites it.

A hand-written config is the one place that default can surprise you. A profile that
declares no `seed`, in a file that declares no `[defaults].seed` either, gets a copy of
your real Chrome profile — every cookie and session — the next time `crom up` creates
its directory, and if `state_dir` points at `.crom/profiles` that copy lands inside your
working tree. Configs crom wrote itself are never in that position, because `crom init`
always records a `[defaults].seed` for the profiles beneath it to inherit. Before it
starts copying, `crom up` prints
`Creating <ref> from seed 'default' …` on stderr, so the copy is announced rather than
silent — and `seed = "fresh"`, on the profile or in `[defaults]`, gets you an empty one.

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
pointing at it still works. A profile you declared but never launched has no port to
keep — the old registry only recorded one on first launch — so it gets a fresh one.
The old registry is preserved as `profiles.json.migrated`.

Those two paths are literal. Migration looks for the previous installation where the
previous crom actually wrote it, which is `~/.config` and `~/.local/state` regardless of
`XDG_CONFIG_HOME` or `XDG_STATE_HOME` — the version that wrote them did not consult
either. Everything crom writes *after* migrating honors both, as below.

Quit your crom-managed Chrome windows first. Migration moves profile directories, and
crom refuses to move one out from under a running browser — it would leave a process it
could no longer find or stop.

## Development

```bash
uv sync
.venv/bin/python -m unittest discover -s tests
```
