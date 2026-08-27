"""Edits crom config files in place, preserving the comments and layout around the edit.

`crom add` and `crom rm` change files a human wrote and may keep in version control, so
they are edits, not rewrites: tomlkit round-trips the document and we touch only the one
table involved. [LAW:one-source-of-truth] the file on disk stays the authority on
everything crom did not explicitly change.
"""

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import tomlkit

from .locking import exclusive
from .model import (
    Conflict,
    CromError,
    NotFound,
    ProfileSpec,
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
#   chrome                  a copy of your everyday Chrome profile — the `Default`
#                           directory — so this browser has your logins and extensions
#   chrome:<dir>            a copy of a different profile directory inside your Chrome.
#                           Name the DIRECTORY (`Default`, `Profile 1`, `Profile 2`),
#                           not the label Chrome shows in its profile menu — a profile
#                           whose directory is `Default` often displays as `Person 1`.
#                           `chrome` and `chrome:Default` mean the same thing.
#   fresh                   an empty profile; Chrome sets it up on first launch
#   ./path/to/user-data-dir a copy of a directory you keep yourself
#
# Flags are appended after crom's launch policy. Available in flags and env values:
# ${{CROM_PROFILE_DIR}}, ${{CROM_CONFIG_DIR}}, ${{CROM_PORT}}, ${{CROM_NAMESPACE}},
# ${{CROM_PROFILE}}.
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
            return "chrome"
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
    except (tomlkit.exceptions.ParseError, OSError) as e:
        raise CromError(f"{path}: cannot be read as TOML ({e}).\nRepair the file.") from e


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
        raise CromError(
            f"{path}: `profiles` must be a table, not {type(profiles).__name__}.\n"
            f"Repair the file."
        )
    return profiles


@contextmanager
def _writing(path: Path) -> Iterator[None]:
    """Translate a filesystem failure into a `CromError` naming the file.

    [LAW:effects-at-boundaries] Every write in this module goes through here, because
    `CromGroup.invoke` catches only `CromError`: a full disk or a read-only mount raised
    a bare `OSError` that escaped the CLI's exit-code contract as a traceback. Putting
    the translation at the calls that touch the disk covers every caller rather than the
    one whose failure was traced — including `migrate.run`, which reaches `_save` through
    `ensure_profile` before anything else in `main` and would therefore have produced
    that traceback on *every* command.

    `CromError` passes through untouched so a `Conflict` raised inside — `init_project`
    reporting a config that already exists — keeps its own meaning and its exit code.
    """
    try:
        yield
    except CromError:
        raise
    except OSError as e:
        raise CromError(f"{path}: {e.strerror or e}") from e


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


def init_project(path: Path, namespace: str, seed: Seed) -> None:
    """Write a new project config, or refuse if one is already there.

    `seed` is written into `[defaults]` rather than defaulted in code, so the project's
    answer to "where does a new profile's data come from" is visible in the file the
    team commits. It arrives already parsed — `cli.init_cmd` runs `--seed` through
    `config.parse_seed`, the same checkpoint a value read back from this file goes
    through. [LAW:parse-dont-validate] there is no second spelling of the vocabulary here.

    The refusal is the kernel's, not a check of ours: `O_CREAT | O_EXCL` creates the
    file only if it does not exist, atomically. An `exists()` test followed by a write
    is check-then-act, and two `crom init` calls in one directory could both pass it and
    the second would clobber the first's config — possibly with a different namespace —
    while both reported success.

    `Conflict` rather than the bare `FileExistsError` the kernel hands back: this is the
    exit-4 case the CLI contract promises. Raised here rather than translated at the call
    site so no future caller can reintroduce the gap by forgetting a wrapper.
    """
    # The `mkdir` sits inside the translation too, and the `FileExistsError` catch stays
    # scoped to `os.open` alone. `exist_ok=True` does not suppress a *non-directory* in a
    # path component, so `mkdir` has its own `FileExistsError` — one that means something
    # completely different from the collision this function reports, and would name the
    # wrong file if the two shared a handler.
    with _writing(path):
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError as e:
            raise Conflict(f"{path} already exists") from e
        with os.fdopen(fd, "w") as handle:
            handle.write(
                PROJECT_CONFIG_TEMPLATE.format(
                    # `namespace` needs no quoting: `validate_name` is its checkpoint and
                    # admits only `[a-z0-9._-]`, so a `ProfileRef`-legal namespace cannot
                    # carry a quote or a newline. [LAW:parse-dont-validate]
                    namespace=namespace,
                    # A seed carries no such stamp — `chrome:<dir>` takes any
                    # Chrome profile name (only `/`, `~` and path components are refused)
                    # and `SeedPath` renders an arbitrary filesystem path, so both can
                    # contain a `"`. Interpolating one into `seed = "{seed}"` produced
                    # `seed = "chrome:My"Work"`: `crom init` reported success and exit 0,
                    # and every command afterwards died on `invalid TOML` — including the
                    # `crom init` and `crom rm` that might have repaired it.
                    #
                    # `tomlkit.item(...).as_string()` emits the value already quoted and
                    # escaped, which is why the template now reads `seed = {seed}`. This
                    # is the same library `_declare` writes every other stanza through;
                    # `init_project` was the one path hand-rolling the serialization, and
                    # `crom add --seed 'chrome:My"Work'` was correct throughout.
                    # [LAW:single-enforcer] one thing knows how to spell a TOML string.
                    seed=tomlkit.item(render_seed(seed, path.parent)).as_string(),
                )
            )


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
                raise CromError(
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

        # A stanza records what the user *stated*, and an unstated seed is a real value in
        # this vocabulary rather than a missing one: TOML spells "inherit `[defaults]`" as
        # the absence of the key, and `resolve_spec` already reads that absence as
        # `scope.default_seed`. Writing a seed nobody chose would freeze one day's default
        # into the file and leave `[defaults].seed` silently powerless over every profile
        # `crom add` ever created — the config format promising an inheritance the writer
        # quietly overrode. [LAW:dataflow-not-control-flow] every key is rendered the same
        # way and its value decides whether it appears.
        stated = {
            "seed": None if spec.seed is None else render_seed(spec.seed, path.parent),
            "flags": list(spec.flags),
            "port": spec.port,
            "env": dict(spec.env) or None,
        }
        table = tomlkit.table()
        for key, value in stated.items():
            if value is not None:
                table[key] = value
        profiles[spec.name] = table
        _save(path, doc)
        return True


def add_profile(path: Path, spec: ProfileSpec, *, header: str = "") -> None:
    """Declare a profile the user asked to create. A name already taken is a conflict."""
    if not _declare(path, spec, header):
        raise FileExistsError(f"{path}: profile '{spec.name}' is already declared")


def ensure_profile(path: Path, spec: ProfileSpec, *, header: str = "") -> bool:
    """Declare a profile unless it is already declared; report whether we wrote it.

    The converging twin of `add_profile`, for callers whose goal is that the declaration
    *exist* rather than that they be the one to create it. Migration is the caller that
    needs this: it re-runs from the top after a failed attempt, so a name it already
    wrote must be a no-op rather than a collision.
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
            raise NotFound(f"{path}: profile '{name}' is not declared here")
        del profiles[name]
        _save(path, doc)
