"""Gets crom ready to run, and hands back the state a command then works from.

Readying is two writes and a read, and the read has to come last. Migration folds a
pre-namespace installation into `~/.config/crom/config.toml`; the user-config bootstrap
declares in that same file the `default` profile a bare `crom up` expects; and
`Session.scope` is what finally reads it. A scope loaded ahead of either writer is a
scope missing what that writer was about to put there.

`Session.begin` is the only assembly of that order. It lived as three statements in
`CromCommand.invoke` under a comment explaining why they went in that sequence — an
ordering held by one caller getting it right, which holds exactly as long as there is
one caller. Nothing outside re-derives it now: a caller gets a session or an error, the
same two endings the CLI gets. [LAW:no-ambient-temporal-coupling] the ordering is a
place, not a convention every future entry point has to be told about.

What makes "the read comes last" hold is that the read is lazy: a `Session` resolves its
scope on first ask rather than at construction, so a caller that came through `begin`
cannot be holding a scope read before the writes, whatever it does with the object
afterwards. [LAW:types-are-the-program] the sequence is a named constructor on the type
it returns, which is the strongest form of "there is one way in" that Python spells.

Not the strongest imaginable: `Session()` stays callable, and a private token demanded by
`__init__` would turn that into a `TypeError`. It was weighed and left out. What it
defends against is a caller who has already gone out of their way, and the assembly worth
copying — three statements and a comment — is what this module deleted.
[LAW:polishing-by-subtraction]

No click below this line, so the sequence is callable by anything.
"""

from . import config, configwrite, migrate, registry, report
from . import resolve as resolver
from .config import load_ambient
from .model import DEFAULT_SEED, USER_NAMESPACE, ProfileSpec, ResolvedProfile, Scope, parse_ref
from .paths import user_config_file


class Session:
    """Lazily-loaded ambient state, so `crom init` need not find a config or a Chrome."""

    def __init__(self):
        self._scope: Scope | None = None

    @classmethod
    def begin(cls, log=report.to_stderr) -> "Session":
        """Ready crom and return the session a command works from — the one way in.

        `log=` rather than a print, exactly as `resolve.py` and `config.repair_unreadable`
        already take one: both steps here converge a prerequisite instead of reporting it
        unmet, and [LAW:no-silent-failure] means none of that may be silent — while a
        caller that is not the CLI, and a test, both need somewhere other than the
        process's stderr to put it. Passing it on also settles which function does the
        printing: `migrate.run_if_needed` defaults to a lambda spelling `report.to_stderr`
        a second time, and a threaded `log` leaves one of them reachable.
        [LAW:one-source-of-truth]
        """
        migrate.run_if_needed(log)
        _bootstrap_user_config(log=log)
        return cls()

    @property
    def scope(self) -> Scope:
        if self._scope is None:
            self._scope = load_ambient()
            if self._scope.source and not self._scope.is_user:
                # Remembering the namespace here — the moment crom reads a project
                # config — is what lets `crom up thatproject/dev` work from anywhere.
                registry.remember_namespace(self._scope.namespace, self._scope.source)
        return self._scope

    def profile(self, ref_text: str) -> ResolvedProfile:
        """A profile that must already be declared — for `down` and `rm`."""
        return resolver.resolve(parse_ref(ref_text, self.scope.namespace), self.scope)

    def working(self, ref_text: str) -> ResolvedProfile:
        """A profile to work with, declared on the spot if nothing declares it yet.

        The split is the whole of crom's stance on prerequisites, stated as two calls
        rather than as a flag: a command asking *where profile X is* gets it created, a
        command asking crom to *take X away* does not. [LAW:types-are-the-program] a
        `declare=True` parameter would have made "create the profile I am about to
        delete" expressible at every call site.
        """
        return resolver.resolve_or_declare(parse_ref(ref_text, self.scope.namespace), self.scope)


def _bootstrap_user_config(log=report.to_stderr) -> None:
    """On a machine with no user config, declare the profile a bare `crom up` expects.

    Written explicitly into the file rather than defaulted in code, so `user/default`
    cloning your real Chrome profile is a visible, editable decision and not folklore.
    """
    # Repairing first is what makes the write below safe on a user config crom cannot
    # read. `configwrite._load` raises on such a file, and this function runs before every
    # command — so an unreadable `~/.config/crom/config.toml` failed all of them, the ones
    # that would have repaired it included. [LAW:no-ambient-temporal-coupling] the
    # ordering is the repair's, and the two calls sitting in one function is what keeps it
    # from being luck.
    #
    # `repair_unreadable`, not `load_user_scope`: loading resolves `chrome_binary`, which
    # would make `find_chrome()` a precondition of every command including `crom init` —
    # the one `Session` exists to keep working on a machine with no Chrome yet. Whether a
    # file tokenizes as TOML is a question about bytes and asks nothing of the machine.
    config.repair_unreadable(user_config_file(), namespace=USER_NAMESPACE, log=log)
    # The seed comes from `model.DEFAULT_SEED`, which the project template renders too.
    # The literal `SeedChrome()` that used to sit here was the half of the disagreement
    # that happened to be right. [LAW:one-source-of-truth]
    #
    # `ensure_profile`, not `add_profile`: the goal is that the declaration *exist*, not
    # that this process be the one to write it. On a fresh machine two crom invocations
    # both find no user config and both try; `add_profile` raises FileExistsError at the
    # loser — a reported failure for a race that harmed nothing. Converging makes it a
    # no-op instead of an error to catch.
    configwrite.ensure_profile(
        user_config_file(),
        ProfileSpec(name="default", seed=DEFAULT_SEED),
        header=configwrite.USER_CONFIG_HEADER,
    )
