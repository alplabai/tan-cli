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
#: The REAL process environment, captured at COLLECTION time -- see
#: `tests/conftest.py`'s own note, and `_driver_env` below, which is this
#: module's one consumer (tan-cli#541).
from tests.conftest import REAL_ENVIRON
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


# ── text mode TEES the transcript too (tan-cli#522 review, MAJOR 1) ────────


def test_spawn_text_mode_tees_the_transcript_for_the_qualification_to_read():
    """`_spawn`'s live-console branch used to hand the child's stdout fd
    straight to the OS (`subprocess.run(stdout=sink)`) and never redirect
    stderr at all -- `outcome.stdout`/`.stderr` stayed `""` regardless of
    what the child said, so `_flow_d_reset_qualified_message` (tan-cli#522)
    had no transcript to qualify against outside `--format json`. The
    live-console branch now TEES both streams via `_Tee`: still streamed to
    the console live, but also collected into the `_Outcome`. Fails against
    the pre-fix source (measured: `outcome.stdout == outcome.stderr == ""`
    here even though the child printed both markers).

    Asserted on `stdout + stderr`, which is WHICH FIELD-agnostic on purpose
    (tan-cli#540/#541 review, minor 2): that concatenation is exactly what the
    consumer this test exists for reads (`_flow_d_reset_qualified_message`,
    `_swd_probe_halt_markers`), and the two transports fill the fields
    differently -- two pipes fill both, a pty is ONE device and fills only
    `.stdout`. Pinning the field made this an assertion about how pytest was
    invoked: `pytest -s` on a terminal takes the pty path (measured:
    `AssertionError: _Outcome(success=True, stdout='****** Error: Failed to
    halt...`). The SPLIT itself is asserted where it belongs, in
    `test_a_non_terminal_sink_keeps_the_pipe_behaviour_unchanged`."""
    from tan.commands.flash_cmd import _spawn

    argv = _stub(
        "import sys; "
        "print('****** Error: Failed to halt CPU'); "
        "print('CPU is not halted', file=sys.stderr)"
    )
    outcome = _spawn(argv, capture=False, timeout=5.0)
    transcript = outcome.stdout + outcome.stderr
    assert outcome.success is True
    assert "Failed to halt CPU" in transcript, outcome
    assert "CPU is not halted" in transcript, outcome


def test_spawn_text_mode_timeout_still_folds_in_the_teed_output_before_the_kill():
    """The timeout sibling of the test above: `Popen.wait`'s own
    `TimeoutExpired` carries no output at all (unlike `subprocess.run
    (capture_output=True)`'s), so `_spawn` builds one BY HAND from what the
    tees had already collected before the kill -- the same
    `before-the-kill` guarantee tan-cli#487 gave the JSON-mode/wrapped-
    console branches, now true on the live-console branch too."""
    from tan.commands.flash_cmd import _spawn

    argv = _stub(
        "import sys, time; print('before-the-kill'); sys.stdout.flush(); time.sleep(30)"
    )
    outcome = _spawn(argv, capture=False, timeout=1.0)
    assert outcome.success is False
    assert "before-the-kill" in outcome.stderr
    assert "timed out after 1s and was killed" in outcome.stderr


# ── _Tee round 3 (tan-cli#519/#522 review round 3): bounded join + live read ─


@pytest.mark.skipif(sys.platform == "win32", reason="`sh -c '... &'` is a POSIX shell shape")
def test_spawn_returns_promptly_when_the_child_leaves_an_orphaned_pipe_holder():
    """BLOCKER: `_Tee.text()` used to `.join()` its background reader thread
    with NO timeout, so anything still holding the child's stdout/stderr
    pipe open after the DIRECT child exits -- a backgrounded grandchild, say
    `sleep 20 &` inside a shell script -- blocked the join, and therefore
    `_spawn`, indefinitely: `proc.wait()` returns on schedule (the direct
    child is gone) but `out_tee.text()`/`err_tee.text()` afterwards do not,
    because the read loop on the pipe never sees EOF until the ORPHAN also
    exits. Measured against the pre-round-3 source: `sh -c 'sleep 20 & echo
    done; exit 0'` under `_spawn(..., timeout=3)` took 20.00s to return, not
    ~0s the way `origin/dev`'s OS-level fd redirect did. Bounded here well
    under that -- `join`'s default timeout is now `_DRAIN_JOIN_S` (2s)."""
    import time

    from tan.commands.flash_cmd import _spawn

    started = time.monotonic()
    outcome = _spawn(["sh", "-c", "sleep 20 & echo done; exit 0"], capture=False, timeout=3.0)
    elapsed = time.monotonic() - started
    assert outcome.success is True
    assert elapsed < 10.0, elapsed


@pytest.mark.skipif(sys.platform == "win32", reason="`sh -c '... &'` is a POSIX shell shape")
def test_spawn_timeout_path_returns_promptly_despite_an_orphaned_pipe_holder():
    """The TIMEOUT-path sibling of the test above: `proc.kill()` reaps only
    the DIRECT child, so a grandchild it left running (`sleep 20 &`) still
    holds the pipe open after the kill too -- the exact shape that made the
    `except subprocess.TimeoutExpired` handler's `out_tee.text()`/
    `err_tee.text()` calls hang past `_spawn`'s own timeout entirely on the
    pre-round-3 source (measured: `sh -c 'sleep 20 & sleep 30'` under
    `_spawn(..., timeout=2)` NEVER RETURNED, killed only by an outer test
    harness timeout). Bounded here the same way."""
    import time

    from tan.commands.flash_cmd import _spawn

    started = time.monotonic()
    outcome = _spawn(["sh", "-c", "sleep 20 & sleep 30"], capture=False, timeout=2.0)
    elapsed = time.monotonic() - started
    assert outcome.success is False
    assert "timed out" in outcome.stderr
    assert elapsed < 10.0, elapsed


def test_tee_forwards_each_read1_chunk_immediately_not_buffered_to_a_char_count():
    """MAJOR 1: the first `_Tee` read a TEXT-mode stream via `.read(4096)`,
    which blocks until 4096 CHARACTERS are decoded or EOF -- not live.
    `_Tee` now reads a BINARY stream via `.read1(n)`, which returns whatever
    the OS has ready, however small, and forwards each `read1()` result to
    the sink as ITS OWN chunk -- proven here with a fake stream that never
    returns anywhere close to a full chunk: three separate `read1()` results
    must arrive as three separate accumulated chunks, not one chunk that
    only appears after all three were buffered up together first. Fails
    against the pre-round-3 source (a `TextIOWrapper`-shaped `.read(4096)`
    loop has no `read1` counterpart to call at all -- this test exercises
    the class's actual read primitive, not merely its net output)."""
    import io

    from tan.commands.flash_cmd import _Tee

    class _FakeBinaryStream:
        def __init__(self, chunks):
            self._chunks = [*chunks, b""]

        def read1(self, _n):
            return self._chunks.pop(0)

    stream = _FakeBinaryStream([b"a", b"b", b"c"])
    sink = io.StringIO()
    tee = _Tee(stream, sink)
    assert tee.text() == "abc"
    assert tee._chunks == ["a", "b", "c"]


def test_tee_sink_write_failure_only_drops_console_display_not_the_transcript():
    """MINOR: `except (OSError, ValueError): pass` around the sink write also
    caught `UnicodeEncodeError` (a `ValueError` subclass) -- raised when the
    sink's own narrower encoding cannot represent a character the decoder
    correctly produced -- and silently discarded the WHOLE chunk, including
    any clean lines it shared. The transcript (`.text()`) must still collect
    everything regardless of what the console sink can display."""
    import io

    from tan.commands.flash_cmd import _Tee

    class _FakeBinaryStream:
        def __init__(self, chunks):
            self._chunks = [*chunks, b""]

        def read1(self, _n):
            return self._chunks.pop(0)

    class _AsciiOnlySink:
        encoding = "ascii"

        def __init__(self):
            self.written = []

        def write(self, text: str) -> None:
            self.written.append(text.encode(self.encoding))  # raises on non-ASCII

        def flush(self) -> None:
            pass

    stream = _FakeBinaryStream(["clean line\n".encode(), "café\n".encode()])
    sink = _AsciiOnlySink()
    tee = _Tee(stream, sink)
    assert tee.text() == "clean line\ncafé\n"


def test_tee_decodes_a_multibyte_utf8_sequence_split_across_a_read1_boundary():
    """tan-cli#519/#522 review, MINOR 2: `_Tee`'s own docstring promises a
    multi-byte UTF-8 sequence split across two `read1` calls is "never
    corrupted or double-counted" -- `codecs.getincrementaldecoder`, not a
    naive per-chunk `bytes.decode()`. Nothing pinned that: the only decoder
    test before this one feeds single-byte ASCII chunks (`b"a"`/`b"b"`/
    `b"c"`), which pass identically under a naive per-chunk decode too, so a
    regression from the incremental decoder back to a plain `.decode()` per
    `read1()` result would ship green. Here U+20AC (`€`, 3 UTF-8 bytes) is
    split 1/2 across the chunk boundary -- confirmed to FAIL against a naive
    per-chunk decode (`b"A\\xe2".decode("utf-8")` alone raises
    `UnicodeDecodeError`, measured)."""
    import io

    from tan.commands.flash_cmd import _Tee

    class _FakeBinaryStream:
        def __init__(self, chunks):
            self._chunks = [*chunks, b""]

        def read1(self, _n):
            return self._chunks.pop(0)

    stream = _FakeBinaryStream([b"A\xe2", b"\x82\xacB"])
    sink = io.StringIO()
    tee = _Tee(stream, sink)
    assert tee.text() == "A€B"


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


def test_execute_message_text_mode_truly_silent_failure_still_falls_back():
    """The narrow case that survives (tan-cli#519/#522 review, MAJOR 1): a
    child that genuinely printed NOTHING on either stream leaves `outcome.
    stdout`/`.stderr` both empty regardless of mode, so there is still
    nothing for `_execute_message` to surface and it falls back to the
    generic sentence. `_Outcome` built by hand here (not via `_spawn`) is
    still a legitimate stand-in for this one case: a silent child's real
    `_spawn` outcome has BOTH fields empty too, whichever branch handled it."""
    from tan.commands.flash_cmd import _Outcome, _execute_message

    outcome = _Outcome(success=False, returncode=1, captured=False)
    assert _execute_message(outcome, "yocto_wic", "a55") == "yocto_wic[a55]: flash command failed"


def test_execute_message_text_mode_now_surfaces_a_real_spawn_diagnosis():
    """The case round 3's `_Tee`/capture-and-replay fix actually changed,
    guarded here against a REAL `_spawn` outcome rather than a hand-built
    `_Outcome` (tan-cli#519/#522 review, MAJOR 1): a text-mode child that
    prints a diagnosis on stderr and then exits non-zero -- the ordinary
    failure shape the old test above claimed was "unaffected" -- now has
    that diagnosis in `outcome.stderr` (both of `_spawn`'s single-tool
    branches capture the child's transcript in every mode now, not only
    under `--format json`), so `_execute_message` surfaces it instead of the
    bare `flash command failed` sentence. Fails against the pre-round-3
    source (measured: `outcome.stderr` was `""` here, and this asserted the
    bare fallback).

    The transcript is read as `stdout + stderr` for the same field-agnostic
    reason the tee test above gives (tan-cli#540/#541 review, minor 2) -- a
    pty run puts it all in `.stdout`. The MESSAGE assertion below needs no
    such care: `_capture_tail` falls back to `stdout` when `stderr` is blank,
    so it is identical on both transports, which is the point."""
    from tan.commands.flash_cmd import _execute_message, _spawn

    outcome = _spawn(
        [
            sys.executable, "-c",
            "import sys; print('Error: could not connect to target', file=sys.stderr); "
            "sys.exit(3)",
        ],
        capture=False,
        timeout=5.0,
    )
    assert (outcome.stdout + outcome.stderr).strip() == "Error: could not connect to target"
    message = _execute_message(outcome, "swd_probe", "e1")
    assert message == "swd_probe[e1]: Error: could not connect to target"


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


# ── swd_probe OpenOCD/pyOCD probe selection (tan-cli#519) ───────────────────


def test_swd_probe_openocd_emits_adapter_usb_location_when_set():
    """tan-cli#519, the headline defect: the OpenOCD arm read no
    probe-selection field at all -- `flash_args.openocd_usb_location` must
    now render as its own `-c "adapter usb location {<path>}"` word, ahead of
    the target config and the `program` command (both can trigger a connect).
    Fails against the pre-fix source (measured: `openocd_usb_location` was
    not read anywhere in `plan_swd_probe`, so this key had no effect at
    all)."""
    inp = _swd_inputs(interface="cmsis-dap", target="gd32g553", openocd_usb_location="3-4.4.3")
    plan = flash_plan.plan_swd_probe(inp, lambda n: n == "openocd")
    assert "-c" in plan.argv
    idx = plan.argv.index("-c")
    assert plan.argv[idx + 1] == "adapter usb location {3-4.4.3}"
    # Ahead of the target config, which can trigger the connect.
    target_idx = plan.argv.index("target/gd32g553.cfg")
    assert idx < target_idx


