# SPDX-License-Identifier: Apache-2.0
"""One wrapping seam every text-mode command can share.

Measured across the 24 read-only commands: 13 emit at least one line over
100 columns (`doctor` 557, `monitor` 362, `size` 267, ...), and only
`build_cmd` (via stdlib `textwrap`) wraps anything at all. `doctor` is the
first command to be fixed; this module is the reusable seam the rest of that
sweep points at afterwards, so it carries no `doctor` concept -- a caller
supplies its own prefix and hanging indent, this just wraps text under them.
"""
from __future__ import annotations

import textwrap


def wrap_block(body: str, width: int, initial_indent: str, hanging_indent: str) -> list[str]:
    """Wrap `body` to `width` columns, `initial_indent` prefixing the first
    line and `hanging_indent` every continuation line -- so a continuation
    never starts at column 0 and reads as subordinate to the line above it.

    `width` is a target, not a hard ceiling: `break_long_words=False` (see
    below) means a single token that, plus whichever indent frames it, is
    alone longer than `width` -- a SKU, a long flag, a path -- comes back
    over-length and intact on its own line rather than being cut mid-
    character. Every OTHER line this returns does fit `width`; a caller that
    needs a hard cap on every line cannot assume that of this one.

    Always returns at least one line (`[initial_indent]` for an empty
    `body`), so a caller building `"{prefix}{first_wrapped_chunk}"` never has
    to special-case an empty result.
    """
    # `break_on_hyphens`/`break_long_words` default True, which is wrong for
    # this seam: this repo's house rule is that SKUs, part numbers, flags and
    # paths are reproduced verbatim, and stdlib's defaults mangle exactly
    # those -- `arm-zephyr-eabi` splits into `arm-` / `zephyr-eabi`,
    # `--sdk-root` into `--sdk-` / `root`. Both off trades a hard `width`
    # ceiling for that guarantee -- see the docstring above for the resulting
    # overflow contract.
    wrapped = textwrap.wrap(
        body,
        width=width,
        initial_indent=initial_indent,
        subsequent_indent=hanging_indent,
        break_on_hyphens=False,
        break_long_words=False,
    )
    return wrapped or [initial_indent]
