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
    _write_renode_wrapper(bin_dir, impl)


def _write_renode_wrapper(bin_dir: Path, impl: Path) -> None:
    """The `renode`/`renode.cmd` launcher shim shared by every fake-binary
    helper in this file: shells out to THIS SAME Python interpreter via its
    full `sys.executable` path rather than an external tool resolved
    through PATH, because `run_renode_cmd` overrides the child's PATH to
    just the fake bin dir (`path_override`) -- a bare external command would
    fail to resolve there."""
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


def _write_fake_sim_renode(
    bin_dir: Path,
    *,
    preamble: list[str] | None = None,
    exit_after_s: float | None = None,
    exit_code: int = 0,
    exit_after_first_echo_code: int | None = None,
) -> None:
    """A fake `renode` on PATH for `--sim-mode` tests: an interactive stub
    that answers just enough of the monitor line protocol for
    `RenodeMonitor.drain_boot`/`command` and a real control-socket round
    trip to work, so `renode_cmd.py`'s sim IO (bind/spawn/monitor/serve/
    teardown, and the post-spawn `renode.sim-exited-early` /
    `renode.cpu-halted` outcomes) is reachable without a real Renode
    install.

    Understands: `echo "TOKEN"` (prints `TOKEN` -- the sentinel protocol
    every `RenodeMonitor.command` relies on), `quit` (exits 0), `sysbus
    WriteByte <addr> <val>` / `sysbus ReadBytes <addr> <count>` (a tiny
    byte-addressed memory, mirroring `_FakeMonitor` in
    `tests/core/test_renode_sim.py`), and silently ignores anything else
    (so `version`, the initial `-e "i @..."` boot argv, etc. never wedge
    the loop).

    `preamble` lines are printed UNPROMPTED before the command loop starts
    -- used to inject an async `CPU was halted` line the way real Renode's
    own boot chatter would. `exit_after_s` starts a BACKGROUND timer that
    exits `exit_code` that many seconds after startup regardless of the
    (still-running, still-answering) command loop -- used to reproduce
    `renode.sim-exited-early` on a session whose `drain_boot` already
    succeeded, distinct from `renode.sim-monitor-failed` (which fires when
    the child is gone before `drain_boot` ever gets a reply).

    `exit_after_first_echo_code` exits SYNCHRONOUSLY, in the same thread,
    right after answering the very first `echo "<sentinel>"` -- i.e. right
    after `drain_boot`'s own round trip succeeds -- instead of on a timer.
    That makes the child ALREADY exited by the time `_run_sim` reaches
    its post-boot `proc.poll()`, deterministically, rather than racing a
    sleep against it; used for the `--timeout 0` regression (tan-cli#804),
    where the hold loop polls only once and a timer-based exit can't be
    trusted to land before that single poll.
    """
    if exit_after_s is not None and exit_after_first_echo_code is not None:
        raise ValueError(
            "exit_after_s and exit_after_first_echo_code are mutually exclusive: the "
            "echo-branch os._exit()s before the timer thread ever fires, making the "
            "timer dead weight"
        )
    bin_dir.mkdir(parents=True, exist_ok=True)
    impl = bin_dir / "_fake_sim_renode_impl.py"
    preamble_src = "\n".join(f"print({line!r}); sys.stdout.flush()" for line in (preamble or []))
    timer_src = (
        f"threading.Thread(target=lambda: (time.sleep({exit_after_s}), os._exit({exit_code})), "
        "daemon=True).start()\n"
        if exit_after_s is not None
        else ""
    )
    echo_action_src = (
        f"print(s[6:-1]); sys.stdout.flush(); os._exit({exit_after_first_echo_code})"
        if exit_after_first_echo_code is not None
        else "print(s[6:-1]); sys.stdout.flush(); continue"
    )
    body = f'''\
import os, sys, time, threading

{preamble_src}
{timer_src}
mem = {{}}
while True:
    raw = sys.stdin.readline()
    if not raw:
        break
    s = raw.rstrip("\\r\\n").strip()
    if s.startswith('echo "') and s.endswith('"'):
        {echo_action_src}
    if s == "quit":
        sys.exit(0)
    parts = s.split()
    if len(parts) == 4 and parts[0] == "sysbus" and parts[1] == "WriteByte":
        mem[int(parts[2], 0)] = int(parts[3], 0) & 0xFF
        continue
    if len(parts) == 4 and parts[0] == "sysbus" and parts[1] == "ReadBytes":
        addr, count = int(parts[2], 0), int(parts[3], 0)
        body = ", ".join(f"0x{{mem.get(addr + i, 0):02X}}" for i in range(count))
        print(f"[\\n{{body}}, \\n]"); sys.stdout.flush(); continue
    continue
'''
    impl.write_text(body, encoding="utf-8")
    _write_renode_wrapper(bin_dir, impl)


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
        # Without these, `text=True` decodes with the host's preferred encoding
        # -- cp1252 on a stock Windows box -- and Click/Rich's box-drawing usage
        # output is UTF-8 (`┐` is E2 94 90). The reader thread `communicate()`
        # spawns for `timeout=` then dies on the undecodable byte and BOTH
        # streams come back `None`, so the assertion fails as
        # `TypeError: argument of type 'NoneType' is not iterable` -- never
        # naming the encoding. Every other spawn harness in this suite
        # (test_init_command, test_sdk_command) already passes these.
        encoding="utf-8",
        errors="replace",
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
        # Without these, `text=True` decodes with the host's preferred encoding
        # -- cp1252 on a stock Windows box -- and Click/Rich's box-drawing usage
        # output is UTF-8 (`┐` is E2 94 90). The reader thread `communicate()`
        # spawns for `timeout=` then dies on the undecodable byte and BOTH
        # streams come back `None`, so the assertion fails as
        # `TypeError: argument of type 'NoneType' is not iterable` -- never
        # naming the encoding. Every other spawn harness in this suite
        # (test_init_command, test_sdk_command) already passes these.
        encoding="utf-8",
        errors="replace",
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


