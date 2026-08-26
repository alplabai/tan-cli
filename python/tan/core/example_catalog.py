# SPDX-License-Identifier: Apache-2.0
"""Read the SDK scaffold catalog's declared SoM support for ONE example.

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
