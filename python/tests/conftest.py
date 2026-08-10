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
