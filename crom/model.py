"""The types crom's pipeline speaks, from a user-typed reference to a launchable spec.

The pipeline is a straight line with one shape per stage, and each stage's output type
is the proof that the stage ran:

    "myapp/dev"  --parse-->  ProfileRef
    ProfileRef + Scope + port  --resolve-->  ResolvedProfile  --launch-->  a process

[LAW:parse-dont-validate] Nothing downstream re-checks a name, re-reads a config file,
or re-derives a port: a `ResolvedProfile` could not have been constructed without all
of that already being true, so `chrome.launch` has nothing left to look up.
"""

import re
from dataclasses import dataclass, field
from enum import Enum, unique
from pathlib import Path


# Namespaces and profile names become both directory components and CLI tokens, so the
# legal set is the intersection of what is safe in each. [LAW:parse-dont-validate] this
# is checked once, where names enter, and never again.
#
# `\Z`, not `$`: Python's `$` also matches immediately before a trailing newline, so `$`
# would accept "dev\n" — a directory component carrying a newline, which then splits the
# process's line in two when `chrome.scan` reads `ps` output. `\Z` is the true end.
#
# The namespace every machine has without declaring one. A domain fact rather than a
# path fact: `paths.py` composes directories from it, and `model` needs it to answer
# whether a ref belongs to the implicit scope. Owning it here makes the dependency
# one-way — `paths` imports `model` for `CromError`, and nothing imports back.
# [LAW:one-way-deps]
USER_NAMESPACE = "user"

# The length cap is named rather than spelled into the pattern, because `slug_for`
# truncates to it — two hand-matched numbers would drift the first time either moved.
NAME_LIMIT = 64
_NAME_RE = re.compile(rf"^[a-z0-9][a-z0-9._-]{{0,{NAME_LIMIT - 1}}}\Z")

# What a namespace is called when nothing better can be derived from the directory.
FALLBACK_NAMESPACE = "project"


# What a failure carries beside its sentence: the values crom looked up on the way to
# refusing, in the shapes JSON already has. A `Path` is not one of them — whoever holds
# the path renders it, so the envelope writes what it is given rather than translating on
# the way out. For crom's own refusals that is the raise site; the one failure with no
# raise site, the OS's, is rendered by the boundary that receives it.
# [LAW:effects-at-boundaries]
FieldValue = str | int | tuple[str, ...] | None
Fields = dict[str, FieldValue]


class CromError(Exception):
    """A failure with a message meant for the user, raised anywhere below the CLI.

    Carries a `Reason` because it cannot be built without one, which is the whole
    enforcement: a raise that forgot to say why is a `TypeError`, not a gap a reviewer
    has to notice. [LAW:types-are-the-program]

    `fields` is that same sentence's data left unflattened — the namespaces crom knows,
    the profiles a config declares — and it is required for the same reason the reason
    is: a raise site cannot decide to omit it, because `Reason.error` builds it from what
    the reason declares, whether that is three names or none.

    `super().__init__` is handed the message alone. An `Exception` built with several
    arguments renders as the tuple — `str(e)` becomes `"('msg', <Reason...>)"` — which
    would corrupt both the sentence click prints to stderr and the envelope's `message`,
    the two the CLI asserts are one string rendered twice.
    """

    def __init__(self, message: str, reason: "Reason", fields: "Fields"):
        super().__init__(message)
        self.reason = reason
        self.fields = fields


class NotFound(CromError):
    """A referenced profile, namespace, or config file does not exist."""


class Conflict(CromError):
    """Two declarations claim the same resource — usually a port."""


