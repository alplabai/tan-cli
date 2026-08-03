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
    argv = [
        "--project",
        str(work_dir),
        verb,
        "--format",
        "json",
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
    assert result.matches, "\n".join(result.diffs)


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
    """`board.yaml` present, no SDK resolvable: BOTH sides answer exit 2
    `validate.sdk-root-unresolved` -- the oracle's own pre-spawn guard 2.

    Was pinned here as a KNOWN divergence (the port answered
    `validate.spawn-not-implemented`, a code the oracle has no counterpart
    for at all, because the spawn path was unported). tan-cli#376 ported it,
    so this is now real parity and is asserted as such. RENAMED with the
    behaviour, which moves its frozen-fixture key -- see this test's entry in
    `oracle_fixtures/test_oracle_parity.json` and the rename note at the end
    of `oracle_fixtures/PROVENANCE.txt`; the captured ORACLE answer under that
    key is unchanged, only the port's half of the comparison moved.

    Both halves of each side stay pinned (exit code AND issue code) rather
    than narrowed to the code alone: exit 2 is also what an invalid board
    gets, so a regression that turned this guard into a verdict would be
    invisible to an exit-code-only assertion.

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


# --- v0.6.0's named command-surface parity ----------------------------------
#
# v0.6.0's own milestone goal states, verbatim: "Full command-surface parity
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
# widening `_ORACLE_REQUIRED`'s condition to also skip on a version mismatch)
# would reopen exactly the hole `missing_for_live`'s own docstring refuses --
# "a quiet skip here would hide exactly the gap that function exists to
# surface" (tan-cli#272) -- so `pinned_oracle`, now a session-scoped, autouse
# fixture in `conftest.py` that every module under `tests/parity/` inherits
# (not just this section), FAILS the run instead, loudly, naming the
# mismatch. `_ORACLE_REQUIRED` below composes only the presence skip: the
# content check no longer needs opting into per case.


def _oracle_required(fn):
    """`@_ORACLE_REQUIRED`'s actual decorator: skips on binary ABSENCE only.
    The version-WRONGNESS check (`pinned_oracle`) is a session-scoped,
    autouse fixture in `conftest.py` now, so every case tagged
    `@_ORACLE_REQUIRED` gets it for free the same way every OTHER module
    under `tests/parity/` does -- nothing here opts it in by hand any more."""
    fn = pytest.mark.skipif(
        RUST is None,
        reason="needs a built Rust tan; run `cargo build --bin tan` (or set TAN_RUST_BINARY)",
    )(fn)
    return fn


_ORACLE_REQUIRED = _oracle_required


@_ORACLE_REQUIRED
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
    r_code, r_out = _run([RUST], argv, work_dir, home)
    p_code, p_out = _run(python_command(), argv, work_dir, home)
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

@_ORACLE_REQUIRED
def test_renode_no_sdk_matches_the_oracle(work_dir, tmp_path):
    """tan-cli#77. ``renode`` with no alp-sdk resolvable and no manifest is
    byte-identical -- the whole ``data`` placeholder shape (empty sku/repl/
    resc/elf, the derived ``logPath``) included."""
    home = tmp_path / "home"
    argv = ["renode", "--format", "json"]
    r_code, r_out = _run([RUST], argv, work_dir, home)
    p_code, p_out = _run(python_command(), argv, work_dir, home)
    assert r_code == p_code == 1
    assert r_out == p_out


@_ORACLE_REQUIRED
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
    r_code, r_out = _run([RUST], argv, work_dir, home)
    p_code, p_out = _run(python_command(), argv, work_dir, home)
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
    rust_tail = (
        "The system cannot find the path specified. (os error 3))."
        if os.name == "nt"
        else "No such file or directory (os error 2))."
    )
    assert r_message == prefix + rust_tail
    # `!r`, not `'{...}'`: `OSError.__str__` interpolates the filename with
    # `%r`, so on Windows every separator in it comes back DOUBLED
    # (`...\\build\\system-manifest.yaml`). Hand-quoting reproduced the POSIX
    # rendering only. `!r` is what the runtime itself does, so it is right on
    # both platforms and cannot drift from it.
    assert p_message == prefix + f"[Errno 2] No such file or directory: {manifest_path!r})."
    # Everything OUTSIDE the message -- exit code, `data`, the issue code --
    # is a real match, not just coincidentally unchecked here.
    assert {**r_out, "issues": []} == {**p_out, "issues": []}


@_ORACLE_REQUIRED
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
    r_code, r_out = _run([RUST], argv, work_dir, home)
    p_code, p_out = _run(python_command(), argv, work_dir, home)
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


@_ORACLE_REQUIRED
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
    r_code, r_out = _run([RUST], argv, work_dir, home)
    p_code, p_out = _run(python_command(), argv, work_dir, home)
    assert r_out["command"] == p_out["command"] == "model"
    assert (r_code, [i["code"] for i in r_out["issues"]]) == (2, ["model.failed"])
    assert (p_code, [i["code"] for i in p_out["issues"]]) == (1, ["model.unknown-subcommand"])


@_ORACLE_REQUIRED
def test_new_som_failure_envelope_now_matches_the_oracle(work_dir, tmp_path):
    """tan-cli#254, CLOSED (tan-cli#399 postmortem) -- was a documented
    divergence here; this is the promotion the module docstring's own
    convention asks for the moment a pinned XFAIL would start passing for
    real, applied by hand since this case is a plain assertion, not an
    ``xfail(strict=True)``.

    The divergence used to be that the port's ``new-som`` accepted
    ``--format`` (a hidden option mirroring clap's ``global = true``
    ``GlobalArgs``) but never READ it -- ``new_som_cmd`` ``del``d the value --
    so a failing run stayed text mode, printed no envelope of its own, and
    ``main``'s fallback wrapped it in the generic ``command: "cli"`` /
    ``cli.parse-error`` envelope, where the oracle's own ``--format json``
    reaches a real ``command: "new-som"`` refusal (``new-som.failed``,
    exit 2). ``new_som_cmd`` now emits that envelope itself (tan-cli#399), so
    both sides agree on ``command``, ``exitCode`` and the issue code -- pinned
    below at BOTH the bare invocation (exit 2 on each side already, before
    tan-cli#399) and under ``--format json`` (the half that used to diverge).

    NOT a flag-POSITION case: the argv below puts ``--format json`` AFTER the
    subcommand, the position that has always parsed. What still differs, and
    is deliberately left OUT of the comparison, is the refusal MESSAGE text --
    the port adds a ``git clone`` suggestion the oracle never had -- which is
    exactly why this checks ``command``/``exitCode``/issue ``code`` and not a
    whole-envelope `compare()`.
    """
    home = tmp_path / "home"
    r_code, _ = _run([RUST], ["new-som"], work_dir, home)
    p_code, _ = _run(python_command(), ["new-som"], work_dir, home)
    assert r_code == 2
    assert p_code == 2
    _, r_json_out = _run([RUST], ["new-som", "--format", "json"], work_dir, home)
    _, p_json_out = _run(python_command(), ["new-som", "--format", "json"], work_dir, home)
    assert r_json_out["command"] == p_json_out["command"] == "new-som"
    assert r_json_out["exitCode"] == p_json_out["exitCode"] == 2
    assert [i["code"] for i in r_json_out["issues"]] == ["new-som.failed"]
    assert [i["code"] for i in p_json_out["issues"]] == ["new-som.failed"]


@_ORACLE_REQUIRED
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
    r_code, r_out = _run([RUST], argv, work_dir, home)
    p_code, p_out = _run(python_command(), argv, work_dir, home)
    assert (r_code, [i["code"] for i in r_out["issues"]]) == (2, ["monitor.failed"])
    assert p_code == 1
    p_codes = [i["code"] for i in p_out["issues"]]
    assert p_codes in (["monitor.pyserial-missing"], ["monitor.no-port"]), p_codes


@_ORACLE_REQUIRED
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
    r_code, r_out = _run([RUST], argv, work_dir, home)
    p_code, p_out = _run(python_command(), argv, work_dir, home)
    assert r_code == p_code == 2
    assert r_out["command"] == "faultdecode"
    assert [i["code"] for i in r_out["issues"]] == ["faultdecode.failed"]
    assert p_out["command"] == "cli"
    assert [i["code"] for i in p_out["issues"]] == ["cli.parse-error"]


# --- tan-cli#398/#403: an INJECTED value-carrying global flag must be
# accept-and-ignore, matching the oracle, not accept-and-REFUSE -------------
#
# `accept_global_flags` (`tan/core/global_flags.py`) injects `--board-yaml`
# and `--target` on every command that does not already implement them, for
# oracle-argv-parity (tan-cli#261): the oracle's clap `GlobalArgs` marks both
# `global = true`, so `target/debug/tan.exe <any command> --board-yaml x` /
# `... --target x` always PARSES, even on a command whose own Rust handler
# never reads the value. tan-cli#398 changed what happens to an INJECTED
# value-carrying flag when a value is actually supplied, from "accept and
# drop" to "refuse, exit 2" -- reasoning (correct for exactly one flag,
# `model`'s `--board-yaml`) that silently dropping a supplied value serves
# the command's own default in its place. Refusing regressed every OTHER
# (command, flag) pair below: the oracle itself accepts and ignores the same
# flag there, so the refusal was a NEW divergence, not a fix, and put the
# WRONG command (`cli`, `main`'s generic usage-error fallback, since the
# refusal fires before the wrapped command -- and therefore before `emit()`
# -- ever runs) in the JSON envelope on an argv the oracle runs cleanly.
#
# Hand-listed rather than derived off `_SUBCOMMAND_NAMES` x `GLOBAL_FLAGS`
# (unlike `tests/gates/test_global_flags_gate.py`'s PARSE-only gate, which IS
# fully derived): this table's job is different -- it is not "does the flag
# parse" but "does the DROPPED VALUE still answer the oracle's exit code",
# which needs a live oracle spawn per pair and is expensive enough that a
# blind full cross product (32 commands x 10 flags) would slow this suite for
# combinations no defect has ever touched. This is the measured tan-cli#398
# regression set, snapshotted 2026-08: `--board-yaml` on 3 commands (`model`
# is EXCLUDED, see below) and `--target` on 14, 17 pairs total here, alongside
# `test_derived_pair_count_has_not_silently_shrunk` guarding against a count
# that quietly drops to zero and stops testing anything. The regression's
# FULL count is 18 -- `model` x `--board-yaml` was the 18th pair, and the ONE
# that computed a genuinely wrong answer when dropped (a real board.yaml built
# for the WRONG SKU, not a refusal); it is excluded from THIS table, not
# untested.
#
# `model` carries NEITHER flag here even though `accept_global_flags` used to
# inject `--board-yaml` for it: `model_cmd.py`'s own `--board` option now
# declares `--board-yaml` as a second name for the SAME option (tan-cli#403),
# so it is a REAL, read alias, not a dropped one -- covered instead by
# `tests/commands/test_model_command.py`'s own `--board-yaml` case.
_BOARD_YAML_DROP_COMMANDS = ("examples", "explain", "sdk")
_TARGET_DROP_COMMANDS = (
    "bootstrap", "debug-config", "doctor", "examples", "flash", "image",
    "kconfig", "model", "presets", "renode", "run", "sdk", "size", "validate",
)

#: A command that needs a SUBCOMMAND before its options even parse (`model
#: build`, `sdk current`) -- `None` for the 30 flat commands. Matches the real
#: shape a caller would use; a bare `model --target x` fails on its OWN
#: `model.unknown-subcommand` regardless of `--target`, which would make the
#: pair "match" the oracle for a reason that has nothing to do with this
#: regression (`test_model_bare_invocation_is_a_known_divergence_from_the_
#: oracle`, above, is exactly that unrelated, already-pinned divergence).
_REQUIRES_SUBCOMMAND = {"model": "build", "sdk": "current"}


def _dropped_flag_pairs():
    for command in _BOARD_YAML_DROP_COMMANDS:
        yield command, "--board-yaml", "nonexistent/board.yaml"
    for command in _TARGET_DROP_COMMANDS:
        yield command, "--target", "zephyr"


def _argv_for(command: str, flag: str, value: str) -> list[str]:
    argv = [command]
    sub = _REQUIRES_SUBCOMMAND.get(command)
    if sub is not None:
        argv.append(sub)
    argv += [flag, value, "--format", "json"]
    return argv


@_ORACLE_REQUIRED
@pytest.mark.parametrize(
    "command,flag,value",
    list(_dropped_flag_pairs()),
    ids=[f"{c}-{f}" for c, f, _v in _dropped_flag_pairs()],
)
def test_an_injected_value_carrying_flag_exits_like_the_oracle_not_refused(
    command, flag, value, work_dir, tmp_path
):
    """The regression test tan-cli#398 shipped without: probed with a
    trailing ``--help`` (`test_global_flags_gate.py`), which Click's own
    eager short-circuit answers before the wrapped command -- and therefore
    before the refusal in `accept_global_flags`'s wrapper -- ever runs, so
    that gate stayed green on both sides of the tan-cli#398 regression.
    A REAL value on a REAL invocation is what actually reaches the refusal.
    """
    home = tmp_path / "home"
    argv = _argv_for(command, flag, value)
    r_code, _ = _run([RUST], argv, work_dir, home)
    p_code, _ = _run(python_command(), argv, work_dir, home)
    assert p_code == r_code, (
        f"`tan {' '.join(argv)}` exited {p_code}, the oracle exits {r_code} -- "
        f"an injected {flag} must be accepted and ignored, not refused."
    )


def test_derived_pair_count_has_not_silently_shrunk():
    """The canary `test_global_flags_gate.py`'s own file names for the same
    reason: a parametrised list that quietly shrinks to zero reports green
    while checking nothing."""
    assert len(list(_dropped_flag_pairs())) == 17


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
