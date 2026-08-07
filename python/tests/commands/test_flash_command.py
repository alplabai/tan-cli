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
import stat
import subprocess
import sys
import types
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


# ── tan-cli#487 defect 7: an entry-level skip must not be diagnosed as
# "nothing matched the requested filters" ───────────────────────────────────
#
# `_flash_entry` can legitimately skip a target it DID dispatch to (an
# unresolved `TBD` flash_arg, no flash_method, or a missing tool under
# `--skip-missing-tools`) and correctly reports the real reason on that one
# entry (`entries[0].status == "skipped"`, `rc == -1`, a specific message).
# The aggregate "did anything flash" check used to know only about the
# PLANNER's own skip buckets (`plan.refused`/`plan.refused_skipped`), which an
# entry-level skip never populates -- so on a run with NO `--core`/`--helper`
# filter at all it fell through to `flash.nothing-matched`, contradicting
# that very message on a manifest that genuinely matched a target.
#
# The shipped Rust oracle carries the SAME misdiagnosis (verified by running
# it: `flash.nothing-matched`, `exitCode 0`, on all five shapes below), which
# is why none of these five cases appear in
# `tests/parity/test_flash_oracle_parity.py` any more -- the maintainer
# authorised a deliberate divergence from that frozen answer for exactly this
# shape (see that file's own divergence note beside `helper-no-flash-method`).
# Do NOT "restore parity" by moving these back.
#
# `ok`/exit code are DELIBERATELY unchanged here -- still SUCCESS, the same
# pinned contract `test_tbd_sentinel_never_reaches_a_flasher` above asserts.
# Only the issue code and message were wrong, and only those are fixed.

_HELPER_487 = """schema_version: 1
hw_info: {{sku: E1M-AEN801}}
slices: []
helper_mcus:
- {{name: h1, chip: cc3501e, firmware_path: fw.bin, flash_method: {method},
   flash_args: {args}}}
boot_order: []
"""


def _h487(method, args="{}"):
    return _HELPER_487.format(method=method, args=args)


@pytest.mark.parametrize(
    "manifest,expected_message_fragment",
    [
        pytest.param(_h487('""'), "has no flash_method", id="helper-no-flash-method"),
        pytest.param(
            """schema_version: 1
hw_info: {sku: S}
slices: []
helper_mcus:
- {name: cc3501e_otp, chip: cc3501e, firmware_path: fw.bin,
   update_channel: alp_ota_spi_otp}
boot_order: []
""",
            "is Alp-OTA-updated",
            id="helper-update-channel-is-not-a-flash-target",
        ),
        pytest.param(
            _h487("swd_probe", "{mode: TBD, device: TBD}"),
            "unresolved 'TBD' flash_arg",
            id="flash-args-tbd-mapping",
        ),
        pytest.param(
            _h487("swd_probe", "TBD"),
            "unresolved 'TBD' flash_arg",
            id="flash-args-tbd-bare-string",
        ),
    ],
)
def test_an_entry_level_skip_is_diagnosed_as_such_not_as_a_filter_miss(
    tmp_path, manifest, expected_message_fragment
):
    """Four of the five #487 defect-7 shapes: a manifest with exactly ONE
    helper, no `--core`/`--helper` filter, that helper skips inside
    `_flash_entry`. Must report `flash.entries-skipped`, never
    `flash.nothing-matched` -- and the per-entry `entries[0]` must still carry
    the real, specific reason, unchanged by this fix."""
    exit_code, out, _ = run_flash(tmp_path, "--format", "json", manifest=manifest)
    payload = envelope(out)
    assert exit_code == 0, payload
    assert payload["ok"] is True, payload
    assert len(payload["data"]["entries"]) == 1, payload
    entry = payload["data"]["entries"][0]
    assert entry["status"] == "skipped", payload
    assert entry["rc"] == -1, payload
    assert expected_message_fragment in entry["message"], payload
    assert codes(payload) == ["flash.entries-skipped"], payload
    issue = payload["issues"][0]
    assert issue["severity"] == "warning", payload
    assert issue["message"] == (
        "flash: every matched target was skipped before dispatch; nothing "
        "was flashed. See entries[] for why each one was skipped."
    ), payload
    # The old, wrong diagnosis must not reappear alongside the fix.
    assert "nothing matched the requested filters" not in issue["message"], payload


def test_a_missing_tool_skip_under_the_flag_is_also_an_entry_skip(tmp_path):
    """The fifth #487 defect-7 shape: `--skip-missing-tools` turns the
    required-tool gate's refusal into an entry-level `rc=-1` skip
    (`missing-tool-skips-with-flag` in the retired parity case), which hit the
    identical misdiagnosis -- and must be fixed the identical way."""
    empty_bin = tmp_path / "no-tools"
    empty_bin.mkdir()
    exit_code, out, _ = run_flash(
        tmp_path,
        "--format", "json", "--skip-missing-tools",
        env={"PATH": str(empty_bin)},
        manifest=OK_SLICE,
    )
    payload = envelope(out)
    assert exit_code == 0, payload
    assert payload["ok"] is True, payload
    assert len(payload["data"]["entries"]) == 1, payload
    entry = payload["data"]["entries"][0]
    assert entry["status"] == "skipped", payload
    assert entry["rc"] == -1, payload
    assert "none found" in entry["message"], payload
    assert "(skipped via --skip-missing-tools)" in entry["message"], payload
    assert codes(payload) == ["flash.entries-skipped"], payload
    assert payload["issues"][0]["severity"] == "warning", payload
    assert "nothing matched the requested filters" not in payload["issues"][0]["message"], payload


def test_an_entry_level_skip_beside_a_real_refusal_still_fails_the_run(tmp_path):
    """A genuine refusal (`status: failed`) alongside an entry-level skip must
    still fail the run -- the defect-7 fix must not swallow a real failure
    just because it sits beside an unrelated skip. `plan.refused` is
    non-empty here, so neither the new `flash.entries-skipped` branch nor the
    old `flash.nothing-matched` one fires -- only the pre-existing
    `flash.slice-not-built` refusal, unchanged."""
    manifest = """schema_version: 1
hw_info: {sku: S}
slices:
- {core_id: c1, os: zephyr, output_artefact: a.elf, status: failed,
   flash_method: swd_probe}
helper_mcus:
- {name: h1, chip: x, firmware_path: f.bin}
boot_order: []
"""
    exit_code, out, _ = run_flash(tmp_path, "--format", "json", "--dry-run", manifest=manifest)
    payload = envelope(out)
    assert exit_code == 1, payload
    assert payload["ok"] is False, payload
    assert codes(payload) == ["flash.slice-not-built"], payload


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


def test_sdk_root_ladder_broken_pin_discloses_on_the_not_found_refusal(tmp_path):
    """tan-cli#464 review: the fourth `_run` refusal (`flash.sdk-root-not-
    found`, the LADDER finding nothing at all -- no `--sdk-root`, unlike the
    test above) used to return bare `['flash.sdk-root-not-found']` even when
    the workspace's OWN `.alp/sdk-path` named a broken pin, where the
    identical state already made `size`/`image` report
    `sdk.project-pin-unresolved` alongside their own not-found code. No
    `~/.alp/sdk-default` and nothing on the positional walk, so this is the
    one ladder outcome where `foreign_global_default_for` cannot also fire
    (only the `globalDefault` tier ever sets it) -- `flash.manifest-not-
    found`/`flash.manifest-invalid` cover that half elsewhere."""
    workspace = tmp_path / "ws"
    (workspace / ".alp").mkdir(parents=True)
    gone = tmp_path / "gone-sdk"
    (workspace / ".alp" / "sdk-path").write_text(
        json.dumps({"sdkPath": str(gone), "updatedAt": "1970-01-01T00:00:00Z"}),
        encoding="utf-8",
        newline="",
    )
    proc = subprocess.run(
        [sys.executable, "-m", "tan", "flash", "--format", "json", "."],
        cwd=workspace,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={**os.environ, "PYTHONPATH": str(PACKAGE_ROOT), "HOME": str(tmp_path),
             "USERPROFILE": str(tmp_path)},
        timeout=180,
    )
    payload = envelope(proc.stdout)
    assert proc.returncode == 1
    assert codes(payload) == ["sdk.project-pin-unresolved", "flash.sdk-root-not-found"]
    assert payload["issues"][0]["severity"] == "warning"
    assert str(gone) in payload["issues"][0]["message"]
    assert "sdk" not in payload


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


# ── `_execute_message` text-mode gate + the TimeoutExpired partial output ──
# (tan-cli#487, defect 4)


def test_execute_message_surfaces_a_tan_authored_diagnosis_in_text_mode():
    """`_execute_message` used to gate on `outcome.captured`, true ONLY in
    JSON mode -- so in text mode (the default human invocation of the one
    command that writes hardware) a TAN-AUTHORED diagnosis (`_spawn`'s
    timeout/spawn-fail messages, `_spawn_pipeline`'s `_timed_out_stderr`,
    `_spawn_jlink`'s could-not-write message -- all four populate `outcome.
    stderr` regardless of mode) was composed and then discarded, reported as
    a bare `flash command failed`. Fails against the pre-fix source
    (measured: the message is the bare fallback, `outcome.stderr` unused)."""
    from tan.commands.flash_cmd import _Outcome, _execute_message

    outcome = _Outcome(
        success=False, stderr="timed out after 900s and was killed",
        returncode=-1, captured=False,
    )
    message = _execute_message(outcome, "yocto_wic", "a55")
    assert message == "yocto_wic[a55]: timed out after 900s and was killed"


def test_execute_message_text_mode_ordinary_stream_failure_is_unaffected():
    """The case this fix must NOT change: an ordinary text-mode failure whose
    child already streamed straight to the console leaves BOTH `stdout`/
    `stderr` empty on the `_Outcome` -- there is nothing tan itself has to
    add, so this still falls back to the generic sentence exactly as
    before."""
    from tan.commands.flash_cmd import _Outcome, _execute_message

    outcome = _Outcome(success=False, returncode=1, captured=False)
    assert _execute_message(outcome, "yocto_wic", "a55") == "yocto_wic[a55]: flash command failed"


def test_spawn_timeout_folds_in_output_the_child_printed_before_the_kill():
    """The wrapped-console half of defect 4: `subprocess.TimeoutExpired`
    carries `.stdout`/`.stderr` from a `capture_output=True` spawn (both the
    JSON-mode branch and the wrapped-console text-mode branch of `_spawn`
    use one), which used to be discarded outright on a kill -- only the
    generic sentence survived. A multi-GB write killed mid-transfer left the
    operator no reason to suspect a truncated image on the card. Fails
    against the pre-fix source (measured: `outcome.stderr` is exactly the
    generic sentence, with `before-the-kill` absent)."""
    from tan.commands.flash_cmd import _spawn

    outcome = _spawn(
        [
            sys.executable, "-c",
            "import sys, time; print('before-the-kill'); sys.stdout.flush(); time.sleep(30)",
        ],
        capture=True, timeout=1.0,
    )
    assert outcome.success is False
    assert "before-the-kill" in outcome.stderr
    assert "timed out after 1s and was killed" in outcome.stderr
    # The sentence stays LAST so `_capture_tail`'s trailing-four-lines
    # truncation keeps it, not an earlier line.
    assert outcome.stderr.strip().splitlines()[-1] == "timed out after 1s and was killed"


# ── _spawn_pipeline (tan-cli#401): the decompressor|dd pipeline verdict ─────
#
# `plan_yocto_wic` builds exactly `<decompressor> | dd of=<target> ...` for a
# compressed `.wic.gz`/`.wic.xz` Yocto artefact -- these stand in for the real
# `gunzip`/`dd` pair with a Python one-liner on each side, so the cases run on
# every platform this suite runs on.


def _stub(code: str) -> list[str]:
    return [sys.executable, "-c", code]


def test_spawn_pipeline_decompressor_failure_is_the_verdict_not_dds_stats():
    """The test that would have caught tan-cli#401. `dd` reads whatever the
    left process wrote before it died and reports its OWN rc 0 plus
    throughput stats -- exactly the truncated-`.wic.gz` shape Hakan measured.
    The pipeline's overall verdict must be the decompressor's failure, its
    stderr, and its non-zero rc -- not dd's."""
    left = _stub(
        "import sys; sys.stdout.write('partial'); sys.stdout.flush(); "
        "sys.stderr.write('gunzip: image.wic.gz: unexpected end of file\\n'); "
        "sys.stderr.write('gunzip: uncompress failed\\n'); sys.exit(1)"
    )
    right = _stub(
        "import sys; data = sys.stdin.buffer.read(); "
        "sys.stderr.write(f'{len(data)} bytes transferred\\n'); sys.exit(0)"
    )
    outcome = flash_cmd._spawn_pipeline(left, right, capture=True, timeout=10.0)
    assert outcome.success is False
    assert outcome.returncode == 1
    assert "gunzip: image.wic.gz: unexpected end of file" in outcome.stderr

    message = flash_cmd._execute_message(outcome, "yocto_wic", "a55")
    assert "unexpected end of file" in message


def test_spawn_pipeline_dds_own_failure_is_still_reported():
    """The right process (`dd`) failing with the decompressor clean must still
    surface, with ITS rc -- the left succeeded so there is no non-zero of its
    own to prefer."""
    left = _stub("import sys; sys.stdout.write('all-good'); sys.exit(0)")
    right = _stub(
        "import sys; sys.stdin.buffer.read(); "
        "sys.stderr.write('dd: write error\\n'); sys.exit(7)"
    )
    outcome = flash_cmd._spawn_pipeline(left, right, capture=True, timeout=10.0)
    assert outcome.success is False
    assert outcome.returncode == 7
    assert "dd: write error" in outcome.stderr


def test_spawn_pipeline_both_stages_succeeding_is_a_clean_outcome():
    left = _stub("import sys; sys.stdout.write('data'); sys.exit(0)")
    right = _stub("import sys; sys.stdin.buffer.read(); sys.exit(0)")
    outcome = flash_cmd._spawn_pipeline(left, right, capture=True, timeout=10.0)
    assert outcome.success is True
    assert outcome.returncode == 0


def test_spawn_pipeline_text_mode_ordinary_failure_leaves_the_outcome_and_message_empty():
    """tan-cli#487 review finding 5: `_execute_message`'s own docstring
    (`test_execute_message_text_mode_ordinary_stream_failure_is_unaffected`)
    claims the ordinary text-mode failure leaves BOTH `outcome.stdout`/
    `.stderr` empty -- true for the single-process `_spawn` path that test
    covers, but NOT for `_spawn_pipeline` before this fix: neither half's
    stderr is ever piped in text mode (`capture=False`), yet `_half_lines`
    still emitted a body-less `"<program> exited rc=<n>:"` header for the
    failing half, so `outcome.stderr` was non-empty and `_execute_message`
    reported a dangling-colon header with no body instead of the generic
    `flash command failed` sentence. Fails against the pre-fix source
    (measured: `outcome.stderr == '<interpreter> exited rc=1:\\n'`, message
    `'yocto_wic[a55]: <interpreter> exited rc=1:'`)."""
    left = _stub("import sys; sys.stdout.write('data'); sys.exit(0)")
    right = _stub("import sys; sys.stdin.buffer.read(); sys.exit(1)")
    outcome = flash_cmd._spawn_pipeline(left, right, capture=False, timeout=10.0)
    assert outcome.success is False
    assert outcome.returncode == 1
    assert outcome.stderr == ""
    assert outcome.stdout == ""
    message = flash_cmd._execute_message(outcome, "yocto_wic", "a55")
    assert message == "yocto_wic[a55]: flash command failed"


def test_spawn_pipeline_drains_more_than_one_pipe_buffer_without_hanging():
    """The anti-hang property this background thread exists for (unchanged by
    tan-cli#401): a decompressor that writes more to stderr than the OS pipe
    buffer holds must not deadlock the pipeline -- its `write()` blocking
    forever because nobody is reading, `dd` then blocked on a stdin that never
    reaches EOF, and `wait()` never returning."""
    left = _stub(
        "import sys; sys.stderr.write('e' * (1 << 20)); "
        "sys.stdout.write('data'); sys.exit(0)"
    )
    right = _stub("import sys; sys.stdin.buffer.read(); sys.exit(0)")
    outcome = flash_cmd._spawn_pipeline(left, right, capture=True, timeout=15.0)
    assert outcome.success is True


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


# ── swd_probe Commander script quoting (tan-cli#369, test gap per #373) ────


def test_jlink_commander_script_quotes_a_spaced_loadbin_path():
    """tan-cli#369 fixed `commander_path` (unquoted whitespace silently
    truncates SEGGER's line, e.g. `C:\\Program Files\\...` -> `C:\\Program`)
    but shipped with no test exercising `jlink_commander_script` -- the
    `swd_probe` generator that is the only real caller -- against a spaced
    path at all (tan-cli#373). A raw `.bin` takes the `loadbin` line."""
    script = flash_plan.jlink_commander_script(
        "C:\\Program Files\\alp\\build\\zephyr.bin", "0x08000000", True
    )
    assert 'loadbin "C:\\Program Files\\alp\\build\\zephyr.bin", 0x08000000' in script


def test_jlink_commander_script_quotes_a_spaced_loadfile_path():
    """The non-`.bin` (`loadfile`) branch must quote a spaced path too -- the
    same SEGGER whitespace-split hazard applies to it, and `commander_path`
    is shared by both `jlink_commander_script` branches."""
    script = flash_plan.jlink_commander_script(
        "C:\\Program Files\\alp\\build\\zephyr.elf", "0x08000000", False
    )
    assert 'loadfile "C:\\Program Files\\alp\\build\\zephyr.elf"' in script


def test_jlink_commander_script_leaves_an_unspaced_path_unquoted():
    """The common case -- every already-measured oracle/bench script path --
    must render byte-identical to before tan-cli#369's quoting fix."""
    script = flash_plan.jlink_commander_script("/build/zephyr.bin", "0x08000000", True)
    assert "loadbin /build/zephyr.bin, 0x08000000" in script
    assert '"' not in script


# ── artefact/atoc path + jlink_serial guards (tan-cli#486) ─────────────────


def test_jlink_commander_script_refuses_a_newline_embedded_in_the_artefact_path():
    """`commander_path`'s conditional quoting (tan-cli#369) stops SEGGER's
    whitespace tokeniser splitting a spaced path into two tokens -- it does
    NOT stop an embedded newline from ending the quoted string's own
    Commander LINE and starting a new, attacker-chosen one. Reproduced on the
    real generator every `swd_probe` write calls."""
    with pytest.raises(FlashPlanError):
        flash_plan.jlink_commander_script("/build/zephyr.bin\nerase", "0x08000000", True)