@unique
class Reason(Enum):
    """Why a command failed, in one word a script can branch on — and the only table
    that says which failures crom can have.

    The exit code sorts every failure into four buckets, which is as fine as a published
    numeric contract can afford to be. It cannot tell "the port is held by another
    process" from "there is no usable Chrome" from "Chrome started and died", and those
    are three different next moves for whoever is reading. The slug is where that
    detail lives, so the numeric contract never has to grow to carry it.

    Each member also names the exception it raises, and `error()` is the only way any of
    them is built. So the class is *derived from* the reason rather than chosen beside
    it: there is no way to pair a not-found reason with `Conflict` and quietly ship exit
    4, because a raise site never names a class at all. [LAW:one-source-of-truth] one
    fact — what went wrong — and the class, the kind and the exit code are all
    projections of it.

    Lives here rather than beside `cli._ANSWERS` because every raise site is below the
    CLI and `model` is what they all already import; the table pointing the other way
    would be an upward dependency. [LAW:one-way-deps]

    Slugs are a promise: rename one and every script branching on it breaks silently, so
    a wrong-but-published name stays. `@unique` makes two reasons sharing a slug a
    definition-time error rather than an aliased member that answers to the wrong name.

    A reason also declares what it `carries` — the field names its envelope holds beside
    the sentence — and that is the fourth thing derived from this one fact, after the
    class, the kind and the exit code. A payload whose shape varies needs a discriminator
    to say which shape it is, and the reason already *is* the discriminator, so the schema
    belongs to it rather than beside it. [LAW:types-are-the-program] a reason answering
    with another reason's fields is unrepresentable: `error` builds the payload from
    `carries` and refuses anything else.

    A field earns its place the way a slug does, and by the same test read one level down:
    a slug separates next moves, and a field carries what crom *looked up* on the way to
    refusing. The namespaces that do exist, the profiles a config declares, the file
    already holding a name — a caller cannot reach any of those without matching English.
    The namespace it typed is not among them; echoing an argument back is a second copy of
    something the caller is already holding, which is why `namespace_reserved` carries
    nothing at all: the one reserved name is a constant, not a lookup.
    """

    def __new__(
        cls, slug: str, raises: type[CromError], carries: tuple[str, ...] = ()
    ) -> "Reason":
        member = object.__new__(cls)
        member._value_ = slug
        member.raises = raises
        member.carries = carries
        return member

    def error(self, message: str, **fields: FieldValue) -> CromError:
        """Build this reason's failure — the only way any `CromError` comes into being.

        The payload is rebuilt in `carries` order rather than kept in call order, so the
        envelope's keys read the same whichever way a raise site happened to name them:
        the declaration decides the shape, and a raise site only supplies values.
        [LAW:one-source-of-truth]

        A mismatch is a `TypeError` here and a failing suite before that — the sweep in
        `test_cli` reads every `Reason.<X>.error(...)` call in the package and compares its
        keywords against this table, because a raise site runs only once something has
        already gone wrong and a landmine in an error path is the worst place to leave one.
        """
        if fields.keys() != set(self.carries):
            raise TypeError(f"{self.value} carries {self.carries}, given {tuple(fields)}")
        return self.raises(message, self, {name: fields[name] for name in self.carries})

    # Nothing crom was asked for is there. (exit 3)
    # `path` is the file crom went looking at, which is never simply what the caller
    # typed: one site has expanded and resolved `CROM_CONFIG`, the other was handed a
    # path discovered by walking up from the working directory.
    CONFIG_MISSING = ("config_missing", NotFound, ("path",))
    # `known`, and deliberately not the namespace that was asked for: that one arrived in
    # the caller's own argument and repeating it back is a second copy of something the
    # caller is holding. The list of namespaces that *do* exist is the registry read crom
    # did on the caller's behalf, and the sentence is otherwise its only copy.
    NAMESPACE_UNKNOWN = ("namespace_unknown", NotFound, ("known",))
    # `source` is null for the one scope that has no file behind it — `user`, on a machine
    # whose user config has not been written yet, which is the null `Scope.source` already
    # documents. Null rather than the sentence's "your user config": that phrase is prose
    # for a human, and a script offered it as a path would try to open it.
    # [LAW:parse-dont-validate]
    PROFILE_UNKNOWN = ("profile_unknown", NotFound, ("source", "declared"))

    # Two claims on one resource, or a claim crom reserves for itself. (exit 4)
    # `namespace` here, where `namespace_unknown` refuses it, because this reason's loudest
    # case is a bare `crom init` whose namespace crom derived from the directory name —
    # the caller never typed it and has nothing to compare against `claimed_by`.
    NAMESPACE_CLAIMED = ("namespace_claimed", Conflict, ("namespace", "claimed_by"))
    # Nothing: the reserved name is a constant crom publishes, so a field would restate
    # `USER_NAMESPACE` to a caller that could read it from the slug alone.
    NAMESPACE_RESERVED = ("namespace_reserved", Conflict)
    # `port` and nothing finer, because it is what all three raise sites hold in common —
    # two profiles pinning one number, a pin on the number held for `user/default`, and a
    # number another profile already reserved. The one thing that would separate them is
    # who the other claimant is, and "the other claimant" is not the same thing in all
    # three, so naming it once would mean inventing it twice. [LAW:one-source-of-truth]
    PORT_CONFLICT = ("port_conflict", Conflict, ("port",))
    # Nothing: the floor of the search is crom's own constant and the machine being full
    # leaves a caller exactly one next move, so no field would separate anything.
    PORT_EXHAUSTED = ("port_exhausted", Conflict)
    # Not `profile_differs`: `_reject_restatement` raises this for `crom add` comparing a
    # profile's declaration *and* for `crom init` comparing the project's own namespace
    # and defaults, a command that names no profile at all. One slug rather than two
    # because the next move is the same either way — edit the file or change the request
    # — and a slug earns its place by separating next moves, not by being finer.
    # `settings` names which of them differ, in the config file's own key names rather
    # than any display label. The declared and asked-for values stay in the sentence: they
    # are for a human deciding which one is right, and a script that acted on them would be
    # editing the user's config from the text of a refusal.
    DECLARATION_DIFFERS = ("declaration_differs", Conflict, ("settings",))

    # A config file crom cannot act on.
    CONFIG_INVALID = ("config_invalid", CromError)
    CONFIG_HEADER_REQUIRED = ("config_header_required", CromError)
    CONFIG_UNWRITABLE = ("config_unwritable", CromError)
    FLAGS_INVALID = ("flags_invalid", CromError)
    VARIABLE_UNKNOWN = ("variable_unknown", CromError)

    # A name that will not survive crom's own machinery, from wherever it was typed.
    # Not grouped with the config file above: `parse_ref` raises this for a reference
    # typed at the CLI — `crom up a/b/c` — which never touched one.
    INVALID_NAME = ("invalid_name", CromError)

    # The port ledger crom keeps for itself — and, during migration, the legacy registry
    # `migrate._read_legacy` reads, which is a different file at a different path.
    REGISTRY_INVALID = ("registry_invalid", CromError)
    REGISTRY_UNSUPPORTED = ("registry_unsupported", CromError)

    # Launching, reaching, or stopping a browser. The distinctions the exit code cannot
    # draw and this ticket exists for: which of these you got decides whether retrying is
    # worth anything.
    CHROME_UNUSABLE = ("chrome_unusable", CromError)
    CHROME_LAUNCH_FAILED = ("chrome_launch_failed", CromError)
    CHROME_STARTUP_FAILED = ("chrome_startup_failed", CromError)
    CHROME_STOP_FAILED = ("chrome_stop_failed", CromError)
    CHROME_LOG_UNWRITABLE = ("chrome_log_unwritable", CromError)
    PORT_IN_USE = ("port_in_use", CromError)
    PORT_CHECK_FAILED = ("port_check_failed", CromError)
    PROCESS_TABLE_UNREADABLE = ("process_table_unreadable", CromError)

    # The lock `locking.exclusive` takes, which is not a browser fact: the config file,
    # the port ledger, the legacy registry and a profile directory are all taken under
    # it, so it sits under nearly every command rather than beneath the ones above.
    LOCK_UNAVAILABLE = ("lock_unavailable", CromError)

    # A seed directory crom will not or cannot copy.
    SEED_MISSING = ("seed_missing", CromError)
    SEED_UNREADABLE = ("seed_unreadable", CromError)
    SEED_UNSAFE = ("seed_unsafe", CromError)
    SEED_BUSY = ("seed_busy", CromError)

    # The move to the namespaced layout, which runs before every command until it takes.
    MIGRATION_BLOCKED = ("migration_blocked", CromError)
    MIGRATION_NEEDS_QUIET = ("migration_needs_quiet", CromError)

    # Pointing another tool at a profile.
    MCP_CONFIG_INVALID = ("mcp_config_invalid", CromError)
    MCP_KEY_TOO_LONG = ("mcp_key_too_long", CromError)

    # The desktop crom is running on.
    AUTOMATION_DENIED = ("automation_denied", CromError)
    PLATFORM_UNSUPPORTED = ("platform_unsupported", CromError)
    WINDOW_RAISE_FAILED = ("window_raise_failed", CromError)

    # Where crom keeps things, when the home directory that anchors it cannot be found.
    # `paths` resolves the XDG config and state directories under nearly every command,
    # so this is no more a desktop fact than the lock above is a browser one.
    HOME_UNKNOWN = ("home_unknown", CromError)

    # Housekeeping that failed after the request was understood.
    PROFILE_VANISHED = ("profile_vanished", CromError)
    PROFILE_DIR_UNDELETABLE = ("profile_dir_undeletable", CromError)

    # crom being wrong about its own state. Distinct from every reason above, which say
    # the request or the machine was wrong; this one says to file a bug.
    INTERNAL = ("internal", CromError)


