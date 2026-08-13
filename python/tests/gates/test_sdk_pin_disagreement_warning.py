# SPDX-License-Identifier: Apache-2.0
"""Gate: `tests/conftest.py` actually says something when the bound
`ALP_SDK_ROOT` is not the commit tan pins (tan-cli#691).

## Why this file exists

The check it exercises is warn-only by design -- binding a newer alp-sdk tree
deliberately is legitimate, and is how the next planner re-sync's workload is
discovered before the re-sync. A warn-only check has no failing CI status of
its own, so nothing but a test like this one can tell "it warned" from "it was
silent because the condition never arose". Every ordinary run of this suite is
the silent case: both pins agree and, on most hosts, nothing is bound at all.
A gate that cannot fail is not a gate -- the same argument
`tests/gates/test_probe_tool_inventory.py` makes for `PROBE_TOOLS` and
`tests/gates/test_tan_under_test_guard.py` makes for `tan_under_test`.

## How the condition is built

With a REAL git checkout in `tmp_path`, not a mock: `sdk_pin_disagreements`
shells out to git for `--show-toplevel`, `rev-parse HEAD` and `rev-list
--left-right --count`, and the answers this gate cares about (a HEAD that is
N commits ahead of the pin; a directory that is NOT the top of a checkout)
are properties of a real repository rather than of the function's control
flow. The pins are read from tmp files handed in through the `gate_path` /
`workflow_path` parameters, so a test can pin an arbitrary SHA without
editing this repository's own pin -- and one test deliberately does NOT do
that, reading the live pins instead, to prove the regexes still match the
files they are aimed at.

## What is NOT asserted here

That the two live pins AGREE. They do today, but `parity.yml` records that
the audit commit "can legitimately sit on either side" of the parity tag, so
a divergence is a legitimate state the maintainer may choose -- and a test
that went red for it would convert this warning into the failure tan-cli#691
explicitly asks it not to be. Only that both pins are still READABLE.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tests.conftest import (
    _FRESHNESS_GATE,
    _PARITY_WORKFLOW,
    _pin_warning_writer,
    sdk_pin_disagreements,
)

#: A SHA that is 40 hex characters and belongs to nothing -- the shape of a
#: pin, none of the identity.
ABSENT_SHA = "0" * 40


def _git(cwd: Path, *args: str) -> str:
    """Run git in `cwd` and hand back stripped stdout, refusing loudly.

    `-c` overrides rather than repository config writes for identity and
    signing: a host with `commit.gpgsign=true` globally (a real setup on the
    bench box) would otherwise make every commit here prompt or fail for a
    reason that has nothing to do with what is under test.
    """
    proc = subprocess.run(
        [
            "git",
            "-c",
            "user.email=tests@example.invalid",
            "-c",
            "user.name=tan-cli tests",
            "-c",
            "commit.gpgsign=false",
            "-C",
            str(cwd),
            *args,
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, f"git {args} failed: {proc.stdout!r} {proc.stderr!r}"
    return proc.stdout.strip()


def _sdk_checkout(root: Path, commits: int = 1) -> list[str]:
    """A real git checkout at `root` shaped like an alp-sdk root -- it carries
    `scripts/alp_project.py`, the one file `sdk_root()` accepts a root on --
    with `commits` commits. Returns their SHAs, oldest first."""
    (root / "scripts").mkdir(parents=True, exist_ok=True)
    (root / "scripts" / "alp_project.py").write_text("# stand-in\n", encoding="utf-8")
    _git(root, "init", "-q")
    shas: list[str] = []
    for index in range(commits):
        (root / f"commit-{index}.txt").write_text(f"{index}\n", encoding="utf-8")
        _git(root, "add", "-A")
        _git(root, "commit", "-q", "-m", f"commit {index}")
        shas.append(_git(root, "rev-parse", "HEAD"))
    return shas


def _pins(tmp_path: Path, commit: str, tag: str) -> dict[str, Path]:
    """The two pin FILES, written in the exact shapes the two regexes are
    aimed at: `PINNED_SDK_COMMIT` at column 0 in a Python gate module, and an
    INDENTED `PINNED_SDK_TAG:` workflow-level `env:` entry.

    The gate file also carries a `HAND_PORT_PINNED_SDK_COMMIT` line, because
    the real one does and because that line is precisely what broke
    `parity.yml`'s unanchored grep -- a pattern that matches it here returns
    two SHAs and this helper's callers see the plural refusal.
    """
    gate = tmp_path / "gate.py"
    gate.write_text(
        f'PINNED_SDK_COMMIT = "{commit}"  # alp-sdk origin/dev\n'
        f'HAND_PORT_PINNED_SDK_COMMIT = "{ABSENT_SHA}"  # alp-sdk origin/dev\n',
        encoding="utf-8",
    )
    workflow = tmp_path / "parity.yml"
    workflow.write_text(f"env:\n  PINNED_SDK_TAG: {tag}\n", encoding="utf-8")
    return {"gate_path": gate, "workflow_path": workflow}


def test_a_bound_tree_ahead_of_the_pin_names_both_shas_and_the_direction(tmp_path):
    """The mutant, and the exact 2026-08-12 shape: a bound checkout four
    commits past the pin. The warning must name the variable, both SHAs and
    which way round they sit -- without any of the three, the reader still has
    to do the three-way comparison by hand, which is the cost tan-cli#691 was
    filed over."""
    root = tmp_path / "alp-sdk"
    shas = _sdk_checkout(root, commits=5)
    pin, head = shas[0], shas[-1]
    pins = _pins(tmp_path, commit=pin, tag=pin)

    lines = sdk_pin_disagreements(("ALP_SDK_ROOT", root), **pins)

    blob = "\n".join(lines)
    assert lines, (
        "a tree four commits past the pin produced NO warning -- this is the "
        "silence tan-cli#691 exists to end, and it cost a three-way manual "
        "comparison the day it was filed."
    )
    assert "ALP_SDK_ROOT" in blob, blob
    assert str(root) in blob, blob
    assert head in blob, f"the warning does not name the BOUND tree's HEAD: {blob}"
    assert pin in blob, f"the warning does not name the PIN: {blob}"
    assert "PINNED_SDK_COMMIT" in blob, blob
    assert "4 commit(s) AHEAD of the pin, and 0 behind" in blob, (
        f"the warning does not state the direction and distance: {blob}"
    )
    assert "tan-cli#691" in blob, blob


def test_a_tree_behind_the_pin_is_reported_as_behind(tmp_path):
    """The other direction, which is the one a stale local checkout produces.
    A message that said only "differs" would leave the reader guessing which
    side owes the move."""
    root = tmp_path / "alp-sdk"
    shas = _sdk_checkout(root, commits=3)
    _git(root, "checkout", "-q", shas[0])
    pins = _pins(tmp_path, commit=shas[-1], tag=shas[-1])

    lines = sdk_pin_disagreements(("ALP_SDK_ROOT", root), **pins)

    blob = "\n".join(lines)
    assert "0 commit(s) AHEAD of the pin, and 2 behind" in blob, blob


def test_the_pinned_commit_itself_is_silent(tmp_path):
    """The control. A tree bound exactly AT the pin is the state every CI job
    and every re-synced developer box is in; warning there would train the
    reader to ignore the warning."""
    root = tmp_path / "alp-sdk"
    shas = _sdk_checkout(root, commits=3)
    pins = _pins(tmp_path, commit=shas[-1], tag=shas[-1])

    assert sdk_pin_disagreements(("ALP_SDK_ROOT", root), **pins) == []


def test_nothing_bound_is_silent(tmp_path):
    """`ALP_SDK_ROOT` unbound is THE common case -- `ci.yml`'s `python` job, a
    bare `pytest tests/`, every contributor checkout. tan-cli#691 names this
    as a hard requirement, not a nicety: a check that fires on every ordinary
    run is a check nobody reads by the second week."""
    pins = _pins(tmp_path, commit=ABSENT_SHA, tag=ABSENT_SHA)

    assert sdk_pin_disagreements(None, **pins) == []


def test_the_two_pins_are_compared_to_each_other_without_any_bound_root(tmp_path):
    """Variant 1 of the same shape (`ci.yml`'s `ref:` vs `parity.yml`'s
    `PINNED_SDK_TAG`, diverged in PR #688 and again mid-review of #485). It
    needs no bound tree at all -- both facts are in this repository -- so it
    must still be checked on a run that binds nothing."""
    commit = "1" * 40
    tag = "2" * 40
    pins = _pins(tmp_path, commit=commit, tag=tag)

    lines = sdk_pin_disagreements(None, **pins)

    blob = "\n".join(lines)
    assert commit in blob and tag in blob, blob
    assert "PINNED_SDK_COMMIT" in blob and "PINNED_SDK_TAG" in blob, blob
    assert "disagree with EACH OTHER" in blob, blob


def test_a_bound_tree_that_is_not_a_git_checkout_is_silent(tmp_path):
    """An alp-sdk delivered as a tarball (or a `tan sdk install` unpack) has
    no HEAD to compare, and that is not an anomaly worth a line of output."""
    root = tmp_path / "alp-sdk"
    (root / "scripts").mkdir(parents=True)
    (root / "scripts" / "alp_project.py").write_text("# stand-in\n", encoding="utf-8")
    pins = _pins(tmp_path, commit=ABSENT_SHA, tag=ABSENT_SHA)

    assert sdk_pin_disagreements(("ALP_SDK_ROOT", root), **pins) == []


def test_a_non_git_sdk_nested_inside_another_repo_does_not_borrow_that_repos_head(tmp_path):
    """`git -C <dir> rev-parse HEAD` answers for the ENCLOSING repository when
    `<dir>` is merely nested inside one. Without the `--show-toplevel`
    equality check, an unpacked alp-sdk sitting under any other checkout would
    be reported as disagreeing with the pin by a SHA belonging to a completely
    unrelated repository -- a false alarm that is worse than the silence, since
    it sends the reader after a tree that was never bound."""
    outer = tmp_path / "outer"
    outer.mkdir()
    (outer / "unrelated.txt").write_text("outer\n", encoding="utf-8")
    _git(outer, "init", "-q")
    _git(outer, "add", "-A")
    _git(outer, "commit", "-q", "-m", "outer")

    root = outer / "vendor" / "alp-sdk"
    (root / "scripts").mkdir(parents=True)
    (root / "scripts" / "alp_project.py").write_text("# stand-in\n", encoding="utf-8")
    pins = _pins(tmp_path, commit=ABSENT_SHA, tag=ABSENT_SHA)

    assert sdk_pin_disagreements(("ALP_SDK_ROOT", root), **pins) == []


def test_a_pin_that_cannot_be_read_is_reported_rather_than_skipped(tmp_path):
    """The rot case: a pin renamed, reshaped or moved. Reading nothing and
    reporting nothing is how this check would quietly stop working while its
    green bar kept saying otherwise, so an unreadable pin is itself a warning
    -- still not a failure, because a partial checkout must not take the suite
    down with it."""
    root = tmp_path / "alp-sdk"
    _sdk_checkout(root)
    gate = tmp_path / "gate.py"
    gate.write_text("RENAMED_SDK_COMMIT = 'nope'\n", encoding="utf-8")
    workflow = tmp_path / "parity.yml"

    lines = sdk_pin_disagreements(
        ("ALP_SDK_ROOT", root), gate_path=gate, workflow_path=workflow
    )

    blob = "\n".join(lines)
    assert "PINNED_SDK_COMMIT" in blob and "found 0" in blob, blob
    assert "could not read PINNED_SDK_TAG" in blob, (
        f"a missing {workflow} was not reported at all: {blob}"
    )


def test_two_pins_in_one_file_are_refused_rather_than_guessed_between(tmp_path):
    """`parity.yml`'s own grep learned this the expensive way. Taking the
    first of two matches would silently compare against whichever pin happens
    to sort first in the file."""
    gate = tmp_path / "gate.py"
    gate.write_text(
        f'PINNED_SDK_COMMIT = "{"1" * 40}"\nPINNED_SDK_COMMIT = "{"2" * 40}"\n',
        encoding="utf-8",
    )
    workflow = tmp_path / "parity.yml"
    workflow.write_text(f"  PINNED_SDK_TAG: {ABSENT_SHA}\n", encoding="utf-8")

    blob = "\n".join(sdk_pin_disagreements(None, gate_path=gate, workflow_path=workflow))

    assert "expected exactly ONE PINNED_SDK_COMMIT" in blob and "found 2" in blob, blob


def test_this_repositorys_own_pins_are_still_readable():
    """The regexes are aimed at two files this repository owns and both of
    those files move. This is what fails the day a pin is renamed or its
    formatting changes -- deliberately NOT an assertion that the two pins
    AGREE, which is a maintainer's call (`parity.yml`: the audit commit "can
    legitimately sit on either side" of the parity tag) and must stay a
    warning rather than a red test."""
    assert _FRESHNESS_GATE.is_file(), _FRESHNESS_GATE
    assert _PARITY_WORKFLOW.is_file(), _PARITY_WORKFLOW

    lines = sdk_pin_disagreements(None)

    unreadable = [line for line in lines if "cannot compare" in line]
    assert not unreadable, (
        "tan's own pins can no longer be read out of the files this check is "
        f"aimed at: {unreadable}. Point `_PINNED_SDK_COMMIT_RE` / "
        "`_PINNED_SDK_TAG_RE` (tests/conftest.py) at the pin's new home -- "
        "until then the tan-cli#691 warning is dead code that reports nothing "
        "and says nothing about reporting nothing."
    )


class _Reporter:
    """Stand-in for pytest's terminal reporter: records the lines and proves
    the markup kwargs are accepted (they are what makes the block visible)."""

    def __init__(self) -> None:
        self.lines: list[str] = []
        self.markup: list[dict] = []

    def write_line(self, line: str, **markup) -> None:
        self.lines.append(line)
        self.markup.append(markup)


class _Config:
    def __init__(self, reporter) -> None:
        self._reporter = reporter
        self.pluginmanager = self

    def getplugin(self, name: str):
        assert name == "terminalreporter", name
        return self._reporter


@pytest.mark.parametrize("emit", ["session-start", "terminal-summary"])
def test_both_emission_points_write_the_block(monkeypatch, emit):
    """The wiring, both ends of it. The warning is emitted twice per session
    on purpose -- once before the tests run so a wrongly-bound hour-long run
    can be aborted in its first second, once in the terminal summary next to
    the failures it explains, which is where the reader is when they ask why
    nine tests failed. Neither point is exercised by an ordinary green run,
    because the block is empty there."""
    import tests.conftest as conftest_module

    monkeypatch.setattr(
        conftest_module, "_sdk_pin_warning_lines", lambda: ("WARNING (tan-cli#691): mutant",)
    )
    reporter = _Reporter()

    if emit == "session-start":
        fixture = conftest_module._warn_when_the_bound_sdk_disagrees_with_the_pins
        fixture.__wrapped__(_Config(reporter))
    else:
        conftest_module.pytest_terminal_summary(reporter)

    assert "WARNING (tan-cli#691): mutant" in reporter.lines, reporter.lines
    assert any(m.get("yellow") for m in reporter.markup), (
        f"the block was written unstyled -- {reporter.markup}. A line that "
        "reads like every other line of a 5000-line run is the silence this "
        "check exists to end."
    )


def test_an_agreeing_session_writes_nothing_at_all(monkeypatch):
    """The control for both emission points: no disagreement, no output --
    not a blank line, not a heading."""
    import tests.conftest as conftest_module

    monkeypatch.setattr(conftest_module, "_sdk_pin_warning_lines", tuple)
    reporter = _Reporter()

    conftest_module.pytest_terminal_summary(reporter)
    conftest_module._warn_when_the_bound_sdk_disagrees_with_the_pins.__wrapped__(
        _Config(reporter)
    )

    assert reporter.lines == []


def test_the_writer_falls_back_to_stderr_without_a_terminal_reporter(capsys):
    """`-p no:terminal` leaves `getplugin` returning `None`. Losing the
    warning there would be silent by construction, so stderr is the floor."""

    class _NoReporter(_Config):
        def getplugin(self, name: str):
            return None

    write = _pin_warning_writer(_NoReporter(None))
    write("WARNING (tan-cli#691): fallback")

    captured = capsys.readouterr()
    assert "WARNING (tan-cli#691): fallback" in captured.err, captured
