# SPDX-License-Identifier: Apache-2.0
"""`tan renode` command-level tests: the IO/envelope framing pure logic tests
cannot reach.

Driven as a REAL SUBPROCESS against a tiny standalone Typer app that wraps
`tan.commands.renode_cmd.renode` directly, rather than `python -m tan renode`
-- `renode` is registered in `tan/cli.py` by a separate parallel task (this
module's docstring explains the scope cut), so a `python -m tan renode`
invocation is not yet wired. The harness app is BYTE-FOR-BYTE what `tan.cli`
would run once `app.command("renode")(renode)` lands: same Typer command
object, same envelope/exit-code plumbing, so every assertion here still holds
once that registration is added -- only the invocation prefix changes.

Every envelope shape below was diff-verified against the shipped `tan.exe`
oracle by hand while writing this port (see the module docstring in
`renode_cmd.py`), including a byte-for-byte match on the `renode.binary-
missing` refusal this file pins.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[2]

_HARNESS = """
import typer
from tan.commands.renode_cmd import renode
app = typer.Typer(add_completion=False)
app.command()(renode)
app()
"""

OK_MANIFEST = """schema_version: 1
hw_info:
  sku: E1M-AEN801
slices:
- core_id: m55_hp
  os: zephyr
  status: pending
  build_dir: m55_hp-zephyr
"""


def _scaffold(work: Path, *, manifest: str | None = OK_MANIFEST, with_elf: bool = False,
              with_descriptors: bool = False) -> None:
    (work / "sdk" / "scripts").mkdir(parents=True, exist_ok=True)
    (work / "sdk" / "scripts" / "alp_project.py").write_text("", encoding="utf-8")
    (work / "build").mkdir(exist_ok=True)
    if manifest is not None:
        (work / "build" / "system-manifest.yaml").write_text(
            manifest, encoding="utf-8", newline=""
        )
    if with_elf:
        elf_dir = work / "build" / "m55_hp-zephyr" / "zephyr"
        elf_dir.mkdir(parents=True, exist_ok=True)
        (elf_dir / "zephyr.elf").write_bytes(b"")
    if with_descriptors:
        renode_dir = work / "sdk" / "metadata" / "renode"
        renode_dir.mkdir(parents=True, exist_ok=True)
        (renode_dir / "alif_ensemble_e8.repl").write_bytes(b"")
        (renode_dir / "alif_ensemble_e8.resc").write_bytes(b"")


def _write_fake_renode(
    bin_dir: Path, lines: list[str], *, exit_code: int = 0, sleep_s: int | None = None
) -> None:
    """A fake `renode` on PATH that actually runs, so `run_renode`'s deadline
    loop / double-EOF `natural_exit` capture / kill-teardown and the five
    post-spawn outcome branches in `renode_cmd.py` are reachable -- every
    `run_renode_cmd(..., path_override="")` call elsewhere in this file stops
    at the `renode.binary-missing` gate before any of that runs. Echoes
    `lines` to stdout (ignoring argv, which none of these tests need to
    inspect), optionally sleeping first to exercise the timeout/kill path,
    then exits `exit_code`.

    The launcher shells out to THIS SAME Python interpreter via its full
    `sys.executable` path rather than an external tool (`ping`/`sleep`)
    resolved through PATH: `run_renode_cmd` overrides the child's PATH to
    just the fake bin dir (`path_override`), so a bare `ping`/`sleep` command
    would fail to resolve and the fake binary would exit near-instantly
    instead of actually sleeping -- silently defeating the deadline tests.
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    impl = bin_dir / "_fake_renode_impl.py"
    body = "import sys, time\n"
    if sleep_s is not None:
        body += f"time.sleep({sleep_s})\n"
    for line in lines:
        body += f"print({line!r})\n"
    body += f"sys.exit({exit_code})\n"
    impl.write_text(body, encoding="utf-8")

    python = sys.executable
    if os.name == "nt":
        script = bin_dir / "renode.cmd"
        script.write_text(
            f'@echo off\n"{python}" "{impl}"\nexit /b %ERRORLEVEL%\n', encoding="utf-8"
        )
    else:
        script = bin_dir / "renode"
        script.write_text(f'#!/bin/sh\nexec "{python}" "{impl}"\n', encoding="utf-8")
        os.chmod(script, 0o755)


