# SPDX-License-Identifier: Apache-2.0
"""Repo-wide test isolation for the SDK discovery ladder.

`resolve_sdk_root_ladder` / `resolve_sdk_tiered` (`tan/core/sdk_discovery.py`,
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

Scrubbing decides what a test OBSERVES. It says nothing about whether the
checkout the SDK-gated tests deliberately bind against is the one this branch
was measured against -- so the second block in this file (tan-cli#691) warns,
once per session and never fatally, when the bound `ALP_SDK_ROOT` is not
`PINNED_SDK_COMMIT`, or when tan's two alp-sdk pins disagree with each other.
"""
import functools
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import types
from collections.abc import Callable, Mapping
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
    bound = _bound_sdk_root()
    return None if bound is None else bound[1]


def _bound_sdk_root(environ: Mapping[str, str] | None = None) -> tuple[str, Path] | None:
    """`sdk_root()`'s answer, plus WHICH variable produced it -- `(var, root)`
    for the first of ``ALP_SDK_PARITY_ROOT`` / ``ALP_SDK_ROOT`` that names a
    real checkout, `None` when neither does.

    Split out of `sdk_root()` for the warning below, which has to NAME the
    variable it is complaining about: "the bound tree disagrees with the pin"
    is unactionable without saying which of the two env vars bound it. The
    acceptance test (`scripts/alp_project.py` is a file under the root) and
    the precedence order are `sdk_root()`'s, unchanged -- there is exactly one
    implementation of both, here, so the warning can never describe a
    different root than the one the SDK-gated tests actually measured.

    `environ` defaults to the LIVE `os.environ` because `sdk_root()`'s callers
    are module-level and want that; the warning passes `REAL_ENVIRON` instead,
    since it runs from a fixture/hook, after `_scrub_sdk_discovery_env` has
    deleted `ALP_SDK_ROOT` for the first test.
    """
    env = os.environ if environ is None else environ
    for var in ("ALP_SDK_PARITY_ROOT", "ALP_SDK_ROOT"):
        raw = env.get(var)
        if raw and (Path(raw) / "scripts" / "alp_project.py").is_file():
            return var, Path(raw).resolve()
    return None


# ---------------------------------------------------------------------------
# WHAT THE BOUND TREE CARRIES -- a different question from whether one is
# bound at all (tan-cli#791).
#
# `sdk_root()` above answers "is an alp-sdk bound?", and every SDK-gated module
# skips on `None`. Nothing answered "does the bound one PUBLISH the metadata
# this module asserts against?", and the two are not the same question: a tree
# bound at a pin that PREDATES that metadata does not skip, it runs and FAILS
# -- and the failure belongs to the pin, not to the branch.
#
# That is not hypothetical. `.github/workflows/ci.yml`'s `sdk_parity` checkout
# `ref:` and `parity.yml`'s `PINNED_SDK_TAG` are both `eb96112b`, which
# predates every artefact ADR-0028 publishes on the alp-sdk side. alp-sdk#1470
# is still OPEN, so there is no post-merge SHA to move the pin to yet, and
# until there is, twelve tests under `tests/model/` measure a tree that cannot
# answer them.
#
# These are CAPABILITY predicates, the same discipline as
# `pytest.importorskip("tflite")`: each names ONE artefact and reads the bound
# tree for it. Deliberately NOT one blanket "is the bound SDK new enough"
# switch -- the twelve failures have four distinct causes, a skip has to say
# WHICH, and a reader has to be able to tell an expected skip from a
# regression without opening the other repository.
#
# Two properties every predicate here must keep, because a skip that hides a
# real check is worse than the failure it replaced:
#
#  * It tests for the PRESENCE of the artefact, never for "would this
#    assertion fail". So it CANNOT fire once the pin moves onto a tree that
#    carries it. A tree carrying it only PARTIALLY (say `npu_toolchain.vela`
#    on i.MX 93 but not on the Alif parts) makes these RUN and FAIL, which is
#    the correct direction: a loud failure names the gap, a silent skip buries
#    it.
#  * It fires only when a root IS bound. With nothing bound, the module's own
#    `ALP_SDK_ROOT is not set` skip is the accurate reason and must be the one
#    reported -- pytest takes the first true `skipif` in closest-first order,
#    so a capability mark that also fired on `None` would shadow it.
# ---------------------------------------------------------------------------

#: Where the pin has to get to. Named in every reason string below rather than
#: paraphrased, so a reader who sees one of these skips can go straight to the
#: PR and see whether it has merged (in which case the skip is a bug in the
#: predicate) or not (in which case it is expected).
_SDK_PR = "alplabai/alp-sdk#1470"


