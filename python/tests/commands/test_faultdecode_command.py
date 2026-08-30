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
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from tan.commands.faultdecode_cmd import faultdecode
from tan.core.shapes import SDK_MARKER
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
        root = Path(override)
        candidate = root / "scripts" / "alp_cli" / "faultdecode.py"
        if candidate.is_file():
            return candidate
        if root.joinpath(*SDK_MARKER).is_file():
            # A REAL alp-sdk that no longer ships the oracle. alp-sdk#1367/
            # #1368 (`210e9fed`, "finish the alp_cli retirement") deleted
            # `scripts/alp_cli/faultdecode.py` outright -- 670 lines, along
            # with twelve sibling modules -- once `tan faultdecode` shipped
            # the native port. There is no live oracle left to diff against
            # at that ref or any later one, and there never will be again,
            # so refusing to skip here would turn a permanent upstream fact
            # into a permanent red.
            #
            # This does NOT silently drop the coverage, which is the thing
            # the refusal below exists to prevent.
            # `tests/fixtures/faultdecode_golden.json` was frozen FROM that
            # module while it still shipped (see its PROVENANCE.txt), and
            # `test_bit_tables_match_the_frozen_golden` /
            # `test_decode_matches_the_frozen_golden` assert tan's port
            # against it on EVERY run -- bound or not, oracle present or
            # not. What is lost is only the live re-verification, and that
            # was lost upstream, not here.
            pytest.skip(
                "the bound alp-sdk retired scripts/alp_cli/faultdecode.py "
                "(alp-sdk#1367/#1368, landed in 210e9fed), so there is no "
                "live oracle to diff against at this ref. The frozen golden "
                "tests/fixtures/faultdecode_golden.json is the authority "
                "now, and its two checks run unconditionally."
            )
        raise RuntimeError(
            f"ALP_SDK_ROOT={override!r} has no scripts/alp_cli/faultdecode.py "
            f"and no {'/'.join(SDK_MARKER)} either, so it does not name an "
            "alp-sdk checkout at all. Refusing to skip: a named-but-missing "
            "oracle would make this check pass vacuously. Fix the path, or "
            "unset it."
        )
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


# The divergence test that used to live here --
# `test_the_sdk_original_decodes_a_negative_cfsr_and_tan_deliberately_does_not`
# -- pinned that alp-sdk's `scripts/alp_cli/faultdecode.py` accepted
# `--cfsr=-8200` and exited 0, while tan refused at exit 2, with the docstring
# itself saying "the day upstream adopts the refusal this test goes red and is
# deleted with a note". alp-sdk dad5b35a (#1389, inside the a3173305..d00dbdc1
# pin range) did exactly that -- `_HexInt.convert` now rejects a negative
# parsed value the same way tan's `_parse_hexint` already did -- so the
# oracle-side half of the assertion (`exit_code == 0`) went red first. Deleted
# per the docstring's own instruction rather than loosened; the refusal itself
# stays covered by the tests above, which assert tan's behaviour directly and
# do not depend on an SDK checkout being reachable.


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
# tan-cli#503, defect 1: a piped/pasted dump must still be READ and MERGED
# when a register flag is ALSO given -- the implicit stdin read used to be
# suppressed entirely by `not registers_given`, silently dropping whatever
# the dump carried (the command's own help promises "Explicit flags win over
# a parsed dump", which is only true if the dump is still read when a flag is
# present too).
# --------------------------------------------------------------------------


def test_a_piped_dump_is_still_read_and_merged_when_a_register_flag_is_also_given():
    """Reproduces tan-cli#503 defect 1's failure scenario exactly: `--hfsr`
    alone used to suppress the implicit stdin read, so a piped CFSR/BFAR was
    dropped and the tool reported the opposite cause (a "Forced HardFault"
    escalation with "its own status bits ... clear", when CFSR=0x00008200 is
    a precise bus fault with BFAR=0xdeadbeef). Both must now show up merged
    with the explicit HFSR."""
    result = runner.invoke(
        app,
        ["--hfsr", "0x40000000"],
        input="CFSR: 0x00008200\nBFAR: 0xdeadbeef\n",
    )
    assert result.exit_code == 0
    assert "PRECISERR" in result.output
    assert "0xdeadbeef" in result.output
    assert "Precise data bus fault" in result.output
    # The old bug's report -- must NOT appear now that CFSR/BFAR are read.
    assert "its own status bits are clear" not in result.output


