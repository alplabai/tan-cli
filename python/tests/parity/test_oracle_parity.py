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
    # `debug-config`'s refusal envelope, which no conformance golden reaches:
    # all four are exit-0 previews. Pins exit 5, the `zephyr-mcu`/`none`
    # placeholder payload, `configuration: null`, the null project AND the
    # message string, across both implementations.
    (
        ["--format", "json", "debug-config", "--target-kind", "bogus"],
        ENVELOPE,
        None,
    ),
    # …and `--format` BEFORE the subcommand, which is how the four goldens
    # invoke it (clap's `global = true`). Worth its own case: Click gives the
    # group only what precedes the subcommand, so this position is a separate
    # code path in the port and not in Rust.
    (
        ["--format", "json", "debug-config", "--target-kind", "native-host", "--preview"],
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
    and breaks the envelope's platform-identical-path contract. Neither side
    has a real `west` on PATH here, so both report the same launch-error
    envelope; that error envelope still carries `data.westCommand`/`westCwd`/
    `args`, which is exactly what a westCwd or args-capture regression would
    move.
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
    result = compare(argv, cwd=work_dir, home=tmp_path / "home")
    assert result.matches, "\n".join(result.diffs)


@LIVE_GATE
@pytest.mark.parametrize(
    "target,server",
    [
        # J-Link resolves `device` + `gdbPath`; OpenOCD resolves
        # `serverpath`/`searchDir`/`configFiles`; pyOCD resolves NOTHING (the
        # board registers no such runner) and must keep its placeholder AND gain
        # the "registers no runner" note; native-host must take the native_sim
        # slice's sibling `.exe`, not the first `os: zephyr` slice's ELF.
        ("zephyr-mcu", "jlink"),
        ("zephyr-mcu", "openocd"),
        ("zephyr-mcu", "pyocd"),
        ("native-host", "none"),
    ],
)
def test_debug_config_resolution_matches_rust(target, server, work_dir, tmp_path):
    """The `<resolved-...>` overlay read off this project's OWN build output
    (#66/#83), diffed against the oracle. `--preview` only: `compare` runs both
    binaries in the SAME cwd, so a write-mode case would have the second run
    merge into what the first one wrote."""
    root = str(work_dir).replace("\\", "/")
    build = work_dir / "build"
    build.mkdir()
    (build / "system-manifest.yaml").write_text(
        PARITY_MANIFEST.format(root=root), encoding="utf-8"
    )
    zephyr = work_dir / "build" / "m55_hp-zephyr" / "build" / "zephyr"
    zephyr.mkdir(parents=True)
    (zephyr / "runners.yaml").write_text(PARITY_RUNNERS, encoding="utf-8")

    result = compare(
        ["debug-config", "--target-kind", target, "--server", server,
         "--preview", "--format", "json"],
        cwd=work_dir,
        home=tmp_path / "home",
    )
    assert result.matches, "\n".join(result.diffs)


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
def test_validate_no_sdk_guard_is_a_known_divergence_from_the_oracle(work_dir, tmp_path):
    """`board.yaml` present, no SDK resolvable: the oracle's pre-spawn guard
    answers exit 2 `validate.sdk-root-unresolved`; the port answers exit 1
    `validate.spawn-not-implemented` (the full spawn path is simply not
    ported yet) -- the genuine, tracked divergence `validate_cmd.py`'s own
    docstring calls tan-cli#262. Pinned as a KNOWN divergence, following the
    same exclude-and-pin convention `test_init_sdk_root_flag_pin_is_a_known_
    divergence_from_the_oracle` above uses, rather than asserted as parity
    that does not exist.

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
    assert (p_code, [i["code"] for i in p_out["issues"]]) == (1, ["validate.spawn-not-implemented"])


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
