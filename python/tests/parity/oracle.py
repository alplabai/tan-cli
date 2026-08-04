# SPDX-License-Identifier: Apache-2.0
"""Run identical argv through the shipped Rust ``tan`` and the Python port and
diff them. Rust is authoritative: a divergence is a port bug until proven
otherwise, and Rust is retired for a capability only once this harness confirms
it.

Lives at ``python/tests/parity/`` -- NOT the repo-root ``tests/parity/``, which
belongs to the Rust workspace. The names overlap; the trees do not.

**Parity is defined PER SURFACE, and the scope is the load-bearing part.**
A whole-document diff of every command would go red for reasons that are not
port bugs, and a harness that is red for a non-bug gets muted -- so each case
declares the surface both binaries genuinely produce:

``ENVELOPE``
    Exit code plus the whole stdout envelope, key by key. Stdout that is not
    JSON degrades to a verbatim ``__raw__`` string compare, which is exactly the
    right assertion for a usage error: the contract is that stdout carries the
    envelope and NOTHING else, so "both wrote nothing" is a real check, not a
    weakened one. stderr is deliberately NOT compared -- it is clap's help
    renderer versus Typer/rich's, human text with no contract over it.

``VERSION``
    Exit code plus the *shape* of the version line. The literals differ BY
    DESIGN and permanently: the port declares ``0.5.0-rc3``
    (``python/tan/version.py``, which records why the port must not reuse the
    shipped number), the checked-out Rust declares ``0.4.1``
    (``Cargo.toml``). What the extension actually contracts on is the
    regex ``/^tan \\d+\\.\\d+\\.\\d+/``
    (alp-sdk-vscode/src/alpCli/service.ts:107-121), so that is what is compared
    -- and a side that fails the regex outright is reported even when both
    sides fail identically.

    Matched with ``fullmatch``, not ``match``. The extension's own regex is
    prefix-anchored, but this harness ALSO claims "stdout carries the envelope
    and NOTHING else", and a prefix match returns before ever reaching the raw
    compare that would enforce it. Prefix-anchoring here silently accepted
    ``"tan 0.5.0-dev\\nLEAKED EXTRA STDOUT LINE"`` and
    ``"tan 9.9.9 THIS IS NOT TAN AT ALL"`` as parity -- on the one case that
    actually runs today. Rust prints exactly ``tan 0.4.1`` (see
    :data:`PINNED_ORACLE_VERSION`, the one place that spelling is owned).

``PLAN``
    Exit code, the envelope shell, and -- inside ``data`` -- a NARROWED view of
    the plan. See ``narrow_plan`` before widening or trusting it: the exclusion
    is PROVISIONAL, and it is NOT justified by the Rust side failing to emit
    those keys.

    Rust emits every key. ``--plan --format json`` parses the SDK's raw JSON
    into a generic ``serde_json::Value`` and puts it in the envelope verbatim
    (``crates/tan-cli/src/commands/build/plan_modes.rs:256-268``), and Rust's own
    ``json_envelope_preserves_fields_the_typed_plan_does_not_model``
    (``plan_modes.rs:404-442``) asserts ``data.slices[0].appDir``,
    ``.sdkVersion`` and ``.sdkCommit`` are all PRESENT in the output. What
    ``BuildSlice`` models governs what Rust can *validate*, never what it emits.

    That test's ``raw_json`` is hand-authored to prove pass-through for an
    ARBITRARY unmodeled key, so read it for that claim only -- its per-slice
    placement of ``sdkVersion``/``sdkCommit`` matches no real plan. For the
    emit's actual shape use the six fixtures at
    ``tests/parity/oracle/*.build-plan.json``.

    The real divergence axis is SUBSTITUTION, not key presence -- and it does
    not line up with the modeled-key split at all:

    * On an UNTOKENED plan (``planPathMode`` absent, which is what the real
      fixture is; ``python/tan/core/plan_tokens.py`` makes substitution a no-op
      there) every key is byte-identical on both sides, so narrowing suppresses
      coverage of a divergence that does not exist.
    * On a TOKENED plan, Rust's ``--plan`` is unsubstituted -- substitution runs
      only on the ``--materialise`` branch (``plan_modes.rs:131-151``) -- while
      Python substitutes. The path-bearing keys then diverge whether or not the
      typed struct models them: ``buildDir``, ``command.args`` and ``env`` are
      all modeled and would all differ. Narrowing does not help.

    So this scope must be RE-DERIVED on the tokened/untokened axis when case 5
    is promoted. It stays narrowed only because no Python ``build`` command
    exists to compare yet.
"""
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import oracle_fixtures, oracle_provenance

