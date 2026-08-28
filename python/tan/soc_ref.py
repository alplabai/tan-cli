# SPDX-License-Identifier: Apache-2.0
"""Pure `silicon:` ref -> SoC-JSON path resolution.

A leaf module: no imports of `tan.planner` or anything else that reads real
files at import time. `tan.planner.som_metadata` imports `resolve_soc_path`
from here and re-exports it (its own four call sites keep working
unchanged); `tan.model.targets` imports it from here directly so that
`tan.model` stays importable without a bound alp-sdk checkout -- importing
`tan.planner` pulls in `tan/planner/__init__.py`, which transitively reads
real `metadata/registries/*` content at import time (`tan/planner/slugs.py`'s
module-scope `_PERIPHERAL_KCONFIG = peripheral_kconfig()`), a dependency
`tan.model` never had in alp-sdk and should not gain here.

One definition, re-exported -- alp-sdk spent issues #997/#1004/#1096
collapsing this resolution to a single source; do not create a second copy.
"""
from __future__ import annotations

from pathlib import Path


def resolve_soc_path(silicon: str | None, metadata_root: Path) -> Path | None:
    """Resolve a `vendor:family:part` `silicon:` key to the SoC-JSON path
    it names: `metadata/socs/<vendor>/<family>/<part>.json`.

    Returns None when `silicon` is falsy or not exactly 3 colon-separated
    parts -- does NOT check the path exists, callers decide what an
    unresolved/missing SoC spec means for them (e.g. a SoM preset with no
    `silicon:` at all is a valid, if incomplete, state; a `silicon:` that
    names a spec that isn't on disk is not).

    alp-sdk keeps its own copies of this resolution behind differing failure
    shapes (`OrchestratorError`, `ZephyrBoardEmitError`, a diagnostic list);
    `loader._silicon_to_soc_path` is the planner's raising variant. This is the
    soft-fail one `tan.planner.som_metadata` and `tan.model.targets` share.
    """
    if not isinstance(silicon, str) or not silicon:
        # A non-string `silicon:` (e.g. a hand-authored `silicon: 7`) is
        # "not exactly 3 colon-separated parts" exactly as much as a falsy
        # one is -- the docstring above already promises None for that
        # shape, `silicon.split(":")` below just didn't deliver on it
        # (`AttributeError: 'int' object has no attribute 'split'`,
        # tan-cli#967 review). This is a SHARED leaf (re-exported by
        # `tan.planner.som_metadata`, imported directly by
        # `tan.model.targets`) -- fixing it here, once, fixes every caller
        # identically rather than needing a matching guard at each call
        # site. Every existing caller already treats a None return as
        # "unresolved" and reports it through its own error message
        # (`_load_soc_spec` -> `ZephyrBoardEmitError("... is not a
        # triple-colon string")`, `resolve_targets` -> `ValueError`), so
        # this changes a crash into the SAME clean diagnostic those callers
        # already produce for a malformed-but-string ref, not a new
        # behaviour.
        return None
    parts = silicon.split(":")
    if len(parts) != 3:
        return None
    return metadata_root / "socs" / parts[0] / parts[1] / f"{parts[2]}.json"
