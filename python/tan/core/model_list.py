# SPDX-License-Identifier: Apache-2.0
"""Pure engine for `tan model list` (tan-cli#674): what `board.yaml` declares
under `models:`, next to what `--out` already holds for each one -- read-only,
no SDK metadata consulted at all. Unlike `build`/`check`, nothing this reports
comes out of `metadata/**`: there is no compile target to resolve and no NPU
support table to read, only a board.yaml already in hand and a directory to
`stat()`. `tan.commands.model_cmd` does the board.yaml IO/validation shared
with `build`/`check` (`_load_board`/`_require_models_list`/
`_require_model_entry`); this module is the per-model shaping, mirroring the
`tan.core.model_check`/`tan.core.model_doctor` split (the engine computes,
this renders/serialises).

The one piece of real IO here -- reading a package's own manifest back off
disk (`tan.model.package.read_manifest_file`) to report its size and whether
it is stale -- is itself read-only and already the pattern `model_cmd.
_shipped_caveat_issues` uses for the same reason: describe the ARTIFACT, not
what a build call happened to hold in memory.

A readback failure there (a corrupt/truncated package, or a `source` that no
longer resolves) is reported the SAME way `_shipped_caveat_issues` reports its
own readback failure: a `model.artifact-stale-unknown` WARNING `Issue`, never
silent. Silence would be indistinguishable from "not stale" -- a `board.yaml`
naming a `source` that no longer exists, with a perfectly good package already
on disk, used to answer `{"exists": true, "bytes": N}` with no sign the
staleness check never ran at all (tan-cli#674 review MAJOR 1). The row itself
still degrades to the bare `exists: True` shape (see `_artifact_status`): the
package IS there, and downgrading that to a failure over a diagnostic
comparison would be the wrong trade -- exactly `_shipped_caveat_issues`'s own
reasoning, applied to a second readback.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from tan.envelope import Issue
from tan.model.package import read_manifest_file


def declared_sku(board_doc: dict) -> str | None:
    """`board.yaml`'s `som.sku`, or `None`. Unlike `build`/`check`'s
    `_require_sku`, a missing or malformed SKU does not refuse `list` --
    naming what is declared and what is built needs no real SoM at all, so
    this degrades instead of raising."""
    som = board_doc.get("som")
    sku = som.get("sku") if isinstance(som, dict) else None
    return sku if isinstance(sku, str) and sku else None


def _artifact_status(
    name: str, out_path: Path, source: Path
) -> tuple[dict[str, Any], Issue | None]:
    """`({"exists": False}, None)` for a package `tan model build` has not
    written (or no longer holds); `({"exists": True, "bytes", "stale"}, None)`
    for one that is on disk and whose staleness could be determined. `stale`
    is `True` when the package's own recorded `src_sha` (read back from the
    FILE, not a cached value) no longer matches the CURRENT `source` file's
    hash -- i.e. the model was edited since the last `tan model build`.
    `bytes`/`stale` are both best-effort: a corrupt/truncated package or a
    `source` that no longer resolves degrades to the bare `exists: True` row
    rather than refusing the whole list -- staleness is a bonus fact about a
    build that DID succeed, not a reason to hide that it exists.

    That degradation is never silent, though (tan-cli#674 review MAJOR 1): the
    second element is a `model.artifact-stale-unknown` WARNING `Issue` whenever
    the readback failed, `None` when it did not need to fall back at all --
    the caller (`list_entry`) collects it into the envelope's `issues` rather
    than reporting a clean row with no sign anything failed."""
    if not out_path.is_file():
        return {"exists": False}, None
    status: dict[str, Any] = {"exists": True, "bytes": out_path.stat().st_size}
    try:
        manifest = read_manifest_file(out_path)
        current_sha = hashlib.sha256(source.read_bytes()).digest()
    except (OSError, ValueError) as err:
        return status, Issue(
            "model.artifact-stale-unknown",
            "warning",
            f"model '{name}': found {out_path} but could not confirm whether "
            f"it is stale against {source}: {type(err).__name__}: {err}",
        )
    status["stale"] = manifest.src_sha != current_sha
    return status, None


def list_entry(name: str, source: Path, out_dir: Path) -> tuple[dict[str, Any], Issue | None]:
    """One declared model's `list` row: its name, resolved source path, and
    the artifact `tan model build` would write for it under `out_dir` -- the
    SAME `{name}.alpmodel` naming `build_model` uses (`tan/model/build.py`),
    read here, never compiled. The second element is `_artifact_status`'s own
    readback-failure `Issue`, or `None`."""
    artifact, issue = _artifact_status(name, out_dir / f"{name}.alpmodel", source)
    return {"name": name, "source": str(source), "artifact": artifact}, issue


def render_list_text(data: dict[str, Any]) -> list[str]:
    """Every declared model's line, in board.yaml order -- `[]` for no
    declared models, the same split `build`/`check`'s own text branches make
    between "no lines" and "needs a filler saying so", decided by the caller
    (`model_cmd.finish`)."""
    lines: list[str] = []
    for m in data.get("models") or []:
        artifact = m.get("artifact") or {}
        if not artifact.get("exists"):
            lines.append(f"{m['name']}: not built ({m['source']})")
            continue
        size = artifact.get("bytes")
        size_note = f", {size} bytes" if size is not None else ""
        stale_note = ", STALE (source changed since last build)" if artifact.get("stale") else ""
        lines.append(f"{m['name']}: built{size_note}{stale_note}")
    return lines
