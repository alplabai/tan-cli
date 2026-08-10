# SPDX-License-Identifier: Apache-2.0
"""Repo-wide test isolation for the SDK discovery ladder.

`resolve_sdk_root_ladder` / `resolve_sdk_tiered` (`tan/commands/build_cmd.py`,
`tan/commands/sdk_cmd.py`) read the REAL process environment
(`ALP_SDK_ROOT`) and the REAL `~/.alp/sdk-default` pointer (`HOME` on POSIX,
`USERPROFILE` on Windows) with no isolation of their own -- by design, so a
developer's actual shell state is what a live `tan` run resolves against.
Left unscrubbed here, the same reads make the suite non-hermetic: a
developer who follows the documented `export ALP_SDK_ROOT=<checkout>`
onboarding, or who has ever run `tan sdk switch --global`, gets a DIFFERENT
SDK resolved than a clean CI runner would, and a test asserting "nothing
resolves" observes a real checkout instead. `ZEPHYR_BASE` gets the same
treatment for the same reason: a developer/CI shell's own value must not
decide what a test observes any more than `ALP_SDK_ROOT` does.

Autouse, function-scoped, and applied to every test in this tree -- both
in-process calls (`resolve_sdk_root_ladder` et al., called directly) and the
`run_tan` subprocess helpers (`test_build_command.py` and friends), which
copy `os.environ` at spawn time and so inherit whatever `monkeypatch` leaves
in the real environment.
"""
import os
import shutil
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

# Typer renders every `--help` through Rich, and BOTH of Rich's inputs are
# ambient rather than code under test:
#
# * COLOUR. Rich emits an option name as TWO styled segments --
#   `\x1b[1;36m-\x1b[0m\x1b[1;36m-core\x1b[0m` -- so `"--core" in output` is
#   False the moment styling is on, whatever the width.
# * WIDTH. A narrow console folds an option name or wraps an asserted phrase
#   mid-string (`curated │ │ starter set`).
#
# Neither is decided by the host's OS: `typer.rich_utils` forces a terminal
# whenever `GITHUB_ACTIONS`, `FORCE_COLOR` or `PY_COLORS` is set, and takes its
# width from the tty. So a help assertion is green on a bare dev shell and red
# under Actions -- on ubuntu, windows AND macos alike.
#
# Pinned here with typer's OWN two knobs, at conftest IMPORT time and not from
# a fixture: `typer.rich_utils` reads both exactly once, at ITS import, which
# happens when the first test module imports the CLI -- after this file is
# imported, before any fixture has run. Subprocess harnesses inherit them
# through `{**os.environ}` and the child re-reads them the same way, so
# in-process `CliRunner` and spawned `python -m tan` agree.
#
# Deliberately NOT `NO_COLOR`: `tan size` and `tan faultdecode` read that
# themselves to decide their own colour, and tests pin both arms of that
# decision (`tests/core/test_size_model.py`). These two touch help rendering
# only.
os.environ["_TYPER_FORCE_DISABLE_TERMINAL"] = "1"
os.environ["TERMINAL_WIDTH"] = "200"


def sdk_root() -> Path | None:
    """The alp-sdk checkout this suite's real-SDK-gated tests bind against:
    whichever of ``ALP_SDK_PARITY_ROOT`` / ``ALP_SDK_ROOT`` first names a real
    checkout.

    Call this at MODULE import time (``SDK = sdk_root()`` at a test module's
    top level), never from inside a test body or a function-scoped fixture --
    `_scrub_sdk_discovery_env` below deletes ``ALP_SDK_ROOT`` from the process
    environment before every test function runs, so a call made after that
    point always sees it already gone. Import happens once at collection,
    before any fixture executes, which is what makes the module-level call
    see the developer's real environment.
    """
    for var in ("ALP_SDK_PARITY_ROOT", "ALP_SDK_ROOT"):
        raw = os.environ.get(var)
        if raw and (Path(raw) / "scripts" / "alp_project.py").is_file():
            return Path(raw).resolve()
    return None


