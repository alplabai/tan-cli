# SPDX-License-Identifier: Apache-2.0
"""`tan model build` -- board.yaml resolution, compile-option path resolution,
and the in-process `tan.model.build.build_model` call, exercised end to end.

Port of `scripts/alp_cli/model.py`'s own shape; no committed Rust `model.rs`
exists (the retired forwarder in `crates/tan-cli/src/commands/sdk_cli.rs` is
the oracle for the outer envelope contract, not the model-building logic,
which stays alp-sdk's own).

ADR-0028 relocated the compiler-adapter engine into `tan.model` and collapsed
the `python -c` driver subprocess that used to run it under a resolved SDK
checkout's own interpreter into a direct in-process call. The tests below
that need to observe what `build_model` was *called with*, or force it to
fail, monkeypatch `model_cmd.build_model` (the name `model_cmd` imported it
under) rather than spawning anything -- there is no subprocess boundary left
to spawn across.
"""
from __future__ import annotations

import json
from pathlib import Path

import typer
from typer.testing import CliRunner

from tan.commands import model_cmd
from tan.commands.model_cmd import model
from tan.model._gen_fixture import build_fixture_bytes

app = typer.Typer(add_completion=False)
app.command("model")(model)

runner = CliRunner()

#: What every `_fake_build_model` below writes. A REAL, well-formed
#: `.alpmodel` container (the committed `minimal` fixture) rather than the
#: `b"x"` / `b"stub-package"` placeholders these fakes used to drop on disk:
#: `_run_build` now reads each written package's manifest back to report the
#: compiler caveats it ships with (`model_cmd._shipped_caveat_issues`), so a
#: fake standing in for `build_model` has to produce something `build_model`
#: could actually have produced. It carries NO caveats, which is what keeps
#: these tests' `issues` lists about the thing each one is testing.
_REAL_PACKAGE_BYTES = build_fixture_bytes()


def envelope(result):
    assert result.stdout.count("\n") == 1, result.stdout
    return json.loads(result.stdout)


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="")


def make_sdk(root: Path) -> Path:
    write(root / "scripts" / "alp_project.py", "")
    return root


def board_yaml(root: Path, models: str = "") -> Path:
    path = root / "board.yaml"
    write(path, f"som:\n  sku: E1M-TEST\n{models}")
    return path


# --------------------------------------------------------------------------
# argument-shape / resolution refusals -- build_model is never reached
# --------------------------------------------------------------------------


def test_unknown_subcommand_is_a_coded_refusal(tmp_path):
    result = runner.invoke(app, ["bogus", "--format", "json"], catch_exceptions=False)
    assert result.exit_code == 1
    doc = envelope(result)
    assert doc["issues"][0]["code"] == "model.unknown-subcommand"


def test_no_sdk_resolvable_refuses(tmp_path):
    board_yaml(tmp_path)
    result = runner.invoke(
        app,
        [
            "build",
            "--project", str(tmp_path),
            "--sdk-root", str(tmp_path / "nope"),
            "--format", "json",
        ],
    )
    assert result.exit_code == 2
    doc = envelope(result)
    assert doc["issues"][0]["code"] == "model.sdk-root-unresolved"


def test_missing_board_yaml_refuses(tmp_path):
    sdk = make_sdk(tmp_path / "sdk")
    result = runner.invoke(
        app,
        [
            "build",
            "--project", str(tmp_path),
            "--sdk-root", str(sdk),
            "--format", "json",
        ],
    )
    assert result.exit_code == 2
    doc = envelope(result)
    assert doc["issues"][0]["code"] == "model.board-yaml-missing"


def test_non_utf8_board_yaml_is_a_coded_validation_refusal_not_internal_failure(tmp_path):
    """tan-cli#396: `_load_board`'s docstring promises "a `ModelError` for
    every way that can fail -- missing file, bad encoding, not YAML, not a
    mapping", but `UnicodeDecodeError` is a `ValueError`, not an `OSError`, so
    the bad-encoding half fell through to `model`'s outer catch-all instead:
    measured `exitCode: 5` / `model.internal-failure` / "model build failed
    unexpectedly: UnicodeDecodeError: 'utf-8' codec can't decode byte 0xff in
    position 32: invalid start byte". A script cannot tell "your board.yaml
    has a bad byte" from "tan broke" at exit 5."""
    sdk = make_sdk(tmp_path / "sdk")
    (tmp_path / "board.yaml").write_bytes(b"som:\n  sku: E1M-TEST\n# \xff\xfe\xfd\n")
    result = runner.invoke(
        app,
        [
            "build",
            "--project", str(tmp_path),
            "--sdk-root", str(sdk),
            "--format", "json",
        ],
    )
    assert result.exit_code == 2
    doc = envelope(result)
    assert doc["issues"][0]["code"] == "model.board-yaml-invalid"
    assert doc["issues"][0]["severity"] == "error"


