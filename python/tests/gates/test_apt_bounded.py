# SPDX-License-Identifier: Apache-2.0
"""Reject a raw `apt-get update`/`apt-get install` in a workflow or script.

Ported from alp-sdk's scripts/check_apt_bounded.py + its
tests/scripts/test_check_apt_bounded.py (alp-sdk#1592/#1575), folded into one
gate module -- tan-cli has no standalone `check_*.py` script layer; every gate
here is a pytest module under `tests/gates/` that both scans and asserts.

`Acquire::http::Timeout` bounds an IDLE read, not a SLOW one: every byte
that arrives resets the timer, and apt has no minimum-transfer-rate
option, so a mirror that trickles a byte every few seconds defeats it
forever. tan-cli#860: measured for real on PR #851, job 96014351754 --
`sudo apt-get update` (no `Acquire::*` flags at all) started 09:14:51,
printed its last output at 09:15:29, and sat silent until the JOB's own
60-minute cap killed it at 10:15:06. Happened twice.

`scripts/ci/apt-bounded.sh` (tan-cli#860, porting alp-sdk#1575) adds the
only thing that bounds the trickle class: a wall-clock `timeout` per
attempt, a `dpkg --configure -a` recovery before each retry, and a retry
only on rc 124/100. A future workflow step (or shell script) that calls
`apt-get update`/`apt-get install` directly reintroduces the unbounded
hang this issue fixed -- this gate catches that at review time.

Scope, and why it is WIDER than alp-sdk's own `^\\s*(sudo )?apt-get
(update|install)\\b`: that anchored-at-line-start form was ported first and
missed real shapes this repo actually has --

  * flags BEFORE the subcommand (`apt-get -qq update`, `python-binaries.yml`'s
    real pre-fix line) -- alp-sdk had no such call site, so it never mattered
    there.
  * a SECOND raw invocation chained after `&&` on a line whose first half is
    already wrapped, or after `;`/`|`/`||` generally -- a pure `^`-anchor can
    never see anything past the first command on the line.
  * an env-var prefix (`DEBIAN_FRONTEND=noninteractive apt-get install ...`).
  * bare `apt` (no `-get`) -- both binaries take `update`/`install`.

The invocation must still sit at a COMMAND position -- the true start of the
line, or immediately after `&&`/`;`/`|`/`||` -- so a quoted doc/hint string
(`"sudo apt-get install -y cmake"`), a generated-manifest print
(`print("apt-get update -qq")`), a probe (`apt-get check`, `command -v
apt-get`), or a shell-variable assignment that merely CONTAINS the word
(`APT="apt-get"`, `APT_MODE=sudo`, matched case-sensitively so these
upper-case identifiers from e2e-full.sh's own `$APT` indirection never
collide) still cannot match. Nor can the wrapper's own
`$SUDO timeout ... apt-get "${ACQ[@]}" "$@"` line inside
scripts/ci/apt-bounded.sh itself -- `$SUDO`/`$APT` are variable references,
never the literal token `sudo`/`apt-get` at a command position.

Files scanned: every `.github/workflows/*.yml` AND every `scripts/**/*.sh` --
`scripts/e2e-container.sh` ran its own raw `apt-get update -qq` invisibly to
a workflow-only scan (tan-cli#860 review finding #4); a shell helper is just
as capable of hanging CI as a workflow step.

Allowlist: a line matching the pattern is still permitted if it carries a
trailing `# apt-bounded:allow (...)` comment -- for a structurally-forced
case where the wrapper genuinely is not reachable yet (e.g. before
checkout). Unused in this repo today; ported so a future such case has
somewhere to go without widening the gate itself.

Run locally:

    python -m pytest tests/gates/test_apt_bounded.py -q
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]

# A command-position anchor (true line start, or right after a shell command
# separator) -- see the module docstring for why this, not a plain `^`, is
# what lets `&&`-chained and flags-first forms get caught without also
# matching apt-get/apt/sudo appearing mid-word or inside a quoted string.
_CMD_START = r"(?:^|&&|\|\||;|\|)\s*"
# `FOO=bar ` prefixes (env-var assignments before the real command), repeated
# zero or more times -- e.g. `DEBIAN_FRONTEND=noninteractive apt-get ...`.
_ENV_PREFIX = r"(?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)*"
# Flags may come BEFORE the subcommand (`apt-get -qq update`) as well as
# after it (`apt-get install -y ...`, which needs none of this to match --
# the subcommand IS the very next word) -- zero or more `-x`/`--long` tokens,
# each carrying its own leading whitespace, then a MANDATORY `\s+` of its own
# before the subcommand (so a flagless `apt update` -- zero repetitions --
# still requires, and gets, the space between "apt" and "update").
_FLAGS = r"(?:\s+-{1,2}\S+)*"
_APT_RAW_RE = re.compile(
    rf"{_CMD_START}{_ENV_PREFIX}(?:sudo\s+)?(?:apt-get|apt)\b{_FLAGS}\s+(update|install)\b"
)
_ALLOW_MARKER = "# apt-bounded:allow"


def _target_files(root: Path):
    workflows_dir = root / ".github" / "workflows"
    if workflows_dir.is_dir():
        # GitHub Actions accepts both `.yml` and `.yaml`; a `.yml`-only glob
        # gives a future `.yaml` workflow zero coverage from this gate
        # (tan-cli#854/#855 review, same pattern fixed in
        # test_parity_workflow_concurrency_and_timeouts.py).
        yield from sorted([*workflows_dir.glob("*.yml"), *workflows_dir.glob("*.yaml")])
    scripts_dir = root / "scripts"
    if scripts_dir.is_dir():
        yield from sorted(scripts_dir.rglob("*.sh"))


def find_problems(root: Path) -> list[str]:
    problems: list[str] = []
    for path in _target_files(root):
        rel = path.relative_to(root)
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if not _APT_RAW_RE.search(line):
                continue
            if _ALLOW_MARKER in line:
                continue
            problems.append(
                f"{rel}:{lineno}: raw {line.strip()!r} -- Acquire::http::Timeout "
                f"bounds an idle read, not a trickling one (tan-cli#860); call "
                f"scripts/ci/apt-bounded.sh update / install ... instead, or add "
                f"a trailing '{_ALLOW_MARKER} (reason)' comment if the wrapper "
                f"genuinely isn't reachable yet (e.g. before checkout)."
            )
    return problems


def _write_workflow(root: Path, name: str, run_body: str) -> None:
    workflows_dir = root / ".github" / "workflows"
    workflows_dir.mkdir(parents=True, exist_ok=True)
    (workflows_dir / name).write_text(
        f"""\