def test_manifest_unavailable_names_the_project_root_not_the_cwd(tmp_path: Path):
    """tan-cli#470: the refusal must name the root `--project` resolved to,
    not the CWD it was invoked from -- both in the searched path and in the
    `tan build --project` remedy it prints (a remedy naming the wrong
    directory would build the wrong project). `proj` here is unbuilt (no
    `build/system-manifest.yaml`), and `--project proj` is driven from the
    sibling `elsewhere` directory. FAILS against pre-fix code: it named
    `elsewhere` (the CWD) in both places instead."""
    proj = tmp_path / "proj"
    elsewhere = tmp_path / "elsewhere"
    proj.mkdir()
    elsewhere.mkdir()
    _scaffold(proj, manifest=None)

    inherited = os.environ.get("PYTHONPATH")
    child_env = {
        **os.environ,
        "HOME": str(elsewhere),
        "USERPROFILE": str(elsewhere),
        "PATH": "",
        "PYTHONPATH": os.pathsep.join(
            [str(PACKAGE_ROOT), *([inherited] if inherited else [])]
        ),
    }
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            _HARNESS,
            "--project",
            str(proj),
            "--sdk-root",
            str(proj / "sdk"),
            "--format",
            "json",
        ],
        cwd=elsewhere,
        env=child_env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    envelope = json.loads(proc.stdout)
    assert envelope["issues"][0]["code"] == "renode.manifest-unavailable"
    message = envelope["issues"][0]["message"].replace("\\", "/")
    expected_root = str(proj).replace("\\", "/")
    assert f"no system-manifest.yaml at {expected_root}/build/system-manifest.yaml" in message
    assert f"tan build --project {expected_root}" in message
    wrong_root = str(elsewhere).replace("\\", "/")
    assert wrong_root not in message


