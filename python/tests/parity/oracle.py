# SPDX-License-Identifier: Apache-2.0
"""Run identical argv through the shipped Rust ``tan`` and the Python port and
diff them. Rust is authoritative: a divergence is a port bug until proven
otherwise, and Rust is retired for a capability only once this harness confirms
it.

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

``PLAN``
    Exit code, the envelope shell, and -- inside ``data`` -- only the keys the
    Rust side actually models. ``crates/tan-core/src/build_plan.rs``'s
    ``BuildSlice`` does not model ``appDir``, ``toolchain``, ``artifacts`` or
    ``debug``, and ``crates/tan-cli/src/commands/build/plan_modes.rs:237-262``
    says why: ``--plan --format json`` passes the SDK's **raw, unsubstituted**
    JSON straight through, because re-serialising the typed struct "would drop
    them and emit a schema-invalid plan". The Python port models and
    token-substitutes those keys because the plan schema requires them. Diffing
    the two whole documents therefore compares different things and always
    differs -- a Rust gap, not a port bug.

    Scoping was chosen over the alternative (adding ``app_dir`` et al to the
    Rust struct): this port does not touch ``crates/``, and the Rust side is the
    one that is behind. Consequence recorded honestly: the four unmodeled keys
    are UNCHECKED by this oracle, so they stay owned by
    ``tests/core/test_build_plan.py`` and by the alp-sdk plan schema.

    Second, separate divergence on this surface, for whoever promotes the case:
    Rust emits the plan unsubstituted here (substitution runs only on the
    ``--materialise`` path), so on a ``planPathMode: "tokened"`` plan even the
    MODELED keys -- ``buildDir``, ``command.args``, ``env`` -- differ, Rust
    carrying literal ``${SDK_ROOT}`` where Python carries the resolved path.
    That needs its own scoping decision at promotion time; it is not covered
    here, because no Python ``build`` command exists to compare yet.
"""
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

#: ``python/`` -- pinned onto the subprocess ``PYTHONPATH`` so ``python -m tan``
#: resolves from a scratch cwd without a ``pip install``, mirroring the
#: conformance harness.
PACKAGE_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = PACKAGE_ROOT.parent

_EXE = ".exe" if sys.platform == "win32" else ""

#: The extension's own acceptance probe for a ``tan`` binary.
VERSION_RE = re.compile(r"^tan \d+\.\d+\.\d+")

ENVELOPE = "envelope"
VERSION = "version"
PLAN = "plan"

#: Top-level build-plan keys ``BuildPlan`` models
#: (``crates/tan-core/src/build_plan.rs:208-253``). ``sdkVersion`` is absent on
#: purpose -- the Rust struct does not carry it.
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
        "sdkCommit",
    }
)

#: Per-slice keys ``BuildSlice`` models (``build_plan.rs:138-164``). The four
#: absentees -- ``appDir``, ``toolchain``, ``artifacts``, ``debug`` -- are the
#: documented Rust gap this scope exists for.
RUST_SLICE_KEYS = frozenset(
    {"coreId", "backend", "buildDir", "configArtefacts", "command", "env", "envAppendPath"}
)


@dataclass(frozen=True)
class ParityResult:
    matches: bool
    diffs: list[str]


def rust_binary() -> str | None:
    """``TAN_RUST_BINARY`` if set, else a build in the usual places. Returns
    ``None`` when there is no oracle to diff against, so the caller can skip
    rather than invent a comparison."""
    override = os.environ.get("TAN_RUST_BINARY")
    if override:
        return override if Path(override).exists() else None
    for profile in ("release", "debug"):
        candidate = REPO_ROOT / "target" / profile / f"tan{_EXE}"
        if candidate.exists():
            return str(candidate)
    return None


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
    """Restrict a ``build --plan`` envelope's ``data`` to the keys the Rust side
    models. Left alone when ``data`` is not a plan object (the no-SDK path emits
    ``data: null`` plus an issue, and that whole envelope IS comparable)."""
    data = payload.get("data")
    if not isinstance(data, dict):
        return payload
    plan = {k: v for k, v in data.items() if k in RUST_PLAN_KEYS}
    if isinstance(plan.get("slices"), list):
        plan["slices"] = [
            {k: v for k, v in s.items() if k in RUST_SLICE_KEYS} if isinstance(s, dict) else s
            for s in plan["slices"]
        ]
    return {**payload, "data": plan}


def compare(
    argv: list[str],
    cwd: Path,
    *,
    surface: str = ENVELOPE,
    home: Path | None = None,
    python: list[str] | None = None,
) -> ParityResult:
    """Diff the two binaries on ``argv``, scoped to ``surface``.

    ``python`` overrides the command used for the port -- that seam is what lets
    the suite plant a known divergence and prove this comparator goes red.
    """
    rust = rust_binary()
    if rust is None:
        raise RuntimeError("no Rust tan to diff against; set TAN_RUST_BINARY")
    home = home or cwd

    r_code, r_out = _run([rust], argv, cwd, home)
    p_code, p_out = _run(python or python_command(), argv, cwd, home)

    diffs: list[str] = []
    if r_code != p_code:
        diffs.append(f"exit code: rust={r_code} python={p_code}")

    if surface == VERSION:
        # The literals differ by design; the CONTRACT is the shape. Report a
        # side that fails the regex even when both fail the same way, so
        # identically-broken output cannot pass as parity.
        for name, out in (("rust", r_out), ("python", p_out)):
            line = out.get("__raw__", "")
            if not VERSION_RE.match(line):
                diffs.append(f"{name} --version does not match /^tan \\d+\\.\\d+\\.\\d+/: {line!r}")
        return ParityResult(not diffs, diffs)

    if surface == PLAN:
        r_out, p_out = narrow_plan(r_out), narrow_plan(p_out)

    for key in sorted(set(r_out) | set(p_out)):
        if r_out.get(key) != p_out.get(key):
            diffs.append(f"{key}: rust={r_out.get(key)!r} python={p_out.get(key)!r}")
    return ParityResult(not diffs, diffs)