def test_no_models_declared_is_a_success_no_op(tmp_path):
    sdk = make_sdk(tmp_path / "sdk")
    board_yaml(tmp_path)
    result = runner.invoke(
        app,
        [
            "build",
            "--project", str(tmp_path),
            "--sdk-root", str(sdk),
            "--format", "json",
        ],
    )
    assert result.exit_code == 0
    doc = envelope(result)
    assert doc["ok"] is True
    assert doc["data"]["built"] == []
    assert doc["data"]["sku"] == "E1M-TEST"


def test_missing_sku_refuses(tmp_path):
    sdk = make_sdk(tmp_path / "sdk")
    write(tmp_path / "board.yaml", "models: []\n")
    result = runner.invoke(
        app,
        [
            "build",
            "--project", str(tmp_path),
            "--sdk-root", str(sdk),
            "--format", "json",
        ],
    )
    assert result.exit_code == 2
    doc = envelope(result)
    assert doc["issues"][0]["code"] == "model.board-yaml-invalid"


# --------------------------------------------------------------------------
# ADR-0028: the in-process `build_model` call, not a subprocess
# --------------------------------------------------------------------------


def test_build_calls_the_in_process_engine_not_a_subprocess(tmp_path, monkeypatch):
    """ADR-0028: the engine is tan's own package. No `python -c` driver, no
    PYTHONPATH=<sdk>/scripts, no subprocess on the build path."""
    import subprocess

    def _boom(*a, **k):
        raise AssertionError("model build must not spawn a subprocess")

    monkeypatch.setattr(subprocess, "run", _boom)
    monkeypatch.setattr(subprocess, "Popen", _boom)

    sdk = make_sdk(tmp_path / "sdk")
    write(tmp_path / "source.tflite", "x")
    board_yaml(
        tmp_path,
        "models:\n  - name: mymodel\n    source: source.tflite\n",
    )

    calls = []

    def _fake_build_model(**kw):
        calls.append(kw)
        out = tmp_path / "build" / "models" / "mymodel.alpmodel"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(_REAL_PACKAGE_BYTES)
        return out

    monkeypatch.setattr(model_cmd, "build_model", _fake_build_model)

    result = runner.invoke(
        app,
        [
            "build",
            "--project", str(tmp_path),
            "--sdk-root", str(sdk),
            "--format", "json",
        ],
    )
    assert result.exit_code == 0, result.stdout
    assert len(calls) == 1
    assert calls[0]["sku"] == "E1M-TEST"
    assert calls[0]["name"] == "mymodel"
    assert calls[0]["source"] == (tmp_path / "source.tflite").resolve()
    assert calls[0]["out_dir"] == tmp_path / "build" / "models"
    assert calls[0]["metadata_root"] == sdk / "metadata"
    assert calls[0]["compile_opts"] is None


def test_build_failure_is_a_coded_issue_not_a_traceback(tmp_path, monkeypatch):
    """Deliberate divergence 1 from the oracle is PRESERVED: a per-model
    failure resolves to `model.build-failed` and the batch continues."""

    def _fail(**kw):
        raise RuntimeError("no blob compiled for model")

    monkeypatch.setattr(model_cmd, "build_model", _fail)

    sdk = make_sdk(tmp_path / "sdk")
    write(tmp_path / "one.tflite", "x")
    write(tmp_path / "two.tflite", "x")
    board_yaml(
        tmp_path,
        "models:\n"
        "  - name: one\n    source: one.tflite\n"
        "  - name: two\n    source: two.tflite\n",
    )
    result = runner.invoke(
        app,
        [
            "build",
            "--project", str(tmp_path),
            "--sdk-root", str(sdk),
            "--format", "json",
        ],
    )
    assert result.exit_code == 3  # WriteFailure -- every model failed
    doc = envelope(result)
    assert doc["ok"] is False
    codes = [i["code"] for i in doc["issues"]]
    names = [i["message"] for i in doc["issues"]]
    assert codes == ["model.build-failed", "model.build-failed"]
    assert any("'one'" in m for m in names)
    assert any("'two'" in m for m in names)
    assert all("no blob compiled" in m for m in names)
    assert doc["data"]["built"] == []


def test_a_built_model_reports_its_output_path(tmp_path, monkeypatch):
    sdk = make_sdk(tmp_path / "sdk")
    write(tmp_path / "source.tflite", "fake-tflite-bytes")
    board_yaml(
        tmp_path,
        "models:\n  - name: mymodel\n    source: source.tflite\n",
    )

    def _fake_build_model(*, out_dir, name, **kw):
        out_dir.mkdir(parents=True, exist_ok=True)
        out = out_dir / f"{name}.alpmodel"
        out.write_bytes(_REAL_PACKAGE_BYTES)
        return out

    monkeypatch.setattr(model_cmd, "build_model", _fake_build_model)

    result = runner.invoke(
        app,
        [
            "build",
            "--project", str(tmp_path),
            "--sdk-root", str(sdk),
            "--out", "build/models",
            "--format", "json",
        ],
    )
    assert result.exit_code == 0, result.stdout
    doc = envelope(result)
    assert doc["ok"] is True
    built = doc["data"]["built"]
    assert len(built) == 1
    assert built[0].endswith("mymodel.alpmodel")
    assert (tmp_path / "build" / "models" / "mymodel.alpmodel").is_file()


