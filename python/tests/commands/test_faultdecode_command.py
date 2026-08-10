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
import inspect
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from tan.commands.faultdecode_cmd import faultdecode
from tests.conftest import REAL_ENVIRON

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


# --------------------------------------------------------------------------
# tan-cli#616 defect B: a negative register value is refused, not decoded
# --------------------------------------------------------------------------
#
# `int(text, 16)` accepts a leading `-`, and everything downstream then treated
# the result as a register word: `--cfsr=-8200` rendered `"0x-0008200"` (not a
# hex integer in any sense), decoded TWELVE flags that are not set out of
# Python's infinite two's-complement sign extension, concluded "Stack
# overflow", and exited 0. A register is unsigned by construction. The strings
# below are pinned verbatim because they are what a firmware engineer reads
# instead of a diagnosis.

#: The exact refusal for `--cfsr=-8200`, naming the offending value.
_NEGATIVE_CFSR_MESSAGE = (
    "--cfsr: '-8200' is negative -- a fault register value is unsigned, so there is "
    "nothing to decode. Pass the value exactly as the dump printed it (hex, with or "
    "without a 0x prefix)."
)


def test_a_negative_register_is_refused_on_the_text_surface():
    result = runner.invoke(app, ["--cfsr=-8200"])
    assert result.exit_code == 2
    assert result.output.strip() == f"Error: {_NEGATIVE_CFSR_MESSAGE}"
    # The whole point: no decode happened, so none of it can be quoted back.
    assert "0x-0008200" not in result.output
    assert "Stack overflow" not in result.output


def test_a_negative_register_is_refused_with_a_coded_envelope():
    """`--format json` gets the same refusal as an envelope carrying a
    REGISTERED issue code, so a consumer can branch on it -- the shape
    `faultdecode.no-registers` already has (tan-cli#399). A `typer.BadParameter`
    would instead land on `cli.main`'s generic `command: "cli"` /
    `cli.parse-error` fallback, i.e. the two-paths-of-one-verb disagreement
    that issue closed."""
    result = runner.invoke(app, ["--format", "json", "--cfsr=-8200"])
    assert result.exit_code == 2
    # Asserted before parsing, so a refusal that fell back to the plain-text
    # surface fails HERE, on this test's own claim, rather than as an incidental
    # `JSONDecodeError` from the line below.
    assert result.stdout.lstrip().startswith("{"), result.stdout
    payload = json.loads(result.stdout)
    assert payload["command"] == "faultdecode"
    assert payload["ok"] is False
    assert payload["exitCode"] == 2
    assert payload["data"] is None
    assert payload["issues"] == [
        {
            "code": "faultdecode.invalid-register-value",
            "severity": "error",
            "message": _NEGATIVE_CFSR_MESSAGE,
        }
    ]


@pytest.mark.parametrize(
    "option",
    ["cfsr", "hfsr", "dfsr", "bfar", "mmfar", "mmfsr", "bfsr", "ufsr", "pc", "lr"],
)
def test_every_register_flag_refuses_a_negative_value_and_names_itself(option):
    """All ten go through `_parse_hexint`, addresses (`--bfar`/`--mmfar`/
    `--pc`/`--lr`) included -- an address is no more signed than a status
    register. The message names the FLAG that was wrong, not just "a
    register", so a caller passing several knows which one to fix."""
    result = runner.invoke(app, [f"--{option}=-1"])
    assert result.exit_code == 2
    assert result.output.strip() == (
        f"Error: --{option}: '-1' is negative -- a fault register value is unsigned, so "
        "there is nothing to decode. Pass the value exactly as the dump printed it "
        "(hex, with or without a 0x prefix)."
    )


def test_a_negative_register_is_refused_before_a_valid_one_is_decoded():
    """The refusal is a refusal, not a warning: a good `--hfsr` alongside a bad
    `--cfsr` does not buy a partial decode at exit 0."""
    result = runner.invoke(app, ["--hfsr", "0x40000000", "--cfsr=-8200"])
    assert result.exit_code == 2
    assert "Forced HardFault" not in result.output


def test_the_json_flag_surface_also_refuses_a_negative_register():
    """`--json` is the unwrapped SDK report surface, so its refusal stays the
    plain stderr line (envelope-free), exactly as the no-registers refusal and
    the bad-hex refusal already do on that surface -- but it is still a
    refusal, and still exit 2."""
    result = runner.invoke(app, ["--cfsr=-8200", "--json"])
    assert result.exit_code == 2
    assert result.output.strip() == f"Error: {_NEGATIVE_CFSR_MESSAGE}"


