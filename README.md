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
| `flags` | `[defaults]`, `[profiles.X]` | Extra Chrome switches. A later layer replaces an earlier layer's entry for the same switch, so a profile overrides `[defaults]` and `[defaults]` overrides crom's launch policy. `${VARIABLE}` is expanded in a flag's *value* only, never in its switch name — crom resolves switches by the name the file spells and expands afterwards, so a variable there would name a switch no other layer could match. `crom config` shows which layer each flag came from and what it replaced. |
| `drop_flags` | `[defaults]`, `[profiles.X]` | Switch names removed from what this stanza inherits, whether it came from `[defaults]` or from crom's own launch policy. `drop_flags = ["--disable-sync"]` in a profile launches that profile with sync available, with no edit to crom. A layer below can set the switch again. Each entry is the literal switch name and nothing else — no value, no `${VARIABLE}`, no surrounding whitespace — because crom matches it against the switches the layers below supply exactly as written. Dropping a switch nothing supplies is fine; naming one twice, naming a switch crom owns, or both setting and dropping the same switch in one stanza is refused. `crom config` prints what was dropped under the command line. |
| `features` | `[defaults]`, `[profiles.X]` | Chrome features to turn on or off, as `FeatureName = true`/`false`. A name no layer mentions is left alone. crom folds every layer's table into a single `--enable-features` and `--disable-features`, so a feature you turn off joins crom's own rather than replacing them — which is why `flags` may not name those two switches itself. Feature names are literal: unlike `flags` and `env`, they do not interpolate `${VARIABLE}`. |
| `env` | `[defaults]`, `[profiles.X]` | Environment variables for the Chrome process. |
| `seed` | `[defaults]`, `[profiles.X]` | Where a new profile's data comes from: `fresh`, `default`, `chrome:<Profile Name>`, or a path to an existing user-data-dir. A profile with no `seed` key inherits `[defaults].seed`, which is `default` unless the config says otherwise. |
| `port` | `[profiles.X]` | Pin the CDP port. Leave it out and crom assigns a free one and remembers it. |

Flag values and env values can interpolate `${CROM_PROFILE_DIR}`, `${CROM_CONFIG_DIR}`,
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
crom --version                # just the version, for a bug report
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

Two commands add a key to that record. `crom restart --json` adds `stopped`, the pids it
killed, which is empty when the profile was not running — that is the one distinction the
command exists to report, and the record alone cannot carry it because the record
describes the profile rather than the act. `crom show --json` adds `started`, whether it
had to launch the browser first, and `windows`, how many came forward — zero for a
headless profile, which is raised successfully and simply has no window to show for it.

`crom list --json` gives an array, but not every element has that shape. A profile that
could not be resolved appears as `{"namespace", "profile", "ref", "error"}`, and with
`--all` a namespace whose config file is gone appears as `{"namespace", "error"}`. That
is deliberate: one broken declaration is reported rather than sinking the whole listing,
since `crom list` is what you run *because* something is wrong. Check for `error` before
reading `port` and friends.

`crom doctor --json` is neither shape. It answers with an object: `registry`, the path of
crom's port ledger, and `reservations`, the rows in it. The path is in the answer because
editing that file by hand is the only way to release a single reservation no config
declares any more. A row carries `ref` whole — `"myapp/beta"` — rather than split into
`namespace` and `profile` the way the record does. Nothing rejects a ledger key a
hand-edit invented, so a row may name something that is not a legal `namespace/name` at
all, and `crom doctor` reports it rather than refusing it.

A row also carries `standing`, where the reservation stands against the config the ledger
names as its source, and `finding`, the sentence behind that verdict — it names the
config crom consulted, or says the ledger records none. `declared` means crom read that
config and it still declares the profile. `orphaned` means crom consulted it and nothing
declares the profile any more, because the config dropped it or because the file is gone;
that is the leak — the port stays held forever, `crom list` cannot see it, and crom's
allocator steps over the number. `unchecked` means crom could not consult a config at all
— the ledger records none, or the one it records would not load — so it claims nothing
either way.

Do not read `unchecked` as a quieter `orphaned`. A released port never comes back, and
every checked-in `.mcp.json` and `CDP_URL` pointing at the number breaks with it, so crom
will not call a reservation orphaned on evidence it could not read: an absent config
declares nothing, which is an answer, while a config that will not parse might still
declare the profile, which is not.

`crom --version` prints the version alone on one line, so a script reads it without
splitting a sentence. It reports the crom you have installed rather than any checkout you
happen to be standing in, which is the number a bug report needs.

### When a command fails