def test_jlink_commander_script_still_quotes_a_spaced_path_after_the_new_guard():
    """tan-cli#486's new control-character guard must not turn a real spaced
    Windows-style path into a refusal -- only a control character is
    rejected; whitespace is left to `commander_path`'s existing conditional
    quoting, unchanged from before this fix."""
    script = flash_plan.jlink_commander_script(
        "C:\\Program Files\\alp\\build\\zephyr.bin", "0x08000000", True
    )
    assert 'loadbin "C:\\Program Files\\alp\\build\\zephyr.bin", 0x08000000' in script


def test_jlink_commander_script_refuses_an_embedded_double_quote():
    """tan-cli#486 REVIEW, defect 3: a `"` inside an artefact defeats
    `commander_path`'s own conditional quoting FROM THE INSIDE. Measured:
    `/b/a" halt "z.bin` (a space AND a `"`) renders
    `loadbin "/b/a" halt "z.bin", 0x8000` -- the embedded quote closes the
    wrapper after `/b/a` and `halt` reads back as a bare Commander token
    mid-line, exactly what `validate_commander_path`'s own docstring claims
    quoting prevents ("controls tokenisation within a line"). `"` is a
    reserved character in a Windows filename and vanishingly rare on POSIX,
    so rejecting it costs a real path nothing."""
    with pytest.raises(FlashPlanError):
        flash_plan.jlink_commander_script('/b/a" halt "z.bin', "0x8000", True)


def test_validate_commander_path_refuses_a_bare_double_quote_with_no_whitespace():
    """The charset check itself, isolated from `commander_path`'s conditional
    quoting: a `"` alone (no space, so `commander_path` would never even
    consider quoting it) must still refuse -- the character is dangerous the
    moment ANY sibling value on the same line legitimately needs quoting,
    not only when this value itself has whitespace."""
    with pytest.raises(FlashPlanError):
        flash_plan.validate_commander_path('/build/a"b.bin', "the flash artefact path")


def test_swd_probe_jlink_artefact_path_is_charset_guarded_against_a_newline():
    """tan-cli#486, reproduced on `plan_swd_probe`'s real J-Link write path
    (not just the pure `jlink_commander_script` generator): a hostile
    `output_artefact`/`firmware_path` must refuse before a Commander script
    is ever handed to `JLinkExe`."""
    inp = FlashInputs(artefact="/build/zephyr.bin\nerase", flash_args={}, core_id="cm7", sku="S")
    with pytest.raises(FlashPlanError):
        flash_plan.plan_swd_probe(inp, lambda name: name == "JLinkExe")


def test_swd_probe_openocd_artefact_path_is_guarded_against_tcl_substitution():
    """tan-cli#486: OpenOCD 0.12's Jim Tcl has `exec`; an unescaped `[...]` in
    the artefact triggers COMMAND SUBSTITUTION while the `-c program ...`
    word is evaluated -- arbitrary host command execution as the user running
    `tan flash`, reachable with no probe attached and even if the flash
    itself would fail. `swd_probe` has no confirm gate, so this must refuse
    at plan time."""
    inp = FlashInputs(
        artefact="/build/[exec calc].bin",
        flash_args={"interface": "cmsis-dap", "target": "stm32h7x", "base": "0x00000000"},
        core_id="cm7",
        sku="S",
    )
    with pytest.raises(FlashPlanError):
        flash_plan.plan_swd_probe(inp, lambda name: name == "openocd")


def test_swd_probe_openocd_artefact_path_guard_accepts_a_space():
    """tan-cli#486 REVIEW: the metacharacter/control-character guard alone
    does not turn a spaced artefact path into a refusal -- but closing the
    injection hole is not the same as producing a CORRECT plan. Before this
    fix OpenOCD's `-c program` word left a spaced artefact UNQUOTED, and Jim
    Tcl splits an unquoted word on whitespace: `program /build/my app.elf
    verify reset exit` parses as SEVEN words
    (`program`/`/build/my`/`app.elf`/`verify`/`reset`/`exit`), so `program`
    receives `/build/my` and treats `app.elf` as a bogus extra argument. The
    fix braces the word whenever it carries whitespace (or a backslash) --
    `openocd_program_word` -- which makes the whole path ONE Jim Tcl word
    with no substitution performed on its contents. Asserted on the actual
    built plan, not just "the guard did not reject it"."""
    flash_plan.validate_openocd_word("/build/my app.elf", "artefact")
    inp = FlashInputs(
        artefact="/build/my app.elf",
        flash_args={"interface": "cmsis-dap", "target": "stm32h7x", "base": "0x00000000"},
        core_id="cm7",
        sku="S",
    )
    plan = flash_plan.plan_swd_probe(inp, lambda name: name == "openocd")
    assert plan.argv[-1] == "program {/build/my app.elf} verify reset exit"


def test_swd_probe_openocd_windows_path_with_space_is_braced_not_mangled():
    """tan-cli#486 REVIEW, defect 1's headline example. Unbraced, Jim Tcl's
    own word-splitting AND backslash substitution both fire on
    `C:\\Program Files\\alp\\build\\zephyr.elf`: it splits into `program` /
    `C:Program` / `Files\\x07lp\\x08uildzephyr.elf` (`\\a`->BEL, `\\b`->BS) /
    `verify` / `reset` / `exit`, so `program` receives the filename
    `C:Program` and treats the mangled remainder as a bogus offset argument
    -- verified against `tclsh`. Bracing (triggered here by either the space
    or the backslash) makes Jim Tcl perform NO substitution on the material
    between the braces and treat it as one atomic word, so the artefact
    reaches `program` byte-identical to the manifest value."""
    inp = FlashInputs(
        artefact="C:\\Program Files\\alp\\build\\zephyr.elf",
        flash_args={"interface": "cmsis-dap", "target": "stm32h7x", "base": "0x00000000"},
        core_id="cm7",
        sku="S",
    )
    plan = flash_plan.plan_swd_probe(inp, lambda name: name == "openocd")
    assert plan.argv[-1] == (
        "program {C:\\Program Files\\alp\\build\\zephyr.elf} verify reset exit"
    )


def test_swd_probe_openocd_space_only_hostile_artefact_cannot_inject_keywords():
    """tan-cli#486 REVIEW, defect 1's second measured example: a hostile
    artefact carrying no Tcl metacharacter at all -- just spaces -- used to
    inject extra Tcl keywords once interpolated unquoted:
    `/build/evil.elf verify exit 0x20000000` rendered `program /build/evil.elf
    verify exit 0x20000000 verify reset exit`, an extra `verify`/`exit`/
    address the manifest author never wrote. Bracing on whitespace closes
    this: the whole hostile string becomes ONE word, i.e. ONE (bogus but
    inert) filename argument to `program`, not five extra Tcl words."""
    inp = FlashInputs(
        artefact="/build/evil.elf verify exit 0x20000000",
        flash_args={"interface": "cmsis-dap", "target": "stm32h7x", "base": "0x00000000"},
        core_id="cm7",
        sku="S",
    )
    plan = flash_plan.plan_swd_probe(inp, lambda name: name == "openocd")
    assert plan.argv[-1] == (
        "program {/build/evil.elf verify exit 0x20000000} verify reset exit"
    )


#: `openocd_program_word` now braces EVERY artefact, plain or not
#: (tan-cli#511) -- see that function's own docstring for why the earlier
#: whitespace/backslash-conditional version never actually preserved the
#: parity it claimed to. The two frozen `tests/parity/test_flash_oracle_
#: parity.py` cases that used to pin the plain (unbraced) rendering
#: (`multi-segment-interface-is-allowed`, `openocd-forced-bin-appends-base`)
#: moved OUT of that suite's byte-diff `CASES` table for exactly this
#: reason; `test_openocd_program_word_diverges_from_the_oracle_by_exactly_
#: the_brace` below is their bounded replacement.


# ── swd_probe device/target reporting (tan-cli#402) ─────────────────────────


def _swd_inputs(**flash_args):
    return FlashInputs(artefact="/build/zephyr.bin", flash_args=flash_args, core_id="cm7", sku="S")


def test_swd_probe_jlink_message_names_the_resolved_device_not_gd32g553():
    """tan-cli#402, the test that would have caught it. The J-Link success
    message used to hardcode `GD32G553` regardless of what `jlink_device`
    resolved to -- a customer flashing an STM32H747XI_M7 was told GD32G553
    was flashed."""
    inp = _swd_inputs(jlink_device="STM32H747XI_M7", base="0x00000000")
    plan = flash_plan.plan_swd_probe(inp, lambda name: name == "JLinkExe")
    assert "STM32H747XI_M7" in plan.ok_message
    assert "GD32G553" not in plan.ok_message
    assert "STM32H747XI_M7" in plan.argv


def test_swd_probe_jlink_refuses_a_target_only_som_instead_of_defaulting_to_gd32():
    """tan-cli#402: `flash_args.target` used to be read only on the
    openocd/pyocd branch, AFTER the J-Link branch's own early `return` -- a
    SoM that declared `interface`/`target` but no `jlink_device` silently got
    the compiled-in `GD32G553MEY7TR` `-device`, with no diagnostic. A J-Link
    device name and an OpenOCD/pyOCD target name are different namespaces, so
    this refuses rather than guessing one from the other."""
    inp = _swd_inputs(interface="cmsis-dap", target="stm32h7x")
    with pytest.raises(FlashPlanError) as raised:
        flash_plan.plan_swd_probe(inp, lambda name: name == "JLinkExe")
    assert "jlink_device" in str(raised.value)
    assert "target" in str(raised.value)


def test_swd_probe_still_defaults_to_gd32_when_neither_device_nor_target_is_set():
    """The unaffected case (tan-cli#402): a SoM naming NEITHER key -- the
    shipped state for boards that flash the GD32 bridge itself -- still gets
    the inherited `_DEFAULT_JLINK_DEVICE`, unchanged."""
    plan = flash_plan.plan_swd_probe(_swd_inputs(), lambda name: name == "JLinkExe")
    assert "GD32G553MEY7TR" in plan.ok_message
    assert "GD32G553MEY7TR" in plan.argv


def test_swd_probe_openocd_message_names_the_resolved_target_not_gd32g553():
    """tan-cli#402: the openocd/pyocd success message was worse than the
    J-Link one -- it named `GD32G553` and echoed no device at all."""
    inp = _swd_inputs(interface="cmsis-dap", target="stm32h7x", base="0x00000000")
    plan = flash_plan.plan_swd_probe(inp, lambda name: name == "openocd")
    assert "stm32h7x" in plan.ok_message
    assert "GD32G553" not in plan.ok_message


# ── swd_probe J-Link probe-serial selection (tan-cli#513) ───────────────────


def test_swd_probe_jlink_emits_selectemubysn_when_serial_is_set():
    """tan-cli#513, the headline defect: `flash_args.jlink_serial` was
    accepted (it passes the #486 charset guard the same as every other
    backend) and then silently DROPPED on `swd_probe`'s J-Link arm -- neither
    `SelectEmuBySN` in the Commander script nor `-SelectEmuBySN` in argv, so a
    bench with more than one J-Link either fails to connect (JLinkExe cannot
    pick) or, worse, could reach the wrong board on a shared/cloned serial.
    Fails against the pre-fix source (measured: no `SelectEmuBySN` anywhere in
    `plan.jlink_script` when `jlink_serial` was set)."""
    inp = _swd_inputs(jlink_serial="603000869")
    plan = flash_plan.plan_swd_probe(inp, lambda name: name == "JLinkExe")
    assert plan.jlink_script.startswith("SelectEmuBySN 603000869\n")


def test_swd_probe_jlink_no_serial_emits_no_selectemubysn():
    """No default serial, same reasoning as Flow D
    (`test_flow_d_probe_serial_is_optional_and_has_no_default`): a bench-wide
    serial can be SHARED by two probes that differ only by USB path, so a
    silent default can select the wrong board. `swd_probe` has no confirm
    gate to hide behind either -- this must stay opt-in."""
    plan = flash_plan.plan_swd_probe(_swd_inputs(), lambda name: name == "JLinkExe")
    assert "SelectEmuBySN" not in plan.jlink_script


def test_swd_probe_jlink_probe_serial_accepts_a_bare_numeric_value():
    """tan-cli#486's own fix for Flow D, extended to `swd_probe`:
    `jlink_serial: 603000869` (unquoted -- the canonical SEGGER spelling) is a
    bare YAML integer. `fa_str_checked`, not the tolerant `fa_str`, is what
    round-trips it into its decimal string form instead of silently treating
    it as absent."""
    plan = flash_plan.plan_swd_probe(_swd_inputs(jlink_serial=603000869), lambda n: n == "JLinkExe")
    assert plan.jlink_script.startswith("SelectEmuBySN 603000869\n")


@pytest.mark.parametrize("bad", ["a;b", "../x", "/x", "C:/x", "a b", "dev\nice"])
def test_swd_probe_jlink_probe_serial_is_charset_guarded(bad):
    """tan-cli#513: before this fix `swd_probe` validated `jlink_serial` NOT
    AT ALL -- the field was dead code on this backend, so a hostile value
    (e.g. an embedded newline forming an extra Commander command) sailed
    through with `ok:true`. Fails against the pre-fix source (measured: no
    refusal for any of these). Same guard-class as Flow D's own
    `test_flow_d_probe_serial_is_charset_guarded`."""
    inp = _swd_inputs(jlink_serial=bad)
    with pytest.raises(FlashPlanError):
        flash_plan.plan_swd_probe(inp, lambda name: name == "JLinkExe")


def test_swd_probe_jlink_probe_serial_refusal_names_jlink_not_openocd():
    """Mirrors Flow D's own `test_flow_d_probe_serial_refusal_names_jlink_
    not_openocd` (tan-cli#486 REVIEW, defect 4): `jlink_serial` reaches a
    J-Link Commander `SelectEmuBySN` line, never an OpenOCD Tcl script, so the
    refusal must say so -- `swd_probe` shares `_JLINK_SERIAL_DESTINATION`
    with Flow D rather than hand-rolling a second wording."""
    inp = _swd_inputs(jlink_serial="dev\nice")
    with pytest.raises(FlashPlanError) as raised:
        flash_plan.plan_swd_probe(inp, lambda name: name == "JLinkExe")
    message = str(raised.value)
    assert "J-Link Commander" in message
    assert "SelectEmuBySN" in message
    assert "OpenOCD" not in message


def test_swd_probe_jlink_probe_serial_empty_string_still_opts_out():
    """`jlink_serial: ""` opts OUT of `SelectEmuBySN` entirely, matching Flow
    D's own `test_flow_d_probe_serial_empty_string_still_opts_out`."""
    plan = flash_plan.plan_swd_probe(_swd_inputs(jlink_serial=""), lambda n: n == "JLinkExe")
    assert "SelectEmuBySN" not in plan.jlink_script


def test_swd_probe_jlink_probe_serial_precedes_the_reset_halt_lines():
    """`SelectEmuBySN` must be the FIRST line of the Commander script -- a
    probe has to be selected before `r`/`halt` can address it. Also proves
    `jlink_commander_script`'s new `serial` parameter composes correctly with
    the rest of the script (reset/halt, load, optional reset-and-go,
    quit-close) rather than merely being accepted and ignored a second time."""
    script = flash_plan.jlink_commander_script(
        "/build/zephyr.bin", "0x08000000", True, serial="603000869"
    )
    lines = script.splitlines()
    assert lines[0] == "SelectEmuBySN 603000869"
    assert lines[1] == "r"
    assert lines[2] == "halt"


def test_jlink_commander_script_serial_defaults_to_absent():
    """The new `serial` parameter is optional and defaults to `None` -- every
    existing call site/test in this file that constructs a Commander script
    with the original 3-positional-argument shape must render byte-identical
    to before this fix."""
    script = flash_plan.jlink_commander_script("/build/zephyr.bin", "0x08000000", True)
    assert not script.startswith("SelectEmuBySN")
    assert script.splitlines()[0] == "r"


@pytest.mark.parametrize("serial", ["801012345", "000440123456", "J-Link-OB_1", "12345678-9"])
def test_swd_probe_jlink_probe_serial_real_segger_spellings_still_render(serial):
    """tan-cli#513 REVIEW: the same four real SEGGER serial spellings Flow D's
    own `test_flow_d_probe_serial_real_segger_spellings_still_render` pins --
    `801012345` (bare numeric), `000440123456` (leading-zero string), the
    on-board-probe name `J-Link-OB_1`, and the hyphenated `12345678-9`. The
    swd_probe/Flow D asymmetry is exactly the one-path drift that let #486's
    guard go missing on this arm in the first place; pinning the identical
    fixture set on both keeps that from silently happening again."""
    plan = flash_plan.plan_swd_probe(
        _swd_inputs(jlink_serial=serial), lambda n: n == "JLinkExe"
    )
    assert plan.jlink_script.startswith(f"SelectEmuBySN {serial}\n")
    assert "-SelectEmuBySN" in plan.argv
    assert plan.argv[plan.argv.index("-SelectEmuBySN") + 1] == serial


# ── swd_probe J-Link probe-serial: argv selector (tan-cli#513 REVIEW) ───────


def test_swd_probe_jlink_argv_includes_selectemubysn_when_serial_is_set():
    """tan-cli#513 REVIEW, finding 1: this arm's argv carries `-AutoConnect 1`
    (Flow D's argv has none), so JLinkExe may start connecting to whatever
    probe autoconnect finds BEFORE the Commander script's own leading
    `SelectEmuBySN` line is ever read -- the script line alone does not
    provably precede the connect on every DLL version. `-SelectEmuBySN` must
    also be passed in argv, which selects at parse time. Fails against the
    original #513 fix alone (measured: `-SelectEmuBySN` absent from
    `plan.argv` even though the script line was already present)."""
    plan = flash_plan.plan_swd_probe(
        _swd_inputs(jlink_serial="603000869"), lambda n: n == "JLinkExe"
    )
    assert "-SelectEmuBySN" in plan.argv
    assert plan.argv[plan.argv.index("-SelectEmuBySN") + 1] == "603000869"