def slug_for(text: str) -> str:
    """A directory name turned into something `validate_name` will accept.

    Lives beside `validate_name` and `NAME_LIMIT` because it is the inverse of them —
    the one rule for deriving a legal name from arbitrary text — and it now has two
    callers that must agree: `crom init` naming a new project, and `config`'s repair path
    naming a project whose config file can no longer say what its namespace was. Two
    spellings would let a reset config claim a different namespace from the one `crom
    init` gave it, which is a new set of profile directories and ports for the same
    project. [LAW:one-source-of-truth]

    Stripping `._-` from both ends, not just `-`: `.` and `_` survive the substitution
    because they are inside the allowed class, so a directory named `.dotfiles` or
    `_internal` used to slugify unchanged and then fail name validation — a confusing
    error from a command whose whole promise is that it works in any directory. Stripping
    them also lets an all-punctuation name fall through to the fallback.

    Truncated to the same 64 characters `validate_name` allows, and re-stripped
    afterwards so the cut cannot leave a trailing separator that fails on its own. A
    deeply nested build directory or a long branch checkout is a name crom can handle,
    not a reason to make the user pick one by hand.
    """
    slug = re.sub(r"[^a-z0-9._-]+", "-", text.lower()).strip("._-")
    return slug[:NAME_LIMIT].strip("._-") or FALLBACK_NAMESPACE