def test_project_flag_resolves_the_build_root_from_a_different_cwd(tmp_path: Path):
    """tan-cli#470: `--project PATH` must set the default build root exactly
    like every sibling command (`size`/`image`/`build`/`validate`/
    `generate`) -- not be silently dropped in favour of the CWD.

    Driven from a cwd that is NOT the built project, pointing `--project` at
    the real one. FAILS against pre-fix code: the pre-fix command resolved
    the build root from `elsewhere` (the cwd) instead of `proj`, so this
    reached `renode.manifest-unavailable` naming `elsewhere/build/system-
    manifest.yaml` -- never `renode.binary-missing` -- and `envelope
    ["project"]["root"]`/`envelope["data"]["elf"]` named `elsewhere`, not
    `proj`. Asserting the PRESENCE of the right (`proj`-rooted) paths, not
    merely the absence of the wrong (`elsewhere`-rooted) ones.
    """
    proj = tmp_path / "proj"
    elsewhere = tmp_path / "elsewhere"
    proj.mkdir()
    elsewhere.mkdir()
    _scaffold(proj, with_elf=True, with_descriptors=True)

    inherited = os.environ.get("PYTHONPATH")
    child_env = {
        **os.environ,
        "HOME": str(elsewhere),
        "USERPROFILE": str(elsewhere),
        "PATH": "",
        "PYTHONPATH": os.pathsep.join(
            [str(PACKAGE_ROOT), *([inherited] if inherited else [])]
        ),
    }
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            _HARNESS,
            "--project",
            str(proj),
            "--sdk-root",
            str(proj / "sdk"),
            "--core",
            "m55_hp",
            "--format",
            "json",
        ],
        cwd=elsewhere,
        env=child_env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    envelope = json.loads(proc.stdout)
    # The manifest + elf under --project's build root WERE found: the run
    # gets all the way to the (renode-not-installed) binary gate rather than
    # refusing on a missing manifest under the cwd.
    assert envelope["issues"][0]["code"] == "renode.binary-missing", envelope
    assert envelope["data"]["sku"] == "E1M-AEN801"
    resolved_root = envelope["project"]["root"].replace("\\", "/")
    resolved_elf = envelope["data"]["elf"].replace("\\", "/")
    expected_root = str(proj).replace("\\", "/")
    expected_elf = str(proj / "build" / "m55_hp-zephyr" / "zephyr" / "zephyr.elf").replace(
        "\\", "/"
    )
    assert resolved_root == expected_root
    assert resolved_elf == expected_elf


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


def test_sim_mode_without_image_bundle_is_a_coded_refusal(tmp_path: Path):
    """`--sim-mode` IS ported (tan-cli#77): it requires `--image-bundle`, and
    refuses with a coded issue -- never a Click usage error, never a silent
    no-op -- when it is missing."""
    _scaffold(tmp_path, with_elf=True, with_descriptors=True)
    exit_code, stdout, _stderr = run_renode_cmd(
        tmp_path, "--sim-mode", "--format", "json", path_override=""
    )
    envelope = json.loads(stdout)
    assert exit_code == 1
    assert envelope["issues"][0]["code"] == "renode.sim-bundle-required"
    assert "--image-bundle" in envelope["issues"][0]["message"]


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


def test_timeout_zero_never_classifies_a_line_so_it_cannot_report_a_pass(tmp_path: Path):
    """tan-cli#568: the literal repro. `--timeout 0` makes `run_renode`'s
    deadline already past when the read loop starts, so it breaks before
    dequeuing a single line -- even though the fake Renode below actually
    prints a real `CPU was halted` line. FAILS against pre-fix code: the
    deadline being in the past broke the loop before either the halt latch
    or `natural_exit` ever saw the line, and with no `--expect` the command
    fell through to `ExitCode.SUCCESS` -- `ok: true`, exit 0, `issues: []`,
    an EMPTY `renode.log` -- exactly the report the fake binary's own halt
    line contradicts."""
    _scaffold(tmp_path, with_elf=True, with_descriptors=True)
    fake_bin = tmp_path / "fakebin"
    _write_fake_renode(
        fake_bin,
        ["cpu: PC does not lay in memory or PC and SP are equal to zero. CPU was halted."],
        exit_code=0,
    )
    exit_code, stdout, _stderr = run_renode_cmd(
        tmp_path, "--format", "json", "--timeout", "0", path_override=str(fake_bin)
    )
    envelope = json.loads(stdout)
    assert exit_code == 1, envelope
    assert envelope["ok"] is False
    assert envelope["issues"][0]["code"] == "renode.no-console-output"
    # Never classified the halt line, so it must not be misreported as the
    # (unrelated, unearned) `renode.cpu-halted` code either.
    assert "renode.cpu-halted" not in [i["code"] for i in envelope["issues"]]
    log_path = Path(envelope["data"]["logPath"])
    assert log_path.read_text(encoding="utf-8") == ""