def test_a_failed_model_is_an_issue_not_a_traceback_and_the_batch_continues(
    tmp_path, monkeypatch
):
    sdk = make_sdk(tmp_path / "sdk")
    write(tmp_path / "good.tflite", "x")
    write(tmp_path / "bad.tflite", "x")
    board_yaml(
        tmp_path,
        "models:\n"
        "  - name: good\n    source: good.tflite\n"
        "  - name: bad\n    source: bad.tflite\n",
    )

    def _fake_build_model(*, out_dir, name, **kw):
        if name == "bad":
            raise ValueError(f"no blob compiled for model '{name}'")
        out_dir.mkdir(parents=True, exist_ok=True)
        out = out_dir / f"{name}.alpmodel"
        out.write_bytes(_REAL_PACKAGE_BYTES)
        return out

    monkeypatch.setattr(model_cmd, "build_model", _fake_build_model)

    result = runner.invoke(
        app,
        [
            "build",
            "--project", str(tmp_path),
            "--sdk-root", str(sdk),
            "--format", "json",
        ],
    )
    assert result.exit_code == 3  # WriteFailure -- at least one model failed
    doc = envelope(result)
    assert doc["data"]["built"] == [
        str(tmp_path / "build" / "models" / "good.alpmodel")
    ] or doc["data"]["built"][0].endswith("good.alpmodel")
    codes = [i["code"] for i in doc["issues"]]
    assert "model.build-failed" in codes
    assert "no blob compiled" in doc["issues"][0]["message"]


def test_a_missing_dxcom_toolchain_is_a_coded_issue_not_a_traceback_and_the_batch_continues(
    tmp_path, monkeypatch
):
    """tan-cli#253 gap (b): the DEEPX host compiler `dxcom` (not `dx_com`) is
    license-gated and, on any machine that hasn't installed it, simply
    absent from PATH. `tan.model.adapters.deepx` resolves it with
    `shutil.which("dxcom")` and raises when it can't find it -- that raise
    reaches `_run_build`'s per-model `except Exception` (module doc,
    `model_cmd.py:404-407`) exactly the same as any other `build_model()`
    failure: there is no dedicated "toolchain not found" branch, so a
    missing `dxcom` is the SAME case as `test_a_failed_model_is_an_issue_
    not_a_traceback_and_the_batch_continues` above, not a different one --
    it resolves to the same coded `model.build-failed` issue, and the batch
    still continues to the next model rather than aborting."""

    def _fake_build_model(*, out_dir, name, **kw):
        if name == "needs_dxcom":
            raise FileNotFoundError("dxcom: command not found")
        out_dir.mkdir(parents=True, exist_ok=True)
        out = out_dir / f"{name}.alpmodel"
        out.write_bytes(_REAL_PACKAGE_BYTES)
        return out

    monkeypatch.setattr(model_cmd, "build_model", _fake_build_model)

    sdk = make_sdk(tmp_path / "sdk")
    write(tmp_path / "good.onnx", "x")
    write(tmp_path / "deepx.onnx", "x")
    board_yaml(
        tmp_path,
        "models:\n"
        "  - name: good\n    source: good.onnx\n"
        "  - name: needs_dxcom\n    source: deepx.onnx\n",
    )

    result = runner.invoke(
        app,
        [
            "build",
            "--project", str(tmp_path),
            "--sdk-root", str(sdk),
            "--format", "json",
        ],
    )
    assert result.exit_code == 3  # WriteFailure -- the dxcom-needing model failed
    doc = envelope(result)
    assert doc["ok"] is False
    built = doc["data"]["built"]
    assert len(built) == 1 and built[0].endswith("good.alpmodel")  # batch continued
    assert len(doc["issues"]) == 1
    failed = doc["issues"][0]
    assert failed["code"] == "model.build-failed"
    assert failed["severity"] == "error"
    assert "needs_dxcom" in failed["message"]
    assert "dxcom" in failed["message"]
    assert "FileNotFoundError" in failed["message"]


# --------------------------------------------------------------------------
# `_resolve_compile` -- the `models[].compile.<backend>` path resolver, unit
# --------------------------------------------------------------------------
#
# Pins alp-sdk#1271: only `config`/`calibration`/`images`/`spec` name paths.
# `_resolve_compile` used to resolve EVERY string value in a compile block to
# an absolute filesystem path, so DRP-AI's `input_shape` ("1,3,224,224"),
# `input_name` ("images") and `product` ("V2N") -- opaque strings the adapter
# must receive unchanged -- were corrupted into filesystem paths before ever
# reaching the adapter, which then made the adapter's own shape check
# misfire. alp-sdk fixed this as issue #1271; tan's hand-ported copy never
# received it until tan-cli#776.


