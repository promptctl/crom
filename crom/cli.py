"""Maps commands onto crom's core and renders the result for a human or a machine.

This is the outer boundary: below it a failure is either a `CromError` or the operating
system refusing; everything user-visible — exit codes, stderr, JSON shape — is decided
here.

[LAW:effects-at-boundaries] The core computes descriptions; this layer performs and
prints them. [CLI binding] stdout carries the answer, stderr carries diagnostics, and
exit codes are a contract a script can branch on:

    0  success            3  no such profile / namespace / config
    1  failure            4  port or declaration conflict
    2  usage error (click's own)

Four codes is as fine as a numeric contract can afford to be, so every failure also
carries a reason slug — one word naming what actually went wrong, enumerated in
`model.Reason`. `_answer` publishes it in the `--json` envelope, with one exception:
`crom down --all` fails once per profile and answers with an array, so its slugs ride the
rows that earned them and no envelope is written. `_reported` is what holds the row's
vocabulary and the envelope's to one table.
"""

import errno
import json
import shlex
from collections.abc import Callable
from datetime import timedelta
from difflib import SequenceMatcher
from itertools import dropwhile, takewhile
from pathlib import Path
from typing import NamedTuple

import click

from . import (
    chrome,
    config,
    configwrite,
    doctor,
    drift,
    mcp,
    operations,
    reclaim,
    registry,
    report,
    resolve as resolver,
    seed,
    window,
)
from .config import discover, load_user_scope, parse_layer, parse_port, parse_seed
from .model import (
    Conflict,
    CromError,
    Emitted,
    FailedProfile,
    Fields,
    Flag,
    NotFound,
    ProfileRef,
    ProfileSpec,
    Ready,
    ResolvedProfile,
    Resolution,
    Scope,
    Stopped,
    Unprobed,
    Unreachable,
    profile_stanza,
    validate_name,
)
from .session import Session

EXIT_FAILURE = 1
EXIT_NOT_FOUND = 3
EXIT_CONFLICT = 4


class _Detail(NamedTuple):
    """Everything a failure can say past its code and kind: the reason slug, and the
    values crom looked up on the way to refusing."""

    reason: str | None
    fields: Fields


def _crom_detail(error: Exception) -> _Detail:
    """crom's own refusals arrive carrying both, and for the same reason: a `CromError`
    cannot be built without a reason, and `Reason.error` fills the fields from what that
    reason declares it carries. Neither is assembled here."""
    return _Detail(error.reason.value, error.fields)


def _errno_detail(error: Exception) -> _Detail:
    """An OS refusal arrives carrying a slug already, and the OS owns how it is spelled.
    `ENOENT` is a name every language's errno table can already look up, so crom
    inventing a parallel spelling beside it would be a second source of truth for a fact
    it does not own. [LAW:one-source-of-truth]

    `None` where there is no errno — `shutil.Error` is an `OSError` carrying none — since
    at that point crom knows nothing finer than `kind` has already said. `null` is the
    one answer that cannot be mistaken for a slug, where a stand-in like `"unknown"`
    would be an answer-shaped void: a caller could branch on it as though crom had
    identified something. [LAW:parse-dont-validate]

    The path comes from the same place for the same reason. This arm's message is
    `<path>: <reason>` joined below, so without the field a caller would be splitting
    crom's sentence on a colon to learn which file the OS refused — and paths contain
    colons. `filename` is what the OS was asked about rather than what the caller typed,
    which is exactly what a field is for; `None` where it named none.

    Stringified here because `open(Path(...))` hands back the `Path` object it was given,
    and the envelope is JSON. That is this boundary's own job — rendering — and not one a
    raise site could have done, since there is no raise site: the OS built this one.
    """
    return _Detail(
        errno.errorcode.get(error.errno),
        {"path": None if error.filename is None else str(error.filename)},
    )


class _Answer(NamedTuple):
    """What crom answers for one class of error: the code a script branches on, the kind
    that says what happened when the code cannot, and where to read the detail that says
    it finer than either."""

    error: type[Exception]
    code: int
    kind: str
    detail: Callable[[Exception], _Detail]


# Every exception this CLI answers for, and the only place a code or a kind is assigned.
# [LAW:dataflow-not-control-flow] the mapping is a table consulted once, not a chain of
# except clauses repeated per command.
#
# `kind` is not `code` spelled twice. crom refusing and the operating system refusing are
# both exit 1, and `kind` is the only field that separates them — which matters, because
# one means the user's request was wrong and the other means the machine got in the way.
# Exit codes are a published four-value vocabulary that cannot grow without breaking the
# contract, and `kind` sorts the same failures four ways again. Neither can tell a port
# held by a stranger from a Chrome that will not run from a Chrome that died on the way
# up, and those are three different next moves. `reason` is where that lives — a column
# here because a class still decides which vocabulary a reason is drawn from: crom's own
# enumerated one in `model.Reason`, or the errno names the OS already publishes. That is
# what keeps `kind` from being `reason` blurred: it names which of the two you are
# holding.
#
# One column and not two, though it answers with two values. The reason and the fields are
# drawn from the same vocabulary — crom's raise site filled both, or the OS did — so a row
# choosing them separately could pair crom's slug with the OS's fields. Returned together,
# that mispairing has nowhere to live. [LAW:one-source-of-truth]
_ANSWERS = (
    _Answer(NotFound, EXIT_NOT_FOUND, "not_found", _crom_detail),
    _Answer(Conflict, EXIT_CONFLICT, "conflict", _crom_detail),
    _Answer(CromError, EXIT_FAILURE, "failure", _crom_detail),
    _Answer(OSError, EXIT_FAILURE, "os_error", _errno_detail),
)

# Where a parsed `--json` is recorded, under the key `_json_option` writes and `_answer`
# reads. `Context.meta` and not the `Session` on `ctx.obj`: meta is one dict shared by
# click's whole context tree, so it is readable from the group without depending on the
# group callback having already built a session.
_JSON_REQUESTED = "crom.json_requested"


def _json_option(command):
    """The `--json` flag: one declaration, which is also how the boundary comes to know.

    `CromGroup.invoke` sees an empty `ctx.params`, because the flag belongs to the
    subcommand and click hands a parent no handle on its child's context. So the value
    has to be left somewhere the boundary can find it, and click runs this callback while
    parsing — meaning the option and the recording are a single declaration.
    [LAW:single-enforcer] the alternative was every `--json` command assigning the flag
    onto its session on the way past: N places to enforce one rule, which is a rule the
    next command gets written without. Here there is nothing to remember, because there
    is nothing to do.

    One declaration also means one help string. Five of the six commands carrying this
    flag declared it with none, so `crom up --help` documented `--json` and
    `crom list --help` left a reader to guess. [LAW:one-source-of-truth]
    """

    def remember(ctx: click.Context, param: click.Parameter, value: bool) -> bool:
        ctx.meta[_JSON_REQUESTED] = value
        return value

    return click.option(
        "--json", "as_json", is_flag=True, callback=remember, help="Emit the result as JSON."
    )(command)


def _probe_option(command):
    """The `--no-probe` flag: one declaration, and the flag stops being a flag right here.

    The callback returns the `chrome.PortReading` the command will use rather than the
    boolean the user typed, so what travels into the commands carrying this option is the
    reading itself. Each of them then calls `chrome.health`/`health_of` the same way with a
    different value, instead of one copy per command of a rule turning a bool into a
    behaviour — which is one more place for the next command to be written without.
    [LAW:single-enforcer] the translation happens once, where click already parses.

    [LAW:dataflow-not-control-flow] and it is the same reason the reading is a value at
    all: no command has an arm asking whether to probe, so what differs between a probed
    run and a suppressed one is which answers come back, never which code ran.

    [LAW:no-mode-explosion] which is also this flag's cap. It multiplies with nothing:
    selecting a value inside one fold, it cannot interact with `--json` or `--all` or
    `--no-restart`, because none of them reaches the same decision. There is no deletion
    date because it is published CLI surface rather than an internal toggle — what a
    reviewer should hold it to is that it stays one axis, which it does for exactly as
    long as the suppression stays a reading and never becomes a branch.

    `crom down` does not carry this. It publishes `Stopped()` outright, because
    `chrome.kill` returns only once the process is gone and the port is free, so there is
    no probe there to suppress and the flag would only imply one.
    """

    def chosen(
        ctx: click.Context, param: click.Parameter, suppressed: bool
    ) -> chrome.PortReading:
        return chrome.unasked_ports if suppressed else chrome.probe_ports

    return click.option(
        "--no-probe",
        "reading",
        is_flag=True,
        callback=chosen,
        help=(
            "Report what the process table says and leave the CDP port unasked. A running "
            "profile's state comes back as 'unprobed' rather than 'ready' or 'unreachable' "
            "— crom did not check, and says so instead of guessing."
        ),
    )(command)


def _json_text(payload) -> str:
    """How crom spells JSON, for the one reader who cannot tell a result from a failure
    until it has parsed one: both are the same document format, indented the same way."""
    return json.dumps(payload, indent=2)


class _Failure(click.ClickException):
    def __init__(self, message: str, exit_code: int):
        super().__init__(message)
        self.exit_code = exit_code


def _reported(error: Exception) -> tuple[int, dict]:
    """What crom answers for one error, as values: the exit code, and the naming a script
    branches on.

    Split out of `_answer` because `crom down --all` needs the naming without the
    envelope. A sweep has N failures and one envelope could name only one of them, so the
    reason travels on the row that failed — and it is built here, from the same `_ANSWERS`
    lookup and the same `detail()` call the envelope is built from, so a row's vocabulary
    and an envelope's cannot drift into two spellings of one refusal.
    [LAW:one-source-of-truth]

    No message, because the two callers do not have one. `_answer` renders `str(error)`
    for a `CromError` and `filename: strerror` for an `OSError`, while a sweep row carries
    `str(error)` under its own `error` key either way — so a message handed back from here
    would be a third spelling, and wrong for one of them.
    """
    answer = next(a for a in _ANSWERS if isinstance(error, a.error))
    detail = answer.detail(error)
    return answer.code, {
        "kind": answer.kind,
        "reason": detail.reason,
        # Always present, `{}` where the reason declares no fields, so a caller reads one
        # shape rather than testing for the key first. [LAW:dataflow-not-control-flow]
        "fields": detail.fields,
    }


def _answer(ctx: click.Context, error: Exception, message: str) -> _Failure:
    """crom's whole answer to a failed command: the machine's copy on stdout when one was
    asked for, and the exception carrying the human's copy and the exit code.

    [LAW:one-source-of-truth] the sentence click prints to stderr and the `message` in the
    envelope are one string rendered twice, not two wordings that can drift apart. stderr
    is untouched either way — the flag adds the machine's copy, it never trades the
    human's away.

    The envelope is written here rather than from the exception's own `show` so that it
    stays under the broken-pipe rule. click calls `show` from its own `except
    ClickException` arm, whose sibling `except OSError` — the one installing
    `PacifyFlushWrapper` for `errno.EPIPE` — cannot catch what that arm raises. Measured:
    `crom up nosuchns/x --json` into a closed pipe returned exit 120 and a traceback,
    where the same command without the flag exited 3 in silence. Written here it
    propagates out of `invoke` into the region click does protect, so a reader that left
    gets the quiet ending every other broken pipe gets. [LAW:single-enforcer] the rule
    keeps one home rather than growing a second guard beside it.
    """
    code, naming = _reported(error)
    # Absent only where the parse itself failed, since nothing crom does runs ahead of
    # it: a malformed command line reaches the user as prose because the flag on it was
    # never understood either. The envelope answers for a command crom has understood.
    if ctx.meta.get(_JSON_REQUESTED, False):
        click.echo(_json_text({"error": {"code": code, **naming, "message": message}}))
    return _Failure(message, code)


