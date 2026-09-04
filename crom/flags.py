"""How a stanza becomes a layer, and how layers resolve into one launch list.

What a stanza can say becomes a layer here — a `flags` list of copy-pasteable Chrome
strings, a `drop_flags` list of switch names it wants gone, and a `features` table of
name -> on/off — and `compose` resolves whatever it is handed without knowing which
produced it. [LAW:composability]

crom emits each Chrome switch exactly once and never lets Chrome resolve a conflict,
because Chrome's rules are per-switch and not inferable. Measured against Google Chrome
151.0.7922.175: `--disable-features` given twice silently discards the first, while
`--disable-blink-features` — one word apart in name — is additive across repetitions.
No table of *which switches merge* could stay true for the switches nobody has tested, so
crom holds no such table. It relies instead on the one fact that is universal: anything
expressible as two occurrences of a switch is expressible as one comma-joined
occurrence, so emitting once costs no expressiveness.

That is what lets `flags` obey `profile > defaults > policy` like every other config
key, and `model.Flag` — a switch and its value, rather than an opaque string — is the
type that makes two layers' answers to one question visible as such.
[LAW:types-are-the-program]
"""

from collections.abc import Callable

from .model import Answer, Composed, Emitted, Flag, Layer, Reason, Removed, Resolution


# A switch name may not interpolate a variable, in either list that can hold one — this is
# the predicate and the phrase, written once because it is one rule. [LAW:single-enforcer]
# Both lists match a switch by the spelling the file used and `resolve._expand` runs
# afterwards, so a variable in a switch name can only ever fail to match: a drop would
# silently remove nothing, and a `flags` entry would silently fail to override, leaving crom
# to emit the same Chrome switch twice. Measured before this rule existed: `[defaults]`
# holding `--${CROM_PROFILE}-x=1` and `[profiles.dev]` holding `--dev-x=2` reached Chrome as
# `--dev-x=1 --dev-x=2`. [LAW:no-silent-failure]
#
# Only the switch half. `${...}` in a flag's *value* is the supported feature and is
# untouched — it is expanded after composition has already decided which switch wins, so
# nothing about it can make two names collide.
_INTERPOLATED_SWITCH = "interpolates a variable into the switch name"


def _interpolates(switch: str) -> bool:
    return "${" in switch


def layer(texts: list[str] | tuple[str, ...], where: str) -> tuple[Flag, ...]:
    """Parse one stanza's flag list, refusing a list that answers a question twice.

    [LAW:parse-dont-validate] the one place a list of flag texts becomes a layer, so a
    `tuple[Flag, ...]` that exists is already known to name each switch at most once and
    nothing downstream re-checks it.

    Repetition *between* layers is the whole feature — that is a profile overriding
    `[defaults]`, and `compose` resolves it. Repetition *within* one layer is a list
    disagreeing with itself: under single emission the earlier entry has no effect at
    all, so the author wrote something that cannot mean what it looks like. Silently
    keeping the last one would make crom the thing that discarded it.
    [LAW:no-silent-failure]

    `where` names the list, not the file, so the same rule can guard crom's own launch
    policy — a duplicate there is a crom bug, and it should be as loud as a user's.
    [LAW:single-enforcer]
    """
    seen: dict[str, Flag] = {}
    for text, flag in ((text, Flag.parse(text)) for text in texts):
        # Before the duplicate rule, for the reason `_ILLEGAL_DROPS` runs before the value
        # check: an interpolated switch cannot be meaningfully compared to another switch,
        # so the duplicate remedy would be advice about a name that is not the name.
        if _interpolates(flag.switch):
            raise Reason.FLAGS_INVALID.error(
                f"{where}: {text!r} {_INTERPOLATED_SWITCH}.\n"
                f"crom resolves each switch by the name the file spells and expands "
                f"variables only afterwards, so this names a switch no other layer can "
                f"meet — it would fail to override the same switch written literally, and "
                f"crom would hand Chrome both. Write the switch name literally; a flag's "
                f"value still interpolates: --load-extension=${{CROM_CONFIG_DIR}}/ext."
            )
        first = seen.get(flag.switch)
        if first is not None:
            raise Reason.FLAGS_INVALID.error(
                f"{where}: {flag.switch} is set twice, as {str(first)!r} and {str(flag)!r}.\n"
                f"crom emits each Chrome switch exactly once, so one of these would simply "
                f"be discarded. {_remedy(first, flag)}"
            )
        seen[flag.switch] = flag
    return tuple(seen.values())