Exit codes are a contract: `0` success, `1` failure, `2` bad usage, `3` no such profile
or namespace, `4` a port or declaration conflict.

Pass `--json` and a failed command also answers on stdout, in one shape for every
failure:

```json
{
  "error": {
    "code": 3,
    "kind": "not_found",
    "reason": "namespace_unknown",
    "fields": {"known": ["user"]},
    "message": "unknown namespace 'nosuchns'. Known namespaces: user"
  }
}
```

stderr still carries the same sentence, byte for byte. `--json` adds the machine's copy
and never trades the human's away, so a wrapper can parse stdout and still show its own
user what crom said.

`code` is the exit code repeated, so a caller holding only the parsed document does not
also need the process's status. `kind` says what the code cannot: `failure` and `os_error`
are both exit `1`, and only `kind` separates crom refusing your request from the machine
refusing crom. The other two kinds are `not_found` and `conflict`, at exit `3` and `4`.

`reason` is one word for what actually went wrong. Exit `1` covers "there is no usable
Chrome", "Chrome started and died" and "the config file will not parse" — three different
next moves under one number — and the reason is where that distinction lives, so the
numeric contract never has to grow to carry it.

Two vocabularies share that key, and you can tell them apart by case: crom's own reasons
are lowercase, like `port_conflict`, `chrome_launch_failed` and `seed_missing`, while an
`os_error` answers with the errno name the OS already owns, `ENOENT` or `EACCES` or
`EISDIR`, rather than a second spelling crom invented beside it. It is `null` for the
occasional `OSError` carrying no errno, since a stand-in like `"unknown"` would be a slug
you could branch on by mistake.

`fields` is what crom looked up on the way to refusing, kept as data rather than only as
English — the namespaces that do exist, the profiles a config declares. It is always
present, and `{}` for the reasons that looked nothing up, so every failure reads the same
way instead of making you test for the key first. Six reasons carry anything at all:

| reason | carries | what those hold |
| --- | --- | --- |
| `config_missing` | `path` | The config file crom went looking at, which is not simply what you typed: it may have been resolved from `CROM_CONFIG` or discovered by walking up from the working directory. |
| `namespace_unknown` | `known` | The namespaces that do exist. Not the one you asked for — that came from your own argument. |
| `profile_unknown` | `source`, `declared` | The config in effect, and the profiles it declares. `source` is `null` for a user config that has not been written yet. |
| `namespace_claimed` | `namespace`, `claimed_by` | The namespace, and the path of the config file already registered against it. The namespace is here because a bare `crom init` derives it from the directory name, so you may never have typed it. |
| `port_conflict` | `port` | The number two claims landed on. Not who the other claimant is: across the three ways this fires it is a second profile, a reservation crom holds for `user/default`, or a ledger entry, so naming it once would mean inventing it twice. |
| `declaration_differs` | `settings` | Which settings differ, in the config file's own key names. The declared and requested values stay in the sentence: they are for a human deciding which is right, and a script acting on them would be editing your config from the text of a refusal. |

A value is a string, an integer, an array of strings, or `null`. Nothing nests deeper
than that.

`message` is the one key not to parse. It is written for a person, it is the same sentence
crom put on stderr, and it is free to be reworded; anything in it a script needs is in
`reason` and `fields` already, which is what they are there for.

What you can rely on: exit codes keep their meanings, a published `reason` is never
renamed, a field name is never renamed, and the values `settings` uses are the config
file's own key names, which do not change either. Reasons and fields do get *added*, so
branch on the ones you know and fall back to `kind` — it is coarse precisely so that an
unfamiliar reason still lands somewhere sensible.

Not every failure gets an envelope, and the rule is that one appears exactly when a
command that takes `--json` was given it and parsed it. `up`, `down`, `restart`, `show`,
`list`, `config` and `doctor` take the flag; `add`, `rm`, `init`, `port`, `env`, `mcp` and
`forget` answer in prose only. Bad usage — exit `2`, an unknown flag or a missing
argument — is parsing itself failing, so the `--json` on that line was never understood
either: there is no flag to honour, and the answer stays prose. `crom --version` is not a
command and answers in prose for the same reason. A broken pipe is deliberately outside
all of it: when `crom list | head` loses its reader mid-write, crom ends silently with
exit `1` and nothing on either stream, because a reader that has already left is the one
failure with nowhere to put a document.

One gap worth knowing, because it looks like an envelope and is not. The `error` string a
`crom list --json` element carries for a declaration it could not resolve is a sentence
with no `reason` and no `fields`, inside a document that exited `0` — a failure carried as
a value rather than raised as one, and the one place left in crom's output where a failure
is only English.