def test_swd_probe_jlink_argv_omits_selectemubysn_when_no_serial():
    """The unaffected case: no `jlink_serial` means no `-SelectEmuBySN` word
    at all, matching the script's own "absent -> no line" default."""
    plan = flash_plan.plan_swd_probe(_swd_inputs(), lambda n: n == "JLinkExe")
    assert "-SelectEmuBySN" not in plan.argv


def test_swd_probe_jlink_argv_with_serial_is_byte_identical_apart_from_the_insertion():
    """tan-cli#513 REVIEW nit: the emit tests must not stop at `startswith` --
    prove `-SelectEmuBySN <serial>` is a pure INSERTION, disturbing no other
    argv word (`-device`, `-if SWD`, `-speed`, `-AutoConnect 1`,
    `-ExitOnError 1`, `-NoGui 1`, `-CommanderScript` all keep their exact
    values and relative order)."""
    without = flash_plan.plan_swd_probe(
        _swd_inputs(jlink_device="STM32H747XI_M7"), lambda n: n == "JLinkExe"
    )
    with_serial = flash_plan.plan_swd_probe(
        _swd_inputs(jlink_device="STM32H747XI_M7", jlink_serial="603000869"),
        lambda n: n == "JLinkExe",
    )
    assert with_serial.argv[:7] == without.argv[:7]
    assert with_serial.argv[7:9] == ("-SelectEmuBySN", "603000869")
    assert with_serial.argv[9:] == without.argv[7:]


def test_swd_probe_jlink_script_with_serial_is_the_no_serial_script_plus_one_line():
    """Companion to the argv-identical check just above, for the Commander
    script half: `SelectEmuBySN {serial}` is a pure PREPEND -- every line
    after it is byte-identical to the no-serial script."""
    without = flash_plan.plan_swd_probe(
        _swd_inputs(jlink_device="STM32H747XI_M7"), lambda n: n == "JLinkExe"
    )
    with_serial = flash_plan.plan_swd_probe(
        _swd_inputs(jlink_device="STM32H747XI_M7", jlink_serial="603000869"),
        lambda n: n == "JLinkExe",
    )
    with_lines = with_serial.jlink_script.splitlines()
    assert with_lines[0] == "SelectEmuBySN 603000869"
    assert with_lines[1:] == without.jlink_script.splitlines()


# ── swd_probe J-Link probe-serial: openocd/pyocd cannot honour it
#    (tan-cli#513 REVIEW, finding 2) ─────────────────────────────────────────


def test_swd_probe_probe_serial_is_refused_on_a_real_openocd_only_host():
    """tan-cli#513 REVIEW, finding 2's headline repro: `flash_args: {
    use_openocd: true, interface: cmsis-dap, target: gd32g553, jlink_serial:
    "a;b"}` used to keep `jlink` `None` (the J-Link arm was never taken), so
    the resolve+validate that lived only inside `if jlink is not None:` never
    ran, and the plan was built and reported `ok` with the hostile value
    silently dropped. Fails against the original #513 fix alone (measured: no
    refusal on this exact manifest with only `openocd` on PATH)."""
    inp = _swd_inputs(use_openocd=True, interface="cmsis-dap", target="gd32g553", jlink_serial="a;b")
    with pytest.raises(FlashPlanError):
        flash_plan.plan_swd_probe(inp, lambda n: n == "openocd")


def test_swd_probe_probe_serial_charset_guard_is_not_host_dependent():
    """tan-cli#513 REVIEW, finding 2's host-dependency half: the SAME hostile
    `jlink_serial` must be refused whether `--dry-run` forces the J-Link arm
    (which validated it even under the original #513 fix) OR a real run on an
    openocd-only host takes the fallback arm -- not one and not the other.
    Fails against the original #513 fix alone (measured: the `--dry-run` case
    already refused; the real-openocd-only case did not)."""
    args = {"interface": "cmsis-dap", "target": "gd32g553", "jlink_serial": "dev\nice"}
    dry = FlashInputs(artefact="/build/zephyr.bin", flash_args=args, core_id="cm7", sku="S", dry_run=True)
    real = FlashInputs(artefact="/build/zephyr.bin", flash_args=args, core_id="cm7", sku="S", dry_run=False)
    with pytest.raises(FlashPlanError):
        flash_plan.plan_swd_probe(dry, lambda n: n == "openocd")
    with pytest.raises(FlashPlanError):
        flash_plan.plan_swd_probe(real, lambda n: n == "openocd")


def test_swd_probe_probe_serial_is_refused_on_the_openocd_arm_even_when_valid():
    """A charset-CLEAN `jlink_serial` must still be refused on the
    openocd/pyocd arm -- the problem is not the value's shape, it is that
    `openocd`'s argv (`-f interface/....cfg -f target/....cfg -c program
    ...`) has no probe-serial word to put it in at all. Accepting a
    well-formed-but-unusable value and reporting `ok:true` would be the exact
    accept-and-ignore shape #513 fixed for the J-Link arm."""
    inp = _swd_inputs(interface="cmsis-dap", target="gd32g553", jlink_serial="603000869")
    with pytest.raises(FlashPlanError) as raised:
        flash_plan.plan_swd_probe(inp, lambda n: n == "openocd")
    message = str(raised.value)
    assert "jlink_serial" in message
    assert "openocd" in message.lower()


def test_swd_probe_probe_serial_is_refused_on_the_pyocd_arm_too():
    """The same refusal on the sibling tool: `pyocd`'s argv (`pyocd flash
    --target ... [--base-address ...] <artefact>`) has no probe-serial word
    either."""
    inp = _swd_inputs(use_pyocd=True, interface="cmsis-dap", target="gd32g553", jlink_serial="603000869")
    with pytest.raises(FlashPlanError):
        flash_plan.plan_swd_probe(inp, lambda n: n == "pyocd")


def test_swd_probe_probe_serial_refusal_precedes_the_interface_target_check():
    """The `jlink_serial`-on-the-wrong-arm refusal fires even when
    `interface`/`target` are ALSO missing -- the diagnosis names the field
    this arm cannot use, not a coincidentally-also-missing one."""
    inp = _swd_inputs(jlink_serial="603000869")
    with pytest.raises(FlashPlanError) as raised:
        flash_plan.plan_swd_probe(inp, lambda n: n == "openocd")
    assert "jlink_serial" in str(raised.value)


def test_swd_probe_no_serial_still_reaches_the_openocd_arm_unaffected():
    """The unaffected case: a manifest naming no `jlink_serial` at all must
    keep working on the openocd/pyocd arm exactly as before this review
    round -- this refusal is scoped to the field being SET, not to taking the
    fallback arm in general."""
    inp = _swd_inputs(interface="cmsis-dap", target="gd32g553")
    plan = flash_plan.plan_swd_probe(inp, lambda n: n == "openocd")
    assert plan.argv[0] == "openocd"


# ── swd_probe success message asserts no address for ELF/HEX (tan-cli#487) ──


def test_swd_probe_jlink_bin_message_still_names_the_base_address():
    """The `.bin` case (unchanged): a raw binary DOES carry a load offset, and
    `jlink_commander_script`'s `loadfile` line actually gets it -- the message
    must keep asserting it."""
    inp = _swd_inputs(jlink_device="STM32H747XI_M7", base="0x08000000")
    plan = flash_plan.plan_swd_probe(inp, lambda name: name == "JLinkExe")
    assert "@ 0x08000000" in plan.ok_message


def test_swd_probe_jlink_elf_message_omits_an_address_the_tool_never_received():
    """tan-cli#487, defect 6: on an ELF/HEX write `jlink_commander_script`'s
    `loadfile` line deliberately withholds `base` (a load OFFSET, meaningful
    only for a raw `.bin`), but the success message used to interpolate
    `@ {base}` unconditionally -- asserting the compiled-in `_DEFAULT_BASE`
    (or the manifest's `base`) on every ELF/HEX write regardless. Fails
    against the pre-fix source (measured: `@ 0x08000000` was present)."""
    inp = FlashInputs(
        artefact="/build/zephyr.elf", flash_args={"jlink_device": "STM32H747XI_M7"},
        core_id="cm7", sku="S",
    )
    plan = flash_plan.plan_swd_probe(inp, lambda name: name == "JLinkExe")
    assert "@" not in plan.ok_message
    assert "0x08000000" not in plan.ok_message


def test_swd_probe_openocd_bin_message_still_names_the_base_address():
    """The `.bin` case (unchanged) on the openocd/pyocd arm."""
    inp = _swd_inputs(interface="cmsis-dap", target="stm32h7x", base="0x08010000")
    plan = flash_plan.plan_swd_probe(inp, lambda name: name == "openocd")
    assert "@ 0x08010000" in plan.ok_message


def test_swd_probe_openocd_elf_message_omits_an_address_the_tool_never_received():
    """tan-cli#487, defect 6, the openocd/pyocd arm: the argv itself already
    withholds `base` for a non-`.bin` artefact (`program ... exit`, no
    trailing address -- see the plan-builder's own comment); the message must
    agree. Fails against the pre-fix source (measured: `@ 0x08000000` was
    present)."""
    inp = FlashInputs(
        artefact="/build/zephyr.elf",
        flash_args={"interface": "cmsis-dap", "target": "stm32h7x"},
        core_id="cm7", sku="S",
    )
    plan = flash_plan.plan_swd_probe(inp, lambda name: name == "openocd")
    assert "@" not in plan.ok_message
    assert "0x08000000" not in plan.ok_message


# ── swd_probe J-Link DPIDR preflight (tan-cli#520) ───────────────────────────
#
# swd_probe flashes an external helper MCU (the GD32G553 supervisor) over its
# own SWD header, and -- like Flow D's MRAM write -- had NO wrong-board guard
# at all: on the alplab-gw bench, serial `603000869` answers BOTH a real
# E1M-AEN801 J-Link (SW-DP `0x4C013477`) and a GD32 bridge probe on a
# different board entirely (SW-DP `0x0BE12477`), so `jlink_serial` alone
# cannot disambiguate them even when correctly pinned (#513). These reuse
# Flow D's own `validate_flow_d_preflight_args`/`flow_d_preflight_script`
# (`method="swd_probe"`, `require_device_key=False` -- `swd_probe`'s
# `jlink_device` already means the write's OWN `-device` profile, oracle-
# pinned with no `expect_dpidr` anywhere near it, so it cannot ALSO be
# Flow D's paired preflight-only read-device key) rather than growing a
# second checker.


def test_swd_probe_jlink_device_alone_still_reaches_the_write_no_preflight_required():
    """The regression this design decision exists to prevent: `jlink_device`
    on `swd_probe` already means the write's own `-device` profile
    (oracle-pinned: `tests/parity/test_flash_oracle_parity.py`'s
    `jlink-bin-artefact-uses-loadbin` sets `jlink_device: NRF_DUMMY` with no
    `expect_dpidr` at all and expects `ok: true`/no refusal). Naively pairing
    `expect_dpidr` with `jlink_device` the way Flow D pairs its OWN (distinct)
    `jlink_device` would retroactively demand a preflight of every manifest
    that only ever set the write device -- this proves it still does not."""
    plan = flash_plan.plan_swd_probe(
        _swd_inputs(jlink_device="NRF_DUMMY"), lambda name: name == "JLinkExe"
    )
    assert plan.argv[0] == "JLinkExe"
    assert plan.preflight_device == "NRF_DUMMY"


@pytest.mark.parametrize("bad_value", ["", None], ids=["empty-string", "null"])
def test_swd_probe_expect_dpidr_present_but_null_or_empty_refuses(bad_value):
    """Mirrors Flow D's own `test_flow_d_preflight_present_but_null_or_empty_
    expect_dpidr_refuses`: a PRESENT `expect_dpidr` that resolves to `None`
    must refuse loudly rather than silently falling through to "no preflight
    armed" -- the one guard standing between a wrong-board attach and a GD32
    write. Fails against the pre-fix source (measured: `expect_dpidr` was not
    read by `plan_swd_probe` at all, so this raised nothing)."""
    inp = _swd_inputs(expect_dpidr=bad_value)
    with pytest.raises(FlashPlanError) as raised:
        flash_plan.plan_swd_probe(inp, lambda name: name == "JLinkExe")
    assert "expect_dpidr" in str(raised.value)


def test_swd_probe_expect_dpidr_is_refused_on_the_openocd_arm():
    """The same accept-and-ignore shape #513 closed for `jlink_serial`, one
    field over: the DPIDR read is a JLinkExe-only primitive, so a manifest
    naming `expect_dpidr` that lands on the openocd/pyocd arm must refuse,
    not silently drop the wrong-board guard. Fails against the pre-fix source
    (measured: `expect_dpidr` was not read anywhere in `plan_swd_probe`, so
    this built an openocd plan with `ok: true` and no preflight ever armed)."""
    inp = _swd_inputs(interface="cmsis-dap", target="gd32g553", expect_dpidr="0x4C013477")
    with pytest.raises(FlashPlanError) as raised:
        flash_plan.plan_swd_probe(inp, lambda name: name == "openocd")
    assert "expect_dpidr" in str(raised.value)
    assert "openocd/pyocd" in str(raised.value)


def test_swd_probe_no_expect_dpidr_still_reaches_the_openocd_arm_unaffected():
    """The unaffected case, mirroring `test_swd_probe_no_serial_still_
    reaches_the_openocd_arm_unaffected`: a manifest naming no `expect_dpidr`
    at all keeps working on the openocd/pyocd arm exactly as before."""
    inp = _swd_inputs(interface="cmsis-dap", target="gd32g553")
    plan = flash_plan.plan_swd_probe(inp, lambda n: n == "openocd")
    assert plan.argv[0] == "openocd"


def test_swd_probe_armed_preflight_reuses_the_resolved_write_device():
    """`flow_d_preflight_script`'s `read_device` override, exercised through
    the public seam `plan_swd_probe` writes into (`FlashPlan.preflight_
    device`) -- the preflight's own connect script must use the SAME device
    the write already resolved, not a second manifest field."""
    inp = _swd_inputs(expect_dpidr="0x4C013477", jlink_device="GD32G553MEY7TR")
    plan = flash_plan.plan_swd_probe(inp, lambda name: name == "JLinkExe")
    assert plan.preflight_device == "GD32G553MEY7TR"
    script, expected = flash_plan.flow_d_preflight_script(
        inp, "swd_probe", read_device=plan.preflight_device
    )
    assert expected == "0x4C013477"
    assert "device GD32G553MEY7TR" in script
    # Read-only: no write command anywhere in the preflight script.
    assert "loadfile" not in script
    assert "loadbin" not in script
    assert "\nr\n" not in script
    assert "\nhalt" not in script


def test_swd_probe_wrong_board_refuses_before_any_write(tmp_path, monkeypatch):
    """tan-cli#520, the headline defect. A CONFIRMED, non-dry-run `swd_probe`
    entry whose read-only DPIDR preflight catches a wrong-board mismatch must
    abort BEFORE the real GD32 bridge write -- reusing Flow D's own
    `_flow_d_preflight` runner (`method="swd_probe"`), the same fix #512 gave
    Flow D's MRAM write.

    `_spawn_jlink` is the ONE spawn site the preflight probe and the eventual
    real write share -- stubbed here to a canned "a different board answered"
    banner (the GD32 bridge's real measured SW-DP ID, `0x0BE12477`, versus an
    `expect_dpidr` deliberately set to the AEN E8's, `0x4C013477`) and to
    RECORD every script it is asked to run. The load-bearing assertion is on
    that record, not just the exit code: exactly ONE JLinkExe session must
    run (the read-only preflight, `si SWD`/`connect`/`exit`, no `loadfile`/
    `loadbin`), never a second one carrying the write's own script.

    Verified to fail against the pre-fix source (measured: with no preflight
    wired for `swd_probe` at all, `_spawn_jlink` is called exactly once -- but
    with the WRITE script, `loadfile ... \\nr\\ng\\nqc`, not the read-only
    one -- and reports `status: failed` because the STUBBED spawn always
    returns `success=False`, not because any preflight ran; the load-bearing
    proof is the recorded script's own content, not the status/exit code,
    which is `failed`/1 either way here)."""
    (tmp_path / "build").mkdir()
    (tmp_path / "sdk" / "scripts").mkdir(parents=True)
    (tmp_path / "sdk" / "scripts" / "alp_project.py").write_text("", encoding="utf-8")

    manifest = """schema_version: 1
hw_info: {sku: S}
slices: []
helper_mcus:
- {name: gd32_bridge, chip: gd32g553, firmware_path: zephyr.bin,
   flash_method: swd_probe,
   flash_args: {jlink_device: GD32G553MEY7TR, expect_dpidr: "0x4C013477",
                base: "0x08000000"}}
boot_order: []
"""
    (tmp_path / "build" / "system-manifest.yaml").write_text(
        manifest, encoding="utf-8", newline=""
    )

    fake_tools = tmp_path / "faketools"
    fake_tools.mkdir()
    jlink_path = fake_tools / ("JLinkExe.exe" if os.name == "nt" else "JLinkExe")
    jlink_path.write_text("", encoding="utf-8")
    if os.name != "nt":
        os.chmod(jlink_path, 0o755)
    monkeypatch.setenv("PATH", str(fake_tools))
    monkeypatch.setattr(flash_cmd, "venv_bin_dir", lambda *_a, **_k: None)

    calls: list[str] = []

    def _fake_spawn_jlink(argv, script, capture, timeout, venv_bin=None, workspace=None):
        calls.append(script)
        return flash_cmd._Outcome(
            success=False,
            stdout="Connecting to target via SWD\nFound SW-DP with ID 0x0BE12477\n",
            stderr="",
        )

    monkeypatch.setattr(flash_cmd, "_spawn_jlink", _fake_spawn_jlink)

    exit_code, data, issues, _lines, _sdk = flash_cmd._run(
        app_path=".", build_root_arg=None, sdk_root_arg=str(tmp_path / "sdk"),
        board_yaml=None, core=None, helper=None, dry_run=False,
        skip_missing_tools=False, capture=True, cwd=str(tmp_path),
    )

    assert exit_code == 1
    entry = data["entries"][0]
    assert entry["status"] == "failed"
    assert "expected SW-DP IDR 0x4C013477" in entry["message"], entry["message"]
    assert "0x0BE12477" in entry["message"], entry["message"]
    assert any(i.code == "flash.entry-failed" for i in issues)
    # The load-bearing proof that nothing was written: exactly one JLinkExe
    # session ran at all (the preflight), and its script never carries a
    # write command.
    assert len(calls) == 1, calls
    assert "loadfile" not in calls[0]
    assert "loadbin" not in calls[0]
    assert calls[0].splitlines()[0] == "si SWD"