def test_the_sdk_original_decodes_a_negative_cfsr_and_tan_deliberately_does_not():
    """The DIVERGENCE, pinned against the live original rather than described.

    alp-sdk's `scripts/alp_cli/faultdecode.py` accepts `--cfsr=-8200` and exits
    0 having printed a decode. tan refuses at exit 2. This asserts BOTH halves,
    so the day upstream adopts the refusal this test goes red and is deleted
    with a note, rather than the divergence quietly persisting in prose only.
    Skips (never fails) with no alp-sdk checkout reachable, same as its
    neighbours."""
    from click.testing import CliRunner as ClickRunner

    oracle_cmd = _load_oracle_command()
    oracle_result = ClickRunner().invoke(oracle_cmd, ["--cfsr=-8200"])
    assert oracle_result.exit_code == 0, "upstream no longer decodes a negative CFSR"
    assert "0x-0008200" in oracle_result.output

    port_result = runner.invoke(app, ["--cfsr=-8200"])
    assert port_result.exit_code == 2
    assert port_result.output.strip() == f"Error: {_NEGATIVE_CFSR_MESSAGE}"


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


def test_format_json_after_subcommand_wraps_the_report_in_the_envelope():
    """tan-cli#399. `--format json` is the EXTENSION's spelling, and the
    extension's `isEnvelope` guard (`alp-sdk-vscode
    src/alpCli/service.ts:705-716`) requires `command`/`ok`/`exitCode`/
    `issues[]`; the unwrapped SDK report carries none of them, so the whole
    decode used to land as `Command completed.` with `data` unreachable.

    This is a DELIBERATE divergence from the v0.4.1 oracle, which maps the
    global `--format json` onto the forwarded child's `--json`
    (`crates/tan-cli/src/commands/sdk_cli.rs:56-58`) and therefore prints the
    unwrapped report for both spellings. The raw shape did not go away -- it
    is what `--json` still prints (`test_json_flag_stays_the_unwrapped_sdk_
    report`), which is the flag every saved script and the forwarder itself
    used."""
    result = runner.invoke(app, ["--cfsr", "0x8200", "--format", "json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["command"] == "faultdecode"
    assert payload["ok"] is True
    assert payload["exitCode"] == 0
    assert payload["project"] == {"root": None, "boardYaml": None}
    assert payload["issues"] == []
    assert payload["data"]["fault_detected"] is True
    assert payload["data"]["flags"][0]["name"] == "PRECISERR"
    assert payload["data"]["root_cause"]


def test_json_flag_stays_the_unwrapped_sdk_report_even_beside_format_json():
    """The command's OWN `--json` is the compatibility surface (see the module
    docstring): it prints exactly what `python -m alp_cli faultdecode --json`
    printed through `sdk_cli.rs`'s stdio forward, envelope-free. It wins when
    both spellings are given, so a caller that already passes `--json` cannot
    have its output shape changed out from under it by a `--format json` that
    something else in the argv chain added (tan-cli#399)."""
    result = runner.invoke(app, ["--cfsr", "0x8200", "--json", "--format", "json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["fault_detected"] is True
    assert "command" not in payload


def test_format_json_with_no_registers_emits_a_faultdecode_envelope():
    """The refusal path must agree with the success path about whether stdout
    is an envelope (tan-cli#399). Before this, `--format json` with no
    registers wrote the human error line to stderr and left `cli.main`'s
    generic fallback to invent a `command: "cli"` / `cli.parse-error`
    envelope for it -- a consumer that parsed stdout got a different
    `command` depending on whether the decode had worked."""
    result = runner.invoke(app, ["--format", "json"])
    assert result.exit_code == 2
    payload = json.loads(result.stdout)
    assert payload["command"] == "faultdecode"
    assert payload["ok"] is False
    assert payload["exitCode"] == 2
    assert payload["data"] is None
    assert [i["code"] for i in payload["issues"]] == ["faultdecode.no-registers"]
    assert "no fault registers supplied" in payload["issues"][0]["message"]


def test_format_json_before_subcommand_reads_off_ctx_obj():
    """Mirrors `debug-config`'s pre-subcommand plumbing (`cli.py`'s `root`
    callback stashes `--format` on `ctx.obj`; a command in
    `_HONOURS_ROOT_FORMAT` reads it back). Both positions must land on the
    SAME shape -- the envelope, since tan-cli#399 -- or `tan --format json
    faultdecode ...` and `tan faultdecode --format json ...` disagree about
    the wire for no reason a caller can see."""
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
    assert payload["command"] == "faultdecode"
    assert payload["data"]["fault_detected"] is True
    assert payload["data"]["flags"][0]["name"] == "PRECISERR"
    assert payload["data"]["root_cause"]


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


def test_explicit_flag_wins_over_a_parsed_dump():
    result = runner.invoke(app, ["--cfsr", "0x1", "--file", "-"], input="CFSR: 0x8200\n")
    assert result.exit_code == 0
    # bit 0 (IACCVIOL) from the explicit flag, not bit 9 (PRECISERR) from the dump.
    assert "IACCVIOL" in result.output
    assert "PRECISERR" not in result.output


# --------------------------------------------------------------------------
# tan-cli#503, defect 1 (a piped dump must still be READ and MERGED when a
# register flag is also given) and defect 3 (fd 0 closed must be a coded
# refusal, never a traceback) are DEFERRED to tan-cli#537, not fixed here:
# both require reworking `_stdin_offers_input`/`_stdin_offers_input_by_reading`
# (the implicit-stdin reader itself), and every rework of that reader tried so
# far closed one shape of hang/data-loss while opening another. This file
# reverted to `dev`'s own `_read_dump(file_, *, auto_consume_stdin=...)` gate,
# so a register flag suppresses the implicit stdin read exactly as it does on
# `dev` -- `test_explicit_flag_wins_over_a_parsed_dump` above and
# `test_stdin_dump_is_auto_consumed_when_piped` below still cover that
# register-free path; the closed-fd-0 and flag-plus-dump-merge cases are
# tan-cli#537's to add tests for, alongside the reader fix.
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# tan-cli#503, defect 2: addr2line must be resolved via `on_path` (PATH-only),
# never `shutil.which` (which inserts CWD ahead of PATH on Windows), and
# spawned by its resolved absolute path, never a bare name.
# --------------------------------------------------------------------------


def test_resolve_symbol_resolves_the_tool_through_on_path_not_shutil_which(
    monkeypatch, tmp_path
):
    from tan.commands import faultdecode_cmd

    elf = tmp_path / "zephyr.elf"
    elf.write_bytes(b"\x7fELF")

    calls: list[list[str]] = []

    def fake_on_path(command: str) -> str | None:
        if command == "arm-zephyr-eabi-addr2line":
            return None
        if command == "llvm-addr2line":
            return None
        if command == "addr2line":
            return "/usr/bin/addr2line"  # a resolved ABSOLUTE path
        raise AssertionError(f"unexpected tool probe: {command}")

    def fake_run(argv, **kwargs):
        calls.append(argv)

        class _Result:
            stdout = "my_func\nfile.c:42\n"

        return _Result()

    monkeypatch.setattr(faultdecode_cmd, "on_path", fake_on_path)
    monkeypatch.setattr(faultdecode_cmd.subprocess, "run", fake_run)

    sym = faultdecode_cmd.resolve_symbol(0x08001000, elf)
    assert sym is not None
    assert sym.func == "my_func"
    # The RESOLVED ABSOLUTE PATH was spawned, not the bare name "addr2line"
    # (tan-cli#503): a bare name would let a project-local decoy on CWD
    # (Windows CreateProcess search order) get executed a second way even
    # past a hardened probe.
    assert calls[0][0] == "/usr/bin/addr2line"


def test_resolve_symbol_skips_gracefully_when_no_tool_resolves(monkeypatch, tmp_path):
    from tan.commands import faultdecode_cmd

    elf = tmp_path / "zephyr.elf"
    elf.write_bytes(b"\x7fELF")
    monkeypatch.setattr(faultdecode_cmd, "on_path", lambda _tool: None)
    assert faultdecode_cmd.resolve_symbol(0x08001000, elf) is None
# `sys.stdin` itself, not just its `.isatty()`, can be `None` (tan-cli#488
# round 5 class sweep): a process launched with its standard handles
# detached -- a GUI launcher, a `pythonw`-style spawn, or a shell that closed
# fd 0 before exec. `CliRunner` can only ever hand the command a real, if
# captured, stdin object, so these call `_read_dump` directly -- the one
# place this defect actually lives.
# --------------------------------------------------------------------------


def test_read_dump_auto_consume_returns_no_dump_when_stdin_is_none(monkeypatch):
    """The IMPLICIT auto-consume path (`... | tan faultdecode`, no `--file`):
    a detached `sys.stdin` has nothing to offer, same as a closed pipe or a
    real tty -- `_read_dump` must answer `""`, not
    `AttributeError: 'NoneType' object has no attribute 'isatty'`."""
    from tan.commands.faultdecode_cmd import _read_dump

    monkeypatch.setattr(sys, "stdin", None)
    assert _read_dump(None, auto_consume_stdin=True) == ""


def test_read_dump_file_dash_refuses_cleanly_when_stdin_is_none(monkeypatch):
    """`--file -` NAMES stdin explicitly, so a detached `sys.stdin` there
    cannot silently fall back to `""` (that would tell the caller "no fault
    detected" for a request that was never actually served) -- it must
    refuse with a clear, coded message instead of a raw `AttributeError`."""
    from tan.commands.faultdecode_cmd import _read_dump

    monkeypatch.setattr(sys, "stdin", None)
    with pytest.raises(typer.BadParameter, match="stdin is detached"):
        _read_dump("-", auto_consume_stdin=True)



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


# --------------------------------------------------------------------------
# An OPEN stdin pipe must not hold the command hostage (tan-cli#388)
# --------------------------------------------------------------------------
#
# `CliRunner(input=...)` structurally cannot catch this: it hands the command
# an already-complete, already-CLOSED buffer, so `sys.stdin.read()` returns at
# once and every test above stayed green while `... | tan faultdecode` blocked
# forever in the field. The defect only exists across a real process boundary,
# where the PARENT owns the write end -- the default shape of
# `subprocess.Popen(..., stdin=PIPE)`, which is how alp-sdk-vscode, CI steps
# and wrapper scripts spawn `tan`. So these spawn the real CLI and, crucially,
# never close (nor write to) the pipe: `Popen.communicate()` is deliberately
# NOT used, because it closes stdin and would hide exactly the bug under test.

#: Generous enough that a cold interpreter start on a loaded CI box is never
#: mistaken for the hang, small enough that the hang is not mistaken for
#: slowness: the defect was unbounded (rc=124 at 12 s and at 25 s alike),
#: while the same argv with a closed stdin answered in 0.32 s.
_OPEN_STDIN_TIMEOUT_S = 20.0


def _run_with_stdin_held_open(args: list[str]) -> tuple[int, str, str]:
    """Spawn `python -m tan faultdecode <args>` with a stdin pipe that stays
    OPEN for the child's whole life, and return `(rc, stdout, stderr)`.

    Fails the test rather than hanging the suite if the child does not exit:
    an unbounded `wait()` here would turn this regression into a CI timeout
    with no attributable test name."""
    proc = subprocess.Popen(  # noqa: S603 -- fixed argv, no shell
        [sys.executable, "-m", "tan", "faultdecode", *args],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        rc = proc.wait(timeout=_OPEN_STDIN_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        pytest.fail(
            f"tan faultdecode {' '.join(args)} did not exit within "
            f"{_OPEN_STDIN_TIMEOUT_S}s with stdin held open (tan-cli#388)"
        )
    finally:
        # Order matters: read what the child produced BEFORE closing the pipes,
        # and close stdin last so nothing here can be mistaken for the EOF the
        # child was supposed not to need.
        out = proc.stdout.read()
        err = proc.stderr.read()
        proc.stdout.close()
        proc.stderr.close()
        proc.stdin.close()
    return rc, out, err


def test_registers_on_the_command_line_never_wait_for_an_open_stdin_pipe():
    """tan-cli#388, the primary documented invocation: a firmware engineer
    passes the registers a HardFault printed, wants no dump and supplied none.
    The read used to happen BEFORE the register flags were even parsed, so a
    parent holding the write end open blocked the child indefinitely with zero
    bytes on both streams -- no partial output, no error string, no exit code,
    nothing for the caller to classify."""
    rc, out, err = _run_with_stdin_held_open(["--cfsr", "0x8200", "--format", "json"])
    assert rc == 0, err
    payload = json.loads(out)
    assert payload["command"] == "faultdecode"
    assert payload["data"]["fault_detected"] is True
    assert payload["data"]["root_cause"]


def test_an_idle_open_stdin_pipe_falls_through_to_the_no_dump_refusal():
    """With NO registers and no `--file`, an open-but-idle pipe has nothing to
    read and never will until its writer decides otherwise. The auto-consume
    is gated on readiness so this lands on the ordinary "no fault registers"
    refusal -- exit 2 with a message naming the flags -- instead of blocking.

    The bytes assertion is the one tan-cli#388 closes with: `faultdecode` must
    never fail to terminate, and must never terminate, having written nothing
    at all."""
    rc, out, err = _run_with_stdin_held_open([])
    assert rc == 2
    assert "no fault registers supplied" in err
    assert (out + err) != ""


# --------------------------------------------------------------------------
# Every `--format json` command must have an envelope to emit (tan-cli#399)
# --------------------------------------------------------------------------

#: Registered commands whose `--format json` deliberately writes something
#: other than `{command, ok, exitCode, project, data, issues}`. EMPTY, and the
#: emptiness is the point: `contract/README.md`'s "Deliberately not covered"
#: rows list what the conformance fixtures do not exercise (`sdk list`,
#: `build --materialise`'s `data.written`, `kconfig`) -- every one of which
#: still EMITS an envelope, it is just not pinned by a golden. An exemption
#: here is a stronger claim than a missing fixture: "this verb answers
#: `--format json` with a non-envelope document". `faultdecode` was the only
#: such verb, argued for in its own module docstring and recorded nowhere a
#: consumer could read it, and tan-cli#399 closed it by enveloping the report
#: rather than by adding a row here. Adding an entry means updating
#: `contract/README.md` in the same commit, or the exemption is once again
#: only in the heads of the people who wrote it.
_NON_ENVELOPE_FORMAT_JSON_COMMANDS: dict[str, str] = {}


def _command_source(command) -> Path:
    """The `*_cmd.py` a registered click command's callback really lives in.

    Not `inspect.getsourcefile(command.callback)`: Typer registers a generated
    shim, and `inspect.unwrap` lands on `tan.core.global_flags`'s own wrapper
    for the ~half of the table that goes through `accept_global_flags` (which
    returns a bare closure, deliberately not `functools.wraps`-ed -- see its
    docstring on why the signature is rebuilt by hand). Nor a
    `name -> tan/commands/<name>_cmd.py` convention: `lock`, `migrate` and
    `quality` all live in `west_forward_cmd.py`, so that mapping would silently
    exempt three commands.

    Raises rather than returning a fallback if neither hop finds a
    `tan.commands.*` function: a gate that cannot locate its subject must go
    red, not quietly pass it."""
    func = inspect.unwrap(command.callback)
    if not (func.__module__ or "").startswith("tan.commands."):
        func = next(
            cell.cell_contents
            for cell in func.__closure__ or ()
            if callable(cell.cell_contents)
            and getattr(cell.cell_contents, "__module__", "").startswith("tan.commands.")
        )
    return Path(inspect.getsourcefile(func))


def test_every_registered_format_json_command_can_emit_an_envelope():
    """tan-cli#399, derived from the REGISTRATION SURFACE rather than a
    hand-kept list, so a command added to `cli.py` tomorrow is covered the day
    it lands.

    `new-som` accepted `--format json`, threw the value away
    (`del ... output_format`) and contained zero `emit(` calls, so its success
    path wrote 1238 bytes of plain text where the extension expected an
    envelope and its failure path emitted `cli.main`'s generic
    `command: "cli"` fallback -- the two paths of one command disagreeing
    about whether stdout is JSON. A command with no envelope path AT ALL is
    the shape this catches; the VALUES on the wire are pinned per command by
    `tests/conformance/test_contract_envelopes.py`'s goldens and by each
    command's own tests (`test_format_json_after_subcommand_wraps_the_report_
    in_the_envelope` above, `test_format_json_dry_run_emits_a_new_som_envelope`
    in `test_new_som_command.py`).

    Lives in this module because it is the always-collected one of the two
    tan-cli#399 touched -- `test_new_som_command.py`'s oracle-parity cases
    skip without an alp-sdk checkout, and a cross-command gate that can skip
    itself into vacuity is the failure mode `contract/README.md` calls out."""
    from typer.main import get_command

    from tan.cli import app as real_app

    click_app = get_command(real_app)
    accepts_format = {
        name: command
        for name, command in click_app.commands.items()
        if any("--format" in (param.opts or []) for param in command.params)
    }
    # Non-vacuity: `--format` is the extension's only output switch and most of
    # the table declares it. A scan that suddenly matched a handful of commands
    # would pass while checking almost nothing.
    assert len(accepts_format) >= 20, sorted(accepts_format)

    offenders = sorted(
        name
        for name, command in accepts_format.items()
        if name not in _NON_ENVELOPE_FORMAT_JSON_COMMANDS
        and "emit(" not in _command_source(command).read_text(encoding="utf-8")
    )
    assert offenders == [], (
        "command(s) accepting `--format json` with no `emit(` call anywhere in "
        "their module -- they cannot produce the envelope "
        "`{command, ok, exitCode, project, data, issues}` the extension's "
        f"`isEnvelope` guard requires: {offenders}"
    )
