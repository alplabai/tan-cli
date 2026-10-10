# SPDX-License-Identifier: Apache-2.0
"""Pure decisions for `tan bootstrap`'s `zephyr/patches.yml` phase (tan-cli#1296).

No IO here: `tan.commands.bootstrap_patches` owns every spawn and feeds the
results through these functions, so the exit-code contract of alp-sdk's
`scripts/verify_west_patches.py` lives in exactly one testable place.
"""
from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass

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


# ---------------------------------------------------------------------------
# Workspace patch CHECK (tan-cli#1376): `tan doctor` / `tan build` ask the SAME
# verifier "is `zephyr/patches.yml` applied" without applying anything.
# ---------------------------------------------------------------------------

#: One failing line of the verifier's stderr: `  ABSENT      <patch rel path>`.
_FAILURE_LINE = re.compile(r"^\s+(ABSENT|DRIFTED|UNRESOLVED)\s+(\S+)\s*$")


@dataclass(frozen=True)
class UnappliedPatch:
    """One patch the verifier reports as not applied. `verdict` is the
    verifier's own word (`ABSENT`/`DRIFTED`/`UNRESOLVED`); `patch` is the path
    under `zephyr/patches/` it prints (e.g. `hal_alif/0001-....patch`)."""

    verdict: str
    patch: str


def parse_unapplied_patches(stderr: str) -> list[UnappliedPatch]:
    """The patches named by the verifier's failure report, in report order."""
    found: list[UnappliedPatch] = []
    for line in stderr.splitlines():
        m = _FAILURE_LINE.match(line)
        if m is not None:
            found.append(UnappliedPatch(m.group(1), m.group(2)))
    return found


def describe_unapplied(patches: list[UnappliedPatch], modules: list[str]) -> str:
    """`hal_alif/0001-x.patch (ABSENT), ...` plus the modules to patch."""
    names = ", ".join(f"{p.patch} ({p.verdict})" for p in patches) or "unnamed patches"
    where = f" in module(s) {', '.join(modules)}" if modules else ""
    return f"{names}{where}"


def patch_fix_text(modules: list[str], workspace_dir: str) -> str:
    """The exact remedy: `tan bootstrap` applies exactly the unapplied modules
    (verify, `west patch --dst-module <m> apply`, re-verify). The per-module
    west form is the manual equivalent; a bare `west patch apply` is NOT
    offered because it re-applies already-patched modules and fails."""
    manual = (
        "; ".join(f"west patch --dst-module {m} apply" for m in modules)
        or "west patch --dst-module <module> apply"
    )
    return f"run `tan bootstrap` (or, from {workspace_dir}: {manual})"


def cache_key(patches_yml: bytes, heads: dict[str, str]) -> str:
    """Stable digest of (`patches.yml` bytes, every workspace module's HEAD)."""
    h = hashlib.sha256()
    h.update(hashlib.sha256(patches_yml).digest())
    for path in sorted(heads):
        h.update(f"\0{path}\0{heads[path]}".encode())
    return h.hexdigest()


def zephyr_base_note(env_value: str | None, workspace_zephyr: str | None) -> str | None:
    """Message when `$ZEPHYR_BASE` is set and names a different tree than the
    resolved workspace's `zephyr/`; `None` otherwise. tan never honours the
    override, so the user must hear it is being ignored."""
    if not env_value or workspace_zephyr is None:
        return None

    def norm(p: str) -> str:
        return os.path.normcase(os.path.realpath(p))

    if norm(env_value) == norm(workspace_zephyr):
        return None
    return (
        f"$ZEPHYR_BASE={env_value} is ignored: tan always builds with the resolved west "
        f"workspace's zephyr ({workspace_zephyr}). To build against another tree, run "
        "tan from that workspace or bootstrap it."
    )