#: ``python/`` -- pinned onto the subprocess ``PYTHONPATH`` so ``python -m tan``
#: resolves from a scratch cwd without a ``pip install``, mirroring the
#: conformance harness.
PACKAGE_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = PACKAGE_ROOT.parent

_EXE = ".exe" if sys.platform == "win32" else ""

#: The extension's acceptance probe, tightened to the WHOLE of stdout: anchored
#: at both ends and applied with ``fullmatch``, so a shape-valid first line
#: cannot smuggle trailing output past the VERSION surface. ``\S*`` carries the
#: pre-release tail (``-dev``); a space after the patch number is a failure.
VERSION_RE = re.compile(r"tan \d+\.\d+\.\d+\S*\Z")

ENVELOPE = "envelope"
VERSION = "version"
PLAN = "plan"

#: Envelope fields that carry a filesystem path and so need separator
#: normalisation before a cross-platform diff. Verbatim from ``PATH_KEYS`` in
#: ``crates/tan-cli/tests/contract.rs`` (frozen) -- the same set
#: ``tests/conformance/test_contract_envelopes.py`` already mirrors for the
#: identical reason, stated there in that Rust source's own words: "CI runs
#: this suite on ubuntu/windows/macos runners, and
#: `PathBuf::to_string_lossy()` renders `./board.yaml` as `.\\board.yaml` on
#: Windows." A frozen fixture captured on Windows (``oracle_fixtures.
#: CAPTURE_PLATFORM``) carries exactly that native rendering in these
#: fields; a live run on a different platform renders the same value with
#: `/`. Normalising here is NOT the blanket separator-flattening a case like
#: ``test_clean_parity.py`` correctly refuses (see that module's own
#: platform gate) -- it is scoped to the exact keys the frozen Rust contract
#: test already treats as separator-INSENSITIVE, so it cannot launder a
#: real divergence sitting in an unlisted field (``issues[].message``, or
#: `clean`'s own ``buildRoot`` -- deliberately absent from both this set and
#: the Rust one).
PATH_KEYS = frozenset(
    {
        "root",
        "boardYaml",
        "boardYamlPath",
        "destination",
        "relativePath",
        "sdkPath",
        "sdkPinned",
        "written",
        "unchanged",
        "launchJsonPath",
    }
)


def normalise_path_separators(value: Any, key: str | None = None) -> Any:
    """Recursively ``\\`` -> ``/`` on :data:`PATH_KEYS` fields only -- see
    that constant's own docstring. ``key`` is the enclosing object field
    name; a list inherits its own key, so every string in ``written: [...]``
    is still recognised."""
    if isinstance(value, str):
        return value.replace("\\", "/") if key in PATH_KEYS else value
    if isinstance(value, list):
        return [normalise_path_separators(item, key) for item in value]
    if isinstance(value, dict):
        return {k: normalise_path_separators(v, k) for k, v in value.items()}
    return value

#: Top-level build-plan keys retained by ``narrow_plan``. NOT "the keys Rust
#: emits" -- Rust emits the SDK's JSON verbatim (see the module docstring) --
#: but the keys the Rust side also *models*, which is all the current narrowing
#: has ever meant. ``sdkVersion``/``sdkCommit`` are kept because they are plain
#: version strings: never path-bearing, never touched by substitution, carried
#: verbatim by both sides from the same emit. They could not produce a false
#: red, so excluding them was pure lost coverage on exactly the fields a
#: version-skew guard cares about. Grounded in the six real plans at
#: ``tests/parity/oracle/*.build-plan.json``: every one carries both keys, and
#: carries them HERE, at top level.
RUST_PLAN_KEYS = frozenset(
    {
        "schemaVersion",
        "generatedBy",
        "boardYaml",
        "sku",
        "buildRoot",
        "slices",
        "sharedArtefacts",
        "warnings",
        "executionPolicy",
        "planPathMode",
        "sdkVersion",
        "sdkCommit",
    }
)