def test_resolve_compile_leaves_non_path_options_unchanged(tmp_path):
    """alp-sdk#1271: only `config`/`calibration`/`images`/`spec` name paths.
    Resolving a shape string turned "1,3,224,224" into a filesystem path and
    made the DRP-AI adapter's own shape check misfire."""
    out = model_cmd._resolve_compile(
        {"drpai": {"input_shape": "1,3,224,224", "input_name": "images",
                   "product": "V2N", "config": "cfg.json"}},
        tmp_path,
    )
    assert out["drpai"]["input_shape"] == "1,3,224,224"
    assert out["drpai"]["input_name"] == "images"
    assert out["drpai"]["product"] == "V2N"
    # the one genuine path key IS resolved, absolute, against board.yaml's dir
    assert out["drpai"]["config"] == str((tmp_path / "cfg.json").resolve())


def test_resolve_compile_leaves_non_string_path_valued_options_unchanged(tmp_path):
    """A path-valued key (`images`) can still carry a non-string value in a
    plausible `board.yaml` spelling -- a YAML flow-sequence
    (`images: [a.png, b.png]`) or a stray int (`calibration: 100`). Those must
    pass through unchanged rather than reaching `Path.__truediv__`, which
    raises `TypeError: unsupported operand type(s) for /: 'PosixPath' and
    'list'` for a non-str/PathLike operand -- caught by `model()`'s outer
    catch-all and turned into `model.internal-failure` / exit
    `INTERNAL_FAILURE` instead of a build.
    Guards the `isinstance(v, str)` half of `_resolve_compile`'s guard, not
    just the `k in _PATH_OPT_KEYS` half that the test above pins."""
    out = model_cmd._resolve_compile({"drpai": {"images": ["a", "b"]}}, tmp_path)
    assert out["drpai"]["images"] == ["a", "b"]


def test_resolve_compile_passes_through_none_and_empty():
    """An absent `compile:` block and an empty one both fall through the
    `if not block:` guard to `None` -- true of both the pre-fix tan code and
    the upstream alp-sdk#1271 fix (`if not block: return None`, unchanged by
    that fix); this is pre-existing, unrelated behaviour, not part of the
    path-key drift this section otherwise pins."""
    assert model_cmd._resolve_compile(None, Path(".")) is None
    assert model_cmd._resolve_compile({}, Path(".")) is None


def test_compile_opts_paths_are_resolved_absolute_relative_to_board_dir(
    tmp_path, monkeypatch
):
    """Port of `model.py::_resolve_compile`: only PATH-VALUED opt keys
    (`config`/`calibration`/`images`/`spec`) become absolute paths relative to
    board.yaml's own directory -- every other compile option (DRP-AI's
    `input_shape`/`input_name`/`product` among them, alp-sdk#1271) must
    survive verbatim. Also covers the two other values `build_model` uses to
    choose which silicon to compile for -- `sku` and `metadata_root` --
    untested before: get either wrong and the driver silently compiles blobs
    for the wrong part.

    ADR-0028: this used to read `opts.json` back out of a stub SDK package a
    spawned driver wrote to disk. There is no subprocess boundary any more --
    `build_model` is called in-process, so what it actually received is
    asserted directly off the captured kwargs."""
    sdk = make_sdk(tmp_path / "sdk")
    write(tmp_path / "source.tflite", "x")
    write(tmp_path / "vela.ini", "x")
    board_yaml(
        tmp_path,
        "models:\n"
        "  - name: mymodel\n"
        "    source: source.tflite\n"
        "    compile:\n"
        "      ethos_u:\n"
        "        config: vela.ini\n"
        "      drpai:\n"
        "        input_shape: \"1,3,224,224\"\n"
        "        input_name: images\n"
        "        product: V2N\n",
    )

    calls = []

    def _fake_build_model(*, out_dir, name, **kw):
        calls.append(kw)
        out_dir.mkdir(parents=True, exist_ok=True)
        out = out_dir / f"{name}.alpmodel"
        out.write_bytes(_REAL_PACKAGE_BYTES)
        return out

    monkeypatch.setattr(model_cmd, "build_model", _fake_build_model)

    result = runner.invoke(
        app,
        [
            "build",
            "--project", str(tmp_path),
            "--sdk-root", str(sdk),
            "--format", "json",
        ],
    )
    assert result.exit_code == 0, result.stdout
    assert len(calls) == 1
    received = calls[0]
    assert received["sku"] == "E1M-TEST"
    assert received["metadata_root"] == sdk / "metadata"
    assert received["compile_opts"]["ethos_u"]["config"] == str(
        (tmp_path / "vela.ini").resolve()
    )
    # alp-sdk#1271 / tan-cli#776: these three DRP-AI opts are opaque strings,
    # not path keys -- they must survive the round trip through
    # `_resolve_compile` byte-for-byte, not get mangled into filesystem paths.
    drpai = received["compile_opts"]["drpai"]
    assert drpai["input_shape"] == "1,3,224,224"
    assert drpai["input_name"] == "images"
    assert drpai["product"] == "V2N"


