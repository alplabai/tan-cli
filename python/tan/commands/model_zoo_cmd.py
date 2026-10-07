# SPDX-License-Identifier: Apache-2.0
"""`tan model zoo` and `tan model add <id>` (tan-cli#1286) -- the runners and
text renderers, kept out of `model_cmd.py` (module-size budget); that file
still owns the typer surface, the SDK-root/board.yaml preamble and `finish()`.

**`zoo [--sku SKU]`** lists the entries in the bound SDK's
`metadata/model_zoo/`, optionally only those bench-validated on `SKU` (an empty
`validated_soms` matches nothing -- `tan.core.model_zoo`). Read-only.

**`add <id>`** copies one entry into the project: it fetches the model (a
bundled starter, or a download verified against the manifest's sha256), writes
it to `models/<id><ext>` beside `board.yaml`, and appends
`{name: <id>, source: models/<id><ext>[, compile: ...]}` to `models:` with
`tan.core.board_yaml_edit` -- a text splice that keeps every other byte of the
file. Everything is computed before anything is written (so a refused edit
leaves no stray model file), the model lands first and `board.yaml` last, and
a failed `board.yaml` write removes the model file it just created. A name
already in `models:` and an existing destination file are refused, never
overwritten.

Refusals are plain `Issue(...)` returns, not exceptions, so the codes stay
visible to the issue-code registry gate.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from tan.commands.build_output import ProjectContext
from tan.core.atomic_write import atomic_write_bytes
from tan.core.board_yaml_edit import BoardEditRefused, append_models_entry
from tan.core.model_zoo import (
    MIN_SDK_COMMIT,
    ZooEntry,
    ZooFetchError,
    ZooUnavailable,
    entry_as_dict,
    fetch_source,
    filter_by_sku,
    load_zoo,
)
from tan.envelope import Issue, Project, SdkInfo
from tan.exit_codes import ExitCode

ZOO_DATA_SCHEMA_VERSION = "1"
ADD_DATA_SCHEMA_VERSION = "1"

#: Directory (beside `board.yaml`) `add` puts the fetched model in.
MODELS_DIR = "models"

Result = tuple[Project, SdkInfo | None, dict, list[Issue], ExitCode]


def zoo_empty_data() -> dict[str, Any]:
    return {"schemaVersion": ZOO_DATA_SCHEMA_VERSION, "sku": None, "entries": []}


def add_empty_data() -> dict[str, Any]:
    return {"schemaVersion": ADD_DATA_SCHEMA_VERSION, "sku": None, "added": None}


def _load(zoo_dir: Path) -> tuple[list[ZooEntry], list[Issue]] | Issue:
    """The loaded entries plus a warning per skipped manifest, or the
    `model.zoo-unavailable` refusal when the SDK predates the zoo."""
    try:
        entries, problems = load_zoo(zoo_dir)
    except ZooUnavailable:
        return Issue(
            "model.zoo-unavailable",
            "error",
            f"The bound alp-sdk has no metadata/model_zoo/ ({zoo_dir}). The model zoo needs "
            f"alp-sdk commit {MIN_SDK_COMMIT} (alp-sdk#2542) or newer; point --sdk-root at "
            "such a checkout.",
        )
    return entries, [
        Issue("model.zoo-entry-invalid", "warning", f"Skipped zoo entry {name}: {why}.")
        for name, why in problems
    ]


def run_zoo(*, context: ProjectContext, zoo_dir: Path, sku: str | None) -> Result:
    project, sdk = context.project(), context.sdk
    data = zoo_empty_data()
    data["sku"] = sku
    loaded = _load(zoo_dir)
    if isinstance(loaded, Issue):
        return project, sdk, data, [loaded], ExitCode.VALIDATION_FAILURE
    entries, issues = loaded
    if sku:
        entries = filter_by_sku(entries, sku)
    data["entries"] = [entry_as_dict(e) for e in entries]
    return project, sdk, data, issues, ExitCode.SUCCESS


def _stage_add(
    entry: ZooEntry, zoo_dir: Path, board_path: Path, reader: Any
) -> tuple[bytes, str, str] | Issue:
    """Everything `add` can compute without touching disk: `(model bytes,
    new board.yaml text, relative source)`, or the refusing `Issue`."""
    rel_source = f"{MODELS_DIR}/{entry.id}{entry.suffix}"
    try:
        blob = fetch_source(entry, zoo_dir, **({"reader": reader} if reader else {}))
    except ZooFetchError as err:
        return Issue("model.zoo-fetch-failed", "error", str(err))
    new_entry: dict[str, Any] = {"name": entry.id, "source": rel_source}
    if entry.compile:
        new_entry["compile"] = entry.compile
    try:
        new_text = append_models_entry(board_path.read_bytes().decode("utf-8"), new_entry)
    except (OSError, UnicodeDecodeError) as err:
        return Issue("model.board-yaml-edit-failed", "error", f"{board_path}: {err}")
    except BoardEditRefused as err:
        return Issue(
            "model.board-yaml-edit-refused",
            "error",
            f"Not editing {board_path}: {err}. Nothing was changed; add this entry by hand: "
            f"name: {entry.id}, source: {rel_source}.",
        )
    return blob, new_text, rel_source


def _commit_add(dest: Path, blob: bytes, board_path: Path, new_text: str) -> str | None:
    """Model file first, `board.yaml` last; a failed `board.yaml` write
    removes the model file just created. An error message, or `None`."""
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_bytes(str(dest), blob)
    except OSError as err:
        return f"{dest}: {err}"
    try:
        atomic_write_bytes(str(board_path), new_text.encode("utf-8"))
    except OSError as err:
        dest.unlink(missing_ok=True)
        return f"{board_path}: {err}"
    return None


def _precheck(
    model_id: str, entry: ZooEntry | None, entries: list[ZooEntry], existing_names: set[str], dest: Path
) -> Issue | None:
    """The refusal `add` must make before fetching anything, or `None`."""
    if entry is None:
        known = ", ".join(e.id for e in entries) or "(none)"
        return Issue(
            "model.zoo-entry-not-found",
            "error",
            f"No model-zoo entry named {model_id}. Available: {known}.",
        )
    if entry.id in existing_names:
        return Issue(
            "model.add-name-exists",
            "error",
            f"board.yaml already declares a model named {entry.id}; nothing was changed.",
        )
    if dest.exists():
        return Issue(
            "model.add-destination-exists",
            "error",
            f"{dest} already exists; move it aside first. Nothing was changed.",
        )
    return None


def _sku_warning(entry: ZooEntry, sku: str | None) -> Issue | None:
    """The hardware-claim caveat for a `kind: model` entry not validated on `sku`."""
    if entry.kind != "model" or not sku or sku in entry.validated_soms:
        return None
    return Issue(
        "model.add-sku-not-validated",
        "warning",
        f"{entry.id} has not been bench-validated on {sku} "
        f"(validated: {', '.join(entry.validated_soms) or 'none'}).",
    )


def _added_row(entry: ZooEntry, dest: Path, rel_source: str, blob: bytes) -> dict[str, Any]:
    return {
        "id": entry.id,
        "name": entry.id,
        "source": rel_source,
        "path": dest.as_posix(),
        "sha256": hashlib.sha256(blob).hexdigest(),
        "bytes": len(blob),
        "kind": entry.kind,
        "license": entry.license,
    }


def run_add(
    *,
    context: ProjectContext,
    zoo_dir: Path,
    model_id: str | None,
    sku: str | None,
    existing_names: set[str],
    reader: Any = None,
) -> Result:
    project, sdk = context.project(), context.sdk
    data = add_empty_data()
    data["sku"] = sku

    def refuse(issue: Issue, exit_code: ExitCode, issues: list[Issue]) -> Result:
        return project, sdk, data, [*issues, issue], exit_code

    if not model_id:
        missing = Issue(
            "model.add-id-missing",
            "error",
            "`tan model add` needs a zoo entry id; list them with `tan model zoo`.",
        )
        return refuse(missing, ExitCode.VALIDATION_FAILURE, [])
    loaded = _load(zoo_dir)
    if isinstance(loaded, Issue):
        return project, sdk, data, [loaded], ExitCode.VALIDATION_FAILURE
    entries, issues = loaded
    entry = next((e for e in entries if e.id == model_id), None)
    board_path = Path(context.board_yaml)
    dest = board_path.parent / MODELS_DIR / f"{model_id}{entry.suffix if entry else ''}"
    refusal = _precheck(model_id, entry, entries, existing_names, dest)
    if refusal is not None:
        return refuse(refusal, ExitCode.VALIDATION_FAILURE, issues)
    assert entry is not None
    issues.extend(w for w in [_sku_warning(entry, sku)] if w is not None)
    staged = _stage_add(entry, zoo_dir, board_path, reader)
    if isinstance(staged, Issue):
        refused = staged.code == "model.board-yaml-edit-refused"
        return refuse(
            staged, ExitCode.VALIDATION_FAILURE if refused else ExitCode.RUNTIME_FAILURE, issues
        )
    blob, new_text, rel_source = staged
    failure = _commit_add(dest, blob, board_path, new_text)
    if failure is not None:
        failed = Issue("model.board-yaml-edit-failed", "error", failure)
        return refuse(failed, ExitCode.RUNTIME_FAILURE, issues)
    data["added"] = _added_row(entry, dest, rel_source, blob)
    return project, sdk, data, issues, ExitCode.SUCCESS


def render_zoo_text(data: dict) -> list[str]:
    lines = []
    for e in data.get("entries", []):
        soms = ",".join(e["validatedSoms"]) or "none"
        lines.append(f"{e['id']}  [{e['kind']}] {e['task']}  {e['license']}  validated: {soms}")
        lines.append(f"    {e['description']}")
    return lines


def render_add_text(data: dict) -> list[str]:
    a = data.get("added")
    if not a:
        return []
    return [
        f"added {a['name']}: {a['source']} ({a['bytes']} bytes, sha256 {a['sha256']})",
        f"board.yaml: appended `{a['name']}` to models:",
    ]
