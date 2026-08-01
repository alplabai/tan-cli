#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""tan-cli#278 rc3: exercise a FROZEN `tan` binary on a genuinely clean host --
no alp-sdk checkout, no `~/.alp`, empty cwd -- and assert exit codes + envelope
self-consistency on the four commands the maintainer's escalation named:

    tan --version
    tan doctor --format json
    tan sdk list --online
    tan bootstrap --dry-run --format json     (--format json added here, to
                                                 read issues[].code; the issue's
                                                 own table runs it bare)

Why these four, and why a clean host: the issue's six-defect table. #304 (no
CA bundle in the freeze) only reproduces against a PyInstaller build, never a
developer's own interpreter -- no `pytest` run on any host can see it. #292/
#301 (an ambient `$ZEPHYR_BASE`, or a stale `~/.alp/sdk-default`, silently
adopted) need a host that starts with NONE of that state, then has exactly one
piece of it added back -- a fixture that already carries the state under test
would just prove the fixture works, which is #286's recorded failure mode: a
probe that passed 77 tests because the helper planted the same wrong layout
the probe looked for.

Two `$ZEPHYR_BASE` variants -- unset, and set to a directory that EXISTS but
carries no Zephyr markers of any kind -- cover #292/#301 without falling into
that trap: a bare, structurally empty directory cannot coincidentally match
whatever shape the code under test happens to look for. Applied only to
`doctor`/`bootstrap` (the two commands #292/#301 are actually about);
`--version` and `sdk list --online` do not read `$ZEPHYR_BASE`, so they run
once, to avoid doubling the one live GitHub API call in this file for zero
extra coverage.

Usage:
    python scripts/clean_host_smoke.py --tan <path-to-binary>
    python scripts/clean_host_smoke.py --selftest   # envelope logic only, no binary

Every problem found is collected and reported, not just the first -- same
reasoning as version_check.py's `check()`.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

#: `alp-sdk-vscode`'s own probe regex (vscodeAdapter.ts), verbatim.
VERSION_RE = re.compile(r"^tan \d+\.\d+\.\d+")
#: The envelope contract every `tan --format json` command owes
#: (`{command,ok,exitCode,project,data,issues}`).
ENVELOPE_KEYS = {"command", "ok", "exitCode", "project", "data", "issues"}
#: `doctor_cmd.Check.status`'s vocabulary, verbatim.
CHECK_STATUSES = {"pass", "warn", "fail", "unknown"}

DOCTOR_TIMEOUT_S = 60
BOOTSTRAP_TIMEOUT_S = 60
VERSION_TIMEOUT_S = 15
#: `sdk_cmd.NETWORK_TIMEOUT_SECONDS` is 20s inside tan itself; this must be
#: longer than that so a real network hang is reported as tan's own coded
#: `fetch-failed` envelope, not killed here first with no diagnostic at all.
SDK_LIST_TIMEOUT_S = 40


class Problem(Exception):
    """Raised only for a condition nothing downstream can usefully continue
    past (the binary would not even start). Every other failure is collected
    into a list and reported together -- see `main()`."""


# ---------------------------------------------------------------------------
# Pure envelope-logic assertions -- these take no subprocess, only parsed
# JSON, so `--selftest` can exercise them with zero binary and zero network.
# ---------------------------------------------------------------------------


def envelope_problems(envelope: dict[str, Any], command: str, actual_rc: int) -> list[str]:
    """Shape + self-consistency of any `tan ... --format json` envelope,
    independent of what command produced it."""
    problems: list[str] = []
    missing = ENVELOPE_KEYS - envelope.keys()
    if missing:
        problems.append(f"{command}: envelope missing key(s) {sorted(missing)}: {envelope!r}")
        return problems  # nothing else below is safe to read

    if not isinstance(envelope["ok"], bool):
        problems.append(f"{command}: 'ok' is not a bool: {envelope['ok']!r}")
    if not isinstance(envelope["exitCode"], int):
        problems.append(f"{command}: 'exitCode' is not an int: {envelope['exitCode']!r}")
        return problems
    if envelope["ok"] != (envelope["exitCode"] == 0):
        problems.append(
            f"{command}: ok={envelope['ok']!r} disagrees with exitCode="
            f"{envelope['exitCode']!r} (ok must be exactly exitCode==0)"
        )
    if envelope["exitCode"] != actual_rc:
        problems.append(
            f"{command}: envelope claims exitCode={envelope['exitCode']!r} but the "
            f"process itself exited {actual_rc} -- the envelope disagrees with its "
            f"own process"
        )
    if not isinstance(envelope.get("issues"), list):
        problems.append(f"{command}: 'issues' is not a list: {envelope.get('issues')!r}")
    return problems


#: doctor_cmd.py's OWN naming convention for "a second check that re-confirms
#: an already-named fact a different way", not a guess: the commit that added
#: `venvProvenance` describes it as living "right beside westResolved (same
#: resolved venv, tan.core.venv.find_workspace_venv)", and `westResolved`
#: itself re-confirms what the bare `west` check already answered. Stripping
#: either suffix off a check's name yields the bare-fact check it corroborates
#: (`westResolved` -> `west`, `sdkProvenance` -> `sdk`).
#:
#: Deliberately NOT "one name contains the other": that was tried first and
#: produced a real false positive on this exact port -- `sdk` (alp-sdk
#: resolution) matched inside `zephyrSdk`/`zephyrSdkAvailableForHost` (the
#: Zephyr TOOLCHAIN), an unrelated subject that legitimately disagrees on a
#: fresh host (nothing installed yet, but a release exists to install). A
#: substring rule cannot tell "sdk" (the whole subject) from "sdk" (a syllable
#: inside a longer, different subject's name); an exact suffix can.
_COROBORATION_SUFFIXES = ("Resolved", "Provenance")


def doctor_check_problems(data: dict[str, Any] | None, exit_code: int) -> list[str]:
    """`data.checks[]` internal self-consistency -- the #299 invariant off the
    issue table ("no check both passes and fails on the same subject"),
    generalised rather than grepped for the literal pair `westResolved`/
    `west`: any check whose name is `<subject><suffix>` for a suffix in
    `_COROBORATION_SUFFIXES`, alongside a check literally named `<subject>`,
    is a same-subject pair by this file's own convention, and future pairs
    following it (any `<x>Resolved`/`<x>Provenance` beside a bare `<x>`) are
    covered without editing this file.

    Stated rather than left implicit: a same-subject pair that does NOT
    follow this suffix convention (`hostPython` / `pythonFloor` is the real
    example doctor_cmd.py's own comments name -- neither is a suffixed form
    of the other) is NOT caught by this heuristic.

    Also checks the exit-code contract `exit_code_for` documents on itself
    ("never 0 on an unhealthy host"), read black-box off `checks[]` rather
    than re-imported from `doctor_cmd.py` -- this is a Python CI script
    checking a Python CLI's own output contract, not a second implementation
    of its logic.
    """
    if data is None:
        return ["doctor: data is null -- no checks to validate"]
    checks = data.get("checks")
    if not isinstance(checks, list) or not checks:
        return ["doctor: data.checks is missing or empty"]

    problems: list[str] = []
    seen: dict[str, str] = {}
    for c in checks:
        name, status = c.get("name"), c.get("status")
        if not isinstance(name, str) or status not in CHECK_STATUSES:
            problems.append(f"doctor: malformed check entry {c!r}")
            continue
        if name in seen:
            problems.append(
                f"doctor: duplicate check name {name!r} (statuses {seen[name]!r} "
                f"and {status!r})"
            )
            continue
        seen[name] = status

    for name, status in seen.items():
        for suffix in _COROBORATION_SUFFIXES:
            if not name.endswith(suffix) or len(name) == len(suffix):
                continue
            subject = name[: -len(suffix)]
            subject_status = seen.get(subject)
            if subject_status is not None and {status, subject_status} == {"pass", "fail"}:
                problems.append(
                    f"doctor: {subject!r}={subject_status} and {name!r}={status} -- "
                    f"same-subject pass/fail contradiction (tan-cli#299 shape)"
                )

    any_fail = any(s == "fail" for s in seen.values())
    if (exit_code == 0) == any_fail:
        problems.append(
            f"doctor: exitCode={exit_code} but checks[] "
            f"{'has a fail' if any_fail else 'has no fail'} -- exit_code_for's own "
            f"contract ('never 0 on an unhealthy host') is violated"
        )
    return problems


def bootstrap_problems(
    envelope: dict[str, Any],
    actual_rc: int,
    *,
    expected_exit: int = 2,
    expected_code: str = "bootstrap.sdk-root-unresolved",
) -> list[str]:
    problems = envelope_problems(envelope, "bootstrap --dry-run", actual_rc)
    if envelope.get("exitCode") != expected_exit:
        problems.append(
            f"bootstrap --dry-run: exitCode={envelope.get('exitCode')!r}, expected "
            f"{expected_exit} (no alp-sdk present anywhere on this host)"
        )
    codes = [i.get("code") for i in envelope.get("issues", []) if isinstance(i, dict)]
    if expected_code not in codes:
        problems.append(
            f"bootstrap --dry-run: issues[].code does not include {expected_code!r}; "
            f"got {codes!r}"
        )
    return problems


# ---------------------------------------------------------------------------
# Process plumbing
# ---------------------------------------------------------------------------


def run(argv: list[str], *, cwd: Path, env: dict[str, str], timeout: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )


def parse_envelope(proc: subprocess.CompletedProcess[str], command: str) -> dict[str, Any]:
    try:
        return json.loads(proc.stdout)
    except ValueError as err:
        raise Problem(
            f"{command}: stdout is not valid JSON ({err}). "
            f"rc={proc.returncode}\n--- stdout ---\n{proc.stdout}\n"
            f"--- stderr ---\n{proc.stderr}"
        ) from err


# ---------------------------------------------------------------------------
# The four assertions
# ---------------------------------------------------------------------------


def assert_version(tan: Path, cwd: Path, env: dict[str, str]) -> list[str]:
    proc = run([str(tan), "--version"], cwd=cwd, env=env, timeout=VERSION_TIMEOUT_S)
    if proc.returncode != 0:
        return [f"--version: exited {proc.returncode}, expected 0 (stderr: {proc.stderr!r})"]
    first_line = (proc.stdout.splitlines() or [""])[0]
    if not VERSION_RE.match(first_line):
        return [f"--version: {first_line!r} does not match {VERSION_RE.pattern!r}"]
    return []


def assert_sdk_list_online(
    tan: Path, cwd: Path, env: dict[str, str], *, retries: int, retry_delay_s: float
) -> list[str]:
    """`sdk list --online` -- the CA-trust canary (#304). Calls the REAL
    GitHub API; deliberately not mocked (a mock stops being a CA-trust check
    and starts being a check on the mock). `tan` itself has no token/auth
    knob for this call (`sdk_cmd._fetch_releases` sends no Authorization
    header at all), so this cannot be authenticated from the workflow side --
    only retried against the anonymous per-IP rate limit, and it must still
    fail loudly, not skip, if every attempt comes back bad.
    """
    last: subprocess.CompletedProcess[str] | None = None
    for attempt in range(1, retries + 1):
        last = run([str(tan), "sdk", "list", "--online"], cwd=cwd, env=env, timeout=SDK_LIST_TIMEOUT_S)
        if last.returncode == 0:
            return []
        if attempt < retries:
            time.sleep(retry_delay_s * attempt)
    assert last is not None
    hint = (
        " (looks like GitHub API rate limiting, not a CA/TLS failure)"
        if "rate limit" in (last.stdout + last.stderr).lower()
        else ""
    )
    return [
        f"sdk list --online: exited {last.returncode} after {retries} attempt(s){hint}. "
        f"stdout={last.stdout!r} stderr={last.stderr!r}"
    ]


def assert_doctor(tan: Path, cwd: Path, env: dict[str, str], *, label: str) -> list[str]:
    proc = run([str(tan), "doctor", "--format", "json"], cwd=cwd, env=env, timeout=DOCTOR_TIMEOUT_S)
    envelope = parse_envelope(proc, f"doctor [{label}]")
    problems = envelope_problems(envelope, f"doctor [{label}]", proc.returncode)
    problems += [f"[{label}] {p}" for p in doctor_check_problems(envelope.get("data"), proc.returncode)]
    return problems


def assert_bootstrap(tan: Path, cwd: Path, env: dict[str, str], *, label: str) -> list[str]:
    proc = run(
        [str(tan), "bootstrap", "--dry-run", "--format", "json"],
        cwd=cwd,
        env=env,
        timeout=BOOTSTRAP_TIMEOUT_S,
    )
    envelope = parse_envelope(proc, f"bootstrap --dry-run [{label}]")
    return [f"[{label}] {p}" for p in bootstrap_problems(envelope, proc.returncode)]


# ---------------------------------------------------------------------------
# selftest -- proves the assertions themselves catch a real pre-fix shape,
# with no binary and no network. Run in every CI invocation of this file
# (`--selftest`) as a cheap, always-on guard on the checker logic, and stands
# alone as tan-cli#278's "show the assertion going red" evidence: the
# `bug_299` fixture below is the exact shape the issue's own table describes
# for tan-cli#299 ("`west` fails while `westResolved` passes in the same
# report").
# ---------------------------------------------------------------------------


def selftest() -> None:
    healthy = {
        "command": "doctor",
        "ok": False,
        "exitCode": 4,
        "project": None,
        "issues": [],
        "data": {
            "checks": [
                {"name": "westResolved", "status": "pass", "detail": "west resolved via venv"},
                {"name": "sdk", "status": "fail", "detail": "no alp-sdk resolvable"},
            ]
        },
    }
    assert envelope_problems(healthy, "doctor", 4) == [], envelope_problems(healthy, "doctor", 4)
    assert doctor_check_problems(healthy["data"], 4) == [], doctor_check_problems(healthy["data"], 4)

    # The tan-cli#299 shape, verbatim from the issue's own defect table: west
    # fails while westResolved passes in the SAME report.
    bug_299 = {
        "checks": [
            {"name": "westResolved", "status": "pass", "detail": "west resolved via venv"},
            {"name": "west", "status": "fail", "detail": "west not on bare PATH"},
        ]
    }
    caught = doctor_check_problems(bug_299, 4)
    print("tan-cli#299 fixture (west=fail, westResolved=pass) --> caught:")
    for p in caught:
        print("  " + p)
    assert any("westResolved" in p and "'west'" in p for p in caught), caught

    # ok/exitCode self-disagreement.
    bad_ok = {**healthy, "ok": True, "exitCode": 4}
    assert envelope_problems(bad_ok, "doctor", 4) != []

    # The envelope claiming an exit code the process did not actually return.
    assert envelope_problems(healthy, "doctor", 0) != []

    # bootstrap: the real clean-host refusal shape must pass...
    good_bootstrap = {
        "command": "bootstrap",
        "ok": False,
        "exitCode": 2,
        "project": None,
        "data": None,
        "issues": [
            {"code": "bootstrap.sdk-root-unresolved", "severity": "error", "message": "no SDK"}
        ],
    }
    assert bootstrap_problems(good_bootstrap, 2) == []
    # ...and a report missing the specific code must not.
    wrong_code = {**good_bootstrap, "issues": [{"code": "bootstrap.manifest", "severity": "error", "message": "x"}]}
    assert bootstrap_problems(wrong_code, 2) != []

    print("selftest: OK")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def clean_env(home: Path, *, zephyr_base: Path | None) -> dict[str, str]:
    """Copy the real environment (Windows subprocess launch needs SystemRoot
    etc., and stripping to an allowlist risks breaking the child for reasons
    unrelated to what this test targets) and override only what a clean host
    must not carry: `HOME`/`USERPROFILE` point at a throwaway directory with
    no `.alp`, and `ZEPHYR_BASE` is set or removed per the variant under test.
    """
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)
    if zephyr_base is None:
        env.pop("ZEPHYR_BASE", None)
    else:
        env["ZEPHYR_BASE"] = str(zephyr_base)
    return env


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tan", metavar="PATH", help="path to the frozen tan binary")
    parser.add_argument("--selftest", action="store_true", help="check the assertion logic only")
    parser.add_argument("--retries", type=int, default=3, help="sdk list --online attempts")
    parser.add_argument("--retry-delay", type=float, default=5.0, help="seconds, multiplied by attempt")
    args = parser.parse_args(argv)

    if args.selftest:
        selftest()
        return 0

    if not args.tan:
        parser.error("--tan is required (or pass --selftest)")
    tan = Path(args.tan).resolve()
    if not tan.is_file():
        print(f"::error::no such file: {tan}", file=sys.stderr)
        return 1
    if os.name != "nt" and not os.access(tan, os.X_OK):
        print(f"::error::{tan} is not executable", file=sys.stderr)
        return 1

    home_dir = Path(tempfile.mkdtemp(prefix="tan-clean-home-"))
    cwd_dir = Path(tempfile.mkdtemp(prefix="tan-clean-cwd-"))
    # Deliberately bare: no cmake/modules/python.cmake, no .west/, nothing --
    # see the module docstring for why a lookalike fixture would be the wrong
    # test here.
    bare_zephyr_base = Path(tempfile.mkdtemp(prefix="tan-clean-zephyr-base-"))

    problems: list[str] = []
    try:
        print(f"::group::clean host fixture -- HOME={home_dir} CWD={cwd_dir}")
        print(f"bare (marker-less) ZEPHYR_BASE candidate: {bare_zephyr_base}")
        print("::endgroup::")

        base_env = clean_env(home_dir, zephyr_base=None)

        print("::group::tan --version")
        try:
            problems += assert_version(tan, cwd_dir, base_env)
        except Problem as err:
            problems.append(str(err))
        print("::endgroup::")

        print("::group::tan sdk list --online (CA-trust canary, tan-cli#304)")
        try:
            problems += assert_sdk_list_online(
                tan, cwd_dir, base_env, retries=args.retries, retry_delay_s=args.retry_delay
            )
        except Problem as err:
            problems.append(str(err))
        print("::endgroup::")

        for label, zb in (("ZEPHYR_BASE unset", None), ("ZEPHYR_BASE set (bare dir)", bare_zephyr_base)):
            env = clean_env(home_dir, zephyr_base=zb)
            print(f"::group::tan doctor --format json [{label}]")
            try:
                problems += assert_doctor(tan, cwd_dir, env, label=label)
            except Problem as err:
                problems.append(str(err))
            print("::endgroup::")

            print(f"::group::tan bootstrap --dry-run --format json [{label}]")
            try:
                problems += assert_bootstrap(tan, cwd_dir, env, label=label)
            except Problem as err:
                problems.append(str(err))
            print("::endgroup::")
    finally:
        shutil.rmtree(home_dir, ignore_errors=True)
        shutil.rmtree(cwd_dir, ignore_errors=True)
        shutil.rmtree(bare_zephyr_base, ignore_errors=True)

    if problems:
        for p in problems:
            print(f"::error::{p}", file=sys.stderr)
        print(f"\n{len(problems)} clean-host problem(s) found.", file=sys.stderr)
        return 1
    print("clean-host smoke: OK -- all four commands, both ZEPHYR_BASE variants.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