# --------------------------------------------------------------------------
# tan-cli#398 -- `--board-yaml` is the spelling `build`/`run`/`kconfig`/
# `validate`/`generate`/`inspect` all use for the board file, and the vscode
# extension's own CLI contract (`docs/CLI.md`, "Common flags") lists it among
# the flags "All commands should support". `model` declared only `--board` and
# took `--board-yaml` as one of `accept_global_flags`' INJECTED options, which
# are accepted and then dropped -- harmless for an arity-0 `--verbose`, a
# wrong answer for a flag that carries a value. Measured before the fix: the
# same file named two ways gave two different SKUs, BOTH at `exitCode: 0` with
# `issues: []`, so `build_model` compiled `.alpmodel` artefacts for the wrong
# silicon and nothing refused or warned.
# --------------------------------------------------------------------------


def _two_boards(tmp_path):
    """`<project>/board.yaml` (the DEFAULT `model` falls back to) and
    `<other>/board.yaml` (the one the caller names), with different
    `som.sku` -- `som.sku` is what selects which silicon `build_model`
    compiles for, so the SKU in the envelope names the file actually read."""
    project = tmp_path / "project"
    other = tmp_path / "other"
    write(project / "board.yaml", "som:\n  sku: E1M-AEN801\n")
    write(other / "board.yaml", "som:\n  sku: E1M-V2N101\n")
    return project, other / "board.yaml"


def test_board_yaml_spelling_reads_the_file_it_names_trailing_form(tmp_path):
    sdk = make_sdk(tmp_path / "sdk")
    project, other_board = _two_boards(tmp_path)
    result = runner.invoke(
        app,
        [
            "build",
            "--project", str(project),
            "--sdk-root", str(sdk),
            "--board-yaml", str(other_board),
            "--format", "json",
        ],
    )
    assert result.exit_code == 0, result.stdout
    doc = envelope(result)
    assert doc["data"]["sku"] == "E1M-V2N101", (
        "`--board-yaml` was accepted and dropped, so the default "
        "`<project>/board.yaml` was packaged instead of the file named"
    )
    # `.as_posix()`, not `str()`: `project.boardYaml` is POSIX-normalised
    # before it is emitted, so `str()` matches on a POSIX host only and this
    # assertion was green on macOS/ubuntu and red on windows-latest alone
    # (tan-cli#414). Normalise the filesystem side; the envelope is the
    # contract.
    assert doc["project"]["boardYaml"] == other_board.as_posix()


def test_board_yaml_spelling_reads_the_file_it_names_leading_form(tmp_path):
    """The canonical leading-global form the extension's own `withSdkRoot`
    produces (`tan --board-yaml <path> model build`). It goes through
    `cli._reorder_global_flags`, which relocates the flag across the
    subcommand boundary -- and, pre-fix, straight into the same drop. Driven
    through the REAL `tan.cli.app` registration plus that real rewrite, not
    this module's throwaway one-command app, because the rewrite is the half
    under test."""
    from tan.cli import _reorder_global_flags
    from tan.cli import app as real_app

    sdk = make_sdk(tmp_path / "sdk")
    project, other_board = _two_boards(tmp_path)
    argv = _reorder_global_flags(
        [
            "--board-yaml", str(other_board),
            "model", "build",
            "--project", str(project),
            "--sdk-root", str(sdk),
            "--format", "json",
        ]
    )
    result = runner.invoke(real_app, argv)
    assert result.exit_code == 0, result.stdout
    doc = envelope(result)
    assert doc["data"]["sku"] == "E1M-V2N101"
    # `.as_posix()`, not `str()`: `project.boardYaml` is POSIX-normalised
    # before it is emitted, so `str()` matches on a POSIX host only and this
    # assertion was green on macOS/ubuntu and red on windows-latest alone
    # (tan-cli#414). Normalise the filesystem side; the envelope is the
    # contract.
    assert doc["project"]["boardYaml"] == other_board.as_posix()


def test_board_and_board_yaml_are_the_same_option(tmp_path):
    """Both spellings must land on ONE parameter -- two independent options
    would put the port right back where tan-cli#398 found it the first time a
    caller passed the other one."""
    sdk = make_sdk(tmp_path / "sdk")
    project, other_board = _two_boards(tmp_path)
    common = ["--project", str(project), "--sdk-root", str(sdk), "--format", "json"]
    via_board = runner.invoke(app, ["build", "--board", str(other_board), *common])
    via_board_yaml = runner.invoke(app, ["build", "--board-yaml", str(other_board), *common])
    assert via_board.exit_code == via_board_yaml.exit_code == 0
    assert envelope(via_board) == envelope(via_board_yaml)