# How `crom --help` groups its commands, as data rather than as prose that has to be
# re-edited alongside every new command. Alphabetical order — click's default — presented
# the commands as one flat undifferentiated list, so the help named every piece and
# nothing about how the pieces fit; a reader could learn that `mcp`, `port` and `forget`
# exist without learning that the first two are things you do *to a running profile* and
# the third is not. [FRAMING:representation] a listing is a map of the CLI, and the CLI's
# real structure is these jobs.
_COMMAND_SECTIONS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Run a browser", ("up", "down", "restart", "show", "list", "status")),
    ("Point tools at one", ("mcp", "env", "port")),
    ("Declare what exists", ("init", "add", "rm", "config", "forget")),
    ("Look after crom's own state", ("doctor", "release", "clean")),
)

# difflib's own default cutoff, so the measure and the point below which it stops calling
# two words close come from one place. [LAW:one-source-of-truth]
_NEARNESS = 0.6


def _closeness(typed: str, name: str) -> float:
    """How near a word someone typed is to a real command, as one number.

    One measure rather than a chain of matching passes, so choosing what to suggest is a
    threshold over values instead of a fallback between mechanisms.
    [LAW:dataflow-not-control-flow] `mcp-serve` reaching `mcp` and `confg` reaching
    `config` are then the same operation on two inputs, not two operations.

    A word whose first segment is exactly a command scores perfect, because `crom
    mcp-serve` is not a misspelling of `crom mcp` — it is the right command with an
    invented suffix, which difflib rates 0.5, under any cutoff loose enough to be worth
    having.

    The separator is what carries that, and nothing weaker does. A command merely
    *contained* in the word matched `rm` inside `confirm`; a command the word merely
    *starts with* matched `rm` inside `rmdir`, and `up` inside `update`, `upload` and
    `uptime` — the same false positive twice, moved from the middle of the word to its
    front, because a two-letter name is satisfied by any word that happens to open with
    those letters. Demanding an exact name up to a word boundary answers the whole class
    rather than one instance of it: `mcp` is a segment of `mcp-serve`, while `up` is only
    the opening of `update`. Everything else is difflib's to judge, and it rates `rmdir`
    against `rm` at 0.57 and `update` against `up` at 0.5, both under the cutoff.

    Case is folded because crom has no two commands that differ by it, so folding cannot
    cost a distinction that exists, and `crom UP` scores 0 against `up` without it.
    """
    typed, name = typed.casefold(), name.casefold()
    stem = "".join(takewhile(str.isalnum, typed))
    return 1.0 if stem == name else SequenceMatcher(None, typed, name).ratio()


def _nearest(typed: str, known: tuple[str, ...]) -> tuple[str, ...]:
    """The commands worth offering for a word that is not one, nearest first.

    A single character is not a word: it begins a third of crom's commands and names
    none of them, so `crom u` answering "Did you mean: up" would be a guess wearing the
    clothes of knowledge. Measured on the word here rather than inside `_closeness`,
    because it is a fact about what was typed and not about any pairing of it with a
    name — kept per-pair it floored one arm of the measure while the other let `u`
    through at 0.667 anyway. [LAW:single-enforcer]

    Carried as the set of candidates rather than as a branch around the search, so the
    same operations run on every input and a word too short to mean anything is measured
    against nothing. [LAW:dataflow-not-control-flow]
    """
    candidates = known if len(typed) > 1 else ()
    scored = sorted(
        ((_closeness(typed, name), name) for name in candidates),
        key=lambda pair: (-pair[0], pair[1]),
    )
    return tuple(name for closeness, name in scored if closeness >= _NEARNESS)


class CromCommand(click.Command):
    """Gets crom ready to run a command, once click knows which command that is.

    `Session.begin` can refuse — a legacy install whose Chrome is still up, a home crom
    cannot write — and *where* that refusal lands is this class's whole subject; what it
    is refusing over belongs to `session.py` and is not spelled a second time here.
    Readying ran from the group callback, which click calls *before* `make_context`
    parses the invoked subcommand, so `--json` had not been recorded yet and `_answer`
    had nothing to honour: `crom list --json` answered a refusal with prose and an empty
    stdout. Calling it here does not catch that failure earlier, it makes it happen later
    — past the parse, where the flag is already known and the boundary can answer for it
    like any other. [LAW:no-ambient-temporal-coupling] the ordering is stated as a place
    rather than left to which of click's phases happens to run first.

    `CromGroup.command_class` is what makes it a rule instead of a habit: every command
    built by `@main.command` is this class, so a command added tomorrow is ready without
    anyone remembering to ready it. [LAW:single-enforcer]

    What this changes for a reader: an eager option answers while the command line is
    still parsing, so it never arrives here. `crom up --help` used to fail on a machine
    whose home crom could not use — the group callback bootstrapped before click could
    descend into the subcommand — while `crom --help` worked, splitting the two things a
    confused user reaches for on exactly the machine where they are confused.
    """

    def invoke(self, ctx):
        ctx.obj = Session.begin()
        return super().invoke(ctx)


class CromGroup(click.Group):
    """Turns a failed command into the CLI's exit-code contract, in one place."""

    command_class = CromCommand

    def parse_args(self, ctx, args):
        """A bare `crom` is `crom up`, said as an argument rather than as a second way in.

        The group used to carry `invoke_without_command` and call `ctx.invoke(up_cmd,
        ref="default", as_json=False)` — a dispatch path that bypasses
        `Command.invoke`, so whatever readies a command would have had to be spelled a
        second time to cover it, and `ref="default"` was already `up`'s own default
        written out again. [LAW:one-source-of-truth] Supplying the name instead leaves one
        road into a command body, which is the whole of what this ticket is about.

        `resilient_parsing` is click's own discriminator for a parse that is not an
        invocation — shell completion working out what `crom <TAB>` could mean, where
        offering the command list is not a request to run `up`.
        [LAW:dataflow-not-control-flow] the branch is on click's own enum rather than on
        a condition crom invented to tell the two apart.
        """
        default_command = [] if ctx.resilient_parsing else ["up"]
        return super().parse_args(ctx, args or default_command)

    def invoke(self, ctx):
        """Answer for both ways a command fails: crom's own refusals, and the OS's.

        Catching `CromError` alone left the second half escaping as a raw traceback, and
        the codebase had begun closing that one call site at a time —
        `operations.delete_directory` wraps `rmtree`, `configwrite._writing` wraps the config
        save, `seed._copy` wraps the seed stat — while `crom mcp --path <a-directory>`
        still printed a stack trace out of `read_text`. [LAW:single-enforcer] a rule kept
        per command is a rule the next command is written without; kept here, a new
        command cannot reintroduce the hole, because it never had to remember.

        Those wrappers stay. They say more than this floor can — which retry is left,
        which seed could not be read — and enrichment is not enforcement.

        The line stops at `OSError` on purpose: that is the world refusing crom, and it
        names something the user can go fix. Anything else arriving here is crom being
        wrong about its own state, and a traceback is the honest report of that.
        [LAW:no-silent-failure]

        A broken pipe is neither, which is why it is handed back: `crom list | head` is
        a reader leaving on purpose, and the conventional end to that is silence.
        """
        try:
            return super().invoke(ctx)
        except CromError as error:
            raise _answer(ctx, error, str(error)) from error
        except OSError as error:
            # `errno` and not `BrokenPipeError`, because click's own handler keys on
            # `errno.EPIPE` for any `OSError` while `BrokenPipeError` also carries
            # `ESHUTDOWN`: re-raising the wider class hands click an error it declines
            # too, and the traceback comes back. [LAW:one-source-of-truth] the set click
            # owns is spelled the way click spells it.
            if error.errno == errno.EPIPE:
                raise
            # The path and the reason, in the shape crom's own filesystem errors already
            # take (`configwrite._writing`). An `OSError` carries neither reliably —
            # `os.kill` names no file, `shutil.Error` carries no `strerror` — so the
            # missing halves drop out as values rather than as branches.
            # [LAW:dataflow-not-control-flow]
            parts = (error.filename, error.strerror or error)
            raise _answer(ctx, error, ": ".join(str(part) for part in parts if part)) from error

    def resolve_command(self, ctx, args):
        """Answer an unrecognised word with a route forward rather than a dead end.

        crom converges rather than errors (`report.py`), and this was the last surface
        handing back nothing to do next: `crom mcp-serve` said only that no such command
        existed, one character away from the `mcp` the user wanted.

        Raised before delegating rather than caught after, because click's own arm is
        `ctx.fail` — by the time this frame could see it, it is a `UsageError` carrying
        nothing that separates it from click's other refusals, so enriching it would mean
        matching on its wording. The lookup is click's own `get_command`, the same call
        the overridden method makes, not a second index of the same names.
        [LAW:one-source-of-truth]

        `resilient_parsing` is the discriminator `parse_args` uses above, for the same
        reason: `crom mcp-ser <TAB>` is shell completion resolving a word it will not run,
        and click answers that with a `None` command rather than a refusal.
        [LAW:dataflow-not-control-flow]

        crom answers for words, and only for words. `crom --nope` never reaches
        resolution — the group's parser refuses it first — but `crom -- --nope` puts the
        same token where a command goes, and there click is the better answer: "No such
        option" is accurate, where a map of sixteen commands, none of them starting with
        a dash, is noise. `isalnum` is click's own test for the same thing, in
        `_split_opt`; restated rather than imported because that name is private.
        """
        typed = args[0]
        word = typed[:1].isalnum() and self.get_command(ctx, typed) is None
        if word and not ctx.resilient_parsing:
            raise self._unrecognised(ctx, typed)
        return super().resolve_command(ctx, args)

    def _unrecognised(self, ctx, typed: str) -> click.UsageError:
        """What crom offers instead of the dead end: the commands nearest what was typed,
        or — when nothing is near — the whole curated map.

        Both arms are one value, `((heading, names), ...)`, written by the same hand that
        writes `--help`, so a suggestion and the full listing cannot drift into two
        pictures of one CLI. [LAW:one-type-per-behavior] a suggestion is that listing
        filtered, not a second kind of thing.

        No near match renders the map rather than an empty "Did you mean" — "did you mean
        nothing" is an answer-shaped void where "here is everything crom does" is the
        answer crom actually has. [LAW:parse-dont-validate]

        Still a `UsageError`, so exit 2 is unchanged: a suggestion is more text, never a
        different outcome. [CLI binding] exit codes are the contract a script branches
        on, and this changes what a human reads.
        """
        near = _nearest(typed, tuple(self.list_commands(ctx)))
        route = (("Did you mean", near),) if near else self._sections(ctx)
        formatter = ctx.make_formatter()
        self._write_sections(ctx, formatter, route)
        return click.UsageError(f"No such command {typed!r}.\n{formatter.getvalue().rstrip()}", ctx)

    def _sections(self, ctx) -> tuple[tuple[str, tuple[str, ...]], ...]:
        """The curated map, plus a heading for whatever the curation missed.

        The leftover section is what makes the grouping safe to curate: a command added
        to the group but not to `_COMMAND_SECTIONS` still appears, under a heading whose
        blankness is the bug report. [LAW:no-silent-failure] the alternative — iterating
        the curated names alone — deletes a real command from the only place users look
        for it, and does so silently, on the machine of someone who does not know the
        command exists. `test_help_sections_cover_every_command` keeps that heading
        empty; this keeps it honest when the test has not run.
        """
        listed = {name for _, names in _COMMAND_SECTIONS for name in names}
        leftover = tuple(n for n in self.list_commands(ctx) if n not in listed)
        return (*_COMMAND_SECTIONS, ("Other", leftover))

    def _write_sections(self, ctx, formatter, sections) -> None:
        """Write titled groups of commands, each row a name beside its short help."""
        for title, names in sections:
            rows = [
                (name, self.get_command(ctx, name).get_short_help_str(limit=68))
                for name in names
                if self.get_command(ctx, name) is not None
            ]
            if rows:
                with formatter.section(title):
                    formatter.write_dl(rows)

    def format_commands(self, ctx, formatter) -> None:
        """Render the command list in sections, and never omit a command."""
        self._write_sections(ctx, formatter, self._sections(ctx))


