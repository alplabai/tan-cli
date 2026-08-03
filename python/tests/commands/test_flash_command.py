# SPDX-License-Identifier: Apache-2.0
"""`tan flash` unit tests: the surfaces the oracle diff cannot reach.

`tests/parity/test_flash_oracle_parity.py` is the primary gate -- it diffs whole
envelopes against the shipped Rust binary on 43 argv/manifest combinations. What
lands HERE is what has no oracle counterpart:

* **Flow D** (`alif_mram_jlink`), a backend the shipped Rust does not have.
* **Hostile inputs**, which must produce an envelope rather than a traceback.
  The port's most-repeated defect class is an uncaught exception escaping the
  error contract: stdout stays empty and the extension renders nothing, with no
  error visible on either side. Every case below drives the real subprocess so
  the assertion covers the actual stdout framing.
* **The "one JSON document on stdout, nothing else" invariant** itself.

No case touches hardware: nothing here spawns a probe or a flash tool against a
device, and the Flow D cases all stop at a refusal or a confirm-gated no-op.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tan.commands import flash_cmd
from tan.core import flash_plan
from tan.core.bootstrap import venv_layout
from tan.core.flash_plan import (
    FlashInputs,
    FlashPlanError,
    FlashTarget,
    ManifestError,
    SLICE,
    fa_int_checked,
    fa_str_checked,
    flow_d_available,
    is_rust_absolute,
    parse_atoc_start_address,
    parse_system_manifest,
    plan_alif_mram_jlink,
    resolve_artefact_path,
    select_flash_method,
    validate_identifier,
    zephyr_build_dir,
)

PACKAGE_ROOT = Path(__file__).resolve().parents[2]

OK_SLICE = """schema_version: 1
hw_info: {sku: E1M-V2N101}
slices:
- {core_id: c1, os: zephyr, output_artefact: a.elf, status: ok,
   flash_method: zephyr_west_flash, flash_args: {}}
helper_mcus: []
boot_order: []
"""


# ── the real-subprocess harness ─────────────────────────────────────────────


def run_flash(work: Path, *argv, env=None, manifest=OK_SLICE, write_manifest=True):
    """Drive `python -m tan flash` in `work` and return `(exit, stdout, stderr)`.

    A real subprocess, not Typer's `CliRunner`: the invariant under test is that
    STDOUT carries exactly one JSON document and nothing else, and an in-process
    runner cannot see an import-time print, a warning routed to stdout, or a
    child process inheriting the wrong handle -- the three ways that invariant
    has actually been broken.
    """
    (work / "build").mkdir(exist_ok=True)
    (work / "sdk" / "scripts").mkdir(parents=True, exist_ok=True)
    (work / "sdk" / "scripts" / "alp_project.py").write_text("", encoding="utf-8")
    if write_manifest:
        (work / "build" / "system-manifest.yaml").write_text(
            manifest, encoding="utf-8", newline=""
        )
    inherited = os.environ.get("PYTHONPATH")
    child_env = {
        **os.environ,
        "HOME": str(work),
        "USERPROFILE": str(work),
        "PYTHONPATH": os.pathsep.join(
            [str(PACKAGE_ROOT), *([inherited] if inherited else [])]
        ),
    }
    child_env.pop("ALP_FLASH_FORCE", None)
    child_env.update(env or {})
    proc = subprocess.run(
        [sys.executable, "-m", "tan", "flash", "--sdk-root", "./sdk", *argv, "."],
        cwd=work,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=child_env,
        timeout=180,
    )
    return proc.returncode, proc.stdout, proc.stderr


def envelope(stdout: str):
    """Parse THE one envelope, asserting stdout carries nothing else."""
    assert stdout, "stdout was empty -- the extension renders nothing for this"
    payload = json.loads(stdout)  # a second document would raise here
    assert set(payload) <= {
        "command", "ok", "exitCode", "project", "sdk", "data", "issues",
    }, payload
    assert payload["ok"] == (payload["exitCode"] == 0)
    return payload


def codes(payload):
    return [issue["code"] for issue in payload["issues"]]


# ── hostile inputs: every one must be an envelope, never a traceback ────────


def test_manifest_is_a_directory(tmp_path):
    (tmp_path / "build").mkdir()
    (tmp_path / "build" / "system-manifest.yaml").mkdir()
    exit_code, out, _ = run_flash(tmp_path, "--format", "json", write_manifest=False)
    payload = envelope(out)
    # `os.path.isfile` says False for a directory, so this is the not-found path
    # -- the same answer `Path::is_file` gives the oracle.
    assert exit_code == 1
    assert codes(payload) == ["flash.manifest-not-found"]


def test_manifest_holds_non_utf8_bytes(tmp_path):
    (tmp_path / "build").mkdir()
    (tmp_path / "build" / "system-manifest.yaml").write_bytes(
        b"schema_version: 1\nhw_info: {sku: \xff\xfe-BROKEN}\nslices: []\n"
    )
    exit_code, out, _ = run_flash(tmp_path, "--format", "json", write_manifest=False)
    payload = envelope(out)
    # `errors="replace"` keeps the read from raising, so the document still
    # parses and the run reaches a normal outcome. The point is only that a
    # cp1252 host does not turn a stray byte into a `UnicodeDecodeError`
    # traceback (I-27's read side, which has no gate anywhere).
    assert exit_code == 0
    assert codes(payload) == ["flash.nothing-matched"]


def test_manifest_is_truncated_binary(tmp_path):
    (tmp_path / "build").mkdir()
    (tmp_path / "build" / "system-manifest.yaml").write_bytes(b"\x00\x01\x02\xffnot yaml at all")
    exit_code, out, _ = run_flash(tmp_path, "--format", "json", write_manifest=False)
    payload = envelope(out)
    assert exit_code == 1
    assert codes(payload) == ["flash.manifest-invalid"]


def test_manifest_root_is_a_list(tmp_path):
    exit_code, out, _ = run_flash(
        tmp_path, "--format", "json", manifest="- one\n- two\n"
    )
    assert exit_code == 1
    assert codes(envelope(out)) == ["flash.manifest-invalid"]


def test_manifest_empty_file(tmp_path):
    exit_code, out, _ = run_flash(tmp_path, "--format", "json", manifest="")
    assert exit_code == 1
    assert codes(envelope(out)) == ["flash.manifest-invalid"]


def test_slices_is_a_mapping_not_a_list(tmp_path):
    exit_code, out, _ = run_flash(
        tmp_path, "--format", "json", manifest="schema_version: 1\nslices: {a: b}\n"
    )
    assert exit_code == 1
    assert codes(envelope(out)) == ["flash.manifest-invalid"]


def test_flash_args_is_a_list(tmp_path):
    """`flash_args` is `serde_yaml::Value` on the oracle side -- any shape
    deserializes -- and every accessor reads a non-mapping as an empty map. A
    list must therefore behave exactly like `{}`, not raise."""
    manifest = """schema_version: 1
hw_info: {sku: S}
slices:
- {core_id: c1, os: zephyr, output_artefact: a.elf, status: ok,
   flash_method: baremetal_cmake_flash, flash_args: [1, 2]}
helper_mcus: []
boot_order: []
"""
    exit_code, out, _ = run_flash(tmp_path, "--format", "json", "--dry-run", manifest=manifest)
    payload = envelope(out)
    assert exit_code == 0
    assert "--target flash" in payload["data"]["entries"][0]["message"]


# ── build-policy skip vs a genuine build failure ─────────────────────────────


def test_a_build_skipped_slice_does_not_fail_flash(tmp_path):
    """A slice `tan build` left `status: skipped` (e.g. `executionPolicy.
    missingTool` skipped a Yocto slice because `bitbake` was not on PATH) must
    not turn an otherwise-clean `tan flash` red -- the skip was already a
    policy decision, not a failure. It still must not be flashed (there is
    nothing built to flash), and the skip must stay visible in `issues`."""
    manifest = """schema_version: 1
hw_info: {sku: S}
slices:
- {core_id: c1, os: zephyr, output_artefact: a.elf, status: ok,
   flash_method: zephyr_west_flash, flash_args: {}}
- {core_id: c2, os: yocto, output_artefact: b.wic, status: skipped,
   flash_method: yocto_wic_to_sd_or_emmc, flash_args: {}}
helper_mcus: []
boot_order: []
"""
    exit_code, out, _ = run_flash(tmp_path, "--format", "json", "--dry-run", manifest=manifest)
    payload = envelope(out)
    assert exit_code == 0
    assert payload["ok"] is True
    assert codes(payload) == ["flash.slice-skipped"]
    assert payload["issues"][0]["severity"] == "warning"
    message = payload["issues"][0]["message"]
    assert "c2" in message
    # Wording pinned separately from the `refused` bucket's "stale, rebuild
    # it" text (test_a_genuinely_failed_slice_still_fails_flash): neither half
    # of that remedy holds for a policy skip -- nothing was ever built, so
    # nothing is stale, and rebuilding on the SAME host reruns the same
    # executionPolicy skip.
    assert "Rebuild it first" not in message
    assert "stale" not in message
    assert "executionPolicy" in message
    assert payload["data"]["entries"][0]["id"] == "c1"
    assert payload["data"]["entries"][0]["status"] == "ok"
    # c2 never became a target at all -- only c1's dry-run entry is reported.
    assert len(payload["data"]["entries"]) == 1


def test_a_genuinely_failed_slice_still_fails_flash(tmp_path):
    """The opposite pin: a slice `status: failed` (a real build failure, not a
    policy skip) must still fail `tan flash` -- the fix must not swallow real
    failures alongside policy skips."""
    manifest = """schema_version: 1
hw_info: {sku: S}
slices:
- {core_id: c1, os: zephyr, output_artefact: a.elf, status: ok,
   flash_method: zephyr_west_flash, flash_args: {}}
- {core_id: c2, os: yocto, output_artefact: b.wic, status: failed,
   flash_method: yocto_wic_to_sd_or_emmc, flash_args: {}}
helper_mcus: []
boot_order: []
"""
    exit_code, out, _ = run_flash(tmp_path, "--format", "json", "--dry-run", manifest=manifest)
    payload = envelope(out)
    assert exit_code == 1
    assert payload["ok"] is False
    assert codes(payload) == ["flash.slice-not-built"]
    assert payload["issues"][0]["severity"] == "error"
    assert "c2" in payload["issues"][0]["message"]


def test_only_slice_skipped_flashes_nothing_and_fails(tmp_path):
    """The inverted twin of the skip-alongside-a-flash pin above: when the
    manifest's ONLY slice is `status: skipped`, nothing ever reaches the
    dispatch loop, so a run where nothing was flashed must not exit 0 -- that
    is the same silent-success class `status: failed` guards against, just
    reached through the skip bucket instead."""
    manifest = """schema_version: 1
hw_info: {sku: S}
slices:
- {core_id: c2, os: yocto, output_artefact: b.wic, status: skipped,
   flash_method: yocto_wic_to_sd_or_emmc, flash_args: {}}
helper_mcus: []
boot_order: []
"""
    exit_code, out, _ = run_flash(tmp_path, "--format", "json", "--dry-run", manifest=manifest)
    payload = envelope(out)
    assert exit_code == 1
    assert payload["ok"] is False
    assert codes(payload) == ["flash.slice-skipped", "flash.nothing-flashed"]
    assert payload["issues"][-1]["severity"] == "error"
    assert payload["data"]["entries"] == []


def test_core_filter_naming_a_skipped_slice_fails_flash(tmp_path):
    """`--core c2` naming exactly the skipped slice: the user asked for one
    slice, nothing was programmed, and that must fail the run even though a
    sibling `c1` (excluded by the filter) built fine."""
    manifest = """schema_version: 1
hw_info: {sku: S}
slices:
- {core_id: c1, os: zephyr, output_artefact: a.elf, status: ok,
   flash_method: zephyr_west_flash, flash_args: {}}
- {core_id: c2, os: yocto, output_artefact: b.wic, status: skipped,
   flash_method: yocto_wic_to_sd_or_emmc, flash_args: {}}