# --------------------------------------------------------------------------
# tan-cli#497 defect 3 -- the SDK-resolution warnings are no longer dropped
# --------------------------------------------------------------------------


def _broken_pin_project(tmp_path: Path) -> Path:
    """A project whose `.alp/sdk-path` names a checkout that does not resolve,
    beside a sibling `alp-sdk` that discovery DOES find -- so resolution
    answers with a DIFFERENT checkout than the pin names. `conftest.py`'s
    autouse fixture has already repointed HOME, so `~/.alp/sdk-default`
    cannot interfere."""
    make_sdk(tmp_path / "alp-sdk")
    project = tmp_path / "proj"
    board_yaml(project)
    write(
        project / ".alp" / "sdk-path",
        json.dumps({"sdkPath": str(tmp_path / "gone-checkout")}),
    )
    return project


def test_a_broken_project_pin_is_reported_on_a_model_build(tmp_path, monkeypatch):
    """tan-cli#497 defect 3. `model build` was the only
    `resolve_project_context` caller that read `.workspace_root`/`.board_yaml`
    /`.project()`/`.sdk` off the returned context and NONE of the three
    resolution facts, so it alone dropped BOTH `sdk.project-pin-unresolved`
    and `sdk.global-default-foreign-project` -- while `tan size` and `tan
    image`, on the identical resolver, reported them from the same directory.

    It matters most here: the `.alpmodel` packages are compiled against
    `<resolved checkout>/metadata`, i.e. against THAT checkout's
    target/backend table for `som.sku`.

    Fails against dev: there `issues` is `[]` at `ok: true`."""
    project = _broken_pin_project(tmp_path)
    monkeypatch.chdir(project)
    result = runner.invoke(app, ["build", "--format", "json"])
    assert result.exit_code == 0
    doc = envelope(result)
    assert doc["ok"] is True
    assert [i["code"] for i in doc["issues"]] == ["sdk.project-pin-unresolved"]
    assert doc["issues"][0]["severity"] == "warning"
    assert "gone-checkout" in doc["issues"][0]["message"]
    assert doc["sdk"]["sourceTier"] == "discovery"


def test_a_broken_project_pin_reaches_model_text_mode_too(tmp_path, monkeypatch):
    """The DEFAULT mode, which `finish()` renders from `data.built` plus
    `issues`. Warnings LEAD, ahead of the result lines: which checkout
    answered is a fact about the whole run, not a per-model outcome.

    Fails against dev: stderr carries only the no-models line."""
    project = _broken_pin_project(tmp_path)
    monkeypatch.chdir(project)
    result = runner.invoke(app, ["build"])
    assert result.exit_code == 0
    lines = [ln for ln in result.stderr.splitlines() if ln.strip()]
    assert lines[0].startswith("warning: .alp/sdk-path names")
    assert "gone-checkout" in lines[0]
    # ...and the run's own line is NOT swallowed by the new warning: the
    # "nothing to build" branch used to key on `not issues`.
    assert any("no `models:` declared in board.yaml" in ln for ln in lines)


def test_a_broken_project_pin_survives_a_model_error_refusal(tmp_path, monkeypatch):
    """The `except ModelError` path. `resolve_project_context` used to be the
    first statement INSIDE `_run_build`, so its resolution facts were
    unreachable from the handler and the pin warning was dropped on every
    refusal too. Resolved by the caller now, so the ONE resolution feeds
    every exit.

    Fails against dev: `issues` is `[model.board-yaml-invalid]` alone."""
    make_sdk(tmp_path / "alp-sdk")
    project = tmp_path / "proj"
    write(project / "board.yaml", "models: []\n")  # no som.sku -> ModelError
    write(
        project / ".alp" / "sdk-path",
        json.dumps({"sdkPath": str(tmp_path / "gone-checkout")}),
    )
    monkeypatch.chdir(project)
    result = runner.invoke(app, ["build", "--format", "json"])
    assert result.exit_code == 2
    doc = envelope(result)
    assert [i["code"] for i in doc["issues"]] == [
        "sdk.project-pin-unresolved",
        "model.board-yaml-invalid",
    ]


def test_a_clean_workspace_model_build_still_reports_no_issues(tmp_path):
    """The negative control: with no pin and no foreign global default the
    envelope must stay `issues: []`, or a fix that appended unconditionally
    would look identical to the cases above."""
    sdk = make_sdk(tmp_path / "sdk")
    board_yaml(tmp_path)
    result = runner.invoke(
        app,
        ["build", "--project", str(tmp_path), "--sdk-root", str(sdk), "--format", "json"],
    )
    assert result.exit_code == 0
    assert envelope(result)["issues"] == []


