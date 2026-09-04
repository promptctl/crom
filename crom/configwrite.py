"""Edits crom config files in place, preserving the comments and layout around the edit.

`crom add` and `crom rm` change files a human wrote and may keep in version control, so
they are edits, not rewrites: tomlkit round-trips the document and we touch only the one
table involved. [LAW:one-source-of-truth] the file on disk stays the authority on
everything crom did not explicitly change.
"""

import os
import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from itertools import count
from pathlib import Path

import tomlkit

from . import flags
from .locking import exclusive
from .model import (
    CromError,
    ProfileSpec,
    Reason,
    Seed,
    SeedChrome,
    SeedFresh,
    SeedPath,
)

USER_CONFIG_HEADER = """\
# crom — your personal Chrome profiles (the `user` namespace).
#
# Profiles declared here are addressable from anywhere as `user/<name>`, or by bare
# name when you are not inside a project that has its own .crom.toml.
"""

PROJECT_CONFIG_TEMPLATE = """\
# crom — this project's Chrome profiles.
#
# The namespace keeps this project's profile directories and CDP ports clear of every
# other project's on this machine. Ports are assigned once, remembered, and reported by
# `crom port`; pin one with `port = 9401` under a profile if you need it fixed.

namespace = "{namespace}"

# What every profile below inherits unless it says otherwise.
#
# `seed` is where a profile's data comes from the first time crom creates it:
#   default                 a copy of your default Chrome profile — your logins, your
#                           extensions, ready to use
#   chrome:<Profile Name>   a copy of another named profile inside your Chrome
#   fresh                   an empty profile; Chrome sets it up on first launch
#   ./path/to/user-data-dir a copy of a directory you keep yourself
#
# A flag here replaces crom's own setting for the same switch, and a profile's replaces
# this one's — each switch reaches Chrome exactly once. To remove a switch a layer below
# set rather than replace it, name it in `drop_flags = ["--disable-sync"]`.
#
# Chrome features are not flags here: write `features = {{ SomeFeature = false }}`, and
# crom folds every layer's table into one --enable-features and one --disable-features.
#
# Available in flags and env values:
# ${{CROM_PROFILE_DIR}}, ${{CROM_CONFIG_DIR}}, ${{CROM_PORT}}, ${{CROM_NAMESPACE}},
# ${{CROM_PROFILE}}.
#
# `crom config --help` is the full reference for every key a config may set.
[defaults]
seed = {seed}
flags = []

# `crom up` with no argument brings this one up. Add more with `crom add <name>`.
[profiles.default]
flags = []
"""


def render_seed(seed: Seed, base: Path) -> str:
    """The config spelling of a seed — the inverse of `config.parse_seed`.

    `base` is the directory the config file lives in, and a path under it is written
    back relative to it. `config.parse_seed` absolutizes every path seed against that
    same directory, so rendering the resolved path verbatim would bake this machine's
    layout into a file the README expects to be committed and shared: `--seed
    ./local-seed` would land in the file as `/home/you/project/local-seed`, which is
    correct on exactly one machine.

    A path outside `base` has no portable spelling and is written absolute — `..`
    chains out of the project would be portable in form and wrong in meaning.
    """
    match seed:
        case SeedFresh():
            return "fresh"
        case SeedChrome(profile="Default"):
            return "default"
        case SeedChrome(profile=which):
            return f"chrome:{which}"
        case SeedPath(path=path):
            return _relative_to(path, base)


def _relative_to(path: Path, base: Path) -> str:
    """`./x` when `path` sits under `base`, else the absolute path.

    The `./` prefix is load-bearing rather than decoration: `config.parse_seed` only
    treats a value as a path when it starts with `.`, `/`, or `~`, so a bare `local-seed`
    would come back as an unrecognised keyword.
    """
    try:
        return f"./{path.relative_to(base)}"
    except ValueError:
        return str(path)


def _load(path: Path) -> tomlkit.TOMLDocument:
    """Read a config for editing, or fail naming the file a human has to repair.

    This runs *before* `config.parse` ever sees the file, and on every command:
    `main` calls `_bootstrap_user_config()` unconditionally, which reaches here through
    `ensure_profile` → `_declare` before any scope is loaded. So an unparseable
    `~/.config/crom/config.toml` raised a raw `tomlkit` error from every invocation —
    including the ones the user would reach for to fix it. [LAW:no-silent-failure] the
    same guard `config.parse` and `registry._read` already apply to their own files.
    """
    if not path.exists():
        return tomlkit.document()
    try:
        return tomlkit.parse(path.read_text())
    except tomlkit.exceptions.ParseError as e:
        # `ParseError` alone, though this once caught `OSError` beside it. "Repair the
        # file" is the wrong remedy for a file whose contents are fine and whose
        # permissions are not, and the CLI boundary answers that case better than any slug
        # here could: `<path>: <strerror>`, with the errno name the OS already published.
        # [LAW:polishing-by-subtraction] the wrong remedy is deleted rather than relabelled.
        raise Reason.CONFIG_INVALID.error(
            f"{path}: cannot be read as TOML ({e}).\nRepair the file."
        ) from e


