"""crom's fixed launch policy — the flags every managed Chrome gets, whoever launches it.

[LAW:one-source-of-truth] this list is the sole owner of that policy;
[LAW:dataflow-not-control-flow] it is data spread into argv, not branches in a launcher.

The top-level switches are long-stable Chrome/Chromium command-line switches. The
trailing --disable-features entries are the version-fragile part: Chrome silently
ignores feature names it no longer knows, so new promo/upsell surfaces get suppressed
by adding a name there, not by touching any code.

A config's `flags` are appended *after* this list, so a profile that genuinely needs a
different setting can override it the way Chrome itself resolves conflicts: last wins.
`crom config <profile>` prints the fully composed argv, so the policy is never invisible.
"""

LAUNCH_POLICY_FLAGS: tuple[str, ...] = (
    # "Don't check for default browser" — suppress the default-browser nag.
    "--no-default-browser-check",

    # "Don't send telemetry." No single switch does this; --disable-background-networking
    # is the big one (kills UMA metrics upload, field-trial fetches, and component /
    # safe-browsing update pings at once), and the rest close the remaining back-channels.
    "--disable-background-networking",
    "--disable-breakpad",            # crash-report upload
    "--disable-domain-reliability",  # network-error reports to Google
    "--no-pings",                    # hyperlink-auditing pings
    # A separate real-time channel background-networking does NOT close: the Safe
    # Browsing phishing lookup on navigation. Trade-off: no client-side phishing check.
    "--disable-client-side-phishing-detection",

    # "Don't register a profile / sign-in junk" — skip the first-run welcome/registration
    # flow and the account sync machinery entirely.
    "--no-first-run",
    "--disable-sync",

    # "Don't try to sell me things" — the upsell surfaces. --disable-search-engine-choice-screen
    # kills the search-engine chooser; ChromeWhatsNewUI is the post-update "What's New" promo tab.
    "--disable-search-engine-choice-screen",
    "--disable-features=ChromeWhatsNewUI",

    # Quiet UI — chrome-only nags, invisible to web content.
    "--disable-session-crashed-bubble",  # no "Chrome didn't shut down correctly" bubble
)
