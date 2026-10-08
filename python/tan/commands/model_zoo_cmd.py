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
import os
from pathlib import Path
from typing import Any

from tan.commands.build_output import ProjectContext
from tan.core.atomic_write import atomic_write_bytes
from tan.core.publish import publish_exclusive as _publish
from tan.core.board_yaml_edit import BoardEditRefused, append_models_entry
from tan.core.model_zoo import (
    MIN_SDK_COMMIT,
    ZooEntry,
    StagedModel,
    bundled_chunks,
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
    return {"schemaVersion": ZOO_DATA_SCHEMA_VERSION, "sku": None, "boardSku": None, "entries": []}


def add_empty_data() -> dict[str, Any]:
    return {"schemaVersion": ADD_DATA_SCHEMA_VERSION, "sku": None, "added": None, "addedEntry": None}


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


def tolerant_board_sku(board_path: Path) -> str | None:
    """`som.sku` of `board_path`, or `None` for ANY problem (absent, unreadable,
    not YAML): `zoo` is a browse command and never refuses over the board."""
    import yaml  # noqa: PLC0415 (declared dependency)

    try:
        doc = yaml.safe_load(board_path.read_bytes().decode("utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError, RecursionError):
        return None
    som = doc.get("som") if isinstance(doc, dict) else None
    sku = som.get("sku") if isinstance(som, dict) else None
    return sku if isinstance(sku, str) and sku else None


def run_zoo(*, context: ProjectContext, zoo_dir: Path, sku: str | None) -> Result:
    """Each row carries `runsHere`: whether the entry is bench-validated on the
    effective SKU (`--sku`, else the project board's `som.sku`); `null` when
    neither names one."""
    project, sdk = context.project(), context.sdk
    board_sku = tolerant_board_sku(Path(context.board_yaml))
    data = zoo_empty_data()
    data["sku"] = sku
    data["boardSku"] = board_sku
    loaded = _load(zoo_dir)
    if isinstance(loaded, Issue):
        return project, sdk, data, [loaded], ExitCode.VALIDATION_FAILURE
    entries, issues = loaded
    if sku:
        entries = filter_by_sku(entries, sku)
    effective = sku or board_sku
    rows = []
    for e in entries:
        row = entry_as_dict(e)
        row["runsHere"] = None if effective is None else effective in e.validated_soms
        rows.append(row)
    data["entries"] = rows
    return project, sdk, data, issues, ExitCode.SUCCESS


def _is_orphan_of(entry: ZooEntry, zoo_dir: Path, dest: Path) -> StagedModel | None:
    """`dest` as a `StagedModel` when it is a plain file already holding exactly
    this entry's bytes -- what a run interrupted between publishing the model
    and writing `board.yaml` leaves behind -- so a re-run can finish the job
    instead of refusing. A symlink, or any other content, is never adopted."""
    if dest.is_symlink() or not dest.is_file():
        return None
    try:
        got = hashlib.sha256()
        with dest.open("rb") as handle:
            while chunk := handle.read(1 << 20):
                got.update(chunk)
        want = entry.source.get("sha256")
        if want is None:
            bundled = hashlib.sha256()
            for chunk in bundled_chunks(zoo_dir, entry.source["bundled"]):
                bundled.update(chunk)
            want = bundled.hexdigest()
        return StagedModel(dest, got.hexdigest(), dest.stat().st_size) if got.hexdigest() == want else None
    except (OSError, ZooFetchError):
        return None


def _precheck(
    model_id: str,
    entry: ZooEntry | None,
    entries: list[ZooEntry],
    existing_names: set[str],
    dest: Path,
    adopt: bool = False,
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
    if os.path.lexists(dest) and not adopt:
        return Issue(
            "model.add-destination-exists",
            "error",
            f"{dest} already exists; move it aside first. Nothing was changed.",
        )
    return _unsafe_models_dir(dest.parent)


def _unsafe_models_dir(models_dir: Path) -> Issue | None:
    """Refuse a `models/` that is a symlink or resolves outside the project
    (a write through it would land elsewhere)."""
    if not os.path.lexists(models_dir):
        return None
    board_dir = models_dir.parent.resolve()
    if models_dir.is_symlink() or not models_dir.is_dir() or models_dir.resolve().parent != board_dir:
        return Issue(
            "model.add-destination-unsafe",
            "error",
            f"{models_dir} is not a plain directory inside the project; nothing was changed.",
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


def _added_row(entry: ZooEntry, dest: Path, rel_source: str, staged: StagedModel) -> dict[str, Any]:
    return {
        "id": entry.id,
        "name": entry.id,
        "source": rel_source,
        "path": dest.as_posix(),
        "sha256": staged.sha256,
        "bytes": staged.size,
        "kind": entry.kind,
        "license": entry.license,
    }


def _plan_edit(entry: ZooEntry, board_path: Path) -> tuple[str, str] | Issue:
    """`(new board.yaml text, relative source)` -- computed before any fetch so
    a refused edit costs nothing."""
    rel_source = f"{MODELS_DIR}/{entry.id}{entry.suffix}"
    new_entry: dict[str, Any] = {"name": entry.id, "source": rel_source}
    if entry.compile:
        new_entry["compile"] = entry.compile
    try:
        return append_models_entry(board_path.read_bytes().decode("utf-8"), new_entry), rel_source
    except (OSError, UnicodeDecodeError) as err:
        return Issue("model.board-yaml-edit-failed", "error", f"{board_path}: {err}")
    except BoardEditRefused as err:
        return Issue(
            "model.board-yaml-edit-refused",
            "error",
            f"Not editing {board_path}: {err}. Nothing was changed; add this entry by hand: "
            f"name: {entry.id}, source: {rel_source}.",
        )


def _commit_add(
    entry: ZooEntry,
    zoo_dir: Path,
    dest: Path,
    board_path: Path,
    new_text: str,
    reader: Any,
    adopted: StagedModel | None = None,
) -> StagedModel | Issue:
    """Stage (stream + verify) into a temp file beside `dest`, publish it
    without overwriting, then write `board.yaml` last. Any failure removes
    everything this run created, including a `models/` it made."""
    models_dir = dest.parent
    created_dir = not os.path.lexists(models_dir)
    staged: StagedModel | None = None
    published = False
    try:
        if adopted is not None:
            atomic_write_bytes(str(board_path), new_text.encode("utf-8"))
            return adopted
        if created_dir:
            models_dir.mkdir()
        staged = fetch_source(entry, zoo_dir, tmp_dir=models_dir, **({"reader": reader} if reader else {}))
        _publish(staged.path, dest)
        published = True
        try:
            staged.path.unlink(missing_ok=True)
        except OSError:
            pass  # the model is published; a leftover temp name is harmless
        atomic_write_bytes(str(board_path), new_text.encode("utf-8"))
        return staged
    except ZooFetchError as err:
        if err.mismatch:
            failure = Issue("model.zoo-integrity-failed", "error", str(err))
        else:
            failure = Issue("model.zoo-fetch-failed", "error", str(err))
    except FileExistsError:
        failure = Issue("model.add-destination-exists", "error", f"{dest} already exists; nothing was changed.")
    except OSError as err:
        if published or adopted is not None:
            failure = Issue("model.board-yaml-edit-failed", "error", f"{board_path}: {err}")
        else:
            failure = Issue("model.add-write-failed", "error", f"{dest}: {err}")
    if published:
        dest.unlink(missing_ok=True)
    if staged is not None:
        staged.path.unlink(missing_ok=True)
    if created_dir:
        try:
            models_dir.rmdir()
        except OSError:
            pass
    return failure


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
    adopted = _is_orphan_of(entry, zoo_dir, dest) if entry and entry.id not in existing_names else None
    refusal = _precheck(model_id, entry, entries, existing_names, dest, adopt=adopted is not None)
    if refusal is not None:
        return refuse(refusal, ExitCode.VALIDATION_FAILURE, issues)
    assert entry is not None
    issues.extend(w for w in [_sku_warning(entry, sku)] if w is not None)

    plan = _plan_edit(entry, board_path)
    if isinstance(plan, Issue):
        refused = plan.code == "model.board-yaml-edit-refused"
        return refuse(plan, ExitCode.VALIDATION_FAILURE if refused else ExitCode.RUNTIME_FAILURE, issues)
    new_text, rel_source = plan
    staged = _commit_add(entry, zoo_dir, dest, board_path, new_text, reader, adopted)
    if isinstance(staged, Issue):
        exit_code = ExitCode.VALIDATION_FAILURE if staged.code == "model.add-destination-exists" else ExitCode.RUNTIME_FAILURE
        return refuse(staged, exit_code, issues)
    data["added"] = entry.id
    data["addedEntry"] = _added_row(entry, dest, rel_source, staged)
    return project, sdk, data, issues, ExitCode.SUCCESS


def render_zoo_text(data: dict) -> list[str]:
    lines = []
    for e in data.get("entries", []):
        soms = ",".join(e["validatedSoms"]) or "none"
        here = {True: "  (runs on this board)", False: "", None: ""}[e.get("runsHere")]
        lines.append(f"{e['id']}  [{e['kind']}] {e['task']}  {e['license']}  validated: {soms}{here}")
        lines.append(f"    {e['description']}")
    return lines


def render_add_text(data: dict) -> list[str]:
    a = data.get("addedEntry")
    if not a:
        return []
    return [
        f"added {a['name']}: {a['source']} ({a['bytes']} bytes, sha256 {a['sha256']})",
        f"board.yaml: appended `{a['name']}` to models:",
    ]