def test_swd_probe_openocd_no_usb_location_emits_no_adapter_line():
    """The unaffected case: a manifest naming no `openocd_usb_location` keeps
    the exact argv shape from before this fix -- no stray `-c "adapter usb
    location ..."` word."""
    inp = _swd_inputs(interface="cmsis-dap", target="gd32g553")
    plan = flash_plan.plan_swd_probe(inp, lambda n: n == "openocd")
    assert not any("adapter usb location" in str(a) for a in plan.argv)


@pytest.mark.parametrize("bad", ["a;b", "a$b", "a[b]", "a\nb", 'a"b', "a{b}"])
def test_swd_probe_openocd_usb_location_is_charset_guarded(bad):
    """`openocd_usb_location` reaches an OpenOCD `-c` Tcl word verbatim, so it
    gets the same Jim-Tcl-metacharacter/control-character guard #486 gives
    every other `-c` word -- `validate_identifier` would also reject the
    dots a real USB path uses (`3-4.4.3`), so this is `validate_openocd_word`
    specifically."""
    inp = _swd_inputs(interface="cmsis-dap", target="gd32g553", openocd_usb_location=bad)
    with pytest.raises(FlashPlanError) as raised:
        flash_plan.plan_swd_probe(inp, lambda n: n == "openocd")
    assert "openocd_usb_location" in str(raised.value)


def test_swd_probe_openocd_usb_location_accepts_a_real_usb_topology_path():
    """The charset guard must not reject the shape a real value actually
    takes -- dots and dashes, no slashes."""
    inp = _swd_inputs(interface="cmsis-dap", target="gd32g553", openocd_usb_location="3-4.4.3")
    plan = flash_plan.plan_swd_probe(inp, lambda n: n == "openocd")
    assert "adapter usb location {3-4.4.3}" in plan.argv


def test_swd_probe_openocd_usb_location_whitespace_is_braced_not_split():
    """tan-cli#519 review, MINOR: `openocd_usb_location` used to be the one
    unbraced `-c` word interpolation in this module -- `validate_openocd_word`
    rejects every Jim Tcl metacharacter and control character but not plain
    whitespace, so `"3-4.4.3 verify"` (no metacharacter in sight) reached the
    tool as `-c "adapter usb location 3-4.4.3 verify"`, a Tcl command with TWO
    words where OpenOCD expects one -- the exact whitespace-splits-a-word
    class `openocd_program_word`'s own docstring names, and #511's answer to
    it was unconditional bracing. Fails against the pre-fix source (measured:
    the argv word is `adapter usb location 3-4.4.3 verify`, unbraced, with no
    refusal at all)."""
    inp = _swd_inputs(
        interface="cmsis-dap", target="gd32g553", openocd_usb_location="3-4.4.3 verify"
    )
    plan = flash_plan.plan_swd_probe(inp, lambda n: n == "openocd")
    assert "adapter usb location {3-4.4.3 verify}" in plan.argv


def test_swd_probe_pyocd_emits_uid_flag_when_set():
    """The pyOCD sibling: `flash_args.pyocd_uid` renders as `--uid <value>`,
    pyOCD's own selector -- different from both `jlink_serial` (serial-only)
    and `openocd_usb_location` (USB-path-only). Fails against the pre-fix
    source (measured: `pyocd_uid` was not read anywhere in
    `plan_swd_probe`)."""
    inp = _swd_inputs(use_pyocd=True, interface="cmsis-dap", target="stm32h7x", pyocd_uid="abc123")
    plan = flash_plan.plan_swd_probe(inp, lambda n: n == "pyocd")
    assert "--uid" in plan.argv
    assert plan.argv[plan.argv.index("--uid") + 1] == "abc123"


def test_swd_probe_pyocd_no_uid_emits_no_uid_flag():
    """The unaffected case: no `pyocd_uid` means no `--uid` word at all."""
    inp = _swd_inputs(use_pyocd=True, interface="cmsis-dap", target="stm32h7x")
    plan = flash_plan.plan_swd_probe(inp, lambda n: n == "pyocd")
    assert "--uid" not in plan.argv


@pytest.mark.parametrize("bad", ["a;b", "a$b", "a.b", "dev\nice"])
def test_swd_probe_pyocd_uid_is_charset_guarded(bad):
    """`pyocd_uid` only ever reaches argv (no shell, no Tcl), but it still
    gets the same `validate_identifier` charset guard every other manifest
    identifier in this module gets. `a.b`, not `a/b`: `validate_identifier`
    deliberately ALLOWS a `/`-separated path of plain identifier segments
    (for OpenOCD's own multi-segment interface configs), so `a/b` is not
    actually hostile to it -- a bare `.` is."""
    inp = _swd_inputs(use_pyocd=True, interface="cmsis-dap", target="stm32h7x", pyocd_uid=bad)
    with pytest.raises(FlashPlanError) as raised:
        flash_plan.plan_swd_probe(inp, lambda n: n == "pyocd")
    assert "pyocd_uid" in str(raised.value)


def test_swd_probe_pyocd_uid_accepts_a_plugin_prefixed_form():
    """tan-cli#519 review, MINOR: the plain `validate_identifier` guard
    refuses `:`, but pyOCD's own `-u`/`--uid` documents an OPTIONAL
    `<plugin>:<uid>` prefix to disambiguate an otherwise-ambiguous UID --
    confirmed against a real installed pyOCD 0.44.1 (`pyocd flash --help`:
    "Optionally prefixed with '<probe-type>:' where <probe-type> is the name
    of a probe plugin"; `pyocd list --plugins` names `jlink`/`stlink`/
    `cmsisdap`/`picoprobe`/`remote`, all plain identifiers themselves).
    Fails against the pre-fix source (measured: `jlink:603000869` -- the
    exact shape needed to disambiguate the alplab-gw bench's two
    cloned-serial probes -- raised `FlashPlanError` naming `pyocd_uid`)."""
    inp = _swd_inputs(
        use_pyocd=True, interface="cmsis-dap", target="stm32h7x", pyocd_uid="jlink:603000869"
    )
    plan = flash_plan.plan_swd_probe(inp, lambda n: n == "pyocd")
    assert "--uid" in plan.argv
    assert plan.argv[plan.argv.index("--uid") + 1] == "jlink:603000869"


@pytest.mark.parametrize(
    "bad",
    [
        "jlink:st:link",  # more than one colon -- not the documented shape
        ":603000869",  # empty plugin half
        "jlink:",  # empty uid half
        "jl.nk:603000869",  # hostile plugin half
        "jlink:60300;869",  # hostile uid half
    ],
)
def test_swd_probe_pyocd_uid_plugin_prefix_still_charset_guards_both_halves(bad):
    """The widening above is exactly one shape -- a single `<plugin>:<uid>`
    split with BOTH halves charset-clean -- not a blanket colon allowance.
    Anything else carrying a colon still refuses, with the same message."""
    inp = _swd_inputs(use_pyocd=True, interface="cmsis-dap", target="stm32h7x", pyocd_uid=bad)
    with pytest.raises(FlashPlanError) as raised:
        flash_plan.plan_swd_probe(inp, lambda n: n == "pyocd")
    assert "pyocd_uid" in str(raised.value)


def test_swd_probe_jlink_refuses_a_stray_openocd_usb_location():
    """The wrong-arm refusal, `jlink_serial`-side: `openocd_usb_location` set
    while the run actually takes the J-Link arm must refuse loudly, not
    silently drop the field -- the same accept-and-ignore shape #513 closed
    for `jlink_serial`."""
    inp = _swd_inputs(openocd_usb_location="3-4.4.3")
    with pytest.raises(FlashPlanError) as raised:
        flash_plan.plan_swd_probe(inp, lambda n: n == "JLinkExe")
    assert "openocd_usb_location" in str(raised.value)


def test_swd_probe_jlink_refuses_a_stray_pyocd_uid():
    """Same shape, `pyocd_uid`-side."""
    inp = _swd_inputs(pyocd_uid="abc123")
    with pytest.raises(FlashPlanError) as raised:
        flash_plan.plan_swd_probe(inp, lambda n: n == "JLinkExe")
    assert "pyocd_uid" in str(raised.value)


def test_swd_probe_openocd_refuses_a_stray_pyocd_uid():
    """The CROSS-arm refusal: `pyocd_uid` set while the run lands on OpenOCD
    (not pyOCD, not J-Link) must also refuse -- `--uid` is not an OpenOCD
    primitive either."""
    inp = _swd_inputs(interface="cmsis-dap", target="gd32g553", pyocd_uid="abc123")
    with pytest.raises(FlashPlanError) as raised:
        flash_plan.plan_swd_probe(inp, lambda n: n == "openocd")
    assert "pyocd_uid" in str(raised.value)


def test_swd_probe_openocd_refuses_a_stray_pyocd_uid_when_pyocd_is_also_on_path():
    """BLOCKER regression: the refusal above (`lambda n: n == "openocd"`)
    cannot catch this, because it leaves pyOCD unavailable -- exactly the one
    case where the arm split's `if openocd: ... elif pyocd: ...` precedence
    doesn't matter. On a host with BOTH tools on PATH, OpenOCD always wins the
    arm regardless, so a `pyocd_uid`-only refusal keyed off pyOCD
    *availability* (`not pyocd`) stayed silent here -- `pyocd` was True, the
    guard never fired, and the run silently landed on OpenOCD with no probe
    selector of any kind, dropping `pyocd_uid` on the floor. This is the
    accept-and-ignore shape #513/#519 exist to close, re-created by testing
    availability instead of the arm this run actually takes."""
    inp = _swd_inputs(interface="cmsis-dap", target="gd32g553", pyocd_uid="abc123")
    with pytest.raises(FlashPlanError) as raised:
        flash_plan.plan_swd_probe(inp, lambda n: n in ("openocd", "pyocd"))
    assert "pyocd_uid" in str(raised.value)


def test_swd_probe_pyocd_refuses_a_stray_openocd_usb_location():
    """The CROSS-arm refusal, the other direction: `openocd_usb_location` set
    while the run lands on pyOCD must also refuse."""
    inp = _swd_inputs(
        use_pyocd=True, interface="cmsis-dap", target="stm32h7x",
        openocd_usb_location="3-4.4.3",
    )
    with pytest.raises(FlashPlanError) as raised:
        flash_plan.plan_swd_probe(inp, lambda n: n == "pyocd")
    assert "openocd_usb_location" in str(raised.value)


def test_swd_probe_usb_location_charset_guard_is_not_host_dependent():
    """Mirrors `test_swd_probe_probe_serial_charset_guard_is_not_host_dependent`:
    the charset check runs unconditionally, ahead of the arm split, so a
    HOSTILE `openocd_usb_location` must be refused whether `--dry-run` forces
    the J-Link arm or a real run on an openocd-only host takes the fallback
    arm -- not one and not the other. (The wrong-arm refusal is a SEPARATE
    guard, exercised on a charset-clean value by the cross-arm tests above.)"""
    args = {"interface": "cmsis-dap", "target": "gd32g553", "openocd_usb_location": "dev\nice"}
    dry = FlashInputs(artefact="/build/zephyr.bin", flash_args=args, core_id="cm7", sku="S", dry_run=True)
    real = FlashInputs(artefact="/build/zephyr.bin", flash_args=args, core_id="cm7", sku="S", dry_run=False)
    with pytest.raises(FlashPlanError):
        flash_plan.plan_swd_probe(dry, lambda n: n == "openocd")
    with pytest.raises(FlashPlanError):
        flash_plan.plan_swd_probe(real, lambda n: n == "openocd")


# ── MAJOR 2 (tan-cli#519/#522 review): --dry-run and a real run must agree ─


def test_swd_probe_dry_run_and_real_run_agree_when_only_openocd_is_on_path():
    """`--dry-run` used to force the J-Link arm UNCONDITIONALLY (`_JLINK_
    BINARIES[0]`, no `which()` call at all), so a manifest naming
    `flash_args.openocd_usb_location` refused on EVERY preview -- even on a
    host that genuinely has openocd (and pyocd) and no J-Link at all, where
    a REAL run takes neither of this arm's wrong-arm refusals and reports
    `ok`. Same manifest, same simulated host (only openocd/pyocd `which()`-
    findable, no J-Link): dry-run and a real run must now agree, byte-for-
    byte. Fails against the pre-fix source (measured: the dry-run call
    raised `FlashPlanError` naming `openocd_usb_location` and 'this run is
    taking the J-Link path'; the real-run call returned an `openocd` plan)."""
    which_openocd_and_pyocd = lambda n: n in ("openocd", "pyocd")  # noqa: E731
    args = {"interface": "cmsis-dap", "target": "gd32g553", "openocd_usb_location": "3-4.4.3"}
    dry = FlashInputs(artefact="/build/zephyr.bin", flash_args=args, core_id="cm7", sku="S", dry_run=True)
    real = FlashInputs(artefact="/build/zephyr.bin", flash_args=args, core_id="cm7", sku="S", dry_run=False)

    dry_plan = flash_plan.plan_swd_probe(dry, which_openocd_and_pyocd)
    real_plan = flash_plan.plan_swd_probe(real, which_openocd_and_pyocd)

    assert dry_plan.argv[0] == "openocd"
    assert dry_plan.argv == real_plan.argv