def test_an_address_only_flag_does_not_suppress_the_piped_dump():
    """`--bfar`/`--mmfar` are address registers, not one of the cfsr/hfsr/dfsr
    "something to analyse" registers `faultdecode` gates on -- so giving one
    alone must not (a) throw away a piped CFSR, nor (b) hit the
    `faultdecode.no-registers` refusal, both of which happened before this
    fix because `--bfar` counted toward suppressing the dump read without
    counting toward the gate that read was meant to satisfy."""
    result = runner.invoke(app, ["--bfar", "0x20001000"], input="CFSR: 0x00008200\n")
    assert result.exit_code == 0
    assert "PRECISERR" in result.output
    # The explicit --bfar flag wins over the dump's own (absent) BFAR.
    assert "0x20001000" in result.output


def test_json_envelope_merges_a_piped_dump_with_an_explicit_flag():
    """Same merge, `--format json`: the explicit HFSR and the piped
    CFSR/BFAR must both land in `data.inputs`/`data.addresses`, not just one
    or the other."""
    result = runner.invoke(
        app,
        ["--hfsr", "0x40000000", "--format", "json"],
        input="CFSR: 0x00008200\nBFAR: 0xdeadbeef\n",
    )
    assert result.exit_code == 0
    payload = json.loads(result.output)
    data = payload["data"]
    assert data["inputs"]["cfsr"] == "0x00008200"
    assert data["inputs"]["hfsr"] == "0x40000000"
    assert data["addresses"]["bfar"] == "0xdeadbeef"


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


def test_read_dump_returns_empty_when_no_file_is_named():
    """`_read_dump` is the `--file` (path or `-`) handler ONLY since
    tan-cli#537 -- the implicit auto-consume path (no `--file` at all) is
    `_read_implicit_stdin`, exercised separately below."""
    from tan.commands.faultdecode_cmd import _read_dump

    assert _read_dump(None) == ""


def test_read_dump_file_dash_refuses_cleanly_when_stdin_is_none(monkeypatch):
    """`--file -` NAMES stdin explicitly, so a detached `sys.stdin` there
    cannot silently fall back to `""` (that would tell the caller "no fault
    detected" for a request that was never actually served) -- it must
    refuse with a clear, coded message instead of a raw `AttributeError`."""
    from tan.commands.faultdecode_cmd import _read_dump

    monkeypatch.setattr(sys, "stdin", None)
    with pytest.raises(typer.BadParameter, match="stdin is detached"):
        _read_dump("-")


def test_read_dump_file_dash_refuses_cleanly_when_stdin_has_no_buffer(monkeypatch):
    """tan-cli#537: reading `.buffer` adds a shape `sys.stdin is None` alone
    does not cover -- a text-only replacement stream (e.g. `io.StringIO`)
    that exists, answers `.isatty()`, but has no `.buffer` at all. `--file -`
    names stdin explicitly, so this must refuse cleanly too, not raise
    `AttributeError` from inside `.buffer.read()`."""
    import io

    from tan.commands.faultdecode_cmd import _read_dump

    monkeypatch.setattr(sys, "stdin", io.StringIO("CFSR: 0x8200\n"))
    with pytest.raises(typer.BadParameter, match="no binary buffer"):
        _read_dump("-")


def test_read_dump_file_dash_and_file_path_decode_identical_bytes_the_same_way():
    """tan-cli#537's decode-parity requirement, at the `_read_dump` unit
    level: `--file <path>` and `--file -` must decode an identical byte
    sequence -- including a stray non-UTF-8 byte -- to the identical string.
    Both go through `errors="ignore"` UTF-8 decoding of the same bytes; a
    split between a text-layer read and a bytes-layer read is exactly what
    let attempt 5 diverge from `--file`."""
    import io

    from tan.commands.faultdecode_cmd import _read_dump

    raw = b"CFSR: 0x8200\n\xffBFAR: 0xdeadbeef\n"

    class _FakeBuffer:
        def read(self):
            return raw

    class _FakeStdin:
        buffer = _FakeBuffer()

    import tempfile

    with tempfile.NamedTemporaryFile(delete=False) as handle:
        handle.write(raw)
        path = handle.name
    try:
        from_file = _read_dump(path)
    finally:
        Path(path).unlink()

    import sys as _sys

    old_stdin = _sys.stdin
    _sys.stdin = _FakeStdin()
    try:
        from_stdin_dash = _read_dump("-")
    finally:
        _sys.stdin = old_stdin

    assert from_file == from_stdin_dash
    assert from_file == raw.decode("utf-8", errors="ignore")


