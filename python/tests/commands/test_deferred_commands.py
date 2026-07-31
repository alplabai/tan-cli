# SPDX-License-Identifier: Apache-2.0
"""The seven not-yet-ported verbs (`scaffold`, `completion`, `diff`, `pinmux`,
`inspect`, `trace`, `support-bundle`) must each RESOLVE -- not fall through to
Typer's unknown-command usage error -- and refuse with the one shared,
documented `cli.command-deferred` code, at `RUNTIME_FAILURE` (1), naming
tan-cli#260. See `tan/commands/deferred_cmd.py`'s module docstring for why
that exit code and that single shared code were chosen over the alternatives.
"""
from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from tan.cli import app
from tan.commands.deferred_cmd import DEFERRED_ISSUE_CODE, DEFERRED_ISSUE_URL
from tan.exit_codes import ExitCode

runner = CliRunner()

DEFERRED_VERBS = (
    "scaffold",
    "completion",
    "diff",
    "pinmux",
    "inspect",
    "trace",
    "support-bundle",
)


@pytest.mark.parametrize("verb", DEFERRED_VERBS)
def test_deferred_verb_resolves_with_the_shared_code_and_exit(verb):
    """A bare invocation: resolves (not Click's exit-2 unknown-command path),
    exits `RUNTIME_FAILURE`, and the JSON envelope carries the shared code."""
    result = runner.invoke(app, [verb, "--format", "json"])
    assert result.exit_code == int(ExitCode.RUNTIME_FAILURE)
    envelope = json.loads(result.stdout)
    assert envelope["exitCode"] == int(ExitCode.RUNTIME_FAILURE)
    assert envelope["ok"] is False
    assert len(envelope["issues"]) == 1
    issue = envelope["issues"][0]
    assert issue["code"] == DEFERRED_ISSUE_CODE
    assert "v0.6.0" in issue["message"]
    assert DEFERRED_ISSUE_URL in issue["message"]


@pytest.mark.parametrize("verb", DEFERRED_VERBS)
def test_deferred_verb_text_mode_exits_runtime_failure_not_usage_error(verb):
    """Text mode (the default): same exit code, no traceback, and stdout
    carries nothing -- the same "stdout is the envelope channel only in JSON
    mode" contract every other command holds."""
    result = runner.invoke(app, [verb])
    assert result.exit_code == int(ExitCode.RUNTIME_FAILURE)
    assert result.exit_code != 2  # not Click's unknown-command usage error
    assert result.stdout == "", "stdout is the envelope channel in text mode too"


@pytest.mark.parametrize("verb", DEFERRED_VERBS)
def test_deferred_verb_ignores_arbitrary_extra_args(verb):
    """A caller's real flags/positionals for the eventual v0.6.0 command must
    not turn into a SEPARATE parse error ahead of the deferral message."""
    result = runner.invoke(app, [verb, "--some-future-flag", "value", "positional"])
    assert result.exit_code == int(ExitCode.RUNTIME_FAILURE)


def test_the_verb_list_here_matches_the_deferred_module():
    """Guards this test file itself against drifting from `deferred_cmd.py`
    if a verb is ever promoted out of it -- a stale entry here would keep
    "passing" against a verb that no longer exists as a stub."""
    import tan.commands.deferred_cmd as deferred_cmd

    stub_names = {"scaffold", "completion", "diff", "pinmux", "inspect", "trace"}
    for name in stub_names:
        assert hasattr(deferred_cmd, name)
    assert hasattr(deferred_cmd, "support_bundle")