def sdk_publishes_vela_profile(sdk: Path) -> bool:
    """True when ANY SoC spec under ``<sdk>/metadata/socs/`` carries an
    ``npu_toolchain.vela`` block -- alp-sdk `fff41087`, which landed it on all
    six Alif Ensemble parts and on i.MX 93 in one commit.

    ANY, not "the part this test is about", and that is the safe direction on
    purpose: `fff41087` is atomic, so there is no real tree in which some
    Ethos-U parts carry the block and others do not. Should one ever exist,
    "any" makes these tests RUN against it and fail loudly on the part that is
    missing, rather than skipping the whole set on a partial answer.
    """
    for spec in sorted((sdk / "metadata" / "socs").rglob("*.json")):
        try:
            soc = json.loads(spec.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue          # a spec this reader cannot parse is not evidence
        block = soc.get("npu_toolchain")
        if isinstance(block, dict) and isinstance(block.get("vela"), dict):
            return True
    return False


def sdk_publishes_npu_op_tables(sdk: Path) -> bool:
    """True when ``<sdk>/metadata/npu_ops/`` holds at least one op-support
    table -- the per-backend, per-variant JSON `tan.model.analyze` resolves by
    SKU (`ethos_u/u85@vela-5.1.0.json`, `drpai/onnx-i8@translator-1.12.json`).

    The whole directory is absent before ADR-0028; there is no partial state
    to distinguish, so presence of the tree is the whole question.
    """
    return any((sdk / "metadata" / "npu_ops").rglob("*.json"))


def sdk_predates_the_model_engine_relocation(sdk: Path) -> bool:
    """True when ``<sdk>/scripts/alp_model/`` still exists -- i.e. this alp-sdk
    still owns a HOST-SIDE model engine and ADR-0028 Task 6 (`ab6968e2`,
    *"delete the host-side model engine, relocated to tan"*) has not landed on
    it.

    A STRUCTURAL fact about the bound tree, deliberately not a comparison
    against the assertion it gates. `ab6968e2` deleted that package AND
    regenerated alp-sdk's three committed C fixtures through the relocated
    generator, so their banner moved from ``python -m alp_model._gen_fixture``
    to ``python -m tan.model._gen_fixture``. Gating on "does the committed
    header already say what we produce?" would be the self-defeating shape --
    a guard that skips exactly when it would have caught something. Gating on
    the package's presence names the CAUSE and goes away with it.
    """
    return (sdk / "scripts" / "alp_model").is_dir()


def sdk_publishes_model_perf_points(sdk: Path) -> bool:
    """True when ``<sdk>/metadata/model_perf/`` holds at least one published
    bench-measured perf point -- the tier-2 facts `tan.model.perf` reads.

    This one is a step further out than its three siblings above: the others
    gate on metadata that EXISTS on `alplabai/alp-sdk#1470` and is merely
    absent from the pin, whereas `metadata/model_perf/` exists in NO alp-sdk
    at all yet. The contract landed (alp-sdk `fe56ff1d`: the schema, the
    validator semantics, the capture recipe and ONE synthetic fixture point
    under `tests/fixtures/`), but the directory this reads stays empty until
    the bench campaign that is Task 5 of the tier-2 plan runs on real silicon.
    So a test carrying this mark skips EVERYWHERE today, including against a
    bound `origin/dev`, and that is the honest state rather than a defect --
    the alternative is a test that asserts against data nobody has measured.
    Presence of a real point, not "would this assertion pass", so it cannot
    fire once the campaign publishes one.
    """
    return any((sdk / "metadata" / "model_perf").rglob("*.json"))


def sdk_ships_the_model_perf_fixture(sdk: Path) -> bool:
    """True when ``<sdk>/tests/fixtures/model_perf/`` holds at least one
    synthetic perf point -- alp-sdk's own `_fixture`-bannered document.

    A SEPARATE artefact from the published tree above and therefore a separate
    predicate, not a broader one: a test that proves tan REFUSES a fixture
    point needs a real fixture document to refuse, and that exists today
    (alp-sdk `fe56ff1d`) while a published point does not. Folding the two
    into one switch would skip the refusal proof for the next several months
    on the strength of an unrelated absence.
    """
    return any((sdk / "tests" / "fixtures" / "model_perf").rglob("*.json"))


#: Resolved once, here, at conftest import -- i.e. at the same moment a test
#: module's own `SDK = sdk_root()` resolves, and before
#: `_scrub_sdk_discovery_env` deletes `ALP_SDK_ROOT` for the first test.
_BOUND_SDK: Path | None = sdk_root()

#: `metadata/socs/**`'s `npu_toolchain.vela`. Without it
#: `tan.model.targets.resolve_targets` yields `vela_memory_mode=None` for every
#: Ethos-U target, so tan invokes vela flagless and vela picks its own
#: DRAM-backed profile -- which on parts that have no DRAM reports 0 KiB SRAM
#: and is exactly what `VelaFootprintRefused` exists to refuse. Under an older
#: pin that refusal is CORRECT behaviour, so these tests have no premise.
needs_sdk_vela_profile = pytest.mark.skipif(
    _BOUND_SDK is not None and not sdk_publishes_vela_profile(_BOUND_SDK),
    reason=(
        "the bound alp-sdk publishes no `npu_toolchain.vela` in any "
        "metadata/socs/**.json: it predates alp-sdk fff41087 "
        f"({_SDK_PR}, still open), so resolve_targets() resolves "
        "vela_memory_mode=None for every Ethos-U part and this test asserts "
        "against metadata the bound tree does not carry. EXPECTED while "
        "ci.yml's sdk_parity `ref:` and parity.yml's PINNED_SDK_TAG sit at "
        "eb96112b; once that pin moves this test RUNS, and a failure here "
        "then is a regression, not this skip."
    ),
)

#: `metadata/npu_ops/**`, the committed op-support tables.
needs_sdk_npu_op_tables = pytest.mark.skipif(
    _BOUND_SDK is not None and not sdk_publishes_npu_op_tables(_BOUND_SDK),
    reason=(
        "the bound alp-sdk ships no metadata/npu_ops/ tables at all: it "
        f"predates alp-sdk 93f2e8f8/ab6968e2 ({_SDK_PR}, still open), so "
        "analyze_backend() resolves table=None for every backend and this "
        "test asserts against real committed op vocabularies the bound tree "
        "does not carry. EXPECTED while ci.yml's sdk_parity `ref:` and "
        "parity.yml's PINNED_SDK_TAG sit at eb96112b; once that pin moves "
        "this test RUNS, and a failure here then is a regression, not this "
        "skip."
    ),
)

#: alp-sdk's own committed C fixtures, as regenerated by the relocated
#: generator.
needs_sdk_after_the_model_engine_relocation = pytest.mark.skipif(
    _BOUND_SDK is not None and sdk_predates_the_model_engine_relocation(_BOUND_SDK),
    reason=(
        "the bound alp-sdk still ships scripts/alp_model/, so it predates "
        f"alp-sdk ab6968e2 ({_SDK_PR}, still open) and its committed C "
        "fixtures were generated by that package -- their banner still reads "
        "`python -m alp_model._gen_fixture`, which this relocated generator "
        "no longer emits and must not. The CONTAINER BYTES are unaffected and "
        "stay asserted unconditionally next door. EXPECTED while ci.yml's "
        "sdk_parity `ref:` and parity.yml's PINNED_SDK_TAG sit at eb96112b; "
        "once that pin moves this test RUNS, and a failure here then is a "
        "regression, not this skip."
    ),
)

#: `metadata/model_perf/**`, the published bench-measured perf points.
needs_sdk_model_perf_points = pytest.mark.skipif(
    _BOUND_SDK is not None and not sdk_publishes_model_perf_points(_BOUND_SDK),
    reason=(
        "the bound alp-sdk publishes no metadata/model_perf/ perf points: the "
        "tier-2 CONTRACT landed (alp-sdk fe56ff1d -- schema, validator "
        "semantics, capture recipe, one synthetic fixture) but the published "
        "tree stays empty until the bench campaign that is Task 5 of "
        "docs/superpowers/plans/2026-08-16-model-perf-tier2.md runs on real "
        "silicon. EXPECTED everywhere today, including against a bound "
        "origin/dev -- there is no measured data to assert against yet, and "
        "authoring one to make this run would be exactly the fabricated "
        "bench number the whole tier forbids. Once a real point is published "
        "this test RUNS, and a failure here then is a regression, not this "
        "skip."
    ),
)

#: `tests/fixtures/model_perf/**`, alp-sdk's own `_fixture`-bannered synthetic.
needs_sdk_model_perf_fixture = pytest.mark.skipif(
    _BOUND_SDK is not None and not sdk_ships_the_model_perf_fixture(_BOUND_SDK),
    reason=(
        "the bound alp-sdk ships no tests/fixtures/model_perf/ synthetic perf "
        f"point: it predates alp-sdk fe56ff1d ({_SDK_PR}, still open), so "
        "there is no real fixture document for this test to prove tan REFUSES. "
        "EXPECTED while ci.yml's sdk_parity `ref:` and parity.yml's "
        "PINNED_SDK_TAG sit at eb96112b; once that pin moves this test RUNS, "
        "and a failure here then is a regression, not this skip."
    ),
)


#: The REAL process environment, captured at collection time -- i.e. while
#: this conftest module is first imported, before `_scrub_sdk_discovery_env`
#: below has run for any test. A test that hands an environment to a
#: subprocess it expects to behave like the developer's own shell (e.g. a
#: real `west build` in `test_native_sim_e2e.py`) must build that environment
#: from THIS, never from `os.environ` read inside a test body/fixture --
#: `_scrub_sdk_discovery_env` has by then already repointed `HOME`/
#: `USERPROFILE` at a pytest tmp dir and deleted `ALP_SDK_ROOT`.
REAL_ENVIRON: dict[str, str] = dict(os.environ)


# ---------------------------------------------------------------------------
# COLLECTION-TIME PRE-FLIGHT: does a SPAWNED `tan` subprocess survive the
# `HOME`/`USERPROFILE` repoint `_scrub_sdk_discovery_env` applies to every
# test in this tree? (tan-cli#903)
#
# That fixture is correct and deliberate -- see the module docstring -- but it
# has a side effect nothing here used to check for: every test that spawns
# `[sys.executable, "-m", "tan", ...]` builds that child's environment from
# `os.environ` AFTER the scrub has already repointed `HOME`, so a dependency
# (`typer`, at the time this was filed) that is importable only via a
# user-site install under the developer's REAL `HOME` (`~/.local`) vanishes
# for every one of those children. The result is not one clear error -- it is
# several hundred unrelated-looking failures deep in the suite, each a
# crashed subprocess with empty stdout, because the ACTUAL cause (a missing
# import in a *different* process) never surfaces as a message at all.
# Measured on the incident that opened this issue: 678 and 679 failures on
# two independent runs, and a THIRD collection (both SDK roots bound) at
# 1127 failed / 4228 passed / 768 skipped / 1 xfailed / 17 errors -- every
# one of them "typer"-shaped or a downstream symptom of a subprocess that
# crashed before writing anything.
#
# On WINDOWS this specific cause can never fire: CPython's user-site base on
# that platform comes from `%APPDATA%`, not `%USERPROFILE%`/`HOME` -- so a
# scrubbed `USERPROFILE` does not hide a Windows user-site install the way a
# scrubbed `HOME` hides a POSIX one. That means no false positive there, and
# the 5 green `windows-latest` legs at the time of writing are consistent
# with that -- but a green Windows leg is NOT evidence this pre-flight (or
# the bug it guards against) does anything on that platform; it is evidence
# of the opposite. Recorded here so nobody reads Windows-green as "the check
# works everywhere".
#
# `tan_under_test` below already spawns this same probe -- but as a
# session-scoped AUTOUSE FIXTURE, which pytest sets up for the first test
# BEFORE that test's function-scoped `_scrub_sdk_discovery_env` runs (broader
# scopes set up first; verified empirically, not merely assumed). So it
# always probes under the REAL, unscrubbed `HOME`, which is precisely the one
# environment this bug does NOT reproduce in. That makes it USELESS for
# tan-cli#903 SPECIFICALLY, by construction -- it was never wired to see the
# environment the bug lives in -- and that is the ONLY thing "useless" means
# here. It remains a good, NECESSARY check for tan-cli#423/#665 (wrong `tan`
# under test), and it stays the only spawn-probe still armed on the two paths
# that skip `pytest_configure` below entirely -- an xdist WORKER process, and
# a session run with `TAN_TEST_SKIP_HOME_PREFLIGHT` set -- so do not read
# "useless for #903" as "useless" and delete it: doing so would silently
# disable the #423/#665 wrong-tree backstop on exactly those two paths, a
# fresh instance of the "check that cannot fire" class this file exists to
# close.
#
# So this runs earlier still, from `pytest_configure` -- before collection,
# before ANY fixture, session-scoped or not, has executed -- and builds its
# OWN scrubbed-HOME environment by hand from `REAL_ENVIRON` rather than
# waiting for `_scrub_sdk_discovery_env` to produce one. The single most
# important property here is that HOOK ORDERING: a check that let the real
# fixture do the scrubbing for it, or that read the live environment after
# collection had begun, would inherit the bug it exists to catch and pass in
# exactly the case that matters. Reading from `REAL_ENVIRON` rather than
# `os.environ` is what makes that independent of a future change nearby --
# AT pytest_configure time the two are still equal (no fixture has run yet
# to diverge them), so today the ordering alone is what protects this check;
# building the scrub from the untouched capture, rather than the live
# environment, is what keeps that true even if something earlier in
# collection someday starts mutating `os.environ` directly.
#
# What is probed is deliberately NOT a hardcoded package name. `typer` was
# this incident's proximate cause, but it is not the only runtime dependency
# `tan.__main__` pulls in (`pyproject.toml`'s `dependencies` also names rich,
# pyyaml, jsonschema, click, truststore, certifi, and that list can grow).
# Spawning the exact command every one of the 31 subprocess-based test files
# spawns -- `sys.executable -m tan --version` -- exercises the real import
# chain as it stands today, whatever it is, without this file needing to know
# its contents. Under-probing (missing the next instance) and over-probing
# (false-failing on an unrelated absence) both come from hand-picking a
# module name; running the actual entry point avoids both failure modes at
# once.
#
# COST: measured on `--collect-only`, three runs each, dev vs this branch --
# 0.56/0.78/0.64s vs 1.50/1.20/1.44s -- so this adds roughly +0.72s to every
# pytest invocation, not only the ones that run anything. Negligible next to
# an ~850s full suite, but it is paid on every IDE-triggered collection and
# doubles the wall time of a single-test edit loop.
# ---------------------------------------------------------------------------

#: Escape hatch, named in the failure message itself so it is discoverable
#: without reading this file. A hard collection failure is the right default
#: -- "a run that cannot produce a meaningful result should not produce 678
#: meaningless ones" (tan-cli#903) -- but it is a big hammer: a developer who
#: already knows their interpreter is unusual (deliberately probing a broken
#: install, say) must be able to say so and get the real suite instead of a
#: refusal.
TAN_TEST_SKIP_HOME_PREFLIGHT = "TAN_TEST_SKIP_HOME_PREFLIGHT"


def _home_scrubbed_environ(home: Path) -> dict[str, str]:
    """`REAL_ENVIRON` with `HOME`/`USERPROFILE` repointed at `home` and
    `ALP_SDK_ROOT` removed, reproduced here as a pure function so the
    pre-flight below can apply it BEFORE `_scrub_sdk_discovery_env`, or any
    fixture, has run.

    NOT the identical transformation `_scrub_sdk_discovery_env` applies,
    despite the resemblance -- two deliberate, SAFE divergences, not a
    drift to reconcile:

    * That fixture also deletes `ZEPHYR_BASE`, `SOURCE_DATE_EPOCH` and
      `ZEPHYR_SDK_INSTALL_DIR`; this function deletes only `ALP_SDK_ROOT`.
      The probe below only asks "can `sys.executable -m tan --version` import
      its dependencies under this HOME", a question those three variables do
      not affect either way, so leaving them at the developer's real values
      here is inert rather than wrong.
    * The caller (`_home_preflight_failure`) additionally prepends this
      repo's `python/` onto `PYTHONPATH` before spawning -- mirroring
      `tan_under_test`'s spawn-side prepend below, NOT this scrub. That
      prepend is load-bearing, not decoration: without it the probe cannot
      find `tan` at all (a bare `ModuleNotFoundError`, nothing to do with
      HOME) before `tan_under_test` ever gets a chance to apply its own.

    Both divergences only ADD to what `_scrub_sdk_discovery_env` would leave
    in place -- the probe environment is a strict superset -- which is why
    the probe still reproduces the HOME-shaped failure this file exists to
    catch (tan-cli#903) despite not being byte-for-byte identical to it.

    Deliberately built from `REAL_ENVIRON`, the capture taken at import before
    anything scrubs it, and not from live `os.environ` -- see the block
    comment above this function for why that ordering is load-bearing rather
    than incidental.
    """
    env = dict(REAL_ENVIRON)
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)
    env.pop("ALP_SDK_ROOT", None)
    return env


