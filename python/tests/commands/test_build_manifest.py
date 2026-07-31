# SPDX-License-Identifier: Apache-2.0
"""`tan.commands.build.manifest`: the post-build `system-manifest.yaml` write
seam -- `write_post_build_manifest`'s failure branches (pure/no-SDK-needed),
`resolve_zephyr_artefact`'s default-nested-build-dir resolution,
`discover_sdk_root`'s candidate ladder, and the tier ORDER of the TWO ladders
that wrap it -- narrow `resolve_sdk_root_ladder` and wide `resolve_sdk_root_wide`,
pinned against the oracle in the layouts that tell them apart.

The SUCCESS path (a real SDK emit + overlay landing on disk) needs a real
alp-sdk checkout and is gated on `ALP_SDK_ROOT`, mirroring
`tests/core/test_kconfig_symbols.py`'s own gate -- every other case here
needs no SDK at all and always runs.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tan.commands.build.manifest import (
    build_dir_overridden,
    resolve_zephyr_artefact,
    write_post_build_manifest,
)
from tan.commands.build_cmd import (
    discover_sdk_root,
    resolve_sdk_root_ladder,
    resolve_sdk_root_wide,
)
from tan.core.system_manifest import SliceRunResult, parse_system_manifest


def _sdk_root() -> Path | None:
    for var in ("ALP_SDK_PARITY_ROOT", "ALP_SDK_ROOT"):
        raw = os.environ.get(var)
        if raw and (Path(raw) / "scripts" / "alp_project.py").is_file():
            return Path(raw).resolve()
    return None


SDK = _sdk_root()


# ------------------------------------------------------- build_dir_overridden


@pytest.mark.parametrize(
    "args,expected",
    [
        ([], False),
        (["build"], False),
        (["-d", "out"], True),
        (["--build-dir", "out"], True),
        (["--build-dir=out"], True),
        (["--build-dir-ish"], False),
    ],
)
def test_build_dir_overridden(args, expected):
    assert build_dir_overridden(args) is expected


# ------------------------------------------------------- resolve_zephyr_artefact


def test_resolve_zephyr_artefact_finds_the_default_nested_elf(tmp_path):
    elf_dir = tmp_path / "build" / "zephyr"
    elf_dir.mkdir(parents=True)
    (elf_dir / "zephyr.elf").write_text("", encoding="utf-8")

    artefact, build_dir = resolve_zephyr_artefact(tmp_path, [])
    assert artefact == os.path.abspath(str(elf_dir / "zephyr.elf"))
    assert build_dir == os.path.abspath(str(tmp_path / "build"))


def test_resolve_zephyr_artefact_none_when_no_elf_present(tmp_path):
    assert resolve_zephyr_artefact(tmp_path, []) == (None, None)


def test_resolve_zephyr_artefact_ignores_an_overridden_build_dir(tmp_path):
    # An elf genuinely sits at the default path, but the command redirected
    # west's build dir -- it must not be trusted as THIS run's artefact.
    elf_dir = tmp_path / "build" / "zephyr"
    elf_dir.mkdir(parents=True)
    (elf_dir / "zephyr.elf").write_text("", encoding="utf-8")

    assert resolve_zephyr_artefact(tmp_path, ["-d", "../out"]) == (None, None)


# ------------------------------------------------------------- discover_sdk_root


def _make_sdk(root: Path) -> None:
    (root / "scripts").mkdir(parents=True, exist_ok=True)
    (root / "scripts" / "alp_project.py").write_text("", encoding="utf-8")


def test_discover_sdk_root_finds_a_child(tmp_path):
    _make_sdk(tmp_path / "alp-sdk")
    assert discover_sdk_root(tmp_path) == tmp_path / "alp-sdk"


def test_discover_sdk_root_finds_a_sibling(tmp_path):
    _make_sdk(tmp_path / "alp-sdk")
    workspace = tmp_path / "myproj"
    workspace.mkdir()
    assert discover_sdk_root(workspace) == tmp_path / "alp-sdk"


def test_discover_sdk_root_finds_an_ancestor(tmp_path):
    _make_sdk(tmp_path)
    nested = tmp_path / "examples" / "aen" / "demo"
    nested.mkdir(parents=True)
    assert discover_sdk_root(nested) == tmp_path


def test_discover_sdk_root_none_when_nothing_nearby(tmp_path):
    workspace = tmp_path / "myproj"
    workspace.mkdir()
    assert discover_sdk_root(workspace) is None


# ------------------------------------------------------- resolve_sdk_root_ladder
#
# The ORDER of the ladder's last two tiers, pinned against the oracle binary
# (tan-cli#263). `discover_sdk_root` above puts the CHILD `<ws>/alp-sdk` first;
# `resolve_sdk_tiered`'s own discovery probes only the workspace root and the
# LATERAL `../alp-sdk`, else the nearest enclosing checkout. A workspace holding
# both is the one layout that tells the two apart -- and nothing exercised it,
# which is why an inversion here was proposed as a fix. Every expectation below
# is a measured `tan <cmd> --format json` -> `sdk.root` from the Rust oracle in
# exactly this layout, not a reading of the port.
#
# `~/.alp/sdk-default` and `ALP_SDK_ROOT` are scrubbed for every test by the
# autouse fixture in `tests/conftest.py`, so only the layout decides.


def _write_pin(workspace: Path, target: Path) -> None:
    """`.alp/sdk-path` in the `{"sdkPath": ...}` shape `sdk_cmd._pointer_target`
    reads -- a bare path string parses as invalid JSON and falls through."""
    (workspace / ".alp").mkdir(parents=True, exist_ok=True)
    (workspace / ".alp" / "sdk-path").write_text(
        json.dumps({"sdkPath": str(target).replace("\\", "/")}), encoding="utf-8"
    )


def test_ladder_takes_the_lateral_checkout_over_a_competing_child(tmp_path):
    # Oracle, measured: `../alp-sdk`. The narrow tier hits laterally and
    # short-circuits, so the child-first wide walk never runs. Hoisting the
    # wide walk above it would return `ws/alp-sdk` here and move the SDK root
    # under every already-built workspace of this shape.
    workspace = tmp_path / "ws"
    workspace.mkdir()
    _make_sdk(workspace / "alp-sdk")
    _make_sdk(tmp_path / "alp-sdk")

    assert resolve_sdk_root_ladder(None, workspace) == (tmp_path / "alp-sdk", "discovery")


def test_ladder_takes_the_enclosing_checkout_over_a_competing_child(tmp_path):
    # Oracle, measured: the enclosing checkout. Same short-circuit, reached
    # through the narrow tier's ancestor fallback rather than its lateral probe.
    workspace = tmp_path / "ws"
    workspace.mkdir()
    _make_sdk(tmp_path)
    _make_sdk(workspace / "alp-sdk")

    assert resolve_sdk_root_ladder(None, workspace) == (tmp_path, "discovery")


def test_ladder_falls_through_to_the_wide_walk_for_a_bootstrap_child(tmp_path):
    # The canonical tan-cli#218 layout: no lateral and no enclosing checkout, so
    # the narrow tier answers None and the wide walk's child rung is what
    # resolves. This is the tier the two tests above must not cost us.
    workspace = tmp_path / "ws"
    workspace.mkdir()
    _make_sdk(workspace / "alp-sdk")

    assert resolve_sdk_root_ladder(None, workspace) == (workspace / "alp-sdk", "discovery")


def test_ladder_project_pin_outranks_every_discovery_candidate(tmp_path):
    # Oracle, measured: the pinned path, tier `projectPin` -- with a child AND a
    # lateral checkout both present and both ignored.
    workspace = tmp_path / "ws"
    workspace.mkdir()
    _make_sdk(workspace / "alp-sdk")
    _make_sdk(tmp_path / "alp-sdk")
    _make_sdk(tmp_path / "pinned")
    _write_pin(workspace, tmp_path / "pinned")

    assert resolve_sdk_root_ladder(None, workspace) == (tmp_path / "pinned", "projectPin")


def test_ladder_sdk_root_flag_is_terminal_and_unvalidated(tmp_path):
    # I-31: the flag wins even when it names no checkout, so a bad `--sdk-root`
    # surfaces as a readiness failure on the path the user typed instead of
    # silently cleaning/building against the lateral one below it.
    workspace = tmp_path / "ws"
    workspace.mkdir()
    _make_sdk(tmp_path / "alp-sdk")

    assert resolve_sdk_root_ladder(str(tmp_path / "nope"), workspace) == (
        tmp_path / "nope",
        "sdkRootFlag",
    )


# --------------------------------------------------------- resolve_sdk_root_wide
#
# The SAME layouts against the OTHER ladder -- the one `init`, `generate`,
# `examples` and `renode` take, whose discovery tier is the wide walk. The oracle
# resolves the child in every layout where the narrow ladder above resolves the
# lateral or enclosing checkout; the two blocks are therefore expected to
# DISAGREE, and a change that makes them agree has broken one of them.
# Measured the same way: `tan <cmd> --format json` -> `sdk.root` in exactly this
# layout.


def test_wide_ladder_takes_the_bootstrap_child(tmp_path):
    # tan-cli#218 layout -- both ladders agree here, which is why this case alone
    # never revealed the split.
    workspace = tmp_path / "ws"
    workspace.mkdir()
    _make_sdk(workspace / "alp-sdk")

    assert resolve_sdk_root_wide(None, workspace) == (workspace / "alp-sdk", "discovery")


def test_wide_ladder_takes_the_child_over_a_competing_lateral(tmp_path):
    # Oracle, measured: `<ws>/alp-sdk`. The narrow ladder returns `../alp-sdk`
    # for this exact layout (see the test of the same shape above). This is the
    # divergence tan-cli#263 was re-scoped to: `tan init` WRITES its answer to
    # `.alp/sdk-path`, so the sibling would outrank discovery permanently.
    workspace = tmp_path / "ws"
    workspace.mkdir()
    _make_sdk(workspace / "alp-sdk")
    _make_sdk(tmp_path / "alp-sdk")

    assert resolve_sdk_root_wide(None, workspace) == (workspace / "alp-sdk", "discovery")


def test_wide_ladder_takes_the_child_over_an_enclosing_checkout(tmp_path):
    # Oracle, measured: `<ws>/alp-sdk` -- a project scaffolded INSIDE a checkout
    # that also bootstrapped its own. The narrow ladder returns the enclosing one.
    workspace = tmp_path / "ws"
    workspace.mkdir()
    _make_sdk(tmp_path)
    _make_sdk(workspace / "alp-sdk")

    assert resolve_sdk_root_wide(None, workspace) == (workspace / "alp-sdk", "discovery")


def test_wide_ladder_project_pin_outranks_the_child(tmp_path):
    # Oracle, measured: the pinned path, tier `projectPin`. Widening discovery
    # must not promote it past the pointer tiers -- a pin that stopped winning
    # is how `tan init && tan build` starts disagreeing again.
    workspace = tmp_path / "ws"
    workspace.mkdir()
    _make_sdk(workspace / "alp-sdk")
    _make_sdk(tmp_path / "alp-sdk")
    _make_sdk(tmp_path / "pinned")
    _write_pin(workspace, tmp_path / "pinned")

    assert resolve_sdk_root_wide(None, workspace) == (tmp_path / "pinned", "projectPin")


# ------------------------------------------------------ write_post_build_manifest


def test_write_post_build_manifest_reports_unsafe_build_root(tmp_path):
    outcome = write_post_build_manifest(
        sdk_root="/sdk",
        board_yaml=str(tmp_path / "board.yaml"),
        base=str(tmp_path),
        plan_build_root="../escape",
        results=[],
    )
    assert outcome.write_failed_reason is not None
    assert outcome.native_sim_target is None
    assert not (tmp_path.parent / "escape").exists()


def test_write_post_build_manifest_reports_reason_when_sdk_unresolved(tmp_path):
    outcome = write_post_build_manifest(
        sdk_root=None,
        board_yaml=str(tmp_path / "no-such-project" / "board.yaml"),
        base=str(tmp_path),
        plan_build_root="build",
        results=[],
    )
    assert outcome.write_failed_reason is not None
    assert outcome.native_sim_target is None
    assert not (tmp_path / "build" / "system-manifest.yaml").exists()


def test_write_post_build_manifest_reports_reason_when_board_yaml_missing(tmp_path):
    outcome = write_post_build_manifest(
        sdk_root=None, board_yaml=None, base=str(tmp_path),
        plan_build_root="build", results=[],
    )
    assert outcome.write_failed_reason is not None
    assert outcome.native_sim_target is None


@pytest.mark.skipif(
    SDK is None,
    reason="set ALP_SDK_ROOT to an alp-sdk checkout for the real emit+write path",
)
def test_write_post_build_manifest_writes_a_real_overlaid_file(tmp_path):
    assert SDK is not None
    board_yaml = SDK / "examples" / "aen" / "aen-analog-validate" / "board.yaml"
    assert board_yaml.is_file(), "fixture example moved -- update the path"

    outcome = write_post_build_manifest(
        sdk_root=str(SDK),
        board_yaml=str(board_yaml),
        base=str(tmp_path),
        plan_build_root="build",
        results=[
            SliceRunResult(
                "m55_hp", "ok",
                "build/m55_hp-zephyr/build/zephyr/zephyr.elf",
                "build/m55_hp-zephyr/build",
            ),
        ],
    )
    assert outcome.write_failed_reason is None
    # AEN801 has no native_sim slice.
    assert outcome.native_sim_target is False

    written = (tmp_path / "build" / "system-manifest.yaml").read_text(encoding="utf-8")
    manifest = parse_system_manifest(written)
    hp = next(s for s in manifest.slices if s["core_id"] == "m55_hp")
    assert hp["status"] == "ok"
    assert hp["output_artefact"] == "build/m55_hp-zephyr/build/zephyr/zephyr.elf"
    # A slice this run never touched keeps its plan-time status.
    a32 = next(s for s in manifest.slices if s["core_id"] == "a32_cluster")
    assert a32["status"] == "pending"


# ------------------------------- hermetic success/failure paths (no real SDK)
#
# The two ALP_SDK_ROOT-gated cases above are the only tests that ever exercise
# a real emit; skipped by default, they leave the whole success path (and the
# `native_sim_target`-survives-a-write-failure property) covered by NOTHING.
# `write_post_build_manifest` imports `tan.planner_root.emit` LOCALLY (inside
# the function -- see its own module docstring), so monkeypatching the module
# attribute intercepts it without a real alp-sdk checkout on disk.

_FIXTURE_MANIFEST = """schema_version: 1
generated_by: scripts/alp_orchestrate.py
hw_info:
  sku: E1M-TEST