def _write_unspawnable_binary(bin_dir: Path) -> None:
    """A `renode` that resolves on PATH (passes `on_path`'s existence + X_OK
    gate) but cannot actually be spawned -- an empty file. Reproduces
    `renode.run-failed` without needing a real broken install."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    name = "renode.exe" if os.name == "nt" else "renode"
    target = bin_dir / name
    target.write_bytes(b"")
    if os.name != "nt":
        os.chmod(target, 0o755)


def run_renode_cmd(work: Path, *argv, path_override: str | None = None):
    """Spawn the harness app in `work` and return `(exit, stdout, stderr)`."""
    inherited = os.environ.get("PYTHONPATH")
    child_env = {
        **os.environ,
        "HOME": str(work),
        "USERPROFILE": str(work),
        "PYTHONPATH": os.pathsep.join(
            [str(PACKAGE_ROOT), *([inherited] if inherited else [])]
        ),
    }
    if path_override is not None:
        child_env["PATH"] = path_override
    proc = subprocess.run(
        [sys.executable, "-c", _HARNESS, "--sdk-root", "./sdk", *argv],
        cwd=work,
        env=child_env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return proc.returncode, proc.stdout, proc.stderr


def test_binary_missing_is_a_coded_refusal_not_a_traceback(tmp_path: Path):
    """The core ask this port exists to satisfy: Renode absent from PATH must
    be a coded, actionable envelope naming what to install -- never a
    traceback, never a silent `ok: true`."""
    _scaffold(tmp_path, with_elf=True, with_descriptors=True)
    exit_code, stdout, stderr = run_renode_cmd(
        tmp_path, "--format", "json", path_override=""
    )
    assert exit_code == 1, stderr
    assert stderr == ""
    envelope = json.loads(stdout)
    assert envelope["ok"] is False
    assert envelope["exitCode"] == 1
    assert envelope["issues"] == [
        {
            "code": "renode.binary-missing",
            "severity": "error",
            "message": (
                "`renode` binary not found on PATH. Install Renode "
                "(https://renode.io). tan renode does not silently pass when "
                "Renode is missing."
            ),
        }
    ]
    # Every pre-flight fact resolved BEFORE the binary gate still reports --
    # verified against the oracle: sku/platformStem/repl/resc/elf are all
    # populated even though the run itself never happened.
    assert envelope["data"]["sku"] == "E1M-AEN801"
    assert envelope["data"]["platformStem"] == "alif_ensemble_e8"
    assert envelope["data"]["elf"] != ""
    assert envelope["data"]["renodeArgv"] == []


def test_binary_missing_text_mode_is_one_line_on_stderr_nothing_on_stdout(tmp_path: Path):
    _scaffold(tmp_path, with_elf=True, with_descriptors=True)
    exit_code, stdout, stderr = run_renode_cmd(tmp_path, path_override="")
    assert exit_code == 1
    assert stdout == ""
    assert "renode.io" in stderr


def test_sdk_root_not_found_never_reports_an_sdk_block(tmp_path: Path):
    """A bad `--sdk-root` is TERMINAL (never falls through to a lower tier)
    and the envelope's `sdk` key is ABSENT, not null -- matching the oracle's
    own `sdk_report` side channel, which is never populated on this path.
    Needs its own harness invocation (not `run_renode_cmd`, which always
    passes `--sdk-root ./sdk`)."""
    inherited = os.environ.get("PYTHONPATH")
    child_env = {
        **os.environ,
        "HOME": str(tmp_path),
        "USERPROFILE": str(tmp_path),
        "PATH": "",
        "PYTHONPATH": os.pathsep.join(
            [str(PACKAGE_ROOT), *([inherited] if inherited else [])]
        ),
    }
    proc = subprocess.run(
        [sys.executable, "-c", _HARNESS, "--sdk-root", "./nope", "--format", "json"],
        cwd=tmp_path,
        env=child_env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    envelope = json.loads(proc.stdout)
    assert proc.returncode == 1
    assert "sdk" not in envelope
    assert envelope["issues"][0]["code"] == "renode.sdk-root-not-found"


def test_manifest_unavailable_names_the_build_command(tmp_path: Path):
    _scaffold(tmp_path, manifest=None)
    exit_code, stdout, _stderr = run_renode_cmd(tmp_path, "--format", "json", path_override="")
    assert exit_code == 1
    envelope = json.loads(stdout)
    assert envelope["issues"][0]["code"] == "renode.manifest-unavailable"
    assert "tan build --project" in envelope["issues"][0]["message"]


def test_schema_version_mismatch_is_validation_failure_exit_2(tmp_path: Path):
    _scaffold(
        tmp_path,
        manifest="schema_version: 2\nhw_info:\n  sku: E1M-AEN801\nslices: []\n",
    )
    exit_code, stdout, _stderr = run_renode_cmd(tmp_path, "--format", "json", path_override="")
    envelope = json.loads(stdout)
    assert exit_code == 2
    assert envelope["exitCode"] == 2
    assert envelope["issues"][0]["code"] == "renode.manifest-schema"


def test_elf_missing_reports_empty_elf_field_matching_the_oracle(tmp_path: Path):
    """`data.elf` stays EMPTY on `renode.elf-missing` -- the oracle's own
    `report.elf` assignment sits AFTER the `is_file()` check, so the unbuilt
    path never reaches the envelope."""
    _scaffold(tmp_path, with_elf=False, with_descriptors=True)
    exit_code, stdout, _stderr = run_renode_cmd(tmp_path, "--format", "json", path_override="")
    envelope = json.loads(stdout)
    assert exit_code == 1
    assert envelope["issues"][0]["code"] == "renode.elf-missing"
    assert envelope["data"]["elf"] == ""
    assert envelope["data"]["sku"] == "E1M-AEN801"  # resolved BEFORE the elf check


def test_descriptor_missing_reports_empty_repl_resc_fields(tmp_path: Path):
    _scaffold(tmp_path, with_elf=True, with_descriptors=False)
    exit_code, stdout, _stderr = run_renode_cmd(tmp_path, "--format", "json", path_override="")
    envelope = json.loads(stdout)
    assert exit_code == 1
    assert envelope["issues"][0]["code"] == "renode.descriptor-missing"
    assert envelope["data"]["repl"] == ""
    assert envelope["data"]["resc"] == ""
    assert envelope["data"]["platformStem"] == ""


def test_unresolvable_sku_is_a_coded_refusal(tmp_path: Path):
    _scaffold(
        tmp_path,
        manifest="schema_version: 1\nslices:\n- {core_id: c1, os: zephyr, status: ok}\n",
    )
    exit_code, stdout, _stderr = run_renode_cmd(tmp_path, "--format", "json", path_override="")
    envelope = json.loads(stdout)
    assert exit_code == 1
    assert envelope["issues"][0]["code"] == "renode.sku-unresolved"


def test_multiple_zephyr_slices_without_core_is_a_coded_refusal(tmp_path: Path):
    manifest = (
        "schema_version: 1\nhw_info:\n  sku: E1M-AEN801\nslices:\n"
        "- {core_id: m55_hp, os: zephyr, status: pending}\n"
        "- {core_id: m55_he, os: zephyr, status: pending}\n"
    )
    _scaffold(tmp_path, manifest=manifest)
    exit_code, stdout, _stderr = run_renode_cmd(tmp_path, "--format", "json", path_override="")
    envelope = json.loads(stdout)
    assert exit_code == 1
    assert envelope["issues"][0]["code"] == "renode.slice"
    assert "--core" in envelope["issues"][0]["message"]


def test_sim_mode_is_a_click_usage_error_not_a_silent_no_op(tmp_path: Path):
    """`--sim-mode` is deliberately NOT ported here (see the module docstring
    in `renode_cmd.py`): the flag is simply not declared, so Click refuses it
    outright rather than accepting it and doing nothing, or doing something
    half-implemented."""
    _scaffold(tmp_path, with_elf=True, with_descriptors=True)
    exit_code, stdout, stderr = run_renode_cmd(tmp_path, "--sim-mode", path_override="")
    assert exit_code == 2
    assert stdout == ""
    assert "--sim-mode" in stderr


def test_one_json_document_on_stdout_nothing_else(tmp_path: Path):
    """The framing invariant every `--format json` command owes: stdout
    carries exactly one JSON document and nothing else."""
    _scaffold(tmp_path, with_elf=True, with_descriptors=True)
    _exit_code, stdout, _stderr = run_renode_cmd(tmp_path, "--format", "json", path_override="")
    lines = [line for line in stdout.splitlines() if line.strip()]
    assert len(lines) == 1
    json.loads(lines[0])  # must parse as exactly one document


def test_negative_timeout_is_a_usage_error_like_the_oracle(tmp_path: Path):
    """A negative `--timeout` used to sail through as a bare `int`: the
    deadline was already past, so `run_renode`'s loop broke before reading a
    single line -- no latch ever tripped, and the run reported `ok: true`
    with an EMPTY issues list. The oracle's clap `u64` rejects it outright
    (rc 2); `min=0` makes Click do the same."""
    _scaffold(tmp_path, with_elf=True, with_descriptors=True)
    exit_code, stdout, stderr = run_renode_cmd(
        tmp_path, "--format", "json", "--timeout", "-1", path_override=""
    )
    assert exit_code == 2
    assert stdout == ""
    assert "--timeout" in stderr


# ── post-spawn outcomes (Finding 2): a fake `renode` that actually runs ─────


def test_clean_exit_with_no_expect_is_a_plain_success(tmp_path: Path):
    _scaffold(tmp_path, with_elf=True, with_descriptors=True)
    fake_bin = tmp_path / "fakebin"
    _write_fake_renode(fake_bin, ["renode: booting", "*** Booting Zephyr OS ***"])
    exit_code, stdout, _stderr = run_renode_cmd(
        tmp_path, "--format", "json", path_override=str(fake_bin)
    )
    envelope = json.loads(stdout)
    assert exit_code == 0, envelope
    assert envelope["ok"] is True
    assert envelope["issues"] == []
    assert envelope["data"]["expectFound"] is False
    assert len(envelope["data"]["renodeArgv"]) == 10
    log_path = Path(envelope["data"]["logPath"])
    assert "*** Booting Zephyr OS ***" in log_path.read_text(encoding="utf-8")


def test_argv_rejected_is_latched_from_console_text_not_exit_status(tmp_path: Path):
    """Renode answers an argv it refuses by printing its usage page and
    exiting 0 -- byte-identical to a clean smoke on exit status alone."""
    _scaffold(tmp_path, with_elf=True, with_descriptors=True)
    fake_bin = tmp_path / "fakebin"
    _write_fake_renode(
        fake_bin, ["usage: renode [options] [file-to-include / snapshot]"], exit_code=0
    )
    exit_code, stdout, _stderr = run_renode_cmd(
        tmp_path, "--format", "json", path_override=str(fake_bin)
    )
    envelope = json.loads(stdout)
    assert exit_code == 1
    assert envelope["issues"][0]["code"] == "renode.argv-rejected"
    assert "nothing was simulated" in envelope["issues"][0]["message"]


def test_cpu_halted_is_latched_even_though_the_child_exits_cleanly(tmp_path: Path):
    """Issue #64: a Renode that boots, halts the CPU on its first instruction
    fetch, then shuts down cleanly exits 0 -- the console text is the only
    signal."""
    _scaffold(tmp_path, with_elf=True, with_descriptors=True)
    fake_bin = tmp_path / "fakebin"
    _write_fake_renode(
        fake_bin,
        ["cpu: PC does not lay in memory or PC and SP are equal to zero. CPU was halted."],
        exit_code=0,
    )
    exit_code, stdout, _stderr = run_renode_cmd(
        tmp_path, "--format", "json", path_override=str(fake_bin)
    )
    envelope = json.loads(stdout)
    assert exit_code == 1
    assert envelope["issues"][0]["code"] == "renode.cpu-halted"


def test_exited_nonzero_before_timeout_is_a_coded_refusal(tmp_path: Path):
    _scaffold(tmp_path, with_elf=True, with_descriptors=True)
    fake_bin = tmp_path / "fakebin"
    _write_fake_renode(fake_bin, ["renode: booting"], exit_code=3)
    exit_code, stdout, _stderr = run_renode_cmd(
        tmp_path, "--format", "json", path_override=str(fake_bin)
    )
    envelope = json.loads(stdout)
    assert exit_code == 1
    assert envelope["issues"][0]["code"] == "renode.exited-nonzero"
    assert "exit code 3" in envelope["issues"][0]["message"]


def test_expect_hit_stops_early_and_reports_success(tmp_path: Path):
    _scaffold(tmp_path, with_elf=True, with_descriptors=True)
    fake_bin = tmp_path / "fakebin"
    _write_fake_renode(fake_bin, ["boot start", "MARKER-FOUND-OK", "tail"])
    exit_code, stdout, _stderr = run_renode_cmd(
        tmp_path,
        "--format",
        "json",
        "--expect",
        "MARKER-FOUND-OK",
        path_override=str(fake_bin),
    )
    envelope = json.loads(stdout)
    assert exit_code == 0, envelope
    assert envelope["data"]["expectFound"] is True
    assert envelope["issues"] == []


def test_expect_miss_is_a_coded_refusal(tmp_path: Path):
    """A missing `--expect` mutation to a no-op would make this exit 0
    instead."""
    _scaffold(tmp_path, with_elf=True, with_descriptors=True)
    fake_bin = tmp_path / "fakebin"
    _write_fake_renode(fake_bin, ["boot start", "tail"])
    exit_code, stdout, _stderr = run_renode_cmd(
        tmp_path,
        "--format",
        "json",
        "--expect",
        "NEVER-APPEARS",
        path_override=str(fake_bin),
    )
    envelope = json.loads(stdout)
    assert exit_code == 1
    assert envelope["issues"][0]["code"] == "renode.expect-not-found"
    assert envelope["data"]["expectFound"] is False


def test_image_bundle_adds_an_info_issue_and_does_not_fail_the_run(tmp_path: Path):
    _scaffold(tmp_path, with_elf=True, with_descriptors=True)
    fake_bin = tmp_path / "fakebin"
    _write_fake_renode(fake_bin, ["ok"])
    exit_code, stdout, _stderr = run_renode_cmd(
        tmp_path,
        "--format",
        "json",
        "--image-bundle",
        "bundle",
        path_override=str(fake_bin),
    )
    envelope = json.loads(stdout)
    assert exit_code == 0, envelope
    assert envelope["issues"] == [
        {
            "code": "renode.image-bundle-unused",
            "severity": "info",
            "message": (
                "renode: --image-bundle bundle accepted but unused by the "
                "single-slice smoke."
            ),
        }
    ]


def test_build_root_log_core_board_overrides_all_take_effect(tmp_path: Path):
    """One spawn-reaching run pinning four overrides at once: each is checked
    against a mutation that would silently drop it (`core_arg=None`,
    `--build-root` ignored, `--log` ignored, `--board` override ignored)."""
    (tmp_path / "sdk" / "scripts").mkdir(parents=True)
    (tmp_path / "sdk" / "scripts" / "alp_project.py").write_text("", encoding="utf-8")
    renode_dir = tmp_path / "sdk" / "metadata" / "renode"
    renode_dir.mkdir(parents=True)
    (renode_dir / "alif_ensemble_e8.repl").write_bytes(b"")
    (renode_dir / "alif_ensemble_e8.resc").write_bytes(b"")

    alt_build = tmp_path / "alt-build"
    alt_build.mkdir()
    manifest = (
        "schema_version: 1\nhw_info:\n  sku: E1M-AEN801\nslices:\n"
        "- {core_id: m55_hp, os: zephyr, status: pending}\n"
        "- {core_id: m55_he, os: zephyr, status: pending}\n"
    )
    (alt_build / "system-manifest.yaml").write_text(manifest, encoding="utf-8", newline="")
    elf_dir = alt_build / "m55_he-zephyr" / "zephyr"
    elf_dir.mkdir(parents=True)
    (elf_dir / "zephyr.elf").write_bytes(b"")

    fake_bin = tmp_path / "fakebin"
    _write_fake_renode(fake_bin, ["ok"])
    custom_log = tmp_path / "custom.log"

    exit_code, stdout, _stderr = run_renode_cmd(
        tmp_path,
        "--format",
        "json",
        "--build-root",
        str(alt_build),
        "--core",
        "m55_he",
        "--board",
        "E1M-AEN802",
        "--log",
        str(custom_log),
        path_override=str(fake_bin),
    )
    envelope = json.loads(stdout)
    assert exit_code == 0, envelope
    assert envelope["data"]["sku"] == "E1M-AEN802"
    expected_elf = str(elf_dir / "zephyr.elf")
    assert envelope["data"]["elf"].replace("\\", "/") == expected_elf.replace("\\", "/")
    assert envelope["data"]["logPath"].replace("\\", "/") == str(custom_log).replace("\\", "/")
    assert custom_log.is_file()
    assert "ok" in custom_log.read_text(encoding="utf-8")


def test_deadline_fires_on_a_child_that_never_exits(tmp_path: Path):
    """Proves the reader-thread + `queue.get(timeout=...)` deadline loop, not
    a blocking readline that would hang for the child's full lifetime: a
    `--timeout 1` sleeping child must be killed and reported well under this
    test's own subprocess safety margin. `natural_exit` stays `None` here
    (killed for the deadline, not its own exit), so the ONLY signal is
    `--expect` not being found."""
    _scaffold(tmp_path, with_elf=True, with_descriptors=True)
    fake_bin = tmp_path / "fakebin"
    _write_fake_renode(fake_bin, [], sleep_s=20)
    started = time.monotonic()
    exit_code, stdout, _stderr = run_renode_cmd(
        tmp_path,
        "--format",
        "json",
        "--timeout",
        "1",
        "--expect",
        "NEVER-APPEARS",
        path_override=str(fake_bin),
    )
    elapsed = time.monotonic() - started
    assert elapsed < 10, f"deadline not enforced -- waited {elapsed:.1f}s"
    envelope = json.loads(stdout)
    assert exit_code == 1
    assert envelope["issues"][0]["code"] == "renode.expect-not-found"


def test_run_failed_still_reports_the_argv_that_could_not_be_started(tmp_path: Path):
    """Finding 3: `renodeArgv` must be set BEFORE `run_renode` is called, so a
    spawn failure still reports the exact command line that could not be
    started -- the single most useful diagnostic on the one path where the
    caller cannot reproduce the command by hand."""
    _scaffold(tmp_path, with_elf=True, with_descriptors=True)
    fake_bin = tmp_path / "fakebin"
    _write_unspawnable_binary(fake_bin)
    exit_code, stdout, _stderr = run_renode_cmd(
        tmp_path, "--format", "json", path_override=str(fake_bin)
    )
    envelope = json.loads(stdout)
    assert exit_code == 1
    assert envelope["issues"][0]["code"] == "renode.run-failed"
    assert len(envelope["data"]["renodeArgv"]) == 10