#: The REAL process environment, captured at collection time -- i.e. while
#: this conftest module is first imported, before `_scrub_sdk_discovery_env`
#: below has run for any test. A test that hands an environment to a
#: subprocess it expects to behave like the developer's own shell (e.g. a
#: real `west build` in `test_native_sim_e2e.py`) must build that environment
#: from THIS, never from `os.environ` read inside a test body/fixture --
#: `_scrub_sdk_discovery_env` has by then already repointed `HOME`/
#: `USERPROFILE` at a pytest tmp dir and deleted `ALP_SDK_ROOT`.
REAL_ENVIRON: dict[str, str] = dict(os.environ)


def _probe_free_path(path: str, scratch_factory: Callable[[], Path]) -> str | None:
    """`path` with every [`PROBE_TOOLS`] identity unresolvable, or `None`
    when it already is.

    Rebuilds only the PATH ENTRIES that actually carry a probe tool, each as
    its own link farm of that directory's other contents, spliced back at the
    SAME position -- so ordering, shadowing and duplicate entries all survive.
    Dropping the offending directory outright is not an option: on this bench
    host `JLinkExe` and `openocd` live in `/usr/bin`, alongside every
    coreutil the installer-script tests execute.

    `None` when nothing matched -- measured, this is NOT guaranteed to be
    "every runner": a runner can carry an unrelated tool whose STEM happens to
    match a [`PROBE_TOOLS`] identity (a pip-installed `west`, say), and this
    function cannot know that in advance. What IS true on the no-match path:
    no filesystem work, no rebuilt PATH, nothing to go wrong -- which is why
    `scratch_factory` is a THUNK rather than an already-created directory.
    Calling `tmp_path_factory.mktemp(...)` eagerly, as a plain argument, would
    create the scratch dir on every session regardless of whether anything
    ever matched -- contradicting exactly this claim. It is called at most
    once, lazily, on the first entry that actually needs farming.

    Links rather than copies where a link is possible, because a copy of a
    live tool binary changes what `file`/codesigning/AV scanning sees and can
    be materially slower for a large directory: a symlink first, a hardlink
    second (Windows refuses a symlink without Developer Mode or elevation,
    but `os.link` works within a volume). Both are tried per FILE, not per
    directory -- `entry`'s directory may itself contain sub-directories (a
    stray `__pycache__`, a vendored tool's own support tree) that neither
    linking call can target correctly with the file-only handling below, so
    those are recursed into with `shutil.copytree` instead of linked, and
    `os.symlink` is always given `target_is_directory=` explicitly rather
    than left to guess -- omitting it is silently correct on POSIX and
    silently WRONG on Windows, where a directory symlink created without it
    is a broken file-type symlink that resolves to nothing.

    A THIRD tier, `shutil.copy2`/`shutil.copytree`, sits below the hardlink:
    `os.link` fails not only for lack of privilege but for two conditions a
    single bench host never exercises and a GitHub Windows runner always
    might -- a source and destination on DIFFERENT VOLUMES (`C:\\hostedtoolcache`
    vs a `D:\\a\\_temp` scratch dir, both real GitHub Windows runner paths,
    raise `OSError` cross-device), and a source that is itself a DIRECTORY
    (`os.link` refuses those unconditionally on every OS). Copying is
    correctness-first, not performance-first, and is the reason a symlink is
    always tried first: the fast, cheap path still wins whenever it is legal.

    Only a directory that yields to NONE of the three still RAISES -- an
    entry silently left out would remove tools the suite needs, and an entry
    silently left in would restore the exact host-dependence this exists to
    close. Loud beats either."""
    entries = path.split(os.pathsep)
    lowered = {t.lower() for t in PROBE_TOOLS}
    rebuilt: list[str] = []
    changed = False
    scratch: Path | None = None
    for index, entry in enumerate(entries):
        if not entry or not os.path.isdir(entry):
            rebuilt.append(entry)
            continue
        try:
            names = os.listdir(entry)
        except OSError:
            rebuilt.append(entry)
            continue
        # Windows matches by STEM, case-insensitively: `JLinkExe.exe` and
        # `west.cmd` are the same identities as their POSIX bare names, and
        # `%PATHEXT%` is what makes them resolvable.
        if not any(os.path.splitext(n)[0].lower() in lowered for n in names):
            rebuilt.append(entry)
            continue
        if scratch is None:
            scratch = scratch_factory()
        farm = scratch / f"probe-free-{index}"
        farm.mkdir(parents=True, exist_ok=True)
        for name in names:
            if os.path.splitext(name)[0].lower() in lowered:
                continue
            link = farm / name
            if link.exists() or link.is_symlink():
                continue
            source = os.path.join(entry, name)
            # `os.path.isdir` already follows symlinks -- a SOURCE that is
            # itself a symlink pointing (transitively) at a directory must
            # still be treated as a directory here. An earlier draft excluded
            # `os.path.islink(source)` sources from this check, which handed
            # `target_is_directory=False` to exactly the case the paragraph
            # above says this exists to get right: a symlink recreated as a
            # FILE-type symlink over a directory target is the same broken
            # shape on Windows, one level of indirection later.
            is_dir = os.path.isdir(source)
            try:
                os.symlink(source, link, target_is_directory=is_dir)
                continue
            except OSError:
                pass
            if not is_dir:
                try:
                    os.link(source, link)
                    continue
                except OSError:
                    pass
            try:
                if is_dir:
                    shutil.copytree(source, link)
                else:
                    shutil.copy2(source, link)
            except OSError as exc:
                raise RuntimeError(
                    f"cannot neutralise the debug/flash probe tooling in {entry!r}: "
                    f"neither a symlink, a hardlink nor a copy of {name!r} could be "
                    f"made ({exc}). This suite would otherwise run against whatever "
                    "probe tools this host happens to have installed and disagree "
                    "with CI silently (tan-cli#603). Remove the probe tooling from "
                    "PATH for this run, or run the suite somewhere this directory "
                    "is writable."
                ) from exc
        rebuilt.append(str(farm))
        changed = True
    return os.pathsep.join(rebuilt) if changed else None


