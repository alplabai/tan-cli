# SPDX-License-Identifier: Apache-2.0
"""Diff the Python ``tan`` against the shipped Rust ``tan`` on identical inputs.
Any divergence is a port bug -- Rust is authoritative until a capability is
confirmed here, and only then is Rust retired for it.

This is the direct replacement for the ``fan_out`` oracle Phase 4 deleted, so it
has to be honest about two things:

**Scope.** Each case names the surface both binaries genuinely produce; see the
module docstring of ``oracle.py`` for why a naive whole-plan diff is red for a
reason that is not a port bug, and which side was declared correct.

**Coverage.** The port registers ``--version`` and ``build`` today. ``build``
is wired end to end (acquire the plan, substitute, materialise, execute), but
its plan-INSPECTION modes (``--plan``/``--materialise``/``--manifest``) are
not, and no other command exists yet. Cases naming any of those therefore
cannot run end to end. They are marked
``xfail(strict=True)`` and listed by name rather than skipped or softened,
following the precedent in ``tests/conformance/test_contract_envelopes.py``: a
case that starts genuinely passing then reports XPASS and FAILS the run, which
forces the one-line promotion instead of letting a landed command sit
mis-classified as "not ported" forever.
"""
import subprocess
import sys
from pathlib import Path

import pytest

from .oracle import ENVELOPE, PLAN, VERSION, compare, narrow_plan, rust_binary

RUST = rust_binary()

#: Every case: argv, the surface it is scoped to, and -- when the port cannot
#: satisfy it yet -- why. A ``None`` reason means the case runs for real.
CASES = [
    # The extension's acceptance probe. Compared by SHAPE: 0.5.0-dev vs
    # 0.4.1-dev is a deliberate, permanent difference (python/tan/version.py).
    (["--version"], VERSION, None),
    # A usage error must put its diagnosis on stderr and leave stdout EMPTY --
    # the extension parses stdout whole, so one stray byte breaks it. clap and
    # Typer agree here today; this case exists to keep them agreeing.
    (["bogus-command"], ENVELOPE, None),
    # Bare invocation. Promoted (tan.cli's root callback now rejects a
    # missing subcommand via ctx.fail, exit 2, stdout empty -- see
    # tests/test_cli_skeleton.py::test_bare_invocation_exits_2_with_help_on_stderr).
    ([], ENVELOPE, None),
    (["validate", "--format", "json"], ENVELOPE, "validate lands in a later sub-project"),
    # The first case that compares a whole SUCCESS envelope from a ported
    # command, not a usage error: `presets` with nothing resolvable exits 0 and
    # reports the frozen `presets.sdk-root-unresolved` warning plus the built-in
    # defaults. Deterministic on any host -- `work_dir`'s isolated parent and the
    # per-case `home` are exactly what stop a stray checkout resolving here, and
    # `project.root` is the same absolute cwd for both sides.
    (["presets", "--format", "json"], ENVELOPE, None),
    (
        ["build", "--plan", "--format", "json"],
        PLAN,
        # `tan build` itself IS ported now (the executing path: acquire the
        # plan, materialise, run each slice). What this case compares is
        # `--plan`, the SHOW-the-plan-and-stop mode, which is not -- so the
        # port answers a usage error where Rust answers a plan envelope. When
        # `--plan` lands, re-derive the PLAN surface on the tokened/untokened
        # axis first (see oracle.py's module docstring): the current narrowing
        # was chosen while nothing on the Python side emitted a plan at all.
        "`build --plan` (show the plan, build nothing) is not ported; the "
        "executing `tan build` is",
    ),
]


@pytest.fixture
def work_dir(tmp_path):
    """A scratch cwd nested under its OWN parent. ``discover_workspace_sdk``
    probes the cwd's PARENT for a sibling ``alp-sdk/``, so running directly in
    ``tmp_path`` would let another test's directory decide whether the oracle
    finds an SDK."""
    work = tmp_path / "root"
    work.mkdir()
    return work


