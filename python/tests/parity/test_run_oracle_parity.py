# SPDX-License-Identifier: Apache-2.0
"""`tan run` against the shipped Rust oracle.

**The option-set pin runs unconditionally** (skipped only when no oracle
binary is available): it proves every flag `run_cmd.run` declares genuinely
exists in the REAL `tan run --help` output, so this port can never invent a
flag the shipped binary does not have.

**The full-envelope cases are `xfail(strict=True)`, not skipped**, following
`tests/parity/test_oracle_parity.py`'s own precedent for a command not yet
wired end to end: `run` is not yet registered in `tan/cli.py` (a shared
registration point another workflow step owns), so `python -m tan run ...`
404s with "no such command" today. `strict=True` means the day that
registration lands these XPASS and fail the suite -- forcing the one-line
promotion (drop the marker) instead of a landed command silently staying
mis-classified as "not wired" forever.
"""
import re
import subprocess

import pytest
import typer
from typer.main import get_command

from tan.commands.run_cmd import run as run_fn

from . import oracle_fixtures
from .oracle import ENVELOPE, compare, missing_for_live, rust_binary

RUST = rust_binary()
LIVE_GATE = pytest.mark.skipif(
    missing_for_live(RUST),
    reason="TAN_PARITY_LIVE=1 needs a Rust tan; set TAN_RUST_BINARY or run `cargo build`",
)


def _rust_help(*argv: str) -> str:
    """`tan <argv> --help`'s raw stdout, frozen by default (tan-cli#272) --
    static usage text with no scratch path in it, so no scrubbing is needed."""

    def _live():
        proc = subprocess.run(
            [RUST, *argv, "--help"], capture_output=True, text=True, encoding="utf-8", timeout=20
        )
        assert proc.returncode == 0, proc.stderr
        return proc.stdout

    return oracle_fixtures.resolve(_live)


def _declared_flags() -> set[str]:
    app = typer.Typer(add_completion=False)
    app.command("run")(run_fn)
    # See test_run_command.py::_app for why a second command is needed here:
    # a single-command Typer app collapses into a bare CLI instead of a
    # subcommand group.
    app.command("_unused")(lambda: None)
    cmd = get_command(app).commands["run"]
    flags: set[str] = set()
    for param in cmd.params:
        flags.update(o for o in param.opts if o.startswith("--"))
    return flags


@LIVE_GATE
def test_declared_flags_all_exist_in_the_real_run_help():
    help_text = _rust_help("run")
    help_flags = set(re.findall(r"--[a-zA-Z][a-zA-Z0-9-]*", help_text))
    declared = _declared_flags()
    missing = declared - help_flags
    assert not missing, (
        f"run_cmd.run declares a flag the oracle's own `tan run --help` does not "
        f"list: {sorted(missing)}\n{help_text}"
    )


@LIVE_GATE
def test_run_help_is_not_the_same_flag_set_as_build_or_flash():
    """The real, shipped surfaces disagree by design -- `run` is a distinct
    `Commands` variant (`crates/tan-cli/src/cli.rs`), not an alias."""
    run_help = _rust_help("run")
    build_help = _rust_help("build")
    flash_help = _rust_help("flash")
    run_flags = set(re.findall(r"--[a-zA-Z][a-zA-Z0-9-]*", run_help))
    assert run_flags != set(re.findall(r"--[a-zA-Z][a-zA-Z0-9-]*", build_help))
    assert run_flags != set(re.findall(r"--[a-zA-Z][a-zA-Z0-9-]*", flash_help))
    assert "--flash" in run_flags and "--flash" not in set(
        re.findall(r"--[a-zA-Z][a-zA-Z0-9-]*", build_help)
    )


@LIVE_GATE
@pytest.mark.xfail(
    strict=True,
    reason="`run` not yet registered in tan.cli -- pending the app.command(\"run\") wiring",
)
@pytest.mark.parametrize(
    "case_id, extra",
    [
        ("no-sdk-found", []),
        ("sdk-root-invalid", ["--sdk-root", "./nowhere"]),
    ],
    ids=["no-sdk-found", "sdk-root-invalid"],
)
def test_run_matches_the_rust_oracle_on_the_build_failed_path(case_id, extra, tmp_path):
    work = tmp_path / "root"
    work.mkdir()
    result = compare(
        ["run", "--format", "json", *extra], cwd=work, surface=ENVELOPE, home=tmp_path
    )
    assert result.matches, f"{case_id}: " + "; ".join(result.diffs)
