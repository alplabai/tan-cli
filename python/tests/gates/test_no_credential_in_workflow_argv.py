# SPDX-License-Identifier: Apache-2.0
"""No workflow or shell script may hand a credential to a tool on a CLI flag.

tan-cli#1185. `python/tan/core/toolchain_provision.py`'s `SDK_TOKEN_ENV_VARS`
block states the invariant in its own words -- a token goes in an environment
variable, not a CLI flag, because a flag

    lands the value in shell history, in the host process table for the whole
    (multi-minute) run, and in any CI log that echoes the command

-- and tan-cli#1143 is the whole design that follows from it: `tan bootstrap`
carries its token to `west sdk install` out of band through a private netrc
(`bootstrap_cmd._stage_sdk_credential`) rather than through
`--personal-access-token`.

Two workflow steps did the opposite anyway (`getting-started.yml`'s
"install the Zephyr SDK (west sdk install, the printed remedy)" and
`release-combination.yml`'s "install the Zephyr SDK (west sdk install,
arm-zephyr-eabi)"), and the harm there was mild and bounded -- a run-scoped
`secrets.GITHUB_TOKEN`, masked in logs, on an ephemeral `--rm` runner. What
was NOT mild is the precedent: a workflow in this repo doing the thing the
source comment forbids is what the next author copies, and the next copy may
carry something that does not expire with the run.

Both were moved onto a netrc in tan-cli#1185. This gate is what stops the
third one appearing, because a `grep` nobody runs is not a check.

## Why COMMENT lines are exempt, and why that is not a loophole

The flag is legitimately NAMED in prose all over this repo -- the upstream
error string west itself prints ("Try executing install script with
--personal-access-token argument or use a .netrc file"), `clean-host.yml`'s
note contrasting a different tool's different endpoint, and the tan-cli#1185
blocks that explain why the netrc is there. Those are documentation and must
stay. Only a line that a shell would EXECUTE can put a value in a process
table, so only those are scanned -- which is also exactly the shape
tan-cli#1185's own acceptance criterion asks for ("either the flag is gone,
or the line carries a comment naming tan-cli#1143 and saying why this site is
the exception").
"""

from __future__ import annotations

import functools
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"
SCRIPTS_DIR = REPO_ROOT / "scripts"

#: Credential-bearing CLI flags. Deliberately short: every entry here has to
#: be a flag whose VALUE is a secret, measured on a real tool, not a guess.
#: `--personal-access-token` is `west sdk install`'s (Zephyr v4.4.1
#: `scripts/west_commands/sdk.py`, the `args.personal_access_token` branch
#: `toolchain_provision.WEST_SDK_TOKEN_FLAG_ATTR` pins). A flag that merely
#: NAMES a variable (`-e TAN_GITHUB_TOKEN`, as PR #1184 forwards into the
#: container) is not one of these and must never be added: name-only is the
#: safe form this gate is protecting.
CREDENTIAL_FLAGS = ("--personal-access-token",)


def _executable_lines(body: str) -> list[tuple[int, str]]:
    """The 1-indexed lines of a shell body a shell would actually run --
    comments and blanks dropped.

    A line whose first non-space character is `#` cannot place anything in a
    process table. Nothing subtler than that is attempted on purpose: a
    heredoc or a quoted string containing a `#` stays IN, so this errs toward
    reporting a site rather than excusing one.
    """
    return [
        (number, line)
        for number, line in enumerate(body.splitlines(), start=1)
        if line.strip() and not line.lstrip().startswith("#")
    ]


def _offenders(body: str) -> list[tuple[int, str]]:
    return [
        (number, line.strip())
        for number, line in _executable_lines(body)
        if any(flag in line for flag in CREDENTIAL_FLAGS)
    ]


@functools.cache
def _run_blocks() -> tuple[tuple[str, str, str], ...]:
    """`(workflow file name, step name, run body)` for every step in every
    workflow that has a `run:`."""
    blocks: list[tuple[str, str, str]] = []
    for path in sorted(WORKFLOW_DIR.glob("*.yml")):
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        for job_name, job in (document.get("jobs") or {}).items():
            for index, step in enumerate(job.get("steps") or []):
                run = step.get("run")
                if isinstance(run, str):
                    label = step.get("name") or f"{job_name}[{index}]"
                    blocks.append((path.name, label, run))
    return tuple(blocks)


@functools.cache
def _shell_scripts() -> tuple[Path, ...]:
    return tuple(sorted(SCRIPTS_DIR.rglob("*.sh")))


def test_the_scan_actually_reaches_something():
    """Anti-vacuity, the tan-cli#1145 shape: every assertion below is "no hit
    in <corpus>", which passes trivially if the corpus is empty. A glob that
    silently stopped matching -- workflows relocated, `*.yml` renamed to
    `*.yaml`, this file moved so `parents[3]` no longer lands on the repo
    root -- would turn this whole module green while checking nothing."""
    assert WORKFLOW_DIR.is_dir(), WORKFLOW_DIR
    assert SCRIPTS_DIR.is_dir(), SCRIPTS_DIR
    assert len(_run_blocks()) > 50, (
        f"only {len(_run_blocks())} workflow `run:` blocks found under "
        f"{WORKFLOW_DIR} -- the scan is not reaching the workflows it is "
        f"supposed to be checking"
    )
    assert len(_shell_scripts()) >= 4, (
        f"only {len(_shell_scripts())} shell scripts found under {SCRIPTS_DIR}"
    )


def test_the_detector_fires_on_a_planted_flag():
    """The other half of anti-vacuity: prove the detector can say NO. A
    `_executable_lines` that returned nothing, or a `CREDENTIAL_FLAGS` that
    drifted away from the spelling west actually uses, would make every
    assertion below pass for the wrong reason."""
    planted = (
        "set -euo pipefail\n"
        "# a comment naming --personal-access-token must NOT be reported\n"
        '  west sdk install --version 1.0.1 --personal-access-token "${GH_TOKEN}"\n'
    )
    hits = _offenders(planted)
    assert len(hits) == 1, hits
    assert hits[0][0] == 3, hits
    assert _offenders("# only a comment mentions --personal-access-token\n") == []


def test_no_workflow_run_block_puts_a_credential_in_argv():
    found = [
        f"{name}: step {step!r}, run-block line {number}: {line}"
        for name, step, body in _run_blocks()
        for number, line in _offenders(body)
    ]
    assert not found, (
        "a workflow step hands a credential to a tool on a CLI flag, which "
        "puts the value in the host process table for the whole run "
        "(tan-cli#1185, tan-cli#1143). Stage it into a private netrc instead "
        "-- `getting-started.yml`'s 'install the Zephyr SDK (west sdk "
        "install, the printed remedy)' step is the worked example: `mktemp "
        "-d`, a `trap ... EXIT` discard, `chmod 0600`, `export NETRC`. If "
        "this site genuinely cannot, say so in a comment on the line itself "
        "naming tan-cli#1143, and add the reason here.\n  " + "\n  ".join(found)
    )


def test_no_shell_script_puts_a_credential_in_argv():
    found = [
        f"{path.relative_to(REPO_ROOT)}:{number}: {line}"
        for path in _shell_scripts()
        for number, line in _offenders(path.read_text(encoding="utf-8"))
    ]
    assert not found, (
        "a script under scripts/ hands a credential to a tool on a CLI flag "
        "(tan-cli#1185, tan-cli#1143):\n  " + "\n  ".join(found)
    )