def test_swd_probe_dry_run_and_real_run_agree_when_only_openocd_is_on_path_pyocd_uid_side():
    """The `pyocd_uid` sibling of the test above -- a manifest naming that
    field instead must ALSO agree between `--dry-run` and a real run on the
    SAME (openocd-only, no pyocd, no J-Link) host: both refuse, for the
    identical reason (pyOCD is not the arm this host/manifest combination
    takes on either side -- OpenOCD wins the arm split whenever it is
    available, per tan-cli#519's own BLOCKER fix)."""
    which_openocd_only = lambda n: n == "openocd"  # noqa: E731
    args = {"interface": "cmsis-dap", "target": "gd32g553", "pyocd_uid": "abc123"}
    dry = FlashInputs(artefact="/build/zephyr.bin", flash_args=args, core_id="cm7", sku="S", dry_run=True)
    real = FlashInputs(artefact="/build/zephyr.bin", flash_args=args, core_id="cm7", sku="S", dry_run=False)

    with pytest.raises(FlashPlanError) as dry_raised:
        flash_plan.plan_swd_probe(dry, which_openocd_only)
    with pytest.raises(FlashPlanError) as real_raised:
        flash_plan.plan_swd_probe(real, which_openocd_only)
    assert "pyocd_uid" in str(dry_raised.value)
    assert str(dry_raised.value) == str(real_raised.value)


def test_swd_probe_dry_run_and_real_run_agree_when_only_pyocd_is_on_path_pyocd_uid_side():
    """tan-cli#519/#522 review round 3, MINOR: the sibling test above
    (`..._openocd_is_on_path_pyocd_uid_side`) uses `which_openocd_only` --
    the one host shape where the pre-round-3 and fixed code already agreed
    BY ACCIDENT (OpenOCD always wins the arm-split precedence, so a
    `pyocd_uid` manifest is refused on that host either way) -- so it never
    actually exercised the pyocd-only host this round's MAJOR 2 fix is
    about. Here, on a PYOCD-only host, `pyocd_uid` is the field that arm CAN
    honour: `--dry-run` and a real run must both plan the SAME `pyocd`
    command line, not merely both refuse or both agree by luck. Fails
    against the pre-round-3 source (measured: the `--dry-run` bypass forced
    `openocd = True` unconditionally, so `chosen` was always `"openocd"` and
    the dry-run call raised `FlashPlanError` naming 'this run is not taking
    the pyOCD path', while the real-run call -- correctly seeing no openocd
    on PATH -- returned a `pyocd` plan)."""
    which_pyocd_only = lambda n: n == "pyocd"  # noqa: E731
    args = {"interface": "cmsis-dap", "target": "stm32h7x", "pyocd_uid": "abc123"}
    dry = FlashInputs(artefact="/build/zephyr.bin", flash_args=args, core_id="cm7", sku="S", dry_run=True)
    real = FlashInputs(artefact="/build/zephyr.bin", flash_args=args, core_id="cm7", sku="S", dry_run=False)

    dry_plan = flash_plan.plan_swd_probe(dry, which_pyocd_only)
    real_plan = flash_plan.plan_swd_probe(real, which_pyocd_only)

    assert dry_plan.argv[0] == "pyocd"
    assert dry_plan.argv == real_plan.argv


def test_swd_probe_dry_run_and_real_run_agree_when_only_pyocd_is_on_path_openocd_usb_location_side():
    """The refusal-shape mirror, same host: `openocd_usb_location` needs the
    OpenOCD arm, which this pyocd-only host does not take on EITHER side, so
    both must refuse, identically. Fails against the pre-round-3 source
    (measured: the `--dry-run` bypass forced `openocd = True` unconditionally
    and planned a full `openocd -f ... -c 'adapter usb location 3-4.4.3' ...`
    command line for a tool not installed on this host, while the real-run
    call correctly refused)."""
    which_pyocd_only = lambda n: n == "pyocd"  # noqa: E731
    args = {"interface": "cmsis-dap", "target": "gd32g553", "openocd_usb_location": "3-4.4.3"}
    dry = FlashInputs(artefact="/build/zephyr.bin", flash_args=args, core_id="cm7", sku="S", dry_run=True)
    real = FlashInputs(artefact="/build/zephyr.bin", flash_args=args, core_id="cm7", sku="S", dry_run=False)

    with pytest.raises(FlashPlanError) as dry_raised:
        flash_plan.plan_swd_probe(dry, which_pyocd_only)
    with pytest.raises(FlashPlanError) as real_raised:
        flash_plan.plan_swd_probe(real, which_pyocd_only)
    assert "openocd_usb_location" in str(dry_raised.value)
    assert str(dry_raised.value) == str(real_raised.value)


def test_swd_probe_openocd_usb_location_whitespace_only_is_refused_at_plan_time():
    """tan-cli#519/#522 review round 3, MINOR: `validate_openocd_word`
    guards the Jim Tcl/control-character charset only -- whitespace is
    deliberately left alone there (a real artefact path needs it) -- so a
    WHITESPACE-ONLY `openocd_usb_location` passed straight through and would
    have reached OpenOCD as `adapter usb location {  }`, an empty selector.
    Refused here instead, at plan time, before anything is spawned. Fails
    against the pre-round-3 source (measured: no error, and `"  "` appeared
    verbatim in `argv`)."""
    inp = _swd_inputs(interface="cmsis-dap", target="gd32g553", openocd_usb_location="  ")
    with pytest.raises(FlashPlanError) as raised:
        flash_plan.plan_swd_probe(inp, lambda name: name == "openocd")
    assert "openocd_usb_location" in str(raised.value)
    assert "whitespace" in str(raised.value)