def _emit(as_json: bool, payload, lines: list[str]) -> None:
    """Render one successful result. The last inch of UI, and the only place a *result*
    chooses its format — a failure has two readers at once, so it renders in
    `_answer`."""
    click.echo(_json_text(payload) if as_json else "\n".join(lines))


def _pid_list(pids: tuple[int, ...]) -> str:
    """How a profile's PIDs read in a message — one spelling, for the three that print them."""
    return ", ".join(map(str, pids))


def _layer_notes(profile: ResolvedProfile) -> dict[str, str]:
    """Where each switch this profile resolves to comes from, keyed by the text it emits.

    The sole builder of that mapping, for the two commands that annotate a flag with its
    provenance: `crom config` beside every line of the resolved command, and `crom up`
    beside a switch a relaunch moved. [LAW:single-enforcer] spelled twice, the collision
    rule `_supplied` documents would have to be changed in both places to stay true, with
    nothing making them agree.

    Keyed by the whole flag text rather than the switch name because that is the spelling
    both readers already hold: `crom config` looks up lines of `profile.argv`, and a
    `drift.Change.resolves` is one of those same strings. Keying by switch name would also
    reintroduce the collision `drift` keys its own entries by kind to rule out — a config
    may legally name a flag `env TZ`.
    """
    return {str(item.flag): _note(item) for item in profile.provenance.emitted}


def _supplied(change: drift.Change, notes: dict[str, str]) -> str:
    """Which layers decided the current value of a changed switch, as a clause beside it.

    `drift` carries no provenance and could not: it compares a record written at launch
    against what the config resolves to now, and only the current side still has layers to
    name — the recorded side was decided by a config file this crom no longer has. So the
    clause is attached here, from the same `_note` `crom config` prints, and only to the
    side that resolves. [LAW:one-source-of-truth] one rendering of where a flag came from.

    Joined on the whole flag text and confirmed against the subject, because `notes` holds
    only what a launch *emits*: a change about `chrome binary` or an `env` variable has no
    layer clause to find, and its value could spell a flag text that does. `drift` keys its
    own entries by kind to make that collision unrepresentable and deliberately does not
    publish the kind, so re-deriving it from the switch is what keeps a variable set to
    `--window-size=800,600` from borrowing `--window-size`'s provenance.
    """
    # `or ""` only to keep the parse total. Both mean "no flag text to look up" here; the
    # `None`-is-not-`""` distinction `Change` protects is about what a config said, not
    # about whether a layer clause exists to find.
    resolves = change.resolves or ""
    supplies = notes.get(resolves, "") * (Flag.parse(resolves).switch == change.subject)
    return f" ({supplies})" * bool(supplies)


def _moved(changes: tuple[drift.Change, ...], notes: dict[str, str]) -> list[str]:
    """The switches a drift names, one indented line each, beside the layer supplying the
    new value.

    One spelling for the two answers `crom up` gives a drifted browser — the relaunch that
    applied the drift and the `--no-restart` report that declined to. Spelled twice, a
    change to how a moved switch reads lands in one and not the other, and the two outputs
    a user compares to decide whether to relaunch disagree about what moved.
    [LAW:single-enforcer]

    The changes rather than the verdict holding them, because the caller now selects its
    arm on an `operations.Outcome` and so holds a `drift.Verdict` no pattern has narrowed
    to `Drifted`. `changes` is on all four verdicts and empty on three of them by `drift`'s
    own design — the same reason `crom list` renders a verdict without asking which one it
    holds — so taking the tuple is the honest parameter and needs no narrowing to be total.

    A comprehension over a tuple that is empty exactly when the flags moved only in order —
    a real drifted state `Drifted.finding` already spells out on the line above — so the
    section appears when it has something to say and no branch decides whether it exists.
    [LAW:dataflow-not-control-flow]
    """
    return [f"  {change}{_supplied(change, notes)}" for change in changes]


@click.group(cls=CromGroup)
# The version is read from the installed distribution's metadata, which the build copies
# out of pyproject.toml, so crom holds no second spelling of it to fall behind a release.
# [LAW:one-source-of-truth]
#
# `package_name` is named rather than left to click, which otherwise infers it by
# inspecting the caller's frame globals — making the distribution crom asks about a fact
# about which module this decorator happens to sit in.
#
# The version alone, as `crom port` prints a port alone: stdout carries the answer, so a
# script reads it without splitting a sentence for it. [CLI binding] click's default,
# `<prog>, version <v>`, would also spell that answer differently from one invocation to
# the next, since `prog` is argv[0].
#
# Eager, as click makes every `--version`: it answers and exits while the command line is
# still being parsed, so it never reaches the readying `CromCommand.invoke` does. That is
# the difference between a version crom can always state and one it can state only on a
# machine whose home crom can already use. [LAW:no-ambient-temporal-coupling]
@click.version_option(package_name="crom", message="%(version)s")
def main():
    """crom — a real Chrome per project, each on a port that never moves.

    \b
    The three words
      profile    one Chrome user-data-dir, plus the CDP port crom assigns it.
      namespace  the profiles belonging to one project, so two projects never
                 collide on a port or a directory.
      ref        how you name a profile. `dev` means dev in the namespace you
                 are standing in; `myapp/dev` names it from anywhere.

    Which namespace you are standing in is decided by the directory you run from:
    it is `user` — your personal profiles — unless a `.crom.toml` sits here or
    above, in which case it is the one that file declares. `crom config` always
    says which, and `crom list` shows both.

    \b
    Start here
      crom up            bring up `default` and print its CDP URL
      crom mcp           point chrome-devtools-mcp at it, here
      crom init          give this project its own namespace and profiles
      crom config        what is in effect here, and what `crom up` will do

    A new profile starts as a copy of your real Chrome profile, so it has your
    logins and extensions. `--seed fresh` on `init` or `add` gets an empty one.

    A `.crom.toml` written by `crom init` sets the namespace, then `[defaults]`
    and a `[profiles.<name>]` for each profile. Where both answer, the profile
    wins, and for flags it wins one Chrome switch at a time, so each switch
    reaches Chrome exactly once. `crom config <profile>` shows the resolved
    command with the layer behind each flag; `crom config --help` is the
    reference for every key a config may set.

    crom does the setup step for you rather than naming it: a profile you refer
    to but never declared is declared, and a config file crom cannot read is
    reset to the default with your original kept beside it as `<name>.broken`.
    Both are reported on stderr as they happen.

    Every command asks for a state, not a change, so asking twice is not an
    error: `crom init` in a project that has a .crom.toml, `crom add` of a
    profile already declared, and `crom up` of a browser already running what
    this config resolves to all report what is there and exit 0. A browser
    running something else is not that state, so `crom up` stops it and
    starts it again on the current config: edit a flag, run `crom up`, and
    the edit is live. Only a request for something *different* from what
    exists is refused — `crom add dev --port 9500` when `dev` is declared on
    another port names the difference and changes nothing.
    """


@main.command("up")
@click.argument("ref", required=False, default="default")
@click.option(
    "--no-restart",
    is_flag=True,
    help="Name a drifted browser's changes instead of replacing it, keeping its session.",
)
@_probe_option
@_json_option
@click.pass_obj
def up_cmd(
    session: Session, ref: str, no_restart: bool, reading: chrome.PortReading, as_json: bool
):
    """Bring a profile up on its current config, whatever is running.

    Idempotent: a browser already running what this config resolves to is reported, not
    restarted. One running something else is stopped and started again on the current
    config, with what moved named before anything stops. A browser crom holds no launch
    record for is left running and reported as unmeasured — crom cannot tell what it was
    started with, which is no grounds to kill a browser that may already match.

    --no-restart takes only the stop off the table: a drifted browser is named and left
    running, tabs and logins intact, and a profile with nothing running still launches. It
    is how a script keeps a profile up without ever costing its user a session, and the
    one way `crom up` exits 0 on a browser it did not bring onto the current config.
    """
    profile = session.working(ref)
    where = f"{profile.ref} on {profile.cdp_url}"
    # Built out here because it is a fact about the resolution, and nothing the operation
    # does can change it.
    notes = _layer_notes(profile)
    # A boolean is click's spelling of this flag and not the domain's, so the crossing
    # happens once, here, and nothing past this line holds a `no_restart` whose negation a
    # reader has to run backwards. [LAW:types-are-the-program]
    on_drift = operations.OnDrift.REPORT if no_restart else operations.OnDrift.REPLACE
    ran = operations.up(profile, on_drift)
    # One arm per ending, wording the ending the operation already decided. The pair
    # `(verdict, policy)` is matched once, inside `operations.up`, and deliberately not
    # again here: matched twice this would be a second copy of the arm map, in a second
    # file, and the day they disagree a run that replaced a browser reports that it left
    # one alone. [LAW:one-source-of-truth]
    #
    # `MATCHED` is the one ending whose `finding` goes unsaid — "running with what this
    # configuration resolves to" is the headline above it in other words. The two endings
    # that do print one are saying something the headline does not already claim.
    match ran.outcome:
        case operations.Outcome.STARTED:
            lines = [f"Started {where}"]
        case operations.Outcome.MATCHED:
            lines = [f"Already running {where}"]
        case operations.Outcome.UNMEASURED:
            # Said rather than swallowed: a bare "Already running" here would be crom
            # reporting an agreement it never established, and is how a browser goes on
            # running flags its config stopped asking for with crom appearing to have
            # checked. [LAW:no-silent-failure]
            lines = [f"Already running {where}", f"  {ran.found.finding}"]
        case operations.Outcome.REPORTED:
            # The one ending where `crom up` exits 0 without having converged, so it reports
            # in the shape `UNMEASURED` reports in — "running, and here is what crom will
            # not act on" — and adds the switches, which is the whole of what `--no-restart`
            # has over the `drift` `crom list` already publishes. Claiming "Already running"
            # alone would be the stale-browser silence this epic exists to end, from the
            # command that ended it. [LAW:no-silent-failure]
            lines = [
                f"Already running {where}",
                f"  {ran.found.finding}",
                *_moved(ran.found.changes, notes),
            ]
        case operations.Outcome.RELAUNCHED:
            # No finding line here: `operations.up` said what moved on stderr before it
            # stopped anything, so printing it again on stdout would give a user watching
            # one terminal the reason twice.
            lines = [
                f"Relaunched {where} (was pid {_pid_list(ran.stopped)}, "
                f"now pid {_pid_list(ran.pids)})",
                *_moved(ran.found.changes, notes),
            ]

    _emit(
        as_json,
        {
            **profile.describe(chrome.health_of(profile, ran.pids, reading)),
            # The verdict crom *found*, under a key that says so. `crom list` and `crom
            # config` publish `drift`, which is how a profile stands right now; this
            # command's answer is how the profile stood when this command reached it, and
            # reusing the name would have one key meaning two things across the JSON
            # surface. Deliberately not "what crom acted on" either — under `--no-restart`
            # crom acts on none of it, and a key that narrowed to the acted-on cases would
            # go silent on the drift in exactly the run that was asked to report it.
            # [FRAMING:representation]
            "found": drift.describe(ran.found),
            # What the convergence replaced, the way `restart` carries it: empty unless this
            # run relaunched, and what separates a browser this command left alone from one
            # it swapped out under the same "running" record. Nearly the whole of it, not
            # quite: a replacement whose browser exited before `chrome.kill` reached it
            # publishes an empty `stopped` too. `operations.Outcome` is where that
            # difference is exact, and this pair of keys is the JSON's long-standing
            # approximation of it — carried across this move unchanged rather than widened
            # under cover of a refactor. [LAW:one-source-of-truth]
            "stopped": list(ran.stopped),
        },
        lines,
    )