# --------------------------------------------------------------------------
# tan-cli#537: `_read_implicit_stdin` / `_read_stdin_bounded` -- the
# implicit-stdin reader itself, unit-tested with a fake byte source so the
# three bounds (idle/byte-cap/total) can be proven without a real 2s/1MiB/30s
# wait. The real-subprocess scenarios (decode parity end-to-end, slow-but-
# steady, infinite producer, silent holder, detached stdin) are driven
# against the actual binary further below, per the design's own "the
# CliRunner layer is structurally blind here" call-out.
# --------------------------------------------------------------------------


class _ScriptedBuffer:
    """A fake binary buffer whose `.read1(n)` plays back a scripted sequence
    of `(chunk, delay_s)` pairs, sleeping `delay_s` before returning each
    `chunk` -- lets the idle/byte-cap/total bounds in `_read_stdin_bounded`
    be proven deterministically, without spawning a real pipe.

    A script that ends WITHOUT an explicit `(b"", 0.0)` EOF entry means a
    writer that stops writing but never closes its end: once exhausted, the
    next `.read1()` call BLOCKS forever (a real held-open pipe would too),
    so an idle/total bound firing in that case is a real bound firing, not
    an accidental EOF the fake buffer invented."""

    def __init__(self, script: list[tuple[bytes, float]]):
        self._script = list(script)
        self._never = threading.Event()

    def read1(self, _n: int) -> bytes:
        if not self._script:
            self._never.wait()  # blocks forever -- see class docstring
            return b""  # pragma: no cover - unreachable, _never is never set
        chunk, delay = self._script.pop(0)
        if delay:
            time.sleep(delay)
        return chunk


def test_read_stdin_bounded_reads_to_a_clean_eof_with_no_bound_firing():
    from tan.commands.faultdecode_cmd import _read_stdin_bounded

    buf = _ScriptedBuffer([(b"CFSR: 0x8200\n", 0.0), (b"", 0.0)])
    outcome = _read_stdin_bounded(buf, idle_s=1.0, byte_cap=1_000_000, total_s=10.0)
    assert outcome.data == b"CFSR: 0x8200\n"
    assert outcome.bound is None


def test_read_stdin_bounded_idle_bound_keeps_data_read_so_far():
    """Reproduces attempt 1's exact failure shape and proves this design does
    NOT have it: a chunk that already arrived is never discarded just
    because the NEXT one never comes."""
    from tan.commands.faultdecode_cmd import _read_stdin_bounded

    buf = _ScriptedBuffer([(b"CFSR: 0x8200\n", 0.0)])  # then blocks forever
    outcome = _read_stdin_bounded(buf, idle_s=0.05, byte_cap=1_000_000, total_s=10.0)
    assert outcome.data == b"CFSR: 0x8200\n"
    assert outcome.bound == "idle"


def test_read_stdin_bounded_idle_resets_on_every_chunk_a_slow_producer_finishes():
    """The constraint attempt 2's TOTAL budget got wrong: an idle bound that
    RESETS on every chunk reads a slow-but-steady producer in full, however
    long it takes overall."""
    from tan.commands.faultdecode_cmd import _read_stdin_bounded

    script = [(f"L{i}\n".encode(), 0.05) for i in range(6)] + [(b"", 0.0)]
    outcome = _read_stdin_bounded(buf := _ScriptedBuffer(script), idle_s=0.5, byte_cap=1_000_000, total_s=10.0)
    assert outcome.data == b"".join(c for c, _ in script)
    assert outcome.bound is None