def test_swd_probe_dry_run_still_defaults_to_jlink_when_neither_new_field_is_set():
    """The case MAJOR 2's fix must not disturb: a manifest naming NEITHER
    `openocd_usb_location` NOR `pyocd_uid` keeps the unconditional, replay-
    host-independent J-Link default under `--dry-run` -- unaffected by
    whatever this box's own PATH happens to hold (`which` here reports
    every tool absent, including J-Link itself). This is what every
    `swd_probe` case in `tests/parity/test_flash_oracle_parity.py` relies on
    for a host-independent `--dry-run` preview."""
    dry = FlashInputs(
        artefact="/build/zephyr.bin", flash_args={}, core_id="cm7", sku="S", dry_run=True
    )
    plan = flash_plan.plan_swd_probe(dry, lambda n: False)
    assert plan.argv[0] == "JLinkExe"


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

    # `executable` (tan-cli#567): the absolute path `_flow_d_preflight` pins
    # the spawn to, alongside the `argv` the child itself sees. Accepted and
    # ignored -- this test's subject is the Commander SCRIPT.
    def _fake_spawn_jlink(
        argv, script, capture, timeout, venv_bin=None, workspace=None, executable=None
    ):
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
    target (`./oops`, `tests/fixtures/oracle_captures/test_flash_oracle_parity.
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


def _stub_flow_d_probe(monkeypatch, tmp_path, stdout: str, stderr: str = "", success: bool = True):
    """Make `_flow_d_preflight` reach a fake connect banner without touching a
    real probe: a resolvable but INERT `JLinkExe` as the only thing on PATH,
    and `_spawn_jlink` replaced so the file is never executed. Every test
    below is about the message the banner produces, nothing else.

    **The stub is a real file on PATH, not a monkeypatched `_tool_available`
    (tan-cli#567).** This helper used to assert availability by patching the
    gate to `True` while leaving PATH untouched, which stopped working the
    moment the spawn started resolving `argv[0]` to an absolute location
    instead of handing the platform a bare name -- the preflight refused with
    "could not be resolved to a real location ... refusing to write MRAM
    without confirming which board is attached" before any banner was read.
    That refusal is CORRECT: a fixture whose gate says "available" and whose
    PATH says "nowhere" is exactly the check/spawn disagreement #567 exists to
    make impossible, so the fixture is what was wrong. Seeding the tool the
    way the other PATH-seeding fake-tool tests in this file do (zero-byte
    file, `0o755` on POSIX so `shutil.which`'s `X_OK` probe sees it, `.exe`
    on Windows, where a bare extensionless file is not a `%PATH%` candidate
    for that identity -- `tan.core.tool_lookup.windows_candidate_names`)
    exercises the real gate and the real resolution, and the refusal arm
    keeps its own pin in `test_bare_argv0_spawn.py`.

    "The other PATH-seeding tests", not "every other test": `_SPAWN_PROBE`
    below still seeds its whole tool dir extension-less. That is deliberately
    left alone -- its cases are green on windows-latest (job 93300561998 at
    01b2e73 names all fourteen failures, and none is a `_SPAWN_PROBE` case),
    so nothing there depends on the seeded name resolving, and changing a
    fixture no evidence indicts would be a guess.

    `_programs_resolved_in_venv` is likewise no longer patched: these calls
    pass no `venv_bin`, and the real function returns `argv` unchanged when
    there is none."""
    tools = tmp_path / "faketools"
    tools.mkdir(exist_ok=True)
    jlink_path = tools / ("JLinkExe.exe" if os.name == "nt" else "JLinkExe")
    jlink_path.write_text("", encoding="utf-8")
    if os.name != "nt":
        os.chmod(jlink_path, 0o755)
    monkeypatch.setenv("PATH", str(tools))
    monkeypatch.setattr(
        flash_cmd,
        "_spawn_jlink",
        lambda *_a, **_k: flash_cmd._Outcome(success=success, stdout=stdout, stderr=stderr),
    )


def test_flow_d_preflight_a_different_reported_dp_id_keeps_the_wiring_message(
    monkeypatch, tmp_path
):
    """tan-cli#312, case (a): the probe DID connect and reported a real, just
    different, SW-DP ID -- a genuine wrong-board / wiring / probe-selection
    problem, so the original remediation stands unchanged."""
    _stub_flow_d_probe(
        monkeypatch,
        tmp_path,
        stdout="Connecting to target via SWD\nFound SW-DP with ID 0x2BA01477\n",
    )
    message = flash_cmd._flow_d_preflight(_flow_d_preflight_inputs())
    assert message is not None
    assert "Check the wiring and which board is physically attached" in message
    assert "re-enumerat" not in message


def test_flow_d_preflight_wrong_dp_id_names_the_actual_id_too(monkeypatch, tmp_path):
    """tan-cli#512, secondary. The mismatch refusal used to name only the
    EXPECTED SW-DP IDR, never the actual one the preflight just read --
    although it took this exact `_dp_id_reported(banner)` branch and
    therefore had the value in hand. On a bench where two probes share a
    cloned USB serial (measured: `603000869` answers both a real E1M-AEN801
    at `0x4C013477` and a GD32 bridge at `0x0BE12477`), the actual ID is the
    single most useful datum for telling which board actually answered."""
    _stub_flow_d_probe(
        monkeypatch,
        tmp_path,
        stdout="Connecting to target via SWD\nFound SW-DP with ID 0x2BA01477\n",
    )
    message = flash_cmd._flow_d_preflight(_flow_d_preflight_inputs())
    assert message is not None
    assert "0x4C013477" in message, message  # the expected id (unchanged)
    assert "0x2BA01477" in message, message  # tan-cli#512: the actual id, new


def test_flow_d_preflight_wrong_dp_id_names_the_sw_dp_id_not_jlink_serial(monkeypatch, tmp_path):
    """tan-cli#369: the wrong-DP-ID remediation used to read as "pin
    jlink_serial to fix this" -- wrong on the bench this preflight actually
    caught a mismatch on, where a cloned/shared USB serial made jlink_serial
    ambiguous between two physical probes. The remediation must name the
    SW-DP ID as the real discriminator and say plainly that a shared/cloned
    serial cannot be disambiguated by jlink_serial alone."""
    _stub_flow_d_probe(
        monkeypatch,
        tmp_path,
        stdout="Connecting to target via SWD\nFound SW-DP with ID 0x2BA01477\n",
    )
    message = flash_cmd._flow_d_preflight(_flow_d_preflight_inputs())
    assert message is not None
    assert "SW-DP ID is the real" in message
    assert "cannot disambiguate" in message
    assert "CLONED serial" in message
    assert "jlink_serial" in message


def test_flow_d_preflight_no_dp_id_at_all_gets_the_re_enumeration_message(monkeypatch, tmp_path):
    """tan-cli#312, case (b): measured verbatim on the rc3 bench run -- the
    probe refused the connect outright, mid re-enumeration after a prior
    `JLinkExe` close, and reported no SW-DP ID whatsoever. This must NOT get
    the wiring/jlink_serial sentence: nothing was wrong with either."""
    _stub_flow_d_probe(
        monkeypatch,
        tmp_path,
        stdout="Connecting to J-Link ...FAILED: Cannot connect to the probe/programmer.\n",
        stderr="J-Link uptime (since boot): 0d 00h 00m 01s\n",
        success=False,
    )
    message = flash_cmd._flow_d_preflight(_flow_d_preflight_inputs())
    assert message is not None
    assert "re-enumerat" in message
    assert "Check the probe selection" not in message
    assert "0x4C013477" in message


def test_flow_d_preflight_an_unrecognised_banner_falls_back_to_the_wiring_message(
    monkeypatch, tmp_path
):
    """Conservative by design (tan-cli#312): a banner with neither a
    recognisable DP-ID token NOR SEGGER's own connect-refused wording is not
    confidently "just re-enumerating" -- the detector must not guess the
    wiring is fine, so this keeps the original sentence.

    **tan-cli#373**: no DP ID was reported here, so this must get the
    ORIGINAL probe-selection/`jlink_serial` sentence -- not #369's
    cloned-serial text, which only applies when a board DID answer with a
    different ID (the wrong-DP-ID test below covers that one)."""
    _stub_flow_d_probe(monkeypatch, tmp_path, stdout="some unrecognised probe banner\n")
    message = flash_cmd._flow_d_preflight(_flow_d_preflight_inputs())
    assert message is not None
    assert "Check the probe selection (flash_args.jlink_serial) and the wiring" in message
    assert "re-enumerat" not in message
    assert "CLONED serial" not in message


def test_flow_d_preflight_a_target_level_cannot_connect_keeps_the_wiring_message(
    monkeypatch, tmp_path
):
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
        tmp_path,
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


def test_flow_d_preflight_a_wrong_jlink_serial_keeps_the_wiring_message(monkeypatch, tmp_path):
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
        tmp_path,
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
    # `.exe` on Windows for the same reason the other seeds in this file
    # spell it that way: since tan-cli#567 the spawn resolves `argv[0]` through
    # `tool_lookup.resolve_tool`, whose Windows `%PATH%` walk tries the name +
    # each `%PATHEXT%` suffix and never the bare extensionless file (matching
    # the Rust oracle's own `find_on_path`). An extensionless stub is invisible
    # there, and the run refuses before it ever builds the Commander script
    # this test reads. Measured red on windows-latest at 01b2e73.
    jlink_path = fake_tools / ("JLinkExe.exe" if os.name == "nt" else "JLinkExe")
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


# ── Flow D's ok_message must not overstate the reset (tan-cli#522) ──────────


def _jlink_stub_path(directory: Path) -> Path:
    """Where a fake `JLinkExe` has to live to be findable, for the one setup
    below that is seeded in one place and REWRITTEN in another.

    `.exe` on Windows: `tool_lookup.resolve_tool`'s `%PATH%` walk tries the
    identity + each `%PATHEXT%` suffix and never the bare extensionless file
    (`tan.core.tool_lookup.windows_candidate_names` carries why, and the Rust
    oracle's `find_on_path` agrees), so an extensionless stub is not a
    candidate for `JLinkExe` there at all. Spelled once so the seed and the
    rewrite cannot name two different files -- which on Windows would leave
    the rewrite's transcript in a file nothing resolves to while the inert
    seed still answers the lookup."""
    return directory / ("JLinkExe.exe" if os.name == "nt" else "JLinkExe")


def _flow_d_reset_report_setup(tmp_path, monkeypatch):
    """Shared scaffolding for the two cases below: a confirmed, non-dry-run
    Flow D write with `subprocess.run` stubbed -- no real J-Link, no board
    reserved. Returns `(work, manifest_path)`; the caller supplies the
    stubbed transcript and drives `flash_cmd._run`."""
    work = tmp_path
    (work / "build").mkdir()
    (work / "sdk" / "scripts").mkdir(parents=True)
    (work / "sdk" / "scripts" / "alp_project.py").write_text("", encoding="utf-8")
    (work / "build" / "atoc.bin").write_bytes(b"real-atoc-bytes")

    manifest = """schema_version: 1
hw_info: {sku: S}
slices:
- {core_id: m55_he, os: zephyr, output_artefact: a.bin, status: ok,
   flash_method: alif_mram_jlink,
   flash_args: {jlink_flash_device: PART_PROFILE, atoc: atoc.bin,
                atoc_address: "0x8057F5B0", confirm: true}}
helper_mcus: []
boot_order: []
"""
    (work / "build" / "system-manifest.yaml").write_text(manifest, encoding="utf-8", newline="")

    fake_tools = work / "faketools"
    fake_tools.mkdir()
    # `.exe` on Windows -- see `_flow_d_atoc_...`'s seed above; an
    # extensionless stub is not a `%PATH%` candidate for the identity
    # `JLinkExe` there, so both callers of this helper refused instead of
    # reaching the transcript they are about to assert on (measured red on
    # windows-latest at 01b2e73). `_jlink_stub_path` is returned so the one
    # caller that OVERWRITES this stub with a real script cannot drift from
    # the name seeded here.
    jlink_path = _jlink_stub_path(fake_tools)
    jlink_path.write_text("", encoding="utf-8")
    if os.name != "nt":
        os.chmod(jlink_path, 0o755)
    monkeypatch.setenv("PATH", str(fake_tools))
    monkeypatch.delenv("ZEPHYR_BASE", raising=False)
    monkeypatch.setattr(flash_cmd, "venv_bin_dir", lambda *_a, **_k: None)
    return work


def test_flow_d_ok_message_qualifies_a_reset_the_transcript_says_failed(tmp_path, monkeypatch):
    """tan-cli#522, the headline defect. A confirmed Flow D write whose
    J-Link transcript ends in `Failed to halt CPU` / `CPU is not halted` --
    the documented busy-resident case, where an image that never idles keeps
    the core running so `VC_CORERESET` cannot halt it -- still exits 0
    (JLinkExe does not itself fail the run over a halt warning), so
    `outcome.success` alone cannot tell this run apart from a clean reset.
    The WRITE is genuinely fine (`verifybin` is what `outcome.success`
    actually proves); it is the `PIN-reset` HALF of the static `ok_message`
    that overstates -- the identical string used to report both cases alike.
    Fails against the pre-fix source (measured: the entry message ends
    `verified and PIN-reset` here too, indistinguishable from a run whose
    reset actually landed)."""
    work = _flow_d_reset_report_setup(tmp_path, monkeypatch)
    transcript = (
        "RSetType 2\n"
        "r\n"
        "VC_CORERESET did not halt CPU\n"
        "WARNING: CPU could not be halted\n"
        "****** Error: Failed to halt CPU\n"
        "g\n"
        "CPU is not halted\n"
    )

    def _fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(list(argv), 0, stdout=transcript, stderr="")

    monkeypatch.setattr(flash_cmd.subprocess, "run", _fake_run)

    exit_code, data, _issues, _lines, _sdk = flash_cmd._run(
        app_path=".", build_root_arg=None, sdk_root_arg=str(work / "sdk"), board_yaml=None,
        core=None, helper=None, dry_run=False, skip_missing_tools=False, capture=True,
        cwd=str(work),
    )

    assert exit_code == 0, data
    assert data["entries"][0]["status"] == "ok", data
    message = data["entries"][0]["message"]
    assert "verified" in message, message
    assert "verified and PIN-reset" not in message, message
    assert "did not halt" in message, message


def test_flow_d_ok_message_keeps_pin_reset_when_the_transcript_says_nothing_of_the_sort(
    tmp_path, monkeypatch
):
    """The unaffected case: a transcript with no halt-failure marker at all
    (the ordinary clean reset, or -- as here -- captured stdout/stderr that
    are simply empty) must keep the original, unqualified `ok_message`
    unchanged. Proves the fix does not turn every Flow D write into the
    qualified sentence regardless of what actually happened."""
    work = _flow_d_reset_report_setup(tmp_path, monkeypatch)

    def _fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(list(argv), 0, stdout="", stderr="")

    monkeypatch.setattr(flash_cmd.subprocess, "run", _fake_run)

    exit_code, data, _issues, _lines, _sdk = flash_cmd._run(
        app_path=".", build_root_arg=None, sdk_root_arg=str(work / "sdk"), board_yaml=None,
        core=None, helper=None, dry_run=False, skip_missing_tools=False, capture=True,
        cwd=str(work),
    )

    assert exit_code == 0, data
    assert data["entries"][0]["status"] == "ok", data
    assert "verified and PIN-reset" in data["entries"][0]["message"], data


@pytest.mark.skipif(
    os.name == "nt",
    reason="the fake JLinkExe below is a POSIX shell script (#!/bin/sh)",
)
def test_flow_d_ok_message_qualifies_a_reset_in_text_mode_too(tmp_path, monkeypatch):
    """tan-cli#522 review, MAJOR 1 -- the SAME defect as `test_flow_d_ok_
    message_qualifies_a_reset_the_transcript_says_failed`, but in the mode a
    standalone bench operator actually reads: `--format json` (`capture=
    True`) is what `alp-sdk-vscode` uses, but text mode is the DEFAULT human
    invocation, and it was left unfixed -- `outcome.stdout`/`.stderr` were
    always empty there (`_spawn`'s live-console branch never captured
    anything), so the qualification silently never fired and the last line
    an operator read was verbatim the sentence #522 was filed against.

    Stubbing `subprocess.run` (the sibling test's technique) does not reach
    this bug at all: `_spawn`'s live-console branch calls `subprocess.Popen`
    directly, so this test spawns a REAL fake `JLinkExe` -- a tiny POSIX
    shell script that prints the same busy-resident transcript -- instead.
    Fails against the pre-fix source (measured: the entry message ends
    `verified and PIN-reset` here too, in text mode, same as JSON mode
    before that fix landed)."""
    work = _flow_d_reset_report_setup(tmp_path, monkeypatch)
    jlink_path = _jlink_stub_path(work / "faketools")
    jlink_path.write_text(
        "#!/bin/sh\n"
        "echo 'RSetType 2'\n"
        "echo 'r'\n"
        "echo 'VC_CORERESET did not halt CPU'\n"
        "echo 'WARNING: CPU could not be halted'\n"
        "echo '****** Error: Failed to halt CPU'\n"
        "echo 'g'\n"
        "echo 'CPU is not halted' 1>&2\n",
        encoding="utf-8",
    )
    os.chmod(jlink_path, 0o755)

    exit_code, data, _issues, _lines, _sdk = flash_cmd._run(
        app_path=".", build_root_arg=None, sdk_root_arg=str(work / "sdk"), board_yaml=None,
        core=None, helper=None, dry_run=False, skip_missing_tools=False, capture=False,
        cwd=str(work),
    )

    assert exit_code == 0, data
    assert data["entries"][0]["status"] == "ok", data
    message = data["entries"][0]["message"]
    assert "verified" in message, message
    assert "verified and PIN-reset" not in message, message
    assert "did not halt" in message, message


def test_flow_d_reset_qualified_message_is_a_pure_substring_swap():
    """The helper itself, in isolation: the one tail `plan_alif_mram_jlink`
    always appends is swapped for the honest one, and nothing else about the
    message moves."""
    base = "alif_mram_jlink[m55_he]: signed ATOC (app embedded) -> 0x8057F5B0 via J-Link (PART_PROFILE); verified and PIN-reset"
    outcome = flash_cmd._Outcome(
        success=True, stdout="****** Error: Failed to halt CPU\n", captured=True
    )
    qualified = flash_cmd._flow_d_reset_qualified_message(base, outcome)
    assert qualified == (
        "alif_mram_jlink[m55_he]: signed ATOC (app embedded) -> 0x8057F5B0 via "
        "J-Link (PART_PROFILE); verified; reset requested, core was busy and did "
        "not halt"
    )


def test_flow_d_reset_qualified_message_matches_cpu_is_not_halted_too():
    """The second marker: JLinkExe's OWN post-reset `g`/status line, not only
    the mid-transcript error banner -- either one alone is enough."""
    base = "alif_mram_jlink[m55_he]: app -> 0x80010000, signed ATOC -> 0x8057F5B0 via J-Link (PART_PROFILE); verified and PIN-reset"
    outcome = flash_cmd._Outcome(success=True, stdout="", stderr="CPU is not halted\n", captured=True)
    qualified = flash_cmd._flow_d_reset_qualified_message(base, outcome)
    assert "CPU is not halted" not in qualified
    assert "reset requested, core was busy and did not halt" in qualified
    assert "PIN-reset" not in qualified


def test_flow_d_reset_qualified_message_untouched_without_the_reset_tail():
    """A message that never carried `verified and PIN-reset` in the first
    place (any other backend's `ok_message`) passes through unchanged,
    regardless of what the transcript says -- the substring guard, not a
    method check, is what scopes this."""
    message = "swd_probe[cm7]: gd32g553 flashed via J-Link @ 0x00000000"
    outcome = flash_cmd._Outcome(
        success=True, stdout="****** Error: Failed to halt CPU\n", captured=True
    )
    assert flash_cmd._flow_d_reset_qualified_message(message, outcome) == message


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

# ── tan-cli#540: swd_probe's J-Link arm must not claim a flash it never saw ──


def _swd_probe_run(
    tmp_path, monkeypatch, *, stdout: str = "", stderr: str = "", firmware: str = "zephyr.bin"
):
    """A confirmed, real `swd_probe` J-Link write whose spawn reports success
    and whatever transcript the caller wants -- the shape tan-cli#540 is
    about. Same manifest/PATH scaffolding the `expect_dpidr` unarmed pair
    above uses; only `_spawn`'s stdout/stderr differ.

    `firmware` selects which of the two arms of `jlink_commander_script` the
    write takes, and that is the whole axis tan-cli#540's real fix turns on: a
    raw `.bin` takes `loadbin`+`verifybin` and CAN be verified, an ELF/HEX
    takes `loadfile` and (J-Link Commander having no `verifybin` that works
    without an address) cannot."""
    (tmp_path / "build").mkdir()
    (tmp_path / "sdk" / "scripts").mkdir(parents=True)
    (tmp_path / "sdk" / "scripts" / "alp_project.py").write_text("", encoding="utf-8")

    manifest = """schema_version: 1
hw_info: {sku: S}
slices: []
helper_mcus:
- {name: gd32_bridge, chip: gd32g553, firmware_path: FIRMWARE,
   flash_method: swd_probe, flash_args: {base: "0x08000000"}}
boot_order: []
""".replace("FIRMWARE", firmware)
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
        flash_cmd,
        "_spawn",
        lambda *_a, **_k: flash_cmd._Outcome(success=True, stdout=stdout, stderr=stderr),
    )
    return flash_cmd._run(
        app_path=".", build_root_arg=None, sdk_root_arg=str(tmp_path / "sdk"),
        board_yaml=None, core=None, helper=None, dry_run=False,
        skip_missing_tools=False, capture=True, cwd=str(tmp_path),
    )


#: The tail of a real bench J-Link session whose reset chain could not halt the
#: core, verbatim from tan-cli#522's own measurement on E1M-AEN801 silicon.
#: JLinkExe still exits 0 through all of it, with `-ExitOnError 1` on the argv.
_HALT_FAILURE_TRANSCRIPT = (
    "RSetType 2\n"
    "r\n"
    "VC_CORERESET did not halt CPU\n"
    "WARNING: CPU could not be halted\n"
    "****** Error: Failed to halt CPU\n"
    "g\n"
    "CPU is not halted\n"
)


def test_swd_probe_bin_write_reads_back_the_bytes_it_wrote(tmp_path):
    """tan-cli#540 defect 2, the REAL fix. `jlink_commander_script` gave this
    arm `r`/`halt`, the load, optionally `r`/`g` and `qc` -- and no
    `verifybin` anywhere, so a successful flash was inferred from JLinkExe's
    exit code alone. #522 measured on real E1M-AEN801 silicon that a halt
    failure does NOT make JLinkExe exit non-zero even with `-ExitOnError 1`
    (this arm carries it), so the exit code cannot tell a landed write from
    one that wrote nothing at all.

    The read-back is `verifybin`, in the same line shape Flow D
    (`plan_alif_mram_jlink`) has always emitted -- `verifybin <path> <addr>`,
    the form this repo has actually run on silicon -- and in the same place:
    AFTER the load, BEFORE the optional reset-and-go, because once `g` runs
    the core is executing and the memory being compared is no longer quiescent.

    Fails against `fix/540-541-flash-verify-and-tee` (measured: the script is
    `r` / `halt` / `loadbin ...` / `r` / `g` / `qc`, with no verify line)."""
    script = flash_plan.jlink_commander_script("/build/zephyr.bin", "0x08000000", True)
    lines = script.splitlines()

    assert "verifybin /build/zephyr.bin 0x08000000" in lines, script
    assert lines.index("loadbin /build/zephyr.bin, 0x08000000") < lines.index(
        "verifybin /build/zephyr.bin 0x08000000"
    ), script
    # ... and the verify is the LAST thing before the reset-and-go pair.
    assert lines[lines.index("verifybin /build/zephyr.bin 0x08000000") + 1 :] == [
        "r",
        "g",
        "qc",
    ], script


def test_swd_probe_verify_survives_a_spaced_path_and_reset_being_off(tmp_path):
    """The verify line goes through the same `commander_path` conditional
    quoting the load line does (tan-cli#369: an unquoted `C:\\Program
    Files\\...` truncates at `C:\\Program`), and it is emitted whether or not
    the manifest asked for the post-write reset -- `reset: false` turns off
    `r`/`g`, not the read-back."""
    script = flash_plan.jlink_commander_script(
        "C:\\Program Files\\alp\\build\\zephyr.bin", "0x08000000", False
    )

    assert 'verifybin "C:\\Program Files\\alp\\build\\zephyr.bin" 0x08000000' in script
    assert script.splitlines()[-1] == "qc"
    assert "\ng\n" not in script


def test_an_elf_load_gets_no_verify_line_because_none_can_be_emitted(tmp_path):
    """GUARD -- passes before and after, and says why. The `loadfile` arm takes
    NO address (`base` is a load offset, meaningful only for a raw binary --
    tan-cli#487), and `verifybin` is defined as `<file>, <addr>`: there is no
    address to give it. J-Link Commander's own `verifyfile` is NOT emitted
    here on purpose -- no call site in this repo has ever issued it, so
    nothing has measured that this DLL/Commander version accepts it, and with
    `-ExitOnError 1` on the argv an unrecognised command would turn every
    working ELF flash into a hard failure. Inventing tool behaviour is exactly
    what the SDK's own I-26 rule forbids. The ELF/HEX arm therefore stays
    genuinely unverifiable, which is why the `flash.swd-probe-write-
    unconfirmed` advisory below still exists for it."""
    script = flash_plan.jlink_commander_script("/build/zephyr.elf", "0x08000000", True)

    assert "loadfile /build/zephyr.elf" in script
    assert "verify" not in script


def test_a_verified_bin_write_says_verified_even_when_the_core_did_not_halt(
    tmp_path, monkeypatch
):
    """The claim `verifybin` buys. With the read-back in the script, a `.bin`
    write that exits 0 has had its bytes COMPARED against the artefact, so
    `flashed and verified` is an observation -- and the `flash.swd-probe-
    write-unconfirmed` advisory, whose text says outright that "this backend
    runs no verifybin", is now false here and must not fire.

    What the halt failure still costs is the RESET half, exactly as on Flow D
    (tan-cli#522): the bytes are on the part, but the core was never taken
    through `r`/`g`, so the target may still be running the OLD firmware. That
    is what the message now says.

    Fails against `fix/540-541-flash-verify-and-tee` (measured: the message
    reads `... write attempted via J-Link @ 0x08000000; the core did not halt
    ... and this backend runs no verifybin ...`, and the advisory fires)."""
    exit_code, data, issues, lines, _sdk = _swd_probe_run(
        tmp_path, monkeypatch, stdout=_HALT_FAILURE_TRANSCRIPT
    )

    assert exit_code == 0
    entry = data["entries"][0]
    assert entry["status"] == "ok", entry
    assert "flashed and verified via J-Link" in entry["message"], entry
    assert "write attempted" not in entry["message"], entry
    # The observation is still quoted, not paraphrased -- and it is scoped to
    # what the halt failure actually put in doubt.
    assert "Failed to halt CPU" in entry["message"], entry
    assert "may still be running the firmware it had" in entry["message"], entry
    # #402's device and #487's address halves both survive the rewording.
    assert "GD32G553MEY7TR" in entry["message"], entry
    assert "0x08000000" in entry["message"], entry
    # The write IS confirmed now, so the unconfirmed advisory must be silent.
    assert not any(i.code == "flash.swd-probe-write-unconfirmed" for i in issues)
    assert not any("UNCONFIRMED" in line for line in lines), lines


def test_swd_probe_write_into_a_core_that_never_halted_is_not_claimed_as_flashed(
    tmp_path, monkeypatch
):
    """tan-cli#540's headline, on the arm that still cannot verify. An ELF/HEX
    load takes `loadfile`, which this backend has no read-back for (see
    `test_an_elf_load_gets_no_verify_line_because_none_can_be_emitted`), so
    the `flashed` claim there rests on the exit code alone -- and #522 proved
    the exit code does not reflect the halt. The qualification is what keeps
    that honest.

    GUARD for this change (the wording was landed by
    `fix/540-541-flash-verify-and-tee`); retargeted from the `.bin` arm, which
    now genuinely verifies. No address assertion: `loadfile` never received
    one, and tan-cli#487 defect 6 is precisely about not naming an address the
    tool never got."""
    exit_code, data, _issues, _lines, _sdk = _swd_probe_run(
        tmp_path, monkeypatch, stdout=_HALT_FAILURE_TRANSCRIPT, firmware="zephyr.elf"
    )

    assert exit_code == 0
    entry = data["entries"][0]
    assert entry["status"] == "ok", entry
    # The bare claim is gone -- and the message says what was observed.
    assert "flashed via J-Link" not in entry["message"], entry
    assert "write attempted via J-Link" in entry["message"], entry
    assert "Failed to halt CPU" in entry["message"], entry
    assert "no verifybin" in entry["message"], entry
    # The resolved device survives the swap: #402 fixed that half of this same
    # string and it may not regress.
    assert "GD32G553MEY7TR" in entry["message"], entry


def test_swd_probe_unconfirmed_write_warns_in_json_and_in_default_text(tmp_path, monkeypatch):
    """The machine-readable and operator-readable halves of tan-cli#540's
    acceptance: an `ok` entry whose prose carries a caveat is not something a
    `--format json` consumer can key off, and a bench operator does not pass
    `--format json` at all -- `_run`'s caller prints only `text_lines` in the
    DEFAULT mode.

    GUARD for this change, retargeted to the ELF arm: the advisory is NOT
    deleted by the verify, it is narrowed to the path that genuinely cannot
    verify. A path that cannot check its own write still needs to say so."""
    _exit_code, _data, issues, lines, _sdk = _swd_probe_run(
        tmp_path, monkeypatch, stderr=_HALT_FAILURE_TRANSCRIPT, firmware="zephyr.elf"
    )

    warnings = [i for i in issues if i.code == "flash.swd-probe-write-unconfirmed"]
    assert len(warnings) == 1, issues
    assert warnings[0].severity == "warning"
    # Not an error: the write may well have landed, and there is no bench
    # evidence that a GD32 halt failure means a failed write.
    assert not any(i.code == "flash.entry-failed" for i in issues)
    assert any("UNCONFIRMED" in line for line in lines), lines


def test_swd_probe_clean_write_is_untouched_by_the_qualification(tmp_path, monkeypatch):
    """The negative control. A `swd_probe` J-Link write whose transcript names
    no halt failure carries the plain claim and raises no warning -- the
    qualification is a targeted substring swap driven by an observed marker,
    not a blanket downgrade of every swd_probe success.

    The claim itself moved with the fix: a `.bin` write now runs `verifybin`,
    so `flashed and verified` is what the run actually did. Fails against
    `fix/540-541-flash-verify-and-tee`, which says only `flashed`."""
    _exit_code, data, issues, lines, _sdk = _swd_probe_run(
        tmp_path, monkeypatch, stdout="Downloading file [zephyr.bin]...\nO.K.\n"
    )

    entry = data["entries"][0]
    assert entry["message"] == (
        "swd_probe[gd32_bridge]: GD32G553MEY7TR flashed and verified via J-Link @ 0x08000000"
    )
    assert not any(i.code == "flash.swd-probe-write-unconfirmed" for i in issues)
    assert not any("UNCONFIRMED" in line for line in lines), lines


def test_an_unverifiable_elf_write_keeps_its_plain_claim_when_nothing_went_wrong(
    tmp_path, monkeypatch
):
    """GUARD. The ELF/HEX arm's claim is unchanged byte-for-byte by this fix
    -- no verify line was added there, so nothing new can be claimed. Pins
    that the `.bin` arm's new `and verified` did NOT leak across the split."""
    _exit_code, data, _issues, _lines, _sdk = _swd_probe_run(
        tmp_path, monkeypatch, stdout="O.K.\n", firmware="zephyr.elf"
    )

    assert data["entries"][0]["message"] == (
        "swd_probe[gd32_bridge]: GD32G553MEY7TR flashed via J-Link"
    )


def test_the_halt_qualification_does_not_reach_the_openocd_arm(tmp_path, monkeypatch):
    """`swd_probe`'s OTHER arm neither emits JLinkExe's halt phrases nor makes
    the `flashed via J-Link` claim, so the qualification is gated on the
    J-Link arm having actually been taken -- the same
    `swd_probe_took_jlink_arm` local tan-cli#520's review hoisted for the
    DPIDR preflight, reused rather than duplicated. Guards against a future
    edit widening the gate to `method == "swd_probe"`, which would fire on an
    openocd transcript that merely happened to carry the phrase."""
    (tmp_path / "build").mkdir()
    (tmp_path / "sdk" / "scripts").mkdir(parents=True)
    (tmp_path / "sdk" / "scripts" / "alp_project.py").write_text("", encoding="utf-8")

    manifest = """schema_version: 1
hw_info: {sku: S}
slices: []
helper_mcus:
- {name: gd32_bridge, chip: gd32g553, firmware_path: zephyr.bin,
   flash_method: swd_probe,
   flash_args: {base: "0x08000000", use_openocd: true, interface: cmsis-dap,
                target: gd32g5x}}
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
        flash_cmd,
        "_spawn",
        lambda *_a, **_k: flash_cmd._Outcome(success=True, stdout=_HALT_FAILURE_TRANSCRIPT),
    )

    _exit_code, data, issues, _lines, _sdk = flash_cmd._run(
        app_path=".", build_root_arg=None, sdk_root_arg=str(tmp_path / "sdk"),
        board_yaml=None, core=None, helper=None, dry_run=False,
        skip_missing_tools=False, capture=True, cwd=str(tmp_path),
    )

    entry = data["entries"][0]
    assert "flashed via openocd" in entry["message"], entry
    assert "write attempted via J-Link" not in entry["message"], entry
    assert not any(i.code == "flash.swd-probe-write-unconfirmed" for i in issues)


def test_flow_d_keeps_its_own_wording_and_never_takes_the_swd_probe_swap():
    """Flow D's halt-failure sentence (tan-cli#522) says `verified; reset
    requested, core was busy and did not halt`; `swd_probe`'s two arms say
    something different again. The three qualifications match different tails
    and must not bleed into each other.

    Tightened by this change: `_swd_probe_qualified_message` now returns
    Flow D's message BYTE-FOR-BYTE unchanged (and `write_unconfirmed=False`)
    rather than appending its own tail to it -- previously it recognised
    neither of its two claims in that string and appended anyway, which the
    old `startswith` assertion accepted."""
    from tan.commands.flash_cmd import (
        _Outcome,
        _flow_d_reset_qualified_message,
        _swd_probe_halt_markers,
        _swd_probe_qualified_message,
    )

    outcome = _Outcome(success=True, stdout=_HALT_FAILURE_TRANSCRIPT, captured=True)
    flow_d = "alif_mram_jlink[m55-he]: AE822 MRAM written; verified and PIN-reset"
    qualified = _flow_d_reset_qualified_message(flow_d, outcome)
    assert qualified.endswith("; verified; reset requested, core was busy and did not halt")
    # swd_probe's swap finds nothing to replace in Flow D's message, and Flow
    # D's finds nothing in either of swd_probe's.
    markers = _swd_probe_halt_markers(outcome)
    assert markers == ["Failed to halt CPU", "CPU is not halted"]
    assert _swd_probe_qualified_message(flow_d, markers) == (flow_d, False)
    assert (
        _flow_d_reset_qualified_message("swd_probe[b]: X flashed via J-Link @ 0x0", outcome)
        == "swd_probe[b]: X flashed via J-Link @ 0x0"
    )
    assert (
        _flow_d_reset_qualified_message(
            "swd_probe[b]: X flashed and verified via J-Link @ 0x0", outcome
        )
        == "swd_probe[b]: X flashed and verified via J-Link @ 0x0"
    )


# ── tan-cli#541: the tee must not cost a flash tool its tty ─────────────────


#: Asks the CHILD what it sees. `_spawn` is what stands between it and the
#: console, so this one line is the entire measurement tan-cli#541 is about.
_ISATTY_PROBE = (
    "import sys; "
    "print('stdout_isatty=%s stderr_isatty=%s' % "
    "(sys.stdout.isatty(), sys.stderr.isatty()))"
)


def _driver_env(tan_module) -> dict[str, str]:
    """The environment for a subprocess that must import `tan` for real.

    Built from `REAL_ENVIRON` (captured at collection time in
    `tests/conftest.py`), NOT from `os.environ` inside a test body:
    `_scrub_sdk_discovery_env` has by then repointed `HOME` at a pytest tmp
    dir, and on a user-site install (`~/.local/lib/pythonX.Y/site-packages`)
    that alone is enough to make the child fail on `import typer` before it
    reaches a single line of `_spawn`. `PYTHONPATH` is then pinned at THIS
    tree's `python/` for the same reason `tan_under_test` pins it -- the child
    must measure this checkout's `flash_cmd`, never an installed one.

    `REAL_ENVIRON` is imported at this MODULE's top level, not here: importing
    `tests.conftest` creates a second module object whose own
    `REAL_ENVIRON = dict(os.environ)` runs at ITS import time, so an import
    inside a test body captures the environment `_scrub_sdk_discovery_env`
    has already rewritten -- the exact trap this helper exists to dodge,
    reintroduced one line lower. Measured: with the import here, the child
    still died on `import typer`."""
    env = dict(REAL_ENVIRON)
    repo_python = str(Path(tan_module.__file__).resolve().parents[1])
    existing = [p for p in env.get("PYTHONPATH", "").split(os.pathsep) if p]
    env["PYTHONPATH"] = os.pathsep.join(
        [repo_python, *(p for p in existing if p != repo_python)]
    )
    return env


def _spawn_under_a_pty(tmp_path, child_code: str, timeout: float = 30.0):
    """Drive `_spawn` from a process whose OWN stderr is a real pty, and hand
    back `(console_text, transcript)`.

    There is no way to fake this: `_spawn` decides on `sink.isatty()`, and
    pytest's `fd` capture makes `sys.stderr` a temp file, so a pty has to be
    created for tan itself. That is exactly the rig tan-cli#541's own
    measurement used (`origin/dev  child sees  stdout_isatty=True`)."""
    import pty as _pty

    import tan as _tan

    report = tmp_path / "transcript.txt"
    driver = tmp_path / "driver.py"
    driver.write_text(
        "import sys\n"
        "from tan.commands.flash_cmd import _spawn\n"
        f"out = _spawn([sys.executable, '-c', {child_code!r}], capture=False,"
        f" timeout={timeout})\n"
        "open(sys.argv[1], 'w', encoding='utf-8').write(out.stdout + out.stderr)\n",
        encoding="utf-8",
    )
    master_fd, slave_fd = _pty.openpty()
    proc = subprocess.Popen(
        [sys.executable, str(driver), str(report)],
        stdout=subprocess.DEVNULL,
        stderr=slave_fd,
        env=_driver_env(_tan),
    )
    os.close(slave_fd)
    console = b""
    try:
        while True:
            try:
                chunk = os.read(master_fd, 65536)
            except OSError:
                break  # the pty master's EOF, on Linux
            if not chunk:
                break
            console += chunk
    finally:
        os.close(master_fd)
    proc.wait(timeout=timeout)
    text = console.decode("utf-8", "replace")
    # The driver's own traceback would otherwise be swallowed by a bare
    # FileNotFoundError on the report, hiding the real reason (an import that
    # did not resolve, a `_spawn` that raised) behind a missing file.
    assert report.exists(), f"the pty driver wrote no transcript. Console said:\n{text}"
    return text, report.read_text(encoding="utf-8")


@pytest.mark.skipif(sys.platform == "win32", reason="pty is POSIX-only -- see _open_console_pty")
def test_a_flash_tool_sees_a_tty_again_when_tan_is_on_one(tmp_path):
    """tan-cli#541's headline, measured the way the issue measured it. Since
    `_Tee` landed, `_spawn` passed `subprocess.PIPE` for both of the child's
    streams, so the child no longer saw a tty and `pyocd`/`west`/`openocd`
    dropped the `\\r`-redrawn progress bar an operator watches through a
    multi-minute write. The live-console branch now tees through a pty.

    Fails against current `dev` (measured: `stdout_isatty=False
    stderr_isatty=False` on both the console and the transcript)."""
    console, transcript = _spawn_under_a_pty(tmp_path, _ISATTY_PROBE)

    assert "stdout_isatty=True stderr_isatty=True" in console, console
    # tan-cli#541 acceptance 2: the transcript tan-cli#522 needs is STILL
    # captured -- the pty buys the tty back without giving the evidence up.
    assert "stdout_isatty=True stderr_isatty=True" in transcript, transcript


@pytest.mark.skipif(sys.platform == "win32", reason="pty is POSIX-only -- see _open_console_pty")
def test_the_pty_transcript_still_feeds_the_flow_d_qualification(tmp_path):
    """tan-cli#541 acceptance 2, on the exact string that made the tee
    load-bearing: #522's qualification reads the transcript for `Failed to
    halt CPU`, and it has to keep finding it once the transcript arrives over
    a pty rather than over two pipes. `ONLCR` is cleared on the slave so the
    captured bytes carry plain `\\n`, not the `\\r\\n` a raw pty would insert
    -- a transcript that differs from the pipe path's for no reason a caller
    can see is its own defect.

    A GUARD, not a both-ways test: it passes against `dev` too, because there
    the same assertions describe the PIPE path. That equality IS the
    acceptance -- the transcript must come out the same whichever transport
    carried it -- and it is what would go red if the pty ever started
    reshaping what #522 reads."""
    console, transcript = _spawn_under_a_pty(
        tmp_path,
        "import sys\n"
        "print('****** Error: Failed to halt CPU')\n"
        "print('CPU is not halted', file=sys.stderr)\n",
    )

    assert "Failed to halt CPU" in transcript, transcript
    # Both streams land in `_Outcome.stdout` on a pty run (one device, one
    # tee) -- the merge `_tee_text` documents.
    assert "CPU is not halted" in transcript, transcript
    assert "\r\n" not in transcript, repr(transcript)
    assert "Failed to halt CPU" in console, console

    from tan.commands.flash_cmd import _Outcome, _flow_d_reset_qualified_message

    qualified = _flow_d_reset_qualified_message(
        "alif_mram_jlink[m55-he]: written; verified and PIN-reset",
        _Outcome(success=True, stdout=transcript),
    )
    assert qualified.endswith("core was busy and did not halt"), qualified


def test_a_non_terminal_sink_gets_no_pty(tmp_path):
    """tan-cli#541 acceptance 3, half one -- the decision itself, asserted
    against a sink this test OWNS.

    tan piped to a file, or running under CI, is where the pipe path is
    already RIGHT: there is no terminal to redraw on, the tool's
    non-interactive rendering is what belongs in the log, and a pty would only
    inject escape runs into a file nobody watches.

    A real file on disk, NOT `sys.stderr` (tan-cli#540/#541 review, minor 2).
    Reading the ambient `sys.stderr` made this test an assertion about how
    pytest was invoked rather than about `_open_console_pty`: under plain
    `pytest` the `fd` capture makes it a temp file and this passed, and under
    `pytest -s` it is the REAL terminal, so it failed (measured: `assert
    (<_io.BufferedReader name=5>, 6) is None`) -- and leaked the pty pair it
    had just been handed. A suite that is only honest under one set of flags
    is not a gate."""
    from tan.commands.flash_cmd import _open_console_pty

    with (tmp_path / "console.log").open("w", encoding="utf-8") as sink:
        assert sink.isatty() is False
        assert _open_console_pty(sink) is None


@pytest.mark.skipif(
    sys.stderr.isatty(),
    reason="needs a non-terminal stderr; `pytest -s` on a tty hands _spawn the pty path",
)
def test_a_non_terminal_sink_keeps_the_pipe_behaviour_unchanged():
    """tan-cli#541 acceptance 3, half two -- the CONSEQUENCE, end to end
    through `_spawn`: with no terminal to draw on, the child still sees no
    tty, both streams still come back in their own fields, and nothing about
    the pipe path moved.

    `_spawn` reads the ambient `sys.stderr` (via `_stderr_sink`) and there is
    no seam to inject a sink through, so this one genuinely depends on how
    pytest was invoked -- hence the skip rather than a rewrite. Under plain
    `pytest` (CI, and the default local run) the `fd` capture makes stderr a
    temp file and this runs; under `pytest -s` on a terminal it SKIPS with the
    reason naming exactly why, instead of failing.

    A GUARD, not a both-ways test: it passes identically before and after,
    which is the whole point (the issue asks for this behaviour to be
    unchanged, and nothing else here proves it stayed that way)."""
    from tan.commands.flash_cmd import _spawn

    outcome = _spawn(_stub(_ISATTY_PROBE), capture=False, timeout=30.0)
    assert outcome.success is True
    assert "stdout_isatty=False stderr_isatty=False" in outcome.stdout, outcome
    # Two tees, two fields -- the pipe path does NOT merge the streams.
    assert outcome.stderr == "", outcome


def test_open_console_pty_declines_rather_than_raising_when_it_cannot_have_one():
    """Every `None` arm of `_open_console_pty`, since each one is a place a
    flash could have become a traceback instead of a write: a sink with no
    `isatty` at all (an embedded capture object), a sink that answers `False`,
    and the host refusing to allocate (`pty.openpty` raising)."""
    from tan.commands import flash_cmd as fc

    class _NoIsatty:
        pass

    class _NotATty:
        def isatty(self):
            return False

    class _Tty:
        def isatty(self):
            return True

        def fileno(self):
            return sys.stderr.fileno()

    assert fc._open_console_pty(_NoIsatty()) is None
    assert fc._open_console_pty(_NotATty()) is None
    if fc.pty is not None:
        monkey = pytest.MonkeyPatch()
        try:
            monkey.setattr(fc.pty, "openpty", _raise_no_ptmx)
            assert fc._open_console_pty(_Tty()) is None
        finally:
            monkey.undo()


def _raise_no_ptmx():
    raise OSError("no /dev/ptmx")


@pytest.mark.skipif(sys.platform == "win32", reason="pty is POSIX-only -- see _open_console_pty")
def test_a_pty_run_still_returns_promptly_when_a_grandchild_holds_the_device(tmp_path):
    """The pty sibling of the `_Tee` bounded-join BLOCKER. A pty master raises
    `OSError(EIO)` at EOF where a pipe returns `b""`, and a backgrounded
    grandchild still holding the slave open means neither happens on time --
    `_Tee.join`'s `_DRAIN_JOIN_S` bound is what keeps `_spawn` returning
    either way. One tee, not two, so the overrun here is bounded by ONE
    `_DRAIN_JOIN_S`, not the pipe path's two.

    A GUARD, not a both-ways test: against `dev` it measures the pipe path's
    already-bounded join and passes. It exists because the pty introduces a
    NEW way for that bound to matter (EIO instead of `b""`), and an unbounded
    join here is the tan-cli#519 BLOCKER coming back through a different
    door."""
    import pty as _pty
    import time as _time

    import tan as _tan

    driver = tmp_path / "driver.py"
    report = tmp_path / "report.txt"
    driver.write_text(
        "import sys, time\n"
        "from tan.commands.flash_cmd import _spawn\n"
        "start = time.monotonic()\n"
        "out = _spawn(['sh', '-c', 'sleep 20 & echo done; exit 0'],"
        " capture=False, timeout=3.0)\n"
        "open(sys.argv[1], 'w', encoding='utf-8').write("
        "'%.2f\\n%s' % (time.monotonic() - start, out.stdout))\n",
        encoding="utf-8",
    )
    master_fd, slave_fd = _pty.openpty()
    started = _time.monotonic()
    proc = subprocess.Popen(
        [sys.executable, str(driver), str(report)],
        stdout=subprocess.DEVNULL,
        stderr=slave_fd,
        env=_driver_env(_tan),
    )
    os.close(slave_fd)
    try:
        while True:
            try:
                if not os.read(master_fd, 65536):
                    break
            except OSError:
                break
    finally:
        os.close(master_fd)
    proc.wait(timeout=60)
    elapsed = _time.monotonic() - started
    assert elapsed < 15.0, f"{elapsed:.2f}s -- the pty EOF never came and the join ran unbounded"
    inner, _, transcript = report.read_text(encoding="utf-8").partition("\n")
    assert float(inner) < 8.0, inner
    assert "done" in transcript, transcript


# ── tan-cli#540 review, MAJOR 1: the halt markers must be read POSITIONALLY ──


#: A WHOLE `swd_probe` J-Link session, in the order `jlink_commander_script`'s
#: own `r, halt, loadbin, r, g, qc` produces it -- established by RUNNING the
#: script through a capturing Commander stub on `PATH`, not by assuming an
#: order. The load is OBSERVED to finish (`Downloading file [...]` then `O.K.`)
#: and only THEN does the post-load `r`/`g` fail to halt the firmware that
#: just started running. That trailing `r`/`g` is ON BY DEFAULT
#: (`do_reset = _default(fa_bool_checked(fa, "reset"), True)`), so this is the
#: shape a shipped `E1M-V2N101` manifest with no `reset:` key produces.
_LOAD_THEN_RESET_FAILURE_TRANSCRIPT = (
    "SEGGER J-Link Commander V7.94 (Compiled Dec  6 2023 16:32:11)\n"
    "Connecting to target via SWD\n"
    "Reset: Halt core after reset via DEMCR.VC_CORERESET.\n"
    "Reset: Reset device via AIRCR.SYSRESETREQ.\n"
    "PC = 08000198, CycleCnt = 00000000\n"
    "Downloading file [/w/build/zephyr.bin]...\n"
    "J-Link: Flash download: Bank 0 @ 0x08000000, 1 range affected\n"
    "J-Link: Flash download: Total: 0.421s\n"
    "O.K.\n"
    "Reset: Halt core after reset via DEMCR.VC_CORERESET.\n"
    "VC_CORERESET did not halt CPU\n"
    "Reset: Reset device via AIRCR.SYSRESETREQ.\n"
    "WARNING: CPU could not be halted\n"
    "****** Error: Failed to halt CPU\n"
    "CPU is not halted\n"
)


def test_a_halt_failure_after_an_observed_load_does_not_doubt_the_write(
    tmp_path, monkeypatch
):
    """The false alarm. `jlink_commander_script` emits TWO halt-capable stages
    -- the pre-load `r`/`halt` and the post-load `r`/`g` -- and the post-load
    one is ON BY DEFAULT. A resident image that starts the instant `loadbin`
    finishes cannot be halted by that second `r`, so a COMPLETELY SUCCESSFUL
    flash prints `Failed to halt CPU` / `CPU is not halted` and exits 0.

    A positionless substring search over the whole transcript cannot tell that
    apart from a load into a core that never halted in the first place, so it
    told the operator to re-flash hardware on a write the transcript itself
    reports as `Downloading file [...] ... O.K.` -- and raised
    `flash.swd-probe-write-unconfirmed`, which alp-sdk-vscode renders as a
    warning. Reusing Flow D's RESET markers to doubt the WRITE is a materially
    stronger claim than the evidence supports; Flow D itself scopes them to
    its reset sentence for exactly that reason.

    Fails against this branch's own first cut (measured: `write attempted via
    J-Link`, plus the warning).

    The claim carries `and verified` because this is the `.bin` arm and
    tan-cli#540 defect 2 gave it a `verifybin` read-back; the point being
    pinned here is the POSITION rule, which is what keeps the advisory silent
    on the ELF/HEX arm too (see
    `test_the_elf_arm_gets_the_same_positional_reading_as_the_bin_arm`)."""
    _exit_code, data, issues, lines, _sdk = _swd_probe_run(
        tmp_path, monkeypatch, stdout=_LOAD_THEN_RESET_FAILURE_TRANSCRIPT
    )

    entry = data["entries"][0]
    assert entry["message"] == (
        "swd_probe[gd32_bridge]: GD32G553MEY7TR flashed and verified via J-Link @ 0x08000000"
    ), entry
    assert not any(i.code == "flash.swd-probe-write-unconfirmed" for i in issues), issues
    assert not any("UNCONFIRMED" in line for line in lines), lines


def test_a_halt_failure_before_the_load_still_doubts_the_write(tmp_path, monkeypatch):
    """The other half of the positional split, and the case tan-cli#540 is
    really about: the PRE-load `r`/`halt` could not stop the core, so the
    `loadfile` that follows never had a halted target to write into. The
    transcript records no completed download at all, so there is nothing that
    could confirm the bytes landed -- and this arm runs no `verifybin`. The
    claim must still be downgraded here.

    On the ELF/HEX arm, deliberately. #575 wrote this against the `.bin` arm,
    which at the time had no read-back either; tan-cli#540 defect 2 gave that
    arm one, so a `.bin` write reaching the `ok` path has had its bytes
    COMPARED whatever the halt markers say (`verifybin` + `-ExitOnError 1`
    makes a mismatch a non-zero exit). The arm that is still living on the
    transcript alone is this one, so this is where the downgrade has to be
    measured -- see
    `test_a_verified_bin_write_says_verified_even_when_the_core_did_not_halt`
    for the same transcript position on the arm that can verify."""
    _exit_code, data, issues, lines, _sdk = _swd_probe_run(
        tmp_path,
        monkeypatch,
        firmware="zephyr.elf",
        stdout=(
            "SEGGER J-Link Commander V7.94\n"
            "Reset: Halt core after reset via DEMCR.VC_CORERESET.\n"
            "VC_CORERESET did not halt CPU\n"
            "****** Error: Failed to halt CPU\n"
            "CPU is not halted\n"
        ),
    )

    entry = data["entries"][0]
    assert "write attempted via J-Link" in entry["message"], entry
    assert "Failed to halt CPU" in entry["message"], entry
    assert any(i.code == "flash.swd-probe-write-unconfirmed" for i in issues), issues
    assert any("UNCONFIRMED" in line for line in lines), lines


def test_a_halt_failure_before_the_load_is_carried_by_the_verify_on_the_bin_arm(
    tmp_path, monkeypatch
):
    """The `.bin` half of the case above, and the one place the two fixes have
    to be read TOGETHER. The pre-load halt failed, so #575's positional rule
    keeps every marker counting and the write is in doubt on the transcript
    alone -- but this arm no longer lives on the transcript alone. `verifybin`
    + `-ExitOnError 1` means a run that reaches the `ok` path at all has had
    its bytes compared against the artefact, so the position of the marker
    changes the WORDING (what the halt cost) and not the VERDICT (the bytes
    landed).

    Fails against `fix/540-541-flash-verify-and-tee` (measured: `write
    attempted via J-Link ... this backend runs no verifybin`, plus the
    advisory)."""
    _exit_code, data, issues, lines, _sdk = _swd_probe_run(
        tmp_path,
        monkeypatch,
        stdout=(
            "SEGGER J-Link Commander V7.94\n"
            "Reset: Halt core after reset via DEMCR.VC_CORERESET.\n"
            "VC_CORERESET did not halt CPU\n"
            "****** Error: Failed to halt CPU\n"
            "CPU is not halted\n"
        ),
    )

    entry = data["entries"][0]
    assert entry["status"] == "ok", entry
    assert "flashed and verified via J-Link" in entry["message"], entry
    assert "write attempted" not in entry["message"], entry
    assert "Failed to halt CPU" in entry["message"], entry
    assert "may still be running the firmware it had" in entry["message"], entry
    assert not any(i.code == "flash.swd-probe-write-unconfirmed" for i in issues), issues
    assert not any("UNCONFIRMED" in line for line in lines), lines


def test_a_download_that_never_reports_completing_is_not_treated_as_observed(
    tmp_path, monkeypatch
):
    """The boundary is the load COMPLETING, not the load STARTING. A
    transcript that opens a download and then reports a halt failure without
    ever printing the completion token has observed nothing about the bytes,
    so the conservative verdict has to survive -- the positional rule must not
    become 'the word Downloading appeared, therefore it worked'.

    Measured on the ELF/HEX arm, for the same reason as the test above: after
    tan-cli#540 defect 2 the `.bin` arm's verdict comes from `verifybin`, not
    from the transcript, so the arm that can still be moved by a truncated
    transcript is this one."""
    _exit_code, data, issues, _lines, _sdk = _swd_probe_run(
        tmp_path,
        monkeypatch,
        firmware="zephyr.elf",
        stdout=(
            "Downloading file [/w/build/zephyr.elf]...\n"
            "****** Error: Failed to halt CPU\n"
            "CPU is not halted\n"
        ),
    )

    assert "write attempted via J-Link" in data["entries"][0]["message"]
    assert any(i.code == "flash.swd-probe-write-unconfirmed" for i in issues), issues


def test_the_elf_arm_gets_the_same_positional_reading_as_the_bin_arm(tmp_path, monkeypatch):
    """`loadfile` (the ELF/HEX arm) prints the same `Downloading file [...]`
    /`O.K.` pair `loadbin` does, and it is the arm tan-cli#540 defect 2 does
    NOT reach -- the `verifybin` read-back lands on the `.bin` side only, so
    ELF/HEX keeps living on the transcript alone. The positional reading
    therefore has to stand on its own here, and this is the case the review of
    #575 named directly: *"It does not remove it for ELF/HEX, where there is
    still no verify and the marker search is still positionless. Fix the
    detector, or scope it to markers emitted before the load completes."*

    So a post-load halt failure must NOT raise the advisory on this arm
    either. The advisory says `nothing confirms the bytes landed`; a marker
    printed AFTER the tool itself reported `Downloading file [...] ... O.K.`
    is evidence about the RESET, and using it to doubt the write is the exact
    false alarm #575 removed -- narrowing it to one arm would not have made it
    true there. What this arm's inability to verify costs is the PRE-load
    case, which still downgrades (see
    `test_a_halt_failure_before_the_load_still_doubts_the_write`).

    Now actually driven through `loadfile`: #575 wrote this against a
    `zephyr.elf` TRANSCRIPT but left the fixture's default `zephyr.bin`
    artefact in place, so it measured the `.bin` arm."""
    _exit_code, data, issues, lines, _sdk = _swd_probe_run(
        tmp_path,
        monkeypatch,
        firmware="zephyr.elf",
        stdout=(
            "Downloading file [/w/build/zephyr.elf]...\n"
            "O.K.\n"
            "VC_CORERESET did not halt CPU\n"
            "****** Error: Failed to halt CPU\n"
            "CPU is not halted\n"
        ),
    )

    # The unverifiable arm's plain claim, byte-for-byte -- no address, because
    # `loadfile` never received one (tan-cli#487 defect 6).
    assert data["entries"][0]["message"] == (
        "swd_probe[gd32_bridge]: GD32G553MEY7TR flashed via J-Link"
    ), data["entries"][0]
    assert not any(i.code == "flash.swd-probe-write-unconfirmed" for i in issues), issues
    assert not any("UNCONFIRMED" in line for line in lines), lines


def test_flow_d_still_reads_its_markers_positionlessly(tmp_path):
    """The positional rule is scoped to the WRITE claim. Flow D's own
    qualification is about the RESET, which is precisely the stage these
    markers belong to, so it must keep matching them after the load -- the
    fix must not quietly disarm tan-cli#522."""
    from tan.commands.flash_cmd import _Outcome, _flow_d_reset_qualified_message

    outcome = _Outcome(
        success=True, stdout=_LOAD_THEN_RESET_FAILURE_TRANSCRIPT, captured=True
    )
    qualified = _flow_d_reset_qualified_message(
        "alif_mram_jlink[m55-he]: AE822 MRAM written; verified and PIN-reset", outcome
    )
    assert qualified.endswith("; verified; reset requested, core was busy and did not halt")


# ── tan-cli#541 review, MAJOR 2: the pty must not degrade the diagnostic ─────


def test_capture_tail_collapses_carriage_return_redraws_and_strips_colour():
    """The pty's cost to the CUSTOMER-VISIBLE string, fixed at the one place
    that composes it.

    `_capture_tail` feeds both the text-mode `FAIL:` line and
    `data.entries[].message`, and it split with `str.splitlines()`, which
    splits on `\\r` as well as `\\n`. A `\\r`-redrawn progress bar is ONE line
    on the terminal and N lines to `splitlines()`, so on the pty path -- where
    the child now (correctly) draws one, because it can see a tty again --
    three of the four slots went to redraws of the same bar and the tool's
    colour arrived as literal `\\x1b[31m` inside a string a customer reads.

    Measured on the branch before this fix, same child, same `_spawn`, driven
    from a process whose stderr is a real pty:

        'swd_probe[gd32_bridge]: [100%] writing image |
         \\x1b[31mError: flash algo timed out\\x1b[0m |
         target reported SWD fault at 0x08000000 |
         the image on the device may be partial'
    """
    from tan.commands.flash_cmd import _Outcome, _capture_tail

    outcome = _Outcome(
        success=False,
        returncode=1,
        stdout=(
            "\r[ 40%] writing image"
            "\r[ 70%] writing image"
            "\r[100%] writing image\n"
            "\x1b[31mError: flash algo timed out\x1b[0m\n"
            "  target reported SWD fault at 0x08000000\n"
            "  the image on the device may be partial\n"
        ),
    )

    tail = _capture_tail(outcome)

    assert "\x1b" not in tail, repr(tail)
    assert "\r" not in tail, repr(tail)
    # The bar is ONE line, the way the terminal drew it -- so it costs one
    # slot, not three, and the whole diagnosis survives.
    assert tail == (
        "[100%] writing image | Error: flash algo timed out"
        " |   target reported SWD fault at 0x08000000"
        " |   the image on the device may be partial"
    ), repr(tail)


def test_capture_tail_strips_the_erase_line_and_cursor_moves_a_bar_leaves_behind():
    """Not just SGR colour. A progress bar erases and repositions with
    `\\x1b[K`/`\\x1b[1G`/`\\x1b[?25l`, and any of those reaching
    `data.entries[].message` is the same defect wearing a different escape."""
    from tan.commands.flash_cmd import _Outcome, _capture_tail

    outcome = _Outcome(
        success=False,
        returncode=1,
        stdout="\x1b[?25l\x1b[1G[100%]\x1b[K done\n\x1b[0mError: target lost\n",
    )

    tail = _capture_tail(outcome)

    assert tail == "[100%] done | Error: target lost", repr(tail)


def test_capture_tail_keeps_a_lone_progress_bar_rather_than_reporting_nothing():
    """The degenerate case the collapse must not create: a child whose ENTIRE
    output is one `\\r`-redrawn bar and no newline at all. Collapsing to the
    final segment must still leave something to report -- an empty tail would
    fall through to the bare `exited rc=` sentence and lose what little the
    tool did say."""
    from tan.commands.flash_cmd import _Outcome, _capture_tail

    outcome = _Outcome(success=False, returncode=2, stdout="\r[ 10%]\r[ 55%] stalled")

    assert _capture_tail(outcome) == "[ 55%] stalled"


def test_capture_tail_is_unchanged_for_a_plain_transcript():
    """The negative control: a transcript with no `\\r` and no escapes comes
    out byte-for-byte as it did before, so the sanitisation is a targeted
    repair of the pty path and not a rewrite of every message tan prints."""
    from tan.commands.flash_cmd import _Outcome, _capture_tail

    outcome = _Outcome(
        success=False,
        returncode=1,
        stderr="one\ntwo\nthree\nfour\nfive\n",
    )

    assert _capture_tail(outcome) == "two | three | four | five"


@pytest.mark.skipif(sys.platform == "win32", reason="pty is POSIX-only -- see _open_console_pty")
def test_the_pty_path_reports_the_whole_diagnosis_with_no_escapes(tmp_path):
    """The same measurement end-to-end, through the real `_spawn` on a real
    pty, on the exact string a customer reads. The child gates its bar and its
    colour on `isatty()` exactly as `pyocd`/`openocd`/`west` do, so this is
    the pty path doing what tan-cli#541 asked for -- and the message still has
    to come out clean."""
    child = (
        "import sys\n"
        "if sys.stdout.isatty():\n"
        "    for pct in (40, 70, 100):\n"
        "        sys.stdout.write('\\r[%d%%] writing image' % pct)\n"
        "        sys.stdout.flush()\n"
        "    sys.stdout.write('\\n')\n"
        "red, off = ('\\x1b[31m', '\\x1b[0m') if sys.stderr.isatty() else ('', '')\n"
        "sys.stderr.write('%sError: flash algo timed out%s\\n' % (red, off))\n"
        "sys.stderr.write('  target reported SWD fault at 0x08000000\\n')\n"
        "sys.stderr.write('  the image on the device may be partial\\n')\n"
        "sys.exit(1)\n"
    )
    _console, transcript = _spawn_under_a_pty(tmp_path, child)

    from tan.commands.flash_cmd import _Outcome, _execute_message

    message = _execute_message(
        _Outcome(success=False, returncode=1, stdout=transcript), "swd_probe", "gd32_bridge"
    )

    assert "\x1b" not in message, repr(message)
    assert "\r" not in message, repr(message)
    for line in (
        "Error: flash algo timed out",
        "target reported SWD fault at 0x08000000",
        "the image on the device may be partial",
    ):
        assert line in message, repr(message)


# ── tan-cli#575 review: `\r\n` is a LINE ENDING, not a progress redraw ───────
#
# The redraw collapse above erased EVERY line of a Windows transcript. These
# cases are written against explicit byte sequences rather than against a real
# Windows child, because this box is Linux and the host's line-ending
# translation would make them vacuous: `subprocess.run(text=True)` folds
# `\r\n` to `\n` before tan ever sees it, and the live-console `_Tee` -- the
# branch that DOES deliver `\r\n` intact, since it decodes the raw pipe itself
# -- only ever sees `\r\n` when the CHILD wrote it, which on Linux it does not.
# `_console_lines` is pure, so feeding it the exact string a Windows child
# produces exercises the identical code path on either host. The Windows CI
# leg (`pytest across python/ (windows-latest)`), which is where this defect
# surfaced, is the end-to-end confirmation.


def test_console_lines_keeps_a_crlf_terminated_line_instead_of_erasing_it():
    """The defect, at the pure function. A Windows child ends its lines with
    `\\r\\n`; splitting on `\\n` alone leaves every row ending in `\\r`, and the
    segment after that LAST `\\r` is the empty string -- so every row failed
    the blank test and the whole transcript vanished.

    Measured on the source before this fix:

        _console_lines('Error: could not connect to target\\r\\n')  ->  []

    which is why the Windows CI leg saw `_capture_tail` fall through to the
    bare `swd_probe[e1]: exited rc=3`. Not a test artefact: it blanks the
    flash failure diagnosis for every Windows operator, the exact surface
    tan-cli#541's MAJOR 2 exists to protect."""
    from tan.commands.flash_cmd import _console_lines

    assert _console_lines("Error: could not connect to target\r\n") == [
        "Error: could not connect to target"
    ]
    # A whole multi-line diagnosis, not just one row.
    assert _console_lines(
        "Error: flash algo timed out\r\n"
        "  target reported SWD fault at 0x08000000\r\n"
        "  the image on the device may be partial\r\n"
    ) == [
        "Error: flash algo timed out",
        "  target reported SWD fault at 0x08000000",
        "  the image on the device may be partial",
    ]


def test_console_lines_collapses_a_redraw_on_a_crlf_stream_without_losing_the_lines():
    """Both rules at once, on ONE stream -- the case that proves the CRLF fix
    was not bought by disabling the redraw collapse.

    A Windows `pyocd`/`west` draws its bar with bare `\\r` (nothing is drawn
    after the last one on that row, so the row shows its final state) and ends
    each completed row with `\\r\\n` (a terminator, which erases nothing). The
    bar must still cost ONE slot AND the CRLF-terminated lines must survive."""
    from tan.commands.flash_cmd import _console_lines

    assert _console_lines("[40%] x\r[70%] x\r[100%] x\r\n Error: y\r\n") == [
        "[100%] x",
        " Error: y",
    ]
    # And with the tool's colour on it, as a real Windows console tool emits.
    assert _console_lines(
        "\r[ 40%] writing image"
        "\r[100%] writing image\r\n"
        "\x1b[31mError: could not connect to target\x1b[0m\r\n"
    ) == ["[100%] writing image", "Error: could not connect to target"]


def test_console_lines_is_unchanged_for_an_lf_only_redraw():
    """The negative control: the Linux/pty shape tan-cli#575 measured must come
    out byte-for-byte as it did before, so this is a repair of the Windows path
    and not a re-litigation of the collapse. #575 measured

        'swd_probe[gd32_bridge]: [100%] writing image
         | Error: could not connect to target'

    and that still holds, end to end through `_execute_message`."""
    from tan.commands.flash_cmd import _Outcome, _console_lines, _execute_message

    transcript = (
        "\r[ 40%] writing image"
        "\r[ 70%] writing image"
        "\r[100%] writing image\n"
        "Error: could not connect to target\n"
    )

    assert _console_lines(transcript) == [
        "[100%] writing image",
        "Error: could not connect to target",
    ]
    message = _execute_message(
        _Outcome(success=False, returncode=1, stdout=transcript), "swd_probe", "gd32_bridge"
    )
    assert message == (
        "swd_probe[gd32_bridge]: [100%] writing image | Error: could not connect to target"
    ), repr(message)


def test_console_lines_keeps_a_row_whose_trailing_carriage_return_drew_nothing():
    """A bare `\\r` at the very end of the input, with no `\\n` after it.

    The reading pinned here: the row SURVIVES. A `\\r` returns the cursor to
    column 0 and erases nothing by itself -- only what is drawn AFTER it
    overwrites anything -- so a transcript that stops there is still showing
    that row on the operator's terminal. This is not a hypothetical shape: it
    is a tool killed on `_FLASH_TIMEOUT_S`, or one whose last bar update never
    got its newline out, and reading the trailing `\\r` as an erasure would
    throw away the diagnosis of the very run that failed."""
    from tan.commands.flash_cmd import _Outcome, _capture_tail, _console_lines

    assert _console_lines("Error: could not connect to target\r") == [
        "Error: could not connect to target"
    ]
    # A redraw that ends the same way still collapses to its final state.
    assert _console_lines("[40%] x\r[70%] x\r") == ["[70%] x"]
    # And a `\r` that drew nothing cannot erase a row that a real one already
    # redrew (`a\r\r\n` is one row reading `a`).
    assert _console_lines("a\r\r\n") == ["a"]
    assert (
        _capture_tail(_Outcome(success=False, returncode=2, stdout="Error: target lost\r"))
        == "Error: target lost"
    )


def test_capture_tail_surfaces_a_windows_shaped_transcript_not_the_bare_rc():
    """The CI failure itself, at the consumer. `test_execute_message_text_mode_
    now_surfaces_a_real_spawn_diagnosis` spawns a real child, so on Linux it
    can only ever produce `\\n`; this feeds the bytes that child produces on
    WINDOWS -- where `_Tee` decodes the raw pipe itself and so hands `\\r\\n`
    straight through -- to the same two functions.

    Before this fix `_capture_tail` returned `None`-shaped nothing here and
    `_execute_message` reported `swd_probe[e1]: exited rc=3`."""
    from tan.commands.flash_cmd import _Outcome, _capture_tail, _execute_message

    outcome = _Outcome(
        success=False, returncode=3, stderr="Error: could not connect to target\r\n"
    )

    assert _capture_tail(outcome) == "Error: could not connect to target"
    assert (
        _execute_message(outcome, "swd_probe", "e1")
        == "swd_probe[e1]: Error: could not connect to target"
    )