def _stop_line(ref: ProfileRef, pids: tuple[int, ...]) -> str:
    """What one stop reads like, in the one spelling `down` and its sweep both use.

    Empty pids are not the sweep's dead case: it acts on the profiles it just found
    running, and a browser that exits in the gap between that reading and the signal
    lands here with nothing stopped. Spelled once, a race in the fleet reads exactly like
    `crom down` on a profile that was already stopped, because it is the same fact.
    [LAW:one-source-of-truth]
    """
    return f"Stopped {ref} (pid {_pid_list(pids)})" if pids else f"{ref} was not running"


def _sweep(session: Session, as_json: bool) -> None:
    """Stop every profile a browser is up for, reporting each outcome, failures included.

    THE SET IS `crom list --running`'S SET, BY CONSTRUCTION. `hidden` below is the same
    expression that command uses, read through the same `state.running` — the attribute
    `describe()` publishes under the key `running` — over a listing built from the same
    `_scopes_to_list`. The listing is therefore a preview of this sweep rather than a
    second opinion about the word "running", which is the whole reason the filter was
    built first. [LAW:one-source-of-truth] a rule spelled twice is two rules with a
    schedule for disagreeing; matching on `state.slug` here would have been that second
    copy, and would have skipped exactly the browser a user most wants swept — an
    `unreachable` one, holding a port it will not give back.

    THE PORT IS NOT ASKED, AND THE SET IS UNCHANGED BY THAT. `running` is true on
    `ready`, `unreachable` and `unprobed` alike, because liveness comes from the process
    table: `_reachability` answers `Stopped` on an empty pid reading before it looks at
    the port at all. So `unasked_ports` selects the same profiles `probe_ports` would, and
    selects them without spending `PORT_REPLY_SECONDS` on each wedged browser in the
    fleet — the cost that made batching matter for `list`. It also keeps `down` a command
    that takes no reading: what it publishes is what `chrome.kill` established.
    [LAW:effects-at-boundaries] the one reading this does take, `chrome.scan`, is taken
    once for the whole fleet.

    WHAT CROM READ NO STATE FOR IS REPORTED, NEVER ACTED ON. `hidden` can only hold refs
    `chrome.health` answered for, so an unresolvable declaration survives the narrowing
    the same way it survives `crom list --running` — and then falls to the `FailedProfile`
    arm, which renders it and stops nothing. [LAW:parse-dont-validate] the absence of an
    answer is not the answer "no", and a declaration crom cannot resolve is exactly where
    a browser it cannot see would be hiding.

    A FAILED STOP DOES NOT ABORT THE SWEEP, AND DOES NOT PASS FOR SUCCESS EITHER. Each
    stop is caught where it happens, becomes a row, and the sweep goes on to the next
    profile; the exit code then answers for all of them at once. [LAW:no-silent-failure]
    exit 0 from a sweep that left a browser running would be crom claiming work it did
    not do — the failure is loud in stdout, in stderr and in `$?`.

    THE REASON RIDES THE ROW, BECAUSE A SWEEP HAS ONE PER FAILURE. Every other command
    answers with `_answer`'s envelope, which names a single refusal. This one can fail on
    `myproj/a` with `chrome_stop_failed` and on `user/b` with `EPERM` in the same run, so
    an envelope would name one of those and drop the other — and would land after the
    records array, where a reader that parses one document never reaches it. Each row
    carries `failure` instead: the `kind`, `reason` and `fields` `_reported` draws from
    `_ANSWERS`, `null` where nothing failed there. [LAW:one-source-of-truth] One key
    rather than three beside `error`, because a row holding a reason and no kind is a
    state with no meaning — nested, a failure is present or absent and never half of one.
    [LAW:types-are-the-program]

    An unresolved declaration does not fail the sweep, though it is reported. "A browser
    is still up" and "a declaration is malformed" are different next moves, and a sweep
    that answered 1 for both would leave a script unable to separate them — the same test
    `Reason` applies to its slugs. The row is the signal there; the exit code is reserved
    for stops crom attempted and could not establish.
    """
    scopes, unavailable = _scopes_to_list(session, everything=False)
    listing = [entry for scope in scopes for entry in resolver.resolve_all(scope)]
    standing = chrome.health(
        (entry for entry in listing if isinstance(entry, ResolvedProfile)),
        chrome.scan(),
        chrome.unasked_ports,
    )
    hidden = {ref for ref, state in standing.items() if not state.running}
    listing = [entry for entry in listing if entry.ref not in hidden]

    records, lines, failed = [], [], []
    for entry in listing:
        match entry:
            case ResolvedProfile():
                try:
                    pids = operations.down(entry)
                except (CromError, OSError) as error:
                    # The state crom read before the attempt, because the attempt is
                    # exactly what did not happen — publishing `Stopped()` here would
                    # report a browser that is still up as down. The `error` beside it is
                    # what `chrome.kill` observed, which names the half that failed.
                    failed.append(entry.ref)
                    # The code is dropped on purpose: it belongs to the command, and a
                    # per-row copy would be a number that means nothing on its own.
                    _, failure = _reported(error)
                    record = {
                        **entry.describe(standing[entry.ref]),
                        "stopped": [],
                        "error": str(error),
                        "failure": failure,
                    }
                    line = f"{entry.ref} — {error}"
                else:
                    record = {
                        **entry.describe(Stopped()),
                        "stopped": list(pids),
                        "error": None,
                        "failure": None,
                    }
                    line = _stop_line(entry.ref, pids)
            case FailedProfile():
                record = {**entry.describe(), "failure": None}
                line = f"{entry.ref} — unresolved — {entry.error}"
        records.append(record)
        lines.append(line)

    # Empty while the sweep asks only the scopes `crom list` shows by default, since that
    # is the arm of `_scopes_to_list` that loads no remembered namespace and so cannot
    # fail to. Rendered rather than dropped because the pair is what that function
    # answers with: taking half of it would leave a namespace crom could not load to
    # vanish from the sweep on the day it widens, which is the one row that says crom may
    # not have seen the whole fleet. [LAW:no-silent-failure]
    for namespace, error in unavailable:
        records.append({"namespace": namespace, "error": error})
        lines.append(f"{namespace}/ — unavailable — {error}")

    # One document on stdout, and the summary on stderr — so a `--json` reader parses the
    # rows it would have parsed on a clean sweep rather than an error envelope appended
    # after them. Every failure is already in those rows; what `_answer` would add is a
    # second telling. [LAW:one-source-of-truth]
    _emit(as_json, records, lines or ["Nothing running to stop."])
    if failed:
        raise _Failure(f"could not stop: {', '.join(str(ref) for ref in failed)}", EXIT_FAILURE)


@main.command("down")
@click.argument("ref", required=False)
@click.option(
    "--all",
    "everything",
    is_flag=True,
    help="Stop every profile `crom list --running` shows, instead of one named profile.",
)
@_json_option
@click.pass_obj
def down_cmd(session: Session, ref: str | None, everything: bool, as_json: bool):
    """Stop a running profile, or the whole fleet with `--all`.

    `crom down --all` stops exactly the profiles `crom list --running` shows, so run that
    listing first to see what the sweep will take down. It keeps going when one profile
    fails and reports every outcome, and it leaves alone what it could not resolve —
    crom read no state for those, so it has nothing there it could claim to stop.
    """
    # Refused here, and `_sweep` takes no ref at all, so the illegal pairing cannot be
    # expressed past this line rather than being defended against below it.
    # [LAW:parse-dont-validate] There is no reading of `crom down ci --all` that is not a
    # mistake: one arm names a profile and the other names every profile, and honouring
    # either would be crom picking which half of the command line to believe.
    if everything and ref is not None:
        raise click.UsageError("--all stops every running profile; it takes no REF.")
    if everything:
        return _sweep(session, as_json)

    # `if ref is None` and not `ref or`, because the two facts an optional argument can
    # carry — "no REF was given" and "the REF given was empty" — are different, and `or`
    # collapses them. A script interpolating an unset `$PROFILE` writes `crom down ""`,
    # which used to be refused by `validate_name` with `invalid_name` and would otherwise
    # stop `default` instead: a wrong profile stopped quietly, in place of a loud refusal.
    # [LAW:no-silent-failure] the discriminator is whether click was given the argument,
    # which it already answers with `None`. [LAW:parse-dont-validate]
    profile = session.profile("default" if ref is None else ref)
    pids = operations.down(profile)
    message = _stop_line(profile.ref, pids)
    # `Stopped()` rather than a reading taken here: `chrome.kill` returns only once the
    # process is gone *and* the port is free, or raises — so this command's own
    # postcondition is what the state says, and probing to be told what crom has just
    # established would be a second answer to a settled question. [LAW:one-source-of-truth]
    #
    # The pids ride alongside as `stopped`, the key `up` and `restart` already publish for
    # what a run took down. They used to sit in the record's own `pids`, which every other
    # command fills with processes a caller can attach to — one key, two meanings, and the
    # meaning flipped on which command wrote it. [FRAMING:representation]
    _emit(
        as_json,
        {**profile.describe(chrome.Stopped()), "stopped": list(pids)},
        [message],
    )