def _with_repo_pythonpath(environ: Mapping[str, str]) -> dict[str, str]:
    """`environ` with this repo's `python/` prepended onto `PYTHONPATH` --
    the same prepend `tan_under_test` applies to `os.environ` for its own
    spawned probe, reproduced here as a pure function so BOTH probes in
    `_home_preflight_failure` (the scrubbed-HOME one and its real-HOME
    control) apply the identical transformation and differ ONLY in
    HOME/USERPROFILE, which is the whole point of running a control at all.
    """
    env = dict(environ)
    repo_python = str(Path(__file__).resolve().parents[1])
    existing = [p for p in env.get("PYTHONPATH", "").split(os.pathsep) if p]
    if repo_python not in existing:
        env["PYTHONPATH"] = os.pathsep.join([repo_python, *existing])
    return env


#: Values that turn the escape hatch ON. Case-insensitive, and deliberately
#: NOT bare truthiness (`os.environ.get(...)`) -- measured, that reading
#: means `TAN_TEST_SKIP_HOME_PREFLIGHT=0` DISABLES the pre-flight (any
#: non-empty string is truthy in Python), the exact opposite of what a
#: developer setting `=0` means. `=1`/`=true`/`=yes`/`=on` all skip it;
#: anything else -- including `=0`, `=false`, or an unset/empty var -- leaves
#: it armed.
_SKIP_HOME_PREFLIGHT_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})