#: Per-slice keys retained -- the keys ``BuildSlice``
#: (``crates/tan-core/src/build_plan.rs:138-164``) and Python's ``Slice``
#: (``python/tan/core/build_plan.py:39-51``) BOTH model. No ``sdkVersion`` or
#: ``sdkCommit``: those live at top level in all six real plans
#: (``tests/parity/oracle/*.build-plan.json``) and appear in no slice of any of
#: them. The slice-shaped ``sdkVersion`` in ``plan_modes.rs:404-442`` is a
#: hand-authored string inside a unit test demonstrating pass-through for an
#: arbitrary unmodeled key -- illustrative, not representative of the emit.
#:
#: The four absentees (``appDir``, ``toolchain``, ``artifacts``, ``debug``) are
#: excluded PROVISIONALLY, pending case-5 promotion. The exclusion is NOT
#: because Rust drops them; Rust's own test asserts it emits them. It is a
#: holding position until the scope is re-derived on the tokened/untokened axis
#: -- the axis that actually separates the two implementations.
RUST_SLICE_KEYS = frozenset(
    {"coreId", "backend", "buildDir", "configArtefacts", "command", "env", "envAppendPath"}
)


@dataclass(frozen=True)
class ParityResult:
    matches: bool
    diffs: list[str]


#: The exact ``--version`` line the whole parity suite is measured against.
#: Not ``0.4.1-dev``: that spelling is this repo's OLDER PROSE for the RUST
#: oracle, from before ``Cargo.toml`` settled on ``version = "0.4.1"``, which
#: is why the binary prints no suffix today. It has never been the PORT's
#: string -- ``python/tan/version.py`` declares ``TAN_VERSION = "0.5.0-rc3"``.
#: The checked-out Rust binary's actual stdout, byte for byte, is
#: ``tan 0.4.1`` with no suffix (verified directly: ``tan --version | cat -A``
#: -> ``tan 0.4.1$``). Shared here, not owned by any one test module, because
#: ``conftest.py``'s session-scoped ``pinned_oracle`` fixture checks against
#: it for every file under ``tests/parity/``, not just the module that used to
#: define it alone.
PINNED_ORACLE_VERSION = "tan 0.4.1"

#: The `crates/` commit the oracle must have been built from -- the HALF of the
#: pin `--version` cannot express (tan-cli#406).
#:
#: `tan --version` is a pure function of `Cargo.toml`'s `[workspace.package]`
#: version, which moves only at release time, so it is BLIND to every change
#: within a release: measured, 15 commits changed the binary's build inputs
#: between `c5dedc1` ("release: v0.4.1") and `ac79d4c` without touching that
#: line, two of them envelope-visible (`bb13283` `project.boardYaml`,
#: `a30adaf` the flash TBD placeholder), and binaries built from both ends
#: printed the IDENTICAL `tan 0.4.1` while comparing DIFFERENT. Measured
#: consequence: the stale end fails three cases with a `project.boardYaml`
#: assertion diff that reads as a port regression -- the apparent fix being to
#: re-introduce the exact bug `bb13283` removed -- and the converse, an
#: envelope regression certified against a baseline that already contains it,
#: is the one that ships.
#:
#: Spelled as the newest commit touching a BUILD INPUT (see
#: :data:`oracle_provenance.BUILD_INPUT_PATHSPEC`), not `git log -1 --
#: crates/`: the last commit over the whole directory is `8a71310`, which
#: edited two crate-level `README.md` files and cannot change a byte of the
#: binary. Recompute with ``git log -1 --format=%H -- 'crates/*/src/*'
#: 'crates/*/Cargo.toml' Cargo.toml Cargo.lock``.
PINNED_ORACLE_CRATES_COMMIT = "ac79d4c7ffe00e43f26f3b7c6265436afbff5b0e"

#: The CONTENT of the build inputs the frozen fixtures were captured against,
#: as `sha256(git ls-files -s -- <BUILD_INPUT_PATHSPEC>)`. This is the pin the
#: freshness gate asserts on; :data:`PINNED_ORACLE_CRATES_COMMIT` above stays
#: as the human-readable "which commit was that" and backs the sidecar /
#: `$TAN_ORACLE_COMMIT` declaration path. Content, not a SHA, because a SHA
#: cannot survive a PR merge ref, a rebase, a cherry-pick or a squash --
#: none of which change a byte the compiler reads (tan-cli#414).
PINNED_ORACLE_BUILD_INPUT_DIGEST = (
    "d3dc3b8bbb24d2cd05ab5944e0438ac7490828f57de11288366260d45c99f6a0"
)


def build_output_root() -> Path:
    """``<repo>/target`` -- cargo's output tree, and the only place a binary
    can be assumed to have come from THIS checkout. Read through
    :data:`REPO_ROOT` at call time so a test that repoints the root moves this
    with it."""
    return REPO_ROOT / "target"


