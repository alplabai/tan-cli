# SPDX-License-Identifier: Apache-2.0
"""SKU / board-preset / pad-route resolution for the `alp_project.py` emit modes.

RELOCATED (was alp-sdk `scripts/alp_project_loader.py`). Only the part the six
relocated `alp_project.py`-owned emit modes actually reach came across --
`dts-overlay`, `native-sim-overlay`, `hw-info-h`, `west-libraries`,
`carrier-netlist` and `zephyr-board`. Everything else that file held was already
here: `_sku_family`, `silicon_to_kconfig`, `resolve_soc_path`,
`_resolve_silicon_variant`, `resolve_memory_map`, `resolve_capabilities` and
`som_unpopulated_capabilities` live in `som_metadata.py`, and `_load_yaml` in
`loader.py`. Copying them again would be a second source of truth for facts the
planner already reads once.

Three mechanical changes, no behavioural one on the success path -- which is the
path byte-identity is measured on
(`tests/parity/test_planner_emit_parity.py`):

* **`sys.exit` became `raise OrchestratorError`.** alp-sdk's copy ran inside a
  spawned interpreter, where exiting was a non-zero rc the caller reported. In
  `tan`'s own process a `sys.exit` is a `SystemExit` that tears down the CLI past
  every envelope guarantee -- an unwritten stdout is rendered as nothing at all
  by the extension. Same message text, minus the `alp_project: ` prefix the
  spawned CLI added.
* **`REPO` is the BOUND SDK checkout** (`paths.REPO`), not a `__file__` walk. The
  walk landed on the SDK because the file lived there; inside `tan` it would land
  on the `tan` package and every `metadata/**` path would be wrong.
* **`_validate_and_load` did NOT come across.** It ran alp-sdk's rich diagnostic
  validator (`alp_cli.validator`) before handing the emitters a plain dict --
  importing `scripts/alp_cli` is exactly the edge this relocation removes. What
  the emitters consume is the plain dict, and alp-sdk's own code already has a
  branch for producing it without `alp_cli` (that function's `ImportError`
  fallback is `_load_yaml`). `load_board_yaml` -- which every caller here runs
  first -- validates the same file against `metadata/schemas/board.schema.json`,
  so nothing goes unvalidated; what is lost is only the rendered-diagnostic
  presentation, which `tan validate` owns on this side.

  One accidental improvement rides along: alp-sdk's `_validate_and_load` re-read
  the file with a bare `path.read_text()`, i.e. in the host's locale encoding, so
  a non-ASCII board.yaml decoded as cp1252 on Windows. `loader._load_yaml` reads
  UTF-8 explicitly.

A fourth change landed later and IS behavioural on a refusal path: alp-sdk
#1025 fixed `_hwrev_pad_route_overrides` to raise on an `hw_rev` absent from
the resolved `hw_revisions:` table -- ported here in the same shape (see that
function's own docstring). Before it, an unrecognised `hw_rev` silently
resolved to base-revision pad routing with a clean exit code on this file's
two emit modes (`carrier-netlist` / `composed-route-table`), which never run
`loader.load_board_yaml` and so never saw that fix's other half.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import sdk_compat
from .loader import _load_yaml
from .models import OrchestratorError, SdkRevisionNotBuildable, SdkRevisionUnknown
from .paths import METADATA_ROOT, REPO
from .som_metadata import _sku_family

__all__ = [
    "_compose_route",
    "_hwrev_pad_route_overrides",
    "_resolve_board",
    "_resolve_inline_or_preset_board",
    "_resolve_pad_routes",
    "_resolve_sku",
    "_sku_form_factor",
]


def _sku_form_factor(sku: str) -> str:
    """Return the E1M connector family for a SKU string."""
    return "e1m-x" if _sku_family(sku) in {"v2n", "v2n-m1"} else "e1m"


def _resolve_sku(sku: str, metadata_root: Path = METADATA_ROOT) -> dict[str, Any]:
    # Per-SKU preset lives at metadata/e1m_modules/<SKU>.yaml.
    preset_path = metadata_root / "e1m_modules" / f"{sku}.yaml"
    if not preset_path.is_file():
        raise OrchestratorError(
            f"no preset for SKU {sku} at "
            f"{preset_path.relative_to(REPO) if preset_path.is_relative_to(REPO) else preset_path} "
            f"(remaining SKUs land alongside the user-supplied HW config writeup)"
        )
    return _load_yaml(preset_path)


def _resolve_board(preset: str, metadata_root: Path = METADATA_ROOT) -> dict[str, Any]:
    # Shared board definition lives at metadata/boards/<preset>.yaml.
    preset_path = metadata_root / "boards" / f"{preset}.yaml"
    if not preset_path.is_file():
        raise OrchestratorError(
            f"`preset: {preset}` does not resolve at "
            f"{preset_path.relative_to(REPO) if preset_path.is_relative_to(REPO) else preset_path}")
    return _load_yaml(preset_path)


def _resolve_inline_or_preset_board(
    project: dict[str, Any], metadata_root: Path = METADATA_ROOT,
) -> dict[str, Any]:
    """Return the resolved board dict for a project.

    Mirrors the orchestrator's resolution: `preset: <name>` loads
    metadata/boards/<name>.yaml; otherwise the project's own
    top-level `name`/`populated`/`e1m_routes` are wrapped into the
    same shape the downstream emitters consume.
    """
    if "preset" in project:
        return _resolve_board(project["preset"], metadata_root)
    return {
        "name":           project.get("name"),
        "populated":      dict(project.get("populated") or {}),
        "e1m_routes":     dict(project.get("e1m_routes") or {}),
        "default_hw_rev": project.get("hw_rev"),
    }


def _resolve_pad_routes(
    sku_preset: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    """Index a SoM preset's `pad_routes:` block by E1M pad/instance.

    Returns a dict keyed by `E1M_*` identifier (e.g., `E1M_GPIO_IO15`,
    `E1M_SPI1`) with the full route entry as value:

        { "dispatch": "...", "dispatch_pin": 14, "doc": "..." }

    Pads NOT present in the preset's `pad_routes:` (or absent block
    entirely) are implicitly `direct` -- they route to the main
    silicon's GPIO / peripheral with no mediator.

    The SDK's codegen layer composes this with the board preset's
    `e1m_routes:` block: when an E1M pad appears in both, the board
    supplies the role and the SoM supplies the dispatch path. The two
    blocks together let a customer swap SoMs without touching the board
    YAML or any app source -- the som-swappable-without-board-changes
    promise.
    """
    routes = sku_preset.get("pad_routes") or []
    if not isinstance(routes, list):
        return {}
    indexed: dict[str, dict[str, Any]] = {}
    for entry in routes:
        if not isinstance(entry, dict):
            continue
        e1m = entry.get("e1m")
        if not isinstance(e1m, str):
            continue
        # Last-write-wins on duplicates -- the schema doesn't forbid
        # them but real SoMs shouldn't have any; if the YAML carries
        # duplicates, surface the later entry (most-recent author wins).
        indexed[e1m] = entry
    return indexed


def _hwrev_pad_route_overrides(
    sku: str,
    hw_rev: str | None,
    metadata_root: Path = METADATA_ROOT,
) -> list[dict[str, Any]]:
    """Per-rev pad-route overrides for the selected ``hw_rev``.

    The base SoM ``pad_routes:`` tracks the *production* revision.  A board
    revision whose routing differs declares the deviating pads as data in
    the family ``hw-revisions.yaml`` ``pad_route_overrides:`` block; this
    returns that rev's list (empty when it declares none, so the base
    ``pad_routes:`` then applies verbatim).  Applying these is what makes
    the composed route table differ between revisions of one SKU.

    Raises `SdkRevisionUnknown` when `hw_rev` isn't a key in the family
    table, and `SdkRevisionNotBuildable` when it IS a key but its `status:`
    refuses a build (`reserved`/`tbd`/status-less -- alp-sdk #1025's
    existence + status halves; the status half by `1a91a232`, ported here
    tan-cli#485). This emit path resolves its own SoM/board data
    independently of `loader.load_board_yaml`, so both gates need their own
    copy here -- before tan-cli#485, `--target carrier-netlist` /
    `composed-route-table` emitted a full route table for a `reserved`
    hw_rev while `tan build` refused the same input. Same predicates
    (`sdk_compat.revision_known`/`revision_buildable`), same error TYPE and
    MESSAGE SHAPE as `loader.load_board_yaml`'s SoM-side refusals -- the
    `status_repr` construction below is `loader._status_repr` INLINED, not
    imported, so a caller does not see two different shapes of the same
    problem depending on which path resolved it; keep the two in sync by
    hand if either one's message wording changes.

    Reads the table through ``sdk_compat.load_family_table()`` rather than
    its own ``yaml.safe_load``, so both independent readers of this file
    agree about what a damaged one means (#563).  This site used to do
    ``yaml.safe_load(...) or {}``, and that ``or {}`` FAILED OPEN on the
    two shapes that parse without raising: an EMPTY file and a file
    TRUNCATED above its ``hw_revisions:`` block both yielded ``{}``, both
    gates above then read "nothing to judge", and ``--emit
    composed-route-table`` shipped a wrong-hardware artefact at exit 0 for
    an hw_rev that does not exist -- the exact outcome the two gates below
    exist to prevent.  An unparseable table escaped as a raw
    ``yaml.ScannerError`` traceback: a refusal, but not a diagnosable one.
    The shared reader turns all of those into one coded
    ``OrchestratorError`` naming the file.  An ABSENT table still returns
    ``{}`` there and stays benign here.
    """
    if not hw_rev:
        return []
    try:
        family = _sku_family(sku)
    except ValueError:
        return []
    data = sdk_compat.load_family_table(metadata_root, family)
    if not data:
        # Absent table only: `load_family_table` raises on every
        # present-but-unusable shape, so reaching here with a falsy
        # `data` means the family genuinely ships no table.
        return []
    if sdk_compat.revision_known(data, hw_rev) is False:
        available = sorted((data.get("hw_revisions") or {}).keys())
        raise SdkRevisionUnknown(
            f"SoM {sku} hw_rev {hw_rev!r} is not a known hardware "
            f"revision. Available hw_rev(s) for {sku}: {available}.")
    if sdk_compat.revision_buildable(data, hw_rev) is False:
        status = (data.get("hw_revisions") or {}).get(hw_rev, {}).get("status")
        status_repr = f"status: {status!r}" if status is not None else \
            "carries no `status:` key"
        raise SdkRevisionNotBuildable(
            f"SoM {sku} hw_rev {hw_rev!r} exists but is not buildable "
            f"({status_repr}).")
    rev = (data.get("hw_revisions") or {}).get(hw_rev) or {}
    overrides = rev.get("pad_route_overrides") or []
    return [e for e in overrides
            if isinstance(e, dict) and isinstance(e.get("e1m"), str)]


def _compose_route(
    e1m_pad: str,
    board_route: dict[str, Any] | None,
    pad_routes: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Compose a board `e1m_routes:` entry (role on the board
    side) with the SoM's `pad_routes:` entry (dispatch path on the
    SoM side) for a single E1M pad / instance.

    Returns a dict with: `board_role`, `board_macro`, `dispatch`
    (defaults to `direct` when the SoM declares no proxy),
    `dispatch_pin`, plus any docs.

    Callers use the composed dict to drive codegen (Zephyr DTS
    overlays, dispatch shims, capability validation).
    """
    out: dict[str, Any] = {"e1m": e1m_pad}
    if board_route:
        out["board_role"] = board_route.get("role")
        out["board_macro"] = board_route.get("macro")
        if board_route.get("doc"):
            out["board_doc"] = board_route["doc"]
    som_route = pad_routes.get(e1m_pad)
    if som_route:
        out["dispatch"] = som_route.get("dispatch", "direct")
        if som_route.get("dispatch_pin") is not None:
            out["dispatch_pin"] = som_route["dispatch_pin"]
        if som_route.get("doc"):
            out["som_doc"] = som_route["doc"]
    else:
        out["dispatch"] = "direct"
    return out