def test_swd_probe_jlink_device_charset_guarded_at_plan_time():
    """tan-cli#520 REVIEW round 2, MAJOR. `_resolve_jlink_device` used to
    return `flash_args.jlink_device` VERBATIM (`fa_str_checked`, no charset
    guard -- safe while the value only ever reached ARGV, a list element,
    newline-inert). Round 1's fix validated it only inside `flow_d_preflight_
    script`'s CONSUMER, at real-write time -- so `plan_swd_probe` itself
    (called for `--dry-run` too) still built a plan with the hostile value
    uninspected. Now `_resolve_jlink_device` validates it directly, at PLAN
    time, the same place `jlink_serial` already is (a few lines below in the
    same function) -- so this refuses at the FIRST point `plan_swd_probe` can
    reach it, not several call-frames downstream.

    Fails against the round-1 source a90e4df (measured: `plan_swd_probe`
    itself raised nothing here -- the hostile value only got caught later,
    inside `flow_d_preflight_script`, and only when THAT function actually
    ran, which `--dry-run` never reaches)."""
    hostile = "GD32G553MEY7TR\nloadfile /tmp/evil.bin 0x08000000\nr\ng"
    inp = _swd_inputs(jlink_device=hostile, expect_dpidr="0x0BE12477")
    with pytest.raises(FlashPlanError) as raised:
        flash_plan.plan_swd_probe(inp, lambda name: name == "JLinkExe")
    assert "jlink_device" in str(raised.value)


def test_swd_probe_hostile_jlink_device_refuses_identically_dry_run_and_real():
    """tan-cli#520 REVIEW round 2, MAJOR's own headline measurement: the SAME
    manifest must not get two different verdicts depending on `--dry-run`.
    `--dry-run` always forces the J-Link arm (`plan_swd_probe`'s own
    bypass), so BOTH modes now reach the same plan-time guard and refuse for
    the same reason.

    Fails against the round-1 source a90e4df (measured: `dry_run=True`
    returned a plan with `planning_only=True` and no refusal at all --
    `flow_d_preflight_script`, the only validator that round added, is never
    called under `--dry-run`; `dry_run=False` on a J-Link host refused only
    once execution reached the write-time preflight consumer)."""
    hostile = "GD32G553MEY7TR\nloadfile /tmp/evil.bin 0x08000000\nr\ng"
    args = {"jlink_device": hostile, "expect_dpidr": "0x0BE12477", "base": "0x08000000"}
    dry = FlashInputs(artefact="/build/zephyr.bin", flash_args=args, core_id="b", sku="S", dry_run=True)
    real = FlashInputs(artefact="/build/zephyr.bin", flash_args=args, core_id="b", sku="S", dry_run=False)
    with pytest.raises(FlashPlanError) as dry_raised:
        flash_plan.plan_swd_probe(dry, lambda name: name == "JLinkExe")
    with pytest.raises(FlashPlanError) as real_raised:
        flash_plan.plan_swd_probe(real, lambda name: name == "JLinkExe")
    assert "jlink_device" in str(dry_raised.value)
    assert "jlink_device" in str(real_raised.value)
    assert str(dry_raised.value) == str(real_raised.value)


def test_swd_probe_openocd_arm_hostile_jlink_device_refuses_identically_dry_run_and_real():
    """tan-cli#520 REVIEW round 3, finding 1. The test just above forces the
    J-Link arm on BOTH calls (`which()` always reports `JLinkExe` present),
    so it never exercises the openocd/pyocd arm at all -- which is exactly
    why the charset guard living only inside `_resolve_jlink_device` (called
    from the J-Link arm alone) went uncaught: on a host where `which()` finds
    only `openocd`, a real run took the openocd/pyocd arm, which never calls
    `_resolve_jlink_device` and never reads `jlink_device` at all, while
    `--dry-run` (which always forces the J-Link arm regardless of `which()`)
    still reached the guard and refused. Same manifest, two different
    verdicts depending on host tooling and `--dry-run` alone.

    `which` here reports NO J-Link binary present (only `openocd`), so the
    real call is forced onto the openocd/pyocd arm while `--dry-run` still
    forces the J-Link arm on its own -- the two calls below therefore
    exercise the two DIFFERENT arms of the split, and both must refuse for
    the identical reason now that the guard is hoisted above it.

    Fails against the round-2 source (measured: `dry_run=True` raised
    `FlashPlanError` naming `jlink_device`; `dry_run=False` with only
    `openocd` on `PATH` returned a plan with `ok_message` reading `swd_probe
    [c]: gd32g553 flashed via openocd @ 0x08000000`, the hostile value never
    inspected)."""
    hostile = "GD32G553MEY7TR\nloadfile /tmp/evil.bin 0x08000000\nr\ng"
    args = {"jlink_device": hostile, "interface": "cmsis-dap", "target": "gd32g553"}
    dry = FlashInputs(artefact="/build/zephyr.bin", flash_args=args, core_id="c", sku="S", dry_run=True)
    real = FlashInputs(artefact="/build/zephyr.bin", flash_args=args, core_id="c", sku="S", dry_run=False)
    which_openocd_only = lambda name: name == "openocd"  # noqa: E731
    with pytest.raises(FlashPlanError) as dry_raised:
        flash_plan.plan_swd_probe(dry, which_openocd_only)
    with pytest.raises(FlashPlanError) as real_raised:
        flash_plan.plan_swd_probe(real, which_openocd_only)
    assert "jlink_device" in str(dry_raised.value)
    assert "jlink_device" in str(real_raised.value)
    assert str(dry_raised.value) == str(real_raised.value)


def test_swd_probe_preflight_read_device_defensive_guard_still_independent():
    """The BELT-AND-BRACES half of BLOCKER 1 (round 1's fix, kept per the
    reviewer's own note: "keep :2331 as the defensive repeat its own comment
    already calls it") -- exercised directly against `flow_d_preflight_
    script`, bypassing `plan_swd_probe` entirely, so this proves the second
    layer independently refuses even for a caller that does not go through
    the now-guarded `_resolve_jlink_device` at all."""
    hostile = "GD32G553MEY7TR\nloadfile /tmp/evil.bin 0x08000000\nr\ng"
    inp = _swd_inputs(expect_dpidr="0x0BE12477")
    with pytest.raises(FlashPlanError) as raised:
        flash_plan.flow_d_preflight_script(inp, "swd_probe", read_device=hostile)
    assert "jlink_device" in str(raised.value)


def test_swd_probe_openocd_arm_with_jlink_device_set_still_writes_with_no_expect_dpidr(
    tmp_path, monkeypatch
):
    """tan-cli#520 REVIEW, BLOCKER 2. A manifest that sets `flash_args.
    jlink_device` (e.g. as a J-Link fallback profile) while `flash_args.
    use_openocd: true` forces the openocd arm for real must still flash
    successfully when `expect_dpidr` is absent -- the SAME manifest already
    reported `ok` under `--dry-run` (`plan_swd_probe`'s own plan-time guard
    only ever checks `expect_dpidr` on this arm, never `jlink_device`), and a
    write-time-only refusal here would be `--dry-run` and a real run
    disagreeing on the identical input.

    Fails against the pre-fix source (measured: gating the preflight call on
    `method == "swd_probe"` alone passed `read_device=None` here, which
    `flow_d_preflight_script` read as "derive `require_device_key` from
    `read_device is None`" -- i.e. Flow D's PAIRED shape -- so `jlink_device`
    present without `expect_dpidr` refused at write time with `flash.entry-
    failed`, `exit 1`)."""
    (tmp_path / "build").mkdir()
    (tmp_path / "sdk" / "scripts").mkdir(parents=True)
    (tmp_path / "sdk" / "scripts" / "alp_project.py").write_text("", encoding="utf-8")

    manifest = """schema_version: 1
hw_info: {sku: S}
slices: []
helper_mcus:
- {name: gd32_bridge, chip: gd32g553, firmware_path: zephyr.bin,
   flash_method: swd_probe,
   flash_args: {jlink_device: GD32G553MEY7TR, use_openocd: true,
                interface: cmsis-dap, target: gd32g553, base: "0x08000000"}}
boot_order: []
"""
    (tmp_path / "build" / "system-manifest.yaml").write_text(
        manifest, encoding="utf-8", newline=""
    )

    fake_tools = tmp_path / "faketools"
    fake_tools.mkdir()
    openocd_path = fake_tools / ("openocd.exe" if os.name == "nt" else "openocd")
    openocd_path.write_text("", encoding="utf-8")
    if os.name != "nt":
        os.chmod(openocd_path, 0o755)
    monkeypatch.setenv("PATH", str(fake_tools))
    monkeypatch.setattr(flash_cmd, "venv_bin_dir", lambda *_a, **_k: None)
    monkeypatch.setattr(
        flash_cmd, "_spawn", lambda *_a, **_k: flash_cmd._Outcome(success=True, stdout="", stderr="")
    )

    exit_code, data, issues, _lines, _sdk = flash_cmd._run(
        app_path=".", build_root_arg=None, sdk_root_arg=str(tmp_path / "sdk"),
        board_yaml=None, core=None, helper=None, dry_run=False,
        skip_missing_tools=False, capture=True, cwd=str(tmp_path),
    )

    assert exit_code == 0
    entry = data["entries"][0]
    assert entry["status"] == "ok", entry
    assert "flashed via openocd" in entry["message"]
    assert not any(i.code == "flash.entry-failed" for i in issues)
    # The design-point warning (unarmed preflight) is scoped to the J-Link
    # arm only (tan-cli#520 REVIEW) -- this run never took it at all, so no
    # warning fires either, matching #519's note that the durable multi-probe
    # answer for openocd differs (a USB-path selector, not `expect_dpidr`).
    assert not any(i.code == "flash.dpidr-preflight-unarmed" for i in issues)


def test_swd_probe_jlink_write_with_no_expect_dpidr_warns_unarmed(tmp_path, monkeypatch):
    """tan-cli#520 REVIEW, the design point: `expect_dpidr` stays optional
    (no shipped preset carries a SW-DP ID for tan to require), but a
    confirmed real `swd_probe` J-Link write that ran with none set used to
    give no signal at all that its wrong-board guard never ran. A
    `flash.dpidr-preflight-unarmed` warning now fires on exactly that shape:
    a successful J-Link write, `expect_dpidr` absent.

    Fails against the pre-fix source (measured: no such code exists at all
    before this review round, so this assertion cannot pass against it)."""
    (tmp_path / "build").mkdir()
    (tmp_path / "sdk" / "scripts").mkdir(parents=True)
    (tmp_path / "sdk" / "scripts" / "alp_project.py").write_text("", encoding="utf-8")

    manifest = """schema_version: 1
hw_info: {sku: S}
slices: []
helper_mcus:
- {name: gd32_bridge, chip: gd32g553, firmware_path: zephyr.bin,
   flash_method: swd_probe, flash_args: {base: "0x08000000"}}
boot_order: []
"""
    (tmp_path / "build" / "system-manifest.yaml").write_text(
        manifest, encoding="utf-8", newline=""
    )

    fake_tools = tmp_path / "faketools"
    fake_tools.mkdir()
    jlink_path = fake_tools / ("JLinkExe.exe" if os.name == "nt" else "JLinkExe")
    jlink_path.write_text("", encoding="utf-8")
    if os.name != "nt":
        os.chmod(jlink_path, 0o755)
    monkeypatch.setenv("PATH", str(fake_tools))
    monkeypatch.setattr(flash_cmd, "venv_bin_dir", lambda *_a, **_k: None)
    monkeypatch.setattr(
        flash_cmd, "_spawn", lambda *_a, **_k: flash_cmd._Outcome(success=True, stdout="", stderr="")
    )

    exit_code, data, issues, _lines, _sdk = flash_cmd._run(
        app_path=".", build_root_arg=None, sdk_root_arg=str(tmp_path / "sdk"),
        board_yaml=None, core=None, helper=None, dry_run=False,
        skip_missing_tools=False, capture=True, cwd=str(tmp_path),
    )

    assert exit_code == 0
    entry = data["entries"][0]
    assert entry["status"] == "ok", entry
    warnings = [i for i in issues if i.code == "flash.dpidr-preflight-unarmed"]
    assert len(warnings) == 1, issues
    assert warnings[0].severity == "warning"
    assert "expect_dpidr" in warnings[0].message


def test_swd_probe_jlink_write_with_no_expect_dpidr_warns_unarmed_in_text_output(
    tmp_path, monkeypatch
):
    """tan-cli#520 REVIEW round 3, finding 2. The test just above proves the
    warning reaches `issues` (`--format json`); this proves it ALSO reaches
    `text_lines`, which is ALL that prints in `tan`'s DEFAULT, non-JSON mode.
    Before this fix, a plain `tan flash` against the identical manifest
    printed only `ok: swd_probe[...] flashed via J-Link @ ...` -- no hint
    the wrong-board guard never armed, for the exact bench operator (a
    cloned probe serial reaching the wrong board) who most needs the signal
    and does not pass `--format json`.

    Fails against the pre-fix source (measured: `flash.dpidr-preflight-
    unarmed` reached `issues` but `_run`'s `text_lines` carried no mention of
    `expect_dpidr` at all -- only the entry's own `ok:` line and the trailing
    `flash: 0 failure(s).` summary)."""
    (tmp_path / "build").mkdir()
    (tmp_path / "sdk" / "scripts").mkdir(parents=True)
    (tmp_path / "sdk" / "scripts" / "alp_project.py").write_text("", encoding="utf-8")

    manifest = """schema_version: 1
hw_info: {sku: S}
slices: []
helper_mcus:
- {name: gd32_bridge, chip: gd32g553, firmware_path: zephyr.bin,
   flash_method: swd_probe, flash_args: {base: "0x08000000"}}
boot_order: []
"""
    (tmp_path / "build" / "system-manifest.yaml").write_text(
        manifest, encoding="utf-8", newline=""
    )

    fake_tools = tmp_path / "faketools"
    fake_tools.mkdir()
    jlink_path = fake_tools / ("JLinkExe.exe" if os.name == "nt" else "JLinkExe")
    jlink_path.write_text("", encoding="utf-8")
    if os.name != "nt":
        os.chmod(jlink_path, 0o755)
    monkeypatch.setenv("PATH", str(fake_tools))
    monkeypatch.setattr(flash_cmd, "venv_bin_dir", lambda *_a, **_k: None)
    monkeypatch.setattr(
        flash_cmd, "_spawn", lambda *_a, **_k: flash_cmd._Outcome(success=True, stdout="", stderr="")
    )

    exit_code, data, issues, lines, _sdk = flash_cmd._run(
        app_path=".", build_root_arg=None, sdk_root_arg=str(tmp_path / "sdk"),
        board_yaml=None, core=None, helper=None, dry_run=False,
        skip_missing_tools=False, capture=True, cwd=str(tmp_path),
    )

    assert exit_code == 0
    entry = data["entries"][0]
    assert entry["status"] == "ok", entry
    assert any(i.code == "flash.dpidr-preflight-unarmed" for i in issues)
    assert any("expect_dpidr" in line for line in lines), lines


# ── yocto_wic target/compress validation (tan-cli#487) ──────────────────────


def _wic_inputs(target="/dev/sdb", artefact="/build/core-image.wic", **extra_flash_args):
    fa = {"target": target, "confirm": True, **extra_flash_args}
    return FlashInputs(artefact=artefact, flash_args=fa, core_id="a55", sku="S")


def test_resolve_dev_root_closes_the_traversal_repro():
    """tan-cli#487, defect 1's primary repro: the old bare
    `target.startswith("/dev/")` check is satisfied by
    `/dev/../home/<user>/important.img` -- the STRING itself starts with
    `/dev/` -- so `plan_yocto_wic` planned `dd ... of=/dev/../home/<user>/
    important.img`, and once confirmed, `dd` overwrites that regular file
    with the whole image. Fails against the pre-fix source (measured: the
    old check is a bare `startswith`, so it answers True/no-refusal for this
    exact string)."""
    assert flash_plan._resolve_dev_root("/dev/../home/dev/important.img") is None


def test_resolve_dev_root_accepts_real_looking_devices():
    """The happy path this fix must not trade away: a real, plausible block
    device target still resolves."""
    assert flash_plan._resolve_dev_root("/dev/sdb") == "/dev/sdb"
    assert flash_plan._resolve_dev_root("/dev/mmcblk0") == "/dev/mmcblk0"
    assert flash_plan._resolve_dev_root("/dev/nvme0n1") == "/dev/nvme0n1"
    assert flash_plan._resolve_dev_root("/dev/disk/by-id/foo") == "/dev/disk/by-id/foo"


def test_resolve_dev_root_rejects_dev_itself():
    assert flash_plan._resolve_dev_root("/dev") is None
    assert flash_plan._resolve_dev_root("/dev/") is None


def test_plan_yocto_wic_refuses_the_traversal_target_with_the_original_wording():
    """The refusal MESSAGE is oracle-parity-pinned for a plain out-of-`/dev/`
    target (`./oops`, `tests/parity/oracle_fixtures/test_flash_oracle_parity.
    json`'s `yocto-target-must-be-a-device` case) -- this proves the
    traversal shape gets the IDENTICAL wording, not a new one: the fix only
    tightens the CONDITION `plan_yocto_wic` checks, never the text."""
    target = "/dev/../home/dev/important.img"
    with pytest.raises(FlashPlanError) as raised:
        flash_plan.plan_yocto_wic(_wic_inputs(target=target), lambda _n: True)
    msg = str(raised.value)
    assert msg == (
        f"yocto_wic: refusing target '{target}' -- must start with /dev/ to avoid "
        "clobbering a regular file. Set flash_args.target to a real block device."
    )


def test_yocto_wic_block_device_refusal_rejects_an_existing_regular_file(tmp_path):
    """tan-cli#487, defect 1's second tier: a regular file that lexically
    lives under a real path (the issue's own `/dev/shm/<name>` shape,
    stood in here by an ordinary `tmp_path` file so the test needs no real
    `/dev/shm` access) passes `_resolve_dev_root`'s pure lexical check
    clean -- `_yocto_wic_block_device_refusal` is what refuses it. Fails
    against the pre-fix source (measured: the function does not exist)."""
    spill = tmp_path / "important.img"
    spill.write_bytes(b"do-not-clobber-me")
    refusal = flash_cmd._yocto_wic_block_device_refusal(str(spill))
    assert refusal is not None
    assert "not a block device" in refusal
    assert str(spill) in refusal
    assert spill.read_bytes() == b"do-not-clobber-me"  # never touched