def oracle_provenance_drift(binary: str) -> str | None:
    """Why *binary* cannot be certified as the pinned oracle, or ``None``.

    The checker itself lives in :mod:`oracle_provenance` and takes every root
    and pin as an argument; this is where THIS checkout's values are bound to
    it, which is the one thing that has to stay here -- ``oracle.py`` owns the
    pins, and a test that repoints :data:`REPO_ROOT` must move the check with
    it.
    """
    return oracle_provenance.drift(
        binary,
        repo_root=REPO_ROOT,
        build_output=build_output_root(),
        pinned_commit=PINNED_ORACLE_CRATES_COMMIT,
        pinned_version=PINNED_ORACLE_VERSION,
    )


def _certified(binary: str) -> str:
    """*binary*, or a raise naming the drift. The raise is what turns a
    stale-oracle run from three `project.boardYaml` assertion diffs into one
    message that says "rebuild the oracle"."""
    drift = oracle_provenance_drift(binary)
    if drift is not None:
        raise RuntimeError(drift)
    return binary


def rust_binary() -> str | None:
    """``TAN_RUST_BINARY`` if set, else the MOST RECENTLY BUILT of
    ``target/{release,debug}/tan``. Returns ``None`` only when NOBODY named an
    oracle and none was built, so the caller can skip rather than invent a
    comparison.

    A set-but-missing ``TAN_RUST_BINARY`` RAISES instead. It must not fall back
    to some other binary -- an operator who named one should not silently get a
    different one -- but it must not skip either: a typo'd path in CI would then
    produce an all-skip, all-green run that certifies nothing, which is the
    exact failure mode this harness exists to prevent.

    When both profiles exist, the choice is by ``st_mtime``, not a fixed
    release-over-debug preference (what this function used to do,
    unconditionally). That preference used to silently pick a STALE binary: a
    ``target/release/tan`` left over from a much earlier build (measured --
    ``tan 0.1.1``, weeks old) sitting next to a freshly rebuilt
    ``target/debug/tan`` (``tan 0.4.1``) always won, because ``release`` was
    tried first regardless of either file's age, and every unpinned caller of
    this function silently measured against the wrong oracle with no signal
    that anything was off. mtime is what an ordinary edit-build-test loop
    actually implies: whichever profile was rebuilt LAST is the one a
    developer (or CI job that just ran ``cargo build``) intends to test
    against right now. A caller that legitimately wants one profile over the
    other regardless of freshness should set ``TAN_RUST_BINARY`` explicitly
    rather than rely on this default.

    Whatever is resolved is then run past :func:`oracle_provenance_drift`,
    which RAISES on a binary that cannot be tied to
    :data:`PINNED_ORACLE_CRATES_COMMIT` (tan-cli#406). mtime ranks BUILD time
    and says nothing about the SOURCE TREE a build came from, so the rule
    above -- correct as far as it goes -- happily elects a freshly rebuilt
    binary from a months-old checkout, and ``--version`` then certifies it,
    because that string cannot change within a release.

    A TIE (identical ``st_mtime`` on both profiles) is refused outright rather
    than resolved silently -- silently is what this function used to do, and
    it always broke to ``target/release``, which is exactly the stale-pick bug
    the mtime rule above replaced. A CI cache restore, ``cp -p``, rsync, or an
    artifact download can equalise (or invert) mtimes on ``target/`` with no
    rebuild involved, so a tie is a real, reachable way to reinstate that bug
    for every caller that does not separately assert the resolved version (see
    ``conftest.py``'s ``pinned_oracle``, which is that safety net -- this raise
    is the thing it nets).
    """
    override = os.environ.get("TAN_RUST_BINARY")
    if override:
        if not Path(override).exists():
            raise RuntimeError(
                f"TAN_RUST_BINARY={override!r} does not exist. Refusing to skip: "
                "a named-but-missing oracle would make this suite pass vacuously. "
                "Fix the path, or unset it to fall back to target/{release,debug}."
            )
        return _certified(override)
    candidates = [
        build_output_root() / profile / f"tan{_EXE}" for profile in ("release", "debug")
    ]
    existing = [c for c in candidates if c.exists()]
    if not existing:
        return None
    newest_mtime = max(c.stat().st_mtime for c in existing)
    tied = [c for c in existing if c.stat().st_mtime == newest_mtime]
    if len(tied) > 1:
        raise RuntimeError(
            "target/release/tan and target/debug/tan report the IDENTICAL "
            f"mtime ({newest_mtime!r}): {', '.join(str(c) for c in tied)}. "
            "Refusing to pick one silently -- a tie used to break to "
            "target/release unconditionally, which is the exact stale-binary "
            "bug the mtime rule this function now uses was written to "
            "replace. Rebuild one of the two profiles to break the tie, or "
            "set TAN_RUST_BINARY=<path> to the one you mean."
        )
    return _certified(str(tied[0]))