slices:
- core_id: m55_hp
  os: zephyr
  status: pending
- core_id: m55_he
  os: zephyr
  status: pending
ipc: []
helper_mcus: []
boot_order: []
"""

_NATIVE_SIM_FIXTURE_MANIFEST = """schema_version: 1
generated_by: scripts/alp_orchestrate.py
hw_info:
  sku: E1M-TEST
slices:
- core_id: native_sim
  os: zephyr
  board: native_sim
  status: pending
ipc: []
helper_mcus: []
boot_order: []
"""


def test_write_post_build_manifest_writes_a_real_overlaid_file_hermetically(tmp_path, monkeypatch):
    """The success path, provably, with no real SDK checkout: a synthetic
    `tan.planner_root.emit` stand-in gives full parse -> overlay -> serialize
    -> write coverage."""
    import tan.planner_root as planner_root

    monkeypatch.setattr(planner_root, "emit", lambda *a, **k: _FIXTURE_MANIFEST)

    outcome = write_post_build_manifest(
        sdk_root="/fake/sdk",
        board_yaml=str(tmp_path / "board.yaml"),
        base=str(tmp_path),
        plan_build_root="build",
        results=[
            SliceRunResult(
                "m55_hp", "ok",
                "build/m55_hp-zephyr/build/zephyr/zephyr.elf",
                "build/m55_hp-zephyr/build",
            ),
        ],
    )
    assert outcome.write_failed_reason is None
    assert outcome.native_sim_target is False

    dest = tmp_path / "build" / "system-manifest.yaml"
    # RAW bytes, not `read_text` -- which applies the SAME universal-newline
    # translation on read that this write path must NOT apply on write, and
    # would mask a CRLF regression by silently normalising it back to LF.
    raw_bytes = dest.read_bytes()
    assert b"\r\n" not in raw_bytes, (
        "system-manifest.yaml must stay LF-only (contract artefact byte parity "
        "with the oracle's std::fs::write, which never translates newlines)"
    )
    written = raw_bytes.decode("utf-8")
    manifest = parse_system_manifest(written)
    hp = next(s for s in manifest.slices if s["core_id"] == "m55_hp")
    assert hp["status"] == "ok"
    assert hp["output_artefact"] == "build/m55_hp-zephyr/build/zephyr/zephyr.elf"
    he = next(s for s in manifest.slices if s["core_id"] == "m55_he")
    assert he["status"] == "pending"


def test_serialize_failure_is_reported_not_raised(tmp_path, monkeypatch):
    """`serialize_system_manifest_raw` (`yaml.safe_dump`) is called OUTSIDE
    any `try` in the oracle's own terms too -- `rewrite_manifest_yaml` returns
    `Result<String, String>` for every branch, including the dumper's own
    failure. Uncaught here, a `RepresenterError` (or anything else the dumper
    can throw) would escape `write_post_build_manifest`, escape
    `execute_slices`, and abort a build whose slices already ran -- breaking
    the documented best-effort contract. Forces the failure directly (rather
    than hunting for a document PyYAML's SafeDumper genuinely can't
    represent) so this test exercises the `try/except` itself, not PyYAML's
    dumper internals."""
    import tan.commands.build.manifest as manifest_module
    import tan.planner_root as planner_root

    monkeypatch.setattr(planner_root, "emit", lambda *a, **k: _FIXTURE_MANIFEST)

    def _boom(_raw):
        raise ValueError("cannot represent this node")

    monkeypatch.setattr(manifest_module, "serialize_system_manifest_raw", _boom)

    outcome = write_post_build_manifest(
        sdk_root="/fake/sdk",
        board_yaml=str(tmp_path / "board.yaml"),
        base=str(tmp_path),
        plan_build_root="build",
        results=[],
    )
    assert outcome.write_failed_reason is not None
    assert "cannot represent this node" in outcome.write_failed_reason
    assert not (tmp_path / "build" / "system-manifest.yaml").exists()


def test_native_sim_target_survives_a_write_failure(tmp_path, monkeypatch):
    """The property both `PostBuildManifest.native_sim_target` and
    `write_post_build_manifest`'s own docstring call load-bearing: derived
    from the emit BEFORE the file write is attempted, so a write failure must
    not erase it. Planting the R1 defect (returning `None` from the OSError
    branch instead of the already-computed `native_sim_target`) must fail
    this assertion."""
    import tan.planner_root as planner_root

    monkeypatch.setattr(planner_root, "emit", lambda *a, **k: _NATIVE_SIM_FIXTURE_MANIFEST)
    # Force the write itself to fail: make the destination FILE a directory.
    (tmp_path / "build").mkdir()
    (tmp_path / "build" / "system-manifest.yaml").mkdir()

    outcome = write_post_build_manifest(
        sdk_root="/fake/sdk",
        board_yaml=str(tmp_path / "board.yaml"),
        base=str(tmp_path),
        plan_build_root="build",
        results=[],
    )
    assert outcome.write_failed_reason is not None
    assert outcome.native_sim_target is True