def test_yocto_wic_block_device_refusal_accepts_a_real_block_device():
    """The happy path this fix must not trade away, proven for a target that
    `stat`s as an actual block device via an INJECTED `stat_fn` -- this
    sandbox has no real block device (`os.mknod` for one needs root), and
    the real, process-wide `os.stat` must never be monkeypatched here:
    pytest's own internals call it too (measured: a first draft that
    monkeypatched `flash_cmd.os.stat` globally broke the whole test run's
    OWN teardown, not just this assertion)."""
    real_mode = stat.S_IFBLK | 0o660
    fake_stat = lambda _p: types.SimpleNamespace(st_mode=real_mode)  # noqa: E731
    assert flash_cmd._yocto_wic_block_device_refusal("/dev/sda", stat_fn=fake_stat) is None


def test_yocto_wic_block_device_refusal_resolves_a_real_symlink_via_the_real_stat(tmp_path):
    """A `/dev/disk/by-id/...`-style symlink: the real, un-injected `os.stat`
    resolves it to the real target's mode by itself, with no separate
    realpath step needed here. Proven with a REAL symlink to a REAL,
    on-disk target rather than an injected `stat_fn` that ignores its path
    argument -- an earlier version of this test used
    `fake_stat = lambda _p: ...st_mode=real_mode`, which is byte-for-byte the
    preceding accepts-a-real-block-device test under a different string, and
    proved nothing beyond "the fixed mode we handed it came back".

    Renamed from `..._follows_a_real_symlink_to_its_own_mode` (tan-cli#511).
    The old name and docstring claimed this proves the symlink is FOLLOWED
    -- i.e. that `os.stat` (which resolves through a link) behaves
    differently here from `os.lstat` (which reports the link node itself).
    That is NOT what this test proves, and re-checking it exposed the gap:
    a symlink node is itself `S_IFLNK`, so `os.lstat` on `link` ALSO reports
    "not a block device", for exactly the same `not stat.S_ISBLK(mode)`
    branch the resolved target hits under `os.stat`. The one assertion this
    test makes (`"not a block device" in refusal`) is identical either way.
    Measured directly: monkeypatching this function's default `stat_fn` from
    `os.stat` to `os.lstat` and re-running `python -m pytest tests -q` from
    `python/` still gives a fully green bar -- nothing here, or anywhere
    else in the suite, distinguishes the two. Proving the FOLLOW property
    for real needs a genuine block device on the far end of the link
    (`os.mknod` for one needs root) so a resolved-vs-unresolved mode
    actually differ observably; that is out of reach in this sandbox, so
    this test is honestly scoped down to what it can prove: the real
    `os.stat`, given a real symlink, resolves it to the real target's mode
    without a separate realpath step, and the refusal is computed over that
    resolved mode -- proven here for a resolved mode of "regular file"
    rather than "block device", since only the former is constructible
    without root.

    The target is a plain regular file created in `tmp_path`, not a
    platform device name -- `S_ISBLK` is False for a regular file on every
    platform, so this needs no `sys.platform` branch (an earlier version
    used Windows' `NUL` / POSIX's `/dev/null`, but `link.symlink_to("NUL")`
    on Windows writes a RELATIVE reparse target resolved against the link's
    own parent directory, not the `NUL` DOS device name, so it pointed at a
    nonexistent path and hit the `FileNotFoundError` branch instead of the
    `not a block device` branch this test means to exercise)."""
    real_target = tmp_path / "not-a-block-device.img"
    real_target.write_bytes(b"stand-in for a real, non-block target")
    link = tmp_path / "by-id-stand-in"
    link.symlink_to(real_target)
    refusal = flash_cmd._yocto_wic_block_device_refusal(str(link))
    assert refusal is not None
    assert "not a block device" in refusal


def test_yocto_wic_block_device_refusal_fails_open_only_when_the_parent_is_dev_itself():
    """tan-cli#487 review finding 1: the OLD blanket fail-open on
    `FileNotFoundError` (for ANY target, not just a direct `/dev/<name>`
    child) let a typo'd `flash_args.target` (`/dev/shm/sdb` for `/dev/sdb`)
    `dd` a whole multi-GB image into a BRAND-NEW file under a real `/dev/`
    subtree at `ok:true`/exit 0. The narrowed contract: fail open ONLY when
    `target`'s own parent directory is `/dev` itself -- the devtmpfs root,
    where "does not exist" genuinely means "not plugged in" -- which is
    exactly the shape tan's OWN oracle-parity suite depends on (it spawns a
    real `dd` against `flash_args.target: /dev/sdb` BY DESIGN because that
    device does not exist on any host the suite runs on:
    `tests/parity/test_flash_oracle_parity.py::
    test_a_real_spawn_diffs_including_the_captured_failure_tail`, a frozen
    fixture under `python/tests/parity/` that may not move). A deeper `/dev/`
    subtree now refuses instead. Fails against the pre-fix source (measured:
    `/dev/shm/tan_review_missing.img` also returned `None`, no refusal)."""
    # The happy path this fix must not trade away, and the exact shape the
    # frozen oracle-parity fixture needs: a direct /dev/<name> child that
    # simply is not plugged in yet.
    assert flash_cmd._yocto_wic_block_device_refusal("/dev/definitely-not-there-xyz") is None

    refusal = flash_cmd._yocto_wic_block_device_refusal("/dev/shm/tan_review_missing.img")
    assert refusal is not None
    assert "does not exist" in refusal
    assert "/dev/shm/tan_review_missing.img" in refusal


def test_yocto_wic_block_device_refusal_refuses_a_traversal_through_a_symlinked_dev_child():
    """tan-cli#487 review finding 1, the `/dev/x/../sdb` shape: were `/dev/x`
    a symlink, the KERNEL resolves `..` against ITS real target, not against
    `/dev` -- a lexically-`..`-collapsed dirname would misread this as
    "parent is /dev" and fail open regardless of where `/dev/x` actually
    points. `posixpath.dirname` is taken on the RAW string here (`/dev/x/..`,
    not `/dev`), so this refuses instead. `/dev/x` need not even exist on the
    test host for this to reach the same `FileNotFoundError` branch --
    ENOENT on any missing intermediate component behaves identically to
    ENOENT on the leaf. Fails against the pre-fix source (measured: `None`,
    no refusal -- the old code failed open on ANY `FileNotFoundError`)."""
    refusal = flash_cmd._yocto_wic_block_device_refusal("/dev/x/../sdb")
    assert refusal is not None
    assert "does not exist" in refusal


def test_yocto_wic_write_time_gate_is_actually_wired_through_a_real_flash_entry(
    tmp_path, monkeypatch
):
    """tan-cli#487 review finding 3: mutation-proven that NOTHING previously
    exercised the write-time gate's WIRING -- deleting the whole
    `if method in YOCTO_WIC_METHODS: ... return 1, entry(...)` block at
    `flash_cmd.py:1373-1380` still passed the full suite (the review's own
    measurement). Every existing `_yocto_wic_block_device_refusal` test
    calls the helper directly; the only path that reaches it through a real
    spawn is the oracle-parity fixture, which exercises the FAIL-OPEN branch
    (a target that does not exist) -- never the refusal branch.

    **Not a subprocess this time (tan-cli#511).** The original version of
    this test drove a real `python -m tan flash` subprocess against a
    genuine `/dev/shm/<name>` regular file -- `/dev/shm` is Linux tmpfs, and
    neither macOS nor Windows have anywhere writable under a path lexically
    starting with `/dev/` (the string `plan_yocto_wic`'s own oracle-pinned
    "must start with /dev/" refusal requires regardless of host, real device
    or not). That made the whole test Linux-only by construction, which is
    exactly the "a platform loses the protection permanently" failure a
    data-loss gate cannot afford. `_flash_entry` (the REAL, unmodified
    dispatch function `tan flash`'s own loop calls) now takes an injectable
    `yocto_wic_stat` -- the write-time gate's own `stat_fn` parameter,
    threaded one level further out -- for exactly this reason: the same
    pattern `_yocto_wic_block_device_refusal`'s OWN direct-call tests already
    use to fake `st_mode` (`test_yocto_wic_block_device_refusal_accepts_a_
    real_block_device` above), now reaching through the real call site
    instead of the helper alone. This calls `_flash_entry` itself, in
    process -- no subprocess, no real filesystem entity under a `/dev/`-
    rooted path on any platform -- so the wiring proof holds identically on
    Linux, macOS, and Windows.

    Proves the wiring TWO ways, either one independently sufficient: the
    returned entry fails with the block-device message, AND `_execute` (the
    function that would actually spawn `dd`/`bmaptool`) is monkeypatched to
    raise if it is ever called at all -- the direct assertion that nothing
    was spawned, standing in for "the file's bytes are never touched" now
    that there is no longer a real file in the loop for a mutated gate to
    clobber. `wic_target` itself needs no real backing file for the SAME
    reason `yocto_wic_stat` needs no real filesystem interaction: it is
    passed to the injected `stat_fn`, which ignores it and answers a fixed,
    fake regular-file mode.

    This is a wiring-regression test, not a pre/post-fix behaviour test: the
    gate itself already exists on `451304a` and this test passes there too
    -- what it proves is that DELETING the gate (`flash_cmd.py:1373-1380`)
    now makes THIS test fail (confirmed by manually deleting that block and
    re-running: `_execute` is reached, the injected spy raises, and the test
    errors), closing the exact hole the review's own mutation run found (the
    full suite passed with the block deleted, because nothing reached it
    through a real spawn). Restored immediately after that manual check;
    the mutation itself is not encoded here."""
    from tan.commands.flash_cmd import _Context, FlashTarget  # noqa: PLC0415 -- test-only seam

    def _execute_must_not_run(*_args, **_kwargs):
        raise AssertionError(
            "flash_cmd._execute was invoked -- the write-time block-device "
            "gate did not fire before the real spawn, exactly the wiring "
            "hole this test exists to catch"
        )

    monkeypatch.setattr(flash_cmd, "_execute", _execute_must_not_run)

    build_root = tmp_path / "build"
    build_root.mkdir()
    (build_root / "core-image.wic").write_bytes(b"fake-image-bytes")

    # A stub `dd` so the required-tool gate (and `plan_yocto_wic`'s own probe)
    # find SOMETHING, on every platform, without needing a real `dd`/
    # `bmaptool` on PATH -- `tool_in_venv` only checks `.is_file()`, and this
    # directory's name is not `Scripts`, so no platform-specific `.exe` suffix
    # is required either. Never spawned: `_execute` is monkeypatched above,
    # and the gate under test refuses before dispatch would ever reach it.
    tool_stub_dir = tmp_path / "tool-stub"
    tool_stub_dir.mkdir()
    (tool_stub_dir / "dd").write_bytes(b"")

    ctx = _Context(
        sku="S",
        build_root=str(build_root),
        sdk_root=str(tmp_path),
        dry_run=False,
        skip_missing_tools=False,
        force_confirm=True,
        capture=True,
        venv_bin=tool_stub_dir,
    )
    target = FlashTarget(
        kind="slice",
        id="a55",
        flash_method="yocto_wic",
        flash_args={"target": "/dev/shm/tan-cli-487-gate.img"},
        output_artefact="core-image.wic",
    )
    fake_stat = lambda _target: types.SimpleNamespace(  # noqa: E731
        st_mode=stat.S_IFREG | 0o644
    )

    rc, result_entry, _lines = flash_cmd._flash_entry(target, ctx, yocto_wic_stat=fake_stat)

    assert rc == 1
    assert result_entry.status == "failed"
    assert "not a block device" in result_entry.message


def test_plan_yocto_wic_compress_out_of_vocabulary_string_refuses():
    """tan-cli#487, defect 2, shape 1: an explicit out-of-vocabulary
    `compress` (`zst`) used to fall through to the uncompressed `else`
    branch and raw-`dd` the still-compressed stream onto the block device.
    Fails against the pre-fix source (measured: no exception -- a plain
    `dd if=<artefact> of=<target> ...` argv, the compressed bytes going
    straight to the device)."""
    inp = _wic_inputs(artefact="/build/core-image.wic", compress="zst")
    with pytest.raises(FlashPlanError) as raised:
        flash_plan.plan_yocto_wic(inp, lambda name: name == "dd")
    assert "compress" in str(raised.value)
    assert "zst" in str(raised.value)


def test_plan_yocto_wic_compress_bare_unsupported_suffix_refuses():
    """tan-cli#487, defect 2, shape 2: NO `compress` key at all, but a
    `.wic.bz2` suffix this backend recognises as a real codec it cannot
    decompress. Fails against the pre-fix source (measured: the old
    auto-detect only recognises "gz"/"xz", so an unrecognised suffix
    silently resolved `compress` to `None` -- "genuinely uncompressed" --
    and raw-`dd`'d the compressed stream)."""
    inp = _wic_inputs(artefact="/build/core-image.wic.bz2")
    with pytest.raises(FlashPlanError) as raised:
        flash_plan.plan_yocto_wic(inp, lambda name: name == "dd")
    assert "bz2" in str(raised.value)


def test_plan_yocto_wic_compress_bad_value_shadows_correct_auto_detect():
    """tan-cli#487, defect 2, shape 3 -- the worst one: a GENUINELY `.wic.gz`
    artefact with an explicit but WRONG `compress` value. The bad explicit
    value must be refused, not silently overridden by the suffix that would
    have auto-detected correctly -- and the artefact really is gzip, so a
    silent pass-through here would raw-`dd` real compressed bytes. Fails
    against the pre-fix source (measured: no exception; a plain `dd
    if=<artefact> of=<target> ...` argv -- the correct `gunzip | dd`
    pipeline never gets built)."""
    inp = _wic_inputs(artefact="/build/core-image.wic.gz", compress="zst")
    with pytest.raises(FlashPlanError) as raised:
        flash_plan.plan_yocto_wic(inp, lambda name: name == "dd")
    assert "zst" in str(raised.value)


def test_plan_yocto_wic_uncompressed_wic_still_plans_a_plain_dd():
    """The `else` branch this fix must not take away: a genuinely
    uncompressed `.wic`, no `compress` key, still raw-`dd`s -- that is the
    CORRECT behaviour for this shape, not a bug."""
    inp = _wic_inputs(artefact="/build/core-image.wic")
    plan = flash_plan.plan_yocto_wic(inp, lambda name: name == "dd")
    assert plan.argv[0] == "dd"
    assert "if=/build/core-image.wic" in plan.argv


def test_plan_yocto_wic_gz_suffix_still_auto_detects_with_no_compress_key():
    """The auto-detect path this fix must not take away: a real `.wic.gz`
    artefact with NO `compress` key still builds the `gunzip | dd`
    pipeline."""
    inp = _wic_inputs(artefact="/build/core-image.wic.gz")
    plan = flash_plan.plan_yocto_wic(
        inp, lambda name: name in ("dd", "gunzip")
    )
    assert flash_plan.PIPE in plan.argv
    assert "gunzip" in plan.argv


def test_plan_yocto_wic_gz_suffix_auto_detects_case_insensitively():
    """tan-cli#487 review, nit 6: the `.lower()` added to the suffix
    extraction is an undeclared behaviour change worth its own test --
    `core-image.wic.GZ` used to auto-detect `compress` as `None` (an
    unrecognised suffix, "genuinely uncompressed"), so it built a raw `dd`
    of a still-gzipped stream -- a real bug, since `IMAGE_FSTYPES` casing
    varies by host/tooling. Now it correctly builds the `gunzip | dd`
    pipeline, same as the lowercase suffix. Fails against the pre-fix
    source (measured: `plan.argv[0] == "dd"`, no `gunzip`, `PIPE` absent)."""
    inp = _wic_inputs(artefact="/build/core-image.wic.GZ")
    plan = flash_plan.plan_yocto_wic(inp, lambda name: name in ("dd", "gunzip"))
    assert flash_plan.PIPE in plan.argv
    assert "gunzip" in plan.argv


def test_plan_yocto_wic_bmaptool_present_ignores_an_unsupported_compress_suffix():
    """tan-cli#487 review finding 2: a live regression this commit shipped --
    both compress refusals used to run BEFORE tool selection, so a stock
    Yocto `IMAGE_FSTYPES` artefact like `core-image.wic.zst` hard-refused
    even when `bmaptool` (this module's own docstring calls it "preferred")
    is on PATH and would have decompressed it natively without ever reading
    `compress`. Fails against the pre-fix source (measured: `FlashPlanError`
    naming the unsupported `.zst` suffix, even with `bmaptool` present)."""
    inp = _wic_inputs(artefact="/build/core-image.wic.zst")
    plan = flash_plan.plan_yocto_wic(inp, lambda name: name == "bmaptool")
    assert plan.argv == ("bmaptool", "copy", "/build/core-image.wic.zst", "/dev/sdb")


def test_plan_yocto_wic_bmaptool_present_ignores_an_out_of_vocabulary_explicit_compress():
    """The explicit-`compress` half of the same regression: an out-of-
    vocabulary `flash_args.compress` (e.g. `zst`) must not refuse a bmaptool
    flash either -- bmaptool never reads the key at all."""
    inp = _wic_inputs(artefact="/build/core-image.wic", compress="zst")
    plan = flash_plan.plan_yocto_wic(inp, lambda name: name == "bmaptool")
    assert plan.argv == ("bmaptool", "copy", "/build/core-image.wic", "/dev/sdb")


def test_plan_yocto_wic_compress_unsupported_suffix_refusal_does_not_loop_back_on_itself():
    """tan-cli#487 review finding 2: the suffix refusal used to tell the
    operator to 'Set flash_args.compress explicitly', but doing exactly that
    (with the SAME unsupported codec, now explicit) walked straight into the
    compress-value refusal a few lines down -- a dead end for a codec `dd`
    genuinely cannot decompress. The suffix-refusal message no longer offers
    that advice."""
    inp = _wic_inputs(artefact="/build/core-image.wic.bz2")
    with pytest.raises(FlashPlanError) as raised:
        flash_plan.plan_yocto_wic(inp, lambda name: name == "dd")
    assert "Set flash_args.compress explicitly" not in str(raised.value)