def resolve_oracle_for_skipif() -> str | None:
    """:func:`rust_binary`, but safe to call at MODULE IMPORT time.

    Test modules outside ``tests/parity/`` bind their oracle at import
    (``_ORACLE = ...`` feeding a ``pytest.mark.skipif``), and ``rust_binary``
    deliberately RAISES on three conditions -- an mtime TIE between
    ``target/{release,debug}``, a set-but-missing ``TAN_RUST_BINARY``, and an
    oracle that cannot be tied to :data:`PINNED_ORACLE_CRATES_COMMIT`
    (:func:`oracle_provenance_drift`, tan-cli#406). A
    raise while a test module is being imported is a COLLECTION error, which
    aborts the entire pytest session rather than the file that asked; that is
    exactly why ``pinned_oracle`` resolves inside its fixture body and not at
    its conftest's import (see that fixture's docstring, and the measured
    ``collected 0 items ... rc=4`` it records).

    Swallowing the raise here would normally trade a loud abort for a SILENT
    SKIP, which is worse and is the shape of the bug this whole area keeps
    reproducing. It does not, because the session-scoped autouse
    ``pinned_oracle`` in ``python/tests/conftest.py`` calls ``rust_binary()``
    itself: whichever condition raised here raises there too, one fixture
    later, and fails the run with the real message. So the deferral costs a
    slightly later error and buys a session that still collects.

    Returning ``None`` therefore means one of: nothing was built and nobody
    named one (the ordinary skip), or a condition ``pinned_oracle`` is about to
    fail the session over anyway.
    """
    try:
        return rust_binary()
    except RuntimeError:
        return None


def missing_for_live(rust: str | None) -> bool:
    """Whether a test must SKIP for lack of an oracle binary.

    Frozen replay (the default -- ``oracle_fixtures.LIVE`` unset) never needs
    one at all, so this is false there regardless of ``rust``: a case with no
    committed fixture fails loudly inside :func:`oracle_fixtures.resolve`
    instead, which is the point (tan-cli#272) -- a quiet skip here would hide
    exactly the gap that function exists to surface. Only an explicit
    ``TAN_PARITY_LIVE=1`` run, asked to spawn a binary that is not there,
    skips.
    """
    return oracle_fixtures.LIVE and rust is None


