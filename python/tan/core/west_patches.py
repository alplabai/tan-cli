# SPDX-License-Identifier: Apache-2.0
"""Pure decisions for `tan bootstrap`'s `zephyr/patches.yml` phase (tan-cli#1296).

No IO here: `tan.commands.bootstrap_patches` owns every spawn and feeds the
results through these functions, so the exit-code contract of alp-sdk's
`scripts/verify_west_patches.py` lives in exactly one testable place.
"""
from __future__ import annotations

#: The verifier's exit codes (its module docstring is the authority).
VERIFY_APPLIED = 0
#: Everything this workspace carries is patched, but a module `patches.yml`
#: names is not checked out here, so it could not be inspected. Normal for a
#: narrow workspace -- a warning, never a failure.
VERIFY_UNCHECKED = 3
#: Nothing could be inspected (no workspace, unreadable/empty `patches.yml`).
VERIFY_UNRUNNABLE = 2

#: Bounds for the output excerpt put into an issue message.
_TAIL_LINES = 12
_TAIL_CHARS = 1500


def classify_verify(returncode: int | None) -> str:
    """`"applied"` / `"unchecked"` / `"unapplied"` / `"unrunnable"` for one run
    of the verifier, before OR after the per-module applies.

    `"unrunnable"` is exit 2 ("nothing could be inspected": no workspace,
    unreadable `patches.yml`) or `None` (the verifier never launched). It is NOT
    "unapplied": the patch state is unknown, and saying "not applied" would send
    the reader to the wrong place. Re-verifying after the applies matters
    because `west patch apply` exiting 0 is not evidence it applied anything
    (it exits 0 on a missing `patches.yml`, an empty patch list, and an
    unresolvable `module:`).
    """
    if returncode == VERIFY_APPLIED:
        return "applied"
    if returncode == VERIFY_UNCHECKED:
        return "unchecked"
    if returncode == VERIFY_UNRUNNABLE or returncode is None:
        return "unrunnable"
    return "unapplied"


def parse_unapplied(stdout: str) -> list[str]:
    """Module names from `--list-unapplied` (one per line), de-duplicated in
    first-seen order. Blank lines are ignored."""
    seen: dict[str, None] = {}
    for line in stdout.splitlines():
        name = line.strip()
        if name:
            seen.setdefault(name, None)
    return list(seen)


def output_tail(text: str) -> str:
    """The last few lines of child output, bounded, for an issue message --
    the cause (`patch does not apply`, a `pykwalify` ImportError) is at the
    end, so the head is what gets dropped."""
    lines = [line.rstrip() for line in text.strip().splitlines() if line.strip()]
    tail = "\n".join(lines[-_TAIL_LINES:])
    return tail[-_TAIL_CHARS:]