@pytest.mark.skipif(not RUST, reason="no Rust tan; set TAN_RUST_BINARY or run `cargo build`")
@pytest.mark.parametrize(
    "argv,surface,pending",
    [
        pytest.param(
            argv,
            surface,
            pending,
            id=" ".join(argv) or "<no args>",
            marks=([pytest.mark.xfail(reason=pending, strict=True)] if pending else []),
        )
        for argv, surface, pending in CASES
    ],
)
def test_python_matches_rust(argv, surface, pending, work_dir, tmp_path):
    result = compare(argv, cwd=work_dir, surface=surface, home=tmp_path / "home")
    assert result.matches, "\n".join(result.diffs)


# --- the harness must be able to go red ------------------------------------
#
# A parity run that cannot fail is worse than no parity run: it reads as
# evidence. These plant a KNOWN divergence into the same code path the real
# cases use and assert the comparator reports it.


@pytest.mark.skipif(not RUST, reason="no Rust tan; set TAN_RUST_BINARY or run `cargo build`")
def test_harness_reports_a_planted_exit_code_difference(work_dir, tmp_path):
    stub = [sys.executable, "-c", "print('tan 0.5.0-dev'); raise SystemExit(3)"]
    result = compare(
        ["--version"], cwd=work_dir, surface=VERSION, home=tmp_path / "home", python=stub
    )
    assert not result.matches
    assert any("exit code" in d for d in result.diffs), result.diffs


@pytest.mark.skipif(not RUST, reason="no Rust tan; set TAN_RUST_BINARY or run `cargo build`")
@pytest.mark.parametrize(
    "printed",
    [
        # Shape-scoping must not degrade into "any stdout passes": a version
        # line that does not satisfy the extension's regex is still a failure.
        "print('tan v0.5-dev')",
        # ...and the shape must cover the WHOLE of stdout. A prefix-anchored
        # match let both of these through as parity, on the one case that
        # actually runs today. Rust prints exactly `tan 0.4.1-dev`.
        "print('tan 0.5.0-dev'); print('LEAKED EXTRA STDOUT LINE')",
        "print('tan 9.9.9 THIS IS NOT TAN AT ALL')",
    ],
    ids=["malformed", "trailing-line", "trailing-words"],
)
def test_harness_reports_a_planted_version_shape_difference(printed, work_dir, tmp_path):
    result = compare(
        ["--version"],
        cwd=work_dir,
        surface=VERSION,
        home=tmp_path / "home",
        python=[sys.executable, "-c", printed],
    )
    assert not result.matches
    assert any("is not exactly" in d for d in result.diffs), result.diffs


@pytest.mark.skipif(not RUST, reason="no Rust tan; set TAN_RUST_BINARY or run `cargo build`")
def test_harness_reports_a_planted_envelope_difference(work_dir, tmp_path):
    stub = [sys.executable, "-c", "print('{\"command\":\"cli\"}')"]
    result = compare(["bogus-command"], cwd=work_dir, home=tmp_path / "home", python=stub)
    assert not result.matches
    assert any(d.startswith("command:") for d in result.diffs), result.diffs


@pytest.mark.skipif(RUST is None, reason="needs a real path to exist for the negative case")
def test_a_named_but_missing_rust_binary_is_an_error_not_a_skip(monkeypatch):
    # A typo'd TAN_RUST_BINARY in CI must not yield an all-skip green run. It
    # must also not fall back to some other binary the operator did not name.
    monkeypatch.setenv("TAN_RUST_BINARY", str(Path("no") / "such" / "tan"))
    with pytest.raises(RuntimeError, match="does not exist"):
        rust_binary()


# --- the PLAN scope must be narrow, not blind -------------------------------
#
# No binary needed: `narrow_plan` is the whole scoping decision, and it is the
# one piece of this harness that will still be load-bearing when `build` lands.
# Its retained-key split is PROVISIONAL -- see oracle.py's module docstring; the
# Rust side emits every key here verbatim, so the split must be re-derived on
# the tokened/untokened axis when case 5 is promoted. These tests pin the
# narrowing's mechanics, not the correctness of the split.