def test_silent_clean_exit_is_not_a_pass_even_at_a_normal_timeout(tmp_path: Path):
    """The general shape of tan-cli#568, not just the `--timeout 0` special
    case: a Renode that exits 0 having printed NOTHING must not report a
    pass either, at any timeout. Deterministic (no sleep, no race on the
    deadline) -- proves the fix keys off `lines_seen`, not off `timeout == 0`
    specifically."""
    _scaffold(tmp_path, with_elf=True, with_descriptors=True)
    fake_bin = tmp_path / "fakebin"
    _write_fake_renode(fake_bin, [], exit_code=0)
    exit_code, stdout, _stderr = run_renode_cmd(
        tmp_path, "--format", "json", "--timeout", "5", path_override=str(fake_bin)
    )
    envelope = json.loads(stdout)
    assert exit_code == 1, envelope
    assert envelope["ok"] is False
    assert envelope["issues"][0]["code"] == "renode.no-console-output"
    log_path = Path(envelope["data"]["logPath"])
    assert log_path.read_text(encoding="utf-8") == ""


def test_output_observed_before_the_deadline_still_passes(tmp_path: Path):
    """The positive control for tan-cli#568's fix: a run that DOES classify
    console lines must still report a plain success -- otherwise the fix
    could pass by making every run fail, which is the mirror-image bug."""
    _scaffold(tmp_path, with_elf=True, with_descriptors=True)
    fake_bin = tmp_path / "fakebin"
    _write_fake_renode(fake_bin, ["renode: booting", "*** Booting Zephyr OS ***"])
    exit_code, stdout, _stderr = run_renode_cmd(
        tmp_path, "--format", "json", "--timeout", "5", path_override=str(fake_bin)
    )
    envelope = json.loads(stdout)
    assert exit_code == 0, envelope
    assert envelope["ok"] is True
    assert envelope["issues"] == []
    log_path = Path(envelope["data"]["logPath"])
    assert "*** Booting Zephyr OS ***" in log_path.read_text(encoding="utf-8")


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


# ── --sim-mode (tan-cli#77): the studio hardware-simulator gateway ──────────
#
# Every envelope shape and stream-separation assertion below was diff-verified
# by driving the shipped `tan.exe` oracle live through the full `--sim-mode`
# pipeline (see `renode_cmd.py`'s module docstring) -- not inferred from
# `sim.rs`/`monitor.rs` alone.


def _scaffold_sim_bundle(
    work: Path, *, manifest: str | None = None, with_elf: bool = True
) -> Path:
    """An SDK checkout (loader script + the V2N101 Renode descriptor) plus an
    `--image-bundle` directory under `work`. Returns the bundle dir."""
    (work / "sdk" / "scripts").mkdir(parents=True, exist_ok=True)
    (work / "sdk" / "scripts" / "alp_project.py").write_text("", encoding="utf-8")
    renode_dir = work / "sdk" / "metadata" / "renode"
    renode_dir.mkdir(parents=True, exist_ok=True)
    (renode_dir / "renesas_rzv2n.repl").write_bytes(b"")
    (renode_dir / "renesas_rzv2n.resc").write_bytes(b"")
    bundle = work / "bundle"
    bundle.mkdir(exist_ok=True)
    if manifest is not None:
        (bundle / "system-manifest.yaml").write_text(manifest, encoding="utf-8", newline="")
    if with_elf:
        (bundle / "app.elf").write_bytes(b"")
    return bundle


def test_sim_bundle_missing_dir_is_a_coded_refusal(tmp_path: Path):
    _scaffold_sim_bundle(tmp_path)
    exit_code, stdout, _stderr = run_renode_cmd(
        tmp_path,
        "--sim-mode",
        "--image-bundle",
        "nope",
        "--format",
        "json",
        path_override="",
    )
    envelope = json.loads(stdout)
    assert exit_code == 1
    assert envelope["issues"][0]["code"] == "renode.sim-bundle-missing"


def test_sim_mode_sku_unresolved_without_board_or_manifest(tmp_path: Path):
    _scaffold_sim_bundle(tmp_path)
    exit_code, stdout, _stderr = run_renode_cmd(
        tmp_path,
        "--sim-mode",
        "--image-bundle",
        "bundle",
        "--format",
        "json",
        path_override="",
    )
    envelope = json.loads(stdout)
    assert exit_code == 1
    assert envelope["issues"][0]["code"] == "renode.sku-unresolved"
    assert "--board" in envelope["issues"][0]["message"]


