# SPDX-License-Identifier: Apache-2.0
"""`tan doctor`'s ADR-0018 curated-library verdict, ported out of alp-sdk.

This is the port of `scripts/alp_cli/doctor.py::_check_libraries` (alp-sdk
ADR-0020 end-state B: the user command surface is `tan`, so a user-facing
check does not stay behind in alp-sdk as a second CLI). It answers one
question about the SELECTED PROJECT -- for every curated library the
project's `board.yaml` asks for, what TIER and LICENCE is it, and can it
actually be wired on this target?

## Why it resolves rather than merely lists

Tier and licence could be read straight out of `metadata/libraries/<name>.yaml`
with no planner at all. The compatibility half cannot: `requires:` constraints
are checked against the project's SoC cores and per-core OSes, and a library
with no `integration:` section for any OS this project runs is a build-time
refusal. Reporting the first without the second is the failure mode a doctor
exists to prevent -- a green "lvgl (tier A, MIT)" line on a project where the
emitter will refuse it. So this routes through
`tan.planner.libraries.resolve_selection`, the SAME function
`tan.planner.buildplan` calls when it renders the plan; the report and the
build can therefore never disagree about a selection.

## WARN, never FAIL

An incompatible selection is a WARN here because the hard gate is the emit:
`tan build` refuses outright. `tan doctor` is a preflight report and a `fail`
row from it costs exit 4, which would make an unbuildable-library selection
indistinguishable from a missing toolchain. Same posture alp-sdk's original
took, kept deliberately.

alp-sdk's `alp doctor` had a second lever this port does not carry: `--strict`,
which folded a WARN row into a nonzero exit (`n_warn` counted the libraries
row same as any other). `tan doctor` has no `--strict` flag AT ALL -- it is
not a libraries-specific gap, every WARN-worthy check in this module is
already exit-0-only (`exit_code_for` above only inspects `fail`). Adding
`--strict` scoped to just this one row would make it behave differently from
every other check `tan doctor` reports; that is a real, pre-existing capability
difference from `alp doctor`, not something this port introduces or reverses,
and it is accepted deliberately: `tan build`'s own refusal is the actual gate
a CI pipeline should script against, not a doctor exit code.

## The defect fixed in the port, not carried across

alp-sdk's version read the RAW `libraries:` list out of the YAML and passed
each entry straight into `load_manifest()` and an f-string. board.yaml's
`libraries:` entries are `{name, cores?}` OBJECTS as well as bare strings
(`alp_orchestrate.loader._normalize_libraries`, and `tan.planner.loader`'s
relocated twin), so a core-scoped entry reached `load_manifest` as a dict:

    $ cd examples/peripheral-io/fmt-formatting
    $ PYTHONPATH=<alp-sdk>/scripts python3 -m alp_cli doctor
    ...
    alp_orchestrate.models.OrchestratorError: unknown library
    `{'name': 'fmt', 'cores': ['m55_hp']}` in `libraries:` -- no manifest at
    metadata/libraries/{'name': 'fmt', 'cores': ['m55_hp']}.yaml.

Measured, not reasoned: exit 1, the whole command down, on the SUCCESS path
(`resolve_selection` had already passed, using the loader's normalised names;
the raw re-read for the labels is what raised). All 42 in-tree alp-sdk
examples that select libraries use that shape, so there was no shipped
project the check worked on. alp-sdk's `docs/getting-started.md` carried it
as a known open bug; the port is where it goes away.

Names therefore come from `scoped_names(project)` -- the loader's own
normalised, deduplicated, two-channel view -- not from the raw document. The
dedup matters on its own: `libraries: [fmt, {name: fmt, cores: [m55_hp]}]` is
schema-valid (`uniqueItems` compares whole entries) and the raw read reports
`fmt` twice.

The raw read survives only as the cheap "is anything selected at all" peek (a
project selecting nothing must cost nothing and stay silent) and as the label
fallback when the project could not be LOADED, which is the one path where no
normalised view exists to take names from.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = ["LibraryReport", "inspect_selection"]

#: `outcome` values, and what each means for the caller's Check status.
#:
#: `ok`            every selection resolved and is wireable          -> pass
#: `unresolved`    a name, a `requires:` or an `integration:` failed -> warn
#: `unreadable`    `board.yaml` itself could not be parsed           -> warn
#: `unverifiable`  libraries ARE selected but nothing could check    -> warn
#:                 them (no SDK checkout resolved, or the planner is
#:                 unavailable/already bound elsewhere in this process)
OUTCOMES = ("ok", "unresolved", "unreadable", "unverifiable")


@dataclass(frozen=True)
class LibraryReport:
    """One project's curated-library verdict.

    `labels` are the rendered `name (tier X, LICENCE)` strings, in declaration
    order -- built even on the `unresolved` path, where the offending entry
    reads `name (unknown)`, because "which of my five libraries is the broken
    one" is the question that report has to answer.

    `detail` carries the underlying refusal verbatim on every non-`ok`
    outcome: it is the emitter's OWN message (`OrchestratorError`), so the
    warning a customer reads here is word-for-word the error `tan build`
    will print, not a paraphrase of it.
    """

    outcome: str
    names: tuple[str, ...]
    labels: tuple[str, ...] = ()
    detail: str = ""

    def __post_init__(self) -> None:
        if self.outcome not in OUTCOMES:
            raise ValueError(f"unknown library-report outcome {self.outcome!r}; expected one of {OUTCOMES}")


def _raw_names(document: Any) -> list[str]:
    """The `libraries:` names as WRITTEN, for the two paths that have no
    loaded project to ask (the emptiness peek, and the label fallback when
    loading failed). Both entry shapes are accepted -- see the module
    docstring for why reading only the string shape was a defect. NOT
    deduplicated and NOT canonicalised: no loader has run on either path, so
    what the customer wrote is all there is to report."""
    if not isinstance(document, dict):
        return []
    entries = document.get("libraries") or []
    if not isinstance(entries, list):
        return []
    names: list[str] = []
    for entry in entries:
        if isinstance(entry, str):
            names.append(entry)
        elif isinstance(entry, dict) and entry.get("name"):
            names.append(str(entry["name"]))
    return names


def _labels(names, load_manifest, metadata_root: Path) -> tuple[str, ...]:
    """`name (tier X, LICENCE)` per selection, in declaration order.

    Per-name `try`, not one around the loop: on the `unresolved` path exactly
    one entry is usually the broken one, and "which of my five" is the whole
    question that report answers. A single guard would collapse all five to
    `(unknown)` the moment any one of them failed to load."""
    out: list[str] = []
    for name in names:
        try:
            manifest = load_manifest(name, metadata_root)
        except Exception:
            out.append(f"{name} (unknown)")
            continue
        out.append(f"{name} (tier {manifest.get('tier', '?')}, {manifest.get('license', '?')})")
    return tuple(out)


def _unverifiable(names: list[str], detail: str) -> LibraryReport:
    return LibraryReport(
        "unverifiable", tuple(names), tuple(f"{n} (unverified)" for n in names), detail
    )


def _planner(sdk_root: Path):
    """`(load_board_yaml, load_manifest, resolve_selection, scoped_names)`.

    Binds BEFORE importing `tan.planner`: `tan/planner/paths.py` evaluates
    `REPO = sdk_root()` at module scope and half the package freezes
    `metadata_root` as a default argument, so an unbound import cannot be
    repaired afterwards (`tan.planner_root`). A rebind to a DIFFERENT root in
    an already-planner-bound process raises `PlannerRootError` by design --
    doctor is a REPORT and must not be the thing that decides which SDK a
    process is pointed at, so the caller turns that into `unverifiable`
    rather than forcing it."""
    from tan.planner_root import bind_sdk_root

    bind_sdk_root(sdk_root)
    from tan.planner import load_board_yaml
    from tan.planner.libraries import load_manifest, resolve_selection, scoped_names

    return load_board_yaml, load_manifest, resolve_selection, scoped_names


def inspect_selection(
    board_yaml: Path | None, sdk_root: Path | None
) -> LibraryReport | None:
    """Resolve the project's `libraries:` selection, or `None` for nothing to say.

    `None` -- not a status, an ABSENCE -- for no `board.yaml` in scope and for
    a `board.yaml` that selects no libraries; see `doctor_cmd.libraries_check`
    for why a "0 selected" row is worse than no row. A project that DOES
    select libraries always gets one, including when nothing could be
    verified.

    Never raises: every failure becomes an outcome, because this runs inside
    `_collect`, whose stated invariant is that no probe may raise.
    """
    if board_yaml is None or not board_yaml.is_file():
        return None
    try:
        import yaml

        document = yaml.safe_load(board_yaml.read_text(encoding="utf-8")) or {}
    except Exception as err:
        # `boardYaml` owns the verdict on the file itself and runs immediately
        # above this, so the wording here stays scoped to the library line.
        return LibraryReport("unreadable", (), (), f"could not read {board_yaml}: {err}")

    raw = _raw_names(document)
    if not raw:
        return None
    if sdk_root is None:
        return _unverifiable(
            raw, "no alp-sdk checkout resolved, so metadata/libraries/*.yaml could not be read"
        )
    return _resolve(board_yaml, Path(sdk_root), raw)


def _resolve(board_yaml: Path, sdk_root: Path, raw: list[str]) -> LibraryReport | None:
    """The second half of `inspect_selection`: does what the customer asked
    for actually resolve against THIS SDK checkout and THIS target? Split from
    the first half at the point the planner is needed -- everything above it
    is a read of the customer's own document and answers with no SDK at all.
    """
    try:
        load_board_yaml, load_manifest, resolve_selection, scoped_names = _planner(sdk_root)
    except Exception as err:
        return _unverifiable(raw, f"the library metadata in {sdk_root} could not be read: {err}")

    metadata_root = sdk_root / "metadata"
    try:
        project = load_board_yaml(board_yaml, metadata_root=metadata_root)
    except Exception as err:
        # No loaded project means no normalised name list to take -- the raw
        # spelling is all there is on this one path.
        return LibraryReport(
            "unresolved", tuple(raw), _labels(raw, load_manifest, metadata_root), str(err)
        )

    # `scoped_names`, not the raw list: the loader's own view of the selection,
    # covering BOTH declaration channels with aliases already applied -- see
    # the module docstring for the defect that reading `raw` here produced.
    names = tuple(scoped_names(project))
    if not names:
        return None
    labels = _labels(names, load_manifest, metadata_root)
    try:
        resolve_selection(project, metadata_root)
    except Exception as err:
        return LibraryReport("unresolved", names, labels, str(err))
    return LibraryReport("ok", names, labels)
