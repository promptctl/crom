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

from .model import Composed, CromError, Flag, Layer


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
    for flag in (Flag.parse(text) for text in texts):
        first = seen.get(flag.switch)
        if first is not None:
            raise CromError(
                f"{where}: {flag.switch} is set twice, as {str(first)!r} and {str(flag)!r}.\n"
                f"crom emits each Chrome switch exactly once, so one of these would simply "
                f"be discarded. {_remedy(first, flag)}"
            )
        seen[flag.switch] = flag
    return tuple(seen.values())


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

    Repetition is refused for the reason `layer` refuses it: dropping a switch once
    removes it, so the second entry does nothing, and a list that answers a question
    twice is the author's mistake to see rather than crom's to absorb.
    """
    seen: list[str] = []
    for text in texts:
        flag = Flag.parse(text)
        if flag.value is not None:
            raise CromError(
                f"{where}: {text!r} carries a value, and an entry here is a switch name.\n"
                f"A drop removes the switch whatever value it inherited, so the whole "
                f"entry is {flag.switch!r}."
            )
        if flag.switch in seen:
            raise CromError(
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


def features(*tables: dict[str, bool]) -> tuple[Flag, ...]:
    """Fold feature tables into the at-most-two switches that carry them.

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

    The result is flags like any others — `resolve_spec` hands them to `compose` as a
    `Layer` of their own, and `compose` needs no case for it. A `Layer` rather than
    something narrower because that is what `compose` takes; the fold has no drops to
    contribute, since a feature is turned off by naming it, never by removing a switch.
    Each switch is omitted when no feature is in its state — an `--enable-features=` with an empty
    value is a switch Chrome is given and told nothing by, which is a different claim
    from the one crom means.
    """
    resolved: dict[str, bool] = {}
    for table in tables:
        resolved.update(table)

    named: dict[str, list[str]] = {switch: [] for switch in FEATURE_SWITCHES.values()}
    for name, state in resolved.items():
        named[FEATURE_SWITCHES[state]].append(name)
    return tuple(Flag(switch, ",".join(names)) for switch, names in named.items() if names)


def compose(*layers: Layer) -> Composed:
    """Resolve layers into the flags to launch with — later layers win, per switch.

    Given crom's launch policy, then `[defaults]`, then the profile, this is the rule
    `profile > defaults > policy` that every other config key already obeys. Each switch
    appears once in the result, at the position where it was *first* introduced and
    carrying the value the *last* layer gave it — which is what a dict assignment does,
    so the ordering is not a second mechanism to keep in step with the resolution.
    [LAW:dataflow-not-control-flow] no branch asks whether a switch is already present;
    the same assignment runs for every flag and the data decides what it means.

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
    remove and what to report as removed. [LAW:dataflow-not-control-flow] Sorted, because
    a set's iteration order is not a fact about the config and `crom config` prints this.

    [LAW:one-source-of-truth] this is the only answer to "what flags does this profile
    launch with". `resolve_spec` builds argv from it and `crom add` compares against it,
    so the two cannot come to disagree about what a declaration means.
    """
    resolved: dict[str, Flag] = {}
    dropped: dict[str, None] = {}
    for one_layer in layers:
        for switch in sorted(one_layer.drops & resolved.keys()):
            del resolved[switch]
            dropped[switch] = None
        for flag in one_layer.sets:
            resolved[flag.switch] = flag
    # What the reader actually lost, which is not everything a drop ever removed: a layer
    # below the drop may set the switch again, and reporting it as dropped beside an argv
    # that plainly contains it would be a false sentence about the list it annotates.
    # A dict, not a list, so a switch removed twice is still one thing that happened.
    return Composed(
        tuple(resolved.values()),
        tuple(switch for switch in dropped if switch not in resolved),
    )


def render(flags: tuple[Flag, ...]) -> tuple[str, ...]:
    """Flags back in the spelling a config file and a command line both use."""
    return tuple(str(flag) for flag in flags)