def test_sim_mode_elf_missing_names_what_was_looked_for(tmp_path: Path):
    _scaffold_sim_bundle(tmp_path, with_elf=False)
    exit_code, stdout, _stderr = run_renode_cmd(
        tmp_path,
        "--sim-mode",
        "--image-bundle",
        "bundle",
        "--board",
        "E1M-V2N101",
        "--format",
        "json",
        path_override="",
    )
    envelope = json.loads(stdout)
    assert exit_code == 1
    assert envelope["issues"][0]["code"] == "renode.elf-missing"
    assert "zephyr.elf" in envelope["issues"][0]["message"]


def test_sim_mode_binary_missing_reports_the_plain_mode_log_path_default(tmp_path: Path):
    """The oracle-verified divergence: a pre-flight sim failure up to and
    including `renode.binary-missing` reports `data.logPath` as the PLAIN
    smoke's OWN default (`<app_path>/build/renode.log`), because the
    sim-specific default is only resolved much later -- see the module
    docstring in `renode_cmd.py`."""
    _scaffold_sim_bundle(tmp_path)
    exit_code, stdout, _stderr = run_renode_cmd(
        tmp_path,
        "--sim-mode",
        "--image-bundle",
        "bundle",
        "--board",
        "E1M-V2N101",
        "--format",
        "json",
        path_override="",
    )
    envelope = json.loads(stdout)
    assert exit_code == 1
    assert envelope["issues"][0]["code"] == "renode.binary-missing"
    log_path = envelope["data"]["logPath"].replace("\\", "/")
    assert log_path.endswith("build/renode.log"), log_path
    # Resolved BEFORE the binary gate: sku/platformStem/repl/elf all report.
    assert envelope["data"]["sku"] == "E1M-V2N101"
    assert envelope["data"]["platformStem"] == "renesas_rzv2n"
    assert envelope["data"]["elf"] != ""
    # Not yet resolved (post-binary-gate): the sim-only fields stay empty/0.
    assert envelope["data"]["descriptor"] == ""
    assert envelope["data"]["controlPort"] == 0
    assert envelope["data"]["uartPort"] == 0


def test_sim_mode_descriptor_missing_when_the_repl_is_absent(tmp_path: Path):
    (tmp_path / "sdk" / "scripts").mkdir(parents=True)
    (tmp_path / "sdk" / "scripts" / "alp_project.py").write_text("", encoding="utf-8")
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "app.elf").write_bytes(b"")
    exit_code, stdout, _stderr = run_renode_cmd(
        tmp_path,
        "--sim-mode",
        "--image-bundle",
        "bundle",
        "--board",
        "E1M-V2N101",
        "--format",
        "json",
        path_override="",
    )
    envelope = json.loads(stdout)
    assert exit_code == 1
    assert envelope["issues"][0]["code"] == "renode.descriptor-missing"