def test_the_internal_failure_catch_all_reports_the_pin_warning_too(
    tmp_path, monkeypatch
):
    """The outer `except Exception` -- the site that was left open in
    `kconfig`/`image`/`size` and is already closed here, pinned so it stays
    that way. `model` resolves in the OUTER function (`sdk_issues` is assigned
    between the `try:` and `_run_build`), so its handler can read the pair
    directly; no `SdkDisclosure` carrier is needed.

    Passes on the pre-fix branch too: this guards a property that is already
    true against a regression, it does not report a defect."""
    project = _broken_pin_project(tmp_path)
    monkeypatch.chdir(project)

    def boom(*args, **kwargs):
        raise OSError(24, "Too many open files")

    monkeypatch.setattr("tan.commands.model_cmd._run_build", boom)
    result = runner.invoke(app, ["build", "--format", "json"])
    assert result.exit_code == 5
    doc = envelope(result)
    assert [i["code"] for i in doc["issues"]] == [
        "sdk.project-pin-unresolved",
        "model.internal-failure",
    ]
    assert "gone-checkout" in doc["issues"][0]["message"]


def test_the_internal_failure_catch_all_reaches_model_text_mode_too(
    tmp_path, monkeypatch
):
    """The DEFAULT mode, same site -- `finish`'s text branch prints warnings
    first, so the pair leads the crash line."""
    project = _broken_pin_project(tmp_path)
    monkeypatch.chdir(project)

    def boom(*args, **kwargs):
        raise OSError(24, "Too many open files")

    monkeypatch.setattr("tan.commands.model_cmd._run_build", boom)
    result = runner.invoke(app, ["build"])
    assert result.exit_code == 5
    lines = [ln for ln in result.stderr.splitlines() if ln.strip()]
    assert lines[0].startswith("warning: .alp/sdk-path names")
    assert any("model build failed unexpectedly" in ln for ln in lines)


def test_a_rejected_sdk_root_flag_is_named_in_the_refusal(tmp_path):
    """tan-cli#497 defect 7. `model build` refuses with NO `sdk` block (the
    same `resolve_metadata_sdk_root` returning `None` is what leaves it
    absent), so before this fix the path the caller typed left no trace at all
    -- and the message opened with "Use --sdk-root", the flag they had just
    passed.

    The rejected root is a real directory missing only the loader marker, so
    this exercises the marker check rather than a nonexistent path."""
    board_yaml(tmp_path)
    typo = tmp_path / "alp-sdk-typo"
    typo.mkdir()
    result = runner.invoke(
        app,
        ["build", "--project", str(tmp_path), "--sdk-root", str(typo), "--format", "json"],
    )
    assert result.exit_code == 2
    doc = envelope(result)
    assert "sdk" not in doc
    assert doc["issues"][0]["code"] == "model.sdk-root-unresolved"
    assert doc["issues"][0]["message"] == (
        f'alp-sdk root is unresolved: --sdk-root "{typo}" is not an alp-sdk '
        "checkout (scripts/alp_project.py not found under it). No models were built."
    )


def test_the_no_flag_refusal_still_offers_the_flag(tmp_path):
    """The other branch: with no `--sdk-root` given there IS no typed value to
    name, and recommending the flag is the right advice. Pinned so the #497
    fix cannot rewrite the branch it was not about."""
    board_yaml(tmp_path)
    result = runner.invoke(
        app, ["build", "--project", str(tmp_path), "--format", "json"]
    )
    assert result.exit_code == 2
    doc = envelope(result)
    assert doc["issues"][0]["code"] == "model.sdk-root-unresolved"
    assert doc["issues"][0]["message"].startswith(
        "alp-sdk root is unresolved. Use --sdk-root, place the project near an "
        "alp-sdk checkout, or "
    )


# --------------------------------------------------------------------------
# tan-cli#789 (f) -- a shipped blob's compiler caveat reaches the operator who
# ran `tan model build`, not just `tan model check --exact`.
# --------------------------------------------------------------------------

_CAVEAT = ('vela used its BUILT-IN default profile (system-config '
           'Ethos_U55_High_End_Embedded, memory-mode Shared_Sram), not one authored '
           'for this module -- vela\'s own warning for that is "Compilation may be '
           'invalid or non-optimal". The arena/SRAM figures and the compiled command '
           "stream describe that default memory model, not this module's.")


def _caveated_package_bytes() -> bytes:
    """A real container whose ethos_u target ships with the verbatim vela
    default-profile caveat. (The COMPILE that produces this exact string is
    pinned against real vela 5.1.0 by `tests/model/test_build.py`'s
    `test_a_refused_target_does_not_take_the_rest_of_the_package_with_it` --
    the both-flags-defaulted shape, which since alp-sdk #1470 means a SoC spec
    carrying no `npu_toolchain.vela`. This file is about what the command does
    with a caveat that already exists, so which shape it is does not matter
    here.)"""
    from tan.model.manifest import Manifest, Target
    from tan.model.package import write_package

    return write_package(
        Manifest(
            name="mymodel", src_sha=bytes(32),
            targets=[
                Target("ethos_u", "alif:ensemble:e8", "vela_tflite", "ethos-u55-128",
                       32, {"sram_kib": 1, "op_features": []}, 0,
                       compiler_version="vela 5.1.0", caveats=[_CAVEAT]),
                Target("cpu", "*", "tflite", "", 0,
                       {"sram_kib": 0, "op_features": []}, 1,
                       compiler_version="passthrough"),
            ],
        ),
        [b"VELA-BLOB", b"TFLITE-BLOB"],
    )