def _profiles_table(doc: tomlkit.TOMLDocument, path: Path, *, create: bool = False):
    """The document's `profiles` table, proven to be one. [LAW:single-enforcer]

    A config with a top-level `profiles = "typo"` is valid TOML, and both readers of this
    key got it wrong in different ways: `_declare`'s `setdefault` handed the string back
    and the later item assignment raised a raw `TypeError`, while `declares` fell through
    to `name in "typo"` — a *substring* test that quietly answers True for a profile
    nobody declared. One returns a traceback, the other a wrong answer, and neither is a
    `CromError`.

    That matters more than it looks: `_bootstrap_user_config` reaches both on every
    command, before `config.parse` would have rejected the file — so the traceback was
    every command, with none left to repair it. Checking here means neither caller has to
    remember, and the wrong answer stops being expressible.
    """
    if create:
        profiles = doc.setdefault("profiles", tomlkit.table(is_super_table=True))
    else:
        profiles = doc.get("profiles", {})
    if not isinstance(profiles, dict):
        raise Reason.CONFIG_INVALID.error(
            f"{path}: `profiles` must be a table, not {type(profiles).__name__}.\n"
            f"Repair the file."
        )
    return profiles


@contextmanager
def _writing(path: Path) -> Iterator[None]:
    """Translate a filesystem failure into a `CromError` naming the file.

    [LAW:effects-at-boundaries] Every write in this module goes through here, so a full
    disk or a read-only mount names the file the user has: `_save` writes a `.tmp` and
    `os.replace`s it, so the raw `OSError` the CLI boundary reports names the temp file.
    Translating at the calls that touch the disk covers every caller rather than the one
    whose failure was traced — `migrate.run` reaches `_save` through `ensure_profile`
    before anything else in `main`.

    `CromError` passes through untouched: anything nested that raises one has already
    produced the precise diagnosis — the file it names, the key, the fix — and rewording
    it as a filesystem failure would send the reader to check the wrong fact. The clause
    guards the invariant rather than a known caller, which is what makes it safe to nest
    a checkpoint inside a write later without discovering this by way of a bad message.
    """
    try:
        yield
    except CromError:
        raise
    except OSError as e:
        raise Reason.CONFIG_UNWRITABLE.error(f"{path}: {e.strerror or e}") from e


def _save(path: Path, doc: tomlkit.TOMLDocument) -> None:
    """Replace the file atomically — this is a document a human wrote and may have
    committed, and `write_text` truncates before it writes. A failure partway through
    that would leave a half-written config, and crom's own parser is strict enough that
    the user would be locked out of every command until they repaired it by hand."""
    with _writing(path):
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(tomlkit.dumps(doc))
        os.replace(tmp, path)


def default_text(*, namespace: str | None, seed: Seed, base: Path) -> str:
    """The complete text of a fresh config for one scope — `None` names the user scope.

    The single answer to "what does a config crom writes look like", because three
    callers now need it and they must not each hold their own: `init_project` creates
    one, `config._reset_unreadable` writes one over a file crom can no longer parse, and
    `resolve._declare` creates one for a project whose config went missing. Two of those
    are repairs, and a repair that produced a *slightly* different file from the one
    `crom init` writes would be a second template drifting from the first.
    [LAW:one-source-of-truth]

    Both scopes get `[profiles.default]`, because every command that takes a ref defaults
    to `default` — a config crom wrote in which crom's own default does not resolve would
    be crom lying about its own contract.
    """
    if namespace is None:
        document = tomlkit.parse(USER_CONFIG_HEADER)
        document.setdefault("profiles", tomlkit.table(is_super_table=True))["default"] = (
            _stanza(ProfileSpec(name="default", seed=seed), base)
        )
        return tomlkit.dumps(document)

    return PROJECT_CONFIG_TEMPLATE.format(
        # `namespace` needs no quoting: `validate_name` is its checkpoint and admits only
        # `[a-z0-9._-]`, so a `ProfileRef`-legal namespace cannot carry a quote or a
        # newline. [LAW:parse-dont-validate]
        namespace=namespace,
        # A seed carries no such stamp — `chrome:<Profile Name>` takes any Chrome profile
        # name (only `/`, `~` and path components are refused) and `SeedPath` renders an
        # arbitrary filesystem path, so both can contain a `"`. Interpolating one into
        # `seed = "{seed}"` produced `seed = "chrome:My"Work"`: `crom init` reported
        # success and exit 0, and every command afterwards died on `invalid TOML` —
        # including the `crom init` and `crom rm` that might have repaired it.
        #
        # `tomlkit.item(...).as_string()` emits the value already quoted and escaped,
        # which is why the template reads `seed = {seed}`. This is the same library
        # `_declare` writes every other stanza through; `init_project` was the one path
        # hand-rolling the serialization, and `crom add --seed 'chrome:My"Work'` was
        # correct throughout. [LAW:single-enforcer] one thing knows how to spell a TOML
        # string.
        seed=tomlkit.item(render_seed(seed, base)).as_string(),
    )