def test_read_stdin_bounded_byte_cap_fires_before_memory_grows_unbounded():
    """The bound no earlier attempt had: an infinite producer must stop
    accumulating once the byte cap is reached, not once a total elapses.

    `_InfiniteBuffer` caps its own output at 10 MB then blocks forever,
    rather than producing truly without limit: `_read_stdin_bounded`
    abandons its reader thread (by design) once the byte cap fires, and a
    fake buffer with no backpressure at all would leave that thread
    spinning `b"x" * n` allocations as fast as the CPU allows for the rest
    of THIS test process's life -- measured, this is what made the full
    `test_faultdecode_command.py` run OOM (>20 GB RSS) when this fixture
    used an actually-unbounded generator. A real OS pipe never has this
    problem: its kernel buffer fills and the writer blocks once nobody is
    reading, which is the backpressure this fake reproduces."""
    from tan.commands.faultdecode_cmd import _read_stdin_bounded

    class _InfiniteBuffer:
        def __init__(self) -> None:
            self._produced = 0
            self._never = threading.Event()

        def read1(self, n: int) -> bytes:
            if self._produced > 10_000_000:  # far past any byte_cap under test
                self._never.wait()  # simulates a full kernel pipe buffer
                return b""  # pragma: no cover - unreachable
            self._produced += n
            return b"x" * n

    outcome = _read_stdin_bounded(
        _InfiniteBuffer(), idle_s=5.0, byte_cap=1000, total_s=10.0
    )
    assert outcome.bound == "byte-cap"
    assert len(outcome.data) == 1000


def test_read_stdin_bounded_total_cap_fires_for_a_producer_that_never_idles_or_caps():
    """The backstop bound: a producer that keeps resetting the idle timer
    (never idle long enough) and never reaches the byte cap must still
    terminate eventually."""
    from tan.commands.faultdecode_cmd import _read_stdin_bounded

    script = [(b"a", 0.03) for _ in range(20)]  # never empty, never idles long
    outcome = _read_stdin_bounded(
        _ScriptedBuffer(script), idle_s=1.0, byte_cap=1_000_000, total_s=0.2
    )
    assert outcome.bound == "total-cap"
    assert len(outcome.data) > 0


def test_read_stdin_bounded_slow_but_steady_dump_is_read_in_full():
    """The measured attempt 6/7 killer, reproduced directly against
    `_read_stdin_bounded`: 26 lines with 0.24s gaps must decode in full under
    this repo's OWN chosen idle bound, with no total cap anywhere near firing
    (uses the real module-default idle/byte-cap/total, not injected small
    ones, precisely because this is the regression the real defaults must
    not reintroduce)."""
    from tan.commands.faultdecode_cmd import _read_stdin_bounded

    lines = [f"L{i:02d}: 0x{i:08x}\n".encode() for i in range(26)]
    script = [(chunk, 0.24) for chunk in lines] + [(b"", 0.0)]
    outcome = _read_stdin_bounded(_ScriptedBuffer(script))
    assert outcome.data == b"".join(lines)
    assert outcome.bound is None


def test_read_implicit_stdin_reports_not_attempted_when_stdin_is_none(monkeypatch):
    from tan.commands.faultdecode_cmd import _read_implicit_stdin

    monkeypatch.setattr(sys, "stdin", None)
    result = _read_implicit_stdin()
    assert result.text == ""
    assert result.attempted is False
    assert result.bound is None


def test_read_implicit_stdin_reports_not_attempted_when_stdin_has_no_buffer(monkeypatch):
    """tan-cli#537: a text-only replacement stream (no `.buffer`) must
    degrade to "no implicit dump" cleanly, never raise."""
    import io

    from tan.commands.faultdecode_cmd import _read_implicit_stdin

    fake = io.StringIO("CFSR: 0x8200\n")
    monkeypatch.setattr(sys, "stdin", fake)
    result = _read_implicit_stdin()
    assert result.text == ""
    assert result.attempted is False