def _build_one_model(tmp_path, monkeypatch, package_bytes, *, args=("--format", "json")):
    sdk = make_sdk(tmp_path / "sdk")
    write(tmp_path / "source.tflite", "x")
    board_yaml(tmp_path, "models:\n  - name: mymodel\n    source: source.tflite\n")

    def _fake_build_model(*, out_dir, name, **kw):
        out_dir.mkdir(parents=True, exist_ok=True)
        out = out_dir / f"{name}.alpmodel"
        out.write_bytes(package_bytes)
        return out

    monkeypatch.setattr(model_cmd, "build_model", _fake_build_model)
    return runner.invoke(
        app,
        ["build", "--project", str(tmp_path), "--sdk-root", str(sdk), *args],
    )


def test_a_shipped_caveat_is_reported_as_a_warning_not_swallowed(tmp_path, monkeypatch):
    """`build` writes the bytes that reach a board. A blob compiled against
    vela's BUILT-IN default memory model carries an `arena`/`sram_kib` pair
    describing THAT model, and those are the figures alp-sdk's on-device
    selector gates on (`src/backends/inference/alp_model_select.c`) -- so the
    operator has to be told, verbatim, at the moment the package is produced.

    Fails against the previous behaviour: `issues` was `[]`."""
    result = _build_one_model(tmp_path, monkeypatch, _caveated_package_bytes())
    assert result.exit_code == 0, result.stdout
    doc = envelope(result)
    assert doc["ok"] is True
    assert [i["code"] for i in doc["issues"]] == ["model.target-caveat"]
    issue = doc["issues"][0]
    assert issue["severity"] == "warning"
    assert issue["message"].startswith(
        "model 'mymodel': the ethos_u ethos-u55-128 target in mymodel.alpmodel "
        "ships with a compiler caveat -- "
    )
    assert issue["message"].endswith(_CAVEAT)      # verbatim, not summarised
    # ...and the package is still reported as built. A caveat is a caveat.
    assert len(doc["data"]["built"]) == 1


def test_a_shipped_caveat_does_not_turn_a_successful_build_into_a_failure(
    tmp_path, monkeypatch
):
    """`_run_build`'s exit code keys on ERROR issues, not on `issues` being
    non-empty. Without that, adding a warning here would have reported every
    caveated build as `WriteFailure` (exit 3) with the package sitting on disk
    perfectly intact."""
    result = _build_one_model(tmp_path, monkeypatch, _caveated_package_bytes())
    assert result.exit_code == 0, result.stdout
    assert envelope(result)["data"]["built"], "the package WAS written"


def test_a_shipped_caveat_reaches_text_mode_too(tmp_path, monkeypatch):
    """The default (non-JSON) mode. Warnings LEAD, so the caveat is readable
    above the `built ...` line rather than buried under it."""
    result = _build_one_model(tmp_path, monkeypatch, _caveated_package_bytes(), args=())
    assert result.exit_code == 0, result.stderr
    lines = [ln for ln in result.stderr.splitlines() if ln.strip()]
    assert lines[0].startswith("warning: model 'mymodel': the ethos_u ethos-u55-128 target")
    assert "Compilation may be invalid or non-optimal" in lines[0]
    assert any(ln.startswith("built ") for ln in lines[1:])


def test_a_package_with_no_caveats_reports_none(tmp_path, monkeypatch):
    """The negative control: an uncaveated package must leave `issues` empty,
    or a caveat line would mean nothing."""
    result = _build_one_model(tmp_path, monkeypatch, _REAL_PACKAGE_BYTES)
    assert result.exit_code == 0, result.stdout
    assert envelope(result)["issues"] == []


def test_an_unreadable_package_is_a_warning_not_a_silent_pass(tmp_path, monkeypatch):
    """`build_model` returned a path this reader cannot decode. The package is
    written, so this is not a build failure -- but staying silent would be
    indistinguishable from "this package has no caveats", which is the exact
    confusion the field exists to end."""
    result = _build_one_model(tmp_path, monkeypatch, b"not-an-alpmodel-container")
    assert result.exit_code == 0, result.stdout
    doc = envelope(result)
    assert [i["code"] for i in doc["issues"]] == ["model.caveat-readback-failed"]
    assert doc["issues"][0]["severity"] == "warning"
    assert "could not read its manifest back" in doc["issues"][0]["message"]
    assert len(doc["data"]["built"]) == 1