def write_default(path: Path, *, namespace: str | None, seed: Seed) -> bool:
    """Create `path` as a fresh default config; report whether this call created it.

    The refusal is the kernel's, not a check of ours: `O_CREAT | O_EXCL` creates the
    file only if it does not exist, atomically. An `exists()` test followed by a write
    is check-then-act, and two `crom init` calls in one directory could both pass it and
    the second would clobber the first's config — possibly with a different namespace —
    while both reported success.

    Returning a bool rather than raising leaves the meaning of "it was already there" to
    the caller. Every caller now reads it the same way — as done — and uses it only to
    choose a verb: `crom init` says "wrote" or "already configures this project", and the
    repair paths in `config` and `resolve` say nothing at all. It stays a bool rather than
    becoming a silent no-op because "did this call create the file" is the one fact none
    of them can recover afterwards.
    """
    text = default_text(namespace=namespace, seed=seed, base=path.parent)
    # The `mkdir` sits inside the translation too, and the `FileExistsError` catch stays
    # scoped to `os.open` alone. `exist_ok=True` does not suppress a *non-directory* in a
    # path component, so `mkdir` has its own `FileExistsError` — one that means something
    # completely different from the collision this function reports, and would name the
    # wrong file if the two shared a handler.
    with _writing(path):
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            return False
        with os.fdopen(fd, "w") as handle:
            handle.write(text)
    return True


def value_at(path: Path, *keys: str) -> object | None:
    """What the config at `path` records at `keys`, or None when it records nothing there.

    The read behind `crom init`'s convergence: an already-initialised project is a
    conflict only when its file already says something *other* than what the user asked
    for, and answering that needs the file's own spelling of the facts `crom init` writes.

    Deliberately the raw TOML value rather than a parsed one. `config.parse` would reject
    the whole file over an unrelated defect — an unknown key, a typo'd `chrome_binary` —
    and `crom init` reporting one of those instead of "already initialised" would make
    the convergence conditional on the rest of the file being perfect. Comparison is
    against a string the caller renders, so no more than the raw value is needed.
    """
    value: object = _load(path)
    for key in keys:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def reset(path: Path, text: str) -> Path:
    """Overwrite `path` with `text`, keeping what was there beside it; return the kept copy.

    crom resets a config it cannot read rather than making the user repair it by hand,
    and a reset that deleted their file would be crom destroying the one artifact that
    says what they meant. So the displaced file is renamed, never removed, and the
    caller reports where it went. [LAW:no-silent-failure]

    The kept name is claimed with `O_CREAT | O_EXCL` and walked forward on collision, so
    a second reset cannot overwrite the first reset's evidence — which would lose exactly
    the data this function exists to preserve.

    The old file is *copied* aside and the new one swapped in with a single `os.replace`,
    so the config never stops existing. Renaming the original away and then writing the
    replacement leaves a window with no config at all: `discover` takes no lock, so a
    concurrent `crom up` in that project would fall through to the user scope and launch
    the wrong profile — and a write that failed in that window (a full disk, a read-only
    mount) would leave the project with nothing rather than with the file it started with.
    """
    with _writing(path):
        for attempt in count(1):
            kept = path.with_name(path.name + (".broken" if attempt == 1 else f".broken-{attempt}"))
            try:
                os.close(os.open(kept, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644))
            except FileExistsError:
                continue
            shutil.copyfile(path, kept)
            # Not `_save`: this is deliberately not a round-tripped edit of the user's
            # document. The document is what could not be parsed.
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(text)
            os.replace(tmp, path)
            return kept


def declares(path: Path, name: str) -> bool:
    """Whether `path` already declares a profile called `name`.

    Lets a caller find out that a name is taken *before* doing work that persists
    something — `crom add` reserves a port while resolving, and must not do that for a
    profile it is about to refuse.
    """
    return name in _profiles_table(_load(path), path)