def test_relative_sdk_root_absolutised_before_venv_and_workspace_resolution(tmp_path, monkeypatch):
    """tan-cli#487 review finding 4: `venv_bin_dir`/`west_workspace_dir` used
    to be called with `resolved_sdk` -- the LITERAL, possibly-relative
    `--sdk-root` value -- 57 lines before defect 3's own fix absolutised a
    DIFFERENT copy of the same value (`ctx.sdk_root`) for artefact
    resolution. Both walk `Path(sdk_root).parent` looking for the
    workspace-wide `.venv` (`tan.core.venv.find_workspace_venv`/
    `_zephyr_base_venv`), so a relative `--sdk-root ../alp-sdk` produced a
    RELATIVE `../.venv/bin` that `prepend_path` then put on the spawned
    child's PATH -- while `_execute` spawns with `cwd=ctx.workspace` (the
    west topdir), not this process's cwd: the tool half of defect 3's
    "validate one base, execute against another" split. Fails against the
    pre-fix source (measured: both captured calls received the literal
    relative `"./sdk"` string, not an absolute path anchored on `cwd`)."""
    captured: dict[str, object] = {}

    def _capture_venv_bin_dir(_start, sdk_root):
        captured["venv_bin_dir_sdk_root"] = sdk_root
        return None

    def _capture_west_workspace_dir(_start, sdk_root):
        captured["west_workspace_dir_sdk_root"] = sdk_root
        return None

    monkeypatch.setattr(flash_cmd, "venv_bin_dir", _capture_venv_bin_dir)
    monkeypatch.setattr(flash_cmd, "west_workspace_dir", _capture_west_workspace_dir)

    (tmp_path / "build").mkdir(exist_ok=True)
    (tmp_path / "sdk" / "scripts").mkdir(parents=True, exist_ok=True)
    (tmp_path / "sdk" / "scripts" / "alp_project.py").write_text("", encoding="utf-8")
    manifest = "schema_version: 1\nhw_info: {sku: S}\nslices: []\nhelper_mcus: []\nboot_order: []\n"
    (tmp_path / "build" / "system-manifest.yaml").write_text(
        manifest, encoding="utf-8", newline=""
    )

    # `_is_sdk_root("./sdk")` checks relative to the REAL process cwd, not the
    # `cwd=` string `_run` otherwise threads through (`_run` is called with a
    # real `os.getcwd()` by the CLI wrapper) -- `run_flash`'s own harness
    # gets this for free by spawning a real subprocess with `cwd=work`.
    monkeypatch.chdir(tmp_path)

    flash_cmd._run(
        app_path=".", build_root_arg=None, sdk_root_arg="./sdk",
        board_yaml=None, core=None, helper=None, dry_run=True,
        skip_missing_tools=False, capture=True, cwd=str(tmp_path),
    )

    # `_abs_join` preserves a `.` path component rather than normalising it
    # away (see its own docstring, and
    # `test_relative_sdk_root_artefact_resolves_against_the_same_base_the_
    # flasher_spawns_from` below for the same shape), so the value
    # `venv_bin_dir` receives (a plain string) is `<cwd>/./sdk`; wrapping
    # that same value in `Path(...)` for `west_workspace_dir` (as the real
    # code does) normalises the `.` segment away.
    expected_raw = os.path.join(str(tmp_path), "./sdk")
    assert captured["venv_bin_dir_sdk_root"] == expected_raw
    assert str(captured["west_workspace_dir_sdk_root"]) == str(Path(expected_raw))
    # And the divergence this closes: neither call received the literal,
    # UN-absolutised `--sdk-root` value.
    assert captured["venv_bin_dir_sdk_root"] != "./sdk"
    assert str(captured["west_workspace_dir_sdk_root"]) != str(Path("./sdk"))


# ── relative --sdk-root artefact resolution (tan-cli#487, defect 3) ─────────


def test_relative_sdk_root_artefact_resolves_against_the_same_base_the_flasher_spawns_from(
    tmp_path,
):
    """tan-cli#487, defect 3, driven through the real CLI subprocess -- the
    relative-cwd semantics this defect turns on need a REAL process cwd, not
    just the `cwd=` string `_run` takes as a parameter (`os.path.isfile` has
    no idea about that parameter). `run_flash`'s own harness already passes
    a relative `--sdk-root ./sdk`, matching the issue's own 'ordinary-
    looking --sdk-root ../alp-sdk layout' common case.

    The DANGEROUS shape: a west workspace topdir DIFFERENT from the project
    root, holding a STALE same-named artefact. Before this fix, `_execute`
    would spawn with `cwd=<topdir>` while tan validated the artefact
    relative to `work` -- so the topdir's STALE file is what a real write
    would reach. Proven here via `--dry-run`'s own preview message, which
    embeds the resolved artefact path directly in the `yocto_wic` argv (`dd
    if=<resolved> of=...`): before the fix the embedded path is the literal
    RELATIVE string `./sdk/images/core-image.wic`, which a child spawned
    from the topdir resolves against the WRONG base; after the fix it is
    absolute, anchored on `work`, the SAME base tan validated it with.

    Fails against the pre-fix source (measured: the message embeds the
    literal relative string, not an absolute path anchored on `work`)."""
    work = tmp_path
    (work / "sdk" / "images").mkdir(parents=True)
    (work / "sdk" / "images" / "core-image.wic").write_bytes(b"real-image-bytes")
    # A west workspace topdir DIFFERENT from `work`, holding a SAME-NAMED but
    # STALE artefact -- proves the dangerous case is a real divergence, not
    # merely a cosmetic relative-path oddity.
    workspace_dir = work / "zephyrproject"
    (workspace_dir / ".west").mkdir(parents=True)
    (workspace_dir / "sdk" / "images").mkdir(parents=True)
    (workspace_dir / "sdk" / "images" / "core-image.wic").write_bytes(b"STALE-bytes")

    manifest = """schema_version: 1
hw_info: {sku: S}
slices:
- {core_id: a55, os: yocto, output_artefact: images/core-image.wic, status: ok,
   flash_method: yocto_wic, flash_args: {target: /dev/sdb}}
helper_mcus: []
boot_order: []
"""
    exit_code, out, _ = run_flash(work, "--format", "json", "--dry-run", manifest=manifest)
    payload = envelope(out)
    assert exit_code == 0
    message = payload["data"]["entries"][0]["message"]
    # `_abs_join` preserves a `.` path component rather than normalising it
    # away (its own docstring: matches Rust's `cwd.join(".")`), so the
    # correctly-anchored path is `<work>/./sdk/images/core-image.wic`, not
    # the collapsed `<work>/sdk/images/core-image.wic` -- what matters here
    # is that it is anchored on `work` AT ALL, which the bare relative
    # `./sdk/images/core-image.wic` (no `work` prefix) that reached the
    # message before this fix was not.
    resolved = os.path.join(os.path.join(str(work), "./sdk"), "images/core-image.wic")
    assert resolved in message, message
    assert f"if={resolved}" in message, message
    assert "if=./sdk/images/core-image.wic" not in message, message


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


def test_flow_d_script_quotes_a_path_containing_a_space(tmp_path):
    """tan-cli#369: SEGGER's J-Link Commander splits an unquoted script line
    on whitespace, so an unquoted `loadbin C:\\Program Files\\...` truncates
    to `C:\\Program` -- a normal Windows SETOOLS install path. `atoc` under a
    directory with a space must be quoted; the no-space `artefact` line
    (proven byte-identical above) must NOT be, so this only widens the
    render, never narrows it."""
    spaced_dir = tmp_path / "Program Files" / "alif" / "setools" / "build"
    spaced_dir.mkdir(parents=True)
    atoc = spaced_dir / "AppTocPackage.bin"
    atoc.write_bytes(b"\x00" * 8)

    plan = plan_alif_mram_jlink(
        flow_d_inputs(atoc=str(atoc), confirm=True), lambda t: t == "JLinkExe"
    )
    script = plan.jlink_script or ""
    assert f'loadbin "{atoc}" 0x8057F5B0' in script, script
    assert f'verifybin "{atoc}" 0x8057F5B0' in script, script
    # The unquoted no-space artefact line is untouched.
    assert "loadbin /build/zephyr/zephyr.bin 0x80010000" in script, script


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


@pytest.mark.parametrize("bad", ["a;b", "../x", "/x", "C:/x", "a b", "dev\nice"])
def test_flow_d_probe_serial_is_charset_guarded(bad):
    """tan-cli#486: `jlink_serial` is interpolated verbatim into a
    `SelectEmuBySN {serial}` J-Link Commander script LINE, the same as
    `jlink_flash_device` -- a newline is a command-injection primitive into a
    process holding SWD write access. `jlink_serial` alone was missed when
    `jlink_flash_device`/`slot0_load_address`/`atoc_address`/`expect_dpidr`
    were guarded; this extends the same guard-class test to cover it."""
    with pytest.raises(FlashPlanError):
        plan_alif_mram_jlink(flow_d_inputs(jlink_serial=bad), lambda t: True)


def test_flow_d_probe_serial_refusal_names_jlink_not_openocd():
    """tan-cli#486 REVIEW, defect 4 (NIT): reusing `validate_identifier`'s
    default `destination` gave a hostile `jlink_serial` a refusal ending "...
    into a spawned command / OpenOCD Tcl script" -- verbatim in a captured
    envelope -- even though `jlink_serial` never reaches an OpenOCD Tcl
    script at all; it reaches a J-Link Commander `SelectEmuBySN` line.
    `validate_identifier`'s `destination` override fixes the text for this
    field without touching `interface`/`target`, which legitimately DO name
    OpenOCD (see `test_flow_d_probe_serial_is_charset_guarded` for the
    refusal itself, and the `interface`/`target` guard tests elsewhere for
    the unchanged sibling wording)."""
    with pytest.raises(FlashPlanError) as raised:
        plan_alif_mram_jlink(flow_d_inputs(jlink_serial="dev\nice"), lambda t: True)
    message = str(raised.value)
    assert "J-Link Commander" in message
    assert "SelectEmuBySN" in message
    assert "OpenOCD" not in message


def test_flow_d_probe_serial_is_optional_and_has_no_default():
    """No default serial: a bench-wide serial can be SHARED by two probes that
    differ only by USB path, so a silent default can select the wrong board."""
    without = plan_alif_mram_jlink(flow_d_inputs(confirm=True), lambda t: True)
    assert "SelectEmuBySN" not in without.jlink_script
    with_serial = plan_alif_mram_jlink(
        flow_d_inputs(confirm=True, jlink_serial="123456789"), lambda t: True
    )
    assert with_serial.jlink_script.startswith("SelectEmuBySN 123456789\n")


def test_flow_d_probe_serial_accepts_a_bare_numeric_value():
    """tan-cli#486: `jlink_serial: 123456789` (unquoted -- the canonical
    SEGGER spelling) used to be silently DISCARDED by the tolerant `fa_str`
    (it only accepts an already-`str` value), dropping the `SelectEmuBySN`
    line with no diagnostic on a bench with more than one probe attached.
    The strict `fa_str_checked` round-trips a bare non-negative integer into
    its decimal string form, exactly like `atoc_address`/`jlink_speed`
    already do for their own bare-integer manifest shapes."""
    plan = plan_alif_mram_jlink(
        flow_d_inputs(confirm=True, jlink_serial=123456789), lambda t: True
    )
    assert plan.jlink_script.startswith("SelectEmuBySN 123456789\n")


@pytest.mark.parametrize("serial", ["801012345", "000440123456", "J-Link-OB_1", "12345678-9"])
def test_flow_d_probe_serial_real_segger_spellings_still_render(serial):
    """tan-cli#486 REVIEW's own regression bar: real SEGGER serial spellings
    must keep rendering a correct `SelectEmuBySN` line after the early-
    validation move (defect 2) and the wording fix (defect 4) above --
    `801012345` (bare numeric), `000440123456` (a leading-zero string,
    quoted in a real manifest), `J-Link-OB_1` (an on-board probe's name),
    and `12345678-9` (a hyphenated form)."""
    plan = plan_alif_mram_jlink(flow_d_inputs(confirm=True, jlink_serial=serial), lambda t: True)
    assert plan.jlink_script.startswith(f"SelectEmuBySN {serial}\n")


def test_flow_d_probe_serial_empty_string_still_opts_out():
    """`jlink_serial: ""` still opts OUT of the `SelectEmuBySN` line entirely
    -- `fa_str_checked` treats an empty string the same as absent, unchanged
    by moving the check into `validate_flow_d_shape` (defect 2)."""
    plan = plan_alif_mram_jlink(flow_d_inputs(confirm=True, jlink_serial=""), lambda t: True)
    assert "SelectEmuBySN" not in plan.jlink_script


def test_flow_d_atoc_path_is_charset_guarded_against_a_newline():
    """tan-cli#486: `commander_path` only wraps a whitespace-bearing path in
    `"..."` -- quoting is not escaping. An embedded newline in `flash_args.
    atoc` still ends the quoted `loadbin`/`verifybin` LINE and starts a new,
    attacker-chosen Commander command. `slot0_load_address` is deliberately
    omitted here (the default single-ATOC-blob shape) so this isolates the
    `atoc` guard from the artefact one, covered separately below."""
    args = {
        "jlink_flash_device": "PART_PROFILE",
        "atoc": "/blobs/AppTocPackage.bin\nerase",
        "atoc_address": "0x8057F5B0",
        "confirm": True,
    }
    with pytest.raises(FlashPlanError):
        plan_alif_mram_jlink(
            FlashInputs(artefact="/build/zephyr/zephyr.bin", flash_args=args, core_id="m", sku="S"),
            lambda t: True,
        )


def test_flow_d_mramxip_artefact_path_is_charset_guarded_against_a_newline():
    """The mramxip shape's `loadbin {artefact} {app_address}` line is
    vulnerable the same way the ATOC line is (tan-cli#486). The newline sits
    ahead of the `.bin` extension so `is_raw_bin`/`resolve_slot0_binary`
    still accept the shape and the injection reaches the real
    Commander-script build rather than an unrelated earlier refusal."""
    args = {**FLOW_D_ARGS, "confirm": True}
    with pytest.raises(FlashPlanError):
        plan_alif_mram_jlink(
            FlashInputs(
                artefact="/build/zephyr/ze\nphyr.bin", flash_args=args, core_id="m", sku="S"
            ),
            lambda t: True,
        )


def test_flow_d_atoc_and_artefact_paths_accept_spaces_after_the_new_guard():
    """A legitimate spaced path must still flash: tan-cli#486's new guard
    rejects only control characters, so a real Windows-style
    `C:\\Program Files\\...`-shaped path (here just a plain space, matching
    `commander_path`'s own quoting tests) must reach the write with the
    SAME conditional quoting as before -- never a refusal."""
    args = {**FLOW_D_ARGS, "atoc": "/blobs/App Toc Package.bin", "confirm": True}
    plan = plan_alif_mram_jlink(
        FlashInputs(
            artefact="/build/zephyr bin/zephyr.bin", flash_args=args, core_id="m", sku="S"
        ),
        lambda t: True,
    )
    assert '"/blobs/App Toc Package.bin"' in plan.jlink_script
    assert '"/build/zephyr bin/zephyr.bin"' in plan.jlink_script


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


def test_flow_d_preflight_serial_is_charset_guarded():
    """tan-cli#486: the injected block this closes would prefix the
    READ-ONLY DPIDR preflight, not just the write -- so the wrong-board
    safety gate (`flash_cmd`'s "the identity is confirmed while the session
    is still read-only" abort) would execute an injected command BEFORE it
    ever gets a chance to abort. Guarded the same way as the write-path
    serial in `plan_alif_mram_jlink`."""
    with pytest.raises(FlashPlanError):
        flash_plan.flow_d_preflight_script(
            flow_d_inputs(
                expect_dpidr="0x4C013477", jlink_device="Generic-Attach", jlink_serial="a;b"
            )
        )


def test_flow_d_preflight_serial_accepts_a_bare_numeric_value():
    """The same numeric-serial round-trip fix as the write path (tan-cli#486),
    proven on the preflight generator too."""
    prepared = flash_plan.flow_d_preflight_script(
        flow_d_inputs(expect_dpidr="0x4C013477", jlink_device="Generic-Attach", jlink_serial=7)
    )
    assert prepared is not None
    script, _ = prepared
    assert script.startswith("SelectEmuBySN 7\n")


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
    assert "Check the wiring and which board is physically attached" in message
    assert "re-enumerat" not in message


def test_flow_d_preflight_wrong_dp_id_names_the_actual_id_too(monkeypatch):
    """tan-cli#512, secondary. The mismatch refusal used to name only the
    EXPECTED SW-DP IDR, never the actual one the preflight just read --
    although it took this exact `_dp_id_reported(banner)` branch and
    therefore had the value in hand. On a bench where two probes share a
    cloned USB serial (measured: `603000869` answers both a real E1M-AEN801
    at `0x4C013477` and a GD32 bridge at `0x0BE12477`), the actual ID is the
    single most useful datum for telling which board actually answered."""
    _stub_flow_d_probe(
        monkeypatch,
        stdout="Connecting to target via SWD\nFound SW-DP with ID 0x2BA01477\n",
    )
    message = flash_cmd._flow_d_preflight(_flow_d_preflight_inputs())
    assert message is not None
    assert "0x4C013477" in message, message  # the expected id (unchanged)
    assert "0x2BA01477" in message, message  # tan-cli#512: the actual id, new