# What the switch a drop entry names may not be, and how to say so. Every one of these
# could never match a switch any layer supplies, so the entry would drop nothing and say
# nothing — a config stating a removal crom does not perform, with no diagnostic anywhere.
# [LAW:no-silent-failure] A name that is merely wrong (`--disbale-sync`) is
# indistinguishable from one deliberately naming a switch no layer happens to set, which
# is allowed; these are the shapes that are wrong on their face.
#
# Read against `flag.switch`, not the raw text, and answered before the value check —
# because the value check's remedy *is* the switch, so the switch is what has to be legal
# for that remedy to work. `"--${FOO}=bar"` told the author to write `"--${FOO}"`, which
# fails again on the next load; `"=foo"` told them to write `""`. The same rule the
# reserved check already follows: a diagnostic whose remedy does not work is worse than
# none. The faults therefore describe the *name*, so the message stays true where name and
# entry differ — `"=foo"` names an empty switch without being an empty entry.
_ILLEGAL_DROPS: tuple[tuple[Callable[[str], bool], str], ...] = (
    (lambda switch: not switch.strip(), "names an empty switch"),
    (
        lambda switch: switch != switch.strip(),
        "names a switch with leading or trailing whitespace, which no switch carries",
    ),
    # The same rule `layer` applies to a `flags` entry's switch half, read from the same
    # predicate so the two lists cannot come to disagree about what a switch name may be.
    (_interpolates, _INTERPOLATED_SWITCH),
)


def drops(texts: list[str] | tuple[str, ...], where: str) -> frozenset[str]:
    """Parse one stanza's `drop_flags` list — switch names, each removing what it inherits.

    [LAW:parse-dont-validate] the one place a list of drop texts becomes the `drops` half
    of a `Layer`, so a `frozenset[str]` that exists here is already known to hold switch
    names and nothing else. `compose` intersects it with the switches resolved so far and
    never asks whether an entry was really a name.

    An entry carrying a value is refused rather than truncated to its switch. A drop
    removes the switch whatever value it inherited, so `--disable-sync=false` is a value
    that could not be honoured — silently ignoring it would let a config say something
    crom does not do. [LAW:no-silent-failure]

    A name is literal, and `${VAR}` is refused rather than expanded — the same rule
    `parse_features` applies to a feature name, for the same reason. crom never expands a
    drop: composition matches it against the switch half of a flag, which is the spelling
    the file used on both sides. So a variable written here could only ever fail to match,
    and the removal the config states would silently not happen.

    Repetition is refused for the reason `layer` refuses it: dropping a switch once
    removes it, so the second entry does nothing, and a list that answers a question
    twice is the author's mistake to see rather than crom's to absorb.
    """
    seen: list[str] = []
    for text in texts:
        flag = Flag.parse(text)
        for is_illegal, fault in _ILLEGAL_DROPS:
            if is_illegal(flag.switch):
                raise Reason.FLAGS_INVALID.error(
                    f"{where}: the entry {text!r} {fault}.\n"
                    f"crom matches each entry against the switches the layers below "
                    f"supply, exactly as written — so it has to be the literal switch "
                    f"name, e.g. --disable-sync."
                )
        if flag.value is not None:
            raise Reason.FLAGS_INVALID.error(
                f"{where}: {text!r} carries a value, and an entry here is a switch name.\n"
                f"A drop removes the switch whatever value it inherited, so the whole "
                f"entry is {flag.switch!r}."
            )
        if flag.switch in seen:
            raise Reason.FLAGS_INVALID.error(
                f"{where}: {flag.switch} is named twice. Dropping a switch once removes "
                f"it; write the name once."
            )
        seen.append(flag.switch)
    return frozenset(seen)


def _remedy(first: Flag, second: Flag) -> str:
    """What to write instead, for each shape two occurrences of one switch can take.

    The domain, enumerated exhaustively — every pair of `Flag`s sharing a switch falls in
    exactly one arm, and the arms are chosen so that none can be reached by a pair it was
    not written for:

    - **identical** (`--foo --foo`, `--foo=bar --foo=bar`) — a copy-paste, and the
      author's own text is the answer. This also absorbs the two-valueless pair, which
      cannot be distinct.
    - **distinct, both valued** — the comma-joined rewrite. Comma-joining is the form
      Chrome accepts for every list-valued switch measured, which is what makes single
      emission cost no expressiveness.
    - **distinct, kinds differ** (`--foo --foo=bar`) — the two disagree about whether the
      switch takes a value at all. crom names the disagreement and stops: for a switch
      whose bare and valued spellings mean different things, picking one and presenting
      it as the author's intent is a guess wearing a suggestion's clothes.
      [LAW:no-silent-failure]

    Written this way after two rounds of review found remedies applied to shapes they
    were not written for — first "with its values comma-joined" over a pair with nothing
    to join, then `--foo=bar,bar` over a pair with nothing distinct to join. Both came
    from a discriminator (how many occurrences carry a value) that did not separate the
    shapes that actually differ. [LAW:types-are-the-program] name the domain first; the
    remedies are residue.
    """
    values = [flag.value for flag in (first, second) if flag.value is not None]
    if first == second:
        return f"Write the switch once: {first}"
    if len(values) == 2:
        return (
            f"Write the switch once with its values comma-joined: "
            f"{Flag(first.switch, ','.join(values))}"
        )
    return (
        f"These disagree about whether {first.switch} takes a value, so only you can say "
        f"which was meant — write that one."
    )