@pytest.fixture(scope="session", autouse=True)
def _probe_tools_are_a_property_of_the_test(tmp_path_factory) -> None:
    """Make no [`PROBE_TOOLS`] identity resolve for the whole session, so
    every which()-gated branch answers the way it answers on CI (tan-cli#603).

    Mutates `os.environ` rather than using `monkeypatch`, for two reasons:
    `monkeypatch` is function-scoped and this is a session-wide property, and
    the 26 test modules that spawn `[sys.executable, "-m", "tan", ...]` build
    their child environment from `os.environ` -- an in-process patch would
    leave every one of those spawns host-dependent. Session-scoped autouse
    fixtures resolve before function-scoped ones, so
    `_scrub_sdk_discovery_env` below and any test's own `monkeypatch.setenv
    ("PATH", ...)` both still win over this, which is exactly right: a test
    that seeds a fake probe tool is DECLARING its inventory, and that
    declaration is the point.

    A no-op on a host with nothing matching -- see `_probe_free_path`, and do
    not assume that is every runner: measured directly on windows-latest
    (tan-cli#625 review, a throwaway diagnostic CI step), it is NOT.
    `C:\\hostedtoolcache\\windows\\Java_Temurin-Hotspot_jdk\\<ver>\\x64\\bin`
    -- on `PATH` for the pre-installed Temurin JDK, nothing to do with this
    suite -- carries `jlink.exe`, the JDK's own module-linker tool. That is a
    STEM collision, not a debug-probe sighting: [`PROBE_TOOLS`] matches
    case-insensitively by extension-stripped stem so it can also catch
    `JLink.cmd`/`JLink.EXE`, and `"JLink"` (the bare identity
    `doctor_cmd._collect` tries second, no `Exe` suffix) collides with Java's
    `jlink` by coincidence of naming, not by anything actually resembling a
    debug probe. Farmed like any other match -- correctly, since the function
    cannot tell "real J-Link" from "same stem, different tool" apart, and
    farming is safe either way -- but it means this fixture is exercised for
    real on windows-latest, working around a directory that was never the
    hazard tan-cli#603 was written against."""
    original = os.environ.get("PATH", "")
    rebuilt = _probe_free_path(
        original, lambda: tmp_path_factory.mktemp("probe-free-path")
    )
    if rebuilt is None:
        yield
        return
    os.environ["PATH"] = rebuilt
    try:
        yield
    finally:
        os.environ["PATH"] = original


