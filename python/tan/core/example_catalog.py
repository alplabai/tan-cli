# SPDX-License-Identifier: Apache-2.0
"""Read the SDK scaffold catalog's declared SoM support for ONE example, and
(tan-cli PR #996 Part 2) select a catalog record BY its `cores:` hardware
topology instead of by id or example path.

`find_example_by_cores` below is `tan init`'s customer-facing counterpart to
alp-sdk#1652's `find_template_by_cores` (`scripts/alp_template.py`,
hand-ported into `tan/planner/template.py` for the freshness-gate audit and
`tan.planner_cli --emit scaffold --cores`'s parity mirror). It is
DELIBERATELY a second, small, standalone implementation rather than a call
into `tan.planner.template.find_template_by_cores` -- same reasoning as
`unsupported_som` below: `tan.planner.paths` binds `REPO = sdk_root()` at
MODULE scope, so importing anything under `tan.planner` before
`bind_sdk_root` has run raises `PlannerRootError`, and `init_cmd.py`'s whole
SDK-free path (`tan init --template ...`, no checkout in sight, invariant
I-32) must keep working. The topology match itself is `json.loads(...)` plus
a list scan and a dict-equality check -- duplicating that one screen of pure
logic costs far less than a bind-then-deferred-import dance for every `tan
init` invocation, template-selecting or not.

**Distinct flag, distinct name, on purpose (tan-cli#996 review).** `tan init`
already has a `--cores` flag (`init_cmd.py`) that SPLICES a companion core
onto an already-chosen template's board.yaml -- a different feature entirely
from choosing WHICH template to use by its hardware topology. The customer
surface for this module's function is `--topology`, never `--cores`: the
same flag spelling meaning two different things depending on whether
`--template`/`--from-example` was also given would be a documentation
problem forever, not a one-time naming choice. See `init_cmd.py`'s `init()`
option help text for both flags' cross-reference to the other.

tan-cli#890. `tan init --from-example` copies an example tree and, when
`--som` is given, retargets the copied `board.yaml` onto that SKU. It never
consulted the catalog, so it answered the opposite of the SDK for the same
question:

    $ alp_project.py --emit scaffold --template multicore-mailbox --sku E1M-AEN301
    alp_project: multicore-mailbox: sku 'E1M-AEN301' is not supported
                 (supported: ['E1M-AEN801'])                            rc=1
    $ tan init --from-example multicore/mproc-mailbox --som E1M-AEN301
    exitCode 0 | ok True | issues 0

Measured worse than same-family: `--som E1M-V2N101` retargets that dual-M55
Alif example onto a Renesas part whose topology is `['a55_cluster', 'm33_sm']`
-- so the written `board.yaml` declares `m55_hp` and `m55_he`, neither of
which exists there. `tan validate` does refuse it a command later
(`ALP-B007`), but `init` reported `ok: true` with no issue at all, and an IDE
scaffolding through this path shows the customer green.

WHY THIS READS THE CATALOG DIRECTLY RATHER THAN THROUGH `tan.planner`
---------------------------------------------------------------------

`tan/planner/template.py` already has `load_catalog` and a
`SkuNotSupportedError`, and it is a hash-audited mirror of alp-sdk's
`scripts/alp_template.py` -- so it must not be edited here. Importing it is
also not free: `tan.planner.paths` binds `REPO = sdk_root()` at MODULE scope,
so any `tan.planner.*` import before `bind_sdk_root` raises

    PlannerRootError: tan.planner was imported before `bind_sdk_root(...)`;
    its metadata paths freeze at import time

(measured). Pulling that into `init_cmd` would either break every SDK-free
`tan init` path or need a bind-then-deferred-import dance, for what is
`json.loads(path.read_text())` plus a list scan. `load_catalog` is exactly
that one line, so nothing here duplicates planner LOGIC -- and the lookup
below is by `example` path, which the mirror does not do at all (it looks
records up by `id`).

WHY A WARNING AND NOT A REFUSAL
--------------------------------

This path has its own written precedent. `--from-example` used to REFUSE an
example with no `board.yaml`, and that "made `tan init --from-example`
unusable for nearly the whole AEN family, which is worse than the original
defect" -- so it warns and still writes every file
(`init.example-missing-board-yaml`). The same reasoning holds here:
`E1M-AEN301` really does carry both `m55_hp` and `m55_he`, so the scaffold it
would have produced is plausibly fine. What was wrong is that NOTHING
CHECKED, and that is what the warning fixes.
"""

from __future__ import annotations

import json
from pathlib import Path

#: The catalog, relative to an alp-sdk checkout root.
CATALOG_RELATIVE = Path("metadata") / "templates" / "catalog-v1.json"


