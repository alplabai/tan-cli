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
    CORE_SCOPABLE_TARGETS,
    EXPLICIT_ONLY_TARGETS,
    IPC_CONTRACT_H,
    ZEPHYR_BOARD,
    GenerateError,
    _ensure_writable,
    _output_path,
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
    assert envelope_of(proc)["issues"][0]["code"] == "generate.sdk-root-unresolved"


def test_bad_target_is_a_refusal_once_the_board_and_sdk_resolve(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "board.yaml").write_text("som:\n  sku: E1M-AEN801\n", encoding="utf-8")
    sdk = make_sdk(tmp_path / "alp-sdk")
    proc = run_cli(["--sdk-root", str(sdk), "--target", "bogus", "--format", "json"], cwd=project)
    assert proc.returncode == 2
    assert envelope_of(proc)["issues"][0]["code"] == "generate.invalid-target"


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
    monkeypatch.setattr(generate_cmd, "_planner_python", lambda: sys.executable)

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
    monkeypatch.setattr(generate_cmd, "_planner_python", lambda: sys.executable)
    assert _call_generate(target="cmake-args", sdk_root=str(sdk), output_format="json") == 0
    env = json.loads(capsys.readouterr().out.strip())
    # Workspace-RELATIVE, never the absolute host path the envelope would
    # otherwise leak into a golden.
    assert env["data"]["written"] == [os.path.join("build", "generated", "alp-cmake-args.txt")]
    assert env["data"]["failed"] == []
    written = project / "build" / "generated" / "alp-cmake-args.txt"
    assert written.read_text(encoding="utf-8") == "generated\n"


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
    monkeypatch.setattr(generate_cmd, "_planner_python", lambda: sys.executable)
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
    monkeypatch.setattr(generate_cmd, "_planner_python", lambda: sys.executable)

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
# `tan generate` now renders the five relocated modes with `tan.planner`
# in-process and spawns alp-sdk only for the rest. The bytes are pinned by
# `tests/parity/test_planner_emit_parity.py` against a real checkout; what these
# cover is the CHOICE -- that it is reported, and that a fallback is never
# silent. A silent one is how the relocation got believed before it happened.
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
    monkeypatch.setattr(generate_cmd, "_planner_python", lambda: sys.executable)
    _stub_in_process(monkeypatch)

    assert _call_generate(sdk_root=str(sdk), all_targets=True,
                          output_format="json") == 0
    env = json.loads(capsys.readouterr().out.strip())
    engine = env["data"]["engine"]
    assert set(engine) == set(ALL_EMIT_MODES)
    assert {m for m, e in engine.items() if e == "in-process"} == (
        set(ALL_EMIT_MODES) & generate_cmd.planner_emit.IN_PROCESS_MODES)
    # And the relocated ones really went through the in-process writer.
    assert (project / "build" / "generated" / "alp.conf").read_bytes() == b"in-process\n"
    assert (project / "build" / "generated" / "alp.overlay").read_bytes() == b"emitted\n"


def test_the_relocated_modes_are_exactly_the_five_from_the_registry():
    """The split is a claim about alp-sdk's emit registry (`owner.module` under
    `scripts/alp_orchestrate/`), so pin it rather than let it drift."""
    assert generate_cmd.planner_emit.IN_PROCESS_MODES == frozenset({
        "zephyr-conf", "cmake-args", "yocto-conf", "os-topology",
        "ipc-contract-h",
    })
    # Every one is a target `generate` can actually reach.
    reachable = set(ALL_EMIT_MODES) | EXPLICIT_ONLY_TARGETS
    assert generate_cmd.planner_emit.IN_PROCESS_MODES <= reachable


def test_a_fallback_to_spawning_is_reported_not_silent(
    tmp_path, monkeypatch, capsys
):
    project, sdk = _project_with_echo_sdk(tmp_path)
    monkeypatch.chdir(project)
    monkeypatch.setattr(generate_cmd, "_planner_python", lambda: sys.executable)
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
    monkeypatch.setattr(generate_cmd, "_planner_python", lambda: sys.executable)
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
    monkeypatch.setattr(generate_cmd, "_planner_python", lambda: sys.executable)
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