@pytest.fixture(autouse=True)
def _scrub_sdk_discovery_env(tmp_path_factory, monkeypatch):
    monkeypatch.delenv("ALP_SDK_ROOT", raising=False)
    # A developer/CI shell's own `$ZEPHYR_BASE` must not decide what a test
    # observes any more than `ALP_SDK_ROOT` does -- left unscrubbed, a test
    # asserting "no ZEPHYR_BASE workspace resolved" instead sees a real one.
    # `test_native_sim_e2e.py` needs the REAL value for its actual `west
    # build` subprocess; it reads that from `REAL_ENVIRON` above, not from
    # `os.environ` inside the test body, for exactly this reason.
    monkeypatch.delenv("ZEPHYR_BASE", raising=False)
    # `SOURCE_DATE_EPOCH` wins over the clock in `tan.core.timestamp`, so a
    # developer or CI image that exports it (reproducible-build setups do)
    # changes every `generatedAt`/`updatedAt` this suite observes -- and a
    # value in MILLISECONDS renders a 5-digit year that `%Y` cannot parse,
    # which is what `tests/core/test_timestamp.py` exists to document. The
    # tests that WANT one set it themselves.
    monkeypatch.delenv("SOURCE_DATE_EPOCH", raising=False)
    # Same class as `ZEPHYR_BASE` above, and load-bearing from tan-cli#547 on:
    # `${TOOLCHAIN_ROOT}` now has a real resolver, and
    # `ZEPHYR_SDK_INSTALL_DIR` short-circuits every branch of it. Left
    # unscrubbed, a developer or CI shell that exports it makes every
    # toolchain-root test observe THAT path instead of the one the test
    # built -- including the demotion tests, which would resolve instead of
    # demoting and go red for a reason that is nothing to do with the code.
    monkeypatch.delenv("ZEPHYR_SDK_INSTALL_DIR", raising=False)
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))


