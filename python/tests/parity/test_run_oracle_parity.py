# SPDX-License-Identifier: Apache-2.0
"""`tan run` against the shipped Rust oracle.

**The option-set pin runs unconditionally** (skipped only when no oracle
binary is available): it proves every flag `run_cmd.run` declares genuinely
exists in the REAL `tan run --help` output, so this port can never invent a
flag the shipped binary does not have.

**The full-envelope cases below are live, pinned known divergences, not
`xfail`.** This file used to carry both as one `xfail(strict=True,
reason="run not yet registered in tan.cli")` block, on the premise that
`python -m tan run ...` still 404d as "no such command" -- stale the moment
`app.command("run")(run)` landed (`tan/cli.py:101`) and never promoted,
caught here only by actually invoking both binaries (tan-cli#257/#258: "do
not skip [running both binaries]... source-reading and doc comments are not
evidence"), not by trusting the comment. `run` genuinely IS registered and
produces a real envelope on both cases; the reason the comparison still
fails is two real, un-narrowed divergences, pinned individually below.
"""
import re
import subprocess

import pytest
import typer
from typer.main import get_command

from tan.commands.run_cmd import run as run_fn

from . import oracle_fixtures
from .oracle import _run, missing_for_live, python_command, rust_binary

RUST = rust_binary()
LIVE_GATE = pytest.mark.skipif(
    missing_for_live(RUST),
    reason="TAN_PARITY_LIVE=1 needs a Rust tan; set TAN_RUST_BINARY or run `cargo build`",
)
#: The two known-divergence cases below spawn the oracle unconditionally
#: whenever a binary is present (mirrors `test_oracle_parity.py`'s
#: `_ORACLE_REQUIRED`) rather than replaying a frozen fixture under
#: `LIVE_GATE`'s default -- a stale frozen answer is exactly what let the
#: old `xfail` reason above go unnoticed for as long as it did.
_ORACLE_REQUIRED = pytest.mark.skipif(
    RUST is None,
    reason="needs a built Rust tan; run `cargo build --bin tan` (or set TAN_RUST_BINARY)",
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


@_ORACLE_REQUIRED
def test_run_no_sdk_is_a_known_divergence_from_the_oracle(tmp_path):
    """Exit code, issue code and `data` all agree (`build.plan-unavailable`,
    exit 1, `data: null`); only the remedy wording differs. The same case as
    `test_run_no_sdk_is_a_known_divergence_from_the_oracle` in
    `test_oracle_parity.py`, re-pinned here too because THIS module -- not
    that one -- owns `run`'s own flag/wiring surface, and used to carry a
    stale `xfail` that hid this exact envelope behind a wrong reason."""
    home = tmp_path / "home"
    work = tmp_path / "root"
    work.mkdir()
    argv = ["run", "--format", "json"]
    r_code, r_out = _run([RUST], argv, work, home)
    p_code, p_out = _run(python_command(), argv, work, home)
    assert r_code == p_code == 1
    assert "sdk" not in r_out and "sdk" not in p_out
    assert [i["code"] for i in r_out["issues"]] == ["build.plan-unavailable"]
    assert [i["code"] for i in p_out["issues"]] == ["build.plan-unavailable"]
    assert r_out["issues"][0]["message"] == (
        "no alp-sdk checkout found — pass `--sdk-root <PATH>`, pin one "
        "with `tan sdk switch <version|path>`, set it in settings, or run "
        "`tan bootstrap`. The build-plan comes from the SDK's "
        "`alp_orchestrate --emit build-plan`."
    )
    assert p_out["issues"][0]["message"] == (
        "no alp-sdk checkout found -- pass `--sdk-root <PATH>` or run from a "
        "project beside one. Planning reads the SDK's `metadata/**`."
    )
    assert r_out["data"] is None
    assert p_out["data"] is None
    assert {**r_out, "issues": []} == {**p_out, "issues": []}


@_ORACLE_REQUIRED
def test_run_sdk_root_invalid_now_matches_the_oracle(tmp_path):
    """Was `test_run_sdk_root_invalid_is_a_known_divergence_from_the_oracle`,
    pinning a real defect; the defect is FIXED (tan-cli#257/#258) and this now
    pins the parity instead.

    The divergence: the oracle treats an unresolvable explicit `--sdk-root` as
    no root at all and refuses with `build.plan-unavailable` / "no alp-sdk
    checkout found". The port carried `nowhere` straight through as
    `sdk.sourceTier: "sdkRootFlag"` -- `resolve_sdk_root_ladder` is TERMINAL
    but UNVALIDATED for the flag tier, matching the oracle's own
    `resolve_sdk_tiered` ("terminal for REPORTING") -- and so fell through to
    the NEXT missing thing, reporting "no board.yaml found" plus an `sdk` key
    the oracle never emits here. It told the customer their project was broken
    when the flag they had just typed was what was wrong.

    Fixed in BOTH `build_cmd.build` and `run_cmd.run`: the guard sits at each
    flag's own entry point rather than inside the shared ladder, since every
    other caller depends on that staying unvalidated -- the same placement
    `clean_cmd.sdk_root_resolves` and `flash_cmd._resolve_sdk` already chose.
    `run`'s copy mattered on its own account: its resolution line was a
    VERBATIM COPY of `build`'s, so fixing only `build` would have left the
    twin live under `tan run`.

    The `message` wording still differs -- the oracle names three remedies
    where the port names two -- so that one field stays pinned literally on
    both sides rather than narrowed away, per this file's own rule against
    weakening a comparison to make it pass. Everything else must now AGREE."""
    home = tmp_path / "home"
    work = tmp_path / "root"
    work.mkdir()
    argv = ["run", "--format", "json", "--sdk-root", "./nowhere"]
    r_code, r_out = _run([RUST], argv, work, home)
    p_code, p_out = _run(python_command(), argv, work, home)
    assert r_code == p_code == 1
    assert [i["code"] for i in r_out["issues"]] == ["build.plan-unavailable"]
    assert [i["code"] for i in p_out["issues"]] == ["build.plan-unavailable"]
    # The heart of the fix: an unresolvable explicit root is no root, so
    # NEITHER side reports an `sdk` block. The port used to carry
    # `{"root": "nowhere", "sourceTier": "sdkRootFlag"}` here.
    assert "sdk" not in r_out
    assert p_out.get("sdk") is None
    assert r_out["issues"][0]["message"] == (
        "no alp-sdk checkout found — pass `--sdk-root <PATH>`, pin one "
        "with `tan sdk switch <version|path>`, set it in settings, or run "
        "`tan bootstrap`. The build-plan comes from the SDK's "
        "`alp_orchestrate --emit build-plan`."
    )
    # Same REFUSAL as the oracle now (no alp-sdk checkout), different wording.
    assert "no alp-sdk checkout found" in p_out["issues"][0]["message"]
    assert "no board.yaml found" not in p_out["issues"][0]["message"]
    assert r_out["data"] is None
    assert p_out["data"] is None