# Which switch carries a feature in each state, and the only place those two switch
# names are written. `config.RESERVED_SWITCHES` reads them from here rather than
# spelling them again, so the switches `features` owns and the switches a `flags` list
# is refused for naming cannot come apart. [LAW:one-source-of-truth]
FEATURE_SWITCHES: dict[bool, str] = {True: "--enable-features", False: "--disable-features"}


def features(*tables: tuple[str, dict[str, bool]]) -> tuple[Emitted, ...]:
    """Fold each layer's feature table into the at-most-two switches that carry them.

    A feature is in one of three states — on, off, or unmentioned — and a table of
    name -> bool is exactly that domain: the two switches are a *rendering* of one fact,
    not two facts. This is what makes the measured Chrome behavior disappear rather than
    get implemented. `--disable-features=X` beats `--enable-features=X` in either order,
    so a naive pair of lists would have to encode that precedence; here the state selects
    the switch, so a name reaches exactly one of them and crom never emits the collision
    whose rule it would otherwise have to model. [LAW:types-are-the-program]

    There is no `drop_features` counterpart to `drop_flags`, and the asymmetry is a
    decision rather than an omission. A layer can flip an inherited feature but cannot
    return it to unmentioned, so "leave this one to Chrome's own default" is a state only
    the layer that first named the feature can express. That is a real gap and a small
    one: the three states are already reachable from any single config, and what is
    missing is only the ability to *withdraw* another layer's opinion. `drop_flags` earns
    its keep because a `flags` entry is a whole answer that can only be replaced by
    another whole answer — there is no way to say less than one. A `features` table is
    already per-name, so a layer below can say the opposite without erasing anything, and
    a second vocabulary for saying nothing at all would be a key nobody has needed.
    [LAW:no-mode-explosion]

    Layers fold later-wins for the same reason `compose` does, and by the same mechanism
    — a dict assignment — so `profile > defaults > policy` holds for a feature exactly as
    it holds for a flag. A profile writing `ChromeWhatsNewUI = true` takes effect by
    *removing* the name from the disable list, which is the only way it could take effect
    at all: an added `--enable-features` would lose to the disable that was still there.

    The result is emitted flags like any others, resolved here rather than by `compose`.
    They used to go through `compose` as a `Layer` of their own, which was uniform and
    told the report a lie: `compose` attributes a whole switch to the last layer that set
    it, and these two switches belong to no single layer — one `--disable-features` can
    carry names contributed by all three at once. `config.RESERVED_SWITCHES` refuses both
    switches inside any `flags` list, so no other layer can supply one and `compose` had
    nothing to resolve about them anyway; folding them here instead means each switch has
    exactly one place that decides its value *and* explains it. [LAW:one-source-of-truth]

    Each switch is omitted when no feature is in its state — an `--enable-features=` with
    an empty value is a switch Chrome is given and told nothing by, which is a different
    claim from the one crom means. The value is joined from the resolutions rather than
    from a second list of names, so what the switch says and what the report says it says
    cannot come apart.
    """
    # name -> the switch its state selects, and every layer's answer about it. The switch
    # is kept rather than the bool because a bool would have to be turned back into a
    # switch after the fold, while the answers keep the states in the vocabulary the file
    # wrote them in. [LAW:one-source-of-truth]
    resolved: dict[str, tuple[str, tuple[Answer, ...]]] = {}
    for origin, table in tables:
        for name, state in table.items():
            _, answers = resolved.get(name, ("", ()))
            resolved[name] = (
                FEATURE_SWITCHES[state],
                (*answers, Answer(origin, str(state).lower())),
            )

    carried: dict[str, list[Resolution]] = {switch: [] for switch in FEATURE_SWITCHES.values()}
    for name, (switch, answers) in resolved.items():
        carried[switch].append(Resolution(name, answers))
    return tuple(
        Emitted(Flag(switch, ",".join(r.question for r in resolutions)), tuple(resolutions))
        for switch, resolutions in carried.items()
        if resolutions
    )


