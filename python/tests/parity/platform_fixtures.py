# SPDX-License-Identifier: Apache-2.0
"""A PER-PLATFORM fixture store, for the last two live-only cases in this
package (tan-cli#409).

`test_support_bundle_oracle_parity.py`'s two remaining cases were the only
thing keeping `tests/parity/` from replaying with no `target/` present, and
the reason they were left is real: what they compare -- doctor's whole check
list and statuses, plus the whole `inspect.context` -- is decided by the HOST
as much as by the binary. `longPaths` exists on Windows and nowhere else; a
darwin capture freezes `macos-aarch64` into a check `detail` and adds
`codeLLDBExtension`/`lldb` rows. One frozen answer replayed on another OS
diffs two platforms' genuinely different -- and both correct -- behaviour.

The conclusion drawn from that was "needs a win32 host", and it was half
right. A SINGLE-keyed store cannot hold this. A store keyed by
`(case, sys.platform)` can: every replay reads the answer captured on ITS OWN
platform, and no comparison ever crosses an OS boundary. What that still
needs is a capture ON each platform -- and this repository already has three,
in `.github/workflows/`. `capture-platform-fixtures.yml` runs the capture on
`ubuntu-latest`, `windows-latest` and `macos-latest` and uploads the result,
which is how the `win32` key gets produced without anyone owning a Windows
machine.

## Absent key: SKIP naming the platform, never a silent pass

A missing key skips **with the platform in the reason**, and
`test_parity_freeze_completeness.py` separately FAILS while any target
platform is uncaptured. That split is deliberate: the skip keeps a
partially-captured store usable while the three artifacts are being
collected, and the gate is what stops "partially captured" becoming the
permanent state -- which is exactly how the hole this store closes was
allowed to persist.

## Scrubbing

The compared surface embeds scratch directories (`inspect.context`'s project
and SDK roots, `outputPath`). Callers pass their roots and both sides get the
identical `oracle_fixtures.scrub` substitution, the same discipline
`oracle.rust_run` uses. Separator normalisation is NOT applied and must not
be: a backslash-vs-slash difference between the two binaries on ONE host is a
real divergence, and this store never compares across hosts, so there is
nothing a normalisation could legitimately reconcile.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Callable

from . import oracle_fixtures

FIXTURES_DIR = Path(__file__).resolve().parent / "oracle_fixtures"
FIXTURE_PATH = FIXTURES_DIR / "platform_bound.json"

#: The platforms a complete store must hold: the three `sys.platform` values
#: this project's CI actually runs (`ubuntu-latest`, `windows-latest`,
#: `macos-latest`). Not "every platform Python knows" -- a store is complete
#: when every leg that REPLAYS it has an answer.
TARGET_PLATFORMS = ("linux", "win32", "darwin")

#: The same two switches `oracle_fixtures` reads, so one recipe captures every
#: store in this directory.
LIVE = oracle_fixtures.LIVE
CAPTURE = oracle_fixtures.CAPTURE

CAPTURE_RECIPE = (
    "cargo build --bin tan && TAN_PARITY_LIVE=1 TAN_PARITY_CAPTURE=1 "
    "python -m pytest tests/parity/test_support_bundle_oracle_parity.py "
    "(or push the branch and let .github/workflows/"
    "capture-platform-fixtures.yml run it on all three runners)"
)


class Absent(LookupError):
    """This platform has no frozen answer yet. Deliberately its own type: the
    caller turns it into a SKIP that NAMES the platform, which reads
    differently from a missing-binary skip and is what the completeness gate
    reports on."""


def _key(case_id: str, platform: str | None = None) -> str:
    return f"{case_id}::{platform or sys.platform}"


def _load() -> dict[str, Any]:
    if not FIXTURE_PATH.exists():
        return {}
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def _save(data: dict[str, Any]) -> None:
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    # Sorted, so a capture on one platform produces a diff containing only
    # that platform's keys -- three runners writing the same file must not
    # each reorder the other two's entries.
    FIXTURE_PATH.write_text(
        json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def captured_platforms(case_id: str) -> set[str]:
    """Which platforms have an answer for `case_id`. Read by the
    freeze-completeness gate, so "is this case frozen?" is answered from the
    STORE rather than from a hand-kept list that can drift away from it."""
    prefix = f"{case_id}::"
    return {key[len(prefix) :] for key in _load() if key.startswith(prefix)}


def missing_platforms(case_ids: tuple[str, ...]) -> list[str]:
    """`["<case>::<platform>", ...]` for every target platform with no
    answer -- the gate's whole input, computed here so the store stays the
    single source of that truth."""
    return sorted(
        _key(case_id, platform)
        for case_id in case_ids
        for platform in TARGET_PLATFORMS
        if platform not in captured_platforms(case_id)
    )


def resolve_for_platform(
    case_id: str, live_fn: Callable[[], Any], *, scrub_roots: tuple[Path | str, ...] = ()
) -> Any:
    """The oracle's answer for `case_id` ON THIS PLATFORM.

    Frozen replay by default -- `live_fn` is never called and no Rust binary
    is needed. `TAN_PARITY_LIVE=1` spawns it; `+TAN_PARITY_CAPTURE=1` also
    writes what it produced under this platform's key.

    Raises :class:`Absent` when this platform has no answer yet. Never falls
    back to another platform's key -- that fallback is the exact defect the
    whole store exists to prevent.
    """
    key = _key(case_id)
    if LIVE:
        result = json.loads(json.dumps(live_fn()))
        if scrub_roots:
            result = oracle_fixtures.scrub(result, *scrub_roots)
        if CAPTURE:
            data = _load()
            data[key] = result
            _save(data)
        return result

    data = _load()
    if key not in data:
        raise Absent(
            f"no frozen oracle answer for {case_id!r} on {sys.platform!r}. "
            f"This case is platform-bound: an answer captured on another OS "
            f"would diff two platforms' genuinely different behaviour. "
            f"Capture it: {CAPTURE_RECIPE}"
        )
    return data[key]


def merge_from(directory: Path) -> int:
    """Fold every `platform_bound*.json` under `directory` into the committed
    store, returning how many keys were added or changed.

    This is how three CI runners' artifacts become one file: each uploads the
    store as IT saw it (its own platform's keys plus whatever was already
    committed), and merging BY KEY keeps all three without any runner having
    to see the others' output. Used by the capture workflow's collect step.
    """
    data = _load()
    added = 0
    for path in sorted(directory.rglob("platform_bound*.json")):
        for key, value in json.loads(path.read_text(encoding="utf-8")).items():
            if data.get(key) != value:
                data[key] = value
                added += 1
    if added:
        _save(data)
    return added