def test_sim_mode_success_writes_descriptor_and_serves_control_socket(tmp_path: Path):
    """The full happy path: pre-flight resolves, the descriptor + boot
    script land on disk with the right shape, the control socket answers a
    real WriteBytes/ReadBytes round trip while the run is live, and the
    envelope reports success with only the deferred-profile warning."""
    import socket

    bundle = _scaffold_sim_bundle(tmp_path)
    fake_bin = tmp_path / "fakebin"
    _write_fake_sim_renode(fake_bin)

    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            _HARNESS,
            "--sdk-root",
            "./sdk",
            "--sim-mode",
            "--image-bundle",
            "bundle",
            "--board",
            "E1M-V2N101",
            "--timeout",
            "6",
            "--format",
            "json",
        ],
        cwd=tmp_path,
        env={
            **os.environ,
            "HOME": str(tmp_path),
            "USERPROFILE": str(tmp_path),
            "PATH": str(fake_bin),
            "PYTHONPATH": str(PACKAGE_ROOT),
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding="utf-8",
        errors="replace",
    )
    try:
        descriptor_path = bundle / "sim-descriptor.json"
        deadline = time.monotonic() + 15
        while not descriptor_path.is_file() and time.monotonic() < deadline:
            time.sleep(0.1)
        descriptor = json.loads(descriptor_path.read_text(encoding="utf-8"))
        assert list(descriptor.keys()) == [
            "control_socket",
            "uart_socket",
            "framebuffers",
            "peripherals",
        ]
        control_port = int(descriptor["control_socket"].rsplit(":", 1)[1])

        with socket.create_connection(("127.0.0.1", control_port), timeout=5) as sock:
            reader = sock.makefile("rb")

            def send(line: str) -> str:
                sock.sendall((line + "\n").encode())
                return reader.readline().decode().rstrip("\r\n")

            assert send("sysbus WriteBytes 0x08010000 0xde 0xad 0xbe 0xef") == "ok"
            assert send("sysbus ReadBytes 0x08010000 4") == "0xde 0xad 0xbe 0xef"

        # The UART socket is deferred-SILENT but must stay CONNECTED: studio's
        # serial view has to open and simply stay empty, never fail to connect
        # and never see an EOF. Mirrors the oracle's own
        # `uart_socket_accepts_and_holds_the_connection_open_while_silent`
        # (crates/tan-cli/src/commands/renode/sim.rs), which had no Python
        # counterpart -- `_serve_uart_silent` could drop every connection with
        # the whole suite still green.
        uart_port = int(descriptor["uart_socket"].rsplit(":", 1)[1])
        with socket.create_connection(("127.0.0.1", uart_port), timeout=5) as uart:
            uart.settimeout(0.25)
            uart_deadline = time.monotonic() + 2
            while time.monotonic() < uart_deadline:
                try:
                    chunk = uart.recv(16)
                except TimeoutError:
                    continue  # connected-and-silent: the only correct outcome
                assert chunk != b"", (
                    "the UART socket closed the connection instead of holding it open"
                )
                raise AssertionError(
                    f"the UART socket streamed {len(chunk)} bytes; the streamer "
                    "is deferred (tan-cli#77)"
                )

        resc_text = (bundle / ".sim-boot.resc").read_text(encoding="utf-8")
        assert 'mach create "v2n_sim"' in resc_text
        assert "sysbus LoadELF" in resc_text

        stdout, stderr = proc.communicate(timeout=20)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate(timeout=10)

    assert proc.returncode == 0, (stdout, stderr)
    assert "tan renode --sim-mode: ready (timeout 6s)." in stderr
    envelope = json.loads(stdout)
    assert envelope["ok"] is True
    assert envelope["exitCode"] == 0
    assert envelope["data"]["sku"] == "E1M-V2N101"
    assert envelope["data"]["descriptor"] == str(descriptor_path)
    assert envelope["data"]["controlPort"] == control_port
    assert envelope["data"]["uartPort"] != 0
    assert [i["code"] for i in envelope["issues"]] == ["renode.sim-profile-deferred"]


def test_sim_mode_text_mode_prints_the_header_immediately_to_stdout(tmp_path: Path):
    """The header lines (sku/elf, descriptor, control, uart, the deferred
    warning) and the readiness marker print DIRECTLY to stdout in text
    mode, not buffered until the run ends -- verified stream-separated
    against the oracle."""
    _scaffold_sim_bundle(tmp_path)
    fake_bin = tmp_path / "fakebin"
    _write_fake_sim_renode(fake_bin)
    exit_code, stdout, stderr = run_renode_cmd(
        tmp_path,
        "--sim-mode",
        "--image-bundle",
        "bundle",
        "--board",
        "E1M-V2N101",
        "--timeout",
        "1",
        path_override=str(fake_bin),
    )
    assert exit_code == 0, stderr
    assert "tan renode --sim-mode: E1M-V2N101 booting app.elf" in stdout
    assert "descriptor :" in stdout
    assert "control    :" in stdout
    assert "uart       :" in stdout
    assert "tan-cli#77" in stdout  # the deferred-profile warning, printed too
    assert "ready (timeout 1s)" in stdout
    assert stderr == ""


def test_sim_mode_cpu_halted_is_latched_even_though_the_session_comes_up(tmp_path: Path):
    _scaffold_sim_bundle(tmp_path)
    fake_bin = tmp_path / "fakebin"
    _write_fake_sim_renode(
        fake_bin,
        preamble=["cpu: PC does not lay in memory or PC and SP are equal to zero. CPU was halted."],
    )
    exit_code, stdout, _stderr = run_renode_cmd(
        tmp_path,
        "--sim-mode",
        "--image-bundle",
        "bundle",
        "--board",
        "E1M-V2N101",
        "--timeout",
        "1",
        "--format",
        "json",
        path_override=str(fake_bin),
    )
    envelope = json.loads(stdout)
    assert exit_code == 1
    codes = [i["code"] for i in envelope["issues"]]
    assert "renode.cpu-halted" in codes