def test_flow_d_preflight_wrong_dp_id_names_the_sw_dp_id_not_jlink_serial(monkeypatch):
    """tan-cli#369: the wrong-DP-ID remediation used to read as "pin
    jlink_serial to fix this" -- wrong on the bench this preflight actually
    caught a mismatch on, where a cloned/shared USB serial made jlink_serial
    ambiguous between two physical probes. The remediation must name the
    SW-DP ID as the real discriminator and say plainly that a shared/cloned
    serial cannot be disambiguated by jlink_serial alone."""
    _stub_flow_d_probe(
        monkeypatch,
        stdout="Connecting to target via SWD\nFound SW-DP with ID 0x2BA01477\n",
    )
    message = flash_cmd._flow_d_preflight(_flow_d_preflight_inputs())
    assert message is not None
    assert "SW-DP ID is the real" in message
    assert "cannot disambiguate" in message
    assert "CLONED serial" in message
    assert "jlink_serial" in message


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
    wiring is fine, so this keeps the original sentence.

    **tan-cli#373**: no DP ID was reported here, so this must get the
    ORIGINAL probe-selection/`jlink_serial` sentence -- not #369's
    cloned-serial text, which only applies when a board DID answer with a
    different ID (the wrong-DP-ID test below covers that one)."""
    _stub_flow_d_probe(monkeypatch, stdout="some unrecognised probe banner\n")
    message = flash_cmd._flow_d_preflight(_flow_d_preflight_inputs())
    assert message is not None
    assert "Check the probe selection (flash_args.jlink_serial) and the wiring" in message
    assert "re-enumerat" not in message
    assert "CLONED serial" not in message


def test_flow_d_preflight_a_target_level_cannot_connect_keeps_the_wiring_message(monkeypatch):
    """tan-cli#312 review finding: an unplugged SWD ribbon / no board present
    produces "Cannot connect to target." -- a genuine wiring problem, not a
    re-enumerating probe. This must NOT get the "not a wiring... problem"
    re-enumeration message: on a bench that would turn a real unplugged cable
    into an infinite wait-and-retry loop instead of the correct remediation.

    **tan-cli#373**: no DP ID was reported here either, so the ORIGINAL
    probe-selection sentence is correct, not the cloned-serial text -- #369's
    rewrite gave this banner the cloned-serial message too, which never
    applies (no ID was even read to compare)."""
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
    assert "CLONED serial" not in message


def test_flow_d_preflight_a_wrong_jlink_serial_keeps_the_wiring_message(monkeypatch):
    """tan-cli#312 review finding: a probe that IS reachable via USB but
    refuses the requested `flash_args.jlink_serial` prints "Cannot connect to
    J-Link." -- a real probe-selection problem, so this keeps the original
    wiring/`jlink_serial` remediation rather than the re-enumeration message.

    **tan-cli#373**: this is the regression #367's own review pattern warned
    about -- this test's docstring already promised the ORIGINAL wiring/
    `jlink_serial` sentence, but its body asserted a phrase that actually
    belongs to #369's cloned-serial rewrite (both messages happen to share
    the words "Check the wiring...physically attached"). No DP ID was
    reported here, so the ORIGINAL sentence -- naming `jlink_serial`
    explicitly as the fix on a multi-probe host -- is correct; the
    cloned-serial text does not apply."""
    _stub_flow_d_probe(
        monkeypatch,
        stdout="Connecting to J-Link via USB...FAILED: Cannot connect to J-Link.\n",
        success=False,
    )
    message = flash_cmd._flow_d_preflight(_flow_d_preflight_inputs())
    assert message is not None
    assert "Check the probe selection (flash_args.jlink_serial) and the wiring" in message
    assert "tan-cli#353" in message
    assert "re-enumerat" not in message
    assert "CLONED serial" not in message


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


# ── tan-cli#353's remaining half: SETOOLS integration for the AEN801 slot0
# flash. The alp-sdk manifest measured on real silicon (e1m-aen-evk-01, E8
# AE822) emits ONLY `flash_args.jlink_flash_device` -- no `atoc`/`atoc_map`/
# `atoc_address` at all -- so a customer used to hit `plan_alif_mram_jlink`'s
# bare "both required" refusal with no path from there to a working flash.
# These three prove the maintainer's minimum bar: (a) a resolved SETOOLS path
# signs for real and the derived `atoc_address` reaches the actual
# `loadbin`/`verifybin` pair; (b) an unresolved one refuses with the SETOOLS
# guidance, not the bare field error; (c) `--dry-run` signs nothing.


def _setools_script_name() -> str:
    """`.bat` on Windows -- a batch-content file needs the extension to be
    directly spawnable via `subprocess.run(..., shell=False)` (measured:
    an extension-less same-content file fails with WinError 193) -- the real
    bare `app-gen-toc` name (`tan.core.setools.APP_GEN_TOC`) everywhere else,
    where a POSIX shebang script IS spawnable extension-less."""
    return "app-gen-toc.bat" if os.name == "nt" else "app-gen-toc"


def _write_working_app_gen_toc(dest: Path, address: str = "0x8057ea50") -> str:
    """A fake `app-gen-toc` that writes a real `build/app-package-map.txt` +
    `build/AppTocPackage.bin` under its OWN cwd and exits 0 -- proves the
    WIRING (`tan.core.setools.sign_slot0`'s own tests cover the failure
    shapes), never a real SETOOLS (license-gated, not redistributed, and not
    needed to prove this)."""
    if os.name == "nt":
        dest.write_text(
            "@echo off\r\n"
            "if not exist build mkdir build\r\n"
            f">build\\app-package-map.txt echo APP Package Start Address: {address}\r\n"
            "echo fake-atoc-bytes> build\\AppTocPackage.bin\r\n"
            "exit /b 0\r\n",
            encoding="utf-8",
        )
    else:
        dest.write_text(
            "#!/bin/sh\n"
            "mkdir -p build\n"
            f'printf "APP Package Start Address: {address}\\n" > build/app-package-map.txt\n'
            'printf "fake-atoc-bytes\\n" > build/AppTocPackage.bin\n'
            "exit 0\n",
            encoding="utf-8",
        )
        os.chmod(dest, 0o755)
    return str(dest)


def test_flow_d_setools_signs_when_the_manifest_supplies_nothing_signing_related(
    tmp_path, monkeypatch
):
    """(a) A manifest carrying ONLY `jlink_flash_device` + `slot0_load_address`
    -- alp-sdk's real current AEN801 emit plus the one key tan cannot derive,
    measured -- gets a REAL SETOOLS sign when `flash_args.setools_dir`
    resolves AND the run is CONFIRMED (tan-cli#487: `confirm=True` here --
    see `test_flow_d_setools_does_not_sign_when_the_run_is_not_confirmed`
    below for the sibling case this same fixture proves does NOT sign), and
    the DERIVED `atoc_address` reaches `plan_alif_mram_jlink`'s actual
    `loadbin`/`verifybin` pair -- not just `_resolve_flow_d_atoc_via_setools`'s
    own return value."""
    from tan.commands.flash_cmd import _Context, _is_file, _resolve_flow_d_atoc_via_setools
    from tan.core import setools as setools_module
    from tan.core.flash_plan import validate_flow_d_shape

    setools_dir = tmp_path / "setools"
    setools_dir.mkdir()
    name = _setools_script_name()
    if name != setools_module.APP_GEN_TOC:
        # `find_app_gen_toc`'s OWN lookup runs unmodified below -- only the
        # name it looks for changes, to the one filename THIS host can
        # actually spawn (see `_setools_script_name`'s own docstring).
        monkeypatch.setattr(setools_module, "APP_GEN_TOC", name)
    script = _write_working_app_gen_toc(setools_dir / name)

    build_root = tmp_path / "build"
    build_root.mkdir()
    artefact = build_root / "zephyr.bin"
    artefact.write_bytes(b"\x50\x42\x00\x20" + b"\x00" * 64)

    flash_args = {
        "jlink_flash_device": "PART_PROFILE",
        "slot0_load_address": "0x80010000",
        "setools_dir": str(setools_dir),
    }
    ctx = _Context(
        sku="S",
        build_root=str(build_root),
        sdk_root=str(tmp_path),
        dry_run=False,
        skip_missing_tools=False,
        force_confirm=False,
        capture=True,
    )
    shape = validate_flow_d_shape(flash_args, str(artefact), _is_file)
    merged, note = _resolve_flow_d_atoc_via_setools(flash_args, shape, ctx, "m55_he", True)

    # tan-cli#373: a real (non-dry-run) sign now returns an informational
    # NOTE (not `None`) naming which SETOOLS install actually signed --
    # `setools.source` used to reach a customer only via a FAILURE message.
    # `flash_args.setools_dir` is what resolved it here, so its OWN value
    # names the source, matching `resolve_setools_dir`'s own precedence text.
    assert note is not None
    assert str(setools_dir) in note
    assert "flash_args.setools_dir" in note
    assert merged["atoc_address"] == "0x8057ea50"
    assert Path(merged["atoc"]).is_file()
    assert Path(script).is_file()  # the fake tool itself was never deleted/moved

    plan = plan_alif_mram_jlink(
        FlashInputs(artefact=str(artefact), flash_args=merged, core_id="m55_he", sku="S"),
        lambda _t: True,
    )
    script_text = plan.jlink_script or ""
    assert f"loadbin {merged['atoc']} 0x8057ea50" in script_text, script_text
    assert f"verifybin {merged['atoc']} 0x8057ea50" in script_text, script_text


def test_flow_d_setools_does_not_sign_when_the_run_is_not_confirmed(tmp_path, monkeypatch):
    """tan-cli#487, defect 5. Identical setup to (a) above -- a working fake
    `app-gen-toc`, a manifest with only `jlink_flash_device` + `slot0_load_
    address` -- but `confirm=False` on a non-dry-run `ctx` (a plain `tan
    flash`: not `--dry-run`, no `flash_args.confirm`, no `ALP_FLASH_FORCE`).

    Before this fix `_resolve_flow_d_atoc_via_setools` gated its real sign on
    `ctx.dry_run` ALONE, so this exact call still spawned `app-gen-toc` for
    real -- into the customer's SETOOLS install, on a run that goes on to
    refuse the MRAM write it was signing for. Proven two ways, mirroring (a)'s
    own structure: the returned `note` PREVIEWS rather than reports a
    completed sign, and none of the real sign's side effects
    (`build/AppTocPackage.bin`, `build/config/`, the appended `build/app-
    package-map.txt`) exist afterwards."""
    from tan.commands.flash_cmd import _Context, _is_file, _resolve_flow_d_atoc_via_setools
    from tan.core import setools as setools_module
    from tan.core.flash_plan import validate_flow_d_shape

    setools_dir = tmp_path / "setools"
    setools_dir.mkdir()
    name = _setools_script_name()
    if name != setools_module.APP_GEN_TOC:
        monkeypatch.setattr(setools_module, "APP_GEN_TOC", name)
    _write_working_app_gen_toc(setools_dir / name)

    build_root = tmp_path / "build"
    build_root.mkdir()
    artefact = build_root / "zephyr.bin"
    artefact.write_bytes(b"\x50\x42\x00\x20" + b"\x00" * 64)

    flash_args = {
        "jlink_flash_device": "PART_PROFILE",
        "slot0_load_address": "0x80010000",
        "setools_dir": str(setools_dir),
    }
    ctx = _Context(
        sku="S",
        build_root=str(build_root),
        sdk_root=str(tmp_path),
        dry_run=False,
        skip_missing_tools=False,
        force_confirm=False,
        capture=True,
    )
    shape = validate_flow_d_shape(flash_args, str(artefact), _is_file)
    merged, note = _resolve_flow_d_atoc_via_setools(flash_args, shape, ctx, "m55_he", False)

    assert note is not None
    assert "would sign" in note
    assert "flash_args.confirm is false" in note
    assert "atoc" not in merged
    assert "atoc_address" not in merged
    assert not (setools_dir / "build" / "AppTocPackage.bin").exists()
    assert not (setools_dir / "build" / "config").exists()
    assert not (setools_dir / "build" / "app-package-map.txt").exists()


def test_flow_d_end_to_end_does_not_sign_via_setools_when_unconfirmed(tmp_path, monkeypatch):
    """tan-cli#487, defect 5, driven through `_run` (the real CLI entry point
    below argument parsing, the same seam
    `test_flow_d_atoc_is_resolved_against_build_root_not_the_spawn_cwd` uses
    for a confirmed Flow D write). A fresh AEN801-shaped manifest -- the
    exact real-silicon shape the ticket measures, `jlink_flash_device` +
    `slot0_load_address` only -- with a resolving `--setools-dir`, NO
    `--dry-run`, no `flash_args.confirm`, no `ALP_FLASH_FORCE` must NOT spawn
    `app-gen-toc`: `tan.core.setools.subprocess.run` (the ACTUAL spawn site
    `sign_slot0` uses, a different module than `flash_cmd`'s own) is
    monkeypatched to raise if called at all, so a regression back to the
    pre-fix `ctx.dry_run`-only gate fails LOUDLY here rather than merely
    leaving a stray file somewhere this assertion forgot to check."""
    from tan.commands import flash_cmd
    from tan.core import setools as setools_module

    setools_dir = tmp_path / "setools"
    setools_dir.mkdir()
    name = _setools_script_name()
    if name != setools_module.APP_GEN_TOC:
        monkeypatch.setattr(setools_module, "APP_GEN_TOC", name)
    _write_working_app_gen_toc(setools_dir / name)

    (tmp_path / "sdk" / "scripts").mkdir(parents=True)
    (tmp_path / "sdk" / "scripts" / "alp_project.py").write_text("", encoding="utf-8")
    (tmp_path / "build").mkdir()
    (tmp_path / "build" / "zephyr.bin").write_bytes(b"\x50\x42\x00\x20" + b"\x00" * 64)
    manifest = """schema_version: 1
hw_info: {sku: S}
slices:
- {core_id: m55_he, os: zephyr, output_artefact: zephyr.bin, status: ok,
   flash_method: zephyr_west_flash,
   flash_args: {jlink_flash_device: PART_PROFILE, slot0_load_address: "0x80010000"}}
helper_mcus: []
boot_order: []
"""
    (tmp_path / "build" / "system-manifest.yaml").write_text(
        manifest, encoding="utf-8", newline=""
    )

    fake_tools = tmp_path / "faketools"
    fake_tools.mkdir()
    jlink_path = fake_tools / ("JLinkExe.exe" if os.name == "nt" else "JLinkExe")
    jlink_path.write_text("", encoding="utf-8")
    if os.name != "nt":
        os.chmod(jlink_path, 0o755)
    monkeypatch.setenv("PATH", str(fake_tools))
    monkeypatch.setenv("SETOOLS_DIR", str(setools_dir))
    monkeypatch.delenv("ALP_FLASH_FORCE", raising=False)
    monkeypatch.setattr(flash_cmd, "venv_bin_dir", lambda *_a, **_k: None)

    def _fail_if_spawned(*_a, **_k):
        raise AssertionError("app-gen-toc was spawned on an unconfirmed run")

    monkeypatch.setattr(setools_module.subprocess, "run", _fail_if_spawned)

    exit_code, data, issues, _lines, _sdk = flash_cmd._run(
        app_path=".", build_root_arg=None, sdk_root_arg=str(tmp_path / "sdk"),
        board_yaml=None, core=None, helper=None, dry_run=False,
        skip_missing_tools=False, capture=True, cwd=str(tmp_path),
    )
    assert exit_code == 0
    entry = data["entries"][0]
    assert entry["status"] == "planned"
    assert "would sign" in entry["message"]
    assert "flash_args.confirm is false" in entry["message"]
    assert any(i.code == "flash.confirm-required" for i in issues)
    assert not (setools_dir / "build" / "AppTocPackage.bin").exists()
    assert not (setools_dir / "build" / "config").exists()


def test_flow_d_wrong_board_refuses_before_any_setools_write(tmp_path, monkeypatch):
    """tan-cli#512. A CONFIRMED, non-dry-run Flow D entry whose read-only
    DPIDR preflight catches a wrong-board mismatch must abort BEFORE the
    SETOOLS auto-sign ever touches the customer's install -- not merely
    before the MRAM write itself.

    Measured on real E1M-AEN801 silicon: a manifest with `expect_dpidr` set
    to the GD32 bridge's DP ID (deliberately the wrong board) correctly
    aborted the MRAM write with slot0 byte-identical -- but
    `_resolve_flow_d_atoc_via_setools` had already run FIRST and rewrote
    `build/app-package-map.txt` (1141 -> 689 bytes), regenerated `build/
    AppTocPackage.bin`, and created `build/images/m55_he.bin` + `build/
    config/m55_he-slot0.json`, destroying the prior accumulated (hand-run-
    inclusive) sign record. `_flow_d_preflight` moving ahead of the sign is
    the fix under test.

    `tan.core.setools.subprocess.run` -- `sign_slot0`'s own real spawn site
    -- is stubbed to raise if it is EVER called: a regression back to the
    pre-fix ordering (sign runs before the preflight) fails LOUDLY here,
    mirroring `test_flow_d_end_to_end_does_not_sign_via_setools_when_
    unconfirmed`'s own technique for the sibling (unconfirmed) case.
    `flash_cmd._spawn_jlink` -- the one spawn site the preflight probe and
    the eventual real write share -- is stubbed to a canned "a different
    board answered" banner; nothing here can reach a real J-Link either way.

    Verified to fail against the pre-fix code (preflight after the sign):
    with the ordering reverted, `_resolve_flow_d_atoc_via_setools` reaches
    `sign_slot0` before the preflight ever runs, tripping the
    `AssertionError` stub above -- the exact regression this guards.
    """
    from tan.core import setools as setools_module

    setools_dir = tmp_path / "setools"
    setools_dir.mkdir()
    name = _setools_script_name()
    if name != setools_module.APP_GEN_TOC:
        monkeypatch.setattr(setools_module, "APP_GEN_TOC", name)
    _write_working_app_gen_toc(setools_dir / name)

    build_root = tmp_path / "build"
    build_root.mkdir()
    (build_root / "zephyr.bin").write_bytes(b"\x50\x42\x00\x20" + b"\x00" * 64)

    (tmp_path / "sdk" / "scripts").mkdir(parents=True)
    (tmp_path / "sdk" / "scripts" / "alp_project.py").write_text("", encoding="utf-8")

    manifest = f"""schema_version: 1
hw_info: {{sku: S}}
slices:
- {{core_id: m55_he, os: zephyr, output_artefact: zephyr.bin, status: ok,
   flash_method: alif_mram_jlink,
   flash_args: {{jlink_flash_device: PART_PROFILE, slot0_load_address: "0x80010000",
                expect_dpidr: "0x0BE12477", jlink_device: Generic-Attach,
                setools_dir: "{setools_dir.as_posix()}", confirm: true}}}}
helper_mcus: []
boot_order: []
"""
    (build_root / "system-manifest.yaml").write_text(manifest, encoding="utf-8", newline="")

    fake_tools = tmp_path / "faketools"
    fake_tools.mkdir()
    jlink_path = fake_tools / ("JLinkExe.exe" if os.name == "nt" else "JLinkExe")
    jlink_path.write_text("", encoding="utf-8")
    if os.name != "nt":
        os.chmod(jlink_path, 0o755)
    monkeypatch.setenv("PATH", str(fake_tools))
    monkeypatch.delenv("ZEPHYR_BASE", raising=False)
    monkeypatch.setattr(flash_cmd, "venv_bin_dir", lambda *_a, **_k: None)

    monkeypatch.setattr(
        flash_cmd,
        "_spawn_jlink",
        lambda *_a, **_k: flash_cmd._Outcome(
            success=False,
            stdout="Connecting to target via SWD\nFound SW-DP with ID 0x4C013477\n",
            stderr="",
        ),
    )

    def _fail_if_spawned(*_a, **_k):
        raise AssertionError(
            "app-gen-toc was spawned before the DPIDR preflight refused a wrong board"
        )

    monkeypatch.setattr(setools_module.subprocess, "run", _fail_if_spawned)

    exit_code, data, issues, _lines, _sdk = flash_cmd._run(
        app_path=".", build_root_arg=None, sdk_root_arg=str(tmp_path / "sdk"),
        board_yaml=None, core=None, helper=None, dry_run=False,
        skip_missing_tools=False, capture=True, cwd=str(tmp_path),
    )

    assert exit_code == 1
    entry = data["entries"][0]
    assert entry["status"] == "failed"
    assert "expected SW-DP IDR 0x0BE12477" in entry["message"], entry["message"]
    # tan-cli#512, secondary: the ACTUAL reported ID must reach the message too.
    assert "0x4C013477" in entry["message"], entry["message"]
    assert any(i.code == "flash.entry-failed" for i in issues)

    # The core assertion: nothing was written into the SETOOLS install.
    assert not (setools_dir / "build" / "AppTocPackage.bin").exists()
    assert not (setools_dir / "build" / "config").exists()
    assert not (setools_dir / "build" / "app-package-map.txt").exists()


def test_flow_d_end_to_end_refuses_with_setools_guidance_when_unresolved(tmp_path):
    """(b) The FIRST failure the ticket measures on real silicon: a fresh
    AEN801 manifest carrying only `jlink_flash_device`, no `SETOOLS_DIR` and
    no `flash_args.setools_dir` anywhere. Must surface the SETOOLS guidance
    refusal -- naming that a signed ATOC is needed, that SETOOLS is
    license-gated, and how to point tan at it -- not
    `plan_alif_mram_jlink`'s bare 'flash_args.atoc ... required' field
    message. `--dry-run`: the SAME reason every other CLI-level Flow D
    refusal test above uses it -- it bypasses the JLinkExe PATH gate, which
    is not what this test is about."""
    manifest = """schema_version: 1
hw_info: {sku: S}
slices:
- {core_id: m55_he, os: zephyr, output_artefact: zephyr.bin, status: ok,
   flash_method: zephyr_west_flash,
   flash_args: {jlink_flash_device: AE822FA0E5597LS0_M55_HE}}
helper_mcus: []
boot_order: []
"""
    exit_code, out, _ = run_flash(
        tmp_path, "--format", "json", "--dry-run", manifest=manifest,
        env={"SETOOLS_DIR": ""},
    )
    payload = envelope(out)
    assert exit_code == 1
    entry = payload["data"]["entries"][0]
    assert entry["status"] == "failed"
    assert "SETOOLS" in entry["message"]
    assert "license-gated" in entry["message"]
    assert "--setools-dir" in entry["message"]
    assert "SETOOLS_DIR=" in entry["message"]
    assert "flash_args.setools_dir" in entry["message"]
    # NOT the old bare field message a customer has never heard of app-gen-toc
    # from.
    assert "both required" not in entry["message"]
    assert codes(payload) == ["flash.entry-failed"]


def test_flow_d_setools_dir_precedence_is_flag_then_env_then_manifest(tmp_path):
    """tan-cli#368's acceptance criterion: with all three sources set to
    DIFFERENT (all app-gen-toc-less) directories, the flag wins; with only
    the environment and the manifest set, the environment wins. Proven via
    `missing_tool_message`'s own source-naming (`setools.source`), not a real
    sign -- none of the three directories holds a working `app-gen-toc`, so
    `tan flash` always refuses, but WHICH directory (and which source phrase)
    it names proves which one actually resolved."""
    manifest_dir = tmp_path / "from-manifest"
    env_dir = tmp_path / "from-env"
    flag_dir = tmp_path / "from-flag"
    for d in (manifest_dir, env_dir, flag_dir):
        d.mkdir()

    manifest = f"""schema_version: 1
hw_info: {{sku: S}}
slices:
- {{core_id: m55_he, os: zephyr, output_artefact: zephyr.bin, status: ok,
   flash_method: zephyr_west_flash,
   flash_args: {{jlink_flash_device: PART_PROFILE,
                setools_dir: "{manifest_dir.as_posix()}"}}}}
helper_mcus: []
boot_order: []
"""
    # All three set -> the flag wins.
    exit_code, out, _ = run_flash(
        tmp_path, "--format", "json", "--dry-run", "--setools-dir", str(flag_dir),
        manifest=manifest, env={"SETOOLS_DIR": str(env_dir)},
    )
    payload = envelope(out)
    entry = payload["data"]["entries"][0]
    assert exit_code == 1
    assert "the --setools-dir flag" in entry["message"]
    assert str(flag_dir) in entry["message"]
    assert str(env_dir) not in entry["message"]
    assert str(manifest_dir) not in entry["message"]

    # No flag -> the environment beats the manifest.
    exit_code, out, _ = run_flash(
        tmp_path, "--format", "json", "--dry-run",
        manifest=manifest, env={"SETOOLS_DIR": str(env_dir)},
    )
    payload = envelope(out)
    entry = payload["data"]["entries"][0]
    assert exit_code == 1
    assert "the SETOOLS_DIR environment variable" in entry["message"]
    assert str(env_dir) in entry["message"]
    assert str(manifest_dir) not in entry["message"]


def test_flow_d_dry_run_signs_nothing_via_setools(tmp_path):
    """(c) `--dry-run` must NOT invoke `app-gen-toc`, even though SETOOLS
    fully resolves here -- planning only. Proven two ways: the entry reports
    a WOULD-sign preview (`status: ok`, not `planned`/`failed`), and nothing
    a real sign would produce (`build/AppTocPackage.bin`, `build/config/`)
    exists afterwards -- if `--dry-run` ever DID invoke the fake tool below,
    it would either fail loudly (the file has no execute bit on POSIX) or, on
    a host where it somehow ran, leave exactly the files these assertions
    check for."""
    setools_dir = tmp_path / "setools"
    setools_dir.mkdir()
    # Present, but NEVER executed under --dry-run -- a real script would prove
    # nothing extra here (see (a) above for that), so the placeholder is
    # deliberately not spawnable at all (posix: no execute bit).
    (setools_dir / "app-gen-toc").write_text("", encoding="utf-8")

    manifest = """schema_version: 1
hw_info: {sku: S}
slices:
- {core_id: m55_he, os: zephyr, output_artefact: zephyr.bin, status: ok,
   flash_method: zephyr_west_flash,
   flash_args: {jlink_flash_device: PART_PROFILE, slot0_load_address: "0x80010000"}}
helper_mcus: []
boot_order: []
"""
    (tmp_path / "build").mkdir(exist_ok=True)
    (tmp_path / "build" / "zephyr.bin").write_bytes(b"\x50\x42\x00\x20" + b"\x00" * 64)
    exit_code, out, _ = run_flash(
        tmp_path, "--format", "json", "--dry-run", manifest=manifest,
        env={"SETOOLS_DIR": str(setools_dir)},
    )
    payload = envelope(out)
    assert exit_code == 0
    entry = payload["data"]["entries"][0]
    assert entry["status"] == "ok"
    assert "would sign" in entry["message"]
    assert "app-gen-toc" in entry["message"]
    assert not payload["issues"], payload["issues"]
    # The real signing side effects a live run would produce -- absent.
    assert not (setools_dir / "build" / "AppTocPackage.bin").exists()
    assert not (setools_dir / "build" / "config").exists()


def test_flow_d_dry_run_with_setools_still_surfaces_a_half_armed_preflight(tmp_path):
    """#366: the SETOOLS auto-sign preview used to return BEFORE `meta.build`
    and BEFORE `validate_flow_d_preflight_args` ever ran, so a `--dry-run`
    whose `SETOOLS_DIR` happens to resolve reported `ok:true` / exit 0 for a
    manifest that would refuse a real (or SETOOLS-less) run outright -- a
    disarmed SW-DP IDR guard passing a dry run. Same half-armed
    `expect_dpidr`/`jlink_device` pair
    `test_flow_d_dry_run_surfaces_a_half_armed_preflight_as_a_failure` proves
    for the non-SETOOLS (`atoc` already supplied) path; this is the SETOOLS
    path that bug actually lived in -- `SETOOLS_DIR` resolves, `app-gen-toc`
    is found, and the manifest supplies neither `atoc` nor `atoc_map`."""
    setools_dir = tmp_path / "setools"
    setools_dir.mkdir()
    # Present, but must never be reached -- validation has to fail BEFORE any
    # SETOOLS spawn is even considered.
    (setools_dir / "app-gen-toc").write_text("", encoding="utf-8")

    manifest = """schema_version: 1
hw_info: {sku: S}
slices:
- {core_id: m55_he, os: zephyr, output_artefact: zephyr.bin, status: ok,
   flash_method: zephyr_west_flash,
   flash_args: {jlink_flash_device: PART_PROFILE, slot0_load_address: "0x80010000",
                expect_dpidr: "0x4C013477"}}
helper_mcus: []
boot_order: []
"""
    (tmp_path / "build").mkdir(exist_ok=True)
    (tmp_path / "build" / "zephyr.bin").write_bytes(b"\x50\x42\x00\x20" + b"\x00" * 64)
    exit_code, out, _ = run_flash(
        tmp_path, "--format", "json", "--dry-run", manifest=manifest,
        env={"SETOOLS_DIR": str(setools_dir)},
    )
    payload = envelope(out)
    assert exit_code == 1
    entry = payload["data"]["entries"][0]
    assert entry["status"] == "failed"
    assert "expect_dpidr" in entry["message"]
    assert "jlink_device" in entry["message"]
    # Never got far enough to preview a sign.
    assert "would sign" not in entry["message"]
    codes = {issue["code"] for issue in payload["issues"]}
    assert "flash.entry-failed" in codes


def test_flow_d_dry_run_with_setools_still_surfaces_a_malformed_jlink_speed(tmp_path):
    """tan-cli#373: #366 moved `jlink_flash_device`/`slot0_load_address`
    validation ahead of the SETOOLS preview short-circuit, but not
    `jlink_speed` -- that stayed checked only deep inside
    `plan_alif_mram_jlink`, unreachable from the preview return exactly like
    the `expect_dpidr`/`jlink_device` case above. Measured: `--dry-run` on
    this manifest used to report `ok:true` / exit 0 despite `jlink_speed`
    being a quoted string, and on a REAL run the SETOOLS auto-sign (writing
    into the customer's install) would have happened before this refusal was
    ever reached. `jlink_speed` is hoisted into `validate_flow_d_shape`
    (shared by both the preview and `plan_alif_mram_jlink` itself), so it now
    surfaces here too, before any SETOOLS spawn is even considered -- same
    shape as the `expect_dpidr` fix above, different field."""
    setools_dir = tmp_path / "setools"
    setools_dir.mkdir()
    # Present, but must never be reached -- validation has to fail BEFORE any
    # SETOOLS spawn is even considered.
    (setools_dir / "app-gen-toc").write_text("", encoding="utf-8")

    manifest = """schema_version: 1
hw_info: {sku: S}
slices:
- {core_id: m55_he, os: zephyr, output_artefact: zephyr.bin, status: ok,
   flash_method: zephyr_west_flash,
   flash_args: {jlink_flash_device: PART_PROFILE, slot0_load_address: "0x80010000",
                jlink_speed: "fast"}}
helper_mcus: []
boot_order: []
"""
    (tmp_path / "build").mkdir(exist_ok=True)
    (tmp_path / "build" / "zephyr.bin").write_bytes(b"\x50\x42\x00\x20" + b"\x00" * 64)
    exit_code, out, _ = run_flash(
        tmp_path, "--format", "json", "--dry-run", manifest=manifest,
        env={"SETOOLS_DIR": str(setools_dir)},
    )
    payload = envelope(out)
    assert exit_code == 1
    entry = payload["data"]["entries"][0]
    assert entry["status"] == "failed"
    assert "flash_args.jlink_speed" in entry["message"]
    assert "bare number" in entry["message"]
    # Never got far enough to preview a sign.
    assert "would sign" not in entry["message"]
    codes = {issue["code"] for issue in payload["issues"]}
    assert "flash.entry-failed" in codes


def test_flow_d_dry_run_with_setools_still_surfaces_a_hostile_jlink_serial(tmp_path):
    """tan-cli#486 REVIEW, defect 2: `jlink_serial` lived only inside
    `plan_alif_mram_jlink`/`flow_d_preflight_script`, never in
    `validate_flow_d_shape` -- the "everything checkable early" half #366/
    #373 created for exactly this. Measured: a Flow D entry with
    `jlink_serial: "1234\\nerase\\nw4 0x50000000 0xDEADBEEF"`, no `atoc`/
    `atoc_map`/`atoc_address`, and a resolving `--setools-dir` used to report
    `ok:true`/exit 0 from `tan flash --dry-run` -- and on a real (confirmed)
    run `_resolve_flow_d_atoc_via_setools` spawns `app-gen-toc` into the
    customer's SETOOLS install BEFORE `plan_alif_mram_jlink` ever gets a
    chance to refuse the serial. Same shape as the sibling
    `jlink_speed`/`expect_dpidr` cases just above -- `jlink_serial` is now
    hoisted into `validate_flow_d_shape` alongside them, so the dry-run and a
    real run agree: both refuse before any SETOOLS spawn is even
    considered."""
    setools_dir = tmp_path / "setools"
    setools_dir.mkdir()
    # Present, but must never be reached -- validation has to fail BEFORE any
    # SETOOLS spawn is even considered.
    (setools_dir / "app-gen-toc").write_text("", encoding="utf-8")

    manifest = """schema_version: 1
hw_info: {sku: S}
slices:
- {core_id: m55_he, os: zephyr, output_artefact: zephyr.bin, status: ok,
   flash_method: zephyr_west_flash,
   flash_args: {jlink_flash_device: PART_PROFILE, slot0_load_address: "0x80010000",
                jlink_serial: "1234\\nerase\\nw4 0x50000000 0xDEADBEEF"}}
helper_mcus: []
boot_order: []
"""
    (tmp_path / "build").mkdir(exist_ok=True)
    (tmp_path / "build" / "zephyr.bin").write_bytes(b"\x50\x42\x00\x20" + b"\x00" * 64)
    exit_code, out, _ = run_flash(
        tmp_path, "--format", "json", "--dry-run", manifest=manifest,
        env={"SETOOLS_DIR": str(setools_dir)},
    )
    payload = envelope(out)
    assert exit_code == 1
    entry = payload["data"]["entries"][0]
    assert entry["status"] == "failed"
    assert "flash_args.jlink_serial" in entry["message"]
    # Never got far enough to preview a sign.
    assert "would sign" not in entry["message"]
    codes = {issue["code"] for issue in payload["issues"]}
    assert "flash.entry-failed" in codes


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


def test_a_no_extension_artefact_resolves_to_its_sibling_bin(tmp_path):
    """tan-cli#373 (item 4): #367(a) named THREE "plausibly ELF" shapes -- no
    extension, `.elf`, `.out` -- but `is_elf_artefact` only ever implemented
    the first of the three, silently narrowing #367's own decision. A
    toolchain that names its ELF output bare (`app`, no suffix) must resolve
    to its same-stem sibling `.bin` exactly like `zephyr.elf` does."""
    from tan.core.flash_plan import plan_alif_mram_jlink

    (tmp_path / "app").write_bytes(b"\x7fELF" + b"\x00" * 64)
    (tmp_path / "app.bin").write_bytes(b"\x50\x42\x00\x20" + b"\x00" * 64)

    plan = plan_alif_mram_jlink(_mramxip_inputs(tmp_path, "app"), lambda _t: True)
    script = plan.jlink_script or ""
    assert "app.bin 0x80010000" in script, script


def test_an_out_artefact_resolves_to_its_sibling_bin(tmp_path):
    """tan-cli#373 (item 4): the second of #367(a)'s three named shapes --
    `.out` -- must also resolve, not just `.elf`."""
    from tan.core.flash_plan import plan_alif_mram_jlink

    (tmp_path / "zephyr.out").write_bytes(b"\x7fELF" + b"\x00" * 64)
    (tmp_path / "zephyr.bin").write_bytes(b"\x50\x42\x00\x20" + b"\x00" * 64)

    plan = plan_alif_mram_jlink(_mramxip_inputs(tmp_path, "zephyr.out"), lambda _t: True)
    script = plan.jlink_script or ""
    assert "zephyr.bin 0x80010000" in script, script
    assert "zephyr.out" not in script, script


def test_a_hex_artefact_is_refused_even_with_a_sibling_bin(tmp_path):
    """#367: a `.hex` is NOT a plausibly-ELF-with-a-known-sibling case. The
    resolution (`flash_plan.resolve_slot0_binary`) is deliberately narrow --
    a plausibly-ELF artefact's (no extension, `.elf`, `.out` -- tan-cli#373
    widened this from `.elf`-only) same-stem `.bin`, the Zephyr build's
    known-good pair -- and a `.hex` carries its own load addresses; silently
    swapping in an unrelated same-stem `.bin` would flash a DIFFERENT
    artefact than the manifest named. #353's decision (a): refuse, never
    resolve. (This test used to assert the opposite of both its own name
    and its own docstring -- its body proved resolution while its
    title/intro promised a refusal; #367 caught the contradiction.)"""
    from tan.core.flash_plan import FlashPlanError, plan_alif_mram_jlink

    (tmp_path / "zephyr.hex").write_text(":00000001FF\n", encoding="utf-8")
    (tmp_path / "zephyr.bin").write_bytes(b"\x50\x42\x00\x20" + b"\x00" * 64)

    with pytest.raises(FlashPlanError) as raised:
        plan_alif_mram_jlink(_mramxip_inputs(tmp_path, "zephyr.hex"), lambda _t: True)
    message = str(raised.value)
    assert "not a raw .bin" in message
    assert "zephyr.hex" in message
    assert "slot0_load_address" in message
    # Proves this is the "wrong shape" refusal, not the "ELF with no sibling"
    # one -- a sibling .bin DOES exist here, and it must still not be used.
    assert "Only a plausibly-ELF artefact's" in message
