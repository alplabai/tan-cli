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

from tan.env import stderr_is_tty, stdin_is_tty


def can_prompt(*, non_interactive: bool, ci: bool, json_mode: bool) -> bool:
    """Whether this invocation may prompt the user, or take any other action
    that needs a human's live consent (installing a toolchain, overwriting a
    file, relocating a checkout).

    All five conditions must hold. The three flags are the caller's explicit
    "do not ask me" signals; the two `isatty()` calls are the same rule applied
    **unasked**, for the automated runs that never thought to pass a flag.

    A command with a documented default takes it when this returns `False`; one
    without a default fails instead of asking.

    tan-cli#488: `sys.stdin` itself, not just the result of calling `.isatty()`
    on it, can be `None` -- a process launched with its standard handles
    detached (a GUI launcher, `pythonw`-style spawn, or a shell that closed fd
    0 before exec, `0<&-`) leaves `sys.stdin` unbound rather than merely
    non-interactive, and a bare `sys.stdin.isatty()` then raises
    `AttributeError: 'NoneType' object has no attribute 'isatty'`. This is the
    ONE place that guard belongs: every caller of `can_prompt` (`doctor_cmd`,
    `scaffold_cmd`) reaches this line before any caller-side guard could run,
    so duplicating the `is not None` check downstream (as doctor_cmd.py's
    `fix_suppressed_issue` still does, for its own separate per-condition
    explanation) does not help -- this call is the one that crashes first.

    tan-cli#488 round 5: the SAME detachment guards ONLY `sys.stdin`, not
    `sys.stderr` -- the very next operand of this same `and` chain, and just
    as reachable: the identical detached-process shape can leave `sys.stdin`
    live (redirected to a real, non-tty file) while `sys.stderr` alone is
    unbound, since each handle is detached independently by whatever spawned
    the process. Verified against the real binary, not a mock: a `pty.fork()`
    child with a genuine tty on stdin (`isatty() is True`, so the chain
    reaches this line) and `stderr` closed before `exec` crashed
    `tan doctor --fix` with exactly this `AttributeError` -- `sys.stdin is not
    None` alone does not protect the `sys.stderr.isatty()` call one line
    below it.

    tan-cli#488 round 6: `is not None` was never the whole guard either --
    it stops a detached (`None`) handle from crashing `.isatty()`, but not a
    handle that EXISTS and simply has no `.isatty()` method, which is
    exactly what `sys.stderr` becomes under `tan --format json`
    (`tan.cli.main` tees it through `_TeeStderr`: `write`/`flush`/
    `getvalue` only). This function's own `not json_mode` operand happens to
    short-circuit ahead of the `sys.stderr` checks before that shape is ever
    reached today, so `can_prompt` itself was never observed to crash on
    it -- but `tan.commands.build_cmd._dispatch` hand-rolled the identical
    `is not None and .isatty()` pair with no `json_mode` operand in front of
    it at all, and DID crash, on a real `tan run --format json`
    (`AttributeError: '_TeeStderr' object has no attribute 'isatty'`,
    measured). Both operands now route through `tan.env.stdin_is_tty`/
    `stderr_is_tty` -- the one shared probe (tan-cli#288) that already
    wraps both exception classes -- rather than leaving this copy's safety
    resting on an operand order a future edit could reshuffle.
    """
    return (
        not non_interactive
        and not ci
        and not json_mode
        and stdin_is_tty()
        and stderr_is_tty()
    )