def validate_name(kind: str, value: str) -> str:
    if not _NAME_RE.match(value):
        raise Reason.INVALID_NAME.error(
            f"invalid {kind} {value!r}: must match {_NAME_RE.pattern} "
            f"(lowercase letters, digits, and . _ - ; starting alphanumeric)"
        )
    return value


# The legal range for a TCP port, stated once. [LAW:one-source-of-truth] `config.parse_port`
# rejects a configured port outside it, `registry._read` rejects a stored one, and
# `registry._allocate` stops searching at the top — three enforcers of one rule, which
# only stay in agreement if they read the same bound rather than each spelling it out.
MIN_PORT = 1
MAX_PORT = 65535


# --- seeds -------------------------------------------------------------------------
# Where a profile's user-data-dir comes from the first time it is created. Three
# variants, because there are exactly three sources: nothing, your real Chrome, or a
# directory on disk. [LAW:types-are-the-program] a `seed: str` would admit "chorme"
# and defer the failure to copy time.


@dataclass(frozen=True)
class SeedFresh:
    """An empty directory; Chrome initializes it on first launch."""


@dataclass(frozen=True)
class SeedChrome:
    """A copy of one of the user's real Chrome profile directories."""

    profile: str = "Default"


@dataclass(frozen=True)
class SeedPath:
    """A copy of a directory on disk — a checked-in fixture, or another profile."""

    path: Path


Seed = SeedFresh | SeedChrome | SeedPath


# Where a profile's data comes from when nothing says otherwise.
#
# [LAW:one-source-of-truth] This question used to be answered in five places, and two of
# the answers disagreed: `cli._bootstrap_user_config` seeded `user/default` from the real
# browser while `configwrite.PROJECT_CONFIG_TEMPLATE` wrote `fresh`. So the word `default`
# meant "your Chrome, with your logins" outside a project and "an empty browser" inside
# one, with nothing in any output marking the difference — someone who ran `crom init` and
# then `crom up` got a browser they could not use and no way to see why it differed from
# the one they had yesterday. Both templates now render *this*, so they cannot drift again.
#
# `default` — your default Chrome profile — is the answer because a profile with no cookies and no extensions cannot do the
# job crom exists for: driving a real session. It costs one copy of one Chrome profile
# directory at create time — `crom init --seed fresh` and `crom add --seed fresh` decline
# it, and `crom up` names the seed on stderr before it starts copying.
DEFAULT_SEED: Seed = SeedChrome()


# --- flags ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Flag:
    """One Chrome switch and the value it carries, if it carries one.

    A flag is not a string: it is an answer to a question — `--disable-features` asks
    "which features are off?" — and only a type that names the question can see that a
    profile and `[defaults]` are answering the same one. `flags.compose` is where that
    is acted on; this is the type it acts on. [LAW:types-are-the-program]

    Split at the *first* `=`, because a value may contain more of them
    (`--host-resolver-rules=MAP a.test b.test=1.2.3.4`). `value is None` is a switch
    that takes no value (`--no-pings`), which is a different thing from `value == ""`
    (`--foo=`), a switch given an empty one; `__str__` preserves both, so a flag written
    in a config round-trips back into argv exactly as typed.
    """

    switch: str
    value: str | None

    @classmethod
    def parse(cls, text: str) -> "Flag":
        switch, separator, value = text.partition("=")
        return cls(switch, value if separator else None)

    def __str__(self) -> str:
        return self.switch if self.value is None else f"{self.switch}={self.value}"