def compose(*layers: Layer) -> Composed:
    """Resolve layers into the flags to launch with — later layers win, per switch.

    Given crom's launch policy, then `[defaults]`, then the profile, this is the rule
    `profile > defaults > policy` that every other config key already obeys. Each switch
    appears once in the result, at the position where it was *first* introduced and
    carrying the value the *last* layer gave it — the first from a dict key's insertion
    order and the second from the end of the answers appended under it, so the ordering is
    not a second mechanism to keep in step with the resolution.
    [LAW:dataflow-not-control-flow] no branch asks whether a switch is already present;
    the same append runs for every flag and the data decides what it means.

    The answers are kept rather than overwritten, which is what makes the fold its own
    report: which layer supplied a flag and which layers it outranked are visible only
    while the fold runs, so `crom config` would otherwise have to compose a second time to
    find them out. See `model.Resolution`. [LAW:one-source-of-truth]

    Position-of-first-introduction is what makes the order stable across runs and keeps
    crom's policy flags where a reader of `crom config` expects them, rather than
    shuffling the whole list because a profile overrode one early switch.

    A drop removes the switch *and its place in the list*, which is what
    position-of-first-introduction means once removal is expressible: nothing survives a
    drop to hold a position, so a lower layer setting the switch again is introducing it,
    not restoring it, and it lands where that layer's flags land. The guarantee above is
    unharmed — every *other* switch keeps its slot, and the one that moves has moved to
    the layer that actually supplies it, which is where a reader of `crom config` should
    find it. Remembering the old index instead would print the switch inside crom's policy
    block while a profile is the thing supplying it, and would keep a second record of
    order alive after the thing it ordered was deleted. [LAW:one-source-of-truth]

    A layer's drops apply to what it *inherits*, so they run before its own sets — which
    is the only reading under which dropping a switch and then setting it in the layer
    below could differ from setting it alone, and `config.parse_layer` refuses a stanza
    that does both, so the two orders are indistinguishable to any config crom accepts.
    The drop is expressed as an intersection rather than a lookup per name: the set of
    switches a layer's drops actually reach *is* `drops & resolved`, so no branch has to
    ask whether each name was present, and the same intersection answers both what to
    remove and what to report as removed. [LAW:dataflow-not-control-flow] The intersection
    is sorted only within the layer that produced it, and only because a `frozenset`'s
    iteration order is not a fact about the config — two runs must print the same line.
    Across layers the report stays in composition order, like the flags it is printed
    beneath: a global sort would spend a real fact about where each removal came from to
    buy an alphabet nobody asked for.

    [LAW:one-source-of-truth] this is the only answer to "what do this config's `flags`
    and `drop_flags` lists resolve to". `resolve_spec` builds argv from it and `crom add`
    compares against it, so the two cannot come to disagree about what a declaration
    means. The two feature switches are the one part of the launch list decided elsewhere,
    because they are decided per feature name rather than per switch — `features` owns
    them end to end, and `RESERVED_SWITCHES` keeps them out of every list this sees.
    """
    # switch -> every answer given to it, earliest first. Appending rather than replacing
    # is what turns the fold into its own report: the last entry is the value that wins,
    # which is the same fact the emitted flag carries, so the two cannot disagree about
    # who won. [LAW:one-source-of-truth] A plain `dict[str, Flag]` threw the losers away
    # and left `crom config` to re-run the composition to find them.
    resolved: dict[str, tuple[Answer, ...]] = {}
    dropped: dict[str, Removed] = {}
    for one_layer in layers:
        for switch in sorted(one_layer.drops & resolved.keys()):
            # The drop takes the whole argument with it, not just the winner — a user
            # hunting for a `[defaults]` flag that a profile overrode and then dropped is
            # owed both halves of why it is gone.
            dropped[switch] = Removed(one_layer.origin, Resolution(switch, resolved.pop(switch)))
        for flag in one_layer.sets:
            resolved[flag.switch] = (
                *resolved.get(flag.switch, ()),
                Answer(one_layer.origin, str(flag)),
            )
    # The emitted flag is the standing answer, parsed back from the text that answer
    # holds rather than kept beside it — `Flag.parse(str(flag))` is the identity for every
    # flag crom makes, since `__str__` joins at the same `=` that `parse` splits at, and one
    # value that renders the flag beats two that can drift. [LAW:one-source-of-truth]
    #
    # `dropped` reports what the reader actually lost, which is not everything a drop ever
    # removed: a layer below the drop may set the switch again, and reporting it as dropped
    # beside an argv that plainly contains it would be a false sentence about the list it
    # annotates. A dict, not a list, so a switch removed twice is still one thing that
    # happened.
    return Composed(
        tuple(
            Emitted(Flag.parse(answers[-1].said), (Resolution(switch, answers),))
            for switch, answers in resolved.items()
        ),
        tuple(record for switch, record in dropped.items() if switch not in resolved),
    )


def render(flags: tuple[Flag, ...]) -> tuple[str, ...]:
    """Flags back in the spelling a config file and a command line both use."""
    return tuple(str(flag) for flag in flags)