@main.command("restart")
@click.argument("ref", required=False, default="default")
@_probe_option
@_json_option
@click.pass_obj
def restart_cmd(session: Session, ref: str, reading: chrome.PortReading, as_json: bool):
    """Stop a profile and start it again on its current config."""
    profile = session.working(ref)
    # Both halves under one hold of the lock, which is the whole of what this command adds
    # over typing `crom down && crom up`. Released in between, another crom process is free
    # to land in the gap: a concurrent `up` sees nothing running and starts the browser, so
    # this command's own start then finds a live Chrome and reports a restart it did not
    # perform — on the old configuration, which is the one thing a restart exists to
    # replace. `rm` in the gap is worse, and deletes the directory this is about to launch
    # against. [LAW:no-ambient-temporal-coupling] the indivisible span is stated here, by
    # the only participant that knows how wide it is.
    #
    # `chrome.kill` is what makes the start safe to follow it directly: it returns only
    # once the profile holds neither a process nor its CDP port, so this cannot race its
    # own socket teardown and lose the port to the corpse of the browser it just stopped.
    with seed.profile_lock(profile):
        stopped = chrome.kill(profile)
        if stopped:
            # Said before the start rather than assembled with the result afterwards, so
            # the fact survives a start that fails. A restart whose launch half fails
            # leaves the user with no browser at all, and an error naming only the start
            # would hide that crom stopped the working one they had. It doubles as the
            # progress line for the pause while Chrome comes up. [CLI binding] stderr.
            click.echo(
                f"Stopped {profile.ref} (pid {_pid_list(stopped)}); starting it again …",
                err=True,
            )
        # The verdict is discarded rather than reported: `kill` has just guaranteed nothing
        # is running, so it can only be `Stopped` and a start here is always a start. The
        # interesting fact is what was stopped, and that is what `stopped` carries.
        _, pids = operations.start_under_lock(profile)

    was, now = _pid_list(stopped), _pid_list(pids)
    message = (
        f"Restarted {profile.ref} on {profile.cdp_url} (was pid {was}, now pid {now})"
        if stopped
        else f"{profile.ref} was not running; started it on {profile.cdp_url}"
    )
    # `stopped` rides alongside the record rather than inside it: what a restart replaced is
    # a fact about this command, not about the profile, and `describe()` is the shape every
    # command's JSON shares. Without it a `--json` caller cannot tell a browser that was
    # replaced from one that was merely started, which is the single distinction this
    # command exists to report. [LAW:one-source-of-truth] `describe()` stays canonical.
    _emit(
        as_json,
        {**profile.describe(chrome.health_of(profile, pids, reading)), "stopped": list(stopped)},
        [message],
    )


@main.command("show")
@click.argument("ref", required=False, default="default")
@_probe_option
@_json_option
@click.pass_obj
def show_cmd(session: Session, ref: str, reading: chrome.PortReading, as_json: bool):
    """Bring a profile's window to the front, launching it if it is not running."""
    profile = session.working(ref)
    # Starting and raising under one hold, so the PIDs raised are the PIDs observed. A
    # `down` landing between the two would leave `window.raise_profile` asking macOS for a
    # process that no longer exists, and the -1719 it answers with reads as "the browser
    # exited" — true, but it would be describing a race this command could have prevented.
    #
    # The raise is inside the lock rather than after it for that reason alone; it costs one
    # osascript round trip, which is the same order as the `ps` call `operations.start_under_lock`
    # already makes while holding it.
    with seed.profile_lock(profile):
        verdict, pids = operations.start_under_lock(profile)
        # `Stopped` is the verdict for a profile with nothing running, which is exactly the
        # case `operations.start_under_lock` launches in — so this reads the launch off the same
        # observation that caused it rather than asking after the fact. `show` raises a
        # window and does not converge: a browser whose config has moved on is the user's
        # to replace with `crom up`, and killing it here would cost them the very window
        # they asked to be shown. [LAW:decomposition] one purpose, said in one sentence.
        started = isinstance(verdict, drift.Stopped)
        if started:
            # Said before the raise rather than assembled with the result afterwards, so
            # the fact survives a raise that fails. Withheld Automation access is likeliest
            # on a first run — the same run likeliest to have started the browser — and a
            # user shown only the raise error would go hunting for a launch failure that
            # never happened. On stderr because for `show` the answer is the raise itself;
            # a launch on the way to it is progress, like `operations.start_under_lock`'s own
            # "Creating … from seed" line. [CLI binding]
            click.echo(f"Started {profile.ref} on {profile.cdp_url}", err=True)
        windows = window.raise_profile(profile, pids)

    raised = (
        f"Raised {profile.ref}"
        if windows
        else f"Raised {profile.ref}, but it has no open windows to show — it is running "
        f"headless, or its last window was closed."
    )
    # The window count rides alongside the record for the same reason `restart` carries
    # what it stopped: the human line already distinguishes a raise that found a window
    # from one that did not, and a `--json` caller confirming the window actually came
    # forward — the whole point of the command — could otherwise only get there by parsing
    # the prose. [FRAMING:representation] one result, and both maps of it say the same.
    _emit(
        as_json,
        {
            **profile.describe(chrome.health_of(profile, pids, reading)),
            "started": started,
            "windows": windows,
        },
        [raised],
    )


def _since(elapsed: timedelta) -> str:
    """A duration as a person reads one: the two largest units that carry it.

    Contiguous units rather than the non-zero ones, so a browser up five days and
    twenty-three minutes reads `5d 0h` and never `5d 23m` — which is the same string a
    browser up five days and twenty-three *hours* would produce, off by a day.
    [FRAMING:representation] the map drops precision, and it may not lie while doing it.
    """
    days, rest = divmod(int(elapsed.total_seconds()), 86400)
    hours, rest = divmod(rest, 3600)
    minutes, seconds = divmod(rest, 60)
    carried = dropwhile(
        lambda part: part[0] == 0, ((days, "d"), (hours, "h"), (minutes, "m"), (seconds, "s"))
    )
    return " ".join(f"{value}{unit}" for value, unit in list(carried)[:2]) or "0s"


def _process_line(pid: int, elapsed: timedelta | None) -> str:
    """One of a profile's processes, and how long it has been there.

    A pid the uptime reading does not know is said so outright rather than left off the
    listing or timed at zero. The pids come from one reading of the process table and the
    durations from the next, so a browser that exits between the two is a real outcome —
    and a caller who sees a pid vanish silently learns nothing, where `no longer in the
    process table` names exactly what happened. [LAW:no-silent-failure]
    """
    if elapsed is None:
        return f"pid {pid}, no longer in the process table"
    return f"pid {pid}, up {_since(elapsed)}"


def _tab_lines(tabs: tuple[chrome.Tab, ...] | None) -> list[str]:
    """A browser's open pages, headed by how many there are.

    Three outcomes and three sentences, because `None` and `()` are different facts:
    `chrome.tabs_on` returns nothing when the browser would not list its targets, and an
    empty tuple when it listed none. Rendering both as "no tabs open" would report an
    empty desktop for a browser crom could not read. [LAW:parse-dont-validate]
    """
    if tabs is None:
        return ["crom could not read its tab list — the browser answered, then did not"]
    headline = {0: "no tabs open", 1: "1 tab open"}.get(len(tabs), f"{len(tabs)} tabs open")
    return [headline, *(f"  {tab.title} — {tab.url}" for tab in tabs)]


def _nothing_heard(heard: str) -> tuple[dict, list[str]]:
    """What `crom status` publishes for the three states no browser answered in.

    One shape for all three, so the keys a script reads are the same document whatever the
    state is: a consumer checks `state` and finds `browser`, `websocket` and `tabs`
    present and null, rather than absent on some runs and present on others.
    [LAW:types-are-the-program] a key that comes and goes is a shape every caller has to
    guard; a key that is null is one it can read.
    """
    return {"heard": heard, "browser": None, "websocket": None, "tabs": None}, [heard]


def _browser_facts(profile: ResolvedProfile, state) -> tuple[dict, list[str]]:
    """What crom can say about the browser behind one state — the keys `crom status --json`
    publishes and the lines a person reads, decided together.

    One value behind both renderings, for the reason `crom list` renders `state.slug`
    rather than a ternary of its own: a sentence and a key derived separately are two maps
    of one fact, and they drift. [LAW:one-source-of-truth]

    [LAW:dataflow-not-control-flow] the one branch here is `Health`'s own discriminator,
    and it is the entire subject of the command — the states differ in what the browser was
    able to say about itself. Everything around this folds values.

    The tab listing is asked inside the `Ready` arm and nowhere else, which is what makes
    `--no-probe` provable rather than promised: suppressing the probe yields `Unprobed`,
    that arm is then unreachable, and no socket is opened anywhere in this command. The
    flag's guarantee holds by the shape of the code rather than by a check that could be
    forgotten. [LAW:parse-dont-validate] `Ready` is the stamp saying a browser answered,
    and only a browser that answered can be asked what it has open.
    """
    match state:
        case Ready(browser=browser, websocket=websocket):
            tabs = chrome.tabs_on(profile.port)
            return (
                {
                    "heard": f"{browser} answered on {profile.cdp_url}",
                    "browser": browser,
                    "websocket": websocket,
                    "tabs": tabs if tabs is None else [
                        {"title": tab.title, "url": tab.url} for tab in tabs
                    ],
                },
                [f"{browser}, answering on {profile.cdp_url}", f"connect at {websocket}",
                 *_tab_lines(tabs)],
            )
        case Unreachable(heard=heard):
            return _nothing_heard(heard)
        case Unprobed():
            return _nothing_heard("its CDP port was not asked")
        case Stopped():
            return _nothing_heard("no process holds its profile directory")


@main.command("status")
@click.argument("ref", required=False, default="default")
@_probe_option
@_json_option
@click.pass_obj
def status_cmd(session: Session, ref: str, reading: chrome.PortReading, as_json: bool):
    """Report what the browser on this profile's port actually is, right now.

    `crom list` says whether a browser answers; this says what answered. Every fact
    below is read live at the moment you ask — crom keeps no record of a running
    browser, because a record of a browser is a second answer to a question the
    machine can already be asked.

    \b
    What it reports
      the state      stopped, ready, unreachable, or unprobed — the same word
                     `crom list` prints and `--json` publishes everywhere
      the processes  each pid holding the profile directory, and how long it has
                     been up, read from `ps`
      the browser    what the CDP endpoint calls itself, and the browser
                     websocket a client connects by. Both come from the version
                     document the reachability probe already fetches
      the tabs       the pages the browser has open, by title and URL

    A browser that does not answer costs you the last two and none of the first
    two: the pids and their uptimes come from the process table, which has an
    answer whatever CDP is doing. That is what "degrades honestly" means here —
    fewer facts, never a guessed one.

    The websocket URL changes every time the browser restarts, so read it at the
    moment you connect rather than storing it. The port does not change, which is
    the whole point of crom, and `crom port` is the stable handle.
    """
    profile = session.working(ref)
    # The two process-table questions back to back, so the pids and their uptimes are as
    # near one moment as two readings get; the probe, which owns a socket and a deadline,
    # comes after both. A pid that dies in the gap is reported as gone rather than timed at
    # zero — `_process_line`. [LAW:no-ambient-temporal-coupling]
    pids = chrome.find_pids(profile)
    timing = chrome.uptimes_on(profile.profile_dir)
    state = chrome.health_of(profile, pids, reading)
    published, said = _browser_facts(profile, state)
    _emit(
        as_json,
        {
            **profile.describe(state),
            **published,
            # Derived from `state.pids` rather than from the uptime reading, so this list
            # and the `pids` `describe` publishes cannot come to disagree about which
            # processes crom found. [LAW:one-source-of-truth]
            "processes": [
                {
                    "pid": pid,
                    "uptime_seconds": None if pid not in timing else int(
                        timing[pid].total_seconds()
                    ),
                }
                for pid in state.pids
            ],
        },
        [
            f"{profile.ref}  {state.slug}  :{profile.port}",
            *(f"  {_process_line(pid, timing.get(pid))}" for pid in state.pids),
            *(f"  {line}" for line in said),
        ],
    )