# Shaped after the six REAL plans at `tests/parity/oracle/*.build-plan.json`
# (repo root, Rust workspace), NOT after the hand-authored `raw_json` in
# plan_modes.rs:404-442. That string exists only to prove pass-through for an
# arbitrary unmodeled key, and it puts `sdkVersion`/`sdkCommit` INSIDE a slice
# -- which no real plan does, and which neither `BuildSlice` nor Python's
# `Slice` models. Every real plan carries them at TOP level. Read this fixture
# as ground truth for the emit's shape; read that one for its one narrow claim.
RAW_SLICE = {
    "coreId": "m55_hp",
    "backend": "zephyr",
    "buildDir": "${PROJECT_ROOT}/build/m55_hp",
    "configArtefacts": [],
    "command": {"tool": "west", "args": ["build"], "cwd": "."},
    "env": {},
    "envAppendPath": {},
    # The four provisionally-excluded keys. Rust DOES emit these, verbatim from
    # the SDK; they are excluded pending the tokened/untokened re-derivation.
    "appDir": "${SDK_ROOT}/examples/blinky",
    "toolchain": {"name": "zephyr"},
    "artifacts": {"elf": "zephyr/zephyr.elf"},
    "debug": {"gdb": "arm-none-eabi-gdb"},
}

#: Top-level keys, values taken from the real fixtures.
RAW_TOP = {"schemaVersion": 1, "sdkVersion": "0.11.1", "sdkCommit": "97ad481b"}


def _envelope(slice_=None, **top):
    data = {**RAW_TOP, **top, "slices": [slice_ or RAW_SLICE]}
    return {"command": "build", "ok": True, "exitCode": 0, "data": data}


def test_plan_scope_drops_the_provisionally_excluded_keys():
    substituted = {
        **RAW_SLICE,
        "appDir": "/home/dev/alp-sdk/examples/blinky",
        "toolchain": {"name": "zephyr", "root": "/opt/zephyr-sdk"},
        "artifacts": {"elf": "/abs/zephyr.elf"},
        "debug": {"gdb": "/opt/gdb"},
    }
    assert narrow_plan(_envelope()) == narrow_plan(_envelope(substituted))


@pytest.mark.parametrize("key", ["buildDir", "coreId"])
def test_plan_scope_still_catches_a_retained_slice_key(key):
    assert narrow_plan(_envelope()) != narrow_plan(_envelope({**RAW_SLICE, key: "DRIFTED"}))


@pytest.mark.parametrize("key", ["sdkVersion", "sdkCommit"])
def test_plan_scope_still_catches_a_drifted_version_skew_field(key):
    # The version-skew guard's own fields, pinned where they actually live: top
    # level. Never path-bearing, never substituted -- so retaining them costs no
    # false red, and dropping them would be pure lost coverage.
    assert narrow_plan(_envelope()) != narrow_plan(_envelope(**{key: "DRIFTED"}))


def test_plan_scope_leaves_a_null_data_envelope_whole():
    # The no-SDK path emits `data: null` plus an issue; that envelope is
    # comparable in full and must not be silently narrowed away.
    envelope = {"command": "build", "exitCode": 1, "data": None, "issues": [{"code": "x"}]}
    assert narrow_plan(envelope) == envelope


def test_plan_scope_does_not_collapse_a_dict_that_is_not_a_plan():
    # Without the `slices` guard both of these narrow to {} and compare
    # VACUOUSLY EQUAL -- a comparator answering "identical" for two different
    # documents, which is the one thing this harness must never do.
    a = {"command": "build", "data": {"message": "plan A", "count": 1}}
    b = {"command": "build", "data": {"message": "plan B", "count": 2}}
    assert narrow_plan(a) != narrow_plan(b)


def test_rust_oracle_is_present_or_the_suite_says_so():
    # Reading a green parity run as evidence requires knowing the cases ran.
    if RUST is None:
        pytest.skip("no Rust tan; set TAN_RUST_BINARY or run `cargo build`")
    proc = subprocess.run([RUST, "--version"], capture_output=True, text=True, encoding="utf-8")
    assert proc.returncode == 0, f"{RUST} is not a working tan binary"
    print(f"\noracle: {RUST} -> {proc.stdout.strip()}")