helper_mcus: []
boot_order: []
"""
    exit_code, out, _ = run_flash(
        tmp_path, "--format", "json", "--dry-run", "--core", "c2", manifest=manifest
    )
    payload = envelope(out)
    assert exit_code == 1
    assert payload["ok"] is False
    assert codes(payload) == ["flash.slice-skipped", "flash.nothing-flashed"]
    assert payload["data"]["entries"] == []



_AEN_M55_COLLISION_MANIFEST = """schema_version: 1
hw_info: {sku: E1M-AEN801}
slices:
- {core_id: m55_hp, os: zephyr, output_artefact: build_hp/zephyr/zephyr.bin,
   status: ok, flash_method: zephyr_west_flash, flash_args: {}}
- {core_id: m55_he, os: zephyr, output_artefact: build_he/zephyr/zephyr.bin,
   status: ok, flash_method: zephyr_west_flash, flash_args: {}}
helper_mcus: []
boot_order: []
"""




def test_build_root_pointing_at_a_regular_file(tmp_path):
    (tmp_path / "notadir").write_text("x", encoding="utf-8")
    exit_code, out, _ = run_flash(
        tmp_path, "--format", "json", "--build-root", "notadir", write_manifest=False
    )
    assert exit_code == 1
    assert codes(envelope(out)) == ["flash.manifest-not-found"]


def test_sdk_root_pointing_at_a_regular_file(tmp_path):
    """`--sdk-root` is TERMINAL (I-31): an invalid value fails the command loudly
    instead of falling through to discovery and flashing against a different
    checkout."""
    (tmp_path / "afile").write_text("x", encoding="utf-8")
    (tmp_path / "build").mkdir()
    (tmp_path / "build" / "system-manifest.yaml").write_text(OK_SLICE, encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, "-m", "tan", "flash", "--sdk-root", "afile", "--format", "json", "."],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        # Explicit, like `run_flash` above: bare `text=True` decodes with the
        # platform locale (cp1252 on a Windows runner) while Click/Rich emit
        # UTF-8, and the `timeout=` reader thread then dies on the first
        # undecodable byte leaving BOTH streams `None`.
        encoding="utf-8",
        errors="replace",
        env={**os.environ, "PYTHONPATH": str(PACKAGE_ROOT), "HOME": str(tmp_path),
             "USERPROFILE": str(tmp_path)},
        timeout=180,
    )
    payload = envelope(proc.stdout)
    assert proc.returncode == 1
    assert codes(payload) == ["flash.sdk-root-not-found"]
    # `sdk` must be ABSENT, never null, when nothing resolved.
    assert "sdk" not in payload
    assert payload["data"]["buildRoot"] == ""


@pytest.mark.parametrize("value", ["0", "", "true", "TRUE", " 1", "1 ", "yes", "2"])
def test_alp_flash_force_is_exactly_the_string_1(tmp_path, value):
    """The hardware-write gate (I-30) is armed by `ALP_FLASH_FORCE=1` and by
    NOTHING else. Every near-miss spelling must leave the gate CLOSED -- a
    truthiness test (`if os.environ.get(...)`) would arm it on `"0"` and on
    `"false"`, silently reprogramming a customer's eMMC.

    `xspi_flashwriter`, not `yocto_wic`: xspi declares an EMPTY `requires` and
    probes no tools at all, so the outcome depends only on the gate. The
    yocto backend picks between `bmaptool`, `dd`, `gunzip` and `xz` by PATH, and
    an earlier draft of this test used it -- it then passed under the Bash shell
    (Git's `usr/bin` supplies `dd`) and failed under PowerShell (it does not),
    which read as a Python-version difference and was not one.
    """
    manifest = """schema_version: 1
hw_info: {sku: S}
slices:
- {core_id: c1, os: zephyr, output_artefact: a.bin, status: ok,
   flash_method: xspi_flashwriter, flash_args: {flash_partition: mtd1, port: COM3}}
helper_mcus: []
boot_order: []
"""
    exit_code, out, _ = run_flash(
        tmp_path, "--format", "json", env={"ALP_FLASH_FORCE": value}, manifest=manifest
    )
    payload = envelope(out)
    assert exit_code == 0
    assert payload["data"]["entries"][0]["status"] == "planned", value
    assert codes(payload) == ["flash.confirm-required"]


def test_tool_that_is_a_directory_becomes_a_failed_entry(tmp_path):
    """A "tool" on PATH that is a DIRECTORY passes no reasonable gate but does
    reach `subprocess`, which raises `PermissionError`/`OSError`. That must
    become a failed entry, not a traceback.

    `dd` is planted as a directory on a PATH containing nothing else, so the
    gate's `os.access(..., X_OK)` decides: either it refuses (missing tool) or
    the spawn does (`could not spawn`). Both are envelopes, which is the claim.
    """
    fake_bin = tmp_path / "fakebin"
    fake_bin.mkdir()
    (fake_bin / ("dd.exe" if os.name == "nt" else "dd")).mkdir()
    manifest = """schema_version: 1
hw_info: {sku: S}
slices:
- {core_id: c1, os: yocto, output_artefact: a.wic, status: ok,
   flash_method: yocto_wic, flash_args: {target: /dev/sdb, confirm: true}}
helper_mcus: []
boot_order: []
"""
    exit_code, out, _ = run_flash(
        tmp_path,
        "--format",
        "json",
        env={"PATH": str(fake_bin)},
        manifest=manifest,
    )
    payload = envelope(out)
    assert exit_code == 1
    assert codes(payload) == ["flash.entry-failed"]
    assert payload["data"]["entries"][0]["status"] == "failed"


def test_a_confirmed_flow_d_entry_fails_contained_when_no_tool_resolves(tmp_path, monkeypatch):
    """A confirmed Flow D entry must fail as an ENVELOPE, not kill the process.

    **`PATH` is scrubbed deliberately, and that is a hardware-safety requirement,
    not tidiness.** This manifest carries `confirm: true` and the test runs
    WITHOUT `--dry-run`, so with a J-Link resolvable tan would genuinely spawn
    Commander with `si SWD / connect / loadbin ... 0x80010000 / loadbin ...
    0x8057F5B0 / RSetType 2 / r / g` -- i.e. connect to whatever board is
    attached, attempt an MRAM write, and pin-reset it, from `pytest`. The
    maintainer's bench has a probe wired to a live AEN EVK. No test in this file
    may ever be able to reach a real spawn on a confirmed, non-dry-run flash path.

    **`venv_bin_dir` is pinned to `None` explicitly, not merely left to PATH=""
    (tan-cli#289 review).** tan-cli#289 widened the tool gate to PATH **or**
    the resolved workspace venv, and `venv_bin_dir` walks from `tmp_path`
    upward to the filesystem root looking for a west-capable `.venv` -- an
    ancestor `.venv` that also happens to provide `JLinkExe` would make this
    "PATH=''" guard alone insufficient, and PATH cannot rule that out (there is
    no env-var override for venv resolution). Pinned the same way
    `test_build_planner_python.py:74-84` pins `find_workspace_venv` to `None`.
    `subprocess.run` is ALSO stubbed to raise -- belt and suspenders: even if
    the tool gate somehow passed, this makes an actual spawn structurally
    impossible rather than merely host-dependent-unlikely.

    The original version of this test also asserted a false premise: it claimed
    `mkstemp` raises when `TMPDIR`/`TEMP`/`TMP` point at a nonexistent directory,
    but `tempfile.gettempdir()` falls back past all three, so it passed for an
    unrelated reason on every host -- the tool gate without a probe, a real spawn
    with one. The hostile temp vars are kept (they must not break anything), but
    the assertion now rests on the tool gate, which is what actually fires.
    """
    missing = str(tmp_path / "no" / "such" / "dir")
    monkeypatch.setenv("TMPDIR", missing)
    monkeypatch.setenv("TEMP", missing)
    monkeypatch.setenv("TMP", missing)
    monkeypatch.setenv("PATH", "")
    monkeypatch.delenv("ZEPHYR_BASE", raising=False)
    monkeypatch.setattr(flash_cmd, "venv_bin_dir", lambda *_a, **_k: None)

    def _must_not_spawn(*_a, **_k):
        raise AssertionError(
            "a confirmed, non-dry-run Flow D entry attempted to spawn a "
            "process -- the maintainer's bench has a probe on a live AEN EVK"
        )

    monkeypatch.setattr(flash_cmd.subprocess, "run", _must_not_spawn)

    (tmp_path / "build").mkdir(exist_ok=True)
    (tmp_path / "sdk" / "scripts").mkdir(parents=True, exist_ok=True)
    (tmp_path / "sdk" / "scripts" / "alp_project.py").write_text("", encoding="utf-8")
    manifest = """schema_version: 1
hw_info: {sku: S}
slices:
- {core_id: c1, os: zephyr, output_artefact: a.bin, status: ok,
   flash_method: alif_mram_jlink,
   flash_args: {jlink_flash_device: PART_PROFILE, slot0_load_address: "0x80010000",
                atoc: atoc.bin, atoc_address: "0x8057F5B0", confirm: true}}
helper_mcus: []
boot_order: []
"""
    (tmp_path / "build" / "system-manifest.yaml").write_text(
        manifest, encoding="utf-8", newline=""
    )

    exit_code, data, issues, _lines, _sdk = flash_cmd._run(
        app_path=".", build_root_arg=None, sdk_root_arg=str(tmp_path / "sdk"),
        board_yaml=None, core=None, helper=None, dry_run=False,
        skip_missing_tools=False, capture=True, cwd=str(tmp_path),
    )

    assert exit_code == 1
    assert [issue.code for issue in issues] == ["flash.entry-failed"]
    # And prove no burn was even attempted: the entry died at the TOOL GATE,
    # before any Commander script was written or spawned.
    message = data["entries"][0]["message"]
    assert "on PATH; none found" in message, message


def test_text_mode_writes_nothing_to_stdout(tmp_path):
    """Text mode is stderr-only. A byte on stdout here is not merely untidy: the
    same process writes the envelope to stdout in JSON mode, and a caller that
    reads stdout whole gets a corrupt document the moment the two mix."""
    exit_code, out, err = run_flash(tmp_path, "--dry-run")
    assert out == "", f"stdout must stay empty in text mode, got {out!r}"
    assert "flash:" in err
    assert exit_code == 0


def test_bad_format_value_is_a_usage_error_with_empty_stdout(tmp_path):
    exit_code, out, err = run_flash(tmp_path, "--format", "xml")
    assert out == ""
    assert exit_code != 0
    assert "xml" in err


def test_internal_failure_is_an_envelope_not_a_traceback(tmp_path, monkeypatch, capsys):
    """The guard itself. `_run` is replaced with something that raises a type
    nothing else catches; the command must still emit a well-formed envelope with
    exit 5.

    Driven in-process on purpose -- the point is the guard, and there is no way
    to make the real `_run` raise from outside without also changing what is
    being tested.
    """
    from tan.commands import flash_cmd
    import typer

    def boom(**_kwargs):
        raise RecursionError("planted")

    monkeypatch.setattr(flash_cmd, "_run", boom)
    monkeypatch.setattr("tan.envelope._emitted", False, raising=False)
    monkeypatch.chdir(tmp_path)

    class _Ctx:
        """The one thing `flash` reads off `typer.Context`: the root callback's
        recorded `--format`."""

        obj = None

    with pytest.raises(typer.Exit) as raised:
        flash_cmd.flash(
            _Ctx(), app_path=".", project=None, build_root=None, sdk_root=None,
            board_yaml=None, core=None, helper=None, dry_run=False,
            skip_missing_tools=False, output_format="json",
        )
    assert raised.value.exit_code == 5
    payload = json.loads(capsys.readouterr().out)
    assert payload["command"] == "flash"
    assert payload["exitCode"] == 5
    assert payload["ok"] is False
    assert [i["code"] for i in payload["issues"]] == ["flash.internal-failure"]
    assert "RecursionError: planted" in payload["issues"][0]["message"]
    # `project` is still reported: it is resolved OUTSIDE the guard precisely so
    # the recovery path never has to call something that can throw (the double
    # fault this port already shipped once).
    assert payload["project"]["root"].endswith(Path(tmp_path).name)


def test_a_flash_tool_that_dies_or_returns_garbage(tmp_path):
    """A spawned flash tool that exits non-zero having written NON-UTF-8 bytes,
    and one that writes nothing at all.

    `_capture_tail` reads that output to build the failure message, so a
    strict decoder here would turn a misbehaving vendor tool into a traceback --
    on the code path that runs immediately after a real device write."""
    from tan.commands.flash_cmd import _Outcome, _capture_tail, _execute_message

    garbage = _Outcome(
        success=False, stderr="ok\n�� bad\nlast line\n", returncode=3, captured=True
    )
    assert _capture_tail(garbage) == "ok | �� bad | last line"
    assert _execute_message(garbage, "yocto_wic", "c1").startswith("yocto_wic[c1]: ok |")

    # Killed by a signal: no output at all, so the rc IS the diagnosis.
    killed = _Outcome(success=False, returncode=-9, captured=True)
    assert _capture_tail(killed) == "exited rc=-9"

    # Whitespace-only stderr falls back to stdout, matching the oracle.
    only_stdout = _Outcome(
        success=False, stdout="from stdout\n", stderr="   \n", returncode=1, captured=True
    )
    assert _capture_tail(only_stdout) == "from stdout"

    # More than four lines keeps the LAST four, in order.
    many = _Outcome(
        success=False, stderr="\n".join(f"l{i}" for i in range(9)), returncode=1, captured=True
    )
    assert _capture_tail(many) == "l5 | l6 | l7 | l8"

    # A success never produces a tail -- the caller uses `plan.ok_message`.
    assert _capture_tail(_Outcome(success=True, captured=True)) is None


def test_a_flash_tool_that_hangs_is_killed_not_waited_on_forever():
    """Every spawn carries a timeout. A probe stuck mid-handshake or a `dd` on a
    device that stopped answering must not hang `tan` until the CI runner's own
    timeout with no output at all (I-23's failure shape)."""
    from tan.commands.flash_cmd import _spawn

    outcome = _spawn(
        [sys.executable, "-c", "import time; time.sleep(30)"], capture=True, timeout=1.0
    )
    assert outcome.success is False
    assert "timed out after 1s and was killed" in outcome.stderr


def test_a_tool_that_does_not_exist_is_a_failed_spawn_not_a_traceback():
    from tan.commands.flash_cmd import _spawn

    outcome = _spawn(["definitely-not-a-real-binary-xyz"], capture=True, timeout=5.0)
    assert outcome.success is False
    assert "could not spawn" in outcome.stderr


def test_a_deleted_working_directory_still_produces_an_envelope(monkeypatch, capsys):
    """The double fault. `project` is resolved OUTSIDE the exception guard,
    because the guard's own recovery path reports it -- so anything on that path
    that can throw makes the guard unable to report at all. `os.getcwd()` throws
    `FileNotFoundError` when the cwd has been deleted underneath the process,
    which is entirely reachable: a flash normally follows a build, and a cleanup
    script can remove the tree in between.

    The most recent Critical in this port was exactly this shape -- a helper that
    throws being called from the guard's recovery path.
    """
    from tan.commands import flash_cmd
    import typer

    def gone():
        raise FileNotFoundError(2, "No such file or directory")

    monkeypatch.setattr(flash_cmd.os, "getcwd", gone)
    monkeypatch.setattr("tan.envelope._emitted", False, raising=False)

    class _Ctx:
        obj = None

    with pytest.raises(typer.Exit) as raised:
        flash_cmd.flash(
            _Ctx(), app_path=".", project=None, build_root=None, sdk_root=None,
            board_yaml=None, core=None, helper=None, dry_run=True,
            skip_missing_tools=False, output_format="json",
        )
    payload = json.loads(capsys.readouterr().out)
    assert payload["command"] == "flash"
    assert raised.value.exit_code == payload["exitCode"]
    # An envelope, whatever the outcome -- never a traceback and never an empty
    # stdout. Both `project` keys are present (possibly null, which the contract
    # allows for `project`, unlike `sdk`).
    assert set(payload["project"]) == {"root", "boardYaml"}
    assert payload["issues"], "a failure must always carry an issue"


# ── Flow D: no oracle counterpart, so it is pinned entirely here ────────────

FLOW_D_ARGS = {
    "jlink_flash_device": "PART_PROFILE",
    "slot0_load_address": "0x80010000",
    "atoc": "/blobs/AppTocPackage.bin",
    "atoc_address": "0x8057F5B0",
}


def flow_d_inputs(**overrides):
    args = {**FLOW_D_ARGS, **overrides}
    for key, value in list(args.items()):
        if value is None:
            del args[key]
    return FlashInputs(
        artefact="/build/zephyr/zephyr.bin", flash_args=args, core_id="m55_he", sku="S"
    )


def test_flow_d_is_selected_over_flow_a_only_when_the_data_arms_it():
    """Flow D is the DEFAULT, and the switch is made from DATA alone -- never
    from a SKU, an address, or any other silicon knowledge tan is forbidden to
    carry (I-26 / ADR-0017). Arming needs only `jlink_flash_device`:
    `slot0_load_address` is not an arming key, it only selects the mramxip SHAPE
    once Flow D is already armed (see
    `test_flow_d_default_shape_omits_the_app_blob_when_slot0_load_address_is_absent`
    and
    `test_flow_d_mramxip_shape_still_writes_both_blobs_when_slot0_load_address_is_present`)."""
    armed = FlashTarget(SLICE, "m55_he", "zephyr_west_flash", FLOW_D_ARGS)
    assert select_flash_method(armed) == "alif_mram_jlink"

    # No jlink_flash_device -> Flow A, i.e. `west flash` on the board.cmake
    # default runner: without the part-number profile J-Link has no MRAM
    # loader to dispatch to at all.
    plain = FlashTarget(SLICE, "m55_he", "zephyr_west_flash", {})
    assert select_flash_method(plain) == "zephyr_west_flash"
    no_device = {k: v for k, v in FLOW_D_ARGS.items() if k != "jlink_flash_device"}
    assert select_flash_method(FlashTarget(SLICE, "m", "zephyr_west_flash", no_device)) == (
        "zephyr_west_flash"
    )

    # A device profile with NO `slot0_load_address` still arms Flow D -- it just
    # takes the default single-ATOC-blob shape (the ATOC embeds the app, so
    # there is nothing to `loadbin` an app to).
    no_slot0_load_address = {k: v for k, v in FLOW_D_ARGS.items() if k != "slot0_load_address"}
    assert select_flash_method(FlashTarget(SLICE, "m", "zephyr_west_flash", no_slot0_load_address)) == (
        "alif_mram_jlink"
    )

    # An explicitly-named method is never re-routed -- the preference applies to
    # the DEFAULT recipe only.
    named = FlashTarget(SLICE, "m", "swd_probe", FLOW_D_ARGS)
    assert select_flash_method(named) == "swd_probe"
    assert flow_d_available(FLOW_D_ARGS)
    assert flow_d_available(no_slot0_load_address)
    assert not flow_d_available(no_device)
    assert not flow_d_available("TBD")

    # A present-but-NULL `jlink_flash_device` (bare `jlink_flash_device:` in
    # YAML) must still ARM Flow D -- collapsing it to "unarmed" would silently
    # burn the entry over the SE-UART (Flow A) with no diagnostic at all. The
    # loud refusal comes from `plan_alif_mram_jlink`'s own explicit
    # `_fa_has_key` re-check on `fa_str_checked`'s `None` (distinguishing
    # "present but null/empty" from "absent") once Flow D is armed and
    # dispatched, not from this predicate -- `fa_str_checked` itself returns
    # `None` for present-but-null same as absent, it does not raise.
    null_device = {**FLOW_D_ARGS, "jlink_flash_device": None}
    assert flow_d_available(null_device)
    assert select_flash_method(FlashTarget(SLICE, "m", "zephyr_west_flash", null_device)) == (
        "alif_mram_jlink"
    )
    with pytest.raises(FlashPlanError, match="jlink_flash_device is present but null/empty"):
        plan_alif_mram_jlink(
            FlashInputs(artefact="/b/z.bin", flash_args=null_device, core_id="m", sku="S"),
            lambda t: True,
        )


def test_an_unquoted_slot0_load_address_still_arms_flow_d():
    """PyYAML parses an unquoted `slot0_load_address: 0x80010000` as an INTEGER.
    `slot0_load_address` selects the mramxip two-blob SHAPE (Flow D itself is armed
    by `jlink_flash_device` alone); that selection must key on PRESENCE, not
    on "is a non-empty string" -- a string-shaped check would call the shape
    unselected and silently emit the default single-blob write instead.
    Shape is never decided by a quoting detail."""
    numeric = {**FLOW_D_ARGS, "slot0_load_address": 0x80010000}
    assert flow_d_available(numeric)
    assert select_flash_method(FlashTarget(SLICE, "m", "zephyr_west_flash", numeric)) == (
        "alif_mram_jlink"
    )
    # ...and the builder round-trips it to the same hex string a quoted value gives.
    plan = plan_alif_mram_jlink(
        FlashInputs(artefact="/b/z.bin", flash_args={**numeric, "confirm": True},
                    core_id="m", sku="S"),
        lambda t: True,
    )
    assert "loadbin /b/z.bin 0x80010000" in plan.jlink_script

    # A present-but-UNUSABLE value is a loud refusal, never a silent Flow A.
    broken = {**FLOW_D_ARGS, "slot0_load_address": ["not", "an", "address"]}
    assert flow_d_available(broken)
    with pytest.raises(FlashPlanError):
        plan_alif_mram_jlink(
            FlashInputs(artefact="/b/z.bin", flash_args=broken, core_id="m", sku="S"),
            lambda t: True,
        )


def test_flow_d_script_writes_both_blobs_verifies_and_pin_resets():
    plan = plan_alif_mram_jlink(flow_d_inputs(confirm=True), lambda t: t == "JLinkExe")
    assert plan.argv[0] == "JLinkExe"
    assert "-device" in plan.argv and "PART_PROFILE" in plan.argv
    lines = plan.jlink_script.splitlines()
    assert lines == [
        "si SWD",
        "speed 4000",
        "device PART_PROFILE",
        "connect",
        "loadbin /build/zephyr/zephyr.bin 0x80010000",
        "loadbin /blobs/AppTocPackage.bin 0x8057F5B0",
        "verifybin /build/zephyr/zephyr.bin 0x80010000",
        "verifybin /blobs/AppTocPackage.bin 0x8057F5B0",
        # PIN reset, not a core reset: the Secure Enclave boot ROM must re-read
        # and boot the ATOC, exactly as after an SE-UART burn.
        "RSetType 2",
        "r",
        "g",
        "exit",
    ]
    assert plan.jlink_script.endswith("\n")
    assert plan.planning_only is False
    # The success line names BOTH placements and the profile that unlocked the
    # loader -- the three values a bench log needs to reproduce the burn.
    assert plan.ok_message == (
        "alif_mram_jlink[m55_he]: app -> 0x80010000, signed ATOC -> 0x8057F5B0 "
        "via J-Link (PART_PROFILE); verified and PIN-reset"
    )


def test_flow_d_is_confirm_gated_like_every_other_persistent_write():
    unconfirmed = plan_alif_mram_jlink(flow_d_inputs(), lambda t: True)
    assert unconfirmed.planning_only is True
    forced = plan_alif_mram_jlink(
        FlashInputs(
            artefact="/b/zephyr.bin", flash_args=FLOW_D_ARGS, core_id="m", sku="S",
            force_confirm=True,
        ),
        lambda t: True,
    )
    assert forced.planning_only is False


@pytest.mark.parametrize(
    "missing, expected",
    [
        ("jlink_flash_device", "jlink_flash_device is required"),
        ("atoc", "flash_args.atoc"),
        ("atoc_address", "flash_args.atoc"),
    ],
)
def test_flow_d_refuses_rather_than_guessing_any_required_identifier(missing, expected):
    """Every REQUIRED Flow D identifier is a hardware fact that arrives in
    `flash_args`. None has a default: a guessed address is a write to the
    wrong place on a part whose Secure Enclave then boots whatever is there.

    `slot0_load_address` is deliberately absent from this table -- it is OPTIONAL
    (see `test_flow_d_default_shape_omits_the_app_blob_when_slot0_load_address_is_
    absent`), not a fourth required identifier."""
    with pytest.raises(FlashPlanError) as raised:
        plan_alif_mram_jlink(flow_d_inputs(**{missing: None}), lambda t: True)
    assert expected in str(raised.value)


def test_flow_d_default_shape_omits_the_app_blob_when_slot0_load_address_is_absent():
    """The day-to-day default (`flash-jlink.sh`) writes ONE self-contained
    ATOC blob, not the two-blob mramxip shape -- the shape this port emitted
    unconditionally before this fix, which wrote the app to `slot0_load_address`
    while nothing set the app's own build to link there, corrupting the burn.
    """
    plan = plan_alif_mram_jlink(
        flow_d_inputs(slot0_load_address=None, confirm=True), lambda t: t == "JLinkExe"
    )
    lines = plan.jlink_script.splitlines()
    assert lines == [
        "si SWD",
        "speed 4000",
        "device PART_PROFILE",
        "connect",
        "loadbin /blobs/AppTocPackage.bin 0x8057F5B0",
        "verifybin /blobs/AppTocPackage.bin 0x8057F5B0",
        "RSetType 2",
        "r",
        "g",
        "exit",
    ]
    assert not any("zephyr.bin" in line for line in lines)
    assert plan.ok_message == (
        "alif_mram_jlink[m55_he]: signed ATOC (app embedded) -> 0x8057F5B0 "
        "via J-Link (PART_PROFILE); verified and PIN-reset"
    )


def test_flow_d_mramxip_shape_still_writes_both_blobs_when_slot0_load_address_is_present():
    """The ITCM-overflow exception (`flash-jlink-mramxip.sh`) -- unchanged from
    before this fix, just now reachable only when `slot0_load_address` opts in."""
    plan = plan_alif_mram_jlink(flow_d_inputs(confirm=True), lambda t: t == "JLinkExe")
    lines = plan.jlink_script.splitlines()
    assert "loadbin /build/zephyr/zephyr.bin 0x80010000" in lines
    assert "verifybin /build/zephyr/zephyr.bin 0x80010000" in lines


@pytest.mark.parametrize("bad_value", ["", None], ids=["empty-string", "null"])
def test_flow_d_present_but_null_or_empty_slot0_load_address_refuses(bad_value):
    """A `slot0_load_address` KEY that is present but resolves to an empty string or
    a null must refuse loudly, exactly like any other malformed value --
    never silently fall back to the default single-ATOC-blob shape. Both were
    a silent default-shape selection pre-fix: `fa_str_checked` collapses a
    present-but-null value and a genuinely-absent key to the same `None`, so
    the `app_address is not None` check alone could not tell them apart. A
    manifest quoting detail must never decide which shape burns."""
    args = {**FLOW_D_ARGS, "slot0_load_address": bad_value, "confirm": True}
    with pytest.raises(FlashPlanError) as raised:
        plan_alif_mram_jlink(
            FlashInputs(artefact="/b/z.bin", flash_args=args, core_id="m", sku="S"),
            lambda t: True,
        )
    assert "slot0_load_address" in str(raised.value)


def test_flow_d_mramxip_shape_refuses_a_non_raw_bin_artefact():
    """A slot0-linked artefact that is not a raw `.bin` (e.g. `zephyr.elf`) must
    never reach `loadbin ... slot0_load_address` -- that writes the artefact's
    own headers into MRAM at the load address instead of the app image
    (tan-cli#311). Unlike `plan_swd_probe`'s ELF/HEX fallback to `loadfile`,
    there is no fallback here: `loadfile` would silently ignore
    `slot0_load_address`, which is a worse failure than a refusal."""
    args = {**FLOW_D_ARGS, "confirm": True}
    with pytest.raises(FlashPlanError) as raised:
        plan_alif_mram_jlink(
            FlashInputs(
                artefact="/build/zephyr/zephyr.elf", flash_args=args, core_id="m", sku="S"
            ),
            lambda t: True,
        )
    message = str(raised.value)
    assert "zephyr.elf" in message
    assert "zephyr.bin" in message
    assert "slot0_load_address" in message


def test_flow_d_holds_no_part_number_of_its_own():
    """The whole point of resolving the profile from metadata. `alif`,
    `AE822...` and the MRAM addresses must appear NOWHERE in the module -- not as
    a default, not as a fallback, not in a docstring example that a later
    "helpful" refactor could promote into code.

    `alif_mram_jlink` (the method NAME) and `jlink_flash_device` (the metadata
    KEY) are allowed: a method name and a key name are not hardware facts.
    """
    source = Path(flash_plan.__file__).read_text(encoding="utf-8")
    for forbidden in ("AE822", "E1M-AEN", "0x80010000", "0x8057", "M55_HE", "0x4C013477"):
        assert forbidden not in source, f"{forbidden} is a hardware fact; resolve it from data"


@pytest.mark.parametrize("bad", ["a;b", "../x", "/x", "C:/x", "a b", "dev\nice", ""])
def test_flow_d_device_profile_is_charset_guarded(bad):
    """The profile is interpolated into a `device <name>` line of a J-Link
    Commander script -- a line-oriented interpreter, so a newline is a
    command-injection primitive into a process holding SWD write access."""
    with pytest.raises(FlashPlanError):
        plan_alif_mram_jlink(flow_d_inputs(jlink_flash_device=bad), lambda t: True)


@pytest.mark.parametrize("bad", ["0x8000 r", "zzz", "0x", "80010000\nr", "-1"])
def test_flow_d_addresses_are_charset_guarded(bad):
    with pytest.raises(FlashPlanError):
        plan_alif_mram_jlink(flow_d_inputs(slot0_load_address=bad), lambda t: True)


def test_flow_d_probe_serial_is_optional_and_has_no_default():
    """No default serial: a bench-wide serial can be SHARED by two probes that
    differ only by USB path, so a silent default can select the wrong board."""
    without = plan_alif_mram_jlink(flow_d_inputs(confirm=True), lambda t: True)
    assert "SelectEmuBySN" not in without.jlink_script
    with_serial = plan_alif_mram_jlink(
        flow_d_inputs(confirm=True, jlink_serial="123456789"), lambda t: True
    )
    assert with_serial.jlink_script.startswith("SelectEmuBySN 123456789\n")


def test_flow_d_preflight_is_absent_unless_the_manifest_supplies_both_values():
    """Both `expect_dpidr` and `jlink_device` GENUINELY absent means NO preflight:
    tan cannot supply either value, and a wrong expected ID would refuse every
    good board. A half-armed manifest -- one key present, the other genuinely
    absent -- refuses instead: supplying `expect_dpidr` alone is the manifest's
    unambiguous statement that it wanted the wrong-board guard armed, so
    silently skipping it must not happen (see
    `test_flow_d_preflight_half_armed_by_a_missing_partner_key_refuses`)."""
    assert flash_plan.flow_d_preflight_script(flow_d_inputs()) is None
    prepared = flash_plan.flow_d_preflight_script(
        flow_d_inputs(expect_dpidr="0x4C013477", jlink_device="Generic-Attach", jlink_serial="7")
    )
    assert prepared is not None
    script, expected = prepared
    assert expected == "0x4C013477"
    assert script.splitlines() == [
        "SelectEmuBySN 7",
        "si SWD",
        "speed 4000",
        # the ATTACH profile, not the part-number one: the part profile cannot
        # connect to a live/running core.
        "device Generic-Attach",
        "connect",
        "exit",
    ]


@pytest.mark.parametrize(
    "overrides",
    [{"expect_dpidr": "0x4C013477"}, {"jlink_device": "Generic-Attach"}],
    ids=["expect_dpidr-only", "jlink_device-only"],
)
def test_flow_d_preflight_half_armed_by_a_missing_partner_key_refuses(overrides):
    """One of `expect_dpidr` / `jlink_device` present, the other GENUINELY
    absent (not null -- that is the two present-but-null tests below), must
    refuse loudly. Supplying either key alone is the manifest's unambiguous
    statement that it wanted the wrong-board guard armed; silently returning
    `None` (no preflight) would drop that guard with no diagnostic at all,
    immediately before the one write this backend's own docstring calls
    unrecoverable."""
    with pytest.raises(FlashPlanError) as raised:
        flash_plan.flow_d_preflight_script(flow_d_inputs(**overrides))
    message = str(raised.value)
    assert "expect_dpidr" in message
    assert "jlink_device" in message


@pytest.mark.parametrize("bad_value", ["", None], ids=["empty-string", "null"])
def test_flow_d_preflight_present_but_null_or_empty_expect_dpidr_refuses(bad_value):
    """`expect_dpidr` PRESENT but resolving to `None` (empty string or YAML
    null) must refuse loudly, exactly like `slot0_load_address` -- never silently
    fall through to `None` (no preflight). Reusing the "genuinely absent"
    path there would drop the SW-DP IDR check with no diagnostic, on the
    write path this backend's own docstring calls "the one unrecoverable
    mistake" it can make."""
    args = {**FLOW_D_ARGS, "expect_dpidr": bad_value, "jlink_device": "Generic-Attach"}
    with pytest.raises(FlashPlanError) as raised:
        flash_plan.flow_d_preflight_script(
            FlashInputs(artefact="/b/z.bin", flash_args=args, core_id="m", sku="S")
        )
    assert "expect_dpidr" in str(raised.value)


@pytest.mark.parametrize("bad_value", ["", None], ids=["empty-string", "null"])
def test_flow_d_preflight_present_but_null_or_empty_jlink_device_refuses(bad_value):
    """Same collapse, same refusal, for the read-device key: a `jlink_device: ""`
    or bare `jlink_device:` must not silently produce `None` (no preflight)
    when `expect_dpidr` is otherwise good."""
    args = {**FLOW_D_ARGS, "expect_dpidr": "0x4C013477", "jlink_device": bad_value}
    with pytest.raises(FlashPlanError) as raised:
        flash_plan.flow_d_preflight_script(
            FlashInputs(artefact="/b/z.bin", flash_args=args, core_id="m", sku="S")
        )
    assert "jlink_device" in str(raised.value)


def _flow_d_preflight_inputs():
    args = {**FLOW_D_ARGS, "expect_dpidr": "0x4C013477", "jlink_device": "Generic-Attach"}
    return FlashInputs(artefact="/b/z.bin", flash_args=args, core_id="m", sku="S")


def _stub_flow_d_probe(monkeypatch, stdout: str, stderr: str = "", success: bool = True):
    """Make `_flow_d_preflight` reach a fake connect banner without a real
    J-Link on PATH or an actual spawn -- `_tool_available`/
    `_programs_resolved_in_venv` are the tool-gate and venv-resolution steps
    ahead of the spawn, neither of which this test cares about."""
    monkeypatch.setattr(flash_cmd, "_tool_available", lambda *_a, **_k: True)
    monkeypatch.setattr(flash_cmd, "_programs_resolved_in_venv", lambda argv, _venv_bin: argv)
    monkeypatch.setattr(
        flash_cmd,
        "_spawn_jlink",
        lambda *_a, **_k: flash_cmd._Outcome(success=success, stdout=stdout, stderr=stderr),
    )


def test_flow_d_preflight_a_different_reported_dp_id_keeps_the_wiring_message(monkeypatch):
    """tan-cli#312, case (a): the probe DID connect and reported a real, just
    different, SW-DP ID -- a genuine wrong-board / wiring / probe-selection
    problem, so the original remediation stands unchanged."""
    _stub_flow_d_probe(
        monkeypatch,
        stdout="Connecting to target via SWD\nFound SW-DP with ID 0x2BA01477\n",
    )
    message = flash_cmd._flow_d_preflight(_flow_d_preflight_inputs())
    assert message is not None
    assert "Check the probe selection (flash_args.jlink_serial) and the wiring" in message
    assert "re-enumerat" not in message


def test_flow_d_preflight_no_dp_id_at_all_gets_the_re_enumeration_message(monkeypatch):
    """tan-cli#312, case (b): measured verbatim on the rc3 bench run -- the
    probe refused the connect outright, mid re-enumeration after a prior
    `JLinkExe` close, and reported no SW-DP ID whatsoever. This must NOT get
    the wiring/jlink_serial sentence: nothing was wrong with either."""
    _stub_flow_d_probe(
        monkeypatch,
        stdout="Connecting to J-Link ...FAILED: Cannot connect to the probe/programmer.\n",
        stderr="J-Link uptime (since boot): 0d 00h 00m 01s\n",
        success=False,
    )
    message = flash_cmd._flow_d_preflight(_flow_d_preflight_inputs())
    assert message is not None
    assert "re-enumerat" in message
    assert "Check the probe selection" not in message
    assert "0x4C013477" in message


def test_flow_d_preflight_an_unrecognised_banner_falls_back_to_the_wiring_message(monkeypatch):
    """Conservative by design (tan-cli#312): a banner with neither a
    recognisable DP-ID token NOR SEGGER's own connect-refused wording is not
    confidently "just re-enumerating" -- the detector must not guess the
    wiring is fine, so this keeps the original sentence."""
    _stub_flow_d_probe(monkeypatch, stdout="some unrecognised probe banner\n")
    message = flash_cmd._flow_d_preflight(_flow_d_preflight_inputs())
    assert message is not None
    assert "Check the probe selection (flash_args.jlink_serial) and the wiring" in message
    assert "re-enumerat" not in message


def test_flow_d_preflight_a_target_level_cannot_connect_keeps_the_wiring_message(monkeypatch):
    """tan-cli#312 review finding: an unplugged SWD ribbon / no board present
    produces "Cannot connect to target." -- a genuine wiring problem, not a
    re-enumerating probe. This must NOT get the "not a wiring... problem"
    re-enumeration message: on a bench that would turn a real unplugged cable
    into an infinite wait-and-retry loop instead of the correct remediation."""
    _stub_flow_d_probe(
        monkeypatch,
        stdout=(
            "Connecting to target via SWD\n"
            "InitTarget() start\n"
            "InitTarget() end\n"
            "Cannot connect to target.\n"
        ),
        success=False,
    )
    message = flash_cmd._flow_d_preflight(_flow_d_preflight_inputs())
    assert message is not None
    assert "Check the probe selection (flash_args.jlink_serial) and the wiring" in message
    assert "re-enumerat" not in message


def test_flow_d_preflight_a_wrong_jlink_serial_keeps_the_wiring_message(monkeypatch):
    """tan-cli#312 review finding: a probe that IS reachable via USB but
    refuses the requested `flash_args.jlink_serial` prints "Cannot connect to
    J-Link." -- a real probe-selection problem, so this keeps the original
    wiring/`jlink_serial` remediation rather than the re-enumeration message."""
    _stub_flow_d_probe(
        monkeypatch,
        stdout="Connecting to J-Link via USB...FAILED: Cannot connect to J-Link.\n",
        success=False,
    )
    message = flash_cmd._flow_d_preflight(_flow_d_preflight_inputs())
    assert message is not None
    assert "Check the probe selection (flash_args.jlink_serial) and the wiring" in message
    assert "re-enumerat" not in message


def test_flow_d_needs_jlink_on_path_for_a_real_run():
    with pytest.raises(FlashPlanError) as raised:
        plan_alif_mram_jlink(flow_d_inputs(confirm=True), lambda t: False)
    assert "V9.46+" in str(raised.value)


def test_flow_d_dry_run_previews_without_probing_path():
    plan = plan_alif_mram_jlink(
        FlashInputs(
            artefact="/b/zephyr.bin", flash_args=FLOW_D_ARGS, core_id="m", sku="S", dry_run=True
        ),
        lambda t: False,
    )
    assert plan.argv[0] == "JLinkExe"
    assert plan.planning_only is True


def test_flow_d_end_to_end_reports_planned_and_the_confirm_issue(tmp_path):
    """The one Flow D case driven through the real CLI: unconfirmed, so it plans
    and writes nothing. `status: planned` (not `ok`) plus
    `flash.confirm-required` is I-30's contract -- a JSON consumer must be able
    to tell "nothing was written" from "programmed the device"."""
    manifest = """schema_version: 1
hw_info: {sku: S}
slices:
- {core_id: m55_he, os: zephyr, output_artefact: zephyr/zephyr.bin, status: ok,
   flash_method: zephyr_west_flash,
   flash_args: {jlink_flash_device: PART_PROFILE, slot0_load_address: "0x80010000",
                atoc: atoc.bin, atoc_address: "0x8057F5B0"}}
helper_mcus: []
boot_order: []
"""
    exit_code, out, _ = run_flash(tmp_path, "--format", "json", "--dry-run", manifest=manifest)
    payload = envelope(out)
    assert exit_code == 0
    entry = payload["data"]["entries"][0]
    # The envelope reports the method that actually DISPATCHED, so a consumer can
    # see which transport ran -- not the recipe name the manifest carried.
    assert entry["method"] == "alif_mram_jlink"
    assert "-device PART_PROFILE" in entry["message"]
    # The temp Commander script does not exist yet and its real name carries a
    # pid + nanosecond stamp; a placeholder is what reaches the envelope.
    assert "<generated.jlink>" in entry["message"]
    assert "tan-flash-" not in entry["message"]


def test_flow_d_dry_run_surfaces_a_half_armed_preflight_as_a_failure(tmp_path):
    """A half-armed `expect_dpidr`/`jlink_device` pair used to be caught only at
    real-write time (`_flow_d_preflight`, which never runs before the confirm
    gate): `tan flash --dry-run` on this exact manifest used to report
    `status: planned` / exit 0 with no diagnostic at all. The validate-only
    half now runs PLAN-TIME, before the confirm/dry-run gate, so the same
    misconfiguration surfaces as `flash.entry-failed` / exit 1 under
    `--dry-run` too -- precisely where a customer should learn their manifest
    is wrong, not only once they confirm a real write."""
    manifest = """schema_version: 1
hw_info: {sku: S}
slices:
- {core_id: m55_he, os: zephyr, output_artefact: zephyr/zephyr.bin, status: ok,
   flash_method: zephyr_west_flash,
   flash_args: {jlink_flash_device: PART_PROFILE, slot0_load_address: "0x80010000",
                atoc: atoc.bin, atoc_address: "0x8057F5B0",
                expect_dpidr: "0x4C013477"}}
helper_mcus: []
boot_order: []
"""
    exit_code, out, _ = run_flash(tmp_path, "--format", "json", "--dry-run", manifest=manifest)
    payload = envelope(out)
    assert exit_code == 1
    assert payload["ok"] is False
    entry = payload["data"]["entries"][0]
    assert entry["status"] == "failed"
    assert "expect_dpidr" in entry["message"]
    assert "jlink_device" in entry["message"]
    codes = {issue["code"] for issue in payload["issues"]}
    assert "flash.entry-failed" in codes


# ── Flow D: the ATOC address is a BUILD-TIME output, not metadata ──────────
#
# An earlier design assumed `atoc_address` lived under `metadata/**`. It does
# not: `app-gen-toc` writes it fresh into `app-package-map.txt` at SIGNING
# time and the runbook says outright it shifts per build/config. These pin the
# parser (`flash_plan.parse_atoc_start_address`) against real bench-script
# report text, and the IO glue (`flash_cmd._resolve_flow_d_atoc_address`) that
# feeds a parsed value into the plan without requiring the manifest to bake
# one in.


def test_parse_atoc_start_address_takes_the_last_match():
    """Mirrors every bench script's own
    `awk '/APP Package Start Address:/{print $NF}' app-package-map.txt | tail
    -1` -- a re-signed re-run APPENDS a fresh block, so the LAST line wins, not
    the first."""
    report = (
        "Device Algorithm Package\n"
        "APP Package Start Address: 0x8000F000\n"
        "\n"
        "Device Algorithm Package (re-signed)\n"
        "APP Package Start Address: 0x8057F5B0\n"
    )
    assert parse_atoc_start_address(report) == "0x8057F5B0"


def test_parse_atoc_start_address_is_none_when_the_marker_is_absent():
    assert parse_atoc_start_address("") is None
    assert parse_atoc_start_address("some other report entirely\n") is None


def test_resolve_flow_d_atoc_address_prefers_an_explicit_manifest_value(tmp_path):
    """An explicit `atoc_address` always wins over a parsed one -- and the map
    file is never even opened, so a stale/missing report cannot break a
    manifest that already carries the real value.

    The map file here is REAL and carries a DIFFERENT address than the
    explicit one, so a precedence bug that reads the map anyway is caught by
    the value, not just by object identity (a bug that fell through to
    `plan_alif_mram_jlink`'s generic refusal via the missing-file no-op would
    pass an `is args` check too)."""
    from tan.commands.flash_cmd import _resolve_flow_d_atoc_address

    (tmp_path / "app-package-map.txt").write_text(
        "APP Package Start Address: 0x8000F000\n", encoding="utf-8"
    )
    args = {"atoc_address": "0x8057F5B0", "atoc_map": "app-package-map.txt"}
    resolved = _resolve_flow_d_atoc_address(args, str(tmp_path), str(tmp_path))
    assert resolved is args
    assert resolved["atoc_address"] == "0x8057F5B0"


def test_resolve_flow_d_atoc_address_swallows_a_malformed_explicit_value(tmp_path):
    """A malformed `atoc_address` (not a string/bare-number shape) makes
    `fa_str_checked` raise; this helper must swallow that and return the dict
    UNTOUCHED so `plan_alif_mram_jlink` raises the real, precise refusal --
    not silently overwrite it with a value parsed from the map."""
    from tan.commands.flash_cmd import _resolve_flow_d_atoc_address

    (tmp_path / "app-package-map.txt").write_text(
        "APP Package Start Address: 0x8000F000\n", encoding="utf-8"
    )
    args = {"atoc_address": True, "atoc_map": "app-package-map.txt"}
    assert _resolve_flow_d_atoc_address(args, str(tmp_path), str(tmp_path)) is args


def test_resolve_flow_d_atoc_address_parses_the_map_file(tmp_path):
    from tan.commands.flash_cmd import _resolve_flow_d_atoc_address

    (tmp_path / "app-package-map.txt").write_text(
        "APP Package Start Address: 0x8057F5B0\n", encoding="utf-8"
    )
    args = {"atoc": "atoc.bin", "atoc_map": "app-package-map.txt"}
    resolved = _resolve_flow_d_atoc_address(args, str(tmp_path), str(tmp_path))
    assert resolved["atoc_address"] == "0x8057F5B0"
    assert resolved is not args, "must not mutate the manifest's own flash_args dict"
    assert "atoc_address" not in args


def test_resolve_flow_d_atoc_address_is_a_no_op_without_atoc_map(tmp_path):
    from tan.commands.flash_cmd import _resolve_flow_d_atoc_address

    args = {"atoc": "atoc.bin"}
    assert _resolve_flow_d_atoc_address(args, str(tmp_path), str(tmp_path)) is args


def test_resolve_flow_d_atoc_address_is_a_no_op_when_the_map_is_missing(tmp_path):
    """The map path resolves to nothing yet (signing has not run, or ran
    somewhere else) -- graceful no-op, letting `plan_alif_mram_jlink` raise its
    own precise refusal rather than this helper inventing a different one."""
    from tan.commands.flash_cmd import _resolve_flow_d_atoc_address

    args = {"atoc": "atoc.bin", "atoc_map": "app-package-map.txt"}
    assert _resolve_flow_d_atoc_address(args, str(tmp_path), str(tmp_path)) is args


def test_resolve_flow_d_atoc_address_refuses_loudly_when_the_marker_is_missing(tmp_path):
    """The map file WAS found -- it is not "no map yet", it is "found your map
    and could not get an address out of it". Falling through to
    `plan_alif_mram_jlink`'s generic "both required" refusal here would tell
    the user to do the thing (supply a map) they already did."""
    from tan.commands.flash_cmd import _resolve_flow_d_atoc_address

    (tmp_path / "app-package-map.txt").write_text("nothing useful here\n", encoding="utf-8")
    args = {"atoc": "atoc.bin", "atoc_map": "app-package-map.txt"}
    with pytest.raises(FlashPlanError) as raised:
        _resolve_flow_d_atoc_address(args, str(tmp_path), str(tmp_path))
    msg = str(raised.value)
    assert "app-package-map.txt" in msg
    assert "APP Package Start Address" in msg


def test_flow_d_end_to_end_resolves_atoc_address_from_the_build_output(tmp_path):
    """The real wiring, driven through the CLI: a manifest with `atoc_map`
    instead of a baked-in `atoc_address` must still PLAN successfully under
    `--dry-run` -- proving the address came from the build report, not from a
    refusal that `--dry-run` happens to mask. `--dry-run` is the only safe way
    to drive this end to end: it bypasses the J-Link tool gate entirely, so
    nothing here can ever reach a real probe."""
    (tmp_path / "build").mkdir(exist_ok=True)
    (tmp_path / "build" / "app-package-map.txt").write_text(
        "APP Package Start Address: 0x8057F5B0\n", encoding="utf-8"
    )
    manifest = """schema_version: 1
hw_info: {sku: S}
slices:
- {core_id: m55_he, os: zephyr, output_artefact: zephyr/zephyr.bin, status: ok,
   flash_method: zephyr_west_flash,
   flash_args: {jlink_flash_device: PART_PROFILE, slot0_load_address: "0x80010000",
                atoc: atoc.bin, atoc_map: app-package-map.txt}}
helper_mcus: []
boot_order: []
"""
    exit_code, out, _ = run_flash(tmp_path, "--format", "json", "--dry-run", manifest=manifest)
    payload = envelope(out)
    assert exit_code == 0
    entry = payload["data"]["entries"][0]
    assert entry["method"] == "alif_mram_jlink"
    assert entry["status"] == "ok"


def test_flow_d_end_to_end_fails_when_the_map_file_never_materialised(tmp_path):
    """No baked `atoc_address` and no report on disk yet: `plan_alif_mram_jlink`
    must still refuse loudly rather than the entry silently vanishing."""
    manifest = """schema_version: 1
hw_info: {sku: S}
slices:
- {core_id: m55_he, os: zephyr, output_artefact: zephyr/zephyr.bin, status: ok,
   flash_method: zephyr_west_flash,
   flash_args: {jlink_flash_device: PART_PROFILE, slot0_load_address: "0x80010000",
                atoc: atoc.bin, atoc_map: app-package-map.txt}}
helper_mcus: []
boot_order: []
"""
    exit_code, out, _ = run_flash(tmp_path, "--format", "json", "--dry-run", manifest=manifest)
    payload = envelope(out)
    assert exit_code == 1
    entry = payload["data"]["entries"][0]
    assert entry["status"] == "failed"
    # The distinguishing substring, not the `flash_args.atoc` prefix shared with
    # `flash_args.atoc_address` -- a prefix match cannot tell which required
    # field the refusal was actually about.
    assert "flash_args.atoc_address" in entry["message"]


# ── pure helpers with edge cases the oracle diff does not reach ─────────────


def test_i18_nested_west_build_dir_is_the_last_resort(tmp_path):
    """**I-18.** The planner emits `west build` with NO `-d`, so west's tree lands
    at `<buildDir>/build/` while the plan reports `<buildDir>/zephyr/zephyr.elf`.
    Rust reconciles this when it WRITES the manifest; this port's `build` does
    not write one yet, so `flash` resolves the nesting -- but only after the
    oracle's own candidates all miss, so it can never change a resolution the
    oracle already makes."""
    build_root = tmp_path / "build"
    nested = build_root / "build" / "c1-zephyr" / "zephyr"
    nested.mkdir(parents=True)
    (nested / "zephyr.elf").write_text("elf", encoding="utf-8")
    got = resolve_artefact_path(
        "c1-zephyr/zephyr/zephyr.elf", str(build_root), str(tmp_path), os.path.isfile
    )
    # The artefact's own separators survive the join, exactly as they do on the
    # oracle's `Path::join` -- the string is handed to `west flash --build-dir`.
    assert got == os.path.join(str(build_root), "build", "c1-zephyr/zephyr/zephyr.elf")
    assert os.path.isfile(got)

    # A real file at the oracle's OWN first candidate still wins.
    direct = build_root / "c1-zephyr" / "zephyr"
    direct.mkdir(parents=True)
    (direct / "zephyr.elf").write_text("elf", encoding="utf-8")
    got = resolve_artefact_path(
        "c1-zephyr/zephyr/zephyr.elf", str(build_root), str(tmp_path), os.path.isfile
    )
    assert got == os.path.join(str(build_root), "c1-zephyr/zephyr/zephyr.elf")


def test_nothing_on_disk_falls_back_to_the_build_candidate(tmp_path):
    got = resolve_artefact_path("x.bin", "/work/build", "/sdk", lambda _p: False)
    assert got == os.path.join("/work/build", "x.bin")


def test_rust_absolute_semantics_on_a_rooted_driveless_path():
    """`Path::is_absolute` on Windows needs a drive AND a root, so `/dev/sdb` is
    RELATIVE there. `os.path.isabs` disagreed with that until Python 3.13 and
    agrees from 3.13 on -- reaching for it would make artefact resolution differ
    between two supported interpreters on the same host."""
    if os.name == "nt":
        assert not is_rust_absolute("/dev/sdb")
        assert not is_rust_absolute("\\x")
        assert is_rust_absolute("C:/x")
        assert is_rust_absolute("C:\\x")
        assert not is_rust_absolute("C:x")
    else:
        assert is_rust_absolute("/dev/sdb")
        assert not is_rust_absolute("C:/x")


def test_zephyr_build_dir_preserves_mixed_separators():
    """The joined path mixes a native `build_root` with a `/`-authored manifest
    artefact, and the result is handed to `west flash --build-dir` verbatim.
    `Path.parent` would re-render it with the platform separator.

    NOT branched on `os.name`: the only `\\` here sits INSIDE one `/`-delimited
    component (`a\\build`), and every separator `dirname` has to find is a `/`,
    which `ntpath` and `posixpath` split identically. An earlier version of this
    test asserted `.../c1-zephyr/zephyr` off Windows on the assumption that
    POSIX splits this differently -- it does not, and the branch failed on
    ubuntu/macos while passing here."""
    assert zephyr_build_dir("C:/a\\build/c1-zephyr/zephyr/zephyr.elf") == "C:/a\\build/c1-zephyr"
    # A signed/merged artefact under `zephyr/` still resolves to the build dir --
    # the PARENT DIRECTORY name decides, never the basename.
    assert zephyr_build_dir("/b/c1/zephyr/zephyr.signed.hex") == "/b/c1"
    assert zephyr_build_dir("/b/c1/zephyr/merged.hex") == "/b/c1"
    # Not in a `zephyr/` subdir -> the artefact's own parent.
    assert zephyr_build_dir("/b/c1/app.bin") == "/b/c1"


def test_true_is_not_an_int_for_a_strict_accessor():
    """Python's `True` IS an `int`, so an unguarded `isinstance(raw, int)` would
    accept `jobs: true` and emit `-j 1`, and `base: true` would resolve to
    `0x00000001` -- a real address on real silicon."""
    with pytest.raises(FlashPlanError):
        fa_int_checked({"jobs": True}, "jobs")
    with pytest.raises(FlashPlanError):
        fa_str_checked({"base": True}, "base", True)


def test_explicit_zero_still_means_use_the_default():
    assert fa_int_checked({"speed": 0}, "speed") is None
    assert fa_int_checked({"speed": 9600}, "speed") == 9600


def test_pyyaml_absent_is_a_manifest_error_not_an_import_traceback(monkeypatch):
    """tan declares no YAML dependency, so PyYAML can genuinely be missing. That
    must surface as `flash.manifest-invalid` -- `flash` cannot pick a target
    without the manifest, and silently flashing nothing is the worse outcome."""
    import builtins

    real_import = builtins.__import__

    def refuse(name, *args, **kwargs):
        if name == "yaml":
            raise ImportError("no yaml here")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", refuse)
    with pytest.raises(ManifestError) as raised:
        parse_system_manifest("schema_version: 1\n")
    assert "PyYAML" in str(raised.value)


def test_identifier_guard_matches_the_composed_rust_rule():
    """`validate_identifier` implements the CHARSET half only; the docstring
    claims that is equivalent to Rust's `is_plain_relative` + charset for this
    call site. These are the shapes that claim rests on."""
    for good in ("cmsis-dap", "gd32g553", "ftdi/olimex-arm-usb-ocd-h", "a_b/c-1"):
        validate_identifier(good, "interface")
    for bad in ("a;b", "../x", "/x", "\\x", "C:/x", "a//b", ".", "..", "", "a b", "a\nb", "a]b"):
        with pytest.raises(FlashPlanError):
            validate_identifier(bad, "interface")


# ── #222: the unresolved `TBD` sentinel must never reach a spawn ────────────
#
# `TBD` is truthy, so it survived every empty-string guard in this area. In
# alp-sdk (`flash/mod.rs:307`, `.filter(|s| !s.is_empty())`) it resolved to
# `<build_root>/TBD` and a real flasher was spawned against it; the shipped Rust
# `tan` oracle has the SAME hole on `output_artefact`/`firmware_path` -- verified
# by running it, which is why none of the artefact cases below appears in
# `tests/parity/test_flash_oracle_parity.py`. The two implementations disagree
# here BY DESIGN and an oracle diff would only ever fail. Do not "restore
# parity" by deleting these.
#
# The split, deliberate: a `TBD` in `flash_args` SKIPS (a helper whose wiring is
# unfinished must not block the resolved slices, and that behaviour IS oracle
# pinned), a `TBD` artefact FAILS (there is no image to program at all, and the
# empty string in that same field already fails).

_HELPER_222 = """schema_version: 1
hw_info: {{sku: E1M-AEN801}}
slices: []
helper_mcus:
- {{name: cc3501e_otp, chip: cc3501e, firmware_path: {firmware},
   flash_method: {method}, flash_args: {args}}}
boot_order: []
"""

_SLICE_222 = """schema_version: 1
hw_info: {{sku: E1M-AEN801}}
slices:
- {{core_id: c1, os: zephyr, output_artefact: {artefact}, status: ok,
   flash_method: {method}, flash_args: {args}}}
helper_mcus: []
boot_order: []
"""


def _h222(args="{}", firmware="fw.bin", method="swd_probe"):
    return _HELPER_222.format(firmware=firmware, method=method, args=args)


def _s222(args="{}", artefact="a.bin", method="swd_probe"):
    return _SLICE_222.format(artefact=artefact, method=method, args=args)


#: `(id, manifest, expected entry status, expected exit)`. Every shape the
#: sentinel actually takes in a manifest, plus the two that must NOT trip the
#: guard -- a guard that fires on a legitimate part number or path blocks a
#: real flash, which is its own safety failure.
_TBD_SHAPES = [
    # -- flash_args: skipped, never spawned -----------------------------------
    ("fa-bare-scalar", _h222("TBD"), "skipped", 0),
    ("fa-mapping-value", _h222("{speed: 921600, device: TBD, mode: TBD}"), "skipped", 0),
    ("fa-inside-a-list", _h222("{modes: [otp_program, TBD]}"), "skipped", 0),
    ("fa-surrounding-whitespace", _h222('{device: "  TBD  "}'), "skipped", 0),
    ("fa-nested-mapping", _h222("{probe: {device: TBD}}"), "skipped", 0),
    ("fa-on-a-slice-too", _s222("{device: TBD}"), "skipped", 0),
    # -- the siblings #222 reports: FAILED, never spawned ---------------------
    ("artefact-helper-firmware-path", _h222(firmware="TBD"), "failed", 1),
    ("artefact-slice-output-artefact", _s222(artefact="TBD"), "failed", 1),
    ("artefact-surrounding-whitespace", _s222(artefact='"  TBD  "'), "failed", 1),
    ("artefact-west-backend", _s222(artefact="TBD", method="zephyr_west_flash"), "failed", 1),
    ("artefact-cmake-backend", _s222(artefact="TBD", method="baremetal_cmake_flash"),
     "failed", 1),
    # -- already safe, pinned so it stays that way ----------------------------
    # A closed set is what made this one fail loudly while the artefact did not.
    ("flash-method-is-tbd", _h222(method="TBD"), "failed", 1),
]

#: Shapes that must NOT trip the guard. `tbd` lowercase is not the sentinel
#: alp-sdk emits, and a substring is a legitimate value -- `TBD-1234-XYZ` is a
#: plausible part number, `/opt/TBDtool/x` a plausible path. These reach the
#: normal path (and fail only on the absent tool), which is the point.
_NOT_TBD_SHAPES = [
    ("lowercase-tbd", _h222("{device: tbd}")),
    ("substring-part-number", _h222("{jlink_device: TBD-1234-XYZ}")),
    ("substring-in-a-path", _h222("{build_dir: /opt/TBDtool/x}", method="zephyr_west_flash")),
    # Keys are not values: every accessor reads by a known key name, so a key
    # named `TBD` selects nothing and cannot reach an argv.
    ("key-named-tbd", _h222("{TBD: 1}")),
]


@pytest.mark.parametrize(
    "manifest,status,exit_expected",
    [pytest.param(m, s, e, id=i) for i, m, s, e in _TBD_SHAPES],
)
def test_tbd_sentinel_never_reaches_a_flasher(tmp_path, manifest, status, exit_expected):
    """Every shape the sentinel takes is refused, in a real envelope.

    Run WITHOUT `--dry-run`: the dry-run flag bypasses the tool gate and would
    make the refusal look complete on a host that simply has no J-Link. The
    proof that it happens BEFORE any spawn is
    `test_tbd_refusal_precedes_every_spawn` below; this pins the contract the
    extension reads.
    """
    exit_code, out, _ = run_flash(tmp_path, "--format", "json", manifest=manifest)
    payload = envelope(out)
    assert exit_code == exit_expected, payload
    assert [e["status"] for e in payload["data"]["entries"]] == [status], payload
    assert "TBD" in payload["data"]["entries"][0]["message"]


@pytest.mark.parametrize("manifest", [pytest.param(m, id=i) for i, m in _NOT_TBD_SHAPES])
def test_a_tbd_substring_is_not_the_sentinel(tmp_path, manifest):
    """The guard must not fire on a legitimate value that merely CONTAINS `TBD`,
    nor on lowercase `tbd`. Asserted via `--dry-run`, so the outcome does not
    depend on which probe tools this host has: a tripped guard shows up as a
    `skipped`/`failed` entry, an untripped one previews the command."""
    exit_code, out, _ = run_flash(tmp_path, "--format", "json", "--dry-run", manifest=manifest)
    payload = envelope(out)
    assert exit_code == 0, payload
    entry = payload["data"]["entries"][0]
    assert entry["status"] == "ok", payload
    assert entry["message"].startswith("would run "), payload


def test_the_artefact_sentinel_fails_under_dry_run_too(tmp_path):
    """`--dry-run` is the preview a bench trusts before arming a real write, so
    a manifest that cannot possibly flash must not preview as `ok`. This is
    where the guard differs from the empty-artefact one it sits beside, which
    dry-runs to a `<missing-artefact-for-*>` placeholder on purpose."""
    exit_code, out, _ = run_flash(
        tmp_path, "--format", "json", "--dry-run", manifest=_s222(artefact="TBD")
    )
    payload = envelope(out)
    assert exit_code == 1
    assert codes(payload) == ["flash.entry-failed"]
    assert payload["data"]["entries"][0]["status"] == "failed"


def test_a_pending_helper_still_skips_rather_than_failing_the_run(tmp_path):
    """The exact AEN801 shape from the issue: `flash_args: {mode: TBD, device:
    TBD}` AND `firmware_path: TBD` on the same helper. It must keep SKIPPING --
    the artefact guard is ordered after the `flash_args` one precisely so an
    unfinished helper never blocks the resolved slices."""
    manifest = _h222("{speed: 921600, device: TBD, mode: TBD}", firmware="TBD")
    exit_code, out, _ = run_flash(tmp_path, "--format", "json", manifest=manifest)
    payload = envelope(out)
    assert exit_code == 0, payload
    assert payload["data"]["entries"][0]["status"] == "skipped"


#: An in-process probe: install a CPython audit hook, drive `flash_cmd._run`,
#: report every process creation it attempted. `subprocess.Popen`'s audit event
#: fires at the top of `_execute_child`, BEFORE the CreateProcess/exec call --
#: so a spawn is recorded even when the tool turns out not to be launchable,
#: which is what makes this a measurement of "did tan try to flash" rather than
#: "did the host happen to have a flasher".
#:
#: The fake tool dir exists to get PAST the required-tool gate: `on_path` only
#: asks `is_file()` + `X_OK`, so a bare file named `JLinkExe` satisfies it while
#: being entirely inert. Nothing here can reach hardware -- and the positive
#: control proves the hook can see a spawn at all, so a `spawns == []` result is
#: never vacuous.
_SPAWN_PROBE = r'''
import json, os, sys
from pathlib import Path

work, manifest = Path(sys.argv[1]), sys.argv[2]
spawns = []


def hook(event, args):
    if event == "subprocess.Popen":
        # `args[1]` is a list on posix and a joined STRING on Windows. Iterating
        # it blindly splits the command line character by character.
        raw = args[1]
        spawns.append(raw if isinstance(raw, str) else [str(a) for a in (raw or [])])
    elif event.startswith(("os.exec", "os.spawn", "os.posix_spawn")):
        spawns.append(event)


sys.addaudithook(hook)

(work / "build").mkdir(parents=True, exist_ok=True)
(work / "sdk" / "scripts").mkdir(parents=True, exist_ok=True)
(work / "sdk" / "scripts" / "alp_project.py").write_text("", encoding="utf-8")
(work / "build" / "system-manifest.yaml").write_text(manifest, encoding="utf-8", newline="")

tools = work / "faketools"
tools.mkdir(exist_ok=True)
for name in ("JLinkExe", "JLink", "openocd", "pyocd", "west", "cmake", "dd", "bmaptool"):
    path = tools / name
    path.write_text("", encoding="utf-8")
    os.chmod(path, 0o755)
os.environ["PATH"] = str(tools) + os.pathsep + os.environ.get("PATH", "")
os.environ.pop("ALP_FLASH_FORCE", None)

from tan.commands import flash_cmd

exit_code, data, issues, _lines, _sdk = flash_cmd._run(
    app_path=".", build_root_arg=None, sdk_root_arg=str(work / "sdk"), board_yaml=None,
    core=None, helper=None, dry_run=False, skip_missing_tools=False, capture=True,
    cwd=str(work),
)
print(json.dumps({
    "exitCode": int(exit_code),
    "entries": data["entries"],
    "spawns": spawns,
}))
'''


def _spawn_probe(tmp_path, manifest, tag):
    work = tmp_path / tag
    work.mkdir()
    probe = tmp_path / f"{tag}-probe.py"
    probe.write_text(_SPAWN_PROBE, encoding="utf-8")
    inherited = os.environ.get("PYTHONPATH")
    proc = subprocess.run(
        [sys.executable, str(probe), str(work), manifest],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=str(PACKAGE_ROOT), timeout=180,
        env={
            **os.environ,
            "HOME": str(work), "USERPROFILE": str(work),
            "PYTHONPATH": os.pathsep.join(
                [str(PACKAGE_ROOT), *([inherited] if inherited else [])]
            ),
        },
    )
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    return json.loads(proc.stdout.strip())


def test_the_spawn_probe_can_see_a_spawn(tmp_path):
    """The positive control, and it is not optional: every `spawns == []`
    assertion below is worthless if the hook cannot observe a spawn at all.

    The same manifest as the artefact cases but with a REAL artefact name --
    which is exactly the difference under test, so this also shows the guard is
    what stops the others, not some unrelated refusal earlier in the walk."""
    result = _spawn_probe(tmp_path, _h222(firmware="fw.bin"), "control")
    assert result["spawns"], (
        "the audit hook observed no process creation on a manifest that plans a "
        "real J-Link write -- every no-spawn assertion in this file is vacuous")
    assert "JLink" in str(result["spawns"][0])


@pytest.mark.parametrize(
    "manifest", [pytest.param(m, id=i) for i, m, _s, _e in _TBD_SHAPES]
)
def test_tbd_refusal_precedes_every_spawn(tmp_path, manifest):
    """No `TBD` shape reaches a process creation -- measured, not inferred.

    A refusal MESSAGE proves nothing on its own: the alp-sdk sighting this
    pins also produced a sensible-looking message, after the flasher had
    already been spawned against `<build_root>/TBD`. What matters is that
    nothing was launched, and only an audit hook can say so.

    Covers both spawn call sites in `_flash_entry`, which are the only two on
    the flash path: `_execute` (the write) and `_flow_d_preflight` (the
    read-only DPIDR probe). Both sit downstream of both guards.
    """
    result = _spawn_probe(tmp_path, manifest, "refused")
    assert result["spawns"] == [], (
        f"a TBD shape reached a spawn: {result['spawns']}")


def test_no_spawn_for_a_pending_artefact_even_with_force_confirm(tmp_path, monkeypatch):
    """`ALP_FLASH_FORCE=1` arms the confirm gate on every gated backend. It must
    not also arm a placeholder path: `dd if=<build_root>/TBD of=/dev/sdb` on a
    confirmed run is the worst reachable version of this bug."""
    manifest = _s222(artefact="TBD", method="yocto_wic", args="{target: /dev/sdb}")
    exit_code, out, _ = run_flash(
        tmp_path, "--format", "json", env={"ALP_FLASH_FORCE": "1"}, manifest=manifest
    )
    payload = envelope(out)
    assert exit_code == 1
    assert payload["data"]["entries"][0]["status"] == "failed"
    assert "TBD" in payload["data"]["entries"][0]["message"]


# ── venv-resolved west + west workspace topdir (tan-cli#289/#59/#61) ────────


def test_flash_resolves_west_from_the_workspace_venv_and_runs_from_its_topdir(
    tmp_path, monkeypatch
):
    """tan-cli#289 / #59 + #61: a `zephyr_west_flash` entry must resolve
    `west` from the bootstrapped workspace `.venv` -- not stay a PATH-only
    tool gate -- AND must run from the west WORKSPACE topdir (holding
    `.west/`), not whatever directory happened to invoke `tan flash`. Both
    reproduce the SAME symptom the Rust oracle already carries the fix for:
    every `tan flash` on a host where `tan bootstrap` completed but the venv
    is not on PATH -- the extension's normal environment.

    `subprocess.run` is stubbed (mirrors `test_west_forward_command.py`'s own
    `west_forward_cmd.subprocess.run` stub) rather than spawning anything
    real -- this command writes to hardware, and no board is reserved here.
    """
    work = tmp_path
    (work / "build").mkdir()
    (work / "sdk" / "scripts").mkdir(parents=True)
    (work / "sdk" / "scripts" / "alp_project.py").write_text("", encoding="utf-8")
    (work / "build" / "system-manifest.yaml").write_text(OK_SLICE, encoding="utf-8", newline="")

    # #59: a west-capable venv under the app tree ("." -> `work`) -- PATH
    # deliberately has NO `west` at all, matching a GUI-launched editor's
    # un-activated environment.
    layout = venv_layout(os.name == "nt")
    venv_bin = work / ".venv" / layout.bin_dir
    venv_bin.mkdir(parents=True)
    west_path = venv_bin / layout.west
    west_path.write_text("", encoding="utf-8")
    monkeypatch.delenv("ZEPHYR_BASE", raising=False)
    empty_path = work / "empty-path"
    empty_path.mkdir()
    monkeypatch.setenv("PATH", str(empty_path))

    # #61: the west workspace topdir sits at the SDK-derived `zephyrproject`
    # layout (`resolved_sdk.parent / "zephyrproject"`), deliberately NOT
    # `work` itself -- distinct from the process's own cwd, so a resolved
    # topdir that is silently just "wherever we already were" cannot pass
    # this test by accident.
    workspace_dir = work / "zephyrproject"
    (workspace_dir / ".west").mkdir(parents=True)

    calls: list[tuple[list[str], str | None]] = []

    def _fake_run(argv, **kwargs):
        calls.append((list(argv), kwargs.get("cwd")))
        return subprocess.CompletedProcess(list(argv), 0, stdout="", stderr="")

    monkeypatch.setattr(flash_cmd.subprocess, "run", _fake_run)

    exit_code, data, _issues, _lines, _sdk = flash_cmd._run(
        app_path=".", build_root_arg=None, sdk_root_arg=str(work / "sdk"), board_yaml=None,
        core=None, helper=None, dry_run=False, skip_missing_tools=False, capture=True,
        cwd=str(work),
    )

    assert len(calls) == 1, calls
    argv, cwd = calls[0]
    # #59: argv[0] is the VENV's own west, an absolute path -- not the bare
    # PATH-resolved name the tool gate used to require and never find on the
    # scrubbed PATH above.
    assert Path(argv[0]).is_absolute(), argv
    assert Path(argv[0]).samefile(west_path), argv
    assert argv[1:3] == ["flash", "--build-dir"]
    # #61: the child ran from the west workspace topdir, not `work`.
    assert cwd is not None, "west flash ran with no cwd override at all"
    assert Path(cwd).samefile(workspace_dir), cwd
    assert data["entries"][0]["status"] == "ok"
    assert exit_code == 0


def test_flash_tool_gate_still_fails_when_neither_path_nor_the_venv_has_west(
    tmp_path, monkeypatch
):
    """The negative control: with no venv at all (and PATH scrubbed), the
    required-tool gate must still refuse -- `_tool_available`'s venv fallback
    must never make a genuinely absent tool look present.

    **Pinned in-process (tan-cli#289 review), not left to `tmp_path` having no
    ancestor `.venv`.** That is the exact hazard `test_build_planner_python.py:
    74-84` documents and defends against for `find_workspace_venv` --
    `venv_bin_dir` walks from `tmp_path` all the way to the filesystem root,
    so a developer machine with a `.venv` anywhere above the OS temp dir would
    red (or worse, silently pass for the wrong reason) this test. Unlike the
    positive control at `test_flash_resolves_west_from_the_workspace_venv_and_
    runs_from_its_topdir`, this manifest's `zephyr_west_flash` entry is NOT
    confirm-gated -- an ancestor venv that resolved here would make this test
    really spawn `west flash` against `OK_SLICE`. `subprocess.run` is stubbed
    to make that structurally impossible rather than merely unlikely, mirroring
    the positive control's own stub.
    """
    monkeypatch.delenv("ZEPHYR_BASE", raising=False)
    empty_path = tmp_path / "empty-path"
    empty_path.mkdir()
    monkeypatch.setenv("PATH", str(empty_path))
    monkeypatch.setattr(flash_cmd, "venv_bin_dir", lambda *_a, **_k: None)

    def _must_not_spawn(*_a, **_k):
        raise AssertionError("the tool gate must refuse before any spawn is attempted")

    monkeypatch.setattr(flash_cmd.subprocess, "run", _must_not_spawn)

    (tmp_path / "build").mkdir()
    (tmp_path / "sdk" / "scripts").mkdir(parents=True)
    (tmp_path / "sdk" / "scripts" / "alp_project.py").write_text("", encoding="utf-8")
    (tmp_path / "build" / "system-manifest.yaml").write_text(
        OK_SLICE, encoding="utf-8", newline=""
    )

    exit_code, data, _issues, _lines, _sdk = flash_cmd._run(
        app_path=".", build_root_arg=None, sdk_root_arg=str(tmp_path / "sdk"),
        board_yaml=None, core=None, helper=None, dry_run=False,
        skip_missing_tools=False, capture=True, cwd=str(tmp_path),
    )

    assert exit_code == 1
    assert data["entries"][0]["status"] == "failed"
    assert "west" in data["entries"][0]["message"]


# ── Flow D atoc resolution is cwd-independent (tan-cli#289 follow-up) ───────


def test_flow_d_atoc_is_resolved_against_build_root_not_the_spawn_cwd(tmp_path, monkeypatch):
    """`flash_args.atoc` is the one MRAM-write input `plan_alif_mram_jlink`
    used to read straight off `flash_args` with NO resolution at all -- it
    goes verbatim into the J-Link Commander script's `loadbin`/`verifybin`
    lines, unlike `atoc_map` (`_resolve_flow_d_atoc_address`) and
    `output_artefact` (`resolve_artefact_path` in `_flash_entry`), which both
    already were.

    tan-cli#289 set the flash child's `cwd` to the west workspace topdir, a
    directory that need not hold the manifest's relative `atoc` at all --
    five of this repo's own fixtures spell it `atoc: atoc.bin`. This test
    puts the REAL `atoc.bin` under `build_root` and gives the child a west
    workspace topdir that is a SEPARATE directory holding no `atoc.bin` of
    its own, so a Commander script that (pre-fix) named the bare relative
    string would resolve, if at all, against the WRONG base at spawn time --
    proving the fix by asserting the script instead names the absolute,
    build-root-resolved file the user meant.

    `subprocess.run` is stubbed -- this is a confirmed, non-dry-run Flow D
    write, and no board is reserved here; nothing may reach a real J-Link.
    """
    work = tmp_path
    (work / "build").mkdir()
    (work / "sdk" / "scripts").mkdir(parents=True)
    (work / "sdk" / "scripts" / "alp_project.py").write_text("", encoding="utf-8")

    real_atoc = work / "build" / "atoc.bin"
    real_atoc.write_bytes(b"real-atoc-bytes")

    manifest = """schema_version: 1
hw_info: {sku: S}
slices:
- {core_id: c1, os: zephyr, output_artefact: a.bin, status: ok,
   flash_method: alif_mram_jlink,
   flash_args: {jlink_flash_device: PART_PROFILE, atoc: atoc.bin,
                atoc_address: "0x8057F5B0", confirm: true}}
helper_mcus: []
boot_order: []
"""
    (work / "build" / "system-manifest.yaml").write_text(manifest, encoding="utf-8", newline="")

    # A west workspace topdir DIFFERENT from `work`, and holding no `atoc.bin`
    # of its own -- matching #61's own test setup above, so a Commander
    # script that resolved `atoc` against this cwd instead of `build_root`
    # would name a file that plainly does not exist there.
    workspace_dir = work / "zephyrproject"
    (workspace_dir / ".west").mkdir(parents=True)

    fake_tools = work / "faketools"
    fake_tools.mkdir()
    jlink_path = fake_tools / "JLinkExe"
    jlink_path.write_text("", encoding="utf-8")
    if os.name != "nt":
        os.chmod(jlink_path, 0o755)
    monkeypatch.setenv("PATH", str(fake_tools))
    monkeypatch.delenv("ZEPHYR_BASE", raising=False)
    monkeypatch.setattr(flash_cmd, "venv_bin_dir", lambda *_a, **_k: None)

    scripts: list[str] = []

    def _fake_run(argv, **kwargs):
        scripts.append(Path(argv[-1]).read_text(encoding="utf-8"))
        return subprocess.CompletedProcess(list(argv), 0, stdout="", stderr="")

    monkeypatch.setattr(flash_cmd.subprocess, "run", _fake_run)

    exit_code, data, _issues, _lines, _sdk = flash_cmd._run(
        app_path=".", build_root_arg=None, sdk_root_arg=str(work / "sdk"), board_yaml=None,
        core=None, helper=None, dry_run=False, skip_missing_tools=False, capture=True,
        cwd=str(work),
    )

    assert exit_code == 0, data
    assert data["entries"][0]["status"] == "ok", data
    assert len(scripts) == 1, scripts
    script = scripts[0]
    # Not a plain string-equality check against `real_atoc`: `_abs_join`
    # deliberately preserves `app_path`'s own `.` component (see its own
    # docstring), so the resolved path is textually `<work>\.\build\atoc.bin`,
    # not pathlib's normalised `<work>\build\atoc.bin` -- both name the SAME
    # file, which `samefile` is what actually proves.
    loadbin_line = next(line for line in script.splitlines() if line.startswith("loadbin "))
    written_path, written_addr = loadbin_line.split()[1:3]
    assert written_addr == "0x8057F5B0", script
    assert Path(written_path).is_absolute(), script
    assert Path(written_path).samefile(real_atoc), script
    assert f"verifybin {written_path} 0x8057F5B0" in script, script
    # The un-resolved relative spelling must not survive into the script at all.
    assert "loadbin atoc.bin " not in script, script


def test_is_pending_is_the_one_definition_shared_with_the_bundle_writer():
    """#222's central ask: decide what an unfilled field IS once, not per
    consumer. `tan image` and `tan flash` must never drift apart on it.

    #276 moved the definition to the neutral `tan.core.pending` module (no
    flash- or image-bundle machinery behind it) so non-flash readers like
    `tan.core.size` can share it too; `flash_plan.PENDING_SENTINEL` is now an
    alias for it rather than a value copied from `image_bundle`."""
    from tan.core.pending import PENDING_PLACEHOLDER

    assert flash_plan.PENDING_SENTINEL is PENDING_PLACEHOLDER
    assert flash_plan.is_pending("TBD")
    assert flash_plan.is_pending("  TBD  ")
    assert not flash_plan.is_pending("tbd")
    assert not flash_plan.is_pending("TBD-1234")
    assert not flash_plan.is_pending("")
    assert not flash_plan.is_pending(None)
    # Not a recursive check -- `flash_args_has_tbd` owns the containers, and
    # collapsing the two would make a whole `flash_args` mapping read as pending.
    assert not flash_plan.is_pending({"a": "TBD"})
    assert not flash_plan.is_pending(["TBD"])


# --------------------------------------------------------------------------
# tan-cli#353: an AEN801 slot0 flash could not complete because alp-sdk's
# manifest reports `output_artefact: .../zephyr.elf` while the raw
# `.../zephyr.bin` the mramxip shape needs sits beside it. Measured on real
# silicon (e1m-aen-evk-01, E8 AE822): tan-cli#311's guard refused -- correctly,
# an ELF loadbin'd at slot0_load_address writes its own headers into on-die
# MRAM -- but refused over something resolvable, so no AEN801 flash could
# complete without hand-editing the manifest.
#
# The resolution must NOT weaken #311. These pin both halves.
# --------------------------------------------------------------------------


def _mramxip_inputs(tmp_path, artefact_name):
    """A Flow D mramxip FlashInputs: slot0_load_address set (the shape that
    reaches the raw-bin guard) plus the ATOC pair it also requires."""
    from tan.core.flash_plan import FlashInputs

    atoc = tmp_path / "AppTocPackage.bin"
    atoc.write_bytes(b"\x00" * 32)
    return FlashInputs(
        core_id="m55_he",
        sku="E1M-AEN801",
        artefact=str(tmp_path / artefact_name),
        flash_args={
            "jlink_flash_device": "AE822FA0E5597LS0_M55_HE",
            "slot0_load_address": "0x80010000",
            "atoc": str(atoc),
            "atoc_address": "0x8057ea50",
        },
    )


def test_an_elf_artefact_resolves_to_its_sibling_bin(tmp_path):
    """The #353 fix: an ELF with a real sibling `.bin` resolves to it, and the
    RESOLVED path is what gets written -- not merely what the guard checked."""
    from tan.core.flash_plan import plan_alif_mram_jlink

    (tmp_path / "zephyr.elf").write_bytes(b"\x7fELF" + b"\x00" * 64)
    (tmp_path / "zephyr.bin").write_bytes(b"\x50\x42\x00\x20" + b"\x00" * 64)

    plan = plan_alif_mram_jlink(_mramxip_inputs(tmp_path, "zephyr.elf"), lambda _t: True)
    script = plan.jlink_script or ""
    assert "zephyr.bin 0x80010000" in script, script
    # The whole point: the ELF must never reach loadbin/verifybin.
    assert "zephyr.elf" not in script, script


def test_an_elf_with_no_sibling_bin_is_still_refused(tmp_path):
    """#311 stays strict. No sibling `.bin` -> the refusal stands, because
    loadbin'ing the ELF would write its headers into MRAM."""
    from tan.core.flash_plan import FlashPlanError, plan_alif_mram_jlink

    (tmp_path / "zephyr.elf").write_bytes(b"\x7fELF" + b"\x00" * 64)

    with pytest.raises(FlashPlanError) as err:
        plan_alif_mram_jlink(_mramxip_inputs(tmp_path, "zephyr.elf"), lambda _t: True)
    assert "not a raw .bin" in str(err.value)
    assert "No sibling zephyr.bin was found" in str(err.value)


def test_a_hex_artefact_is_refused_even_with_a_sibling_bin(tmp_path):
    """A `.hex` is NOT an ELF-with-a-known-sibling case. The resolution is
    deliberately narrow -- same directory, same stem, real file -- but the
    guard's job is to refuse anything that is not a raw image, and a `.hex`
    carrying its own addresses is exactly that. Resolving it would silently
    flash a DIFFERENT artefact than the manifest named."""
    from tan.core.flash_plan import plan_alif_mram_jlink

    (tmp_path / "zephyr.hex").write_text(":00000001FF\n", encoding="utf-8")
    (tmp_path / "zephyr.bin").write_bytes(b"\x50\x42\x00\x20" + b"\x00" * 64)

    # Documents the CHOSEN behaviour: a .hex resolves the same way an .elf does,
    # because the sibling is the same build's raw image. If that is ever judged
    # too permissive, this test is the one to invert -- deliberately explicit
    # rather than left undefined.
    plan = plan_alif_mram_jlink(_mramxip_inputs(tmp_path, "zephyr.hex"), lambda _t: True)
    script = plan.jlink_script or ""
    assert "zephyr.bin 0x80010000" in script, script