def empty_tool_inventory(scratch: Path) -> str:
    """A ``PATH`` value that resolves NOTHING except ``which`` itself -- the
    general form of ``test_flash_oracle_parity.py``'s ``_pin_tool_inventory``
    for a case whose frozen fixture answer is "no such tool anywhere on PATH",
    not "found this specific stand-in". Any ``shutil.which``/
    ``doctor_cmd.on_path`` probe run against this directory reports every
    PROBED name absent -- ``west`` for ``test_west_forward_matches_rust``
    today, ``git``/``cmake``/``ninja``/``python3``/``xz``/``wget`` for
    ``support-bundle.hostPrerequisites``, and any other which()-gated case's
    absent-tool branch tomorrow -- matching whatever tool inventory a
    fixture's capture host happened to lack (tan-cli#313, tan-cli#324),
    without inventing a second bespoke per-file helper for each one that
    comes up.

    Seeds the directory with exactly one file: a symlink to the REAL
    ``which`` this host resolves on its own PATH, POSIX only (a no-op on
    Windows, whose probe -- ``crate::util::windows_path_lookup`` -- walks
    ``%PATH%`` by hand and never spawns an external ``which`` at all). A
    directory that is genuinely, literally empty is NOT a clean "nothing on
    PATH" answer on POSIX: the oracle's own probe
    (``crates/tan-cli/src/util.rs:35-50``, ``command_on_path``) resolves a
    tool by SPAWNING ``which <tool>`` as a subprocess, and that spawn itself
    has to find ``which`` via this SAME (oracle-controlled) PATH. An empty
    directory can't resolve ``which`` either, so the probe fails before it
    ever answers the question asked -- measured directly: a PATH holding
    every one of ``support-bundle``'s six required tools but NOT ``which``
    still reports all six missing, and copying a working ``which`` in
    (touching nothing else) makes that same warning vanish entirely. A
    literally-empty directory's "everything missing" answer was an artefact
    of the unresolvable ``which`` spawn, not a real absence measurement --
    see ``_DEFERRED_VERBS``'s own comment for what the pinned
    ``support-bundle`` answer means now that this seeds a working ``which``:
    a GENUINE probe that ran and found nothing, not a degenerate one that
    could not run at all.

    REPLACES ``PATH`` outright rather than prepending, for the identical
    reason ``_pin_tool_inventory`` does: prepending would still let a REAL
    tool further down the replay host's own PATH be found, which is exactly
    the host-dependence this exists to remove.

    Hand the result to :func:`compare`'s ``python_env_overrides`` -- e.g.
    ``{"PATH": empty_tool_inventory(tmp_path)}`` -- for a FROZEN-REPLAY
    caller, and never fabricate the equivalent for the rust side THERE: the
    frozen answer is a recorded fixture, and pinning only the python side's
    PATH is exactly right in that mode (see that parameter's own docstring
    for why forwarding the same pin to the rust side under
    ``TAN_PARITY_LIVE=1`` is not a safe substitute -- the whole tan-cli#313/
    #324 bug was one side pinned and the other left to whatever happened to
    be installed). That ban is scoped to ``compare()``'s frozen-replay path --
    it does not reach a case that spawns BOTH binaries live on every run
    instead (e.g. ``test_deferred_verb_is_a_known_divergence_from_the_
    oracle``): there, applying this SAME value to both sides' subprocess
    environments is sound, because it is one identical override applied
    twice, symmetrically, not a pin smuggled onto one side only.
    """
    stub_dir = scratch / "empty-path"
    stub_dir.mkdir(exist_ok=True)
    if sys.platform != "win32":
        # The seed is NOT optional. A literally-empty PATH makes the oracle's
        # POSIX probe unable to resolve `which` ITSELF, so its tool report goes
        # degenerate: it names every tool missing because it could not look,
        # not because they are absent. A caller pinning that answer measures a
        # broken probe. Refuse rather than silently return to it (tan-cli#313,
        # tan-cli#324 -- the same silent-degradation class both closed).
        real_which = shutil.which("which")
        if real_which is None:
            raise RuntimeError(
                "empty_tool_inventory() cannot seed `which` into the stub PATH: "
                "shutil.which('which') returned None on this POSIX host. Without "
                "it the oracle's tool probe cannot run at all and reports every "
                "tool missing for the wrong reason -- refusing rather than "
                "pinning that artefact."
            )
        link = stub_dir / "which"
        if not link.exists():
            os.symlink(real_which, link)
        assert link.exists(), f"failed to seed `which` into {stub_dir}"
    return str(stub_dir)


def python_command() -> list[str]:
    """The port under test. Defaults to the source tree so the harness runs
    without a packaging step; ``TAN_PYTHON_BINARY`` points it at the PyInstaller
    artifact instead (``python/scripts/build_binary.sh``)."""
    packaged = os.environ.get("TAN_PYTHON_BINARY")
    return [packaged] if packaged else [sys.executable, "-m", "tan"]


def _env(home: Path) -> dict[str, str]:
    """One environment, shared by both sides -- the whole point is that the only
    difference between the two runs is the implementation.

    ``HOME``/``USERPROFILE`` are redirected at a scratch directory for the same
    reason the conformance harness does it: a developer's real
    ``~/.alp/sdk-default`` would otherwise decide whether ``build``/``validate``
    find an SDK, making the result machine-dependent.
    """
    inherited = os.environ.get("PYTHONPATH")
    return {
        **os.environ,
        "SOURCE_DATE_EPOCH": "0",
        "HOME": str(home),
        "USERPROFILE": str(home),
        "PYTHONPATH": os.pathsep.join([str(PACKAGE_ROOT), *([inherited] if inherited else [])]),
    }


def _run(
    command: list[str],
    argv: list[str],
    cwd: Path,
    home: Path,
    *,
    env_overrides: dict[str, str] | None = None,
):
    """``env_overrides`` layers on top of :func:`_env`'s shared environment --
    e.g. pinning ``PATH`` so a tool-presence probe INSIDE the spawned process
    (``shutil.which``/``doctor_cmd.on_path``) cannot pick up whatever happens
    to be installed on whichever host is replaying this suite (tan-cli#313).
    Empty by default, so every existing caller is unaffected."""
    env = _env(home)
    if env_overrides:
        env = {**env, **env_overrides}
    proc = subprocess.run(
        [*command, *argv],
        capture_output=True,
        text=True,
        # Match the Rust harness's `String::from_utf8_lossy`; the platform
        # locale decoder would turn a non-UTF-8 byte into a harness CRASH
        # masquerading as a parity failure.
        encoding="utf-8",
        errors="replace",
        cwd=cwd,
        env=env,
    )
    try:
        payload = json.loads(proc.stdout)
    except ValueError:
        payload = {"__raw__": proc.stdout.strip()}
    if not isinstance(payload, dict):  # a bare JSON scalar/array on stdout
        payload = {"__raw__": proc.stdout.strip()}
    return proc.returncode, payload


