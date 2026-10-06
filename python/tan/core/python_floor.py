# SPDX-License-Identifier: Apache-2.0
"""The Python floor `tan build` and `tan doctor` both enforce -- ONE
composition (tan-cli#1317), pure `tan/core` so neither command imports the other.

`effective_python_floor` is the higher of the alp-sdk manifest's
`prerequisites.pythonMinVersion` and the floor Zephyr's CMake itself enforces
(`zephyr_python_floor`).
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from tan.core.bootstrap import BOOTSTRAP_MANIFEST_SCHEMA_VERSION

#: Zephyr's own floor, from `<zephyr>/cmake/modules/python.cmake`'s
#: `set(PYTHON_MINIMUM_REQUIRED 3.12)`. The LAST-resort fallback --
#: `zephyr_python_floor` reads the real file when a workspace resolves, and
#: (tan-cli#606) prefers alp-sdk's own manifest-declared
#: `zephyr.pythonMinVersion` over this constant when no workspace resolves but
#: a manifest does; this is what is left once BOTH are unavailable, so a
#: Zephyr bump raises the floor on the customer's machine without waiting for
#: a tan release only via one of those two live reads, never this one.
ZEPHYR_PYTHON_FLOOR = (3, 12)

#: The floor `metadata/bootstrap.json` is assumed to declare when no manifest
#: resolves at all -- used ONLY as the `manifest_floor` input to `max()` below,
#: never as a verdict by itself. It mirrors `crate::util::MIN_PYTHON`
#: (`crates/tan-cli/src/util.rs`), which is frozen at 3.10 and does NOT track
#: `metadata/bootstrap.json` -- that Rust constant and the manifest's declared
#: `pythonMinVersion` are two independently-edited numbers, not one fact, and
#: they can and do drift apart (the manifest is mid-raise to 3.12 as of this
#: writing; the oracle constant is not). The manifest is the authority: when it
#: resolves AND declares `pythonMinVersion`, that number is read live and this
#: constant is not consulted for the verdict -- but a manifest that resolves
#: while omitting the key still falls back to this same constant (see
#: `resolve_manifest_python_floor`/`_collect` below), so this is not a
#: no-manifest-only fallback. `ZEPHYR_PYTHON_FLOOR` above still composes with
#: it via `max()` either way, so a resolvable SDK checkout with the key present
#: never depends on this value being current.
FALLBACK_PYTHON_FLOOR = (3, 10)


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        return None


def parse_two(raw: str) -> tuple[int, int] | None:
    match = re.search(r"(\d+)\.(\d+)", raw)
    return (int(match.group(1)), int(match.group(2))) if match else None


def zephyr_python_floor(
    zephyr_base: str | None, *, manifest_zephyr_floor: tuple[int, int] | None = None
) -> tuple[tuple[int, int], str]:
    """The floor Zephyr's CMake will actually enforce, and where it came from.

    Read from `<zephyr_base>/cmake/modules/python.cmake` when that resolves,
    because THAT is the file whose `PYTHON_MINIMUM_REQUIRED` aborts the build --
    a constant compiled into tan goes stale the moment Zephyr bumps it, and a
    stale floor here reintroduces exactly the silent gap this command exists to
    close.

    When it does NOT resolve, `manifest_zephyr_floor` -- alp-sdk's OWN declared
    `zephyr.pythonMinVersion` (tan-cli#606), when the caller's manifest read
    found one -- is now preferred over `ZEPHYR_PYTHON_FLOOR`: a fact alp-sdk
    already publishes beats a constant compiled into tan, the same reasoning
    that prefers `python.cmake` itself one level up. `ZEPHYR_PYTHON_FLOOR`
    remains the LAST resort, for an SDK whose manifest predates that key (or
    when no manifest resolves at all) -- every host at `tan bootstrap` time
    used to land here unconditionally; now only a manifest-less one does.

    `zephyr_base` is a plain path in, not necessarily `$ZEPHYR_BASE` itself --
    THIS function has no opinion on where it came from, only `_collect` (this
    module's `hostPython`/`pythonFloor` caller) does. As of tan-cli#301,
    `_collect` passes the resolved workspace's `zephyr/` subtree -- the SAME
    `tan.core.venv.west_workspace_dir` result `zephyrWorkspace` reports -- when
    one resolved, a literal `$ZEPHYR_BASE` read only when no workspace resolved
    at all, and `None` (landing on `ZEPHYR_PYTHON_FLOOR` below) when neither
    does; that is the three-way split the resulting `source` string names. The
    OTHER caller, `tan.commands.bootstrap_cmd.resolve_python_floor`, still
    passes a literal `$ZEPHYR_BASE` read directly -- `tan bootstrap` runs before
    any workspace can have resolved, so there is nothing else for it to prefer.

    **The fallback names WHICH of three causes fired (tan-cli#488 defect 7).**
    It used to be one hardcoded string -- "no $ZEPHYR_BASE workspace on this
    host to read `cmake/modules/python.cmake` from" -- for every way the read
    could fail, but only ONE of the three causes below makes that true. A
    `.west` workspace mid-`west update` (`zephyr_workspace_check`'s own
    "legitimate, working-in-progress host state") resolves a real
    `zephyr_base` whose `cmake/modules/python.cmake` simply is not there yet
    -- reported by `_collect` as `workspace`/`zephyrWorkspace` BOTH passing,
    in the same envelope that then blamed a `$ZEPHYR_BASE` env var never
    consulted for this call (`_collect` feeds this function the RESOLVED
    workspace's own `zephyr/` subtree, never `$ZEPHYR_BASE` itself, once a
    workspace resolves -- see above). `jlink_flash_device` fixed the identical
    shape for its own three-cause fallback in tan-cli#310; this mirrors it.
    """
    if manifest_zephyr_floor is not None:
        fallback_floor = manifest_zephyr_floor
        fallback_label = (
            f"alp-sdk metadata/bootstrap.json zephyr.pythonMinVersion "
            f"{fallback_floor[0]}.{fallback_floor[1]}"
        )
    else:
        fallback_floor = ZEPHYR_PYTHON_FLOOR
        fallback_label = f"tan's built-in pin {fallback_floor[0]}.{fallback_floor[1]}"

    if zephyr_base:
        path = Path(zephyr_base) / "cmake" / "modules" / "python.cmake"
        text = _read_text(path)
        if text is not None:
            match = re.search(r"PYTHON_MINIMUM_REQUIRED\s+(\d+)\.(\d+)", text)
            if match is not None:
                return (int(match.group(1)), int(match.group(2))), str(path)
            return fallback_floor, (
                f"Zephyr's PYTHON_MINIMUM_REQUIRED, from {fallback_label} -- {path} was "
                f"read but did not declare a parseable PYTHON_MINIMUM_REQUIRED"
            )
        return fallback_floor, (
            f"Zephyr's PYTHON_MINIMUM_REQUIRED, from {fallback_label} -- {path} could not "
            f"be read (a `.west` workspace mid-`west update` is a legitimate, "
            f"working-in-progress host state, not a broken one)"
        )
    return fallback_floor, (
        f"Zephyr's PYTHON_MINIMUM_REQUIRED, from {fallback_label} -- no $ZEPHYR_BASE "
        f"workspace on this host to read `cmake/modules/python.cmake` from"
    )


def read_manifest_floors(sdk_root: str | None) -> tuple[tuple[int, int], tuple[int, int] | None]:
    """`(manifest_floor, zephyr_manifest_floor)` from `<sdk>/metadata/bootstrap.json`
    with the SAME fallbacks `tan doctor`'s `_load_manifest` applies: an absent,
    unparseable, wrong-schema or `prerequisites`-less manifest yields
    `(FALLBACK_PYTHON_FLOOR, None)`."""
    if sdk_root is None:
        return FALLBACK_PYTHON_FLOOR, None
    text = _read_text(Path(sdk_root) / "metadata" / "bootstrap.json")
    if text is None:
        return FALLBACK_PYTHON_FLOOR, None
    try:
        doc = json.loads(text)
    except ValueError:
        return FALLBACK_PYTHON_FLOOR, None
    if not isinstance(doc, dict):
        return FALLBACK_PYTHON_FLOOR, None
    version = doc.get("schemaVersion")
    if (
        not isinstance(version, int)
        or isinstance(version, bool)
        or version != BOOTSTRAP_MANIFEST_SCHEMA_VERSION
    ):
        return FALLBACK_PYTHON_FLOOR, None
    prerequisites = doc.get("prerequisites")
    if not isinstance(prerequisites, dict):
        return FALLBACK_PYTHON_FLOOR, None
    manifest = parse_two(str(prerequisites.get("pythonMinVersion") or "")) or FALLBACK_PYTHON_FLOOR
    zephyr = doc.get("zephyr")
    raw = zephyr.get("pythonMinVersion") if isinstance(zephyr, dict) else None
    return manifest, parse_two(raw) if isinstance(raw, str) else None


def effective_python_floor(
    manifest_floor: tuple[int, int],
    zephyr_base: str | None,
    manifest_zephyr_floor: tuple[int, int] | None = None,
) -> tuple[tuple[int, int], str]:
    """`(floor, source)`: the highest anything in the build chain enforces."""
    zephyr_floor, zephyr_source = zephyr_python_floor(
        zephyr_base, manifest_zephyr_floor=manifest_zephyr_floor
    )
    if zephyr_floor >= manifest_floor:
        return zephyr_floor, zephyr_source
    return manifest_floor, "alp-sdk metadata/bootstrap.json pythonMinVersion"