def test_read_implicit_stdin_reports_not_attempted_on_a_tty(monkeypatch):
    """A `.buffer` IS present here (unlike the None/no-buffer tests above) --
    deliberately, so this test isolates the TTY check specifically: without
    it, `_read_implicit_stdin` would fall through to `_read_stdin_bounded`
    on `_buffer` and this assertion would fail on `attempted`, not pass by
    accident via the separate no-`.buffer` guard."""
    import io

    from tan.commands.faultdecode_cmd import _read_implicit_stdin

    class _TtyStdin:
        buffer = io.BytesIO(b"CFSR: 0x8200\n")

        def isatty(self) -> bool:
            return True

    monkeypatch.setattr(sys, "stdin", _TtyStdin())
    result = _read_implicit_stdin()
    assert result.attempted is False



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
    is gated on the idle bound so this lands on the ordinary "no fault
    registers" refusal -- exit 2 -- instead of blocking, and (tan-cli#537)
    the refusal now says WHY: "stdin was open but silent", distinguishing
    this case from "no stdin was ever offered at all" (a closed pipe, a TTY,
    or a detached stdin), which keeps the older, plainer wording.

    The bytes assertion is the one tan-cli#388 closes with: `faultdecode` must
    never fail to terminate, and must never terminate, having written nothing
    at all."""
    rc, out, err = _run_with_stdin_held_open([])
    assert rc == 2
    assert "no fault registers supplied" in err
    assert "stdin was open but silent" in err
    assert (out + err) != ""


# --------------------------------------------------------------------------
# tan-cli#537: the implicit-stdin reader's real-subprocess scenarios. The
# in-process `CliRunner` layer is structurally blind here -- it hands the
# command an already-closed buffer, so a green `CliRunner` run is compatible
# with a total field hang. These extend `_run_with_stdin_held_open`'s real
# OS-pipe harness with scripted WRITERS, so each of the design's scenarios is
# driven against the actual binary, not simulated.
# --------------------------------------------------------------------------


def _run_with_scripted_stdin_writer(args: list[str], writer_code: str) -> tuple[int, str, str]:
    """Spawn `python -m tan faultdecode <args>` with stdin fed by a SEPARATE
    child process running `writer_code` (a `python -c` snippet writing to its
    own stdout, piped into the `tan faultdecode` child's stdin) -- so writes
    can be scheduled with real delays without this test process itself
    blocking on a write the reader might not drain fast enough.

    Whether the writer's own process (and therefore the pipe's write end)
    stays alive/open after writing -- so the read side can only terminate via
    a bound firing, never via EOF -- is entirely up to `writer_code` itself
    (`_SLOW_STEADY_WRITER` exits promptly; `_WRITE_THEN_HOLD_OPEN_WRITER`
    sleeps past the idle bound before exiting); there is nothing this helper
    could do to affect that from the outside, so it takes no flag for it."""
    writer = subprocess.Popen(  # noqa: S603 -- fixed argv, no shell
        [sys.executable, "-c", writer_code],
        stdout=subprocess.PIPE,
    )
    proc = subprocess.Popen(  # noqa: S603 -- fixed argv, no shell
        [sys.executable, "-m", "tan", "faultdecode", *args],
        stdin=writer.stdout,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    writer.stdout.close()  # this process's own handle; the child still has it
    try:
        rc = proc.wait(timeout=_OPEN_STDIN_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        pytest.fail(
            f"tan faultdecode {' '.join(args)} did not exit within "
            f"{_OPEN_STDIN_TIMEOUT_S}s against a scripted stdin writer"
        )
    finally:
        out = proc.stdout.read()
        err = proc.stderr.read()
        proc.stdout.close()
        proc.stderr.close()
        writer.kill()
        writer.wait()
    return rc, out, err


def test_decode_parity_between_the_implicit_pipe_and_file_including_a_stray_byte():
    """tan-cli#537's named regression pin: identical bytes -- including ONE
    stray non-UTF-8 byte -- through the IMPLICIT stdin pipe and through
    `--file` must decode to byte-identical `--format json` output. This is
    the one test the issue names as sufficient to have caught attempts 3, 5
    and 6: a per-chunk text-layer decode (3), a `reconfigure()` that patched
    only the stdin half of the `--file` idiom (5), and a total-time cap that
    silently dropped a slow dump (6) would all show up here as a DIFFERENT
    payload between the two paths, or a truncated one."""
    raw = b"CFSR: 0x00008200\nBFAR: 0x\xffdeadbeef\n"

    with tempfile.NamedTemporaryFile(delete=False) as handle:
        handle.write(raw)
        path = handle.name
    try:
        file_result = subprocess.run(
            [sys.executable, "-m", "tan", "faultdecode", "--file", path, "--format", "json"],
            capture_output=True, text=True, timeout=_OPEN_STDIN_TIMEOUT_S, check=False,
        )
    finally:
        Path(path).unlink()

    pipe_result = subprocess.run(
        [sys.executable, "-m", "tan", "faultdecode", "--format", "json"],
        input=raw,
        capture_output=True, timeout=_OPEN_STDIN_TIMEOUT_S, check=False,
    )

    assert file_result.returncode == 0, file_result.stderr
    assert pipe_result.returncode == 0, pipe_result.stderr
    file_payload = json.loads(file_result.stdout)
    pipe_payload = json.loads(pipe_result.stdout.decode("utf-8"))
    assert file_payload["data"] == pipe_payload["data"]
    assert file_payload["data"]["addresses"]["bfar"] == "0xdeadbeef"


#: The write side of the 6/7-killer fixture: 26 lines, one every 0.24s, THEN
#: close -- the exact shape the issue measured as taking 6.2s total and
#: being cut at a reverted 5.0s constant. Written as a `python -c` snippet
#: (not inline Python in this test process) so the delays are real
#: wall-clock time in a SEPARATE process, matching how a serial capture
#: script would actually feed `tan faultdecode`.
_SLOW_STEADY_WRITER = (
    "import sys, time\n"
    "for i in range(26):\n"
    "    sys.stdout.buffer.write(f'CFSR: 0x{(0x8000 + i):08x}\\n'.encode())\n"
    "    sys.stdout.buffer.flush()\n"
    "    time.sleep(0.24)\n"
)


def test_slow_but_steady_dump_is_decoded_in_full_with_no_truncation_notice():
    """The measured attempt-6/7 killer, driven against the real binary: 26
    lines with 0.24s gaps (~6.2s total) must decode in FULL, with NO
    truncation notice on stderr -- this is the fixture that fails against
    the reverted "20x the idle window" constant and would have caught it
    before it shipped."""
    rc, out, err = _run_with_scripted_stdin_writer(["--format", "json"], _SLOW_STEADY_WRITER)
    assert rc == 0, err
    payload = json.loads(out)
    assert payload["data"]["inputs"]["cfsr"] == "0x00008019"  # the LAST line wins (last parsed)
    assert payload["issues"] == []
    assert "stdin idle" not in err
    assert "stdin exceeded" not in err
    assert "stdin did not reach EOF" not in err


#: A complete dump, flushed, then the writer sleeps far past the idle bound
#: before exiting -- so the PIPE stays open (write end held) well past
#: `_STDIN_IDLE_TIMEOUT_S` even though nothing MORE was ever going to
#: arrive. Proves the "partial write, then hold open" scenario: the dump
#: must still be decoded and the run must still exit 0, with the idle bound
#: announced (this reader cannot know the writer had nothing left to say).
_WRITE_THEN_HOLD_OPEN_WRITER = (
    "import sys, time\n"
    "sys.stdout.buffer.write(b'CFSR: 0x00008200\\nBFAR: 0xdeadbeef\\n')\n"
    "sys.stdout.buffer.flush()\n"
    "time.sleep(6)\n"
)


def test_a_complete_dump_written_then_held_open_past_the_idle_bound_still_decodes():
    rc, out, err = _run_with_scripted_stdin_writer(
        ["--format", "json"], _WRITE_THEN_HOLD_OPEN_WRITER
    )
    assert rc == 0, err
    payload = json.loads(out)
    assert payload["data"]["inputs"]["cfsr"] == "0x00008200"
    assert payload["data"]["addresses"]["bfar"] == "0xdeadbeef"
    assert "stdin idle" in err
    assert [i["code"] for i in payload["issues"]] == ["faultdecode.stdin-truncated"]


#: A PREAMBLE with no register values at all, then held open past the idle
#: bound -- the shape a bound can fire on WITHOUT ever producing a complete
#: register set (tan-cli#537 constraint 4's hole, closed after initial
#: review): unlike `_WRITE_THEN_HOLD_OPEN_WRITER` above, nothing here ever
#: parses into cfsr/hfsr/dfsr, so this lands on the `faultdecode.no-registers`
#: REFUSAL, not a successful decode -- the exact case a refusal used to
#: announce nothing about.
_PREAMBLE_THEN_HOLD_OPEN_WRITER = (
    "import sys, time\n"
    "sys.stdout.buffer.write(b'preamble that is not a register at all\\n')\n"
    "sys.stdout.buffer.flush()\n"
    "time.sleep(6)\n"
)


def test_a_bound_firing_with_bytes_but_no_registers_still_announces_on_stderr():
    """The reviewer's own reproduction of constraint 4's hole on `#998`: bytes
    arrive, the idle bound fires, but nothing in what arrived is a complete
    register set -- so this falls all the way through to the
    `faultdecode.no-registers` refusal. Before the fix, that refusal path
    built its own message from scratch and never looked at whether a bound
    had fired, so the truncation vanished -- the engineer was told "no fault
    registers supplied" (implying nothing arrived at all) with EMPTY stderr,
    when tan had in fact read 39 bytes and then given up.

    Asserted in DEFAULT TEXT MODE specifically, not `--format json`:
    constraint 4 says the announcement belongs on the default surface, and
    `_refuse`'s envelope-mode branch and its plain-text branch build stderr
    differently enough that only actually exercising the plain-text path
    proves it."""
    rc, out, err = _run_with_scripted_stdin_writer([], _PREAMBLE_THEN_HOLD_OPEN_WRITER)
    assert rc == 2
    assert out == ""
    assert "Warning: stdin idle" in err  # the bound announcement...
    assert "Error: no fault registers supplied" in err  # ...alongside the refusal
    assert "stopped reading part-way" in err  # ...distinguished from "nothing arrived"


def test_an_infinite_producer_terminates_via_the_byte_cap_and_announces_truncation():
    """The `yes` shape (issue #537): unguarded, this reached 409.7 MB RSS over
    9.59s. Guarded, the end-to-end SUBPROCESS must terminate (via the byte
    cap, well before the idle/total bounds would even matter for a fast
    producer) and announce truncation -- proven here at the timing/exit-code/
    issues[] level, on the real binary.

    Peak RSS is NOT measured here -- by the time this subprocess exits (a
    handful of milliseconds after the cap fires: decode + render + exit,
    nothing slower), there is no live PID left to sample, and the previous
    name of this test (`..._stays_far_under_the_byte_cap`) claimed a
    memory-bound property this test could never have caught regardless of
    what the queue actually did. `test_read_stdin_bounded_queue_stays_
    bounded_after_the_byte_cap_fires` below is the one that actually
    measures RSS -- it calls `_read_stdin_bounded` in-process against a real
    OS pipe precisely so the reader thread's growth after abandonment stays
    observable, which a subprocess boundary here would hide."""
    yes = subprocess.Popen(["yes", "CFSR: 0x00008200"], stdout=subprocess.PIPE)
    proc = subprocess.Popen(
        [sys.executable, "-m", "tan", "faultdecode", "--format", "json"],
        stdin=yes.stdout,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    yes.stdout.close()
    try:
        rc = proc.wait(timeout=_OPEN_STDIN_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        pytest.fail("an infinite stdin producer was not bounded by the byte cap")
    finally:
        out = proc.stdout.read()
        err = proc.stderr.read()
        proc.stdout.close()
        proc.stderr.close()
        yes.kill()
        yes.wait()
    assert rc == 0, err
    payload = json.loads(out)
    assert payload["data"]["fault_detected"] is True
    assert "stdin exceeded" in err
    assert [i["code"] for i in payload["issues"]] == ["faultdecode.stdin-truncated"]


@pytest.mark.skipif(
    sys.platform == "win32", reason="RSS sampling here is `resource.getrusage`, POSIX-only"
)
def test_read_stdin_bounded_queue_stays_bounded_after_the_byte_cap_fires():
    """tan-cli#537 follow-up: constraint 3 says the cap bounds the BUFFER,
    but until now the abandoned reader thread (see `_read_stdin_bounded`'s
    own docstring on why it is abandoned, not joined) kept enqueuing to an
    unbounded `queue.Queue()` that nobody was left to drain. Measured
    against a real OS pipe fed as fast as a background thread could write,
    timestamped from the moment the byte cap fired: 640.1 MB at +0.5s,
    1059.6 MB at +1.0s, 2001.9 MB at +2.0s, 3845.8 MB at +4.0s -- bounded
    only by how soon the process happened to exit, which is not a bound.

    Calls `_read_stdin_bounded` directly, in-process, rather than driving
    the full `tan faultdecode` subprocess (the test above): the guarded
    subprocess exits within milliseconds of the cap firing, before there is
    any live PID left whose RSS could be sampled meaningfully. Keeping the
    reader thread inside THIS test process, and this process alive for a
    few seconds after the cap fires, is what makes the abandoned thread's
    growth observable at all.

    `_STDIN_DRAIN_QUEUE_MAXSIZE` is the fix under test: `ru_maxrss` is a
    high-water mark, so comparing it before the call against several
    seconds after asserts the abandoned thread cannot keep pushing that
    peak up without limit, without needing to poll a live RSS number."""
    import resource

    from tan.commands.faultdecode_cmd import _read_stdin_bounded

    read_fd, write_fd = os.pipe()

    def _feed_forever() -> None:
        chunk = b"x" * 65536
        try:
            while True:
                os.write(write_fd, chunk)
        except OSError:
            pass  # the read end closed at teardown -- this thread is daemon

    feeder = threading.Thread(target=_feed_forever, daemon=True)
    feeder.start()

    class _RealFdBuffer:
        """Wraps a raw fd so `_read_stdin_bounded`'s `buffer.fileno()` probe
        takes the real-`os.read` path, not the `.read1()` fallback -- the
        shape a real `sys.stdin.buffer` over a pipe actually is."""

        def fileno(self) -> int:
            return read_fd

    try:
        before_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        outcome = _read_stdin_bounded(
            _RealFdBuffer(), idle_s=5.0, byte_cap=1_048_576, total_s=10.0
        )
        assert outcome.bound == "byte-cap"
        assert len(outcome.data) == 1_048_576
        time.sleep(2.0)  # give the abandoned reader thread room to misbehave
        after_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        # ru_maxrss is KiB on Linux, bytes on Darwin (a long-standing libc
        # quirk) -- normalise both to MB so one assertion covers both CI
        # runners this suite ships on (ubuntu-latest, macos-latest).
        unit_kib = 1.0 if sys.platform != "darwin" else 1.0 / 1024.0
        grew_mb = (after_kb - before_kb) * unit_kib / 1024.0
        assert grew_mb < 50, (
            f"RSS grew {grew_mb:.1f} MB in 2s after the byte cap fired -- "
            "the abandoned reader thread's queue is not bounded"
        )
    finally:
        os.close(write_fd)
        os.close(read_fd)


def test_detached_stdin_devnull_is_a_clean_no_dump_not_a_crash():
    """`stdin=DEVNULL` is immediate EOF, zero bytes -- one of the "detached /
    replaced stdin" shapes tan-cli#537 names, driven end to end rather than
    at the `_read_implicit_stdin` unit level."""
    result = subprocess.run(
        [sys.executable, "-m", "tan", "faultdecode", "--cfsr", "0x8200", "--format", "json"],
        stdin=subprocess.DEVNULL,
        capture_output=True, text=True, timeout=_OPEN_STDIN_TIMEOUT_S, check=False,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["data"]["fault_detected"] is True
    assert payload["issues"] == []


@pytest.mark.skipif(
    sys.platform == "win32", reason="preexec_fn is POSIX-only; Popen raises on Windows"
)
def test_detached_stdin_closed_fd_is_a_clean_no_dump_not_a_crash():
    """A shell (or launcher) that closes fd 0 before exec -- `sys.stdin` is
    still a real object, but reading it raises. Must degrade to "no implicit
    dump" cleanly.

    A GENUINELY closed fd 0, not `stdin=subprocess.DEVNULL` (the test above):
    `preexec_fn` runs in the child between `fork()` and `exec()`, before
    `subprocess` would otherwise `dup2` a redirection onto fd 0 -- passing no
    `stdin=` kwarg here means no such `dup2` ever happens, so the `os.close(0)`
    below survives into the exec'd process. `/dev/null` is an OPEN fd whose
    reads return `b""` immediately; a CLOSED fd 0 makes `buffer.fileno()`
    still return `0` (the number is just stored, not validated) but the
    background reader's `os.read(0, ...)` then raises `OSError` (EBADF) --
    caught by `_drain`'s own `except (OSError, ValueError)`, not the clean-EOF
    path `DEVNULL` takes. Verified this doesn't crash before writing this
    test: `preexec_fn=os.close(0)` gave rc 0 with `--cfsr` supplied and this
    same rc 2 without it, never raising -- this test pins the second half of
    that, plus that the no-flags refusal path still answers (not the
    open-but-silent wording, since there is no pipe at all here to have been
    silent on)."""
    result = subprocess.run(
        [sys.executable, "-m", "tan", "faultdecode", "--format", "json"],
        preexec_fn=lambda: os.close(0),  # noqa: S603 -- POSIX-only; a real closed fd 0
        capture_output=True, text=True, timeout=_OPEN_STDIN_TIMEOUT_S, check=False,
    )
    assert result.returncode == 2
    payload = json.loads(result.stdout)
    assert payload["issues"][0]["code"] == "faultdecode.no-registers"
    assert "stdin was open but silent" not in payload["issues"][0]["message"]


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