@main.command("list")
@click.option("--all", "everything", is_flag=True, help="Include every namespace crom knows.")
@click.option(
    "--running",
    "only_running",
    is_flag=True,
    help="Only the profiles a browser is up for, ready to act on.",
)
@_probe_option
@_json_option
@click.pass_obj
def list_cmd(
    session: Session,
    everything: bool,
    only_running: bool,
    reading: chrome.PortReading,
    as_json: bool,
):
    """List the profiles addressable from here.

    The two narrowings are independent and compose: `--all` widens which namespaces are
    asked, `--running` drops the profiles nothing is up for. Together they are the fleet
    you can act on right now, across every project on the machine.
    """
    scopes, unavailable = _scopes_to_list(session, everything)

    # Every row is resolved before any row is judged, because the probe behind
    # `chrome.health` is only one round trip if it is handed the whole listing at once —
    # asked a profile at a time it becomes twenty, and a listing of wedged browsers costs
    # `PORT_REPLY_SECONDS` apiece. The rendering loop below then reads a decided value
    # rather than reaching for the world mid-row.
    listing = [(scope, list(resolver.resolve_all(scope))) for scope in scopes]
    standing = chrome.health(
        (entry for _, entries in listing for entry in entries if isinstance(entry, ResolvedProfile)),
        chrome.scan(),
        reading,
    )

    # What `--running` becomes: the refs the listing leaves out, empty where the flag was
    # not typed. The narrowing below then runs once either way over a value, rather than
    # the rendering growing an arm that asks which flag it is under.
    # [LAW:dataflow-not-control-flow]
    #
    # `state.running` and not the slug, because `running` is the verdict `describe`
    # publishes under that very name — so the filter and the JSON key are one fact read
    # twice, and `crom list --running --json` cannot drop a row that would have carried
    # `"running": true`. Matching slugs here would be a second copy of crom's
    # state-to-liveness rule, free to disagree with the first the day a fifth state lands.
    # [LAW:one-source-of-truth]
    #
    # Only a ref crom probed can reach this set, which is what keeps an unresolvable
    # declaration and an unavailable namespace in the listing under `--running`: crom read
    # no state for either, and reporting them as not-running would answer a question
    # nobody got to ask. [LAW:parse-dont-validate] the absence of an answer is not the
    # answer "no" — and a namespace crom could not load is exactly where a browser it
    # cannot see would be hiding, in the command a user runs *because* something is
    # broken. [LAW:no-silent-failure]
    hidden = {ref for ref, state in standing.items() if only_running and not state.running}
    listing = [
        (scope, [entry for entry in entries if entry.ref not in hidden])
        for scope, entries in listing
    ]

    records, lines = [], []
    for scope, entries in listing:
        for entry in entries:
            match entry:
                case ResolvedProfile():
                    state = standing[entry.ref]
                    # Every row carries a verdict, including the rows whose verdict is
                    # that there is nothing to compare — so the listing has no per-row
                    # branch asking whether this line has one. [LAW:dataflow-not-control-flow]
                    verdict = drift.of(entry, state.pids)
                    records.append({**entry.describe(state), "drift": drift.describe(verdict)})
                    # The state's own word, so the line a human reads and the `state` a
                    # script parses are one value rendered twice rather than two
                    # descriptions someone has to keep in step. [LAW:one-source-of-truth]
                    # The ternary this replaces could name only two of the three, and had
                    # no third arm to add: `running` was the branch, and "running" was the
                    # word that made a wedged browser read like a healthy one.
                    shown = f"{state.slug} :{entry.port}"
                    # Nineteen is the longest of these plus the two-space gutter every
                    # other column here keeps: `unreachable :9240`.
                    lines.append(f"  {str(entry.ref):28s}  {shown:19s}  {verdict.finding}")
                case FailedProfile():
                    records.append(entry.describe())
                    lines.append(f"  {str(entry.ref):28s}  unresolved — {entry.error}")
        if not scope.profiles:
            lines.append(f"  {scope.namespace}/ — no profiles declared in {scope.source or 'user config'}")

    for namespace, error in unavailable:
        records.append({"namespace": namespace, "error": error})
        lines.append(f"  {namespace + '/':28s}  unavailable — {error}")

    _emit(as_json, records, lines)


def _scopes_to_list(session: Session, everything: bool) -> tuple[list[Scope], list[tuple[str, str]]]:
    """The scopes `crom list` should report, plus the namespaces it could not load.

    A remembered namespace whose config file has been deleted or moved raises `NotFound`
    from `scope_for`, which drops crom's record of where it lives on the way past. One
    stale entry used to abort the entire listing, so the command that would have shown
    the user which namespace was broken was the one command that could not run. Each
    namespace is isolated and reported by name instead. [LAW:no-silent-failure] nothing
    is skipped quietly: the failure is a row in the output, human and JSON alike, and
    `scope_for` narrates the drop on stderr with the file it could no longer find.
    """
    scopes = [session.scope]
    if not session.scope.is_user:
        scopes.append(load_user_scope())

    unavailable: list[tuple[str, str]] = []
    if everything:
        for namespace in sorted(registry.namespaces()):
            if namespace == session.scope.namespace:
                continue
            try:
                scopes.append(resolver.scope_for(namespace, session.scope))
            except CromError as error:
                unavailable.append((namespace, str(error)))
    return scopes, unavailable


# How wide the flag column grows before the notes beside it stop lining up. A cap rather
# than the true widest flag: one long `--host-resolver-rules=...` would otherwise push
# every note on the listing off the right of an ordinary terminal, to align with a line
# nobody was reading the note for.
_NOTE_COLUMN = 46


def _resolution(answered: Resolution, *, named: bool) -> str:
    """One question's history, in a clause that can sit beside the flag it decided.

    The one place a resolution becomes prose, so the three shapes the report has — an
    ordinary switch, a feature name inside a switch that carries several, and a switch a
    drop removed — read the same way rather than each inventing a phrasing.
    [LAW:single-enforcer]

    `named` because the question is worth printing only where the line does not already
    carry it: an ordinary flag's question *is* the switch printed to its left.

    That comparison spans an expansion — the question is the switch as the file spells it,
    the flag has been through `resolve._expand` — and it is sound because `flags.layer`
    refuses a `${` in a switch name, so the two spellings cannot differ. If that border
    rule is ever relaxed, this reduces an ordinary flag to the feature shape and prints the
    pre-expansion switch beside its expanded self. [LAW:parse-dont-validate] the border is
    what makes this safe to read, not care taken here.
    """
    # The value first and the layer last, so one phrasing carries both vocabularies: a
    # replaced flag reads "over --window-size=800,600 from [defaults]" and a replaced
    # feature reads "over false from [defaults]". Layer-first put a bare `false` against a
    # layer name and left the reader to guess which word was the value.
    over = "".join(f", over {answer.said} from {answer.layer}" for answer in answered.replaced)
    return f"{answered.question + ' ' if named else ''}from {answered.stands.layer}{over}"


def _note(item: Emitted) -> str:
    """Where one emitted switch came from — one clause, or one per feature it carries."""
    return " · ".join(
        _resolution(answered, named=answered.question != item.flag.switch)
        for answered in item.why
    )


@main.command("add")
@click.argument("name")
@click.option(
    "--seed",
    "seed_text",
    default=None,
    help=(
        "default | chrome:<Profile> | fresh | ./path — where this profile's data comes "
        "from. Omit to inherit [defaults].seed from the config."
    ),
)
@click.option("--flag", "flag_texts", multiple=True, help="Chrome flag; repeatable.")
@click.option("--port", type=int, default=None, help="Pin the CDP port instead of letting crom assign one.")
@click.pass_obj
def add_cmd(session: Session, name: str, seed_text: str | None, flag_texts: tuple[str, ...], port: int | None):
    """Declare a profile in the config governing this directory. Idempotent."""
    validate_name("profile name", name)
    scope = session.scope
    target = config.write_target(scope)
    where = profile_stanza(name)
    spec = ProfileSpec(
        name=name,
        # No drops: `crom add` has no `--drop-flag`, so the request it builds cannot state
        # one. The empty list is the request, not a placeholder — a stanza that drops
        # nothing is what `--flag` alone asks for.
        flags=parse_layer(list(flag_texts), [], where, target),
        # None when `--seed` was not given, which `configwrite` writes as no `seed` key
        # and `resolve_spec` reads as `scope.default_seed`. The old `default="fresh"`
        # meant every added profile carried an explicit `seed = "fresh"` nobody had asked
        # for, so a project that set `[defaults].seed` found it applied to the profile
        # `crom init` wrote and to no profile added afterwards.
        seed=None if seed_text is None else parse_seed(seed_text, where, target, scope.config_dir),
        # Through `parse_port`, the same validator a port from the file goes through.
        # click only proves this is an int, so `--port 0` or `--port 99999` used to be
        # written to disk and then rejected by the parser on the next load — bricking
        # every command in the project, which is the failure `operations.add` goes to
        # lengths to avoid where it refuses a duplicate pin before the write rather than
        # after. [LAW:single-enforcer] the range rule has one home; this path was
        # bypassing it rather than needing a copy.
        port=parse_port(port, where, target),
    )
    declaration = operations.add(scope, spec)
    profile = declaration.profile
    # The arm `operations.add` reached, rendered rather than re-derived. "Declared" over a
    # call that wrote nothing is the one sentence this seam exists to keep unsayable, and
    # the only other way to reach it from here is to ask `scope.profiles` whether the name
    # was already there — a picture taken before the write, and wrong for exactly the
    # caller that lost the race for the name. [LAW:one-source-of-truth]
    match declaration.outcome:
        case operations.Declaration.CREATED:
            verb = "Declared"
        case operations.Declaration.ALREADY_PRESENT:
            verb = "Already declared"
    click.echo(f"{verb} {profile.ref} in {declaration.target}")
    # The seed is reported even when it came from `[defaults]` rather than from `--seed`:
    # it decides whether the browser opens with the user's logins or empty, which is the
    # one thing about a new profile that surprises people, and inheriting it silently is
    # how it stays a surprise until launch.
    click.echo(
        f"  seed {configwrite.render_seed(profile.seed, profile.config_dir)}"
        f" · port {profile.port} · {profile.profile_dir}"
    )
    click.echo(f"Run: crom up {profile.ref}")


@main.command("rm")
@click.argument("ref")
@click.option("--yes", is_flag=True, help="Skip the confirmation prompt.")
@click.option("--keep-data", is_flag=True, help="Undeclare the profile but leave its directory.")
@click.pass_obj
def rm_cmd(session: Session, ref: str, yes: bool, keep_data: bool):
    """Stop a profile if it is running, undeclare it, release its port, delete its data."""
    profile = session.profile(ref)

    # Read only to compose the prompt, which is why these two reads stay in the command
    # rather than moving with the removal: they are the wording of a question crom asks a
    # human, and a caller that is not a terminal has nobody to ask. Exported as a record
    # for `operations.rm` to hand back, they would be a shape whose only consumer is
    # `click.confirm`. [LAW:composability] The authoritative act is `chrome.kill` under
    # `operations.rm`'s lock, which converges a profile to stopped whether or not this saw
    # it run — so this read is stale by construction and says so.
    running = chrome.is_running(profile)
    deletes_data = not keep_data and profile.profile_dir.exists()

    # Assembled rather than templated because `doctor.measure` walks the whole profile
    # directory: folding it into a comprehension over both consequences would measure a
    # gigabyte of Chrome data on the `--keep-data` path that is not going to delete it.
    consequences = []
    if running:
        consequences.append(f"stop the browser running on port {profile.port}")
    if deletes_data:
        size = _human_size(doctor.measure(profile.profile_dir))
        consequences.append(
            f"delete {profile.profile_dir} ({size}) — its logins, cookies, and history"
        )

    if consequences and not yes:
        click.confirm(
            f"Removing {profile.ref} will:\n"
            + "\n".join(f"  · {line}" for line in consequences)
            + "\nContinue?",
            abort=True,
        )

    # The prompt above is deliberately the last thing before the call: `operations.rm`
    # takes the profile lock for the whole removal, and holding it across an interactive
    # question would block every other crom process for as long as the human takes to
    # answer.
    stopped = operations.rm(profile, session.scope, keep_data=keep_data)
    # The stop is reported rather than performed quietly: killing someone's browser is
    # the most surprising thing this command does, and `--yes` skips the prompt that
    # would otherwise have been its only mention. [LAW:no-silent-failure]
    stopped_note = f" (stopped pid {', '.join(map(str, stopped))})" if stopped else ""
    click.echo(f"Removed {profile.ref}{stopped_note}")


