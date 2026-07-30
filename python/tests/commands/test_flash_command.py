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

from tan.core import flash_plan
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


def test_a_confirmed_flow_d_entry_fails_contained_when_no_tool_resolves(tmp_path):
    """A confirmed Flow D entry must fail as an ENVELOPE, not kill the process.

    **`PATH` is scrubbed deliberately, and that is a hardware-safety requirement,
    not tidiness.** This manifest carries `confirm: true` and the test runs
    WITHOUT `--dry-run`, so with a J-Link on the inherited `PATH` tan would
    genuinely spawn Commander with `si SWD / connect / loadbin ... 0x80010000 /
    loadbin ... 0x8057F5B0 / RSetType 2 / r / g` -- i.e. connect to whatever board
    is attached, attempt an MRAM write, and pin-reset it, from `pytest`. The
    maintainer's bench has a probe wired to a live AEN EVK. No test in this file
    may ever inherit `PATH` on a confirmed, non-dry-run flash path.

    The original version of this test also asserted a false premise: it claimed
    `mkstemp` raises when `TMPDIR`/`TEMP`/`TMP` point at a nonexistent directory,
    but `tempfile.gettempdir()` falls back past all three, so it passed for an
    unrelated reason on every host -- the tool gate without a probe, a real spawn
    with one. The hostile temp vars are kept (they must not break anything), but
    the assertion now rests on the tool gate, which is what actually fires.
    """
    missing = str(tmp_path / "no" / "such" / "dir")
    manifest = f"""schema_version: 1
hw_info: {{sku: S}}
slices:
- {{core_id: c1, os: zephyr, output_artefact: a.bin, status: ok,
   flash_method: alif_mram_jlink,
   flash_args: {{jlink_flash_device: PART_PROFILE, mram_address: "0x80010000",
                atoc: atoc.bin, atoc_address: "0x8057F5B0", confirm: true}}}}
helper_mcus: []
boot_order: []
"""
    exit_code, out, _ = run_flash(
        tmp_path,
        "--format",
        "json",
        # PATH="" so NOTHING can resolve: no JLinkExe, no west, no cmake. This is
        # the guard that stops a confirmed, non-dry-run Flow D entry reaching real
        # hardware from the test suite. Do not remove it.
        env={"TMPDIR": missing, "TEMP": missing, "TMP": missing, "PATH": ""},
        manifest=manifest,
    )
    payload = envelope(out)
    assert exit_code == 1
    assert codes(payload) == ["flash.entry-failed"]
    # And prove no burn was even attempted: the entry died at the TOOL GATE,
    # before any Commander script was written or spawned.
    message = payload["data"]["entries"][0]["message"]
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
    "mram_address": "0x80010000",
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
    carry (I-26 / ADR-0017)."""
    armed = FlashTarget(SLICE, "m55_he", "zephyr_west_flash", FLOW_D_ARGS)
    assert select_flash_method(armed) == "alif_mram_jlink"

    # Neither key -> Flow A, i.e. `west flash` on the board.cmake default runner.
    plain = FlashTarget(SLICE, "m55_he", "zephyr_west_flash", {})
    assert select_flash_method(plain) == "zephyr_west_flash"

    # HALF-armed is Flow A too: a device profile with no MRAM address leaves
    # nothing to `loadbin` to, and guessing the address is the one thing this
    # path must never do.
    for key in ("jlink_flash_device", "mram_address"):
        half = dict(FLOW_D_ARGS)
        del half[key]
        assert select_flash_method(FlashTarget(SLICE, "m", "zephyr_west_flash", half)) == (
            "zephyr_west_flash"
        ), key

    # An explicitly-named method is never re-routed -- the preference applies to
    # the DEFAULT recipe only.
    named = FlashTarget(SLICE, "m", "swd_probe", FLOW_D_ARGS)
    assert select_flash_method(named) == "swd_probe"
    assert flow_d_available(FLOW_D_ARGS)
    assert not flow_d_available("TBD")


def test_an_unquoted_mram_address_still_arms_flow_d():
    """PyYAML parses an unquoted `mram_address: 0x80010000` as an INTEGER. Arming
    must key on PRESENCE, not on "is a non-empty string": a string-shaped check
    would call this entry unarmed and silently route the burn to Flow A over the
    SE-UART. Transport is never decided by a quoting detail."""
    numeric = {**FLOW_D_ARGS, "mram_address": 0x80010000}
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
    broken = {**FLOW_D_ARGS, "mram_address": ["not", "an", "address"]}
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
        ("mram_address", "mram_address is required"),
        ("atoc", "flash_args.atoc"),
        ("atoc_address", "flash_args.atoc"),
    ],
)
def test_flow_d_refuses_rather_than_guessing_any_identifier(missing, expected):
    """Every Flow D identifier is a hardware fact that arrives in `flash_args`.
    None has a default: a guessed MRAM address is a write to the wrong place on a
    part whose Secure Enclave then boots whatever is there."""
    with pytest.raises(FlashPlanError) as raised:
        plan_alif_mram_jlink(flow_d_inputs(**{missing: None}), lambda t: True)
    assert expected in str(raised.value)


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
        plan_alif_mram_jlink(flow_d_inputs(mram_address=bad), lambda t: True)


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
    """No `expect_dpidr` (or no attach-profile `jlink_device`) means NO preflight:
    tan cannot supply either value, and a wrong expected ID would refuse every
    good board."""
    assert flash_plan.flow_d_preflight_script(flow_d_inputs()) is None
    assert (
        flash_plan.flow_d_preflight_script(flow_d_inputs(expect_dpidr="0x4C013477")) is None
    )
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
   flash_args: {jlink_flash_device: PART_PROFILE, mram_address: "0x80010000",
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
    `Path.parent` would re-render it with the platform separator."""
    assert zephyr_build_dir("C:/a\\build/c1-zephyr/zephyr/zephyr.elf") == (
        "C:/a\\build/c1-zephyr" if os.name == "nt" else "C:/a\\build/c1-zephyr/zephyr"
    )
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