@dataclass(frozen=True)
class Layer:
    """One stanza's whole say over the launch flags: what it sets, drops, and is called.

    A stanza says the first two in two TOML keys — `flags` and `drop_flags` — but they
    are one fact: what this layer does to what it inherits. Carrying them as one value is
    what makes it impossible to compose a layer's flags while forgetting its drops. Two
    fields on `ProfileSpec` would leave every present and future composition site to
    remember the pairing by convention, and a site that forgot would silently launch
    with a switch the config removed. [LAW:types-are-the-program]

    `sets` is ordered because position-of-first-introduction decides where a switch
    lands in argv. `drops` is a set because a drop names a switch and says nothing else
    about it, so neither order nor multiplicity could carry meaning.

    `origin` is how a reader of `crom config` will hear this stanza named — `[defaults]`,
    `[profiles.dev]`, `crom's launch policy` — and it is a field rather than something the
    renderer looks up because only the stanza's own parser knows it. A layer that says
    something must say where it was said: `__post_init__` refuses a non-empty layer with
    no name, so `flags.compose` cannot produce a flag whose origin renders as a blank.
    [LAW:types-are-the-program] Only a layer that says something needs a name, which is
    why the field can carry a default at all: a bare `Layer()` contributes nothing to the
    report, so there is nothing for a name to label.

    `sets` and `drops` are disjoint, and this constructor is what makes that true rather
    than `config.parse_layer` being careful: a layer that both sets and drops one switch
    has said two things where only one can mean anything, and refusing it here means
    nothing downstream has to decide which wins. Enforced in `__post_init__` for the
    reason `ProfileSpec` gives above — a convention every future call site must
    rediscover becomes a property of the type. [LAW:types-are-the-program]

    The rule is stated once, here. `parse_layer` catches this and prepends the file and
    stanza it was reading, because the location is what *it* knows and the rule is what
    *this* knows; a second copy of the check upstream, written to produce a better
    message, is the copy that would drift. [LAW:single-enforcer]
    """

    sets: tuple[Flag, ...] = ()
    drops: frozenset[str] = frozenset()
    origin: str = ""

    def __post_init__(self) -> None:
        if (self.sets or self.drops) and not self.origin:
            raise Reason.INTERNAL.error(
                "a layer that sets or drops a flag must name where it was written; "
                "this is a crom bug, not a fault in any config"
            )
        both = sorted(self.drops & {flag.switch for flag in self.sets})
        if both:
            raise Reason.FLAGS_INVALID.error(
                f"both sets and drops {', '.join(both)}.\n"
                f"A drop removes a switch this stanza inherits, and setting it here "
                f"already replaces whatever was inherited — so one of the two cannot mean "
                f"anything. Keep the flag to override the switch, or the drop to remove it."
            )


# How a stanza is named to the user, in the spelling their config file uses. Written here
# rather than at each site that needs one, because three of them must agree: `config`
# labels the layer it parses, `cli` labels the stanza `crom add` writes, and `resolve`
# labels the `features` table that sits beside the same stanza's `flags`. Two spellings
# would put one stanza under two names in a single `crom config` listing.
# [LAW:one-source-of-truth]
DEFAULTS_STANZA = "[defaults]"


def profile_stanza(name: str) -> str:
    return f"[profiles.{name}]"


# --- how the launch flags came to be -------------------------------------------------
#
# Every flag crom emits is the end of an argument between layers, and `crom config` is
# where that argument is readable. The unit of the report is not a flag but a *question*:
# for an ordinary switch the question is the switch itself, and for a feature it is the
# feature name — because `flags.features` folds per name into a switch every layer
# contributes to at once, so "the profile overrode the policy's `--disable-features`" is a
# false sentence about a union. Both are one mechanism at different grains: an emitted
# switch carries the resolutions that decided it, exactly one in the ordinary case and one
# per feature name in the two that features own. [LAW:one-type-per-behavior]


