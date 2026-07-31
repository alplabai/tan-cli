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

from tan.cli import _HONOURS_ROOT_FORMAT, app
from tan.commands.deferred_cmd import DEFERRED_ISSUE_CODE, DEFERRED_ISSUE_URL, DEFERRED_VERBS
from tan.exit_codes import ExitCode

runner = CliRunner()

# `DEFERRED_VERBS` is imported from `deferred_cmd` (the module that owns the
# seven stubs) rather than retyped here as a THIRD copy of the same names --
# `test_the_verb_list_here_matches_the_deferred_module` below still guards it
# against drift independently, by introspecting the stub callables themselves.


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
def test_deferred_verb_honours_root_position_format(verb):
    """`--format json` BEFORE the verb name must reach the deferral envelope
    too, not Click's exit-2 `cli.parse-error` for an unrecognised pre-command
    option. This is the headline behaviour `cli.py`'s `root` callback and
    `_HONOURS_ROOT_FORMAT` add for these seven verbs -- verified by hand
    against `target/debug/tan.exe` when the feature landed, but until now
    pinned by no test, so a revert of either the frozenset or the `ctx.obj`
    read stayed green."""
    result = runner.invoke(app, ["--format", "json", verb])
    assert result.exit_code == int(ExitCode.RUNTIME_FAILURE)
    envelope = json.loads(result.stdout)
    assert envelope["issues"][0]["code"] == DEFERRED_ISSUE_CODE


@pytest.mark.parametrize("verb", DEFERRED_VERBS)
def test_deferred_verb_ignores_arbitrary_extra_args(verb):
    """A caller's real flags/positionals for the eventual v0.6.0 command must
    not turn into a SEPARATE parse error ahead of the deferral message."""
    result = runner.invoke(app, [verb, "--some-future-flag", "value", "positional"])
    assert result.exit_code == int(ExitCode.RUNTIME_FAILURE)


def test_the_verb_list_here_matches_the_deferred_module():
    """Guards this test file itself against drifting from `deferred_cmd.py`:
    an eighth stub added there (or one removed) must fail THIS test, not just
    leave `DEFERRED_VERBS` above stale while every parametrized test still
    passes at reduced coverage. So this derives its expectation from the
    module's own stub callables instead of hardcoding a third copy of the
    verb list -- `stub_names` used to be exactly that third copy."""
    import inspect

    import tan.commands.deferred_cmd as deferred_cmd

    # Every stub `_make_stub` builds carries this exact docstring prefix (see
    # `_make_stub`); other module-level callables (`_run_deferred`,
    # `_deferred_message`, `_make_stub` itself) do not, so this identifies the
    # stub callables actually present without re-listing their names.
    stub_marker = "Deferred to v0.6.0, not yet ported to this build"
    stub_attrs = {
        attr_name
        for attr_name, value in vars(deferred_cmd).items()
        if inspect.isfunction(value) and (value.__doc__ or "").startswith(stub_marker)
    }
    assert stub_attrs == {verb.replace("-", "_") for verb in DEFERRED_VERBS}


def test_deferred_verbs_all_honour_root_position_format():
    """`cli.py`'s `_HONOURS_ROOT_FORMAT` must list every deferred verb, not
    just however many happened to be typed in by hand there -- a regression
    check on top of `cli.py` now deriving the set from `DEFERRED_VERBS`
    directly (see `_HONOURS_ROOT_FORMAT`'s own comment)."""
    assert set(DEFERRED_VERBS) <= _HONOURS_ROOT_FORMAT