@main.command("init")
@click.argument("namespace", required=False)
@click.option(
    "--seed",
    "seed_text",
    default=None,
    help=(
        "default | chrome:<Profile> | fresh | ./path — what this project's profiles start "
        "from. Written into [defaults].seed. Default: default, a copy of your default profile."
    ),
)
def init_cmd(namespace: str | None, seed_text: str | None):
    """Give this project its own namespace by writing a .crom.toml here. Idempotent."""
    project = operations.init(Path.cwd(), namespace, seed_text)

    # The arm `operations.init` reached, worded rather than re-derived. The other way to
    # reach it from here is to ask whether the file existed before the call — a question
    # only answerable by a second stat, taken after the write, which cannot tell this
    # process's creation from a concurrent one's. [LAW:one-source-of-truth]
    match project.outcome:
        case operations.Declaration.CREATED:
            headline = f"Wrote {project.target} (namespace '{project.namespace}')"
        case operations.Declaration.ALREADY_PRESENT:
            headline = (
                f"{project.target} already configures this project "
                f"(namespace '{project.namespace}')"
            )
    click.echo(headline)
    click.echo(f"  profiles here start from seed '{project.seed}' — change it in [defaults]")
    click.echo(f"Run: crom up  # brings up {project.namespace}/default")


@main.command("config")
@click.argument("ref", required=False)
@_probe_option
@_json_option
@click.pass_obj
def config_cmd(session: Session, ref: str | None, reading: chrome.PortReading, as_json: bool):
    """Show the config in effect, and how a profile resolves flag by flag.

    With a REF, every flag of the launch command is printed with the layer that
    supplied it and whatever it outranked — the layering rule below, on your own
    config. This help is the reference for writing that config.

    \b
    Where a key may appear
      top level          namespace (required), chrome_binary, state_dir
      [defaults]         flags, drop_flags, features, env, seed
      [profiles.<name>]  flags, drop_flags, features, env, seed, port

    Where two layers answer the same question, the profile's answer wins — per
    switch for `flags`, per feature name for `features`, per variable for `env`,
    and outright for `seed`. `flags` and `features` have a third layer beneath
    both, crom's own launch policy, which they beat in turn. `drop_flags` is the
    one key that never conflicts: every layer's drops apply.

    Flags resolve by switch name rather than by concatenation, so each Chrome
    switch is emitted exactly once — crom composes the command instead of
    handing Chrome two answers to the same question.

    The top-level keys do not layer. They are set once for the whole file, and
    `[defaults]` has no counterpart for them — a `port` under `[defaults]` is an
    unknown key, not an inherited default.

    \b
    What each key accepts
      namespace      this project's name: lowercase letters, digits, and . _ -
                     starting with a letter or digit, at most 64 characters.
                     Required in a project config, and never `user`. Your own
                     config in ~/.config/crom is the `user` namespace and must
                     not set the key at all.
      chrome_binary  path to the Chrome to launch. Default: the one crom finds.
      state_dir      where this namespace's profile directories live.
                     Default: crom's own state directory.
      flags          Chrome switches as you would type them on a command line:
                     ["--window-size=1280,800", "--no-pings"]. A later layer's
                     entry replaces an earlier layer's for the same switch.
      drop_flags     switch names alone, never their values: to drop an
                     inherited --window-size=1280,800, write ["--window-size"].
                     Removes a switch a layer below supplied, crom's launch
                     policy included — ["--disable-sync"] launches Chrome with
                     sync left on. This is the only way to say *less* than a
                     lower layer did; a `flags` entry can only replace it.
      features       Chrome feature name -> true/false. The layers union rather
                     than replace, later layers winning per name, and the whole
                     table is emitted as one --enable-features and one
                     --disable-features. There is deliberately no
                     `drop_features`: a table is already per name, so a layer
                     can say the opposite without erasing anything.
      env            string values put into Chrome's environment. Merged one
                     variable at a time, so a profile adding a variable keeps
                     the rest of `[defaults]` rather than replacing the table.
      seed           where a profile's data comes from the first time crom
                     creates it:
    \b
                       default                your default Chrome profile
                       chrome:<Profile Name>  another profile inside your Chrome
                       fresh                  an empty profile
                       ./dir  /dir  ~/dir     a directory you keep yourself
    \b
      port           pin this profile's CDP port, 1..65535. Left out, crom
                     assigns one and remembers it.

    Paths in `chrome_binary`, `state_dir` and a `seed` resolve against the
    directory the config file is in, so a committed config means the same thing
    on every machine.

    \b
    Switches crom owns, and what to write instead
      --user-data-dir, --remote-debugging-port, --remote-debugging-pipe
        The profile's identity and its CDP contract, which crom sets. Naming one
        in `flags` or `drop_flags` is refused.
      --enable-features, --disable-features
        Write `features` entries instead. crom folds every layer's table into
        these two switches, so naming either in `flags` or `drop_flags` is
        refused.

    Inside `flags` values and `env` values, ${CROM_NAMESPACE}, ${CROM_PROFILE},
    ${CROM_PORT}, ${CROM_PROFILE_DIR} and ${CROM_CONFIG_DIR} expand. A switch
    *name* may not interpolate — crom resolves switches by the spelling your
    file uses, and expands afterwards — and feature names are literal.

    \b
    A config using all of it
      namespace = "myapp"
    \b
      [defaults]
      seed = "default"
      flags = ["--window-size=1280,800", "--disable-blink-features=PIP"]
      features = { SharedStorageAPI = false }
      env = { TZ = "UTC" }
    \b
      [profiles.dev]                            # inherits every default above
    \b
      [profiles.ci]
      seed = "fresh"
      port = 9401
      flags = ["--window-size=800,600"]         # replaces the [defaults] size
      drop_flags = ["--disable-blink-features"] # launches without it at all
      features = { SharedStorageAPI = true }    # flips the default back on
      env = { TZ = "America/Denver" }
    """
    scope = session.scope
    default_seed = configwrite.render_seed(scope.default_seed, scope.config_dir)
    payload = {
        "namespace": scope.namespace,
        "source": str(scope.source) if scope.source else None,
        "discovered_from": str(discover() or ""),
        "profiles_root": str(scope.profiles_root),
        "chrome_binary": str(scope.chrome_binary),
        "profiles": sorted(scope.profiles),
        "default_seed": default_seed,
        "bare_up_ref": f"{scope.namespace}/default",
    }
    # This command is what someone runs when they cannot tell what crom is doing, so it
    # leads with the two facts that decide that — which namespace this directory puts
    # them in, and what a bare `crom up` therefore means — before the paths. The previous
    # ordering opened with `profiles_root` and `chrome_binary`, which are the two facts a
    # confused reader needs last.
    lines = [
        f"Here, crom is in the '{scope.namespace}' namespace.",
        f"  declared by   {scope.source or '(no config file — your implicit user scope)'}",
        f"  profiles      {', '.join(sorted(scope.profiles)) or '(none declared)'}",
        f"  new ones use  seed '{default_seed}'",
        f"  data in       {scope.profiles_root / scope.namespace}",
        f"  chrome        {scope.chrome_binary}",
        "",
        f"`crom up` with no argument here means `crom up {scope.namespace}/default`.",
        (
            "Profiles in other namespaces stay reachable as `<namespace>/<name>`; "
            "`crom list --all` shows them."
        ),
    ]

    if ref:
        profile = session.working(ref)
        state = chrome.health_of(profile, chrome.find_pids(profile), reading)
        verdict = drift.of(profile, state.pids)
        notes = _layer_notes(profile)
        # Measured over the annotated lines alone: the bare ones are the binary path and
        # the profile directory, which are the longest things here and have nothing to line
        # anything up with.
        width = min(max((len(arg) for arg in notes), default=0), _NOTE_COLUMN)
        payload["resolved"] = {
            **profile.describe(state),
            "argv": list(profile.argv),
            # Beside `argv` rather than inside `describe()`, for the reason the seed is:
            # this is how the profile came to be what it is, which is `crom config`'s
            # subject, while `up` and `list` report what it is now.
            #
            # Rendered by the report's own types, so the JSON a consumer parses and the
            # lines a human reads are two views of one value rather than two hand-kept
            # descriptions of it. [LAW:one-source-of-truth]
            "flags": [item.describe() for item in profile.provenance.emitted],
            "dropped": [removal.describe() for removal in profile.provenance.dropped],
            # The seed lives here rather than in `describe()` because it is a create-time
            # input, not a property of the profile: once the directory exists it records
            # where the data came from, and every other `describe()` consumer — `up`,
            # `list` — is reporting what the profile *is* right now.
            "seed": configwrite.render_seed(profile.seed, profile.config_dir),
            # Last, because it is the only fact here about the *browser* rather than
            # about the resolution: everything above says what `crom up` would launch,
            # and this says how what is already running stands against it.
            "drift": drift.describe(verdict),
        }
        lines += [
            "",
            f"{profile.ref} resolves to:",
            (
                f"  seed {configwrite.render_seed(profile.seed, profile.config_dir)}"
                f" · port {profile.port} · {profile.profile_dir}"
            ),
            # Every line of the command, each annotated with where it came from. A flag a
            # user wrote can legitimately not be here — a later layer replaced it, or a
            # layer dropped it — and this listing is the only place that difference is
            # visible, so it says which layer supplied each switch and which layers it
            # outranked to get there. [LAW:no-silent-failure]
            #
            # `notes` is keyed by the flag text because the report holds the same expanded
            # strings `argv` was built from; a line crom frames rather than composes — the
            # binary, `--user-data-dir`, `--remote-debugging-port` — is simply not in it and
            # prints bare, with no branch asking which kind of line this is.
            # [LAW:dataflow-not-control-flow]
            *(f"  {arg.ljust(width)}  {notes.get(arg, '')}".rstrip() for arg in profile.argv),
            # A dropped switch is absent from argv and indistinguishable there from one
            # nobody ever set, so the only reader who could tell them apart is the one who
            # wrote `drop_flags` — and they are the reader least in need of being told.
            # Named here with the layer it would have come from, the removal is something
            # the listing shows rather than something the reader has to already know.
            #
            # A generator over a tuple that is usually empty, so the line appears when
            # there is one to print without a branch deciding whether this section exists.
            *(
                # The whole flag as its subject, not the bare switch — the same rule the
                # emitted lines above follow, so the two shapes are parallel rather than
                # this one being a reduced version of them. A switch set once and then
                # dropped has no other channel carrying the value that was lost: the flag
                # is absent from argv, which is the whole reason this line exists.
                f"  (dropped {removal.what.stands.said}, "
                f"{_resolution(removal.what, named=False)} — removed by {removal.by})"
                for removal in profile.provenance.dropped
            ),
            # The verdict, then a line per entry that moved. Every line above is the
            # *current* resolution, so a flag the user has since edited appears there as
            # its new value with nothing saying the running browser never got it — and
            # the old value has no other channel. [LAW:no-silent-failure]
            "",
            f"  {verdict.finding}",
            *(f"    {change}" for change in verdict.changes),
        ]

    _emit(as_json, payload, lines)