def test_sim_mode_exited_early_after_drain_boot_succeeded(tmp_path: Path):
    _scaffold_sim_bundle(tmp_path)
    fake_bin = tmp_path / "fakebin"
    _write_fake_sim_renode(fake_bin, exit_after_s=1.0, exit_code=9)
    exit_code, stdout, _stderr = run_renode_cmd(
        tmp_path,
        "--sim-mode",
        "--image-bundle",
        "bundle",
        "--board",
        "E1M-V2N101",
        "--timeout",
        "5",
        "--format",
        "json",
        path_override=str(fake_bin),
    )
    envelope = json.loads(stdout)
    assert exit_code == 1
    assert envelope["issues"][0]["code"] == "renode.sim-exited-early"
    assert "exit code 9" in envelope["issues"][0]["message"]


def test_sim_mode_timeout_zero_still_reports_exited_early(tmp_path: Path):
    """tan-cli#804: the hold loop used to compute
    `deadline = time.monotonic() + timeout` and then guard on
    `while time.monotonic() < deadline:` -- at `--timeout 0` the deadline is
    already in the past the instant it's read, so the loop body (the only
    place `proc.poll()` was called) never ran even once, `early_exit` stayed
    `None`, and `renode.sim-exited-early` was structurally unreachable no
    matter how the child actually exited. The fix polls once up front so a
    child that is ALREADY dead by the time `_run_sim` reaches the hold
    is still caught even at a zero-length hold."""
    _scaffold_sim_bundle(tmp_path)
    fake_bin = tmp_path / "fakebin"
    _write_fake_sim_renode(fake_bin, exit_after_first_echo_code=9)
    exit_code, stdout, _stderr = run_renode_cmd(
        tmp_path,
        "--sim-mode",
        "--image-bundle",
        "bundle",
        "--board",
        "E1M-V2N101",
        "--timeout",
        "0",
        "--format",
        "json",
        path_override=str(fake_bin),
    )
    envelope = json.loads(stdout)
    assert exit_code == 1
    assert envelope["issues"][0]["code"] == "renode.sim-exited-early"
    assert "exit code 9" in envelope["issues"][0]["message"]


def test_sim_mode_timeout_zero_still_succeeds_for_a_healthy_child(tmp_path: Path):
    """The positive control for tan-cli#804's fix: a `--timeout 0` run
    against a Renode that is still alive (never exits) must keep succeeding
    -- the up-front `proc.poll()` added for the regression above must only
    ever OBSERVE the child, never affect a healthy zero-length hold. Pins
    the deliberately-kept `ok:true` / `exitCode:0` behaviour so a later move
    of the poll past `_teardown_sim` can't silently flip it."""
    _scaffold_sim_bundle(tmp_path)
    fake_bin = tmp_path / "fakebin"
    _write_fake_sim_renode(fake_bin)
    exit_code, stdout, _stderr = run_renode_cmd(
        tmp_path,
        "--sim-mode",
        "--image-bundle",
        "bundle",
        "--board",
        "E1M-V2N101",
        "--timeout",
        "0",
        "--format",
        "json",
        path_override=str(fake_bin),
    )
    envelope = json.loads(stdout)
    assert exit_code == 0
    assert envelope["ok"] is True
    assert envelope["issues"][0]["code"] == "renode.sim-profile-deferred"


def test_sim_mode_expect_is_ignored_with_an_info_issue(tmp_path: Path):
    _scaffold_sim_bundle(tmp_path)
    fake_bin = tmp_path / "fakebin"
    _write_fake_sim_renode(fake_bin)
    exit_code, stdout, _stderr = run_renode_cmd(
        tmp_path,
        "--sim-mode",
        "--image-bundle",
        "bundle",
        "--board",
        "E1M-V2N101",
        "--timeout",
        "1",
        "--expect",
        "NEVER-SCANNED",
        "--format",
        "json",
        path_override=str(fake_bin),
    )
    envelope = json.loads(stdout)
    assert exit_code == 0, envelope
    codes = [i["code"] for i in envelope["issues"]]
    assert "renode.expect-ignored" in codes
    assert "renode.sim-profile-deferred" in codes