## Commands

```
crom                          launch the default profile
crom up [REF]                 launch, or report the running browser
crom down [REF]               stop it
crom restart [REF]            stop it and start it again on its current config
crom show [REF]               bring its window to the front, launching it if needed
crom list [--all]             profiles addressable from here; --all covers every namespace
crom add NAME [--seed SEED]   declare a profile in the config governing this directory
crom rm REF                   stop it if running, undeclare it, release its port, delete its data
crom init [NS] [--seed SEED]  write a .crom.toml here
crom config [REF]             which config is in effect, and the exact Chrome command line
                              — each flag attributed to the layer that supplied it
crom port [REF]               print the port
crom env [REF]                print shell exports
crom mcp [REF]                write .mcp.json
crom doctor                   every reservation in the ledger, and which ones are orphaned
crom forget NAMESPACE         drop a namespace deliberately, releasing its ports
```

`REF` is `name` (resolved in the ambient namespace) or `namespace/name`. It defaults to
`default`, with two exceptions: `crom config` without a REF reports the ambient scope
alone (which config is in effect, and what it declares) rather than resolving a profile,
and `crom rm` requires a REF — it will not guess which profile you meant to delete.

`crom restart` holds one profile lock across both halves, so a concurrent `crom up` or
`crom rm` lands wholly before it or wholly after rather than in the gap between the stop
and the start. Typing `crom down && crom up` leaves that gap open.

`crom show` is the one macOS-only command. Every crom-managed Chrome is the same
application bundle, so `activate` cannot pick between them — it raises whichever instance
the window server prefers. crom targets the exact process instead, which is unambiguous
however many are running, and that needs AppleScript. macOS gates it behind Automation
access granted to the program that ran crom, not to crom itself; when that is withheld,
crom names the System Settings pane that grants it. A profile running headless is raised
successfully and reported as having no window, rather than as a window you cannot find.

## How collisions are avoided

Ports come from one ledger at `~/.local/state/crom/registry.json`, shared by every project
on the machine — `crom doctor` prints every reservation in it, sorted by port, so a hole
in the run is visible, and says which ones no config declares any more. A profile is
assigned a free port the first time it resolves and keeps it forever, so a checked-in
`.mcp.json` or an app's `CDP_URL` stays correct. Before handing out an *assigned* port
crom binds it, which catches the unrelated dev server that grabbed 9222 an hour ago. A
port you pin yourself skips that search, so its availability is checked at launch instead
— `crom up` names the process holding it rather than timing out. Two projects that pin
the same port get an error naming the config file on the other side, not a mysterious
launch failure. Every read-modify-write of the ledger takes an exclusive lock, because
several agents each bringing up a browser at once is the case crom is for — and the
config files crom edits and the profile directories it seeds are locked the same way, for
the same reason.

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
That covers `crom up`, `crom restart`, `crom show`, `crom port`, `crom env`, `crom mcp`,
and `crom config <ref>`, and
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

crom refuses to start the copy while a browser is running on the Chrome installation you
are copying from — the whole user-data-dir, not just the profile you named, so
`seed = "chrome:Work"` is refused while a Personal window is open. A live profile holds
databases Chrome writes continuously, and a plain copy can catch one mid-write; the copy
would report success, and the damage would surface days later as missing history or
logins that stopped working, with nothing to connect it back to the copy. So crom looks
for the lock file Chrome keeps in a running user-data-dir, and if it finds a live one it
stops, names the directory, quotes what it saw, and gives you two ways on: quit the
browser and run again, or `seed = "fresh"` for a profile that starts empty. A lock left
behind by a Chrome that crashed does not stop anything — crom checks whether the process
it names is still alive. It checks once more after the copy: a browser that opened
partway through and is still open is caught, and the half-copied profile is thrown away.
One that opens and quits entirely inside the copy window is not — Chrome takes its lock
with it, so by the time crom looks again there is nothing left to see. The first `crom up`
on a machine is where you are most likely to meet this, because the default profile is
seeded from the Chrome you probably have open right now.

## Where things live

```
~/.config/crom/config.toml                    your profiles (the `user` namespace)
~/.local/state/crom/registry.json             port assignments + known namespaces
~/.local/state/crom/profiles/<ns>/<name>/     Chrome user-data-dirs
~/.local/state/crom/profiles/<ns>/<name>/crom-stderr.log
                                              what Chrome has printed since its last launch
```

`crom-stderr.log` is rewritten each time crom starts that profile's browser, and goes when
`crom rm` deletes the profile — `crom down` leaves it, so a browser that died has an
account of itself. A failed launch quotes its tail and names the file.

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
