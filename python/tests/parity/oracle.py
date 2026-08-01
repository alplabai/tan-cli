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
    DESIGN and permanently: the port declares 0.5.0-dev, the checked-out Rust
    declares 0.4.1-dev (``python/tan/version.py`` records why the port must not
    reuse the shipped number). What the extension actually contracts on is the
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
    actually runs today. Rust prints exactly ``tan 0.4.1-dev``.

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
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from . import oracle_fixtures

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


def rust_binary() -> str | None:
    """``TAN_RUST_BINARY`` if set, else a build in the usual places. Returns
    ``None`` only when NOBODY named an oracle and none was built, so the caller
    can skip rather than invent a comparison.

    A set-but-missing ``TAN_RUST_BINARY`` RAISES instead. It must not fall back
    to some other binary -- an operator who named one should not silently get a
    different one -- but it must not skip either: a typo'd path in CI would then
    produce an all-skip, all-green run that certifies nothing, which is the
    exact failure mode this harness exists to prevent.
    """
    override = os.environ.get("TAN_RUST_BINARY")
    if override:
        if not Path(override).exists():
            raise RuntimeError(
                f"TAN_RUST_BINARY={override!r} does not exist. Refusing to skip: "
                "a named-but-missing oracle would make this suite pass vacuously. "
                "Fix the path, or unset it to fall back to target/{release,debug}."
            )
        return override
    for profile in ("release", "debug"):
        candidate = REPO_ROOT / "target" / profile / f"tan{_EXE}"
        if candidate.exists():
            return str(candidate)
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


def _run(command: list[str], argv: list[str], cwd: Path, home: Path):
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
        env=_env(home),
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
    """
    home = home or cwd
    roots = (cwd, home, *extra_scrub_roots)

    r_code, r_out = rust_run(argv, cwd, home, scrub_roots=roots)
    p_code, p_out = _run(python or python_command(), argv, cwd, home)
    p_out = oracle_fixtures.scrub(p_out, *roots)

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