@dataclass(frozen=True)
class Answer:
    """What one layer said about one question, in the spelling its stanza used.

    `said`, not `value`: this is what a layer *stated*, which is a different fact from what
    Chrome is given. They part company whenever a flag's value interpolates — the stanza
    said `--load-extension=${CROM_CONFIG_DIR}/ext` and Chrome gets an absolute path — and a
    field called `value` invited a reader to treat the two as one. The stated spelling is
    the useful one here: this text is printed so a user can find the flag they wrote, and
    for a dropped flag it is the *only* honest text, since nothing was launched for an
    expansion to describe.

    A rendered string rather than a `Flag` or a bool because the two producers answer
    differently shaped questions — a whole flag, or a feature's `true`/`false` — and the
    shape is known only where the answer is made. Rendering it there keeps one report type
    for both instead of a union the renderer would have to take apart.
    """

    layer: str
    said: str

    def describe(self) -> dict:
        return {"layer": self.layer, "said": self.said}


@dataclass(frozen=True)
class Resolution:
    """One question, and every answer given to it in the order the layers spoke.

    The last answer stands and the earlier ones were replaced, which is the same
    later-wins rule the fold itself runs on — so the report cannot disagree with the
    composition about who won. [LAW:one-source-of-truth]

    Answer values stay unexpanded — `${CROM_PORT}` is shown as the file spells it, because
    a user looking for the flag they wrote is looking for the text they typed. The question
    has no such choice to make: `flags.layer` refuses a variable in a switch name and
    `parse_features` refuses one in a feature name, so a question has only ever one
    spelling.
    """

    question: str
    answers: tuple[Answer, ...]

    def __post_init__(self) -> None:
        if not self.answers:
            raise Reason.INTERNAL.error(
                f"nothing was resolved for {self.question!r}; this is a crom bug, "
                f"not a fault in any config"
            )

    @property
    def stands(self) -> Answer:
        return self.answers[-1]

    @property
    def replaced(self) -> tuple[Answer, ...]:
        return self.answers[:-1]

    def describe(self) -> dict:
        """The whole resolution — every answer, and which one stands.

        The standing answer is carried rather than left to a sibling channel. `Emitted` has
        such a channel (its flag) and `Removed` has none, so omitting it reported a dropped
        switch with no trace of what was lost. A rendering complete only when read from one
        of its two directions is a map true in one direction. [FRAMING:representation]

        `stands` and `over` render through the same `Answer.describe()`, so an answer looks
        the same wherever it appears. Flattening the standing one into sibling keys made it
        the one answer with a bespoke shape, and named its text `value` — which invited the
        reading that it must equal the emitted flag. It does not: `Emitted.flag` is what
        Chrome is given and `stands.said` is what the layer stated, and the two differ
        exactly when the value interpolates a variable. [LAW:one-type-per-behavior]
        """
        return {
            "question": self.question,
            "stands": self.stands.describe(),
            "over": [answer.describe() for answer in self.replaced],
        }


@dataclass(frozen=True)
class Emitted:
    """One switch crom puts on the command line, and the questions that decided it."""

    flag: Flag
    why: tuple[Resolution, ...]

    def describe(self) -> dict:
        return {"flag": str(self.flag), "why": [r.describe() for r in self.why]}


@dataclass(frozen=True)
class Removed:
    """A switch a `drop_flags` entry took away, and the resolution it took with it.

    `by` is the layer that dropped; `what` is everything that had been said about the
    switch up to that point. Both halves are needed to answer the question this record
    exists for — a user who wrote a flag and cannot find it in argv wants to know who
    removed it *and* that it was theirs.
    """

    by: str
    what: Resolution

    def describe(self) -> dict:
        return {"by": self.by, **self.what.describe()}


@dataclass(frozen=True)
class Composed:
    """What every layer agreed on: the flags to launch with, and the switches removed.

    Neither field can be derived from the other, nor from the flags alone. A switch no
    layer ever set and a switch a layer removed are both simply absent from `emitted`, and
    which layer supplied a flag stops being visible the moment the fold has folded — both
    differences exist only while it is running, which is why `flags.compose` reports them
    here rather than leaving `crom config` to reconstruct them from the layers a second
    time. [LAW:one-source-of-truth]

    Only switches a drop actually removed appear in `dropped`. Dropping a switch nothing
    supplies is allowed and changes nothing, so reporting it as dropped would tell the
    reader a layer below had set something it never did.
    """

    emitted: tuple[Emitted, ...] = ()
    dropped: tuple[Removed, ...] = ()

    @property
    def flags(self) -> tuple[Flag, ...]:
        """Just the flags, for the callers that are launching rather than explaining."""
        return tuple(item.flag for item in self.emitted)


# --- declarations ------------------------------------------------------------------