def narrow_plan(payload: dict) -> dict:
    """Restrict a ``build --plan`` envelope's ``data`` to the retained plan keys.

    ``data`` is left WHOLE unless it actually looks like a plan -- a dict
    carrying ``slices``. Two guards, not one:

    * ``data: null`` is the no-SDK error path (Rust's ``plan_error_run`` emits
      ``Value::Null`` plus an issue); that envelope is fully comparable.
    * A dict with no ``slices`` key would otherwise narrow to ``{}`` on BOTH
      sides and compare vacuously equal no matter what it held --
      ``{"message": "plan A"}`` and ``{"message": "plan B"}`` alike. Not
      reachable today, but it goes live the moment case 5 is promoted, and a
      comparator that returns "equal" for two different documents is the one
      thing this harness must never do.

    See the module docstring for why the retained-key split is provisional.
    """
    data = payload.get("data")
    if not isinstance(data, dict) or "slices" not in data:
        return payload
    plan = {k: v for k, v in data.items() if k in RUST_PLAN_KEYS}
    if isinstance(plan.get("slices"), list):
        plan["slices"] = [
            {k: v for k, v in s.items() if k in RUST_SLICE_KEYS} if isinstance(s, dict) else s
            for s in plan["slices"]
        ]
    return {**payload, "data": plan}


def rust_run(
    argv: list[str], cwd: Path, home: Path, *, scrub_roots: tuple[Path | str, ...] | None = None
) -> tuple[int, dict]:
    """The rust side of a comparison, ALONE -- for a case whose assertions
    are not a symmetric :func:`compare` (a known, pinned divergence; a
    bespoke multi-step flow like ``init`` then ``generate``). Frozen by
    default, exactly like :func:`compare`.

    ``scrub_roots`` defaults to ``(cwd, home)``, the same pair every
    :func:`compare` caller scrubs -- pass ``()`` explicitly for a case whose
    assertions only ever touch small literal fields (an exit code, an issue
    code) that cannot contain a scratch path, to skip the pointless work.
    """
    roots = (cwd, home) if scrub_roots is None else scrub_roots

    def _live():
        rust = rust_binary()
        if rust is None:
            raise RuntimeError("no Rust tan to diff against; set TAN_RUST_BINARY")
        code, out = _run([rust], argv, cwd, home)
        return [code, oracle_fixtures.scrub(out, *roots)]

    code, out = oracle_fixtures.resolve(_live)
    return code, out