@main.command("port")
@click.argument("ref", required=False, default="default")
@click.pass_obj
def port_cmd(session: Session, ref: str):
    """Print a profile's CDP port and nothing else."""
    click.echo(session.working(ref).port)


@main.command("env")
@click.argument("ref", required=False, default="default")
@click.pass_obj
def env_cmd(session: Session, ref: str):
    """Print shell exports for a profile: eval "$(crom env dev)"."""
    profile = session.working(ref)
    # `CROM_PROFILE` is the profile *name*, matching what the same spelling means inside
    # a config's `${CROM_PROFILE}` interpolation. It used to be the full "namespace/name"
    # here and the bare name there, so one identifier named two different things
    # depending on where it was read — and the README presents both as one vocabulary,
    # which is what made the collision misleading rather than merely inconsistent.
    # [LAW:one-source-of-truth] The interpolation vocabulary already decomposes a ref
    # into namespace and name, so that is the meaning that composes; `CROM_REF` carries
    # the joined form under a name that means only that.
    for key, value in {
        "CROM_NAMESPACE": profile.ref.namespace,
        "CROM_PROFILE": profile.ref.name,
        "CROM_REF": str(profile.ref),
        "CROM_PORT": str(profile.port),
        "CROM_CDP_URL": profile.cdp_url,
        "CROM_PROFILE_DIR": str(profile.profile_dir),
    }.items():
        # This output is meant to be `eval`ed, so it is shell source, not text: a profile
        # directory under a path like `~/My Projects` would otherwise end the assignment
        # at the space and the rest of the path would be read as a command. `shlex.quote`
        # leaves ordinary values exactly as they were.
        click.echo(f"export {key}={shlex.quote(value)}")


def _legacy_notes(legacy: mcp.Legacy, ref: ProfileRef, key: str, path: str) -> tuple[str, ...]:
    """What to say about an entry the file already held under crom's old constant key.

    A table over the three outcomes rather than a chain of `if`s, and total rather than
    a `.get(..., ())`, so an outcome added to `mcp.Legacy` later fails here loudly
    instead of quietly printing nothing about itself. [LAW:dataflow-not-control-flow]
    [LAW:no-silent-failure]

    The kept clause claims only that the entry is not what crom writes for this profile,
    which is the whole of what crom knows about it. Both shapes that reach KEPT are in
    it — an entry naming another browser and one whose body a human edited — and telling
    those apart, or naming the port in either, would need a parser for an entry crom may
    not have written, which is exactly the guess `mcp.write` refuses to make. It says
    nothing about which, because what the user can act on is the same either way: this
    file now declares two chrome-devtools servers, and crom will not merge them.
    """
    return {
        mcp.Legacy.ABSENT: (),
        mcp.Legacy.REPLACED: (
            f"Renamed {path}'s '{mcp.LEGACY_KEY}' entry to '{key}' — "
            f"same wiring, now named for {ref}.",
        ),
        mcp.Legacy.KEPT: (
            f"Left {path}'s existing '{mcp.LEGACY_KEY}' entry alone — it is not the entry "
            f"crom writes for {ref}, so the file now declares two chrome-devtools servers.",
        ),
    }[legacy]


@main.command("mcp")
@click.argument("ref", required=False, default="default")
@click.option("--path", "path", default=".mcp.json", help="File to write.")
@click.pass_obj
def mcp_cmd(session: Session, ref: str, path: str):
    """Wire chrome-devtools-mcp at a profile by writing .mcp.json here."""
    profile = session.working(ref)
    legacy = mcp.write(profile, Path(path))
    # Recomputed from the ref rather than carried back from `write`, which is a
    # derivation and not a copy: `entry_key` is pure, so the two callers cannot disagree
    # about the key for one ref the way two stored spellings of it could.
    # [LAW:one-source-of-truth]
    key = mcp.entry_key(profile.ref)
    # Before the answer, and on the other stream: renaming an entry is convergence — work
    # crom did on the user's behalf that they did not ask for — and `report` is where that
    # goes, while the line below is the answer a script parses. [CLI binding]
    for note in _legacy_notes(legacy, profile.ref, key, path):
        report.to_stderr(note)
    click.echo(f"Wrote {path}: '{key}' wired to {profile.ref} ({profile.cdp_url})")


@main.command("forget")
@click.argument("namespace")
def forget_cmd(namespace: str):
    """Drop a namespace from the registry, releasing its reserved ports."""
    released = registry.forget_namespace(validate_name("namespace", namespace))
    click.echo(f"Forgot namespace '{namespace}' ({released} port reservation(s) released)")


@main.command("doctor")
@_json_option
def doctor_cmd(as_json: bool):
    """Show the state crom owns on this machine, and where it has leaked.

    `crom list` reads the config files and reports what they declare; this reports what
    crom is actually holding. The two answers differ exactly where something has leaked,
    which is the only reason to run this.

    Every reservation in the port ledger comes first, each with where it stands against
    the config the ledger names as its source: `declared`, `orphaned` — nothing declares
    it any more — or `unchecked`, which is crom refusing to guess about a config it could
    not read.

    Each also carries who holds its port right now, which is a separate question with a
    separate answer: `idle` if nothing does, `own` if this profile's own browser does,
    `foreign` if something else does, or `unprobed` if crom could not tell. `foreign` is
    the one to act on — only `crom up` checks that a port is still crom's, so `crom port`,
    `crom env` and `crom mcp` will hand out a number a stranger is already answering on.

    Then every staging directory under a profile root. Seeding builds a profile beside its
    final path and moves it in only once it is whole, so a `crom up` killed mid-copy
    leaves the half-built copy behind — dot-prefixed, so `ls` hides it, and the retry
    succeeds, so nothing looks wrong. A seed running right now leaves the same evidence
    and is listed the same way, so a directory here may still be filling rather than
    abandoned; crom does not tell the two apart. Each is reported with its size, and so
    is every namespace crom could not look under.

    Nothing here writes. `crom release <key>` hands back the port under one reservation
    this reports, and `crom clean <path>` deletes one staging directory it found — both
    act only on what this command already named, and both refuse a verdict crom could not
    establish. Releasing a port and deleting a profile copy cannot be undone, which is why
    they are things you ask for rather than things a doctor does on its way past.
    """
    found = doctor.survey()
    _emit(
        as_json,
        found.describe(),
        [
            # All three counts are unconditional, so a clean machine reads as an answer
            # rather than as output that got cut off — and the last of them is what keeps
            # "nothing is leaking" apart from "I could not check".
            # [LAW:dataflow-not-control-flow] `reservation(s)` is how `crom forget`
            # already counts the same noun.
            f"{len(found.rows)} reservation(s) in {found.registry}",
            *(
                f"  {row.held.port:<6}{row.ref:30s}"
                f"{'pinned' if row.held.pinned else '':8s}{row.standing.slug:11s}"
                f"{row.liveness.slug:10s}"
                # Both findings, joined, on every row. The standing's names the config
                # crom consulted or says the ledger records none; the liveness's is the
                # only place a reader learns *why* crom could not tell who holds a port,
                # which no slug can carry. Joining them keeps the line the same shape for
                # every row rather than growing a second line for the rows that have
                # something to add. [LAW:dataflow-not-control-flow]
                f"{row.standing.finding} — {row.liveness.finding}"
                for row in found.rows
            ),
            f"{len(found.staged)} staging directory(s) from a seed interrupted or still running",
            # The size leads: it is what decides whether this is worth acting on, and the
            # paths are long enough to push it off the end of a line if it followed.
            *(f"  {_human_size(item.bytes):>8}  {item.path}" for item in found.staged),
            f"{len({item.namespace for item in found.unscanned})} namespace(s) crom "
            f"could not check for them",
            *(f"  {item.namespace + '/':30s}{item.error}" for item in found.unscanned),
        ],
    )


@main.command("release")
@click.argument("key")
def release_cmd(key: str):
    """Hand one reservation's port back, leaving the rest of its namespace alone.

    The other way to release a port is `crom forget <namespace>`, which releases every
    port under that namespace — the live profiles' included — so it cannot reach one
    orphan without taking its neighbours with it. This reaches exactly one.

    Give it the key `crom doctor` prints, spelled exactly as it prints it. The key is
    taken as written and never taken apart: a hand repair can strand a reservation under
    something that is not a legal `namespace/name` at all, and those are the very ones
    nothing else can reach.

    Crom releases a reservation `crom doctor` calls `orphaned`. It refuses one a config
    still declares — `crom rm <ref>` undeclares and releases together — and one whose
    own browser is still running, which you close first. It also refuses a reservation
    it could not check or could not probe: a released port goes to the next profile that
    asks for one and never comes back, so crom will not release on evidence it never
    got. `crom doctor` says which of those any reservation is.
    """
    match reclaim.releasable(key, doctor.survey()):
        case reclaim.Refused(reason=reason, why=why):
            raise reason.error(why)
        case reclaim.Releasable(row=row):
            # The survey read the ledger and `forget` writes it under its own lock, so a
            # concurrent release can empty the key in between. Which of the two happened
            # is what `forget` reports, and the line says it rather than crediting crom
            # with freeing a number it found already free. The state the caller asked for
            # holds either way, which is why this is an answer and not a failure.
            # [LAW:no-silent-failure]
            act = "Released" if registry.forget(row.ref) else "Already released"
            click.echo(
                f"{act} {row.ref} — port {row.held.port} is free for the next profile "
                f"that asks for one"
            )


@main.command("clean")
@click.argument("path")
@click.option("--yes", is_flag=True, help="Skip the confirmation prompt.")
def clean_cmd(path: str, yes: bool):
    """Delete one staging directory a seed abandoned, by the path `crom doctor` prints.

    Seeding builds a profile beside its final path and moves it in only once it is whole,
    so a `crom up` killed mid-copy leaves a whole seed's worth of bytes behind under a
    dot-prefixed name `ls` hides. The retry succeeds, so nothing ever looks wrong.

    Only a directory `crom doctor` reported can be deleted here, and that is the whole of
    the safety: a migration stages a legacy profile under the same dot-prefixed shape,
    and for part of its run that copy is the only one there is. The path may be spelled
    however your shell produced it — pasted, relative, through a symlink — because what
    gets deleted is the directory `crom doctor` walked to, not the name you typed.

    A seed running right now leaves identical evidence, and crom cannot tell the two
    apart. The prompt names the size so you can; `--yes` skips it.
    """
    match reclaim.deletable(path, doctor.survey()):
        case reclaim.Refused(reason=reason, why=why):
            raise reason.error(why)
        case reclaim.Deletable(staged=staged):
            size = _human_size(staged.bytes)
            if not yes:
                click.confirm(
                    f"Deleting {staged.path} ({size}) cannot be undone.\nContinue?", abort=True
                )
            operations.delete_directory(
                staged.path, f"Run `crom clean {staged.path}` again once that is fixed."
            )
            click.echo(f"Deleted {staged.path} ({size} reclaimed)")


def _human_size(total: int) -> str:
    """A byte count as a person reads it.

    A number in, a string out: the walk that produces the number belongs to
    `doctor.measure`, which every command that quotes a size now shares, and what is left
    here is the rendering — which is the presenter's whole job. [LAW:effects-at-boundaries]
    """
    size = float(total)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f}{unit}"
        size /= 1024