@dataclass(frozen=True)
class ProfileRef:
    """A profile's global identity: which namespace, and which profile within it.

    Both fields are validated here, so the module docstring's "checked once, where they
    enter, and never again" is a property of the type rather than of caller discipline.
    [LAW:parse-dont-validate] the constructor is the checkpoint: a `ProfileRef` that
    exists names two legal components, and `resolve_spec` can compose
    `profiles_root / ref.namespace / ref.name` without wondering whether either could
    carry a `..` or a separator.
    """

    namespace: str
    name: str

    def __post_init__(self) -> None:
        validate_name("namespace", self.namespace)
        validate_name("profile name", self.name)

    def __str__(self) -> str:
        return f"{self.namespace}/{self.name}"


@dataclass(frozen=True)
class ProfileSpec:
    """One `[profiles.<name>]` stanza, parsed but not yet resolved against a port.

    `name` is validated here for the same reason `ProfileRef` validates both of its
    fields: it is the profile's identity, and it is written to places that cannot
    re-check it. [LAW:parse-dont-validate] `configwrite._declare` indexes
    `profiles[spec.name]` straight into a TOML document, and `resolve_spec` composes
    `ProfileRef(scope.namespace, spec.name)` — so a name carrying a `/` or a `..` would
    become an illegal TOML key and a profile directory outside the profiles root.

    Every existing caller happens to validate first, which is exactly the arrangement
    this constructor replaces: a convention every future call site must rediscover
    becomes a property of the type. [LAW:types-are-the-program]
    """

    name: str
    # A `Layer`, not a list of strings: the stanza's flags have been through
    # `config.parse_layer`, which is where a list naming one switch twice is refused, and
    # carrying the parsed form is what lets `flags.compose` see that this profile's
    # `--disable-blink-features` and `[defaults]`'s are two answers to one question.
    # [LAW:parse-dont-validate]
    flags: Layer = Layer()
    # Which Chrome features this stanza turns on or off. A table rather than two lists,
    # because a feature is in one of three states — on, off, or unmentioned — and two
    # lists would let one name appear in both. `flags.features` folds this across layers
    # and renders it; an absent name is unmentioned and reaches neither switch.
    features: dict[str, bool] = field(default_factory=dict)
    env: dict[str, str] = field(default_factory=dict)
    seed: Seed | None = None
    port: int | None = None

    def __post_init__(self) -> None:
        validate_name("profile name", self.name)


def _config_dir(source: Path | None) -> Path:
    """The directory a config's relative paths resolve against.

    [LAW:one-source-of-truth] `Scope` and `ResolvedProfile` both expose this and must
    agree — a profile's seed is parsed against the scope's answer and rendered back
    against the profile's, so a divergence would write a path that reads back as a
    different one. Stating the rule once is what makes them agree; two copies with a
    comment claiming they match is the arrangement that silently stops being true.

    The fileless `user` scope has no directory of its own, so it anchors on the working
    directory — which is why this is a function of `source` alone and nothing else.
    """
    return source.parent if source else Path.cwd()


@dataclass(frozen=True)
class Scope:
    """One config file's contents: a namespace, its defaults, and its profiles.

    `source` is None only for the implicit `user` scope on a machine with no user
    config file yet — the one case where a scope exists without a file behind it.
    """

    namespace: str
    source: Path | None
    profiles_root: Path
    chrome_binary: Path
    default_flags: Layer = Layer()
    default_features: dict[str, bool] = field(default_factory=dict)
    default_env: dict[str, str] = field(default_factory=dict)
    # `DEFAULT_SEED`, not a literal — this dataclass default is the sixth answer to the
    # question that constant exists to answer, and it sat 80 lines below the comment
    # claiming the drift was gone. It is reachable, not decorative: `load_user_scope`
    # builds a fileless `Scope` without this field on a machine with no user config, so
    # that scope reported `fresh` while every file-backed scope reported `chrome`.
    default_seed: Seed = DEFAULT_SEED
    profiles: dict[str, ProfileSpec] = field(default_factory=dict)

    @property
    def config_dir(self) -> Path:
        """The directory a project's relative paths and ${CROM_CONFIG_DIR} resolve against."""
        return _config_dir(self.source)

    @property
    def is_user(self) -> bool:
        return self.namespace == USER_NAMESPACE


# --- the stamped type --------------------------------------------------------------


