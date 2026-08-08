# SPDX-License-Identifier: Apache-2.0
"""The one implementation of `GlobalArgs::can_prompt()` — the gate every
command must pass before it prompts a human or mutates the host.

Ported from the Rust oracle's `GlobalArgs::can_prompt()`, whose own
`--non-interactive` help text states the rule the port must honour verbatim:

    Never prompt. A command with a documented default takes it (`tan init`
    scaffolds `zephyr-app` into `.`); one without fails instead of asking
    (`tan scaffold` needs `--name`). **The same rule applies unasked when
    stdin or stderr is not a terminal — piped, redirected, or a CI runner.**

That last sentence is the half a re-derivation keeps dropping, and dropping it
is not cosmetic. Before this module existed the check was written out by hand
in four places and **one of them was wrong**: `doctor --fix` (tan-cli#91)
tested only `not non_interactive and not ci and not json_mode` and omitted both
`isatty()` calls, so a CI runner that redirected its output but did not happen
to pass `--ci` got **unattended host mutation** — demonstrated live with fully
captured pipes, where `tan doctor --fix` spawned four real `winget install`
runs (`Git.Git`, `Kitware.CMake`, `Python.Python.3.12`, `Ninja-build.Ninja`)
with nobody watching. A redirected stdio stream is the single most common shape
of an automated run, so the omitted condition was the one that mattered most.

Hence one function, imported. Duplicating a consent gate means every future
copy is another chance to drop the clause that makes it a consent gate at all.

**Why BOTH `stdin` and `stderr`, not just `stdin`.** A prompt is a
question-and-answer pair and each half needs its own real terminal: `stdin`
carries the answer, and `stderr` — never `stdout`, which belongs to the
envelope — carries the question. `tan doctor --fix < /dev/null` has no way to
receive consent; `tan doctor --fix 2>log` has no way to ask for it, and would
block on a question the user never saw. Requiring both is what makes "the user
actually agreed to this" true rather than merely likely.

**Not `stdout`.** Under `--format json` stdout is a single parsed envelope, and
`| jq` is a normal, fully-interactive way to run tan. Testing `stdout.isatty()`
would refuse consent in a session where the human is sitting right there. The
`json_mode` flag already covers the case that actually matters.
"""
from __future__ import annotations

import sys


def can_prompt(*, non_interactive: bool, ci: bool, json_mode: bool) -> bool:
    """Whether this invocation may prompt the user, or take any other action
    that needs a human's live consent (installing a toolchain, overwriting a
    file, relocating a checkout).

    All five conditions must hold. The three flags are the caller's explicit
    "do not ask me" signals; the two `isatty()` calls are the same rule applied
    **unasked**, for the automated runs that never thought to pass a flag.

    A command with a documented default takes it when this returns `False`; one
    without a default fails instead of asking.

    `sys.stdin`/`sys.stderr` are probed inside `try`/`except (AttributeError,
    ValueError)` (tan-cli#503), not an `is not None` check ahead of
    `.isatty()`: an EARLIER round of this fix used `is not None`, which
    stops `None.isatty()` (fd 0/2 closed -- a daemon/service parent, some CI
    runners, a Windows `pythonw`/frozen no-console launch) but does nothing
    for a stream that EXISTS yet still lacks `.isatty()` -- exactly the shape
    `cli.main` already creates for `sys.stderr` under `--format json`
    (`_TeeStderr`: `write`/`flush`/`getvalue` only). `json_mode` short-
    circuits this function before it would reach that object on the path
    that passes it correctly, but a caller that forgets to thread `json_mode`
    through (the exact bug `build_cmd._dispatch` had) reaches the bare
    `.isatty()` anyway, and `is not None` would not have caught it -- a
    `None.isatty()` and a `_TeeStderr().isatty()` raise the identical
    `AttributeError`, so one `except` clause handles both shapes without
    needing to name either one, and does not depend on every future caller
    getting `json_mode` right.
    """
    if non_interactive or ci or json_mode:
        return False
    try:
        if not sys.stdin.isatty():
            return False
    except (AttributeError, ValueError):
        return False
    try:
        return sys.stderr.isatty()
    except (AttributeError, ValueError):
        return False