def _declare(path: Path, spec: ProfileSpec, header: str) -> bool:
    """Append a `[profiles.<name>]` table for `spec`; report whether it was written.

    Returns False, having changed nothing, when the name is already declared — leaving
    each caller to say what that means for it. `header` is written only when the file is
    being created, so an existing file's own preamble is never duplicated or displaced.

    The whole load-mutate-save runs under an exclusive lock: two `crom add` calls
    against one config would otherwise both read the document before either wrote, and
    the later `_save` would drop the earlier profile while its process still reported
    success. Same hazard the port ledger takes a lock for, same lock.

    Creating a file requires a header, and this is the one place that says so.
    [LAW:single-enforcer] Without a header the created document has no `namespace` key,
    so the profile is appended to a file `config.parse` then rejects wholesale on the
    very next load — every command in that project failing on the file crom itself just
    wrote. `cli.add_cmd` guarded its own call against this, which protected that one
    caller rather than the invariant; enforcing it where the document is created covers
    every caller, including the tests that were quietly producing unparseable configs.
    """
    with exclusive(path):
        if not path.exists():
            if not header:
                raise Reason.CONFIG_HEADER_REQUIRED.error(
                    f"{path} does not exist, and crom will not create a config without a "
                    f"header declaring its namespace — the file would be written and then "
                    f"rejected by crom's own parser on the next command."
                )
            with _writing(path):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(header)

        doc = _load(path)
        profiles = _profiles_table(doc, path, create=True)
        if spec.name in profiles:
            return False

        profiles[spec.name] = _stanza(spec, path.parent)
        _save(path, doc)
        return True


def _stanza(spec: ProfileSpec, base: Path) -> tomlkit.items.Table:
    """A `[profiles.<name>]` table for `spec`, the only way one is ever built.

    A stanza records what the user *stated*, and an unstated seed is a real value in this
    vocabulary rather than a missing one: TOML spells "inherit `[defaults]`" as the
    absence of the key, and `resolve_spec` already reads that absence as
    `scope.default_seed`. Writing a seed nobody chose would freeze one day's default into
    the file and leave `[defaults].seed` silently powerless over every profile `crom add`
    ever created — the config format promising an inheritance the writer quietly
    overrode. [LAW:dataflow-not-control-flow] every key is rendered the same way and its
    value decides whether it appears.
    """
    stated = {
        "seed": None if spec.seed is None else render_seed(spec.seed, base),
        "flags": list(flags.render(spec.flags.sets)),
        # Sorted, because a `Layer`'s drops are a set: any order this rendered would be
        # an order the file did not ask for, and a stable one at least reads back the same
        # way twice.
        "drop_flags": sorted(spec.flags.drops) or None,
        "features": dict(spec.features) or None,
        "port": spec.port,
        "env": dict(spec.env) or None,
    }
    table = tomlkit.table()
    for key, value in stated.items():
        if value is not None:
            table[key] = value
    return table


def ensure_profile(path: Path, spec: ProfileSpec, *, header: str = "") -> bool:
    """Declare a profile unless it is already declared; report whether we wrote it.

    Every caller's goal is that the declaration *exist*, not that they be the one to
    create it, so a name already taken is a no-op rather than a failure. Migration needs
    this because it re-runs from the top after a failed attempt; `_bootstrap_user_config`
    needs it because two crom processes on a fresh machine both find no user config; and
    `crom add` needs it because a user asking for a profile that is already there has
    asked for a state the config is already in.

    The raising twin this replaced — `add_profile`, which turned a taken name into a
    `FileExistsError` for `crom add` to translate into exit 4 — made `crom add dev` twice
    a failure on the second call. Whether *this* process wrote the stanza is a fact about
    crom's plumbing, and it was being reported to the user as a fact about their project.
    [LAW:no-silent-failure] cuts the other way here than it looks: the bool still carries
    the difference, so `crom add` can say "Already declared" instead of "Declared", and
    nothing is hidden — only the exit code stops lying about whether the request was met.

    `crom add`'s check that the existing declaration *matches* what was asked for lives
    in the CLI, where the knowledge of which options the user actually typed lives. This
    is the writer; it has no opinion about that.
    """
    return _declare(path, spec, header)


def remove_profile(path: Path, name: str) -> None:
    """Delete a profile's declaration, under the same lock `_declare` writes beneath."""
    with exclusive(path):
        doc = _load(path)
        # Through `_profiles_table` like its two siblings: `rm_cmd` validates the config
        # once and then waits on an open-ended `click.confirm` outside the lock, so this
        # read is a fresh one against a file that may have been rewritten in between. A
        # non-table `profiles` reaching `name not in profiles` is a substring test on a
        # string or a raw `TypeError` on an int — neither a `CromError`, so neither
        # inside the CLI's exit-code contract. [LAW:single-enforcer]
        profiles = _profiles_table(doc, path)
        if name not in profiles:
            raise Reason.PROFILE_UNKNOWN.error(f"{path}: profile '{name}' is not declared here")
        del profiles[name]
        _save(path, doc)