# ---------------------------------------------------------------------------
# The SUBJECT under test, pinned the way the ORACLE used to be (tan-cli#423).
#
# `pinned_oracle` -- retired with the oracle itself in tan-cli#269 -- stopped
# the suite measuring the wrong ORACLE. Nothing stopped it measuring the wrong
# SUBJECT, and that half is still live: `import tan` and `python -m tan`
# both resolve through `sys.path`, so whichever `tan` is INSTALLED wins
# whenever the repo's own `python/` is not first. On a developer box with an
# editable install pointing at a second checkout, that is a completely
# different tree -- measured:
#
#     <repo root>/          python -m tan --version  ->  tan 0.5.0-rc3
#     <repo root>/python/   python -m tan --version  ->  tan 0.5.0-rc5.dev0
#
# and `importlib.util.find_spec("tan").origin` resolved to
# `<another tan-cli worktree>/python/tan/__init__.py`.
#
# That is not a theoretical hazard. It made `tests/parity/seam1_field_diff.py`
# report
#
#     FAIL multicore_rpmsg-imx93: alp-sdk refuses but tan does not
#
# for a `status: tbd` hw_rev the tan under test refuses correctly in every
# emit mode -- a FALSE parity divergence, filed as a real one before the cause
# was found. A green run from `python/` and a red run from the repo root, for
# code that never changed.
#
# Two halves, because there are two ways in:
#   * IN-PROCESS `import tan` -- asserted below, and a mismatch FAILS the
#     session rather than skipping: a quiet skip would hide exactly the gap
#     this fixture exists to surface.
#   * SPAWNED `[sys.executable, "-m", "tan", ...]` -- 26 test files do this,
#     and a child resolves through its OWN sys.path, so asserting in this
#     process would not touch them. `PYTHONPATH` is prepended instead, which
#     is inherited by every child regardless of cwd or how the argv is built.
# ---------------------------------------------------------------------------
@pytest.fixture(scope="session", autouse=True)
def tan_under_test() -> None:
    """Refuse to run the suite against a `tan` from another tree.

    Session-scoped and autouse from the tree root, so it nets every consumer
    -- in-process import and spawned child alike -- present and future.
    """
    repo_python = Path(__file__).resolve().parents[1]

    # Children first: prepend rather than replace, so a caller who set
    # PYTHONPATH for their own reasons keeps it, just not ahead of us.
    existing = os.environ.get("PYTHONPATH", "")
    parts = [p for p in existing.split(os.pathsep) if p]
    if str(repo_python) not in parts:
        os.environ["PYTHONPATH"] = os.pathsep.join([str(repo_python), *parts])

    import tan as _tan

    origin = getattr(_tan, "__file__", None)
    assert origin is not None, (
        "`import tan` resolved to a module with no __file__ (a namespace "
        "package shadowing the real one?) -- this suite cannot tell what it "
        "would be measuring (tan-cli#423)"
    )
    resolved = Path(origin).resolve()
    assert repo_python in resolved.parents, (
        f"the suite imported `tan` from {resolved}, which is NOT under this "
        f"repository's {repo_python}. That is almost always an editable "
        "install pointing at another checkout, and it means every assertion "
        "here would describe a different tree -- including the parity gates, "
        "which have already reported a divergence that did not exist because "
        "of exactly this (tan-cli#423). Run pytest from `python/`, or "
        "`pip uninstall alp-tan`, or set PYTHONPATH to this repo's `python/`."
    )


# ---------------------------------------------------------------------------
# The DEBUG/FLASH PROBE INVENTORY, made a property of the test rather than of
# the host (tan-cli#603).
#
# A bench host genuinely has `JLinkExe`, `openocd`, `pyocd` and `west`
# installed; a CI runner has none of them. Every which()-gated branch that
# turns on one of those identities therefore answers DIFFERENTLY in the two
# places, silently. tan-cli#600 is what that costs: seven
# `test_flow_d_preflight_*` cases green on the bench host and red on
# ubuntu-latest, windows-latest AND macos-latest at once -- a false negative
# that does not merely fail to warn, it points the next hour of debugging at
# "a CI problem".
#
# Measured on the tip this landed at, by instrumenting tan's three resolution
# seams (`doctor_cmd.on_path`, `tool_lookup.resolve_tool`, `shutil.which`)
# across two full suite runs: 34 tests took a DIFFERENT BRANCH on the two
# PATHs -- every `_collect` case in `tests/commands/test_doctor_command.py`,
# three in `tests/gates/test_doctor_check_scope.py`, and one in
# `tests/commands/test_support_bundle_command.py`. Identical outcome (3710
# passed either way), different meaning; and on the bench host those runs
# really do spawn `/usr/bin/JLinkExe -?` and `west --version`.
#
# SURGICAL, deliberately. `empty_tool_inventory` below removes EVERYTHING,
# which is right for one test and wrong for the suite: measured, a full run
# under a PATH holding only `sh bash git which env ls cat uname` fails 36
# tests that need real `python3`/`sleep`/`mktemp`/`sed`/`curl`/`tar`/
# `sha256sum` -- tools a CI runner has. So only the identities a runner
# genuinely lacks are removed, and `tests/gates/test_probe_tool_inventory.py::
# test_ordinary_host_tooling_is_untouched` holds this to that.
# ---------------------------------------------------------------------------
#: The probe/flash/Zephyr-meta identities tan resolves through `on_path` /
#: `resolve_tool` / `shutil.which` that a GitHub runner never has installed.
#: Deliberately NOT `git`/`cmake`/`ninja`/`python3`/`xz`/`wget`/`dd`/`gzip`/
#: `addr2line`/`7z`: a runner HAS those, so removing them would invent a new
#: divergence in the opposite direction rather than close this one.
PROBE_TOOLS: frozenset[str] = frozenset(
    {
        # J-Link, all three names `doctor_cmd._collect` tries in order.
        "JLinkExe",
        "JLink",
        "JLinkGDBServerCL",
        "JLinkGDBServer",
        # The other two flash back-ends `flash_cmd` probes for.
        "openocd",
        "pyocd",
        # Zephyr's meta-tool: the single biggest divergence measured (74
        # resolutions across the suite on the bench host, none on CI).
        "west",
        # Emulator + image writer, same class: probed by `renode_cmd` /
        # `flash_cmd`, absent from every runner.
        "renode",
        "bmaptool",
        # Zephyr-SDK-only; `faultdecode_cmd` tries it ahead of the ordinary
        # `llvm-addr2line`/`addr2line` pair, which are NOT listed here.
        "arm-zephyr-eabi-addr2line",
    }
)