def unsupported_som(sdk_root: Path, example_src: str, sku: str) -> tuple[str, ...] | None:
    """The catalog's `supported.som_skus` for `example_src`, when `sku` is
    NOT in it. `None` means "nothing to report" -- and that covers every
    reason, deliberately:

      * no catalog in this checkout (an older SDK; `--from-example` worked
        there before this gate and must keep working),
      * catalog present but unparseable, or not the shape expected,
      * no record for this example -- the COMMON case, not an edge one: the
        catalog declares 9 templates while `examples/aen/` alone holds 66
        directories, so an undeclared example has no support set to check
        and inventing one would be this defect pointed the other way,
      * a record with no `supported.som_skus`,
      * `sku` is in the set.

    Never raises: a scaffold must not fail because a catalog could not be
    read. The caller reports the returned set as a warning.
    """
    catalog = sdk_root / CATALOG_RELATIVE
    try:
        doc = json.loads(catalog.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(doc, dict):
        return None

    # The catalog spells it `examples/<src>`; `--from-example` takes `<src>`.
    # Compared with `/` separators on both sides -- `example_src` is already
    # validated as a plain relative path by the caller, and the catalog is
    # authored with forward slashes regardless of host.
    wanted = f"examples/{example_src.strip().strip('/')}"
    for record in doc.get("templates") or ():
        if not isinstance(record, dict):
            continue
        if str(record.get("example", "")).strip().strip("/") != wanted:
            continue
        supported = ((record.get("supported") or {}).get("som_skus")) or ()
        if not isinstance(supported, (list, tuple)) or not supported:
            return None
        if sku in supported:
            return None
        return tuple(str(s) for s in supported)
    return None


# ---------------------------------------------------------------------------
# `--topology`: select a catalog record BY its cores: hardware topology
# ---------------------------------------------------------------------------


class CoresTopologyError(Exception):
    """Base for `find_example_by_cores`'s two refusals. Never a bare
    ValueError/KeyError -- `init_cmd.py` catches this one type and turns it
    into an `InitError` with the right issue code for each case."""


class CoresTopologyNotFoundError(CoresTopologyError):
    """No catalog record's `cores:` topology exactly equals the requested
    one. `known_topologies` names every topology the catalog DOES offer, the
    same "never a bare error, always the candidates" spirit
    `AmbiguousCoresTopologyError` follows for its own case."""

    def __init__(self, cores: dict[str, str], known_topologies: list[tuple[tuple[str, str], ...]]):
        self.cores = cores
        self.known_topologies = known_topologies
        super().__init__(
            f"no catalog template with cores topology {cores!r} "
            f"(known topologies: {known_topologies})"
        )


class AmbiguousCoresTopologyError(CoresTopologyError):
    """More than one catalog record shares the requested `cores:` topology.

    The correct behaviour is to name every candidate and let the customer
    choose with `--template`/`--from-example` -- never to silently pick the
    first. This is NOT a hypothetical: measured against the live alp-sdk
    v0.16.0 catalog, the single-core `{"m55_hp": "zephyr"}` topology (the
    shape a single-M55 AEN scaffold is most likely to ask for) already
    matches five templates (`diagnostics`, `iot`, `minimal`, `peripheral`,
    `sensor`) -- see `tests/planner/test_find_template_by_cores.py::
    test_the_live_catalog_is_ambiguous_for_a_single_m55_hp_zephyr_topology`.
    """

    def __init__(self, cores: dict[str, str], candidate_ids: list[str]):
        self.cores = cores
        self.candidate_ids = candidate_ids
        super().__init__(
            f"cores topology {cores!r} matches multiple catalog templates "
            f"{candidate_ids} -- use --template or --from-example to "
            f"disambiguate"
        )


def find_example_by_cores(sdk_root: Path, cores: dict[str, str]) -> str:
    """The `--from-example`-compatible `src` (e.g. `multicore/mailbox`, no
    leading `examples/`) whose catalog record's `cores:` topology is EXACTLY
    `cores` -- `tan init --topology`'s resolution step.

    Raises `CoresTopologyNotFoundError` (no exact match) or
    `AmbiguousCoresTopologyError` (more than one) -- both refuse rather than
    guess, unlike `unsupported_som` above, which is a best-effort warning
    path. There is no project yet to scaffold without a real answer here:
    "which template" is not a fact `init` can degrade silently on the way
    "is this SoM supported" can.

    A catalog record with no declared `example` (schema requires it, so this
    is only reachable against a hand-edited or corrupted catalog) is
    excluded from the match set entirely rather than raising -- the same
    "cannot tell means silent" posture `unsupported_som` takes, scoped here
    to just that one record instead of the whole call.
    """
    catalog = sdk_root / CATALOG_RELATIVE
    doc = json.loads(catalog.read_text(encoding="utf-8"))
    records = [r for r in (doc.get("templates") or ()) if isinstance(r, dict)]

    def _topology(record: dict) -> dict[str, str]:
        return {c["id"]: c["os"] for c in record.get("cores") or ()}

    matches = [
        r for r in records
        if r.get("example") and _topology(r) == cores
    ]
    if not matches:
        known = sorted(
            {tuple(sorted(_topology(r).items())) for r in records}
        )
        raise CoresTopologyNotFoundError(cores, known)
    if len(matches) > 1:
        ids = sorted(str(r.get("id", "?")) for r in matches)
        raise AmbiguousCoresTopologyError(cores, ids)
    return str(matches[0]["example"]).strip().strip("/").removeprefix("examples/")


def parse_topology_arg(raw: str) -> dict[str, str]:
    """`--topology core_id:os[,core_id:os...]` -- STRICT `id:os` pairs, no OS
    inference (unlike `--cores`' splice parser, `parse_cores`): a selector
    must match a catalog record's topology exactly, so an inferred OS could
    silently look up a topology the caller never actually typed. Mirrors
    alp-sdk's `alp_project._parse_cores_arg` (the same parser
    `find_template_by_cores`'s own CLI front door uses).

    Raises `ValueError` on any malformed entry -- the caller wraps this in
    an `InitError` (`init.invalid-topology`).
    """
    cores: dict[str, str] = {}
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        if ":" not in entry:
            raise ValueError(
                f"--topology entry {entry!r} must be core_id:os (e.g. "
                f"m55_hp:zephyr)"
            )
        core_id, _, os_name = entry.partition(":")
        core_id, os_name = core_id.strip(), os_name.strip()
        if not core_id or not os_name:
            raise ValueError(
                f"--topology entry {entry!r} must be core_id:os (e.g. "
                f"m55_hp:zephyr)"
            )
        if core_id in cores:
            raise ValueError(f"--topology names {core_id!r} more than once")
        cores[core_id] = os_name
    if not cores:
        raise ValueError("--topology must name at least one core_id:os pair")
    return cores
