# SPDX-License-Identifier: Apache-2.0
"""`tan generate`: target resolution, output paths, and every refusal path.

The one committed conformance fixture pins only `board-yaml-missing`. These
cover what it cannot: the argument-shape refusals, the two guards that protect a
user's tree, and -- the whole point -- that no failure path escapes as a
traceback instead of an envelope.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import typer

from tan.commands import generate_cmd
from tan.commands.generate_cmd import (
    ALL_EMIT_MODES,
    COMPOSED_ROUTE_TABLE,
    CORE_SCOPABLE_TARGETS,
    EXPLICIT_ONLY_TARGETS,
    IPC_CONTRACT_H,
    ZEPHYR_BOARD,
    GenerateError,
    _ensure_writable,
    _output_path,
    _OVERLAY_BANNER,
    _overlay_would_overwrite,
    _resolve_output_override,
    _scan_som_sku,
    generate,
    resolve_targets,
    zephyr_board_dir_name,
)
from tan.exit_codes import ExitCode

PACKAGE_ROOT = Path(__file__).resolve().parents[2]


def run_cli(argv, cwd):
    """Spawn the real CLI -- the only way to prove stdout carries exactly one
    JSON envelope and stderr stays empty under `--format json`."""
    return subprocess.run(
        [sys.executable, "-m", "tan", "generate", *argv],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=cwd,
        env={**os.environ, "PYTHONPATH": str(PACKAGE_ROOT)},
        check=False,
    )


def envelope_of(proc):
    assert proc.stderr.strip() == "", f"stderr must be empty under --format json:\n{proc.stderr}"
    return json.loads(proc.stdout.strip())


def make_sdk(root: Path) -> Path:
    """A directory `_resolve_sdk_root` accepts. The refusal tests all fail out
    before the script is ever spawned, so its contents do not matter."""
    (root / "scripts").mkdir(parents=True, exist_ok=True)
    (root / "scripts" / "alp_project.py").write_text("", encoding="utf-8")
    return root


# --------------------------------------------------------------------------
# Target resolution
# --------------------------------------------------------------------------


def test_bare_invocation_resolves_every_default_target():
    assert resolve_targets(None, False, None) == ALL_EMIT_MODES
    assert resolve_targets(None, True, None) == ALL_EMIT_MODES
    # zephyr-board is never folded into the default set: it hard-requires
    # --core and writes a directory, so a bare run has no path for it.
    assert ZEPHYR_BOARD not in ALL_EMIT_MODES
    # Pinned against the Rust oracle's `tan_core::ALL_EMIT_MODES` (9 entries,
    # crates/tan-core/src/loader.rs) so a target landing in
    # `_OUTPUT_RELATIVE_PATH` without also landing in `EXPLICIT_ONLY_TARGETS`
    # cannot silently grow what a bare `tan generate` runs again.
    assert len(ALL_EMIT_MODES) == 9
    assert COMPOSED_ROUTE_TABLE not in ALL_EMIT_MODES


def test_single_target_resolves_to_itself():
    assert resolve_targets("cmake-args", False, None) == ("cmake-args",)
    # The Studio netlist handoff and the native_sim overlay must reach the SDK
    # spawn, not be rejected at the allowlist.
    assert resolve_targets("carrier-netlist", False, None) == ("carrier-netlist",)
    assert resolve_targets("native-sim-overlay", False, None) == ("native-sim-overlay",)


def test_unknown_target_is_a_validation_failure_not_internal():
    with pytest.raises(GenerateError) as err:
        resolve_targets("bogus", False, None)
    assert err.value.code == "generate.invalid-target"
    assert err.value.exit_code == ExitCode.VALIDATION_FAILURE
    assert "Unsupported generate target" in err.value.message


def test_zephyr_board_requires_core():
    # The FAILING case: no --core must be refused, not silently defaulted --
    # alp_project.py itself hard-requires it. And it must be exit 2, not the
    # exit 5 an ordinary usage mistake used to get.
    with pytest.raises(GenerateError) as err:
        resolve_targets(ZEPHYR_BOARD, False, None)
    assert err.value.exit_code == ExitCode.VALIDATION_FAILURE
    assert "--core" in err.value.message
    assert resolve_targets(ZEPHYR_BOARD, False, "m55_hp") == (ZEPHYR_BOARD,)


@pytest.mark.parametrize("target", CORE_SCOPABLE_TARGETS)
def test_core_is_accepted_for_the_scopable_targets(target):
    assert resolve_targets(target, False, "m55_hp") == (target,)


@pytest.mark.parametrize(
    "target,all_targets",
    [
        # Targets the SDK never reads --core for at all...
        ("carrier-netlist", False),
        ("native-sim-overlay", False),
        # ...one that accepts and then ignores it...
        ("os-topology", False),
        # ...and the default/--all sets, which mix core-scoped and core-blind
        # targets in one run.
        (None, False),
        ("cmake-args", True),
    ],
)
def test_core_is_refused_where_it_would_do_nothing(target, all_targets):
    with pytest.raises(GenerateError) as err:
        resolve_targets(target, all_targets, "m55_hp")
    assert "--core" in err.value.message


# --------------------------------------------------------------------------
# Output paths
# --------------------------------------------------------------------------


def test_output_paths_match_the_documented_conventions():
    ws = Path("/ws")
    assert _output_path(ws, "zephyr-conf", None).as_posix().endswith(
        "build/generated/alp.conf"
    )
    assert _output_path(ws, "west-libraries", None).as_posix().endswith(
        "build/generated/alp-west-libs.yml"
    )
    assert _output_path(ws, "hw-info-h", None).as_posix().endswith(
        "build/generated/alp_hw_info_build.h"
    )
    # Zephyr auto-discovers boards/<board>.overlay in the app SOURCE tree, NOT
    # build/generated -- put it in the wrong place and native_sim GPIO never
    # resolves.
    assert _output_path(ws, "native-sim-overlay", None).as_posix().endswith(
        "boards/native_sim_native_64.overlay"
    )


def test_output_path_uses_the_native_separator_throughout():
    """The FAILING case an `os.path.join` of the whole `/`-separated literal
    produces: one envelope's `written[]` mixing `build/generated/alp.conf` with
    `build\\boards\\<dir>`. Asserts the RENDERED string, because Windows path
    parsing treats `/` as a separator regardless of the raw bytes."""
    import os

    if os.sep == "/":
        pytest.skip("nothing to prove where '/' is already native")
    for mode in ALL_EMIT_MODES:
        rendered = str(_output_path(Path("."), mode, None))
        assert "/" not in rendered, f"{mode}: {rendered!r} carries a non-native '/'"


def test_zephyr_board_dir_name_folds_in_the_sku():
    assert zephyr_board_dir_name("E1M-AEN801", "m55_hp") == "alp_e1m_aen801_m55_hp"
    # The FAILING case a bare core id produced: two projects on the SAME core
    # but DIFFERENT SoMs collided in one build/boards/m55_hp/.
    assert zephyr_board_dir_name("E1M-AEN901", "m55_hp") != zephyr_board_dir_name(
        "E1M-AEN801", "m55_hp"
    )
    assert zephyr_board_dir_name("BOGUS-SKU", "m55_hp") is None


def test_scan_som_sku_reads_the_nested_key_without_pyyaml():
    assert _scan_som_sku("som:\n  sku: E1M-AEN801\n") == "E1M-AEN801"
    assert _scan_som_sku("som:\n  sku: 'E1M-V2N101'\n") == "E1M-V2N101"
    # A `sku:` under some OTHER top-level block is not the SoM's.
    assert _scan_som_sku("cores:\n  sku: nope\n") is None
    assert _scan_som_sku("som:\n") is None


def test_overlay_guard_fires_only_on_an_existing_unforced_overlay(tmp_path):
    (tmp_path / "boards").mkdir()
    (tmp_path / "boards" / "native_sim_native_64.overlay").write_text("x", encoding="utf-8")
    assert _overlay_would_overwrite(tmp_path, ("native-sim-overlay",), False)
    assert not _overlay_would_overwrite(tmp_path, ("native-sim-overlay",), True)
    assert not _overlay_would_overwrite(tmp_path, ("cmake-args",), False)


def test_overlay_guard_is_content_aware(tmp_path):
    """tan-cli#457: existence alone used to fire the guard, so a re-run
    against the overlay THIS command itself just wrote refused forever. The
    banner every emitted overlay opens with is the free provenance marker
    that tells them apart."""
    overlay = tmp_path / "boards" / "native_sim_native_64.overlay"
    overlay.parent.mkdir()

    overlay.write_text(f"/* {_OVERLAY_BANNER} */\nsome content\n", encoding="utf-8")
    assert not _overlay_would_overwrite(tmp_path, ("native-sim-overlay",), False)

    overlay.write_text("hand tuned, no banner\n", encoding="utf-8")
    assert _overlay_would_overwrite(tmp_path, ("native-sim-overlay",), False)


# --------------------------------------------------------------------------
# End-to-end refusals: an envelope with a code, never a traceback
# --------------------------------------------------------------------------


def test_missing_board_yaml_is_exit_2_with_the_frozen_message(tmp_path):
    proc = run_cli(["--format", "json"], cwd=tmp_path)
    assert proc.returncode == 2
    env = envelope_of(proc)
    assert env["issues"][0]["code"] == "generate.board-yaml-missing"
    assert env["data"] == {
        "schemaVersion": "1",
        "targets": [],
        "written": [],
        "failed": [],
    }
    # `sdk` is absent, never null.
    assert "sdk" not in env


def test_missing_board_yaml_is_checked_before_a_bad_target(tmp_path):
    """An asymmetry worth pinning: the oracle checks the board FIRST, so a bad
    `--target` in a directory with no board.yaml reports the missing board."""
    proc = run_cli(["--target", "bogus", "--format", "json"], cwd=tmp_path)
    env = envelope_of(proc)
    assert env["issues"][0]["code"] == "generate.board-yaml-missing"


def test_unresolved_sdk_is_a_refusal_not_a_crash(tmp_path):
    project = tmp_path / "deep" / "project"
    project.mkdir(parents=True)
    (project / "board.yaml").write_text("som:\n  sku: E1M-AEN801\n", encoding="utf-8")
    proc = run_cli(["--sdk-root", str(tmp_path / "nope"), "--format", "json"], cwd=project)
    assert proc.returncode == 2
    env = envelope_of(proc)
    assert env["issues"][0]["code"] == "generate.sdk-root-unresolved"
    # No `sdk` key: nothing resolved, so there is nothing to report -- absent,
    # never null, matching the oracle (`crates/tan-cli`'s own refusal here).
    assert "sdk" not in env


def test_bad_target_is_a_refusal_once_the_board_and_sdk_resolve(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "board.yaml").write_text("som:\n  sku: E1M-AEN801\n", encoding="utf-8")
    sdk = make_sdk(tmp_path / "alp-sdk")
    proc = run_cli(["--sdk-root", str(sdk), "--target", "bogus", "--format", "json"], cwd=project)
    assert proc.returncode == 2
    env = envelope_of(proc)
    assert env["issues"][0]["code"] == "generate.invalid-target"
    # Unlike the unresolved-SDK refusal above, this guard runs AFTER the SDK
    # resolved, so the envelope carries it -- matching the oracle, which
    # reports `sdk` on every refusal past a successful resolution.
    assert env["sdk"] == {"root": Path(sdk).as_posix(), "sourceTier": "sdkRootFlag"}


def test_sdk_root_flag_reports_verbatim_including_a_trailing_separator(tmp_path):
    """`sdk.root` echoes the flag's raw text, not `str(Path(...))` --
    `pathlib` silently drops a trailing separator, which shell tab-completion
    of a directory routinely adds, and the oracle does not drop it."""
    project = tmp_path / "project"
    project.mkdir()
    (project / "board.yaml").write_text("som:\n  sku: E1M-AEN801\n", encoding="utf-8")
    sdk = make_sdk(tmp_path / "alp-sdk")
    # Posix form: both sides normalise a `--sdk-root` backslash to `/` before
    # reporting it (`SdkInfo.to_dict`'s `.replace("\\", "/")`), so a
    # backslash-separated raw string would pin that normalisation instead of
    # the trailing-separator behaviour this case targets.
    raw = sdk.as_posix() + "/"
    proc = run_cli(["--sdk-root", raw, "--target", "bogus", "--format", "json"], cwd=project)
    assert proc.returncode == 2
    env = envelope_of(proc)
    assert env["sdk"] == {"root": raw, "sourceTier": "sdkRootFlag"}


def test_sdk_project_pin_tier_reports_in_the_envelope(tmp_path):
    """Every other case here only ever exercises `sdkRootFlag` -- the same
    shape of gap that once let the whole `sdk` envelope key drop out
    unnoticed. `.alp/sdk-path` is written directly (mirroring
    `test_sdk_command.py`'s own `write_pointer`) because `tan sdk switch` is
    not ported."""
    project = tmp_path / "project"
    project.mkdir()
    (project / "board.yaml").write_text("som:\n  sku: E1M-AEN801\n", encoding="utf-8")
    sdk = make_sdk(tmp_path / "alp-sdk")
    (project / ".alp").mkdir()
    (project / ".alp" / "sdk-path").write_text(
        json.dumps({"sdkPath": str(sdk), "updatedAt": "1970-01-01T00:00:00Z"}),
        encoding="utf-8",
    )
    proc = run_cli(["--target", "bogus", "--format", "json"], cwd=project)
    assert proc.returncode == 2
    env = envelope_of(proc)
    assert env["issues"][0]["code"] == "generate.invalid-target"
    assert env["sdk"] == {"root": Path(sdk).as_posix(), "sourceTier": "projectPin"}


def test_sdk_global_default_tier_reports_in_the_envelope(tmp_path, monkeypatch):
    """The third tier: `~/.alp/sdk-default`, tried when there is no
    `--sdk-root` and no project pin. `conftest.py`'s autouse fixture has
    already repointed `HOME`/`USERPROFILE` at a scratch dir, so writing the
    pointer there cannot collide with a developer's real global default."""
    project = tmp_path / "project"
    project.mkdir()
    (project / "board.yaml").write_text("som:\n  sku: E1M-AEN801\n", encoding="utf-8")
    sdk = make_sdk(tmp_path / "alp-sdk")
    home = Path(os.environ["HOME"])
    (home / ".alp").mkdir(parents=True, exist_ok=True)
    (home / ".alp" / "sdk-default").write_text(
        json.dumps({"sdkPath": str(sdk), "updatedAt": "1970-01-01T00:00:00Z"}),
        encoding="utf-8",
    )
    proc = run_cli(["--target", "bogus", "--format", "json"], cwd=project)
    assert proc.returncode == 2
    env = envelope_of(proc)
    assert env["issues"][0]["code"] == "generate.invalid-target"
    assert env["sdk"] == {"root": Path(sdk).as_posix(), "sourceTier": "globalDefault"}


def test_sdk_discovery_tier_reports_in_the_envelope(tmp_path):
    """The last resolving tier: no flag, no pin, no global default -- the
    workspace root itself doubles as the SDK checkout, which is what an
    in-tree `tan generate` (no separate alp-sdk checkout) resolves against."""
    project = tmp_path / "project"
    project.mkdir()
    (project / "board.yaml").write_text("som:\n  sku: E1M-AEN801\n", encoding="utf-8")
    make_sdk(project)
    proc = run_cli(["--target", "bogus", "--format", "json"], cwd=project)
    assert proc.returncode == 2
    env = envelope_of(proc)
    assert env["issues"][0]["code"] == "generate.invalid-target"
    assert env["sdk"] == {"root": project.as_posix(), "sourceTier": "discovery"}


def test_non_e1m_sku_is_refused_before_naming_a_board_directory(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "board.yaml").write_text("som:\n  sku: BOGUS-SKU\n", encoding="utf-8")
    sdk = make_sdk(tmp_path / "alp-sdk")
    proc = run_cli(
        [
            "--sdk-root", str(sdk),
            "--target", ZEPHYR_BOARD,
            "--core", "m55_hp",
            "--format", "json",
        ],
        cwd=project,
    )
    assert proc.returncode == 2
    assert envelope_of(proc)["issues"][0]["code"] == "generate.board-sku-unresolved"


def test_unreadable_board_yaml_is_board_sku_unresolved_not_a_traceback(tmp_path):
    """Non-UTF-8 bytes where a SKU should be. The oracle swallows the read
    failure into "no SKU"; what must never happen is a traceback on stdout."""
    project = tmp_path / "project"
    project.mkdir()
    (project / "board.yaml").write_bytes(b"som:\n  sku: \xff\xfe\x00bad\n")
    sdk = make_sdk(tmp_path / "alp-sdk")
    proc = run_cli(
        [
            "--sdk-root", str(sdk),
            "--target", ZEPHYR_BOARD,
            "--core", "m55_hp",
            "--format", "json",
        ],
        cwd=project,
    )
    assert proc.returncode == 2
    assert envelope_of(proc)["issues"][0]["code"] == "generate.board-sku-unresolved"


def test_overlay_overwrite_is_exit_3(tmp_path):
    project = tmp_path / "project"
    (project / "boards").mkdir(parents=True)
    (project / "board.yaml").write_text("som:\n  sku: E1M-AEN801\n", encoding="utf-8")
    (project / "boards" / "native_sim_native_64.overlay").write_text("tuned", encoding="utf-8")
    sdk = make_sdk(tmp_path / "alp-sdk")
    proc = run_cli(
        ["--sdk-root", str(sdk), "--target", "native-sim-overlay", "--format", "json"],
        cwd=project,
    )
    assert proc.returncode == 3
    assert envelope_of(proc)["issues"][0]["code"] == "generate.would-overwrite"
    # And the hand-tuned file is still there.
    assert (project / "boards" / "native_sim_native_64.overlay").read_text() == "tuned"


def _call_generate(**kwargs) -> int:
    """Invoke the command in-process, returning its exit code. In-process
    because the emitter stand-ins below must run under THIS interpreter, and
    `_planner_python` deliberately names a PATH interpreter -- monkeypatching it
    is the seam, not a test-only env var in production code."""
    defaults = dict(
        target=None,
        all_targets=False,
        core=None,
        force=False,
        project=None,
        board_yaml=None,
        sdk_root=None,
        output=None,
        quiet=False,
        non_interactive=False,
        output_format="text",
        verbose=False,
    )
    with pytest.raises(typer.Exit) as exit_info:
        generate(**{**defaults, **kwargs})
    return int(exit_info.value.exit_code)


def test_a_failing_emit_reports_the_sdk_stderr_and_exit_3(tmp_path, monkeypatch, capsys):
    """The delegation path, with a stand-in for the SDK script: tan reports
    WHAT the SDK said, it does not paraphrase or swallow it."""
    project = tmp_path / "project"
    project.mkdir()
    (project / "board.yaml").write_text("som:\n  sku: E1M-AEN801\n", encoding="utf-8")
    sdk = make_sdk(tmp_path / "alp-sdk")
    (sdk / "scripts" / "alp_project.py").write_text(
        "import sys\nsys.stderr.write('alp_project: kaboom\\n')\nsys.exit(1)\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(project)
    monkeypatch.setattr(generate_cmd, "_planner_python", lambda *_a, **_k: sys.executable)

    code = _call_generate(
        target="cmake-args", sdk_root=str(sdk), output_format="json"
    )
    assert code == 3
    env = json.loads(capsys.readouterr().out.strip())
    assert env["data"]["failed"] == ["cmake-args"]
    assert env["data"]["written"] == []
    assert env["issues"][0]["code"] == "generate.emit-failed"
    assert "kaboom" in env["issues"][0]["message"]


def test_a_successful_emit_reports_the_relative_written_path(tmp_path, monkeypatch, capsys):
    """The happy path, exercised against a stand-in emitter: tan reports the
    workspace-RELATIVE output path, and creates nothing itself -- the SDK's own
    `_write_or_print` makes the parent directories."""
    project = tmp_path / "project"
    project.mkdir()
    (project / "board.yaml").write_text("som:\n  sku: E1M-AEN801\n", encoding="utf-8")
    sdk = make_sdk(tmp_path / "alp-sdk")
    (sdk / "scripts" / "alp_project.py").write_text(
        "import sys, pathlib\n"
        "out = pathlib.Path(sys.argv[sys.argv.index('--output') + 1])\n"
        "out.parent.mkdir(parents=True, exist_ok=True)\n"
        "out.write_text('generated\\n', encoding='utf-8')\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(project)
    monkeypatch.setattr(generate_cmd, "_planner_python", lambda *_a, **_k: sys.executable)
    assert _call_generate(target="cmake-args", sdk_root=str(sdk), output_format="json") == 0
    env = json.loads(capsys.readouterr().out.strip())
    # Workspace-RELATIVE, never the absolute host path the envelope would
    # otherwise leak into a golden.
    assert env["data"]["written"] == [os.path.join("build", "generated", "alp-cmake-args.txt")]
    assert env["data"]["failed"] == []
    written = project / "build" / "generated" / "alp-cmake-args.txt"
    assert written.read_text(encoding="utf-8") == "generated\n"
    # The resolved SDK, threaded through into the envelope -- this command used
    # to discard the tier `_resolve_sdk_root` already had and drop `sdk` from
    # the envelope entirely, a silent break in the vscode handshake that no
    # fixture or compile error caught.
    assert env["sdk"] == {"root": Path(sdk).as_posix(), "sourceTier": "sdkRootFlag"}


# --------------------------------------------------------------------------
# `composed-route-table`: the third explicit-only target
#
# The older pad-route debug/demonstrator view of `carrier-netlist`'s own
# route rows -- a maintainer-only tool with no product consumer, so it must
# stay reachable via an explicit `--target` without joining the default set a
# bare `tan generate` runs.
# --------------------------------------------------------------------------


def test_composed_route_table_is_reachable_but_never_default():
    """The regression this guards: `composed-route-table` landing in
    `_OUTPUT_RELATIVE_PATH` without also landing in `EXPLICIT_ONLY_TARGETS`
    silently grows the default/`--all` set and makes a bare `tan generate`
    run a full pad-route composition for every project."""
    assert COMPOSED_ROUTE_TABLE not in ALL_EMIT_MODES
    assert COMPOSED_ROUTE_TABLE in EXPLICIT_ONLY_TARGETS
    assert resolve_targets(COMPOSED_ROUTE_TABLE, False, None) == (COMPOSED_ROUTE_TABLE,)


# --------------------------------------------------------------------------
# `ipc-contract-h`: the second explicit-only target
#
# It exists so `alp_sdk_ipc_contract_header()` in alp-sdk's `cmake/alp.cmake`
# has a `tan` front door at all -- four multicore examples consume the header
# via `zephyr_include_directories`, and before this it had no `--target`.
# --------------------------------------------------------------------------


def test_ipc_contract_h_is_reachable_but_never_default():
    """The FAILING case folding it into the default set produces: a bare `tan
    generate` on any of the 95 single-core examples reports a failed target,
    because there is no `ipc:` block to render."""
    assert IPC_CONTRACT_H not in ALL_EMIT_MODES
    assert IPC_CONTRACT_H in EXPLICIT_ONLY_TARGETS
    assert resolve_targets(IPC_CONTRACT_H, False, None) == (IPC_CONTRACT_H,)


def test_ipc_contract_h_lands_where_cmake_puts_it_on_the_include_path():
    """`alp_sdk_ipc_contract_header()` writes
    `${CMAKE_BINARY_DIR}/generated/alp/system_ipc.h` and then puts
    `${CMAKE_BINARY_DIR}/generated` on the include path -- so the header has to
    be one level down, under `alp/`, or `#include <alp/system_ipc.h>` misses."""
    assert _output_path(Path("/ws"), IPC_CONTRACT_H, None).as_posix().endswith(
        "build/generated/alp/system_ipc.h"
    )


def test_ipc_contract_h_refuses_a_core():
    """The contract IS the cross-core agreement; scoping it to one core is
    meaningless, so it is refused rather than silently ignored."""
    with pytest.raises(GenerateError) as err:
        resolve_targets(IPC_CONTRACT_H, False, "m55_hp")
    assert "--core" in err.value.message
    assert err.value.exit_code == ExitCode.VALIDATION_FAILURE


# --------------------------------------------------------------------------
# `--output`
# --------------------------------------------------------------------------


def test_output_requires_exactly_one_target():
    """`--output` names ONE file. Against the default/--all set it would mean
    "write this path eight times, last emit wins" -- a silent wrong answer, so
    it is refused with a code and exit 2."""
    for targets in (ALL_EMIT_MODES, ("zephyr-conf", "cmake-args")):
        with pytest.raises(GenerateError) as err:
            _resolve_output_override("out.conf", targets, Path("/ws"))
        assert err.value.code == "generate.invalid-target"
        assert err.value.exit_code == ExitCode.VALIDATION_FAILURE


def test_output_is_resolved_against_the_workspace_when_relative():
    ws = Path("/ws").resolve()
    assert _resolve_output_override("sub/out.conf", ("zephyr-conf",), ws) == (
        ws / "sub" / "out.conf"
    )
    absolute = (Path("/elsewhere") / "out.conf").resolve()
    assert _resolve_output_override(str(absolute), ("zephyr-conf",), ws) == absolute


def test_resolving_output_touches_nothing_on_disk(tmp_path):
    """Path resolution has to stay side-effect-free: the overlay guard runs
    AFTER it and asks whether the destination already exists. A resolve that
    created the file would make that guard fire on its own handiwork."""
    _resolve_output_override("out.conf", ("zephyr-conf",), tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_ensure_writable_creates_the_missing_parent(tmp_path):
    """`alp_sdk_zephyr_conf()` asks for `<build>/generated/alp.conf` in a build
    tree where `generated/` does not exist yet."""
    target = tmp_path / "generated" / "alp.conf"
    _ensure_writable(target, "zephyr-conf")
    assert target.parent.is_dir()




def test_ensure_writable_does_not_truncate_an_existing_file(tmp_path):
    """The probe opens for APPEND, not write: a later guard can still refuse the
    run, and a destroyed destination would be unrecoverable by then."""
    target = tmp_path / "alp.conf"
    target.write_text("previous\n", encoding="utf-8")
    _ensure_writable(target, "zephyr-conf")
    assert target.read_text(encoding="utf-8") == "previous\n"


@pytest.mark.parametrize("kind", ["directory", "read-only", "embedded-nul"])
def test_an_unwritable_output_is_write_failure_3_not_internal_5(tmp_path, kind):
    """Every shape of unwritable destination is `generate.output-unwritable`
    with exit 3. Exit 5 would be the bug: an InternalFailure says "tan broke",
    and the recurring defect in this port has been exactly this -- an unforeseen
    exception escaping into the catch-all instead of being coded.

    `embedded-nul` is the one that raises `ValueError`, not `OSError`, which is
    why `_ensure_writable` catches both."""
    if kind == "directory":
        target = tmp_path / "adir"
        target.mkdir()
    elif kind == "read-only":
        target = tmp_path / "ro.conf"
        target.write_text("x", encoding="utf-8")
        target.chmod(0o444)
    else:
        target = Path(str(tmp_path / "bad\x00name.conf"))

    with pytest.raises(GenerateError) as err:
        _ensure_writable(target, "zephyr-conf")
    assert err.value.code == "generate.output-unwritable"
    assert err.value.exit_code == ExitCode.WRITE_FAILURE


def test_the_overlay_guard_follows_an_output_override(tmp_path):
    """The FAILING case a guard that only ever looked at the default path
    produces: `--target native-sim-overlay --output <elsewhere>` refuses because
    of an untouched `boards/native_sim_native_64.overlay`, and silently
    truncates the file it was actually pointed at."""
    (tmp_path / "boards").mkdir()
    (tmp_path / "boards" / "native_sim_native_64.overlay").write_text(
        "tuned", encoding="utf-8"
    )
    elsewhere = tmp_path / "build" / "elsewhere.overlay"
    assert not _overlay_would_overwrite(
        tmp_path, ("native-sim-overlay",), False, elsewhere
    )
    elsewhere.parent.mkdir(parents=True)
    elsewhere.write_text("also tuned", encoding="utf-8")
    assert _overlay_would_overwrite(
        tmp_path, ("native-sim-overlay",), False, elsewhere
    )


def _project_with_echo_sdk(tmp_path):
    """A project plus a stand-in `alp_project.py` that writes whatever
    `--output` names, so the plumbing can be tested without a real SDK."""
    project = tmp_path / "project"
    project.mkdir()
    (project / "board.yaml").write_text("som:\n  sku: E1M-AEN801\n", encoding="utf-8")
    sdk = make_sdk(tmp_path / "alp-sdk")
    (sdk / "scripts" / "alp_project.py").write_text(
        "import sys, pathlib\n"
        "out = pathlib.Path(sys.argv[sys.argv.index('--output') + 1])\n"
        "out.parent.mkdir(parents=True, exist_ok=True)\n"
        "out.write_text('emitted\\n', encoding='utf-8', newline='')\n",
        encoding="utf-8",
    )
    return project, sdk


def test_output_redirects_the_emit_and_is_reported_verbatim(
    tmp_path, monkeypatch, capsys
):
    """The whole point: CMake names the destination, the emit lands there, and
    `written[]` reports that path rather than the default one."""
    project, sdk = _project_with_echo_sdk(tmp_path)
    monkeypatch.chdir(project)
    monkeypatch.setattr(generate_cmd, "_planner_python", lambda *_a, **_k: sys.executable)
    destination = tmp_path / "cmake-binary-dir" / "generated" / "alp.conf"

    code = _call_generate(
        target="zephyr-conf",
        core="m55_hp",
        sdk_root=str(sdk),
        output=str(destination),
        output_format="json",
    )
    assert code == 0
    env = json.loads(capsys.readouterr().out.strip())
    assert env["data"]["written"] == [str(destination)]
    assert destination.read_bytes() == b"emitted\n"
    # And nothing landed at the default path it would have used without the flag.
    assert not (project / "build" / "generated" / "alp.conf").exists()


def test_output_writes_a_file_and_never_puts_its_contents_on_stdout(
    tmp_path, monkeypatch, capsys
):
    """The hard constraint: stdout carries exactly one JSON envelope. A
    `--output` that also echoed the artefact would give the extension two
    documents to parse, and it renders nothing at all."""
    project, sdk = _project_with_echo_sdk(tmp_path)
    monkeypatch.chdir(project)
    monkeypatch.setattr(generate_cmd, "_planner_python", lambda *_a, **_k: sys.executable)

    _call_generate(
        target="zephyr-conf",
        core="m55_hp",
        sdk_root=str(sdk),
        output=str(tmp_path / "out.conf"),
        output_format="json",
    )
    stdout = capsys.readouterr().out
    assert json.loads(stdout.strip())  # exactly one document, and it parses
    assert "emitted" not in stdout


def test_output_with_the_default_target_set_is_exit_2_end_to_end(tmp_path):
    project, sdk = _project_with_echo_sdk(tmp_path)
    proc = run_cli(
        ["--sdk-root", str(sdk), "--output", str(tmp_path / "x.conf"),
         "--format", "json"],
        cwd=project,
    )
    assert proc.returncode == 2
    env = envelope_of(proc)
    assert env["issues"][0]["code"] == "generate.invalid-target"
    assert env["data"]["written"] == []


def test_an_unwritable_output_is_exit_3_end_to_end(tmp_path):
    """Through the real process, because that is where an escaping exception
    would show up as a traceback on stdout instead of an envelope."""
    project, sdk = _project_with_echo_sdk(tmp_path)
    a_directory = tmp_path / "adir"
    a_directory.mkdir()
    proc = run_cli(
        ["--sdk-root", str(sdk), "--target", "zephyr-conf", "--core", "m55_hp",
         "--output", str(a_directory), "--format", "json"],
        cwd=project,
    )
    assert proc.returncode == 3
    assert envelope_of(proc)["issues"][0]["code"] == "generate.output-unwritable"


def test_a_board_yaml_that_fails_to_load_is_still_an_envelope(tmp_path):
    """A board.yaml the SDK cannot parse: the SDK's own diagnostic becomes the
    issue message, exit 3, and stdout stays a single envelope."""
    project = tmp_path / "project"
    project.mkdir()
    (project / "board.yaml").write_text("som:\n\tsku: [unclosed\n", encoding="utf-8")
    sdk = make_sdk(tmp_path / "alp-sdk")
    (sdk / "scripts" / "alp_project.py").write_text(
        "import sys\nsys.stderr.write('alp_project: failed to parse\\n')\n"
        "sys.exit(1)\n",
        encoding="utf-8",
    )
    proc = run_cli(
        ["--sdk-root", str(sdk), "--target", IPC_CONTRACT_H,
         "--output", str(tmp_path / "system_ipc.h"), "--format", "json"],
        cwd=project,
    )
    assert proc.returncode == 3
    env = envelope_of(proc)
    assert env["issues"][0]["code"] == "generate.emit-failed"
    assert env["data"]["failed"] == [IPC_CONTRACT_H]


# --------------------------------------------------------------------------
# The flags `cmake/alp.cmake` passes alongside `--output`
# --------------------------------------------------------------------------


def test_the_cmake_invocation_shape_is_accepted_whole(tmp_path):
    """`cmake/alp.cmake` builds ONE argv, and an unknown flag anywhere in it is a
    Click usage error that fails the entire CMake configure -- so the shape is
    pinned here verbatim, `--non-interactive` and `--quiet` included."""
    project, sdk = _project_with_echo_sdk(tmp_path)
    destination = tmp_path / "cmake-binary-dir" / "generated" / "alp.conf"
    proc = run_cli(
        ["--target", "zephyr-conf",
         "--board-yaml", str(project / "board.yaml"),
         "--sdk-root", str(sdk),
         "--output", str(destination),
         "--core", "m55_hp",
         "--non-interactive", "--quiet"],
        cwd=project,
    )
    assert proc.returncode == 0, proc.stderr
    assert destination.read_bytes() == b"emitted\n"


def test_quiet_drops_the_summary_but_never_an_issue(tmp_path):
    """A `--quiet` that swallowed the reason would put CMake back where
    `cmake/alp.cmake` found it: a bare `rv=1` with the diagnostic thrown away."""
    project, sdk = _project_with_echo_sdk(tmp_path)
    (sdk / "scripts" / "alp_project.py").write_text(
        "import sys\nsys.stderr.write('alp_project: kaboom\\n')\nsys.exit(1)\n",
        encoding="utf-8",
    )
    loud = run_cli(["--sdk-root", str(sdk), "--target", "cmake-args"], cwd=project)
    quiet = run_cli(
        ["--sdk-root", str(sdk), "--target", "cmake-args", "--quiet"], cwd=project
    )
    assert loud.returncode == quiet.returncode == 3
    assert "wrote 0/1" in loud.stderr
    assert "wrote 0/1" not in quiet.stderr
    assert "kaboom" in quiet.stderr


# --------------------------------------------------------------------------
# Which engine ran, and saying so
#
# `tan generate` renders every one of its twelve targets with `tan.planner`
# in-process; spawning alp-sdk is now only an escape hatch
# (`TAN_GENERATE_EXECUTOR=subprocess`) or a reported fallback. The bytes are
# pinned by `tests/parity/test_planner_emit_parity.py` against a real checkout;
# what these cover is the CHOICE -- that it is reported, and that a fallback is
# never silent. A silent one is how the relocation got believed before it
# happened.
# --------------------------------------------------------------------------


def _stub_in_process(monkeypatch, *, available=True, text="in-process\n"):
    """Make the in-process engine usable without an alp-sdk checkout."""
    monkeypatch.setattr(
        generate_cmd.planner_emit, "unavailable",
        lambda root: None if available else "no planner here",
    )
    monkeypatch.setattr(
        generate_cmd.planner_emit, "render",
        lambda mode, **kw: text,
    )


def test_the_engine_split_is_reported_per_target(tmp_path, monkeypatch, capsys):
    """`data.engine` names the engine for every target, so a caller can tell a
    relocated emit from a spawned one without guessing."""
    project, sdk = _project_with_echo_sdk(tmp_path)
    monkeypatch.chdir(project)
    monkeypatch.setattr(generate_cmd, "_planner_python", lambda *_a, **_k: sys.executable)
    _stub_in_process(monkeypatch)

    assert _call_generate(sdk_root=str(sdk), all_targets=True,
                          output_format="json") == 0
    env = json.loads(capsys.readouterr().out.strip())
    engine = env["data"]["engine"]
    assert set(engine) == set(ALL_EMIT_MODES)
    assert {m for m, e in engine.items() if e == "in-process"} == (
        set(ALL_EMIT_MODES) & generate_cmd.planner_emit.IN_PROCESS_MODES)
    # And they really went through the in-process writer, not the echo SDK the
    # fixture puts on disk (which writes `emitted\n`).
    for name in ("alp.conf", "alp.overlay", "alp_hw_info_build.h",
                 "alp-west-libs.yml", "carrier-netlist.json"):
        assert (project / "build" / "generated" / name).read_bytes() == b"in-process\n"


def _project_with_echo_sdk_that_writes_a_real_overlay_banner(tmp_path):
    """Like `_project_with_echo_sdk`, except its `--emit native-sim-overlay`
    writes the same banner the real `native_sim.py` renderer opens every
    overlay with, so a re-run through the subprocess engine can be told apart
    from a hand edit exactly as it would against the real SDK."""
    project = tmp_path / "project"
    project.mkdir()
    (project / "board.yaml").write_text("som:\n  sku: E1M-AEN801\n", encoding="utf-8")
    sdk = make_sdk(tmp_path / "alp-sdk")
    (sdk / "scripts" / "alp_project.py").write_text(
        "import sys, pathlib\n"
        "out = pathlib.Path(sys.argv[sys.argv.index('--output') + 1])\n"
        "out.parent.mkdir(parents=True, exist_ok=True)\n"
        "mode = sys.argv[sys.argv.index('--emit') + 1]\n"
        "banner = '/* --emit native-sim-overlay -- do not edit by hand. */\\n'\n"
        "out.write_text(banner if mode == 'native-sim-overlay' else 'emitted\\n', "
        "encoding='utf-8', newline='')\n",
        encoding="utf-8",
    )
    return project, sdk


def test_all_is_rerunnable_when_the_overlay_already_exists(tmp_path, monkeypatch, capsys):
    """tan-cli#457: `tan generate --all` used to succeed once (`wrote 9/9`,
    exit 0) and then refuse the WHOLE run forever after (exit 3, `data`
    empty) -- purely because `boards/native_sim_native_64.overlay` existed
    from the first run. Nothing was hand-edited; the other eight default
    targets were always freely rewritten on a re-run. `--all` must treat all
    nine the same way -- re-runnable, not a permanent refusal after the first
    lap of the normal edit-board.yaml / generate / build loop.
    """
    project, sdk = _project_with_echo_sdk_that_writes_a_real_overlay_banner(tmp_path)
    monkeypatch.chdir(project)
    monkeypatch.setattr(generate_cmd, "_planner_python", lambda *_a, **_k: sys.executable)
    monkeypatch.setenv(generate_cmd.EXECUTOR_ENV, "subprocess")

    first = _call_generate(all_targets=True, sdk_root=str(sdk), output_format="json")
    env1 = json.loads(capsys.readouterr().out.strip())
    assert first == 0
    assert len(env1["data"]["written"]) == 9
    assert env1["data"]["failed"] == []

    # Nothing touched in between -- the exact tan-cli#457 reproduce shape.
    second = _call_generate(all_targets=True, sdk_root=str(sdk), output_format="json")
    env2 = json.loads(capsys.readouterr().out.strip())
    assert second == 0
    assert env2["data"]["failed"] == []
    assert len(env2["data"]["written"]) == 9
    assert env2["issues"] == []
    # The overlay itself was really rewritten again, not silently skipped --
    # it agrees with the other eight rather than being left stale forever.
    assert _OVERLAY_BANNER in (
        project / "boards" / "native_sim_native_64.overlay"
    ).read_text(encoding="utf-8")


def test_all_refuses_when_the_overlay_is_hand_edited(tmp_path, monkeypatch, capsys):
    """The flip side, pinned so the tan-cli#457 fix cannot silently widen into
    the regression it would otherwise reintroduce: a `boards/
    native_sim_native_64.overlay` that lacks tan's own banner is a real hand
    edit, and `--all` refuses the WHOLE run for it exactly as an explicit
    `--target native-sim-overlay` already does (`test_overlay_overwrite_is_
    exit_3`) -- it is not silently truncated."""
    project, sdk = _project_with_echo_sdk(tmp_path)
    (project / "boards").mkdir()
    (project / "boards" / "native_sim_native_64.overlay").write_text(
        "tuned", encoding="utf-8"
    )
    monkeypatch.chdir(project)
    monkeypatch.setattr(generate_cmd, "_planner_python", lambda *_a, **_k: sys.executable)
    monkeypatch.setenv(generate_cmd.EXECUTOR_ENV, "subprocess")

    code = _call_generate(all_targets=True, sdk_root=str(sdk), output_format="json")
    env = json.loads(capsys.readouterr().out.strip())
    assert code == 3
    assert env["issues"][0]["code"] == "generate.would-overwrite"
    assert env["data"]["written"] == []
    # The hand-tuned file is untouched, not truncated.
    assert (project / "boards" / "native_sim_native_64.overlay").read_text() == "tuned"


def test_every_reachable_target_renders_in_process():
    """The claim this whole change exists to make: `tan generate` serves all
    TWELVE targets without alp-sdk's Python.

    Pinned as an equality against the reachable set, not a subset check -- a
    subset check passes just as happily when a mode quietly goes back to being
    spawned, which is exactly the regression this guards. `test_planner_emit_
    parity.py` measures the closure that proves the claim; this one keeps the
    intent from drifting.
    """
    reachable = set(ALL_EMIT_MODES) | EXPLICIT_ONLY_TARGETS
    assert len(reachable) == 12
    assert generate_cmd.planner_emit.IN_PROCESS_MODES == reachable
    # `zephyr-board` is the one that writes a DIRECTORY, so it must be the one
    # (and only) member of TREE_MODES -- `render()` refuses it by design.
    assert generate_cmd.planner_emit.TREE_MODES == frozenset({ZEPHYR_BOARD})
    assert generate_cmd.planner_emit.TREE_MODES <= reachable


def test_a_fallback_to_spawning_is_reported_not_silent(
    tmp_path, monkeypatch, capsys
):
    project, sdk = _project_with_echo_sdk(tmp_path)
    monkeypatch.chdir(project)
    monkeypatch.setattr(generate_cmd, "_planner_python", lambda *_a, **_k: sys.executable)
    _stub_in_process(monkeypatch, available=False)

    assert _call_generate(target="zephyr-conf", core="m55_hp",
                         sdk_root=str(sdk), output_format="json") == 0
    env = json.loads(capsys.readouterr().out.strip())
    assert env["data"]["engine"] == {"zephyr-conf": "subprocess"}
    assert [i["code"] for i in env["issues"]] == ["generate.in-process-unavailable"]
    assert env["issues"][0]["severity"] == "warning"
    assert "no planner here" in env["issues"][0]["message"]


def test_the_fallback_notice_never_displaces_a_target_failure(
    tmp_path, monkeypatch, capsys
):
    """`issues[0]` stays the thing the user asked about. The notice is context
    about HOW the run went, so it is appended, not prepended."""
    project, sdk = _project_with_echo_sdk(tmp_path)
    (sdk / "scripts" / "alp_project.py").write_text(
        "import sys\nsys.stderr.write('alp_project: kaboom\\n')\nsys.exit(1)\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(project)
    monkeypatch.setattr(generate_cmd, "_planner_python", lambda *_a, **_k: sys.executable)
    _stub_in_process(monkeypatch, available=False)

    assert _call_generate(target="cmake-args", sdk_root=str(sdk),
                         output_format="json") == 3
    env = json.loads(capsys.readouterr().out.strip())
    assert [i["code"] for i in env["issues"]] == [
        "generate.emit-failed", "generate.in-process-unavailable"]


def test_pinning_the_subprocess_engine_is_a_choice_not_a_fallback(
    tmp_path, monkeypatch, capsys
):
    """`TAN_GENERATE_EXECUTOR=subprocess` reports itself through `data.engine`
    and nothing else -- warning about a path the caller asked for is noise."""
    project, sdk = _project_with_echo_sdk(tmp_path)
    monkeypatch.chdir(project)
    monkeypatch.setattr(generate_cmd, "_planner_python", lambda *_a, **_k: sys.executable)
    _stub_in_process(monkeypatch)  # available, and deliberately not used
    monkeypatch.setenv(generate_cmd.EXECUTOR_ENV, "subprocess")

    assert _call_generate(target="zephyr-conf", core="m55_hp",
                         sdk_root=str(sdk), output_format="json") == 0
    env = json.loads(capsys.readouterr().out.strip())
    assert env["data"]["engine"] == {"zephyr-conf": "subprocess"}
    assert env["issues"] == []
    assert (project / "build" / "generated" / "alp.conf").read_bytes() == b"emitted\n"


def test_pinning_the_in_process_engine_refuses_instead_of_falling_back(
    tmp_path, monkeypatch, capsys
):
    """The seam the parity suite needs: with `=in-process`, a run that could
    only have been served by spawning fails loudly rather than measuring the
    wrong engine and reporting it as proof."""
    project, sdk = _project_with_echo_sdk(tmp_path)
    monkeypatch.chdir(project)
    _stub_in_process(monkeypatch, available=False)
    monkeypatch.setenv(generate_cmd.EXECUTOR_ENV, "in-process")

    assert _call_generate(target="zephyr-conf", core="m55_hp",
                         sdk_root=str(sdk), output_format="json") == 1
    env = json.loads(capsys.readouterr().out.strip())
    assert env["issues"][0]["code"] == "generate.in-process-unavailable"
    assert "no planner here" in env["issues"][0]["message"]
    assert "engine" not in env["data"]


def test_an_unknown_executor_is_a_validation_failure(tmp_path, monkeypatch, capsys):
    project, sdk = _project_with_echo_sdk(tmp_path)
    monkeypatch.chdir(project)
    monkeypatch.setenv(generate_cmd.EXECUTOR_ENV, "in_process")

    assert _call_generate(target="zephyr-conf", core="m55_hp",
                         sdk_root=str(sdk), output_format="json") == 2
    env = json.loads(capsys.readouterr().out.strip())
    assert env["issues"][0]["code"] == "generate.invalid-executor"


def test_a_refusal_carries_no_engine_key(tmp_path):
    """`contract/envelopes/generate-board-yaml-missing` freezes the refusal
    payload shape for both harnesses; an `engine: {}` there would break it while
    reporting nothing."""
    proc = run_cli(["--format", "json"], cwd=tmp_path)
    assert proc.returncode == 2
    assert "engine" not in envelope_of(proc)["data"]


def test_an_in_process_planner_exception_is_an_issue_not_a_traceback(
    tmp_path, monkeypatch, capsys
):
    """The widened defect surface: in a subprocess a planner blow-up arrived as
    an exit code, and in-process it lands in THIS process."""
    project, sdk = _project_with_echo_sdk(tmp_path)
    monkeypatch.chdir(project)
    _stub_in_process(monkeypatch)

    def boom(mode, **kwargs):
        raise ValueError("the Ethos-U sizing path does this")

    monkeypatch.setattr(generate_cmd.planner_emit, "render", boom)
    assert _call_generate(target="os-topology", sdk_root=str(sdk),
                         output_format="json") == 3
    env = json.loads(capsys.readouterr().out.strip())
    assert env["issues"][0]["code"] == "generate.emit-failed"
    assert "ValueError" in env["issues"][0]["message"]
    assert env["data"]["failed"] == ["os-topology"]


def test_an_in_process_sys_exit_is_an_issue_not_a_dead_cli(
    tmp_path, monkeypatch, capsys
):
    """`tan.planner.loader` calls `sys.exit()` on a missing PyYAML/jsonschema.
    `SystemExit` is not an `Exception`, so a bare `except Exception` would let it
    tear down the CLI past every envelope guarantee."""
    project, sdk = _project_with_echo_sdk(tmp_path)
    monkeypatch.chdir(project)
    _stub_in_process(monkeypatch)

    def exits(mode, **kwargs):
        raise SystemExit("alp_orchestrate: PyYAML is required.")

    monkeypatch.setattr(generate_cmd.planner_emit, "render", exits)
    assert _call_generate(target="os-topology", sdk_root=str(sdk),
                         output_format="json") == 3
    env = json.loads(capsys.readouterr().out.strip())
    assert env["issues"][0]["code"] == "generate.emit-failed"
    assert "exited early" in env["issues"][0]["message"]


def test_a_fully_in_process_run_never_probes_the_path_interpreter(
    tmp_path, monkeypatch, capsys
):
    """The in-process engine IS the interpreter, so refusing a run because the
    PATH `python` is old -- at the cost of a subprocess to find out -- would be
    both wrong and slow."""
    project, sdk = _project_with_echo_sdk(tmp_path)
    monkeypatch.chdir(project)
    _stub_in_process(monkeypatch)

    def never(python):
        raise AssertionError("the PATH interpreter was probed anyway")

    monkeypatch.setattr(generate_cmd, "_python_too_old", never)
    assert _call_generate(target="ipc-contract-h", sdk_root=str(sdk),
                         output_format="json") == 0
    assert json.loads(capsys.readouterr().out.strip())["data"]["engine"] == {
        "ipc-contract-h": "in-process"}


# --------------------------------------------------------------------------
# tan-cli#397: a zero exit code is evidence the emitter RAN, never evidence of
# what it produced. `written[]` is the only signal the extension has for what a
# generate landed, so it must come from disk, not from a child's exit status.
# --------------------------------------------------------------------------


def _sdk_whose_emitter_writes_nothing(tmp_path):
    """A checkout whose `alp_project.py` exits 0 and produces no file -- the
    shape a partially cloned SDK, a differently configured emitter or a future
    `--emit` path presents. `make_sdk`'s own empty script does exactly this;
    spelled out here so the test reads as what it measures."""
    sdk = make_sdk(tmp_path / "alp-sdk")
    (sdk / "scripts" / "alp_project.py").write_text(
        "import sys\nsys.exit(0)\n", encoding="utf-8"
    )
    return sdk


def _project_for(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "board.yaml").write_text("som:\n  sku: E1M-AEN801\n", encoding="utf-8")
    return project


@pytest.mark.parametrize("how", ["pinned", "fallback"])
def test_an_emitter_that_exits_0_without_writing_is_a_failed_target(
    tmp_path, monkeypatch, capsys, how
):
    """Both ways the spawned engine is reached, because this was never confined
    to the `TAN_GENERATE_EXECUTOR=subprocess` escape hatch: the default `auto`
    engine falls back to the identical `_emit_one` whenever the checkout cannot
    serve the in-process planner, and reported the same untrue `written[]`.

    The FAILING case before the fix: `ok: true`, exit 0, `failed: []`, and
    `written: ["build/generated/alp.conf"]` for a file that is not on disk.
    """
    project = _project_for(tmp_path)
    sdk = _sdk_whose_emitter_writes_nothing(tmp_path)
    monkeypatch.chdir(project)
    monkeypatch.setattr(generate_cmd, "_planner_python", lambda *_a, **_k: sys.executable)
    if how == "pinned":
        _stub_in_process(monkeypatch)  # usable, and deliberately not used
        monkeypatch.setenv(generate_cmd.EXECUTOR_ENV, "subprocess")
    else:
        _stub_in_process(monkeypatch, available=False)  # `auto` falls back

    code = _call_generate(
        target="zephyr-conf", core="m55_hp", sdk_root=str(sdk), output_format="json"
    )

    assert code == int(ExitCode.WRITE_FAILURE)
    env = json.loads(capsys.readouterr().out.strip())
    assert env["ok"] is False
    assert env["data"]["written"] == []
    assert env["data"]["failed"] == ["zephyr-conf"]
    assert env["data"]["engine"] == {"zephyr-conf": "subprocess"}
    # The failure is the FIRST issue in both shapes: under `fallback` the
    # `generate.in-process-unavailable` warning is appended behind it, and a
    # warning is not what the user needs to read first here.
    assert env["issues"][0]["code"] == "generate.emit-failed"
    assert env["issues"][0]["severity"] == "error"
    assert not (project / "build" / "generated" / "alp.conf").exists()




def test_an_empty_board_directory_is_not_a_successful_zephyr_board_emit(
    tmp_path, monkeypatch, capsys
):
    """The tree target needs its own rule: its `--output` IS a directory, and
    `_ensure_writable` creates it, so "the path exists" is true before the
    emitter runs. At least one file inside is the weakest honest check."""
    project = _project_for(tmp_path)
    sdk = _sdk_whose_emitter_writes_nothing(tmp_path)
    monkeypatch.chdir(project)
    monkeypatch.setattr(generate_cmd, "_planner_python", lambda *_a, **_k: sys.executable)
    monkeypatch.setenv(generate_cmd.EXECUTOR_ENV, "subprocess")
    destination = tmp_path / "build" / "boards" / "alp_e1m_aen801_m55_hp"

    code = _call_generate(
        target=ZEPHYR_BOARD,
        core="m55_hp",
        sdk_root=str(sdk),
        output=str(destination),
        output_format="json",
    )

    assert code == int(ExitCode.WRITE_FAILURE)
    env = json.loads(capsys.readouterr().out.strip())
    assert env["data"]["written"] == []
    assert env["data"]["failed"] == [ZEPHYR_BOARD]
    assert env["issues"][0]["code"] == "generate.emit-failed"
    assert destination.is_dir() and not any(destination.iterdir())


# --------------------------------------------------------------------------
# `tan.planner_emit.write`: the newline trap
# --------------------------------------------------------------------------


def test_the_in_process_writer_keeps_lf_on_every_host(tmp_path):
    """Windows text mode would CRLF every emit, and `alp_project.py`'s own
    `_write_or_print` passes `newline=""` for exactly this reason. A CRLF
    `alp.conf` flips every emit-snapshot golden in alp-sdk."""
    from tan import planner_emit

    destination = tmp_path / "deep" / "alp.conf"
    planner_emit.write("CONFIG_A=y\nCONFIG_B=y\n", destination)
    assert destination.read_bytes() == b"CONFIG_A=y\nCONFIG_B=y\n"


# --------------------------------------------------------------------------
# tan-cli#325: the default output path must not follow a symlink out of the
# project, and must not report the logical in-project path as written when
# it would have.
# --------------------------------------------------------------------------

WINDOWS = os.name == "nt"


def _make_dir_link(link: Path, target: Path) -> bool:
    """A directory link `link` -> `target`: a Windows JUNCTION (no elevated
    privilege needed, unlike a real symlink) or a POSIX symlink. `False` when
    the host refuses to make one at all (policy-dependent) -- the same
    tradeoff `test_clean_command.py` already makes for the same reason."""
    target.mkdir(parents=True, exist_ok=True)
    if WINDOWS:
        made = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
        )
        return made.returncode == 0
    link.symlink_to(target, target_is_directory=True)
    return True


def test_default_output_refuses_a_write_through_a_symlinked_build_dir(
    tmp_path, monkeypatch, capsys
):
    """The exact tan-cli#325 repro: `<project>/build` is a pre-existing
    directory link to somewhere outside the project. Before the fix this
    followed the link, wrote `<outside>/generated/alp-cmake-args.txt`, and
    still reported `ok: true` with the logical in-project path. Now it must
    refuse before ever spawning the emitter, and nothing may land outside."""
    project = tmp_path / "project"
    outside = tmp_path / "outside"
    project.mkdir()
    (project / "board.yaml").write_text("som:\n  sku: E1M-AEN801\n", encoding="utf-8")
    if not _make_dir_link(project / "build", outside):
        pytest.skip("cannot create a directory link on this host")
    sdk = make_sdk(tmp_path / "alp-sdk")
    monkeypatch.chdir(project)
    monkeypatch.setattr(generate_cmd, "_planner_python", lambda *_a, **_k: sys.executable)

    code = _call_generate(target="cmake-args", sdk_root=str(sdk), output_format="json")

    assert code == int(ExitCode.WRITE_FAILURE)
    env = json.loads(capsys.readouterr().out.strip())
    assert env["ok"] is False
    assert env["issues"][0]["code"] == "generate.write-escapes-project"
    # The filesystem outcome, not just the message: nothing landed outside
    # the project, through the link or otherwise.
    assert not any(outside.rglob("*"))
    assert not (project / "build" / "generated").exists()


def test_default_output_still_works_when_the_project_root_itself_is_a_symlink(
    tmp_path, monkeypatch
):
    """The fix must not be over-tightened: a project reached THROUGH a
    symlinked root is still a legitimate write, because the write still lands
    inside the resolved root."""
    real_project = tmp_path / "real_project"
    real_project.mkdir()
    (real_project / "board.yaml").write_text("som:\n  sku: E1M-AEN801\n", encoding="utf-8")
    link = tmp_path / "project_link"
    if not _make_dir_link(link, real_project):
        pytest.skip("cannot create a directory link on this host")
    sdk = make_sdk(tmp_path / "alp-sdk")
    (sdk / "scripts" / "alp_project.py").write_text(
        "import sys, pathlib\n"
        "out = pathlib.Path(sys.argv[sys.argv.index('--output') + 1])\n"
        "out.parent.mkdir(parents=True, exist_ok=True)\n"
        "out.write_text('generated\\n', encoding='utf-8')\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(generate_cmd, "_planner_python", lambda *_a, **_k: sys.executable)

    code = _call_generate(
        target="cmake-args", project=str(link), sdk_root=str(sdk), output_format="text"
    )

    assert code == 0
    written = real_project / "build" / "generated" / "alp-cmake-args.txt"
    assert written.read_text(encoding="utf-8") == "generated\n"


def test_an_explicit_output_override_is_exempt_from_the_confinement_guard(
    tmp_path, monkeypatch
):
    """`--output` names the caller's OWN destination (CMake decides where a
    Zephyr build reads from, not tan) -- an explicit out-of-project path there
    is honoured, not refused as an accidental escape. This is pre-existing,
    documented behaviour (see the module docstring's `--output` section); the
    new guard must not touch it."""
    project = tmp_path / "project"
    project.mkdir()
    (project / "board.yaml").write_text("som:\n  sku: E1M-AEN801\n", encoding="utf-8")
    outside = tmp_path / "outside" / "alp-cmake-args.txt"
    sdk = make_sdk(tmp_path / "alp-sdk")
    (sdk / "scripts" / "alp_project.py").write_text(
        "import sys, pathlib\n"
        "out = pathlib.Path(sys.argv[sys.argv.index('--output') + 1])\n"
        "out.parent.mkdir(parents=True, exist_ok=True)\n"
        "out.write_text('generated\\n', encoding='utf-8')\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(project)
    monkeypatch.setattr(generate_cmd, "_planner_python", lambda *_a, **_k: sys.executable)

    code = _call_generate(
        target="cmake-args", sdk_root=str(sdk), output=str(outside), output_format="text"
    )

    assert code == 0
    assert outside.read_text(encoding="utf-8") == "generated\n"


# --------------------------------------------------------------------------
# tan-cli#397: a zero exit from the SPAWNED emitter is not proof a file landed
#
# `_emit_one` used to return `None` -- this command's spelling of "this target
# succeeded" -- on `returncode == 0` alone, and `written[]` was built from that
# `None`. Nothing ever looked at the destination. A checkout whose
# `scripts/alp_project.py` exits 0 down a path tan did not expect (a partially
# installed clone, a differently configured emitter, a future SDK version) then
# produced `ok: true`, exit 0, `failed: []` and a `written[]` naming a file that
# is not on disk -- and `cmake/alp.cmake` configures a whole Zephyr build from
# that answer.
#
# The in-process engine was never affected: `_emit_one_in_process` performs the
# write itself, so its `None` is earned.
# --------------------------------------------------------------------------


def _project_with_silent_sdk(tmp_path):
    """A project plus a stand-in `alp_project.py` that exits 0 having written
    nothing at all -- the partially installed checkout of tan-cli#397.

    `_project_with_echo_sdk`'s stub always writes, which is precisely why the
    existing coverage (`test_a_successful_emit_reports_the_relative_written_
    path`) could not catch this.
    """
    project = tmp_path / "project"
    project.mkdir()
    (project / "board.yaml").write_text("som:\n  sku: E1M-AEN801\n", encoding="utf-8")
    sdk = make_sdk(tmp_path / "alp-sdk")
    (sdk / "scripts" / "alp_project.py").write_text(
        "import sys\nsys.exit(0)\n", encoding="utf-8"
    )
    return project, sdk


def test_a_pinned_subprocess_emit_that_wrote_nothing_is_a_failed_target(
    tmp_path, monkeypatch, capsys
):
    """The `TAN_GENERATE_EXECUTOR=subprocess` arm: the target lands in
    `failed[]` with a `generate.emit-failed` error and exit 3, identical to
    every other failed emit -- not `ok: true` with an imaginary `written[]`."""
    project, sdk = _project_with_silent_sdk(tmp_path)
    monkeypatch.chdir(project)
    monkeypatch.setattr(generate_cmd, "_planner_python", lambda *_a, **_k: sys.executable)
    monkeypatch.setenv(generate_cmd.EXECUTOR_ENV, "subprocess")

    code = _call_generate(
        target="zephyr-conf", sdk_root=str(sdk), output_format="json"
    )

    assert code == int(ExitCode.WRITE_FAILURE)
    env = json.loads(capsys.readouterr().out.strip())
    assert env["ok"] is False
    assert env["exitCode"] == 3
    assert env["data"]["written"] == []
    assert env["data"]["failed"] == ["zephyr-conf"]
    assert env["issues"][0]["code"] == "generate.emit-failed"
    assert env["issues"][0]["severity"] == "error"
    # The filesystem outcome the envelope must agree with.
    assert not (project / "build" / "generated" / "alp.conf").exists()


def test_the_auto_fallback_to_spawning_gets_the_same_did_it_land_check(
    tmp_path, monkeypatch, capsys
):
    """This is NOT confined to the escape hatch. With no environment variable
    set, `auto` falls back to the same `_emit_one` whenever the checkout cannot
    serve the in-process planner -- the second reproduction in tan-cli#397 --
    and reported the identical untrue `written[]`.

    The fallback warning stays a warning and stays LAST: it is context about
    how the run went, and the emit failure is what the user asked about."""
    project, sdk = _project_with_silent_sdk(tmp_path)
    monkeypatch.chdir(project)
    monkeypatch.delenv(generate_cmd.EXECUTOR_ENV, raising=False)
    monkeypatch.setattr(generate_cmd, "_planner_python", lambda *_a, **_k: sys.executable)
    _stub_in_process(monkeypatch, available=False)

    code = _call_generate(
        target="zephyr-conf", sdk_root=str(sdk), output_format="json"
    )

    assert code == int(ExitCode.WRITE_FAILURE)
    env = json.loads(capsys.readouterr().out.strip())
    assert env["ok"] is False
    assert env["data"]["engine"] == {"zephyr-conf": "subprocess"}
    assert env["data"]["written"] == []
    assert env["data"]["failed"] == ["zephyr-conf"]
    assert [(i["code"], i["severity"]) for i in env["issues"]] == [
        ("generate.emit-failed", "error"),
        ("generate.in-process-unavailable", "warning"),
    ]


def test_an_output_override_the_emitter_never_filled_is_not_reported_written(
    tmp_path, monkeypatch, capsys
):
    """The `cmake/alp.cmake` shape, and the reason mere existence is not the
    test: `_ensure_writable` opens the destination for APPEND before the spawn
    to prove it is writable, which CREATES a zero-byte file. An existence-only
    check would report tan's own probe as the emitter's artefact.

    Also asserts the probe file is taken back off disk (tan-cli#420) -- the
    zero-byte-survives assertion this used to carry is flipped on purpose, see
    the comment on that assertion.
    """
    project, sdk = _project_with_silent_sdk(tmp_path)
    destination = tmp_path / "cmake-binary-dir" / "generated" / "alp.conf"
    monkeypatch.chdir(project)
    monkeypatch.setattr(generate_cmd, "_planner_python", lambda *_a, **_k: sys.executable)
    monkeypatch.setenv(generate_cmd.EXECUTOR_ENV, "subprocess")

    code = _call_generate(
        target="zephyr-conf",
        sdk_root=str(sdk),
        output=str(destination),
        output_format="json",
    )

    assert code == int(ExitCode.WRITE_FAILURE)
    env = json.loads(capsys.readouterr().out.strip())
    assert env["data"]["written"] == []
    assert env["data"]["failed"] == ["zephyr-conf"]
    assert env["issues"][0]["code"] == "generate.emit-failed"
    # tan-cli#420: the probe's own zero-byte file is REMOVED again.
    #
    # This read `destination.stat().st_size == 0` and that was a deliberate
    # assertion, not an oversight -- the envelope is honest either way. It is
    # flipped because the envelope was never the exposure. `cmake/alp.cmake`
    # hands this exact path to Zephyr as `EXTRA_CONF_FILE`, and Zephyr accepts
    # an empty conf file silently: no configuration applied, nothing said. A
    # standalone `west build` after a failed `tan generate` reaches that
    # configure with nothing re-running generate in between, and hand-written
    # firmware bypassing the tan-driven flow is first-class in this SDK.
    assert not destination.exists()
    # The PARENT survives: `_ensure_writable` had to create it either way, an
    # empty `generated/` misleads nobody, and removing directories a run did
    # not necessarily create is a much worse trade than leaving one behind.
    assert destination.parent.is_dir()


def _project_with_a_confirmed_empty_write_sdk(tmp_path):
    """tan-cli#498: a stand-in for `alp_project.py`'s REAL behaviour on an
    unscoped per-core emit whose mode's OS class matches no core in this
    project -- it writes a genuine zero-byte file and, via `_write_or_print`,
    announces exactly that on stderr before exiting 0. Unlike
    `_project_with_silent_sdk` (tan-cli#397's stand-in, which writes nothing
    and says nothing), this is the SDK confirming its own empty artefact."""
    project = tmp_path / "project"
    project.mkdir()
    (project / "board.yaml").write_text("som:\n  sku: E1M-AEN801\n", encoding="utf-8")
    sdk = make_sdk(tmp_path / "alp-sdk")
    (sdk / "scripts" / "alp_project.py").write_text(
        "import sys, pathlib\n"
        "out = pathlib.Path(sys.argv[sys.argv.index('--output') + 1])\n"
        "out.parent.mkdir(parents=True, exist_ok=True)\n"
        "out.write_text('', encoding='utf-8')\n"
        "print(f'alp_project: wrote {out} (0 bytes)', file=sys.stderr)\n",
        encoding="utf-8",
    )
    return project, sdk


def test_an_sdk_confirmed_empty_write_is_not_reported_as_a_failed_emit(
    tmp_path, monkeypatch, capsys
):
    """The false positive tan-cli#498 reports: `zephyr-conf`/`cmake-args` on a
    project touching no core of the mode's OS class legitimately write a
    zero-byte file and exit 0 -- that must land in `written[]`, not `failed[]`,
    when the SDK's own stderr confirms the empty write."""
    project, sdk = _project_with_a_confirmed_empty_write_sdk(tmp_path)
    monkeypatch.chdir(project)
    monkeypatch.setattr(generate_cmd, "_planner_python", lambda *_a, **_k: sys.executable)
    monkeypatch.setenv(generate_cmd.EXECUTOR_ENV, "subprocess")

    code = _call_generate(target="zephyr-conf", sdk_root=str(sdk), output_format="json")

    assert code == int(ExitCode.SUCCESS), capsys.readouterr().out
    env = json.loads(capsys.readouterr().out.strip())
    assert env["ok"] is True
    assert env["data"]["failed"] == []
    assert env["data"]["written"] == ["build/generated/alp.conf"]
    assert env["issues"] == []
    written = project / "build" / "generated" / "alp.conf"
    assert written.is_file()
    assert written.stat().st_size == 0


def test_an_sdk_confirmed_empty_write_with_output_override_is_kept_not_discarded(
    tmp_path, monkeypatch, capsys
):
    """The sharper failure mode tan-cli#498 names: with `--output`,
    `_ensure_writable`'s own probe already created a zero-byte file, so a false
    `generate.emit-failed` used to trigger `_discard_probe_file` and UNLINK the
    real (empty) artefact the SDK had just written. A confirmed empty write
    must survive on disk."""
    project, sdk = _project_with_a_confirmed_empty_write_sdk(tmp_path)
    destination = tmp_path / "cmake-binary-dir" / "generated" / "alp.conf"
    monkeypatch.chdir(project)
    monkeypatch.setattr(generate_cmd, "_planner_python", lambda *_a, **_k: sys.executable)
    monkeypatch.setenv(generate_cmd.EXECUTOR_ENV, "subprocess")

    code = _call_generate(
        target="zephyr-conf",
        sdk_root=str(sdk),
        output=str(destination),
        output_format="json",
    )

    assert code == int(ExitCode.SUCCESS), capsys.readouterr().out
    env = json.loads(capsys.readouterr().out.strip())
    assert env["data"]["failed"] == []
    assert env["issues"] == []
    assert destination.is_file()
    assert destination.stat().st_size == 0


def test_a_tree_target_that_left_no_board_directory_is_a_failed_target(
    tmp_path, monkeypatch, capsys
):
    """`zephyr-board`'s `--output` IS a directory, so the same question has to
    be asked of a directory rather than a file."""
    project, sdk = _project_with_silent_sdk(tmp_path)
    monkeypatch.chdir(project)
    monkeypatch.setattr(generate_cmd, "_planner_python", lambda *_a, **_k: sys.executable)
    monkeypatch.setenv(generate_cmd.EXECUTOR_ENV, "subprocess")

    code = _call_generate(
        target=ZEPHYR_BOARD, core="m55_hp", sdk_root=str(sdk), output_format="json"
    )

    assert code == int(ExitCode.WRITE_FAILURE)
    env = json.loads(capsys.readouterr().out.strip())
    assert env["data"]["written"] == []
    assert env["data"]["failed"] == [ZEPHYR_BOARD]
    assert env["issues"][0]["code"] == "generate.emit-failed"
    assert not (project / "build" / "boards" / "alp_e1m_aen801_m55_hp").exists()


def test_an_empty_board_directory_is_not_a_generated_board_tree(
    tmp_path, monkeypatch, capsys
):
    """The directory half of the pre-creation trap: `_ensure_writable` mkdirs
    a tree target's `--output` before the spawn, so the directory exists no
    matter what the emitter did. Only its CONTENTS prove an emit."""
    project, sdk = _project_with_silent_sdk(tmp_path)
    destination = tmp_path / "board-root" / "alp_e1m_aen801_m55_hp"
    monkeypatch.chdir(project)
    monkeypatch.setattr(generate_cmd, "_planner_python", lambda *_a, **_k: sys.executable)
    monkeypatch.setenv(generate_cmd.EXECUTOR_ENV, "subprocess")

    code = _call_generate(
        target=ZEPHYR_BOARD,
        core="m55_hp",
        sdk_root=str(sdk),
        output=str(destination),
        output_format="json",
    )

    assert code == int(ExitCode.WRITE_FAILURE)
    env = json.loads(capsys.readouterr().out.strip())
    assert env["data"]["written"] == []
    assert env["data"]["failed"] == [ZEPHYR_BOARD]
    assert destination.is_dir()
    assert not any(destination.iterdir())


def test_a_tree_target_that_wrote_files_is_still_a_success(
    tmp_path, monkeypatch, capsys
):
    """The guard against over-tightening: a board directory with files in it
    is a produced artefact, reported at the directory path exactly as before."""
    project, sdk = _project_with_silent_sdk(tmp_path)
    (sdk / "scripts" / "alp_project.py").write_text(
        "import sys, pathlib\n"
        "out = pathlib.Path(sys.argv[sys.argv.index('--output') + 1])\n"
        "out.mkdir(parents=True, exist_ok=True)\n"
        "(out / 'board.yml').write_text('identifier: alp_e1m_aen801_m55_hp\\n',"
        " encoding='utf-8')\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(project)
    monkeypatch.setattr(generate_cmd, "_planner_python", lambda *_a, **_k: sys.executable)
    monkeypatch.setenv(generate_cmd.EXECUTOR_ENV, "subprocess")

    code = _call_generate(
        target=ZEPHYR_BOARD, core="m55_hp", sdk_root=str(sdk), output_format="json"
    )

    assert code == 0
    env = json.loads(capsys.readouterr().out.strip())
    assert env["data"]["failed"] == []
    assert env["data"]["written"] == [
        os.path.join("build", "boards", "alp_e1m_aen801_m55_hp")
    ]


def test_a_destination_that_already_existed_is_never_discarded(
    tmp_path, monkeypatch, capsys
):
    """The tan-cli#420 cleanup must only take back the file the probe itself
    created. A destination the user already had is theirs -- deleting it on a
    failed emit would turn a refusal into data loss, which is far worse than
    the stray it is cleaning up.

    Empty on purpose: a zero-byte pre-existing file is the one case where the
    unlink-time size check cannot tell the two apart, so the decision has to
    rest on `existed`, captured before the probe opens it.
    """
    project, sdk = _project_with_silent_sdk(tmp_path)
    destination = tmp_path / "cmake-binary-dir" / "generated" / "alp.conf"
    destination.parent.mkdir(parents=True)
    destination.touch()
    monkeypatch.chdir(project)
    monkeypatch.setattr(generate_cmd, "_planner_python", lambda *_a, **_k: sys.executable)
    monkeypatch.setenv(generate_cmd.EXECUTOR_ENV, "subprocess")

    code = _call_generate(
        target="zephyr-conf",
        sdk_root=str(sdk),
        output=str(destination),
        output_format="json",
    )

    assert code == int(ExitCode.WRITE_FAILURE)
    env = json.loads(capsys.readouterr().out.strip())
    assert env["data"]["failed"] == ["zephyr-conf"]
    assert destination.exists(), "a pre-existing destination was deleted by the #420 cleanup"


def test_a_partly_written_destination_survives_a_failed_emit(tmp_path, monkeypatch, capsys):
    """The cleanup re-checks zero-byte at unlink time rather than trusting the
    probe. An emitter that wrote something and then failed leaves evidence the
    user needs to see -- that is a partial artefact, not litter."""
    project, sdk = _project_with_silent_sdk(tmp_path)
    destination = tmp_path / "cmake-binary-dir" / "generated" / "alp.conf"
    monkeypatch.chdir(project)
    monkeypatch.setattr(generate_cmd, "_planner_python", lambda *_a, **_k: sys.executable)
    monkeypatch.setenv(generate_cmd.EXECUTOR_ENV, "subprocess")

    # Writes, then reports failure -- an emitter that died partway through.
    # Deliberately NOT the real `_emit_one` after the write: the artefact
    # guard's only refusal for `zephyr-conf` is missing-or-zero-byte, so a
    # non-empty file would be reported as SUCCEEDED and this test would
    # exercise nothing. Measured: the first version of it did exactly that
    # (`ok:true`, `written:[...]`, exit 0).
    def _emit_then_fail(python, script, board, mode, output, core):
        Path(output).write_text("CONFIG_HALF_WRITTEN=y\n", encoding="utf-8")
        return "the SDK emitter exited 1 partway through"

    monkeypatch.setattr(generate_cmd, "_emit_one", _emit_then_fail)

    code = _call_generate(
        target="zephyr-conf",
        sdk_root=str(sdk),
        output=str(destination),
        output_format="json",
    )

    assert code == int(ExitCode.WRITE_FAILURE)
    capsys.readouterr()
    assert destination.exists(), "a partially written artefact was deleted"
    assert "CONFIG_HALF_WRITTEN=y" in destination.read_text(encoding="utf-8")