@dataclass(frozen=True)
class ResolvedProfile:
    """Everything needed to launch, seed, find, or describe one profile.

    `argv` is complete: `subprocess.Popen(resolved.argv)` is the entire launch. This is
    the seam the rest of crom is built on — [LAW:composability] every consumer takes
    this one type, so none of them needs to know that config files or registries exist.
    """

    ref: ProfileRef
    port: int
    profile_dir: Path
    chrome_binary: Path
    argv: tuple[str, ...]
    env: dict[str, str]
    seed: Seed
    source: Path | None
    # How the flags in `argv` came to be — which layer supplied each one, what it
    # replaced, and which switches a `drop_flags` entry removed. Unrecoverable from `argv`,
    # which is the whole reason it is carried: a flag a user wrote can legitimately not
    # appear there, and `crom config` can say why instead of leaving the reader to notice
    # that something they wrote in `[defaults]` is missing. [LAW:no-silent-failure]
    #
    # The flags here are the expanded ones `argv` was built from, so `crom config` can
    # match a report line to a command line by the text itself rather than by counting
    # positions into a list whose ends crom owns.
    provenance: Composed = Composed()

    @property
    def cdp_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def config_dir(self) -> Path:
        """The directory this profile's relative paths are written and read against.

        The same notion `Scope.config_dir` carries, available downstream of resolution
        so a seed can be rendered back in the spelling its config file would use.
        """
        return _config_dir(self.source)

    def describe(self, *, running: bool, pids: tuple[int, ...]) -> dict:
        """The machine-readable view — the contract `--json` output promises.

        [LAW:one-source-of-truth] every command that emits JSON emits *this*, so the
        shape a consuming app parses is defined in exactly one place.
        """
        return {
            "namespace": self.ref.namespace,
            "profile": self.ref.name,
            "ref": str(self.ref),
            "port": self.port,
            "cdp_url": self.cdp_url,
            "profile_dir": str(self.profile_dir),
            "chrome_binary": str(self.chrome_binary),
            "source": str(self.source) if self.source else None,
            "running": running,
            "pids": list(pids),
        }


@dataclass(frozen=True)
class FailedProfile:
    """A declared profile that could not be resolved, carried as a value not a raise.

    `crom list` is the command a user reaches for *because* something is broken, so one
    unresolvable declaration must not hide every working one. [LAW:no-silent-failure]
    this is not a swallow: the failure is rendered inline in the listing and present in
    `--json`, so it is strictly more visible than the traceback it replaces — what
    changes is that it no longer takes the other profiles down with it.

    [LAW:dataflow-not-control-flow] `resolve_all` returns this alongside
    `ResolvedProfile`, so the variability is in the values the caller matches on rather
    than in whether the listing runs.
    """

    ref: ProfileRef
    error: str

    def describe(self) -> dict:
        return {
            "namespace": self.ref.namespace,
            "profile": self.ref.name,
            "ref": str(self.ref),
            "error": self.error,
        }


# What one entry in a listing is: resolved, or explaining why it is not.
ProfileEntry = ResolvedProfile | FailedProfile


def namespace_of(ref: str) -> str:
    """The namespace a ledger key names, for a reader that must survive a bad key.

    `ProfileRef.__str__` owns the `namespace/name` format and this is its inverse, here
    rather than spelled again at the one call site. [LAW:one-source-of-truth]

    Total where `parse_ref` is partial, and deliberately so. `parse_ref` raises on a key
    with a second `/` or a component that fails `validate_name`, and a hand-edited ledger
    is the only way to release an orphaned reservation today — so `crom doctor`, the
    command a person runs *because* the ledger is a mess, cannot afford a reader that dies
    on one. A namespace this returns is a name to look for on disk, never a name to act
    on: the worst a nonsense key yields is a directory that does not exist, which its
    caller already answers with "nothing there". [LAW:no-silent-failure] guessing is safe
    here only because the guess is checked against the filesystem before it means anything.
    """
    return ref.split("/", 1)[0]


def parse_ref(text: str, ambient: str) -> ProfileRef:
    """Parse a user-typed reference; a bare name resolves in the ambient namespace.

    `dev` -> ambient/dev, `myapp/dev` -> myapp/dev. Anything else is an error rather
    than a guess — [LAW:no-silent-failure] a mistyped reference must not quietly
    become a different profile.
    """
    parts = text.split("/")
    if len(parts) == 1:
        namespace, name = ambient, parts[0]
    elif len(parts) == 2:
        namespace, name = parts
    else:
        raise Reason.INVALID_NAME.error(
            f"invalid profile reference {text!r}: expected 'name' or 'namespace/name'"
        )
    # No validation here: splitting is this function's job, and `ProfileRef` validates
    # its own fields. [LAW:single-enforcer] one place decides what a legal name is.
    return ProfileRef(namespace, name)
