"""The one channel crom uses to say what it did on the user's behalf.

crom converges: a command that finds a prerequisite unmet performs it rather than
reporting it, so repairs happen below the CLI — an unreadable config is reset, a
profile you only referred to is declared, a namespace whose project is gone is
dropped. [LAW:no-silent-failure] none of that may be silent, and none of it belongs on
stdout, which carries the answer a script parses.

Passed in as a `log=` parameter rather than called directly, so the repair stays a
description the caller performs and a test can capture what was said without capturing
the process's stderr. [LAW:effects-at-boundaries]
"""

import sys


def to_stderr(message: str) -> None:
    print(message, file=sys.stderr)
