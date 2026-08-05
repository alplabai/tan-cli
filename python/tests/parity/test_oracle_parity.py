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
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.conftest import sdk_root

from . import oracle_fixtures
from .oracle import (
    ENVELOPE,
    PLAN,
    REPO_ROOT,
    VERSION,
    _run,
    compare,
    empty_tool_inventory,
    missing_for_live,
    narrow_plan,
    normalise_path_separators,
    python_command,
    rust_binary,
    rust_run,
)

RUST = rust_binary()
LIVE_GATE = pytest.mark.skipif(
    missing_for_live(RUST),
    reason="TAN_PARITY_LIVE=1 needs a Rust tan; set TAN_RUST_BINARY or run `cargo build`",
)

#: A real, resolvable alp-sdk checkout for the `generate` case below -- set
#: once at import time, before `tests.conftest._scrub_sdk_discovery_env` (an
#: autouse fixture) deletes `ALP_SDK_ROOT` for every test function; see
#: `sdk_root`'s own docstring for why the read must happen here and not inside
#: a test body.
GENERATE_SDK = sdk_root()

#: Every case: argv, the surface it is scoped to, and -- when the port cannot
#: satisfy it yet -- why. A ``None`` reason means the case runs for real.
CASES = [
    # The extension's acceptance probe. Compared by SHAPE: the port's
    # 0.5.0-rc3 (python/tan/version.py) vs the oracle's 0.4.1 (Cargo.toml) is
    # a deliberate, permanent difference.
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
    # `debug-config`'s refusal envelope, which no conformance golden reaches:
    # all four are exit-0 previews. Pins exit 5, the `zephyr-mcu`/`none`
    # placeholder payload, `configuration: null`, the null project AND the
    # message string, across both implementations.
    (
        ["--format", "json", "debug-config", "--target-kind", "bogus"],
        ENVELOPE,
        None,
    ),
    # The first case that compares a whole SUCCESS envelope from a ported
    # command, not a usage error: `presets` with nothing resolvable exits 0 and
    # reports the frozen `presets.sdk-root-unresolved` warning plus the built-in
    # defaults. Deterministic on any host -- `work_dir`'s isolated parent and the
    # per-case `home` are exactly what stop a stray checkout resolving here, and
    # `project.root` is the same absolute cwd for both sides.
    (["presets", "--format", "json"], ENVELOPE, None),
    # `clean` in a scratch directory with no SDK anywhere: both sides refuse with
    # `clean.sdk-root-not-found` at exit 1, report an empty `data.buildRoot`, and
    # emit NO `sdk` key. Non-destructive on either side, which is what makes it
    # safe here -- `clean`'s real cases delete, so running both implementations in
    # one shared `work_dir` would leave the second nothing to do and "match"
    # vacuously. Those live in `test_clean_parity.py`, on mirrored trees.
    (["clean", "--format", "json"], ENVELOPE, None),
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


@LIVE_GATE
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


#: A post-build manifest with a Cortex-M Zephyr slice FIRST and a `native_sim`
#: slice SECOND -- the ordering that broke `native-host` resolution (#83), plus a
#: `runners.yaml` for the MCU slice so the J-Link `device` and the toolchain GDB
#: actually resolve. BOTH slices record `zephyr.elf`, because that is the only
#: thing tan ever writes (`resolve_zephyr_artefact` has no `.exe` branch and
#: alp-sdk never writes the field), which is what makes the sibling `.exe` swap
#: observable.
PARITY_MANIFEST = """\
schema_version: 1
hw_info:
  sku: E1M-AEN701
slices:
- core_id: m55_hp
  os: zephyr
  board: alp_e1m_aen701_m55_hp
  status: ok
  build_dir: {root}/build/m55_hp-zephyr/build
  output_artefact: {root}/build/m55_hp-zephyr/build/zephyr/zephyr.elf
- core_id: native_sim
  os: zephyr
  board: native_sim/native/64
  status: ok
  output_artefact: {root}/build/native_sim-zephyr/build/zephyr/zephyr.elf
ipc: []
helper_mcus: []
boot_order: []
"""

PARITY_RUNNERS = """\
runners:
- jlink
- openocd
config:
  gdb: /zephyr-sdk/arm-zephyr-eabi-gdb
  openocd: /usr/bin/openocd
  openocd_search:
  - /usr/share/openocd/scripts
args:
  jlink:
  - --device=AE822F4M55_HP
  openocd:
  - --config=board/alp.cfg
"""


#: The flag(s) tan-cli#454 made REQUIRED, with no tan-side default, on
#: `quality`/`migrate`'s own surface -- `lock`'s `alp-lock` has no required
#: flag at all, so it stays empty. See the argv comment inside
#: `test_west_forward_matches_rust` for why this must be supplied here.
_REQUIRED_MODE_ARGS: dict[str, list[str]] = {
    "quality": ["--profile", "quick"],
    "migrate": ["--check"],
    "lock": [],
}


@LIVE_GATE
@pytest.mark.parametrize("verb", ["migrate", "lock", "quality"])
def test_west_forward_matches_rust(verb, work_dir, tmp_path):
    """`west_forward_cmd.py`'s three verbs, run inside a real `.west` workspace
    so `data.westCwd` actually goes through the workspace-walk branch (not just
    the already-posix `--project` echo) -- the branch where a bare
    `str(PathLikeObject)` re-renders with the platform separator on Windows
    and breaks the envelope's platform-identical-path contract. The frozen
    fixture was captured on a host with no `west` on PATH at all, so the rust
    side's (frozen) answer is the "west not found on PATH" launch error;
    `python_env_overrides` pins the PYTHON side's PATH to match that same
    absence, rather than whatever this replay host happens to have installed
    -- on any host with a PATH-resolvable `west`, working or not, the python
    side would otherwise genuinely launch it and diverge on ITS output
    instead of reporting the same launch error (tan-cli#324; the identical class of bug
    `_pin_tool_inventory` fixed for the yocto_wic flash cases, tan-cli#313).
    That error envelope still carries `data.westCommand`/`westCwd`/`args`,
    which is exactly what a westCwd or args-capture regression would move.
    """
    (work_dir / ".west").mkdir()
    # `--format json` sits BEFORE the forwarded `--core`/`-b` flags on purpose:
    # the oracle's clap `WestForwardArgs` (`trailing_var_arg = true`) swallows
    # everything from the first unrecognised token onward, including a later
    # `--format` -- so `--format` after `--core` never reaches JSON mode on
    # the Rust side at all (see `test_json_mode_forwards_interspersed_
    # unrecognised_flags_verbatim` in test_west_forward_command.py for that
    # documented divergence). Ordered this way both sides land in JSON mode
    # and the envelope, including `data.westCwd`/`args`, is directly
    # comparable.
    #
    # `_REQUIRED_MODE_ARGS[verb]`: tan-cli#454 made `alp-quality --profile`
    # and `alp-migrate`'s one-of `--check`/`--preview`/`--apply` REQUIRED on
    # tan's own surface too -- omitting it now refuses BEFORE `west` is ever
    # spawned (`test_west_forward_command.py`'s own tan-cli#454 tests cover
    # that refusal), which would short-circuit this test before it ever
    # reaches the `westCwd`-computation branch it exists to exercise: the
    # refusal envelope's own `data` is `{"schemaVersion": "1"}` only, with no
    # `westCwd` at all, and its exit code (`VALIDATION_FAILURE`, 2) would
    # diverge from the frozen fixture's `1` -- a divergence on `exitCode`,
    # which line 284's waiver does NOT cover.
    #
    # This does NOT make the frozen fixture below argv-accurate, though: that
    # fixture was captured BEFORE tan-cli#454 and still records `args` with no
    # `--profile`/`--check` in it (`oracle_fixtures/test_oracle_parity.json`).
    # Supplying `_REQUIRED_MODE_ARGS[verb]` here only changes the PYTHON
    # side's computed `data.args`; the frozen rust side is unaffected (fixture
    # lookup is keyed by pytest node id, not argv -- see
    # `oracle_fixtures.resolve`), so the two sides now diff on `data.args`
    # too. That new mismatch is harmless only because line 284 already waives
    # ANY divergence inside `data` wholesale (tan-cli#395's `westExitCode`/
    # `stdout`/`stderr` were the reason it exists) -- it does not verify that
    # `data.args` itself matches. If `data` ever stops being waived outright,
    # this fixture must be re-captured against the current argv first, or this
    # case will fail for a stale-fixture reason that reads as a port bug.
    argv = [
        "--project",
        str(work_dir),
        verb,
        "--format",
        "json",
        *_REQUIRED_MODE_ARGS[verb],
        "--core",
        "m55_hp",
        "-b",
        "some_board",
    ]
    result = compare(
        argv,
        cwd=work_dir,
        home=tmp_path / "home",
        python_env_overrides={"PATH": empty_tool_inventory(tmp_path)},
    )
    # A BOUNDED divergence, introduced deliberately by tan-cli#395 and asserted
    # here rather than waived: the port's `data` carries three keys the oracle
    # does not -- `westExitCode`, `stdout`, `stderr`.
    #
    # The oracle spawns the `alp-*` child with `capture_output` and then reads
    # NEITHER stream on either branch, so `alp-quality`'s report,
    # `alp-migrate --preview`'s diff and `alp-lock`'s resolved manifest are all
    # unreachable through `--format json`, and every non-zero child exit
    # collapses to 1. A consumer whose only channel is JSON -- alp-sdk-vscode --
    # could obtain none of them, and on failure was told to "re-run without
    # --format json to see the log", i.e. to use a mode it does not have.
    # Reproducing that faithfully would be parity with a defect.
    #
    # Bounded the way `test_flash_oracle_parity.py`'s shape-error case bounds
    # its own: the diff may fail on `data` and on NOTHING else, so the exit
    # code, `issues`, `project` and `ok` stay under full byte-parity and a
    # divergence that wandered into any other field still fails here. The
    # per-key shape of the addition is pinned separately, on this port's own
    # terms, by `test_west_forward_command.py`.
    offending = {d.split(":", 1)[0] for d in result.diffs}
    assert offending <= {"data"}, f"{verb}: unexpected divergence: {result.diffs}"


@LIVE_GATE
@pytest.mark.parametrize(
    "target,server,expected_pre_launch_task",
    [
        # J-Link resolves `device` + `gdbPath`; OpenOCD resolves
        # `serverpath`/`searchDir`/`configFiles`; pyOCD resolves NOTHING (the
        # board registers no such runner) and must keep its placeholder AND gain
        # the "registers no runner" note; native-host must take the native_sim
        # slice's sibling `.exe`, not the first `os: zephyr` slice's ELF.
        #
        # `expected_pre_launch_task` is tan-cli#138's restored default, a
        # DELIBERATE, PERMANENT divergence from the frozen `crates/` oracle:
        # #138 predates the oracle's freeze and it never emits this key.
        # Measured live against `tan --format json debug-config ...` for every
        # combination below -- not inferred from source.
        ("zephyr-mcu", "jlink", "alp: build active target"),
        ("zephyr-mcu", "openocd", "alp: build active target"),
        ("zephyr-mcu", "pyocd", "alp: build active target"),
        ("native-host", "none", "alp: build native_sim target"),
    ],
)
def test_debug_config_resolution_matches_rust(target, server, expected_pre_launch_task, work_dir, tmp_path):
    """The `<resolved-...>` overlay read off this project's OWN build output
    (#66/#83), diffed against the oracle. `--preview` only: both sides run in
    the SAME cwd, so a write-mode case would have the second run merge into
    what the first one wrote.

    NOT a plain `compare()` (tan-cli#138 vs the frozen oracle): the restored
    `preLaunchTask` default is a permanent divergence `compare()`'s whole-key
    equality would flag as a false failure, so this does `compare()`'s own
    scrub/normalise recipe by hand, strips `preLaunchTask` from the python
    side after asserting its value, and diffs everything else."""
    root = str(work_dir).replace("\\", "/")
    build = work_dir / "build"
    build.mkdir()
    (build / "system-manifest.yaml").write_text(
        PARITY_MANIFEST.format(root=root), encoding="utf-8"
    )
    zephyr = work_dir / "build" / "m55_hp-zephyr" / "build" / "zephyr"
    zephyr.mkdir(parents=True)
    (zephyr / "runners.yaml").write_text(PARITY_RUNNERS, encoding="utf-8")

    argv = ["debug-config", "--target-kind", target, "--server", server, "--preview", "--format", "json"]
    home = tmp_path / "home"
    roots = (work_dir, home)
    r_code, r_out = rust_run(argv, work_dir, home, scrub_roots=roots)
    p_code, p_out = _run(python_command(), argv, work_dir, home)
    p_out = oracle_fixtures.scrub(p_out, *roots)
    r_out = normalise_path_separators(r_out)
    p_out = normalise_path_separators(p_out)
    r_out = oracle_fixtures.normalise_scrubbed_path_separators(r_out)
    p_out = oracle_fixtures.normalise_scrubbed_path_separators(p_out)

    assert r_code == p_code, (r_code, p_code, r_out, p_out)
    r_config = r_out.get("data", {}).get("configuration") or {}
    assert "preLaunchTask" not in r_config, r_config
    p_config = p_out.get("data", {}).get("configuration") or {}
    assert p_config.get("preLaunchTask") == expected_pre_launch_task, p_config

    p_out_stripped = json.loads(json.dumps(p_out))  # deep copy
    del p_out_stripped["data"]["configuration"]["preLaunchTask"]
    diffs = [
        f"{key}: rust={r_out.get(key)!r} python={p_out_stripped.get(key)!r}"
        for key in sorted(set(r_out) | set(p_out_stripped))
        if r_out.get(key) != p_out_stripped.get(key)
    ]
    assert not diffs, "\n".join(diffs)


@LIVE_GATE
def test_debug_config_native_host_preview_global_format_matches_rust(work_dir, tmp_path):
    """`--format` BEFORE the subcommand (`["--format", "json", "debug-config",
    "--target-kind", "native-host", "--preview"]`), which is how the four
    `debug-config` goldens invoke it (clap's `global = true`). Worth its own
    case: Click gives the group only what precedes the subcommand, so this
    position is a separate code path in the port and not in Rust. Used to be a
    plain `CASES` entry (whole-envelope `compare()`), but tan-cli#138's
    restored `preLaunchTask` default is a DELIBERATE, PERMANENT divergence
    from the frozen `crates/` oracle (which predates #138 and never emits the
    key) -- see `test_debug_config_resolution_matches_rust`'s own docstring
    for why this needs the manual `rust_run`/`_run` diff instead."""
    argv = ["--format", "json", "debug-config", "--target-kind", "native-host", "--preview"]
    home = tmp_path / "home"
    roots = (work_dir, home)
    r_code, r_out = rust_run(argv, work_dir, home, scrub_roots=roots)
    p_code, p_out = _run(python_command(), argv, work_dir, home)
    p_out = oracle_fixtures.scrub(p_out, *roots)
    r_out = normalise_path_separators(r_out)
    p_out = normalise_path_separators(p_out)
    r_out = oracle_fixtures.normalise_scrubbed_path_separators(r_out)
    p_out = oracle_fixtures.normalise_scrubbed_path_separators(p_out)

    assert r_code == p_code, (r_code, p_code, r_out, p_out)
    r_config = r_out.get("data", {}).get("configuration") or {}
    assert "preLaunchTask" not in r_config, r_config
    p_config = p_out.get("data", {}).get("configuration") or {}
    assert p_config.get("preLaunchTask") == "alp: build native_sim target", p_config

    p_out_stripped = json.loads(json.dumps(p_out))  # deep copy
    del p_out_stripped["data"]["configuration"]["preLaunchTask"]
    diffs = [
        f"{key}: rust={r_out.get(key)!r} python={p_out_stripped.get(key)!r}"
        for key in sorted(set(r_out) | set(p_out_stripped))
        if r_out.get(key) != p_out_stripped.get(key)
    ]
    assert not diffs, "\n".join(diffs)


@LIVE_GATE
@pytest.mark.skipif(
    GENERATE_SDK is None,
    reason="set ALP_SDK_ROOT/ALP_SDK_PARITY_ROOT to a real alp-sdk checkout",
)
def test_generate_matches_rust_with_a_resolvable_sdk(tmp_path):
    """`tan generate`'s success envelope, against a REAL alp-sdk checkout --
    the case this suite had ZERO of when the top-level `sdk` envelope key
    (`root` + `sourceTier`) silently dropped out of the port: no fixture, no
    compile error, and this suite green throughout, all at once (see the
    module docstring on why scope is everything here).

    Each side scaffolds its OWN workspace via its OWN `tan init` first --
    mirroring the exact repro (`tan init --template minimal-app` then
    `generate --format json --sdk-root <sdk>`) -- rather than sharing one, so a
    divergence in `init` itself could not silently feed `generate` two
    different trees and still "match".

    `data.engine` is the one key excluded from the diff: which engine
    (`in-process` vs `subprocess`) rendered each target is a PYTHON-ONLY
    concept -- Rust has no spawn-the-SDK escape hatch to report, so it never
    emits this key at all, on any input. Every other key, including `sdk`
    itself, is compared whole.

    `GENERATE_SDK` IS among the scrubbed roots (unlike the note this
    docstring used to carry): that reasoning held only while both sides
    spawned live in the same run, where `sdk.root` was necessarily the same
    literal string on both sides regardless of whether it was scrubbed. Once
    the rust side is a FROZEN fixture (tan-cli#272), it carries whatever path
    string the capture host's checkout happened to sit at -- and a replay
    host (CI, a different machine, even a second checkout of the same ref at
    a different path) resolves `GENERATE_SDK` to a different string, so an
    unscrubbed `sdk.root` would diff on every host but the one that captured
    it. Scrubbed here with the exact mechanism `work`/`home` already use
    (`oracle_fixtures.scrub`), position-keyed so a replay host's differently
    spelled but equivalent path still lands on the same placeholder token.
    """
    home = tmp_path / "home"

    def _run_side(name: str, work: Path, argv: list[str]) -> tuple[int, dict]:
        # Both sides scrubbed with the SAME root tuple, in the SAME order --
        # rust via `rust_run`'s own `scrub_roots` (applied at capture time for
        # a frozen fixture, or at call time when TAN_PARITY_LIVE=1), python
        # via an explicit `oracle_fixtures.scrub` call here. Before tan-cli#272
        # froze the rust side, the python side went through `compare()`, which
        # scrubs unconditionally -- this bespoke helper predates that and
        # never scrubbed the python side at all, comparing a scrubbed string
        # against an unscrubbed one for every field a scratch path could
        # appear in.
        roots = (work, home, GENERATE_SDK)
        if name == "rust":
            return rust_run(argv, work, home, scrub_roots=roots)
        code, out = _run(python_command(), argv, work, home)
        return code, oracle_fixtures.scrub(out, *roots)

    sides: dict[str, tuple[int, dict]] = {}
    for name in ("rust", "python"):
        work = tmp_path / name
        work.mkdir()
        init_code, init_out = _run_side(name, work, ["init", "--template", "minimal-app"])
        assert init_code == 0, f"{name} tan init failed: {init_out}"
        sides[name] = _run_side(
            name, work, ["generate", "--format", "json", "--sdk-root", str(GENERATE_SDK)]
        )

    (r_code, r_out), (p_code, p_out) = sides["rust"], sides["python"]
    diffs: list[str] = []
    if r_code != p_code:
        diffs.append(f"exit code: rust={r_code} python={p_code}")
    p_out = {**p_out, "data": {k: v for k, v in p_out.get("data", {}).items() if k != "engine"}}
    # `data.written` is in `oracle.PATH_KEYS`: the frozen rust side renders
    # it with THIS fixture's capture-host separators (`oracle_fixtures.
    # CAPTURE_PLATFORM`), and a replay on a different platform (`parity.yml`'s
    # python-tests job runs ubuntu/windows/macos) would otherwise diff two
    # platforms' own, both-correct renderings -- not a port defect.
    r_out = normalise_path_separators(r_out)
    p_out = normalise_path_separators(p_out)
    for key in sorted(set(r_out) | set(p_out)):
        if r_out.get(key) != p_out.get(key):
            diffs.append(f"{key}: rust={r_out.get(key)!r} python={p_out.get(key)!r}")
    assert not diffs, "\n".join(diffs)


@LIVE_GATE
def test_init_sdk_root_flag_pin_is_a_known_divergence_from_the_oracle(tmp_path):
    """tan-cli#263 review: `init --sdk-root <relative>` is a DELIBERATE,
    permanent divergence from the oracle, not an uncovered port bug -- proven
    here rather than left implicit by the fact that no other case in this
    file ever passes `--sdk-root` to `init` (`test_generate_matches_rust_with_
    a_resolvable_sdk` above scaffolds with a bare `tan init`, and only hands
    `--sdk-root` to the later `generate` call).

    The oracle's `resolve_sdk_root` (`crates/tan-cli/src/util.rs`) returns an
    explicit `--sdk-root` AS TYPED, and `init/from_example.rs::pin_resolved_
    sdk` writes that string verbatim into `.alp/sdk-path`: a relative flag
    survives into the PERSISTED pointer file un-anchored. Read back later by a
    different invocation (a different cwd -- typically `tan sdk current` run
    from inside the project `init` just created), that pointer silently
    resolves to the wrong directory or nowhere at all: the maintainer's exact
    repro. `crates/` is frozen (`docs/ROADMAP.md`'s standing rule -- "Never
    edit crates/ or contract/"), so the fix lands only on the Python side:
    `init_cmd._resolve_sdk_root` anchors the flag to an absolute path before
    either using or persisting it. `test_init_command.py`'s
    `test_a_relative_sdk_root_pin_survives_being_read_back_from_inside_the_
    project` pins the corrected (Python-only) behaviour end to end; this test
    is the other half -- proving the two implementations really do disagree on
    the identical input, following the exclude-and-pin convention
    `test_flash_oracle_parity.py` already uses for a case that would always
    read red.
    """
    home = tmp_path / "home"
    sides: dict[str, tuple[int, dict]] = {}
    pins: dict[str, str] = {}
    for name in ("rust", "python"):
        sdk_dir = tmp_path / f"{name}-sdk"
        (sdk_dir / "scripts").mkdir(parents=True)
        (sdk_dir / "scripts" / "alp_project.py").write_text("", encoding="utf-8")
        work = tmp_path / name
        work.mkdir()
        argv = [
            "init", "--template", "minimal-app", "--sdk-root", f"../{name}-sdk", "--format", "json"
        ]
        pointer = work / ".alp" / "sdk-path"
        if name == "rust":
            # The pointer FILE has to be part of what is frozen: in replay
            # mode nothing actually runs `init` against `work`, so a plain
            # disk read after the fact would always see "file absent" and
            # report the divergence backwards. No scrub roots either --
            # every assertion below reads a small literal exit code or the
            # pointer's own content, and the pointer's whole point (the
            # divergence under test) is that it is written un-anchored, so
            # it never contains `work`/`home` to scrub in the first place.
            def _live(argv=argv, work=work, home=home, pointer=pointer):
                code, out = _run([RUST], argv, work, home)
                pin = pointer.read_text(encoding="utf-8") if pointer.exists() else None
                return [code, out, pin]

            code, out, pin_text = oracle_fixtures.resolve(_live)
            sides[name] = (code, out)
        else:
            sides[name] = _run(python_command(), argv, work, home)
            pin_text = pointer.read_text(encoding="utf-8") if pointer.exists() else None
        pins[name] = json.loads(pin_text)["sdkPath"] if pin_text is not None else "<no pointer written>"

    (r_code, r_out), (p_code, p_out) = sides["rust"], sides["python"]
    assert r_code == 0, f"rust tan init failed: {r_out}"
    assert p_code == 0, f"python tan init failed: {p_out}"

    # The divergence itself: the oracle keeps the flag verbatim; the port
    # anchors it. If this ever starts matching, `init`'s own docstring and
    # `test_init_command.py`'s pin need re-deriving, not just this assertion.
    assert pins["rust"] == "../rust-sdk", pins
    assert pins["python"] == (tmp_path / "python-sdk").as_posix(), pins
    assert pins["rust"] != pins["python"]


# --- tan-cli#272: cases the suite had none of, captured before the freeze --
#
# `python/tests/parity/`'s own docstring on `run_oracle_parity.py`'s style:
# each gap tan-cli#272 named is its own case, driven directly against the
# oracle rather than inferred from `crates/` or a docstring.

#: The six REAL, already-committed plans at `tests/parity/oracle/` (repo
#: root, the Rust-workspace parity tree -- see `oracle.py`'s own docstring on
#: why that is not this directory). All six are UNTOKENED (no `planPathMode`),
#: which is exactly the case `oracle.py`'s module docstring says needs no PLAN
#: narrowing at all: verified by hand before writing this as a whole-envelope
#: `ENVELOPE` assertion, not inferred from that docstring.
REAL_PLAN_FIXTURES = sorted((REPO_ROOT / "tests" / "parity" / "oracle").glob("*.build-plan.json"))


def _embedded_sdk_root(plan_path: Path) -> str | None:
    """The alp-sdk checkout path baked into a committed plan fixture's own
    ``env.ALP_SDK_ROOT`` (every slice of every one of the six fixtures carries
    the same literal value -- whichever checkout the fixture was captured
    against), or ``None`` if a fixture ever lacks it.

    This is a THIRD root neither ``cwd`` nor ``home`` cover: the fixture file
    is copied verbatim into the scratch dir and relayed unsubstituted by a
    bare ``--plan-from`` (`generate_cmd.py`'s module docstring on why Rust's
    ``--plan`` substitutes nothing), so whatever machine captured
    `tests/parity/oracle/*.build-plan.json` leaks straight through unless this
    is ALSO scrubbed. Discovered the hard way: an unscrubbed capture of these
    two tests put a real developer's checkout path into this file's own
    committed JSON.
    """
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    for slice_ in plan.get("slices", []):
        root = (slice_.get("env") or {}).get("ALP_SDK_ROOT")
        if root:
            return root
    return None


@LIVE_GATE
@pytest.mark.skipif(not REAL_PLAN_FIXTURES, reason="no committed build-plan fixtures found")
@pytest.mark.parametrize("plan_path", REAL_PLAN_FIXTURES, ids=lambda p: p.stem)
def test_plan_from_shows_the_plan_and_writes_nothing(plan_path, work_dir, tmp_path):
    """`build --plan-from <file>` with no `--materialise` is a pure SHOW: the
    SDK is never invoked (unlike bare `--plan`, still xfail above), so it IS
    ported, and it writes nothing to disk either side."""
    shutil.copy(plan_path, work_dir / "plan.json")
    extra = _embedded_sdk_root(plan_path)
    result = compare(
        ["build", "--plan-from", "plan.json", "--format", "json"],
        cwd=work_dir,
        surface=ENVELOPE,
        home=tmp_path / "home",
        extra_scrub_roots=(extra,) if extra else (),
    )
    assert result.matches, "\n".join(result.diffs)
    assert not (work_dir / "build").exists(), "a bare --plan-from must write nothing"


@LIVE_GATE
@pytest.mark.skipif(not REAL_PLAN_FIXTURES, reason="no committed build-plan fixtures found")
def test_plan_from_with_materialise_writes_every_artefact(work_dir, tmp_path):
    """...and `--materialise` writes every shared + per-slice artefact the
    plan names -- measured (tan-cli#272) at 5 files for this fixture (3
    shared + 1 per slice x 2 slices), matching `build_cmd.py`'s own
    `--plan-from ... --materialise -> six files` measurement on the AEN
    fixture qualitatively (a different plan, a different artefact count)."""
    plan_path = REPO_ROOT / "tests" / "parity" / "oracle" / "multicore_rpmsg-v2n.build-plan.json"
    shutil.copy(plan_path, work_dir / "plan.json")
    extra = _embedded_sdk_root(plan_path)
    result = compare(
        ["build", "--plan-from", "plan.json", "--materialise", "--format", "json"],
        cwd=work_dir,
        surface=ENVELOPE,
        home=tmp_path / "home",
        extra_scrub_roots=(extra,) if extra else (),
    )
    assert result.matches, "\n".join(result.diffs)
    written = sorted(p.relative_to(work_dir).as_posix() for p in (work_dir / "build").rglob("*") if p.is_file())
    assert written == [
        "build/a55_cluster-yocto/local.conf",
        "build/generated/alp/system_ipc.h",
        "build/generated/dts-partitions.dtsi",
        "build/generated/dts-reservations.dtsi",
        "build/m33_sm-zephyr/alp.conf",
    ], written


@LIVE_GATE
def test_validate_board_yaml_missing_guard_matches_the_oracle_at_exit_2(work_dir, tmp_path):
    """The empty-project pre-spawn guard, captured directly -- not inferred
    from `validate_cmd.py`'s own docstring (which names this exact scenario
    and says, in its own words, "re-measure before changing any of this; run
    the binary"). Scoped to exit code + issue code, not the whole envelope:
    `project.root`/`data.boardYamlPath` are `"."`/`"./board.yaml"` on the
    port by deliberate design (`_resolve_board_path`'s docstring cites the
    committed conformance fixtures for that spelling) versus an absolute path
    on the oracle -- an already-decided, unrelated divergence this case must
    not paper over by asserting more than tan-cli#272 measured.

    `scrub_roots=(work_dir, home)`, not `()`: the assertions below only ever
    read the issue CODE, but the frozen fixture still stores the oracle's
    WHOLE envelope regardless of what this test looks at, and the oracle's
    absolute-path `project.root`/`data.boardYamlPath` (the very divergence
    named above) land straight into the committed JSON unscrubbed otherwise --
    which is exactly how a real capture-host path reached this file. Scrubbing
    costs nothing here: the assertions never inspect those fields either way.
    """
    home = tmp_path / "home"
    argv = ["validate", "--format", "json"]
    r_code, r_out = rust_run(argv, work_dir, home, scrub_roots=(work_dir, home))
    p_code, p_out = _run(python_command(), argv, work_dir, home)
    assert r_code == p_code == 2
    assert [i["code"] for i in r_out["issues"]] == ["validate.board-yaml-missing"]
    assert [i["code"] for i in p_out["issues"]] == ["validate.board-yaml-missing"]


@LIVE_GATE
def test_validate_no_sdk_guard_matches_the_oracle_at_exit_2(work_dir, tmp_path):
    """`board.yaml` present, no SDK resolvable: the oracle's pre-spawn guard
    answers exit 2 `validate.sdk-root-unresolved`. This used to be pinned as a
    KNOWN divergence -- the port answered exit 2 `validate.spawn-not-
    implemented` while the full spawn path was unported (tan-cli#262) -- but
    tan-cli#376 landed the real spawn path, which reaches this SAME guard
    (`resolve_sdk_root_ladder`) instead of the placeholder refusal that used
    to sit in front of it: see `validate_cmd.py`'s own module docstring,
    "`validate.spawn-not-implemented` is GONE as of #376". Both sides now
    agree on issue code too, so this asserts the converged behaviour instead
    of the stale gap (the frozen fixture backing this case was renamed with
    it, 2026-08-03 -- see `oracle_fixtures/PROVENANCE.txt`).

    `scrub_roots=(work_dir, home)`: see the sibling `test_validate_board_
    yaml_missing_guard_matches_the_oracle_at_exit_2` above for why an empty
    tuple here still leaks -- this case's own `boardYamlPath`/`project.root`
    carry the same absolute `work_dir` the oracle reports its guard against.
    """
    home = tmp_path / "home"
    (work_dir / "board.yaml").write_text("schema_version: 1\n", encoding="utf-8")
    argv = ["validate", "--format", "json"]
    r_code, r_out = rust_run(argv, work_dir, home, scrub_roots=(work_dir, home))
    p_code, p_out = _run(python_command(), argv, work_dir, home)
    assert (r_code, [i["code"] for i in r_out["issues"]]) == (2, ["validate.sdk-root-unresolved"])
    assert (p_code, [i["code"] for i in p_out["issues"]]) == (2, ["validate.sdk-root-unresolved"])


@LIVE_GATE
def test_sdk_switch_unresolvable_version_is_a_known_divergence_from_the_oracle(work_dir, tmp_path):
    """`sdk switch <version-nobody-installed>`: the oracle resolves the
    version to a cache path that does not exist and refuses with exit 1
    `sdk.path-not-found`. `sdk switch`/`install` are not ported at all yet
    (`sdk_cmd.py`: "sdk.not-ported (exit 5) rather than half-working" --
    `switch` in particular must not write a pointer file `west` would then
    resolve differently than what tan just reported) -- the port answers
    `sdk.not-ported`. Both happen to exit 1, so only the issue code is the
    real divergence; pinned rather than silently narrowed to "exit code
    only", which would hide that coincidence going away.

    `scrub_roots=(work_dir, home)`: the refusal MESSAGE (not just the code
    the assertions below actually check) embeds the resolved-but-missing
    cache path under `home/.alp/sdk-cache/...` -- an unscrubbed capture put
    the capture host's own `home` straight into this committed file.
    """
    home = tmp_path / "home"
    argv = ["sdk", "switch", "9.9.9-does-not-exist", "--format", "json"]
    r_code, r_out = rust_run(argv, work_dir, home, scrub_roots=(work_dir, home))
    p_code, p_out = _run(python_command(), argv, work_dir, home)
    assert (r_code, [i["code"] for i in r_out["issues"]]) == (1, ["sdk.path-not-found"])
    assert (p_code, [i["code"] for i in p_out["issues"]]) == (1, ["sdk.not-ported"])


# --- the named command-surface parity ---------------------------------------
#
# The milestone goal states, verbatim: "Full command-surface parity
# with the v0.4.1 oracle: model, new-som, monitor, faultdecode, the
# introspection set, renode, and the seven entirely-unported verbs." Nothing
# above this point in the file ever runs any of those verbs -- this section is
# what actually reads that claim, one case per verb, against a REAL run of the
# oracle (never inferred from `crates/` or a docstring).
#
# These do NOT go through `compare()`/`rust_run()`: both replay a COMMITTED
# fixture by default (`oracle_fixtures.resolve`), and adding a fixture entry
# for a brand-new case is a separate, deliberate act with its own capture
# recipe (`oracle_fixtures/PROVENANCE.txt`) -- out of scope for this change.
# Instead these spawn `RUST` directly, every run, skipped only when no oracle
# binary is built (`_ORACLE_REQUIRED`, keyed on BINARY PRESENCE, not
# `TAN_PARITY_LIVE` like `LIVE_GATE` above): reusing `LIVE_GATE` here would NOT
# skip when `RUST is None`, since `missing_for_live` only ever fires under
# `TAN_PARITY_LIVE=1`, and would instead crash inside `subprocess.run([None,
# ...])`. `ci.yml`'s `python` job never runs `cargo build`, so `RUST is None`
# there and these cleanly skip; `parity.yml`'s `python-tests` job DOES
# (`cargo build --locked --bin tan`), so there -- and on any host with
# `target/{release,debug}/tan` already built, this one included -- these
# genuinely spawn both binaries fresh, in the SAME `work_dir`/`home`, and diff
# them for real. Because both sides share that one scratch `work_dir`, an
# embedded absolute path is already byte-comparable with no
# `oracle_fixtures.scrub` needed, unlike the frozen-replay cases above (whose
# fixture was captured from a DIFFERENT scratch dir than any replay).
#
# `_ORACLE_REQUIRED` skips on binary ABSENCE only -- never on a resolved binary
# being the WRONG one. `rust_binary()` picks the MOST RECENTLY BUILT of
# target/{release,debug}/tan (its own docstring), so a resolved-but-wrong
# binary today means either an inverted or TIED mtime between the two
# profiles (a tie is refused outright inside `rust_binary()` itself -- see
# that function) or an explicit TAN_RUST_BINARY naming the wrong one. This
# comment used to describe an OLDER rule -- a fixed release-over-debug
# preference -- and the failure that rule caused: measured on a real host, a
# stale `target/release/tan` (`tan 0.1.1`, weeks old) sat next to a fresh
# `target/debug/tan` (`tan 0.4.1`), `RUST` silently bound to the stale one
# because release was tried unconditionally regardless of either file's age,
# and every case below -- which, unlike the `LIVE_GATE` cases above, has no
# frozen fixture to fall back to -- measured itself against a binary that
# predates half the commands it runs: 7 of these failed, with no signal that
# the oracle, not the port, was wrong. Turning that into a SKIP (e.g.
# would reopen exactly the hole `missing_for_live`'s own docstring refuses --
# "a quiet skip here would hide exactly the gap that function exists to
# surface" (tan-cli#272) -- so `pinned_oracle`, a session-scoped, autouse
# fixture in `conftest.py` that every module under `tests/parity/` inherits
# (not just this section), FAILS the run instead, loudly, naming the mismatch.
#
# tan-cli#409 removed the `_ORACLE_REQUIRED` decorator this section used to
# carry, and with it the last of this module's binary-PRESENCE skips. It
# composed one `skipif(RUST is None, ...)`, which is a hole rather than a
# gate: the seven cases wearing it had no frozen fixture to fall back on, so
# the day tan-cli#269 deletes `crates/` every one would have turned into a
# passing skip with the run still exiting 0 -- the precise outcome the freeze
# exists to prevent. They now go through `_both_sides` (or, where the
# assertions read no path at all, `rust_run(..., scrub_roots=())`, the
# spelling `test_new_som_matches_the_oracle_on_command_and_issue_code`
# already uses). `tests/parity/test_parity_freeze_completeness.py` is what
# stops the decorator's shape being reintroduced by hand.


def _both_sides(argv, work_dir, home):
    """`(r_code, r_out, p_code, p_out)` for a pinned-DIVERGENCE case whose
    assertions read a PATH -- with the oracle side frozen (tan-cli#409).

    Three normalisations, each load-bearing:

    * `rust_run` returns its side already scrubbed of `work_dir`/`home`, so
      the PORT side needs the identical substitution or `project.root` diffs
      a live scratch path against the placeholder the fixture recorded.
    * `force=True` on the separator normalisation, because these keys were
      captured on **darwin**, not on `oracle_fixtures.CAPTURE_PLATFORM`
      (`win32`). The automatic rule reads the store as single-platform and
      disables itself on win32 -- right for the win32-captured majority and
      exactly backwards here, where the fixture records `/` and a Windows
      replay's live port answers a backslash. See `PROVENANCE.txt`.
    * Applied to BOTH sides: idempotent on a path that already uses `/`, and
      doing only one leaves a win32 replay diffing a normalised string
      against an un-normalised one.
    """
    r_code, r_out = rust_run(argv, work_dir, home)
    p_code, p_out = _run(python_command(), argv, work_dir, home)
    p_out = oracle_fixtures.scrub(p_out, work_dir, home)
    normalise = oracle_fixtures.normalise_scrubbed_path_separators
    return r_code, normalise(r_out, force=True), p_code, normalise(p_out, force=True)


@pytest.mark.parametrize(
    "argv,exit_code",
    [
        (["explain", "--format", "json"], 0),
        (["explain", "--template", "bogus-template", "--format", "json"], 1),
        (["explain", "--target", "bogus-target", "--format", "json"], 1),
    ],
    ids=["overview", "unknown-template", "unknown-target"],
)
def test_explain_matches_the_oracle(argv, exit_code, work_dir, tmp_path):
    """tan-cli#257 (the introspection set). `explain` reads no board.yaml and
    no alp-sdk checkout at all -- it is a static topic index over the
    template/target catalogues baked into both binaries -- and its envelope
    is byte-identical on every invocation measured here: the overview, an
    unknown ``--template``, and an unknown ``--target``.

    ``exit_code`` is PINNED per case (0 for the overview, 1 for each
    unknown-topic refusal), measured directly rather than left as a bare
    ``r_code == p_code``: that comparison, plus ``oracle._run``'s own
    degrade-on-unparseable-stdout fallback (``{'__raw__': ...}``), lets two
    binaries that both wrote NOTHING to stdout (say, both crashing before
    printing) compare equal at exit ``0 == 0`` having measured nothing at
    all. The explicit non-empty, non-``__raw__`` envelope check below closes
    that the rest of the way."""
    home = tmp_path / "home"
    r_code, r_out, p_code, p_out = _both_sides(argv, work_dir, home)
    assert r_code == p_code == exit_code
    assert r_out and "__raw__" not in r_out, r_out
    assert p_out and "__raw__" not in p_out, p_out
    assert r_out == p_out


# No `image`-missing-manifest case here, unlike its introspection-set siblings
# above and below: `test_image_missing_manifest` in `test_image_size_oracle.py`
# already covers this exact surface (exit 1, byte-identical envelope,
# including the message's embedded absolute path) and does so with NO
# divergence to pin -- `image`'s refusal message carries no OS-error tail to
# normalise or narrow, unlike `size` just below. A case living here would
# duplicate that assertion verbatim while adding nothing (measured: the two
# read byte-for-byte identical envelopes on this oracle), so it was dropped
# rather than kept as a second copy of the same check.
#
# Honestly, the drop gives up two things `size`'s own case below keeps, and
# both are acceptable for the identical reason -- no divergence exists for
# `image` to hide from either axis:
#
# * LIVE-SPAWN coverage. `test_image_missing_manifest` runs through
#   `assert_parity` -> `_rust_run` -> `oracle_fixtures.resolve`, which REPLAYS
#   a frozen fixture unless `TAN_PARITY_LIVE=1` is set. `size`'s case here
#   uses `@_ORACLE_REQUIRED`, which spawns the oracle live unconditionally
#   whenever a binary is present. Dropping `image` here means it is never
#   exercised by THIS file's unconditional-live mode, only by a frozen replay
#   or an opt-in live run elsewhere.
# * DEFAULT-BUILD-ROOT coverage. `test_image_missing_manifest` always passes
#   an explicit `--build-root br`; `size`'s case here passes no
#   `--build-root` at all, resolving the DEFAULT `work_dir/build/system-
#   manifest.yaml`. `image`'s missing-manifest path is never measured against
#   the default build root anywhere in this repo.
#
# Both gaps are safe to leave open because they are gaps in HOW the answer is
# produced, not in WHAT could go wrong: `image`'s refusal message is a fixed
# string plus an embedded path with no OS-error tail, so it cannot drift
# between a frozen fixture and a live run, or between an explicit and a
# default build root, the way `size`'s OS-`errno` rendering can. A live,
# default-build-root `image` case would measure the identical envelope this
# file already confirmed byte-identical under `--build-root br`, adding
# coverage of the harness's own plumbing, not of `image` itself.

def test_renode_no_sdk_matches_the_oracle(work_dir, tmp_path):
    """tan-cli#77. ``renode`` with no alp-sdk resolvable and no manifest is
    byte-identical -- the whole ``data`` placeholder shape (empty sku/repl/
    resc/elf, the derived ``logPath``) included."""
    home = tmp_path / "home"
    argv = ["renode", "--format", "json"]
    # `data.logPath` is `<work_dir>/build/renode.log` in the HOST's native
    # style (this command deliberately does not POSIX-normalise -- see
    # `renode_cmd`'s module docstring), so both the scrub and the forced
    # separator normalisation are what let one darwin capture replay against
    # a Windows run of the port.
    r_code, r_out, p_code, p_out = _both_sides(argv, work_dir, home)
    assert r_code == p_code == 1
    assert r_out == p_out


def test_size_missing_manifest_is_a_known_divergence_from_the_oracle(work_dir, tmp_path):
    """tan-cli#257 (the introspection set). Exit code and issue CODE match;
    the message's trailing OS-error text does not, and permanently cannot --
    it is Rust's ``io::Error`` Display ("No such file or directory (os error
    2)") against Python's ``OSError`` str ("[Errno 2] No such file or
    directory: '<path>'"), two runtimes rendering the identical ``ENOENT``.
    Pinned literally on BOTH the matching prefix and the diverging tail, per
    this file's own rule against narrowing a comparison down to "exit code
    only" to make it pass -- a change to either rendering, or the two
    converging, must fail this test rather than pass it silently.

    Overlaps `test_size_missing_manifest` in `test_image_size_oracle.py` on
    the SAME setup (an empty ``build/system-manifest.yaml``-less project) but
    NOT on what it asserts: that test's `_normalise` collapses this exact
    OS-error tail into a placeholder (``run \\`tan build\\` first
    (<OS-ERROR>).``) before comparing, deliberately treating the wording as
    immaterial -- this test asserts the opposite, pinning the literal,
    un-normalised text on BOTH sides as the divergence itself. It is also,
    unlike that one, an unconditional LIVE spawn (`_ORACLE_REQUIRED` is keyed
    on binary presence, not `TAN_PARITY_LIVE`; see the module comment above
    the v0.6.0 section), where the counterpart replays a committed fixture by
    default and only spawns the oracle under `TAN_PARITY_LIVE=1`."""
    home = tmp_path / "home"
    argv = ["size", "--format", "json"]
    r_code, r_out, p_code, p_out = _both_sides(argv, work_dir, home)
    assert r_code == p_code == 1
    assert [i["code"] for i in r_out["issues"]] == ["size.manifest-unavailable"]
    assert [i["code"] for i in p_out["issues"]] == ["size.manifest-unavailable"]
    # Both sides render this path the same way, and it is NOT `str(Path)`:
    # the project root arrives as the POSIX-ish string the caller passed and
    # is kept verbatim, then the `build/system-manifest.yaml` tail is joined
    # with the platform separator -- so on Windows the real message carries
    # `C:/.../root\build\system-manifest.yaml`, mixed on purpose. Rebuilding
    # it as `str(work_dir / ...)` gives an all-backslash path that NEITHER
    # binary emits: a defect in the expectation, not in either side. The two
    # agree with each other here, which is the thing this test measures.
    manifest_path = os.path.join(work_dir.as_posix(), "build", "system-manifest.yaml")
    prefix = f"no system-manifest.yaml at {manifest_path}; run `tan build` first ("
    r_message = r_out["issues"][0]["message"]
    p_message = p_out["issues"][0]["message"]
    # Both sides came back through `_both_sides`, so `work_dir` is redacted to
    # `<ORACLE-ROOT-0>` and the tail's separators are forced to `/`. The
    # EXPECTATION has to take the identical round trip or it diffs a live
    # scratch path against the placeholder the fixture recorded -- and, on
    # Windows, an `os.path.join` backslash against the normalised form
    # (tan-cli#409).
    _redact = oracle_fixtures.normalise_scrubbed_path_separators
    prefix = _redact(oracle_fixtures.scrub(prefix, work_dir, home), force=True)
    quoted_manifest = _redact(
        oracle_fixtures.scrub(repr(manifest_path), work_dir, home), force=True
    )
    # The Rust tail is PLATFORM-dependent, and pinning only the POSIX
    # rendering made this a Linux-only pass -- it reddens on Windows against
    # a completely healthy tree. The missing component here is the `build`
    # DIRECTORY, not merely the leaf file, and Windows distinguishes those
    # two: it returns ERROR_PATH_NOT_FOUND (3), "The system cannot find the
    # path specified.", where POSIX reports plain ENOENT (2) for both cases.
    # Measured on this host against the shipped oracle, not inferred.
    #
    # Python's `OSError` draws no such distinction on either platform -- it
    # says `[Errno 2] No such file or directory` for both -- and that is
    # itself part of the divergence this test exists to pin, so the Python
    # side stays one literal. Both tails are still pinned exactly; this
    # widens the expectation by PLATFORM, never to "exit code only".
    #
    # tan-cli#409: the condition is now `os.name == "nt" AND live`, not
    # `os.name == "nt"` alone. The Rust side is a FROZEN fixture by default,
    # captured on darwin, so a Windows replay reads back the POSIX tail the
    # capture host recorded -- the platform that decides this string is the
    # CAPTURE host, not the replay host. Keying on `os.name` alone would
    # therefore redden every Windows run against a perfectly good fixture.
    # Under `TAN_PARITY_LIVE=1` the oracle really is spawned and the replay
    # host IS the deciding one, which is what the `LIVE` term restores.
    rust_tail = (
        "The system cannot find the path specified. (os error 3))."
        if os.name == "nt" and oracle_fixtures.LIVE
        else "No such file or directory (os error 2))."
    )
    assert r_message == prefix + rust_tail
    # `!r`, not `'{...}'`: `OSError.__str__` interpolates the filename with
    # `%r`, so on Windows every separator in it comes back DOUBLED
    # (`...\\build\\system-manifest.yaml`). Hand-quoting reproduced the POSIX
    # rendering only. `!r` is what the runtime itself does, so it is right on
    # both platforms and cannot drift from it.
    assert p_message == prefix + f"[Errno 2] No such file or directory: {quoted_manifest})."
    # Everything OUTSIDE the message -- exit code, `data`, the issue code --
    # is a real match, not just coincidentally unchecked here.
    assert {**r_out, "issues": []} == {**p_out, "issues": []}


def test_run_no_sdk_is_a_known_divergence_from_the_oracle(work_dir, tmp_path):
    """tan-cli#257 (the introspection set). Exit code and issue CODE match
    (``build.plan-unavailable``, 1); the message's wording does not -- the
    oracle names three remedies (``--sdk-root``, ``tan sdk switch``, ``tan
    bootstrap``) where the port names one (``--sdk-root`` or a sibling
    checkout), and neither is a substring of the other. Pinned literally, not
    narrowed to the codes alone.

    Everything OUTSIDE the message -- exit code, ``data``, the issue code --
    is a real match too, not just coincidentally unchecked here: mirrors the
    whole-envelope-minus-message bar
    ``test_size_missing_manifest_is_a_known_divergence_from_the_oracle`` sets
    one function above, measured true for ``run`` the same way."""
    home = tmp_path / "home"
    argv = ["run", "--format", "json"]
    r_code, r_out, p_code, p_out = _both_sides(argv, work_dir, home)
    assert r_code == p_code == 1
    assert [i["code"] for i in r_out["issues"]] == ["build.plan-unavailable"]
    assert [i["code"] for i in p_out["issues"]] == ["build.plan-unavailable"]
    r_message = r_out["issues"][0]["message"]
    p_message = p_out["issues"][0]["message"]
    assert r_message == (
        "no alp-sdk checkout found — pass `--sdk-root <PATH>`, pin one "
        "with `tan sdk switch <version|path>`, set it in settings, or run "
        "`tan bootstrap`. The build-plan comes from the SDK's "
        "`alp_orchestrate --emit build-plan`."
    )
    assert p_message == (
        "no alp-sdk checkout found -- pass `--sdk-root <PATH>` or run from a "
        "project beside one. Planning reads the SDK's `metadata/**`."
    )
    assert r_out["data"] is None
    assert p_out["data"] is None
    assert {**r_out, "issues": []} == {**p_out, "issues": []}


def test_model_bare_invocation_is_a_known_divergence_from_the_oracle(work_dir, tmp_path):
    """tan-cli#253. The oracle's ``model`` is a generic ARGS-forwarding
    wrapper (``[ARGS]...`` in its own ``--help``, shared machinery with
    ``new-som``/``monitor``/``faultdecode``) that resolves an alp-sdk
    checkout before doing anything else and refuses
    ``model.failed``/"alp-sdk root is unresolved", exit 2, when none is
    found. The port re-implements ``model`` natively with its own ``build``
    subcommand (``Usage: tan model [OPTIONS] [SUBCOMMAND]``) and never
    touches an SDK at this step, refusing instead with
    ``model.unknown-subcommand``, exit 1, when no subcommand is named.
    Neither the exit code nor the issue code agree -- both pinned, not
    narrowed to the one thing they share (a ``command: "model"`` JSON
    envelope shape)."""
    home = tmp_path / "home"
    argv = ["model", "--format", "json"]
    # `scrub_roots=()`: every assertion below reads a `command` string, an
    # exit code or an issue code, none of which can contain a scratch path.
    r_code, r_out = rust_run(argv, work_dir, home, scrub_roots=())
    p_code, p_out = _run(python_command(), argv, work_dir, home)
    assert r_out["command"] == p_out["command"] == "model"
    assert (r_code, [i["code"] for i in r_out["issues"]]) == (2, ["model.failed"])
    assert (p_code, [i["code"] for i in p_out["issues"]]) == (1, ["model.unknown-subcommand"])


def test_new_som_matches_the_oracle_on_command_and_issue_code(work_dir, tmp_path):
    """tan-cli#254, closed. Three invocation shapes, all now agreeing with the
    oracle on ``command``/issue code/exit code; wording is the one thing left
    unpinned (the port adds a ``git clone`` remedy suggestion the oracle never
    had).

    * Bare ``new-som`` (no ``--format``): both exit 2 on the SDK-root-
      unresolved preflight.
    * Post-subcommand ``new-som --format json``: tan-cli#399 gave
      ``new_som_cmd`` a real ``new-som.failed`` envelope -- it used to have no
      ``emit(`` call at all (``del ... output_format`` was the whole handling
      of the flag), so this answered the generic ``command: "cli"`` /
      ``cli.parse-error`` fallback instead.
    * Pre-subcommand ``--format json new-som``: this was the one shape left
      diverging even after #399, because ``cli.py`` used to gate the
      pre-subcommand position behind a hand-listed ``_HONOURS_ROOT_FORMAT``
      frozenset that ``new-som`` was never added to. tan-cli#378 replaced that
      whole mechanism with uniform ``--format`` RELOCATION
      (``_reorder_global_flags`` moving the token past the subcommand name to
      the parameter that already declares and reads it) -- every registered
      command is covered with nothing to add per-command, so this position now
      reaches the same ``new-som.failed`` envelope as the post-subcommand one,
      matching the oracle's clap ``global = true`` semantics.
    """
    home = tmp_path / "home"
    r_code, _ = rust_run(["new-som"], work_dir, home, scrub_roots=())
    p_code, _ = _run(python_command(), ["new-som"], work_dir, home)
    assert r_code == p_code == 2

    for argv in (["new-som", "--format", "json"], ["--format", "json", "new-som"]):
        _, r_out = rust_run(argv, work_dir, home, scrub_roots=())
        _, p_out = _run(python_command(), argv, work_dir, home)
        assert r_out["command"] == p_out["command"] == "new-som", (argv, r_out, p_out)
        assert r_out["exitCode"] == p_out["exitCode"] == 2, (argv, r_out, p_out)
        assert [i["code"] for i in r_out["issues"]] == ["new-som.failed"], (argv, r_out)
        assert [i["code"] for i in p_out["issues"]] == ["new-som.failed"], (argv, p_out)


def test_monitor_is_a_known_divergence_from_the_oracle(work_dir, tmp_path):
    """tan-cli#255. The oracle's ``monitor`` shares the same SDK-resolving
    forwarder as ``model``/``new-som``/``faultdecode`` and refuses
    ``monitor.failed``/"alp-sdk root is unresolved", exit 2, with no SDK
    resolvable. The port's ``monitor`` is a deliberate redesign
    (``monitor_cmd.py``'s own docstring: "no alp-sdk checkout required,
    unlike `model`" -- "a deliberate, documented improvement, not a
    regression") that never touches an SDK at all; with no ``--port`` given
    it refuses at exit 1 with EITHER ``monitor.pyserial-missing`` (pyserial
    not installed in THIS interpreter) or ``monitor.no-port`` (pyserial
    present, no port named) -- which of the two fires depends on this host's
    own package set, so both are accepted here rather than pinning the one
    this authoring host happened to hit (tan-cli#313/#324 is exactly the
    class of bug that would be).

    This is NOT the same tool-inventory gap `empty_tool_inventory` pins PATH
    against for the (now-real, tan-cli#260) `support-bundle` verb: pyserial is
    an interpreter PACKAGE, invisible to any PATH pin. The either-or is real
    and stays real across this repo's own two CI legs,
    named explicitly rather than left as an unexplained widening:
    `parity.yml`'s `python-tests` job installs `-e ".[monitor]"` (pyserial
    present -> `monitor.no-port`), `ci.yml`'s `python` job installs the bare
    package with no extras (`pip install -e ./python`, pyserial absent ->
    `monitor.pyserial-missing`) -- both are legitimate, currently-running CI
    configurations, not a hypothetical."""
    home = tmp_path / "home"
    argv = ["monitor", "--format", "json"]
    r_code, r_out = rust_run(argv, work_dir, home, scrub_roots=())
    p_code, p_out = _run(python_command(), argv, work_dir, home)
    assert (r_code, [i["code"] for i in r_out["issues"]]) == (2, ["monitor.failed"])
    assert p_code == 1
    p_codes = [i["code"] for i in p_out["issues"]]
    assert p_codes in (["monitor.pyserial-missing"], ["monitor.no-port"]), p_codes


def test_faultdecode_is_a_known_divergence_from_the_oracle(work_dir, tmp_path):
    """tan-cli#256. Exit codes COINCIDE at 2 here -- which is exactly why
    this case exists: pinned on the issue code and ``command`` field too, so
    a narrowed "exit code only" comparison could never quietly stand in for
    a real match (this file's own stated trap). The oracle forwards to
    ``alp faultdecode`` and refuses on the SAME unresolved-SDK guard as
    ``model``/``monitor``/``new-som``. The port re-implements
    ``faultdecode`` natively (pure ARMv8-M register arithmetic, no SDK read
    at all -- see ``faultdecode --help``'s own text) and refuses instead
    because no fault register was supplied on the command line."""
    home = tmp_path / "home"
    argv = ["faultdecode", "--format", "json"]
    r_code, r_out = rust_run(argv, work_dir, home, scrub_roots=())
    p_code, p_out = _run(python_command(), argv, work_dir, home)
    assert r_code == p_code == 2
    assert r_out["command"] == "faultdecode"
    assert [i["code"] for i in r_out["issues"]] == ["faultdecode.failed"]
    # tan-cli#399 narrowed this one rather than closing it. The port used to
    # answer `command: "cli"` / `cli.parse-error`, because `faultdecode` folded
    # `--format json` into its own `--json` and printed a bespoke `indent=2`
    # object carrying none of the six envelope keys. It now emits a real
    # `faultdecode` envelope, so `command` and the exit code AGREE.
    assert p_out["command"] == "faultdecode"
    assert p_code == 2
    # The residual divergence is the code string and the reason behind it. The
    # oracle refuses through its shared SDK-resolving forwarder
    # (`faultdecode.failed`, "alp-sdk root is unresolved"); the port's
    # `faultdecode` needs no SDK at all and refuses for the reason that is
    # actually true of the invocation -- no CFSR/HFSR/DFSR was supplied. Same
    # exit, more accurate cause; kept as a divergence rather than "fixed" by
    # copying a message that would name the wrong problem.
    assert [i["code"] for i in p_out["issues"]] == ["faultdecode.no-registers"]


# --- the harness must be able to go red ------------------------------------
#
# A parity run that cannot fail is worse than no parity run: it reads as
# evidence. These plant a KNOWN divergence into the same code path the real
# cases use and assert the comparator reports it.


@LIVE_GATE
def test_harness_reports_a_planted_exit_code_difference(work_dir, tmp_path):
    stub = [sys.executable, "-c", "print('tan 0.5.0-dev'); raise SystemExit(3)"]
    result = compare(
        ["--version"], cwd=work_dir, surface=VERSION, home=tmp_path / "home", python=stub
    )
    assert not result.matches
    assert any("exit code" in d for d in result.diffs), result.diffs


@LIVE_GATE
@pytest.mark.parametrize(
    "printed",
    [
        # Shape-scoping must not degrade into "any stdout passes": a version
        # line that does not satisfy the extension's regex is still a failure.
        "print('tan v0.5-dev')",
        # ...and the shape must cover the WHOLE of stdout. A prefix-anchored
        # match let both of these through as parity, on the one case that
        # actually runs today. Rust prints exactly `tan 0.4.1`
        # (oracle.PINNED_ORACLE_VERSION owns that spelling); the strings below
        # are deliberately fabricated stdout, not either binary's real output.
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


@LIVE_GATE
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
