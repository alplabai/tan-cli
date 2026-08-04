# SPDX-License-Identifier: Apache-2.0
"""`tan faultdecode` -- CLI surface tests.

`faultdecode` is not registered in `tan.cli.app` by this change (the shared
`cli.py` registration point is owned by the orchestrator wiring commands in
parallel), so these tests mount the command on a throwaway `typer.Typer()`
rather than importing `tan.cli.app`, matching how the command will actually be
invoked once registered: `app.command("faultdecode")(faultdecode)`.

Where an alp-sdk checkout is reachable (`ALP_SDK_ROOT`, or one sitting next to
this repo -- see `_resolve_oracle_path`; not a hardcoded machine path), several
tests diff this command's CliRunner output directly against
`alp_cli.faultdecode`'s own `click.testing` output -- text AND `--json` --
proving the replacement forwarder-target is byte-identical to what
`sdk_cli.rs`'s subprocess forward used to stream. Those tests skip (never
fail) when no checkout is reachable; they are a bonus re-check, not the
register-fidelity guard itself (`tests/core/test_faultdecode.py` pins that
against a committed golden fixture, unconditionally).
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from tan.commands.faultdecode_cmd import faultdecode
from tests.conftest import REAL_ENVIRON

#: `python/` -- `python -m tan` resolves the package off `os.getcwd()`, so the
#: real-subprocess case below needs this on the child's `PYTHONPATH`.
PACKAGE_ROOT = Path(__file__).resolve().parents[2]

app = typer.Typer()
app.command("faultdecode")(faultdecode)
runner = CliRunner()


def _resolve_oracle_path() -> Path | None:
    """Locate alp-sdk's `scripts/alp_cli/faultdecode.py`. Mirrors
    `tests/core/test_faultdecode.py::_resolve_oracle_path`: `ALP_SDK_ROOT` if
    set (a set-but-missing value RAISES rather than skipping), else an
    `alp-sdk` checkout sitting next to this repo at any ancestor level.
    Returns `None` only when neither is present.

    Reads `REAL_ENVIRON` (captured at collection time in `tests/conftest.py`),
    NOT `os.environ` -- this function runs from inside test bodies (via
    `_load_oracle_command`), by which point the autouse
    `_scrub_sdk_discovery_env` fixture has already deleted `ALP_SDK_ROOT`
    from the live process environment, so an `os.environ` read here always
    saw it gone and every oracle-parity test below skipped unconditionally
    (tan-cli#254/#256 fix)."""
    override = REAL_ENVIRON.get("ALP_SDK_ROOT")
    if override:
        candidate = Path(override) / "scripts" / "alp_cli" / "faultdecode.py"
        if not candidate.is_file():
            raise RuntimeError(
                f"ALP_SDK_ROOT={override!r} has no scripts/alp_cli/faultdecode.py. "
                "Refusing to skip: a named-but-missing oracle would make this "
                "check pass vacuously. Fix the path, or unset it."
            )
        return candidate
    for parent in Path(__file__).resolve().parents:
        candidate = parent.parent / "alp-sdk" / "scripts" / "alp_cli" / "faultdecode.py"
        if candidate.is_file():
            return candidate
    return None


def _load_oracle_command():
    path = _resolve_oracle_path()
    if path is None:
        pytest.skip("no alp-sdk checkout found (set ALP_SDK_ROOT, or run next to one)")
    sdk_scripts = str(path.parents[1])
    added = sdk_scripts not in sys.path
    if added:
        sys.path.insert(0, sdk_scripts)
    try:
        spec = importlib.util.spec_from_file_location("_faultdecode_oracle_cli", path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module.faultdecode_cmd
    finally:
        if added:
            sys.path.remove(sdk_scripts)


# --------------------------------------------------------------------------
# Oracle parity (text + JSON), skipped when the SDK sibling isn't present
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "args",
    [
        ["--cfsr", "0x00020000"],
        ["--cfsr", "0x100000", "--json"],
        ["--hfsr", "0x40000000"],
        ["--mmfsr", "0x02", "--bfsr", "0x82", "--ufsr", "0x01", "--json"],
        ["--cfsr", "0x8200", "--bfar", "0x20001000"],
        ["--dfsr", "0x2"],
        ["--cfsr", "0x8200", "--no-color"],
    ],
)
def test_matches_oracle_output_byte_for_byte(args):
    from click.testing import CliRunner as ClickRunner

    oracle_cmd = _load_oracle_command()
    oracle_result = ClickRunner().invoke(oracle_cmd, args)
    port_result = runner.invoke(app, args)
    assert port_result.exit_code == oracle_result.exit_code
    assert port_result.output == oracle_result.output


def test_matches_oracle_on_a_realistic_zephyr_fault_dump():
    from click.testing import CliRunner as ClickRunner

    oracle_cmd = _load_oracle_command()
    dump = (
        "E: ***** HARD FAULT *****\n"
        "E: MMFSR: 0x00000082, BFSR: 0x00000000, UFSR: 0x00020000\n"
        "E: HFSR: 0x40000000\n"
        "E: MMFAR: 0x00000000, BFAR: 0x00000000\n"
        "E: pc: 0x08004a2e\n"
        "E: lr: 0x08004a01\n"
    )
    oracle_result = ClickRunner().invoke(oracle_cmd, ["--file", "-"], input=dump)
    port_result = runner.invoke(app, ["--file", "-"], input=dump)
    assert port_result.exit_code == oracle_result.exit_code
    assert port_result.output == oracle_result.output


def test_matches_oracle_exit_code_on_missing_registers():
    """Text differs (click's own usage banner vs this port's plain message,
    matching every other bespoke-validation command in this package -- see
    `explain_cmd._fail`), but the exit code -- the part a caller branches on --
    must agree."""
    from click.testing import CliRunner as ClickRunner

    oracle_cmd = _load_oracle_command()
    oracle_result = ClickRunner().invoke(oracle_cmd, [])
    port_result = runner.invoke(app, [])
    assert port_result.exit_code == oracle_result.exit_code == 2


def test_matches_oracle_exit_code_on_bad_hex_value():
    from click.testing import CliRunner as ClickRunner

    oracle_cmd = _load_oracle_command()
    oracle_result = ClickRunner().invoke(oracle_cmd, ["--cfsr", "zz"])
    port_result = runner.invoke(app, ["--cfsr", "zz"])
    assert port_result.exit_code == oracle_result.exit_code == 2


def test_short_help_matches_the_shipped_rust_oracle():
    """Typer takes a command's short help from the docstring's first line.
    Must read `Decode an ARM Cortex-M (ARMv8-M) fault dump.` -- matching
    `crates/tan-cli/src/cli.rs`'s `Faultdecode` doc comment (minus its
    now-obsolete `` (`alp faultdecode`) `` forwarder suffix) -- so `tan
    --help` / `tan faultdecode --help` read the same as the shipped binary,
    matching the sibling convention `explain_cmd`/`size_cmd` already follow."""
    first_line = faultdecode.__doc__.strip().splitlines()[0].strip()
    assert first_line == "Decode an ARM Cortex-M (ARMv8-M) fault dump."


# --------------------------------------------------------------------------
# Standalone CLI behaviour (no oracle needed)
# --------------------------------------------------------------------------


def test_success_exits_zero_even_with_no_flags_set():
    result = runner.invoke(app, ["--cfsr", "0x0"])
    assert result.exit_code == 0
    assert "No fault flags set." in result.output


def test_json_flag_emits_the_machine_report_shape():
    result = runner.invoke(app, ["--cfsr", "0x8200", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["fault_detected"] is True
    assert {"reg", "name", "bit", "meaning"} <= payload["flags"][0].keys()
    assert payload["root_cause"]


def test_no_registers_at_all_is_exit_2_validation_failure():
    result = runner.invoke(app, [])
    assert result.exit_code == 2
    assert "no fault registers supplied" in result.output


def test_bad_hex_value_is_exit_2():
    result = runner.invoke(app, ["--cfsr", "not-hex"])
    assert result.exit_code == 2


def test_missing_elf_path_is_exit_2():
    result = runner.invoke(app, ["--cfsr", "0x8200", "--elf", "does-not-exist.elf"])
    assert result.exit_code == 2


def test_missing_file_path_is_exit_2():
    result = runner.invoke(app, ["--file", "does-not-exist.txt"])
    assert result.exit_code == 2


def test_project_and_sdk_root_are_accepted_but_unused():
    """Declared-not-read, matching `explain`/`debug-config`'s pattern for
    global flags a project-agnostic command must still parse."""
    result = runner.invoke(
        app, ["--cfsr", "0x8200", "--project", "/tmp/x", "--sdk-root", "/tmp/sdk"]
    )
    assert result.exit_code == 0


def test_full_global_flag_set_is_accepted_even_when_meaningless():
    """The oracle's clap `GlobalArgs` are `global = true`, so `faultdecode`
    accepts `--board-yaml`/`--target`/`--all`/`--verbose`/`--quiet`/
    `--non-interactive`/`--ci` even though it never reads any of them --
    confirmed live: `tan.exe faultdecode --sdk-root <bad> --board-yaml x
    --target t --all --verbose --quiet --non-interactive --ci --cfsr
    0x8200` is a forwarder-shaped SDK-root-unresolved refusal, not a parse
    error, on the oracle; this port's native `faultdecode` needs no SDK root
    at all (see the module docstring) so the SAME argv succeeds outright.
    Regression for the Click "No such option" usage error (exit 2) this
    port used to raise for each of these instead (tan-cli#256)."""
    result = runner.invoke(
        app,
        [
            "--board-yaml", "x.yaml",
            "--target", "zephyr-conf",
            "--all",
            "--verbose",
            "--quiet",
            "--non-interactive",
            "--ci",
            "--cfsr", "0x8200",
        ],
    )
    assert result.exit_code == 0, result.output


def test_format_json_after_subcommand_is_equivalent_to_json_flag():
    """`--format json`, declared after the subcommand name (Typer's own
    option), must behave exactly like `--json`: the oracle maps the global
    `--format json` onto the child's `--json`
    (`crates/tan-cli/src/commands/sdk_cli.rs:56-58`) -- confirmed live against
    the built `tan.exe`: `tan faultdecode --format json --cfsr 0x8200` prints
    the same unwrapped SDK report shape as `--json`, rc=0."""
    result = runner.invoke(app, ["--cfsr", "0x8200", "--format", "json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["fault_detected"] is True
    assert payload["flags"][0]["name"] == "PRECISERR"
    assert payload["root_cause"]


def test_format_json_before_subcommand_reads_off_ctx_obj():
    """Mirrors `debug-config`'s pre-subcommand plumbing (`cli.py`'s `root`
    callback stashes `--format` on `ctx.obj`, and this command reads it back;
    since tan-cli#378 the real `cli.py` relocates the token past the
    subcommand name instead, and the `ctx.obj` seam this exercises is the
    fallback that keeps the two positions interchangeable). Confirmed live
    against the built
    `tan.exe`: `tan --format json faultdecode --cfsr 0x8200` and `tan
    faultdecode --format json --cfsr 0x8200` print byte-identical unwrapped
    JSON at rc=0, because the oracle's clap `--format` is `global = true`."""
    root_app = typer.Typer()

    @root_app.callback(invoke_without_command=True)
    def _root(ctx: typer.Context, output_format: str = typer.Option(None, "--format")) -> None:
        ctx.obj = {"format": output_format}

    root_app.command("faultdecode")(faultdecode)
    result = runner.invoke(
        root_app, ["--format", "json", "faultdecode", "--cfsr", "0x8200"]
    )
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["fault_detected"] is True
    assert payload["flags"][0]["name"] == "PRECISERR"
    assert payload["root_cause"]


def test_format_bad_value_is_rejected():
    result = runner.invoke(app, ["--cfsr", "0x8200", "--format", "bogus"])
    assert result.exit_code == 2


def test_pc_without_elf_notes_the_missing_elf_on_stderr_but_still_exits_zero():
    result = runner.invoke(app, ["--cfsr", "0x8200", "--pc", "0x08001000"])
    assert result.exit_code == 0
    assert "--pc given without --elf" in result.output


def test_stdin_dump_is_auto_consumed_when_piped():
    result = runner.invoke(app, [], input="CFSR: 0x8200\n")
    assert result.exit_code == 0
    assert "PRECISERR" in result.output


@pytest.mark.parametrize(
    "argv",
    [
        ["--cfsr", "0x8200", "--json"],
        ["--mmfsr", "0x02", "--bfsr", "0x82", "--json"],
    ],
    ids=["cfsr", "sub-registers"],
)
def test_registers_on_the_command_line_do_not_wait_on_a_held_open_stdin(argv):
    """tan-cli#388. `sys.stdin.read()` returns at EOF, and a pipe reaches EOF
    only when the last writer CLOSES it -- not when it merely stops writing.
    `_read_dump` ran unconditionally for any non-tty stdin, and BEFORE the
    register flags were parsed, so a parent that spawns `tan` with
    `stdin=PIPE` and holds the write end open blocked the child forever, with
    zero bytes on stdout and stderr and no exit code. Measured against a
    held-open FIFO: `rc=124` under a 12 s `timeout(1)`, where `tan presets`
    on the SAME FIFO returned in 0.33 s. The affected invocation is the
    PRIMARY documented one -- registers pasted straight off a HardFault.

    A real subprocess with `stdin=PIPE` LEFT OPEN is the only shape that can
    catch this: `CliRunner(input=...)` hands the command an already-complete,
    already-closed buffer, which is why this module was fully green with the
    defect live. Re-verified in reverse before landing -- with `_read_dump`
    restored to its pre-fix shape the same probe hangs for the full timeout
    and produces 0 bytes.
    """
    proc = subprocess.Popen(
        [sys.executable, "-m", "tan", "faultdecode", *argv],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        env={
            **os.environ,
            "PYTHONPATH": os.pathsep.join(
                [str(PACKAGE_ROOT), *([p] if (p := os.environ.get("PYTHONPATH")) else [])]
            ),
        },
    )
    try:
        # Deliberately NOT `communicate()`: that closes stdin, handing the
        # child the EOF whose absence is the whole defect.
        stdout = ""
        try:
            proc.wait(timeout=30)
            stdout = proc.stdout.read()
        except subprocess.TimeoutExpired:  # pragma: no cover - the defect
            proc.kill()
            raise AssertionError(
                "tan faultdecode blocked on a held-open stdin pipe for 30s with "
                "every register already supplied on the command line (tan-cli#388)"
            ) from None
    finally:
        proc.stdin.close()
        proc.stdout.close()
        proc.stderr.close()

    assert proc.returncode == 0, stdout
    report = json.loads(stdout)
    assert report["fault_detected"] is True
    assert report["root_cause"]


def test_explicit_flag_wins_over_a_parsed_dump():
    result = runner.invoke(app, ["--cfsr", "0x1", "--file", "-"], input="CFSR: 0x8200\n")
    assert result.exit_code == 0
    # bit 0 (IACCVIOL) from the explicit flag, not bit 9 (PRECISERR) from the dump.
    assert "IACCVIOL" in result.output
    assert "PRECISERR" not in result.output


def test_symbolication_is_skipped_gracefully_for_a_non_elf_file():
    with tempfile.NamedTemporaryFile(suffix=".elf", delete=False, mode="w") as handle:
        handle.write("not an elf")
        fake_elf = handle.name
    try:
        result = runner.invoke(
            app,
            ["--cfsr", "0x8200", "--pc", "0x08001000", "--elf", fake_elf],
        )
        assert result.exit_code == 0
        # No crash, and no bogus "Symbolication:" section for a file the tool
        # cannot actually read symbols from.
        assert "Symbolication:" not in result.output
    finally:
        Path(fake_elf).unlink()


# --------------------------------------------------------------------------
# NO_COLOR is PRESENCE, not truthiness (tan-cli#288)
# --------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["", "0", "false"])
def test_no_color_env_var_suppresses_color_without_crashing(monkeypatch, value):
    """`NO_COLOR=<value>` -- including set-but-empty -- must disable colour,
    matching the oracle (`crates/tan-cli/src/style.rs:27`'s
    `var_os("NO_COLOR").is_none()`) and the spec (any value disables colour).
    `sys.stdout.isatty` is forced True so the divergence is actually
    observable: under pytest's own non-tty stdout, a truthy check on an empty
    `NO_COLOR` would fall through to the tty probe and land on the same
    (correct) answer by accident, hiding the bug this pins."""
    from tan.commands.faultdecode_cmd import _use_color

    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    monkeypatch.setenv("NO_COLOR", value)
    assert _use_color(no_color=False) is False
