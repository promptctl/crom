# Snapshots: storage and semantics

A snapshot is a copy of a stopped profile's user-data-dir, kept under a name, that any
profile can seed from. It closes the half of the capability the `path` seed leaves open:
crom can already seed from captured state, but today the user has to place and manage
that directory by hand.

This note settles the four questions that had to be answered before `crom snapshot`
could be written. Each is a decision with evidence behind it — reopen one only with
evidence that contradicts what is recorded here.

Measurements below were taken on one macOS/APFS machine against `user/default` after
several months of use. Treat them as the order of magnitude, not as constants.

## Snapshots are machine-global

`snapshot:<name>` names the same directory from every namespace. There is no
per-namespace snapshot space.

Namespaces exist to stop two projects from colliding on one profile directory and one
port; `registry.remember_namespace` refuses a second claimant for exactly that reason.
They have never been a confidentiality boundary, and crom crosses them
already, in both directions:

- Every namespace's default seed is the user's own Chrome profile — `model.DEFAULT_SEED`
  is `SeedChrome()`, and `Scope.default_seed` hands it to project scopes as well as to
  `user`. That copy carries every login on the machine into whatever namespace asked.
- A path seed may name another namespace's profile directory. `config._parse_seed_path`
  refuses the home directory and the filesystem root and nothing else, so
  `seed = "~/.local/state/crom/profiles/other/dev"` parses and copies today.

Scoping snapshots per namespace would add a boundary crom keeps nowhere else, and it would
deny the case the feature exists for: authenticate once, branch ten profiles, usually
across more than one project.

## They live in `<state_home>/snapshots/<name>/`, and not in the registry

`paths.state_home() / "snapshots"`, alongside `profiles/` and `registry.json` — with a
`snapshots_root()` in `paths.py`, since that module owns every directory crom composes.

Not under `profiles_root`. That root is per-scope: a config setting `state_dir` moves it
(`config.py:545`), so snapshots underneath it would become
project-local for precisely the projects that set the key — which contradicts the
decision above, silently, and only for some users.

Not in the registry, for two reasons of different weight.

The registry arbitrates a resource that has no truth on disk. A port is a number the
machine hands out; nothing in the filesystem records who holds it, so the ledger has to.
A snapshot is a directory, and the directory is the fact. A ledger row beside it would be
a second copy of one truth, free to disagree with it — a snapshot deleted with `rm -rf`
would live on in the ledger, and the row would have to be reconciled by something.

The harder reason is the cost of the change. Adding a table means `registry.SCHEMA_VERSION`
2 → 3, and `registry._read` raises `REGISTRY_UNSUPPORTED` for any version it does not
recognize. Every command reads the ledger, so every command of every older crom on the
machine would begin failing against a registry a newer one had touched. That is a large
bill for bookkeeping the filesystem already does.

`crom doctor` should still report snapshots — their names, paths and sizes — the way it
reports staging directories today. That is a survey of what is on disk, which is the kind
of answer doctor already gives; it is not a ledger.

## Chrome must be stopped, and crom must not stop it

There is no flush. CDP offers no command that quiesces a user-data-dir; a clean exit is
the only checkpoint Chrome takes. The evidence is on disk after one: on cleanly stopped
profiles every SQLite rollback journal in `Default/` is 0 bytes, and no `SingletonLock`,
`SingletonCookie` or `SingletonSocket` remains. During a session those journals are live,
which is the torn copy `seed._refuse` already warns about.

So capture refuses a running profile, using the same ancestry check `seed._held` performs
— it is total over both a profile root and any directory beneath it, and capture wants
that same totality.

Capture must not stop the browser on the user's behalf. `chrome.kill` guarantees that the
port comes free, and it gets there by escalating SIGTERM to SIGKILL. A SIGKILLed Chrome
leaves exactly the mid-transaction profile this refusal exists to prevent, so an
auto-stop would occasionally manufacture the corruption the feature is built to avoid.
The user quits the browser; crom checks and copies.

One consequence to handle rather than discover: a profile that was SIGKILLed keeps
`SingletonSocket`, an absolute symlink into `/var/folders`. `seed._link_guard` therefore
already refuses that tree — correctly, since it is an unclean profile, but with a
sentence written for a different problem ("Make it relative"). Capture should either skip
Chrome's three singleton links or give an unclean-shutdown profile its own message.

## Neither dedup nor copy-on-write; capture excludes cache instead

The premise that made dedup look necessary does not survive measurement. Summing file
sizes under `user/default`: **2015 MB total, 249 MB once the cache trees are excluded** —
88% of a profile is data Chrome regenerates. `Default/Cache` alone is 979 MB and
`Default/Code Cache` is 544 MB.

Excluded at capture: `Cache`, `Code Cache`, `GPUCache`, `DawnWebGPUCache`,
`GraphiteDawnCache`, `ShaderCache`, `component_crx_cache`, `extensions_crx_cache`,
`optimization_guide_model_store`, `WasmTtsEngine`, `OnDeviceHeadSuggestModel`,
`Safe Browsing`, `ActorSafetyLists`, `CertificateRevocation`. Every one is rebuilt or
re-downloaded on demand.

Kept deliberately, and not to be added to that list without a reason: `Extensions`
(77 MB), `Service Worker` (66 MB) and `IndexedDB` (61 MB). The latter two are where
single-page applications keep session and auth state, which is the state a snapshot
exists to carry. Those two are 127 MB of the surviving 249, and dropping them to halve a
snapshot would break the logins it was taken for.

Ten snapshots is therefore about 2.5 GB, not 20 GB, and neither mechanism is worth its
cost yet.

Copy-on-write works, and still is not worth it. Measured on APFS against a 420 MB profile,
`cp -Rc` took 0.65s and consumed no space, against 2.14s and 418 MB for `cp -R`. The cost
is structural: `shutil.copytree` does not clone, so adopting CoW means shelling out to
`cp -c` (macOS) or `cp --reflink=auto` (Linux, and only on btrfs or xfs) and giving up
`_link_guard` as `copytree`'s `ignore` hook. That hook is what gives the copy one
traversal and one notion of the tree's contents, which `seed._link_guard` documents as
the close of a TOCTOU window. That property is not worth 249 MB. Revisit only if
snapshots stay large after exclusion.

Dedup across snapshots is a further step past CoW — content hashing, a store, a
collection problem — for a saving exclusion has already taken.

## What this decides for the remaining tickets

- **6a5.2 (capture)** is wider than "copy, and refuse if running": it owns the exclusion
  list. That list is an enumeration, so it belongs in one named place the way
  `MIN_PORT`/`MAX_PORT` do, and capture is its only reader.
- Exclusion belongs to capture alone. A seed reading a snapshot copies whatever the
  snapshot holds, so it inherits the lean tree for free, and `chrome:` and `path` seeding
  keep the behavior they have.
- **6a5.3 (`snapshot:<name>`)** adds a fourth `Seed` variant rather than rewriting a
  snapshot name into a `SeedPath`. The name is what the user wrote and what an error
  should quote back; resolving it to a path at parse time loses that, and `_plan` is
  already the one place a seed becomes a copy.