def compare(
    argv: list[str],
    cwd: Path,
    *,
    surface: str = ENVELOPE,
    home: Path | None = None,
    python: list[str] | None = None,
    extra_scrub_roots: tuple[Path | str, ...] = (),
    python_env_overrides: dict[str, str] | None = None,
) -> ParityResult:
    """Diff the two binaries on ``argv``, scoped to ``surface``.

    ``python`` overrides the command used for the port -- that seam is what lets
    the suite plant a known divergence and prove this comparator goes red.

    The rust side is frozen by default (tan-cli#272) -- see
    :func:`oracle_fixtures.resolve`. Both sides are scrubbed of ``cwd``/``home``
    (:func:`oracle_fixtures.scrub`) before comparing: a live run relies on
    both binaries sharing the identical scratch cwd to make an embedded
    absolute path byte-comparable at all, and a frozen fixture was captured
    from a DIFFERENT scratch cwd than whatever this replay is using, so the
    substitution is what keeps that comparison meaningful in both modes.

    ``extra_scrub_roots`` widens that same substitution for a case whose
    envelope can carry a THIRD absolute path neither ``cwd`` nor ``home``
    cover -- e.g. a checked-in fixture file (a ``--plan-from`` plan) that
    itself embeds the path of whatever alp-sdk checkout it was captured
    against. Empty by default, so every existing caller is unaffected.

    ``python_env_overrides`` (tan-cli#313) layers extra environment onto the
    PYTHON side's subprocess ONLY, never the rust side's -- and is REFUSED
    outright whenever ``oracle_fixtures.LIVE`` is set, rather than silently
    applied to one side only. In frozen replay (the default, no
    ``TAN_PARITY_LIVE``) the rust side never spawns anything at all, so
    pinning only the python side is exactly right: the frozen fixture IS the
    rust answer, already captured against a specific tool inventory, and the
    python side's live PATH probe needs pinning to match it, or the
    comparison depends on what happens to be installed on whoever runs the
    suite. See ``test_flash_oracle_parity.py``'s ``_pin_tool_inventory``.

    Under ``TAN_PARITY_LIVE=1`` both sides DO spawn, and ``_env``'s own
    invariant applies in full ("one environment, shared by both sides --
    the whole point is that the only difference between the two runs is the
    implementation"): applying the pin to python alone would silently break
    that invariant for exactly the pinned cases, and forwarding it to
    ``rust_run`` too is NOT a safe substitute -- measured against the real
    oracle (``target/debug/tan``), POSIX ``command_on_path``
    (``crates/tan-cli/src/util.rs:45-50``) resolves a tool by SPAWNING
    ``which`` as its own subprocess, which itself has to be found via the
    (now-pinned, ``which``-free) ``PATH`` -- so a PATH replaced with only a
    ``dd`` stand-in makes ``which`` itself unresolvable, every rust-side
    probe report "not found" including ``dd``, and the SAME pin that makes
    python answer "dd" makes rust answer "bmaptool" -- the opposite tool, on
    the identical override. So under ``TAN_PARITY_LIVE=1`` this parameter
    raises rather than comparing two binaries under two different (or two
    subtly-broken-in-opposite-directions) environments; the caller should
    drop the pin for a live run (both binaries then share this host's REAL
    tool inventory, which is the whole point of a live re-validation) and
    keep it only for frozen replay. ``None`` by default, so every existing
    caller is unaffected.
    """
    home = home or cwd
    roots = (cwd, home, *extra_scrub_roots)

    if oracle_fixtures.LIVE and python_env_overrides:
        raise RuntimeError(
            "compare() got python_env_overrides under TAN_PARITY_LIVE=1: both "
            "binaries spawn for real in this mode, and pinning only the "
            "python side's PATH breaks _env's 'one environment, shared by "
            "both sides' invariant -- see this parameter's own docstring for "
            "why forwarding the same pin to the rust side is not a safe fix "
            "either (tan-cli#313). Drop python_env_overrides for a live run, "
            "or drop TAN_PARITY_LIVE to replay the frozen fixture with it."
        )

    r_code, r_out = rust_run(argv, cwd, home, scrub_roots=roots)
    p_code, p_out = _run(
        python or python_command(), argv, cwd, home, env_overrides=python_env_overrides
    )
    p_out = oracle_fixtures.scrub(p_out, *roots)
    # Scoped to PATH_KEYS, on BOTH sides, before the diff -- see that
    # constant's own docstring. A no-op whenever the two sides already agree
    # (including every case on the capture platform itself, where both are
    # already native-Windows `\`), so this never masks a real divergence.
    r_out = normalise_path_separators(r_out)
    p_out = normalise_path_separators(p_out)
    # And, on a non-capture-platform replay only, the separators INSIDE any
    # path anchored at a `scrub` placeholder -- the redacted scratch root the
    # harness itself made. Anchored on the token rather than a key, so it also
    # reaches the `issues[].message`/`entries[].message` text that embeds one.
    # See that function for what this trades away, and where it does not.
    r_out = oracle_fixtures.normalise_scrubbed_path_separators(r_out)
    p_out = oracle_fixtures.normalise_scrubbed_path_separators(p_out)

    diffs: list[str] = []
    if r_code != p_code:
        diffs.append(f"exit code: rust={r_code} python={p_code}")

    if surface == VERSION:
        # The literals differ by design; the CONTRACT is the shape. Report a
        # side that fails the regex even when both fail the same way, so
        # identically-broken output cannot pass as parity. `fullmatch` covers
        # the WHOLE of stdout, not just its first line -- this branch returns
        # before the raw compare below, so it is the only thing standing
        # between a leaked stdout line and a green run.
        for name, out in (("rust", r_out), ("python", p_out)):
            line = out.get("__raw__", "")
            if not VERSION_RE.fullmatch(line):
                diffs.append(f"{name} --version is not exactly /{VERSION_RE.pattern}/: {line!r}")
        return ParityResult(not diffs, diffs)

    if surface == PLAN:
        r_out, p_out = narrow_plan(r_out), narrow_plan(p_out)

    for key in sorted(set(r_out) | set(p_out)):
        if r_out.get(key) != p_out.get(key):
            diffs.append(f"{key}: rust={r_out.get(key)!r} python={p_out.get(key)!r}")
    return ParityResult(not diffs, diffs)
