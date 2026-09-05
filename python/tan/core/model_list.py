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
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from tan.model.package import read_manifest_file


def declared_sku(board_doc: dict) -> str | None:
    """`board.yaml`'s `som.sku`, or `None`. Unlike `build`/`check`'s
    `_require_sku`, a missing or malformed SKU does not refuse `list` --
    naming what is declared and what is built needs no real SoM at all, so
    this degrades instead of raising."""
    som = board_doc.get("som")
    sku = som.get("sku") if isinstance(som, dict) else None
    return sku if isinstance(sku, str) and sku else None


def _artifact_status(out_path: Path, source: Path) -> dict[str, Any]:
    """`{"exists": False}` for a package `tan model build` has not written (or
    no longer holds); `{"exists": True, "bytes", "stale"}` for one that is on
    disk. `stale` is `True` when the package's own recorded `src_sha` (read
    back from the FILE, not a cached value) no longer matches the CURRENT
    `source` file's hash -- i.e. the model was edited since the last `tan
    model build`. `bytes`/`stale` are both best-effort: a corrupt/truncated
    package or a `source` that no longer resolves degrades to the bare
    `exists: True` row rather than refusing the whole list -- staleness is a
    bonus fact about a build that DID succeed, not a reason to hide that it
    exists."""
    if not out_path.is_file():
        return {"exists": False}
    status: dict[str, Any] = {"exists": True, "bytes": out_path.stat().st_size}
    try:
        manifest = read_manifest_file(out_path)
        current_sha = hashlib.sha256(source.read_bytes()).digest()
    except (OSError, ValueError):
        return status
    status["stale"] = manifest.src_sha != current_sha
    return status


def list_entry(name: str, source: Path, out_dir: Path) -> dict[str, Any]:
    """One declared model's `list` row: its name, resolved source path, and
    the artifact `tan model build` would write for it under `out_dir` -- the
    SAME `{name}.alpmodel` naming `build_model` uses (`tan/model/build.py`),
    read here, never compiled."""
    return {
        "name": name,
        "source": str(source),
        "artifact": _artifact_status(out_dir / f"{name}.alpmodel", source),
    }


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