# ---------------------------------------------------------------------------
# A PATH that resolves NOTHING. Lifted here from `tests/parity/oracle.py` when
# tan-cli#269 deleted that module: the helper was never about the oracle, and
# its one surviving caller (`tests/test_cli_skeleton.py`) uses it on the
# PYTHON `tan`.
# ---------------------------------------------------------------------------
def empty_tool_inventory(scratch: Path) -> str:
    """A ``PATH`` value under which every PROBED tool reports absent.

    Any ``shutil.which`` / ``doctor_cmd.on_path`` probe run against this
    directory finds nothing -- ``west`` for the west-forwarding verbs,
    ``git``/``cmake``/``ninja``/``python3``/``xz``/``wget`` for
    ``support-bundle.hostPrerequisites``, and any other which()-gated case's
    absent-tool branch -- so a test's answer does not depend on what the host
    running it happens to have installed (tan-cli#313, tan-cli#324).

    Seeds the directory with exactly one file: a symlink to the REAL ``which``
    this host resolves on its own PATH, POSIX only (a no-op on Windows, whose
    probe walks ``%PATH%`` by hand and never spawns an external ``which`` at
    all). A directory that is genuinely, literally empty is NOT a clean
    "nothing on PATH" answer on POSIX: a probe that resolves a tool by
    SPAWNING ``which <tool>`` has to find ``which`` itself via this SAME
    (test-controlled) PATH. An empty directory cannot resolve ``which``
    either, so the probe fails before it ever answers the question asked --
    measured directly: a PATH holding every one of ``support-bundle``'s six
    required tools but NOT ``which`` still reports all six missing, and
    copying a working ``which`` in (touching nothing else) makes that same
    warning vanish entirely. Seeding it is what makes "everything missing" a
    GENUINE probe that ran and found nothing, rather than a degenerate one
    that could not run at all.

    REPLACES ``PATH`` outright rather than prepending: prepending would still
    let a REAL tool further down the host's own PATH be found, which is
    exactly the host-dependence this exists to remove.
    """
    stub_dir = scratch / "empty-path"
    stub_dir.mkdir(exist_ok=True)
    if sys.platform != "win32":
        # The seed is NOT optional -- see the docstring. Refuse rather than
        # silently return a PATH whose "everything missing" answer is an
        # artefact of an unresolvable `which` spawn (tan-cli#313, #324).
        real_which = shutil.which("which")
        if real_which is None:
            raise RuntimeError(
                "empty_tool_inventory() cannot seed `which` into the stub PATH: "
                "shutil.which('which') returned None on this POSIX host. Without "
                "it a tool probe cannot run at all and reports every tool missing "
                "for the wrong reason -- refusing rather than pinning that "
                "artefact."
            )
        link = stub_dir / "which"
        if not link.exists():
            os.symlink(real_which, link)
        assert link.exists(), f"failed to seed `which` into {stub_dir}"
    return str(stub_dir)
