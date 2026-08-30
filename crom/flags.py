"""How a list of flag texts becomes a layer, and how layers resolve into one launch list.

crom emits each Chrome switch exactly once and never lets Chrome resolve a conflict,
because Chrome's rules are per-switch and not inferable. Measured against Google Chrome
151.0.7922.175: `--disable-features` given twice silently discards the first, while
`--disable-blink-features` — one word apart in name — is additive across repetitions.
No table crom could hold would stay true for the switches nobody has tested, so crom
holds no table. It relies instead on the one fact that is universal: anything
expressible as two occurrences of a switch is expressible as one comma-joined
occurrence, so emitting once costs no expressiveness.

That is what lets `flags` obey `profile > defaults > policy` like every other config
key, and `model.Flag` — a switch and its value, rather than an opaque string — is the
type that makes two layers' answers to one question visible as such.
[LAW:types-are-the-program]
"""

from .model import CromError, Flag


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


def _remedy(first: Flag, second: Flag) -> str:
    """What to write instead, for each shape a duplicated switch can take.

    Three shapes, and the count of occurrences carrying a value tells them apart — so
    they are enumerated rather than papered over with one sentence. The single sentence
    this replaced claimed "with its values comma-joined" for all three, which is a false
    claim in two of them: `--no-pings --no-pings` joins nothing, and `--foo --foo=bar`
    disagrees about whether `--foo` takes a value at all. crom cannot know which of those
    two forms was meant — for a switch whose bare and valued spellings mean different
    things, picking one and presenting it as what the author meant is a guess wearing a
    suggestion's clothes. [LAW:no-silent-failure]

    Comma-joining is the form Chrome accepts for every list-valued switch measured, which
    is why it is the remedy whenever both occurrences carry values.
    """
    switch = first.switch
    values = [flag.value for flag in (first, second) if flag.value is not None]
    return {
        2: f"Write the switch once with its values comma-joined: {Flag(switch, ','.join(values))}",
        1: (
            f"These disagree about whether {switch} takes a value, so only you can say "
            f"which was meant — write that one, or comma-join the values if you meant both."
        ),
        0: f"Write the switch once: {switch}",
    }[len(values)]


def compose(*layers: tuple[Flag, ...]) -> tuple[Flag, ...]:
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

    [LAW:one-source-of-truth] this is the only answer to "what flags does this profile
    launch with". `resolve_spec` builds argv from it and `crom add` compares against it,
    so the two cannot come to disagree about what a declaration means.
    """
    resolved: dict[str, Flag] = {}
    for flag in (flag for one_layer in layers for flag in one_layer):
        resolved[flag.switch] = flag
    return tuple(resolved.values())


def render(flags: tuple[Flag, ...]) -> tuple[str, ...]:
    """Flags back in the spelling a config file and a command line both use."""
    return tuple(str(flag) for flag in flags)