def _skip_home_preflight_requested() -> bool:
    """Whether `TAN_TEST_SKIP_HOME_PREFLIGHT` names one of
    `_SKIP_HOME_PREFLIGHT_TRUE_VALUES`, case-insensitively -- never bare
    truthiness. See that constant for why `=0` must NOT skip the check."""
    value = os.environ.get(TAN_TEST_SKIP_HOME_PREFLIGHT, "")
    return value.strip().lower() in _SKIP_HOME_PREFLIGHT_TRUE_VALUES


def _run_version_probe(environ: Mapping[str, str], context: str) -> subprocess.CompletedProcess[str] | str:
    """Spawn `sys.executable -m tan --version` under `environ`.

    Returns the `CompletedProcess` on any ordinary exit (the CALLER decides
    what a nonzero `returncode` means -- this function only reports whether
    the probe ran at all), or a ready-to-print diagnostic STRING when the
    subprocess could not even be spawned, or hung past its timeout. Those two
    failure shapes get their own wording -- a spawn that never happened
    ("could not even SPAWN") is a materially different claim from a spawn
    that happened and then hung ("DID spawn, never returned"), and conflating
    them under one message misdescribes whichever one didn't occur. Both name
    the escape hatch, so a developer reading either one has the same way out
    regardless of which branch they hit.

    `context` is a short phrase describing which of the two probes this is
    ("under a scrubbed HOME", "under this interpreter's own, REAL,
    unmodified HOME (the control probe)") so the two callers in
    `_home_preflight_failure` don't have to duplicate this function just to
    get a different noun into the message.
    """
    try:
        return subprocess.run(
            [sys.executable, "-m", "tan", "--version"],
            env=dict(environ),
            capture_output=True,
            text=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired as exc:
        return (
            f"`{sys.executable} -m tan --version` HUNG for over 60 seconds "
            f"{context} instead of exiting -- it DID spawn, it just never "
            f"returned: {exc}. This pre-flight cannot tell whether the suite "
            "itself would hang the same way; investigate directly (a shell "
            "that never finishes venv-activating, an interactive prompt "
            "`--version` should never trigger, ...). To bypass this check "
            f"and run the suite anyway, set {TAN_TEST_SKIP_HOME_PREFLIGHT}=1."
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return (
            f"could not even SPAWN `{sys.executable} -m tan --version` "
            f"{context}: {exc}. To bypass this check and run the suite "
            f"anyway, set {TAN_TEST_SKIP_HOME_PREFLIGHT}=1."
        )


def _home_preflight_failure() -> str | None:
    """`None` when `sys.executable -m tan --version` still works once `HOME`/
    `USERPROFILE` are repointed at a throwaway directory; otherwise a
    ready-to-print diagnostic naming the cause and the fix.

    `-m tan`, not `-c "import tan"`: `tan/__init__.py` is empty (see
    `tan_under_test` below), so a bare import proves nothing about whether
    the dependencies `tan.__main__` actually needs are reachable. `--version`
    is the cheapest subcommand that still forces that full import chain.

    A nonzero exit under the scrubbed HOME is NOT on its own evidence that
    HOME caused it -- `tan --version` can be broken for reasons that have
    nothing to do with HOME (a `SyntaxError` in `tan/cli.py`, say), and
    printing this function's venv-repair recipe in that case sends a
    developer chasing a cause that isn't there. So a scrubbed-HOME failure
    triggers a CONTROL: the identical probe run again under `REAL_ENVIRON`,
    with only `ALP_SDK_ROOT` also popped (matching `_home_scrubbed_environ`,
    so a variable neither probe's subject reads today cannot later become an
    unnoticed second difference) -- the two runs differ in HOME/USERPROFILE
    and nothing else, which is what lets the diagnostic tell "HOME did this"
    from "tan is just broken here" apart, and print the right one.

    A pure function of `REAL_ENVIRON` and this host's filesystem -- no
    fixture, no pytest state -- so `pytest_configure` below can call it before
    collection has even started.

    This is a TWO-SAMPLE comparison, once each, with no retry and no stderr
    diffing between the two runs -- it cannot, on its own, distinguish "the
    scrubbed probe failed because of HOME" from "the scrubbed probe hit a
    one-off flake (a transient fork failure, a momentarily-full disk, a slow
    CI neighbour) that the control's later, unrelated run simply didn't
    repeat." A flaky scrubbed-probe failure paired with a healthy control
    still prints the venv recipe and blames HOME, wrongly, on exactly that
    scenario. Treat a report from this function as a strong lead, not a
    proof, if the failure doesn't reproduce on a second run.
    """
    with tempfile.TemporaryDirectory(prefix="tan-home-preflight-") as scratch_home:
        scrubbed_env = _with_repo_pythonpath(_home_scrubbed_environ(Path(scratch_home)))
        probe = _run_version_probe(scrubbed_env, "under a scrubbed HOME to pre-flight this suite")
    if isinstance(probe, str):
        return probe
    if probe.returncode == 0:
        return None

    # `ALP_SDK_ROOT` popped here too, matching `_home_scrubbed_environ` --
    # otherwise the two probes would differ in HOME/USERPROFILE AND in
    # whether `ALP_SDK_ROOT` is bound, which is exactly the two-variable
    # confound the "differ ONLY in HOME" claim below (and in
    # `_with_repo_pythonpath`'s own docstring) exists to rule out. `tan
    # --version` does not read `ALP_SDK_ROOT` today, so this is inert now --
    # but a control that could pass or fail on ALP_SDK_ROOT alone would stop
    # being a control the moment that stops being true.
    control_env = dict(REAL_ENVIRON)
    control_env.pop("ALP_SDK_ROOT", None)
    control_env = _with_repo_pythonpath(control_env)
    control = _run_version_probe(
        control_env, "under this interpreter's own, REAL, unmodified HOME (the control probe)"
    )
    if isinstance(control, str):
        # The control itself could not be run -- say so rather than guessing
        # at HOME's role from a comparison that never completed.
        return (
            f"`{sys.executable} -m tan --version` fails (exit {probe.returncode}) "
            "under a scrubbed HOME, AND the control probe meant to confirm HOME "
            f"is the cause could not be run either: {control}\n\n"
            f"stderr from the scrubbed-HOME probe:\n{probe.stderr.strip()}"
        )

    if control.returncode != 0:
        # BOTH probes fail -- scrubbed HOME and the developer's own, real
        # HOME alike -- so the failure is not caused by the HOME/USERPROFILE
        # repoint this suite's own `_scrub_sdk_discovery_env` fixture applies.
        # `tan` is broken in this tree regardless of HOME; the venv recipe
        # below would not fix that, so it is deliberately NOT printed here.
        return (
            f"`{sys.executable} -m tan --version` fails (exit {probe.returncode}) "
            "under a scrubbed HOME, AND fails the SAME way "
            f"(exit {control.returncode}) under this interpreter's own, REAL, "
            "unmodified HOME -- so this is NOT the scrubbed-HOME failure mode "
            "tan-cli#903 exists to catch. `tan --version` is broken in this "
            "tree regardless of HOME; fix `tan` itself first (this pre-flight "
            "has nothing more specific to add, and the usual scrubbed-HOME "
            "venv recipe would not fix a failure that also reproduces under "
            "your real HOME).\n\n"
            f"stderr from the scrubbed-HOME probe:\n{probe.stderr.strip()}\n\n"
            f"stderr from the control (real-HOME) probe:\n{control.stderr.strip()}\n\n"
            f"To bypass this check and run the suite anyway, set"
            f" {TAN_TEST_SKIP_HOME_PREFLIGHT}=1."
        )

    # The control SUCCEEDED under the developer's real HOME -- confirming the
    # scrubbed HOME really is what broke the scrubbed-HOME probe.
    return (
        f"`{sys.executable} -m tan --version` fails (exit {probe.returncode}) "
        "once HOME/USERPROFILE are repointed at a throwaway directory -- "
        "exactly what THIS SUITE's own `_scrub_sdk_discovery_env` fixture "
        "does to every test's subprocess environment (python/tests/"
        "conftest.py); the SAME probe under this interpreter's own, REAL "
        "HOME succeeds (control probe, exit 0), which is what pins the cause "
        "to HOME rather than to `tan` itself. Left uncaught, that produces "
        "several hundred unrelated-looking test failures (crashed subprocesses "
        "returning empty stdout) instead of this one message (tan-cli#903).\n\n"
        f"stderr from the (scrubbed-HOME) probe:\n{probe.stderr.strip()}\n\n"
        "The usual cause: this interpreter's runtime dependencies (typer, "
        "pydantic, ...) are importable only via a user-site install under "
        "your REAL HOME (e.g. `~/.local`), which a scrubbed-HOME subprocess "
        "cannot see. Fix it the documented way (README.md, \"New "
        "implementation work belongs under python/\"):\n"
        "    python3.12 -m venv .venv\n"
        '    .venv/bin/python -m pip install -e "./python[monitor]"\n'
        "    .venv/bin/python -m pip install pytest\n"
        "    (cd python && ../.venv/bin/python -m pytest tests -q)\n\n"
        f"To bypass this check and run the suite anyway (NOT recommended --"
        " every test that spawns a subprocess will fail with its own"
        " unrelated-looking error instead), set"
        f" {TAN_TEST_SKIP_HOME_PREFLIGHT}=1."
    )


def pytest_configure(config: pytest.Config) -> None:
    """Fail collection ONCE, with one clear diagnostic, rather than letting
    hundreds of individually-spawned subprocesses fail for a reason none of
    them can name (tan-cli#903).

    `pytest_configure` fires before collection starts and therefore before
    ANY fixture -- session-scoped or function-scoped -- has touched `HOME`;
    `_home_preflight_failure` builds its own scrubbed-HOME environment rather
    than relying on `_scrub_sdk_discovery_env` to have already produced one,
    which is what lets this check observe the failure mode instead of
    inheriting the scrub that hides it (see the block comment above).

    Skipped on xdist WORKER processes (`config.workerinput` is only set
    there): the controller process runs this exact hook first and would have
    already aborted the whole session via `UsageError` before any worker is
    spawned, so a worker re-running the same subprocess probe is pure
    duplicated cost, not additional coverage.

    FAILS rather than warns -- deliberately, per tan-cli#903's own framing:
    "a run that cannot produce a meaningful result should not produce 678
    meaningless ones." A warning here is exactly as easy to miss as the 678
    failures it would otherwise sit above; a `pytest.UsageError` stops the
    run immediately, before a single test executes, with nothing else to
    scroll past. `TAN_TEST_SKIP_HOME_PREFLIGHT=1` is the escape hatch for a
    developer who already knows their interpreter is unusual on purpose --
    named in the failure message itself, so it is discoverable without
    reading this file.

    SELECTION-blind, by design: this spawns the probe regardless of which
    node IDs pytest was actually asked to collect, even a selection that
    would never itself spawn a `tan` subprocess (measured:
    `pytest -q tests/core/test_timestamp.py` against a broken interpreter is
    `14 passed` on a healthy HOME, and this `UsageError` on a scrubbed one).
    Nothing here can know in advance whether a later `-k`/path selection
    needs a working `tan` child, so it aborts on the safe assumption that it
    might; `TAN_TEST_SKIP_HOME_PREFLIGHT=1` runs the selection anyway.
    """
    if getattr(config, "workerinput", None) is not None:
        return
    if _skip_home_preflight_requested():
        return
    problem = _home_preflight_failure()
    if problem is not None:
        raw_value = os.environ.get(TAN_TEST_SKIP_HOME_PREFLIGHT)
        if raw_value:
            # The var IS set, just not to a recognised value (fail-safe:
            # `y`/`t`/`enabled` and similar near-misses don't skip the
            # check) -- say so explicitly, or a developer who already tried
            # to bypass this sees only "set X=1" and has no reason to
            # suspect their existing `X=y` was the reason it didn't work.
            problem += (
                f"\n\n(note: {TAN_TEST_SKIP_HOME_PREFLIGHT} is already set to "
                f"{raw_value!r}, which is not one of the recognised bypass "
                f"values -- {sorted(_SKIP_HOME_PREFLIGHT_TRUE_VALUES)!r}, "
                "case-insensitively.)"
            )
        raise pytest.UsageError(problem)


# ---------------------------------------------------------------------------
# THE PINS vs THE BOUND TREE, said out loud once per session (tan-cli#691).
#
# `sdk_root()` above answers "which alp-sdk checkout is bound?". Nothing
# answered "is that the checkout this branch was measured against?", and the
# two are not the same question: `ALP_SDK_ROOT` is whatever the developer's
# shell exports, while the commit tan's own gates are pinned to is written
# down in this repository (`PINNED_SDK_COMMIT`). When they disagree, the
# SDK-gated tests measure a tree this branch has never been reconciled to and
# report real-looking failures that are neither pre-existing nor caused by the
# change under test.
#
# Measured, 2026-08-12, same node IDs, unmodified `origin/dev`, only
# `ALP_SDK_ROOT` varied:
#
#     ALP_SDK_ROOT=a317330595f744d35f4d785869517110f3678f70  ->  30 passed
#     ALP_SDK_ROOT=c07254b2589406acb3fcb5556bf1e995395431e3  ->   5 failed, 25 passed
#
# `c07254b2` is four commits ahead of the pin; alp-sdk#1389 adopted BOTH
# halves of tan-cli#616 upstream (so `faultdecode`'s declared divergence
# evaporates and the non-vacuity guard fires with `classes: []`) and
# alp-sdk#1400 changed the scaffold's `ALP_SDK_ROOT` emit (so the vendored
# CMakeLists no longer match). Both are CORRECT behaviour against a tree tan
# has not been reconciled to yet -- which is exactly what a pin means. A full
# suite reported `9 failed, 4986 passed` and it took a three-way comparison by
# hand to establish that five of the nine were the bound tree, not the branch.
#
# WARN, NEVER FAIL, and the distinction is the point. Binding a newer tree
# deliberately is legitimate -- it is how the next re-sync's workload is
# discovered before the re-sync -- so failing here would forbid the very thing
# this repository's planner-resync flow depends on. The defect is that it is
# SILENT, not that it is done. Nor may it fire when nothing is bound, which is
# the common case (`ci.yml`'s `python` job, a bare `pytest tests/`): a warning
# on every ordinary run is a warning nobody reads.
#
# The SECOND comparison here is between the two pins THEMSELVES, and it needs
# no bound tree at all. `PINNED_SDK_COMMIT` (the freshness gate's audit ref)
# and `parity.yml`'s `PINNED_SDK_TAG` (what `ci.yml`'s `sdk_parity` checkout
# `ref:` must equal, "MUST be bumped together" in that file's own words) have
# now drifted apart twice -- mid-review of #485, and again in PR #688 -- both
# times caught by a human reading output rather than by a gate. It is one line
# once the machinery above exists, so it is here rather than in a fourth
# place. A split IS sometimes deliberate (`parity.yml`: the audit commit "can
# legitimately sit on either side" of the parity tag), which is the other
# reason this warns instead of failing.
# ---------------------------------------------------------------------------

#: `python/tests/conftest.py` -> `tests` -> `python` -> the repository root.
_REPO_ROOT = Path(__file__).resolve().parents[2]

#: The only place `PINNED_SDK_COMMIT` is written down.
_FRESHNESS_GATE = (
    _REPO_ROOT / "python" / "tests" / "gates" / "test_planner_relocation_freshness.py"
)

#: The only place `PINNED_SDK_TAG` is written down -- `getting-started.yml`
#: already `sed`s it out of this same file rather than keeping a second copy.
_PARITY_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "parity.yml"

#: ANCHORED at line start, exactly as `parity.yml`'s own `resolve planner
#: audit commit` step greps it, and for the same reason: the gate declares
#: `HAND_PORT_PINNED_SDK_COMMIT` too, and an unanchored pattern matches that
#: line as well because it CONTAINS the substring `PINNED_SDK_COMMIT = "..."`.
_PINNED_SDK_COMMIT_RE = re.compile(r'^PINNED_SDK_COMMIT = "([0-9a-f]{40})"', re.MULTILINE)

#: `PINNED_SDK_TAG: <sha>` as a workflow-level `env:` entry -- indented, so
#: unlike the pin above this one cannot be anchored hard at column 0.
_PINNED_SDK_TAG_RE = re.compile(r"^[ \t]*PINNED_SDK_TAG:[ \t]*([0-9a-f]{40})[ \t]*$", re.MULTILINE)


def _sole_pin(path: Path, pattern: re.Pattern[str], name: str) -> tuple[str | None, str | None]:
    """`(sha, None)` when `path` declares exactly one `name`, else
    `(None, <what went wrong>)`.

    Refuses a plural match rather than taking the first: two matches means the
    pattern has started catching a pin it was never about, and silently
    comparing against the wrong one of them is the same class of quiet wrong
    answer this whole block exists to end. `parity.yml`'s own grep learned
    this the expensive way -- an unanchored pattern returned two SHAs and
    wrote a malformed `$GITHUB_OUTPUT` line that failed three steps later
    with nothing naming the cause.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return None, f"could not read {name} out of {path}: {exc}"
    found = pattern.findall(text)
    if len(found) != 1:
        return None, f"expected exactly ONE {name} in {path}, found {len(found)}: {found}"
    return found[0], None


def _git(root: Path, *args: str) -> str | None:
    """Stripped stdout of `git -C <root> <args...>`, or `None` for ANY
    unhappy outcome -- git absent, a non-zero exit, a hang, a decoding
    failure.

    Every caller below treats `None` as "say nothing", which is deliberate:
    the check is an aid to interpreting a test run, so a host without git or
    an alp-sdk delivered as a tarball rather than a checkout must be silent,
    not noisy. `timeout` rather than an unbounded wait for the same reason --
    a wedged git must not hold the session open.
    """
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError, UnicodeDecodeError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


def _git_head(root: Path) -> str | None:
    """The 40-hex `HEAD` of the git checkout AT `root`, or `None` when `root`
    is not itself the top of one.

    `--show-toplevel` first, and it is load-bearing, not belt-and-braces:
    `git -C <dir> rev-parse HEAD` happily answers for the ENCLOSING
    repository when `<dir>` is merely nested inside one. An alp-sdk unpacked
    from a tarball into a directory that happens to live under some other
    checkout would otherwise be reported as "bound to <that repo's HEAD>",
    warning about a disagreement between two SHAs that have nothing to do
    with each other. `sdk_root()` only ever accepts a path with
    `scripts/alp_project.py` directly under it -- the alp-sdk repository ROOT
    -- so equality with the toplevel is the correct test, not an
    over-restriction.
    """
    toplevel = _git(root, "rev-parse", "--show-toplevel")
    if toplevel is None:
        return None
    try:
        resolved = Path(toplevel).resolve()
    except OSError:
        return None
    # normcase, because Windows path comparison is case-insensitive and git
    # reports forward slashes there while `Path.resolve()` reports backslashes.
    if os.path.normcase(str(resolved)) != os.path.normcase(str(root.resolve())):
        return None
    head = _git(root, "rev-parse", "HEAD")
    if head is None or not re.fullmatch(r"[0-9a-f]{40}", head):
        return None
    return head


def _distance(root: Path, pin: str, head: str) -> tuple[int, int] | None:
    """`(ahead, behind)` of `head` relative to `pin`, or `None` when the two
    are not comparable in this checkout.

    Not comparable is the NORMAL case in CI, not an error: `actions/checkout`
    clones at depth 1, so `pin` is usually not an object in the bound tree at
    all. The SHA mismatch is still worth reporting without a count, so this
    returns `None` rather than suppressing the whole warning.
    """
    counts = _git(root, "rev-list", "--left-right", "--count", f"{pin}...{head}")
    if counts is None:
        return None
    parts = counts.split()
    if len(parts) != 2:
        return None
    try:
        behind, ahead = (int(part) for part in parts)
    except ValueError:
        return None
    return ahead, behind


def sdk_pin_disagreements(
    bound: tuple[str, Path] | None,
    *,
    gate_path: Path = _FRESHNESS_GATE,
    workflow_path: Path = _PARITY_WORKFLOW,
) -> list[str]:
    """Every way the alp-sdk pins and the bound tree currently disagree, one
    warning per disagreement, as ready-to-print lines. Empty means agreement
    -- which is the ordinary answer and prints nothing at all.

    A pure function of `(bound, gate_path, workflow_path)` and the git state
    at `bound`, deliberately: it takes no fixture, reads no global, and asserts
    nothing, so the gate that proves it fires can build a REAL two-commit
    checkout in a tmp dir and read the answer back, rather than mocking the
    condition it is supposed to detect.
    """
    lines: list[str] = []

    pinned_commit, commit_problem = _sole_pin(gate_path, _PINNED_SDK_COMMIT_RE, "PINNED_SDK_COMMIT")
    pinned_tag, tag_problem = _sole_pin(workflow_path, _PINNED_SDK_TAG_RE, "PINNED_SDK_TAG")
    for problem in (commit_problem, tag_problem):
        if problem is not None:
            lines.append(
                f"WARNING (tan-cli#691): this session cannot compare tan's alp-sdk "
                f"pins -- {problem}. A pin that moved or was reshaped leaves this "
                f"check reading nothing while reporting nothing; point it at the "
                f"pin's new home."
            )

    if pinned_commit is not None and pinned_tag is not None and pinned_commit != pinned_tag:
        lines += [
            "WARNING (tan-cli#691): tan's two alp-sdk pins disagree with EACH OTHER:",
            f"    PINNED_SDK_COMMIT  {pinned_commit}  ({gate_path})",
            f"    PINNED_SDK_TAG     {pinned_tag}  ({workflow_path})",
            "    `ci.yml`'s `sdk_parity` checkout `ref:` tracks PINNED_SDK_TAG and must",
            "    equal it too -- they 'MUST be bumped together' (ci.yml), and that pair",
            "    has already drifted twice (mid-review of #485, and PR #688). A split is",
            "    sometimes deliberate (the audit commit may legitimately sit on either",
            "    side of the parity tag); a forgotten one measures tan against two",
            "    different alp-sdks at once.",
        ]

    if bound is None or pinned_commit is None:
        return lines

    var, root = bound
    head = _git_head(root)
    if head is None or head == pinned_commit:
        return lines

    distance = _distance(root, pinned_commit, head)
    if distance is None:
        direction = (
            "    the commit distance is unknown here (the pin is not an object in the"
            " bound tree -- a shallow clone, or a different repository)."
        )
    else:
        ahead, behind = distance
        direction = (
            f"    the bound tree is {ahead} commit(s) AHEAD of the pin,"
            f" and {behind} behind."
        )
    return lines + [
        "WARNING (tan-cli#691): the bound alp-sdk tree is NOT the commit tan pins.",
        f"    {var}={root}",
        f"        HEAD               {head}",
        f"        PINNED_SDK_COMMIT  {pinned_commit}  ({gate_path})",
        direction,
        "    This does NOT fail the run, and binding a newer tree is a legitimate thing",
        "    to do -- it is how the next re-sync's workload is discovered. But every",
        "    failure in this run may belong to the BOUND TREE rather than to this",
        "    branch: on 2026-08-12 five of nine did. Re-run with the pinned tree bound,",
        "    or with the variable unset, before reading a failure as this branch's.",
    ]


@functools.cache
def _sdk_pin_warning_lines() -> tuple[str, ...]:
    """`sdk_pin_disagreements` for THIS session, computed at most once.

    Cached because it is emitted from two places (see below) and it spawns
    git; `REAL_ENVIRON` rather than `os.environ` because both of those places
    run after `_scrub_sdk_discovery_env` has deleted `ALP_SDK_ROOT` for the
    first test, and reading the live environment there would answer "nothing
    bound" on exactly the runs this exists for.
    """
    return tuple(sdk_pin_disagreements(_bound_sdk_root(REAL_ENVIRON)))


def _write_sdk_pin_warning(write_line: Callable[[str], None]) -> None:
    """Emit the block through `write_line`, or emit nothing at all."""
    lines = _sdk_pin_warning_lines()
    if not lines:
        return
    write_line("")
    for line in lines:
        write_line(line)


def _pin_warning_writer(config: pytest.Config) -> Callable[[str], None]:
    """A `write_line` that is actually visible.

    The terminal reporter, not `warnings.warn` and not a bare `print`: a
    warning has to survive `-q` (which suppresses `pytest_report_header`
    entirely -- measured), pytest's output capture (which swallows a `print`
    from a fixture until something replays it), and `-W error` (which would
    turn a `warnings.warn` into an ERROR and break the "never fail" rule this
    whole block is built on). Coloured, because the point is that it is not
    missed in a 5000-line run. `None` when the reporter is not installed
    (`-p no:terminal`), where stderr is the only channel left.
    """
    reporter = config.pluginmanager.getplugin("terminalreporter")
    if reporter is None:
        return lambda line: print(line, file=sys.stderr)
    return lambda line: reporter.write_line(line, yellow=True, bold=True)


@pytest.fixture(scope="session", autouse=True)
def _warn_when_the_bound_sdk_disagrees_with_the_pins(pytestconfig) -> None:
    """Say it BEFORE the tests run, so a session bound to the wrong tree can
    be aborted in its first second rather than its 55th minute."""
    _write_sdk_pin_warning(_pin_warning_writer(pytestconfig))


def pytest_terminal_summary(terminalreporter) -> None:
    """Say it AGAIN at the end, next to the failures it explains.

    Deliberately the same block twice per session, and only ever twice: the
    fixture above catches it early, this catches it where the reader is
    actually looking when they ask "why did these nine fail?" -- the question
    tan-cli#691 was opened about. Both are no-ops on an agreeing session, so
    the cost of the duplication is zero on every ordinary run.
    """
    _write_sdk_pin_warning(
        lambda line: terminalreporter.write_line(line, yellow=True, bold=True)
    )


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
    the 31 test modules that spawn `[sys.executable, "-m", "tan", ...]` build
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
    # Same reasoning, same class, added when `build.toolchain._candidates`
    # started scanning the ADR 0021 artifact-keyed store (tan-cli#990
    # review): `$ALP_TOOLCHAIN_ROOT` is documented for real bench/CI
    # machines, and left unscrubbed a shell that exports it would make every
    # toolchain-root test see THAT store instead of the fresh-per-test
    # `home/.alp/toolchains` this fixture builds below.
    monkeypatch.delenv("ALP_TOOLCHAIN_ROOT", raising=False)
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))


# Every `tan.planner`-or-under module is BOTH a `sys.modules[name]` entry AND
# a plain attribute of its PARENT package module -- Python's import machinery
# keeps the two in lockstep on every ordinary import, but any test that does
# `sys.modules` surgery (deleting/rebinding `tan.planner*` entries to force a
# fresh reimport, e.g. to rebind against a different SDK root) can leave them
# pointing at two DIFFERENT module objects if it restores one location and
# not the other -- tan-cli#943's actual defect. This is NOT a one-level
# check: the defect reproduces just as well one layer down (e.g.
# `tan.planner.kconfig` restored in `sys.modules` while `tan.planner`'s own
# `.kconfig` attribute keeps the leaked reimport, or vice versa), so this
# hook walks every `sys.modules` key equal to or under `tan.planner` and
# compares EACH ONE against its own parent's attribute, not just the
# top-level package against `tan`. Two different `tan.planner.models`
# modules -- at ANY depth -- means two different `OrchestratorError` classes
# alive in one interpreter, so a raise from one and a `pytest.raises` against
# the other silently stop matching; the failure surfaces in whatever
# unrelated test happens to run next -- often in a different shard entirely,
# since `tests/commands/` and `tests/planner/` are `pytest-shard`-partitioned
# per test, and #943's own pair landed deterministically split across shards
# -- so the drift can go unnoticed for a full week until the unsharded canary
# (`unsharded-python-canary.yml`) catches it.
#
# A `pytest_runtest_teardown` HOOKWRAPPER, not an autouse fixture: an earlier
# draft used a plain `@pytest.fixture(autouse=True)` with its check after
# `yield`, and that produced a false positive on
# `tests/core/test_planner_root.py::test_rebinding_a_different_root_after_
# import_is_refused`, which deliberately does
# `monkeypatch.setitem(sys.modules, "tan.planner", object())` as its OWN
# test technique (simulating "already imported" for a narrower check that
# reads only `sys.modules`, never the parent's attribute, by design). A
# fixture's post-yield code runs as one more finalizer in the SAME LIFO
# teardown chain as `monkeypatch`'s own restore, and fixture-instantiation
# order does not guarantee this fixture's finalizer runs after
# `monkeypatch`'s -- measured: it ran BEFORE, so this fixture observed
# `sys.modules["tan.planner"]` still holding that test's sentinel
# `object()` with the `.planner` attribute already back at its pre-test
# value, and flagged a "drift" that was really just an in-flight teardown a
# moment away from resolving itself correctly. A hookwrapper sidesteps the
# ordering question entirely: `pytest_runtest_teardown` wraps the ENTIRE
# teardown phase, including every fixture finalizer (`monkeypatch`'s
# among them), so `yield`ing past it and checking afterward is guaranteed
# to observe the fully-settled post-teardown state, not an intermediate one.
#
# The `isinstance(..., ModuleType)` guards below are a second, independent
# safety net for the same false-positive shape: they skip a comparison
# whenever either side is present but is not an actual module object (e.g.
# that same test's sentinel `object()`) -- a test is free to park an
# arbitrary non-module placeholder in `sys.modules["tan.planner"]` for its
# own purposes; the identity this check protects is specifically between
# TWO REAL module objects at the same name, which is the only shape
# tan-cli#943 actually manifested as (a genuine reimported module left on
# one side, the stale genuine module -- or nothing -- on the other).
#
# Latched PER NAME (`_PLANNER_DRIFT_ALREADY_REPORTED`, a `set` of already-
# reported `tan.planner`-or-under names): once a given name has been named,
# EVERY later test's teardown re-observes the same drifted pair for THAT
# name and would otherwise raise again -- measured on the real #943 shape
# with the guard un-latched, bound to `eb96112b`: `1057 passed, 15 skipped,
# 484 errors` for what is one root cause. The set makes it one clean error
# naming the polluting test, not 483 more burying it. A single process-wide
# boolean latch (the original shape) over-collapses: measured with two
# INDEPENDENT permanent leaks in one process (`tan.planner.kconfig` from one
# test, `tan.planner.slugs` from a second, unrelated one), a boolean latch
# reports only the first name and lets the second sail through as `passed`
# while it is a live polluter -- run alone, it reds. Keying the latch on the
# drifted NAME instead of a single flag still collapses the fan-out for the
# SAME name re-observed on every subsequent teardown, but a second, distinct
# drifted name is not shadowed by the first.
#
# Checked after every test in the WHOLE suite. The walk scans every
# `sys.modules` key (239 keys, unbound, measured) and filters down to
# whatever `tan.planner`-or-under names are currently live (23, measured --
# not "typically single digits") + one `getattr` per matching name --
# effectively free: 42.6 microseconds/teardown measured, ~0.25s total over
# 5885 tests; a synthetic 3000-teardown microbenchmark measured
# 10.88/11.12/11.32s with the hook installed vs. 10.22/10.29/10.34s without
# -- the delta is inside run-to-run noise.
#
# Mutation-proven, both the original one-level shape and the child shape a
# one-level check is blind to (tan-cli#943 review round 3):
#   * reverting `test_presets_command.py`'s parent-attribute restore to a
#     no-op turns THIS hook red on the very next test that imports
#     `tan.planner`, naming both mismatched objects (with their `hex(id())`)
#     instead of surfacing as a baffling `pytest.raises` mismatch three files
#     away;
#   * planting `monkeypatch.delitem(sys.modules, "tan.planner.kconfig")`
#     followed by a re-import, WITHOUT restoring `tan.planner`'s `.kconfig`
#     attribute, is silent under the one-level version of this hook (it only
#     ever compared `tan.planner` itself against `tan`'s `.planner`
#     attribute) -- `2 passed`, no teardown error -- and reds under the
#     generalised version above, which walks every `tan.planner`-or-under
#     name.
# Restoring the fix in both cases turns the hook green again. Verified NOT
# to false-positive on `test_rebinding_a_different_root_after_import_is_
# refused` (the sentinel case above), on `monkeypatch.setitem(sys.modules,
# "tan.planner", None)` (`test_net.py`, `test_monitor_command.py`,
# `test_diff_command.py`), on `monkeypatch.delitem(sys.modules, "tan")`, on
# `test_planner_root.py`'s other `object()` sentinel case, nor on a full
# unbound/bound run of `python/tests`.
_PLANNER_DRIFT_ALREADY_REPORTED: set[str] = set()


@pytest.hookimpl(wrapper=True)
def pytest_runtest_teardown(item, nextitem):
    result = yield
    names = sorted(
        n for n in sys.modules if n == "tan.planner" or n.startswith("tan.planner.")
    )
    for name in names:
        if name in _PLANNER_DRIFT_ALREADY_REPORTED:
            continue
        mod = sys.modules.get(name)
        parent_name, _, leaf = name.rpartition(".")
        parent = sys.modules.get(parent_name)
        if parent is None:
            # The parent package itself is absent from sys.modules (e.g. a
            # permanent `del sys.modules["tan.planner"]` with children left
            # behind) -- there is no attribute to compare `mod` against, so
            # treating a missing parent as `attr = None` would report a
            # bogus "two live copies" drift against `hex(id(None))` for what
            # is really just one copy and an absent parent. Not reachable
            # today (every `tan.planner*` mutation in this tree is
            # monkeypatch-based, so the wrapper always sees it restored),
            # but a future permanent parent teardown should skip, not
            # misdiagnose.
            continue
        attr = getattr(parent, leaf, None)
        if not (
            (mod is None or isinstance(mod, types.ModuleType))
            and (attr is None or isinstance(attr, types.ModuleType))
        ):
            continue
        if mod is None and attr is None:
            continue
        if mod is attr:
            continue
        _PLANNER_DRIFT_ALREADY_REPORTED.add(name)
        raise AssertionError(
            f"{name} drifted after {item.nodeid}: sys.modules[{name!r}] is "
            f"{mod!r} ({hex(id(mod))}) but the parent {parent_name!r}'s own "
            f".{leaf} attribute is {attr!r} ({hex(id(attr))}) -- these must "
            "be the SAME object. A test rebound one location (sys.modules "
            "or the parent's attribute) via monkeypatch without restoring "
            f"the other, exactly the tan-cli#943 shape: two live copies of "
            f"{name} (and of every class it defines) in one interpreter. "
            "Find the test named above and make it restore BOTH locations "
            "for every torn-out module, not just the top-level package."
        )
    return result


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
#   * SPAWNED `[sys.executable, "-m", "tan", ...]` -- 31 test files do this,
#     and a child resolves through its OWN sys.path, so asserting in this
#     process would not touch them. `PYTHONPATH` is prepended instead, which
#     is inherited by every child regardless of cwd or how the argv is built.
#
# The `__file__`-resolution assertion below is necessary but not sufficient
# (tan-cli#665): it proves the module object THIS process imported resolves
# under `repo_python`, and says nothing about whether `sys.executable` -- the
# interpreter every spawned child runs under -- can import `tan` at all. An
# executable missing a dependency, or with no `tan` on its path, passes the
# `__file__` check trivially (this process's own import already succeeded)
# and then fails in the spawned-child tests as unrelated-looking
# `ModuleNotFoundError` / non-zero-exit failures instead of one message here.
# The second probe below closes that gap by spawning `sys.executable`.
# ---------------------------------------------------------------------------
@pytest.fixture(scope="session", autouse=True)
def tan_under_test() -> None:
    """Refuse to run the suite against a `tan` from another tree.

    Session-scoped and autouse from the tree root, so it nets every consumer
    -- in-process import and spawned child alike -- present and future.

    Before deleting this because it looks "USELESS for tan-cli#903
    SPECIFICALLY" (it is -- see the long block comment ~870 lines above,
    right before `TAN_TEST_SKIP_HOME_PREFLIGHT` is defined): read that
    comment first. It stays the only spawn-probe still armed on the two
    paths that skip `pytest_configure`'s check entirely (an xdist WORKER
    process, and a session run with `TAN_TEST_SKIP_HOME_PREFLIGHT` set), and
    it is the only thing guarding tan-cli#423/#665 (wrong `tan` under test)
    on those two paths.
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

    # Second, independent probe (tan-cli#665): does `sys.executable` -- the
    # interpreter every spawned-child test runs under, via the PYTHONPATH
    # prepended above -- import `tan` at all? Not redundant with the
    # `__file__` assertion above: that one proves only that THIS process's
    # import resolved correctly.
    # `-m tan --version`, not `-c "import tan"`: `tan/__init__.py` is empty, so
    # a bare `import tan` succeeds on any interpreter the PYTHONPATH above
    # reaches, third-party dependencies or not. The children run `-m tan`,
    # which pulls `tan.__main__` and its typer/pydantic chain -- that is the
    # import that has to work.
    probe = subprocess.run(
        [sys.executable, "-m", "tan", "--version"],
        env=os.environ,
        capture_output=True,
        text=True,
    )
    assert probe.returncode == 0, (
        f"`{sys.executable} -m tan` failed (exit {probe.returncode}):\n"
        f"{probe.stderr}\n"
        "The spawned-child tests in this suite launch "
        f"[{sys.executable!r}, '-m', 'tan', ...], and that interpreter must "
        "be able to import tan for those to run at all -- when it can't, "
        "the failure shows up as unrelated-looking errors deep in the suite "
        "instead of one message here. Fix it by creating a "
        "venv for this interpreter, `pip install -e ./python` into it, and "
        "running pytest from that venv."
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
        # Image writer: probed by `flash_cmd`, absent from every runner.
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