name: example
on: [push]
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@d23441a48e516b6c34aea4fa41551a30e30af803
      - name: install
        run: |
{run_body}
""",
        encoding="utf-8",
    )


def _write_script(root: Path, rel_path: str, body: str) -> None:
    p = root / rel_path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"#!/usr/bin/env bash\n{body}\n", encoding="utf-8")


def test_no_workflows_dir_passes(tmp_path: Path) -> None:
    assert find_problems(tmp_path) == []


def test_clean_tree_via_wrapper_passes(tmp_path: Path) -> None:
    _write_workflow(
        tmp_path,
        "clean.yml",
        "          bash scripts/ci/apt-bounded.sh update\n"
        "          bash scripts/ci/apt-bounded.sh install -y cppcheck",
    )
    assert find_problems(tmp_path) == []


def test_raw_apt_get_update_fails(tmp_path: Path) -> None:
    _write_workflow(
        tmp_path,
        "seeded.yml",
        "          sudo apt-get update -o Acquire::http::Timeout=30",
    )
    problems = find_problems(tmp_path)
    assert len(problems) == 1
    assert "seeded.yml:10" in problems[0]
    assert "apt-bounded.sh" in problems[0]


def test_raw_apt_get_install_fails(tmp_path: Path) -> None:
    _write_workflow(
        tmp_path,
        "seeded2.yml",
        "          apt-get install -y --no-install-recommends doxygen",
    )
    problems = find_problems(tmp_path)
    assert len(problems) == 1
    assert "doxygen" in problems[0]


def test_flags_before_subcommand_fails(tmp_path: Path) -> None:
    """python-binaries.yml's real pre-fix line, verbatim -- the exact gap
    alp-sdk's `^\\s*(sudo )?apt-get (update|install)\\b` could never catch,
    since `update`/`install` was not the token right after `apt-get`."""
    _write_workflow(
        tmp_path,
        "flagsfirst.yml",
        "              apt-get -qq update && apt-get -qq install -y binutils",
    )
    problems = find_problems(tmp_path)
    assert len(problems) == 1
    assert "flagsfirst.yml:10" in problems[0]


def test_chained_second_invocation_after_a_wrapped_first_fails(tmp_path: Path) -> None:
    """The first half of the line is already wrapped; the second, raw half
    after `&&` must still be caught -- a plain `^`-anchor can never see it."""
    _write_workflow(
        tmp_path,
        "chained.yml",
        "          bash scripts/ci/apt-bounded.sh update && apt-get install -y foo",
    )
    problems = find_problems(tmp_path)
    assert len(problems) == 1
    assert "chained.yml:10" in problems[0]


def test_sudo_with_flags_before_install_fails(tmp_path: Path) -> None:
    _write_workflow(
        tmp_path,
        "sudoflags.yml",
        "          sudo apt-get -y install foo",
    )
    assert len(find_problems(tmp_path)) == 1


def test_env_var_prefixed_install_fails(tmp_path: Path) -> None:
    _write_workflow(
        tmp_path,
        "envprefix.yml",
        "          DEBIAN_FRONTEND=noninteractive apt-get install -y foo",
    )
    assert len(find_problems(tmp_path)) == 1


def test_bare_apt_update_fails(tmp_path: Path) -> None:
    _write_workflow(
        tmp_path,
        "bareapt.yml",
        "          apt update",
    )
    assert len(find_problems(tmp_path)) == 1


def test_allowlisted_line_passes(tmp_path: Path) -> None:
    _write_workflow(
        tmp_path,
        "allowed.yml",
        "          apt-get update -o Acquire::http::Timeout=30  "
        "# apt-bounded:allow (pre-checkout, tan-cli#860)",
    )
    assert find_problems(tmp_path) == []


def test_quoted_fixture_is_not_a_false_positive(tmp_path: Path) -> None:
    # A doc/hint string literal, not an invocation -- the line does not START
    # with apt-get, and the quote mark blocks the command-position anchor.
    _write_workflow(
        tmp_path,
        "fixture.yml",
        '            "sudo apt-get install -y cmake" \\',
    )
    assert find_problems(tmp_path) == []


def test_generated_manifest_print_is_not_a_false_positive(tmp_path: Path) -> None:
    _write_workflow(
        tmp_path,
        "manifest.yml",
        '              print("apt-get update -qq")',
    )
    assert find_problems(tmp_path) == []


def test_apt_get_check_probe_is_not_a_false_positive(tmp_path: Path) -> None:
    # e2e-full.sh's real shape: a lock/permission PROBE, not update/install.
    _write_script(tmp_path, "scripts/probe.sh", "  if apt-get check >/dev/null 2>&1; then")
    assert find_problems(tmp_path) == []


def test_command_dash_v_apt_get_is_not_a_false_positive(tmp_path: Path) -> None:
    _write_script(tmp_path, "scripts/probe2.sh", "if command -v apt-get >/dev/null 2>&1; then")
    assert find_problems(tmp_path) == []


def test_an_apt_named_shell_variable_is_not_a_false_positive(tmp_path: Path) -> None:
    # e2e-full.sh's real `$APT`/`$APT_MODE`/`$CAN_APT` indirection, verbatim
    # shape -- these are identifiers that CONTAIN "apt", never the literal
    # token at a command position, and the match is case-sensitive.
    _write_script(
        tmp_path,
        "scripts/probe3.sh",
        'CAN_APT=1; APT_MODE=sudo; APT="sudo -n env DEBIAN_FRONTEND=noninteractive apt-get"\n'
        'APT="apt-get"',
    )
    assert find_problems(tmp_path) == []


def test_the_wrappers_own_invocation_line_is_not_a_false_positive(tmp_path: Path) -> None:
    # `$SUDO`/`$slice` are shell variables, not the literal tokens -- this is
    # scripts/ci/apt-bounded.sh's real line, verbatim.
    _write_script(
        tmp_path,
        "scripts/ci/apt-bounded.sh",
        '  $SUDO timeout --signal=TERM --kill-after=30 "$slice" apt-get "${ACQ[@]}" "$@"',
    )
    assert find_problems(tmp_path) == []


def test_a_raw_call_in_a_shell_script_fails(tmp_path: Path) -> None:
    """scripts/**/*.sh is in scope, not just workflows -- the tan-cli#860
    review's #4: e2e-container.sh's own apt-get was invisible to a
    workflows-only scan."""
    _write_script(tmp_path, "scripts/e2e-container.sh", "apt-get update -qq >/dev/null 2>&1")
    problems = find_problems(tmp_path)
    assert len(problems) == 1
    assert "scripts" in problems[0] and "e2e-container.sh" in problems[0]


def test_this_repos_own_workflows_and_scripts_are_clean() -> None:
    """The real thing this gate exists to guard: no apt-get in
    .github/workflows/*.yml or scripts/**/*.sh bypasses
    scripts/ci/apt-bounded.sh, right now, in this checkout."""
    problems = find_problems(REPO_ROOT)
    assert problems == [], "\n".join(problems)
