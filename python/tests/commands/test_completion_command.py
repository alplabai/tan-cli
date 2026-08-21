# SPDX-License-Identifier: Apache-2.0
"""`tan completion` -- CLI surface tests.

`completion` is not registered in `tan.cli.app` by this change (the shared
`cli.py` registration point is owned by the orchestrator wiring commands in
parallel), so these tests mount the command on a throwaway `typer.Typer()`
rather than importing `tan.cli.app` -- matching `test_faultdecode_command.py`'s
own note for the same situation.

Named twin: `crates/tan-cli/src/commands/completion.rs`'s `#[cfg(test)] mod
tests`. `test_resolve_shell_defaults_and_normalizes` and
`test_embedded_scripts_are_nonempty_and_shell_specific` mirror its
`resolve_shell_defaults_and_normalizes`/`scripts_are_nonempty_and_shell_specific`
1:1; `test_embedded_scripts_list_every_registered_subcommand` mirrors its
`embedded_scripts_list_every_cli_command` (this port's cheaper equivalent --
`tan.cli._SUBCOMMAND_NAMES` stands in for walking clap's built command graph).
This port has no twin of the oracle's `completion_scripts_match_clap_flags_
exactly` gate (there is no local clap graph to diff against): the three
scripts are frozen, byte-for-byte captures of the oracle's own already-gated
output, not derived from a live command graph here, so there is no
independent flag table this port could drift out of sync with.

**One part of them IS derived, and does have such a gate: the `--format`
value lists (tan-cli#403).** The captures said `text json` on every command,
including `validate`, which really accepts four values -- exactly the drift
"frozen capture" cannot notice. Those lists are now spliced from
`tan.output_format`'s enums, and `tests/commands/test_output_format.py`
diffs the emitted script against what each command's parser accepts, which is
this port's local answer to `completion_scripts_match_clap_flags_exactly`.

Every value in this file was confirmed against the built oracle
(`target/debug/tan.exe`, reports `tan 0.4.1` -- see
`tests/parity/oracle.py:219`'s `PINNED_ORACLE_VERSION`, the one place that
spelling is owned): `tan completion --shell
<bash|zsh|fish> [--format json]`, `tan completion --shell <bogus>
[--format json]`, and the JSON `data.script` values these tests assert
`BASH_SCRIPT`/`ZSH_SCRIPT`/`FISH_SCRIPT` equal were extracted byte-for-byte
from the oracle's own `--format json` output, not retyped by hand.
"""

from __future__ import annotations

import functools as _functools
import json

import pytest
import typer
from typer.testing import CliRunner

from tan.commands.completion_cmd import (
    BASH_SCRIPT,
    _COMMAND_NAMES,
    FISH_SCRIPT,
    SHELL_UNSUPPORTED_CODE,
    SHELL_UNSUPPORTED_MESSAGE,
    SHELL_UNSUPPORTED_TEXT_LINE,
    ZSH_SCRIPT,
    completion,
    resolve_shell,
    script_for,
)

app = typer.Typer()
app.command("completion")(completion)
runner = CliRunner()


# ---------------------------------------------------------------------------
# `resolve_shell` / `script_for` -- twins of completion.rs's own unit tests
# ---------------------------------------------------------------------------


def test_resolve_shell_defaults_and_normalizes():
    assert resolve_shell(None) == "bash"
    assert resolve_shell("  ZSH ") == "zsh"
    assert resolve_shell("fish") == "fish"
    assert resolve_shell("tcsh") is None
    assert resolve_shell("") is None  # blank is not "absent" -- only None defaults


def test_script_for_selects_the_matching_script():
    assert script_for("bash") == BASH_SCRIPT
    assert script_for("zsh") == ZSH_SCRIPT
    assert script_for("fish") == FISH_SCRIPT


def test_script_for_unrecognised_value_falls_back_to_bash():
    """Mirrors the oracle's `script_for` match arm (`_ => BASH_SCRIPT`).
    Unreachable from `completion()` itself -- `resolve_shell` already rejects
    anything this would matter for -- kept as its own unit so the fallback
    stays intentional if a future caller reaches `script_for` directly."""
    assert script_for("tcsh") == BASH_SCRIPT


def test_embedded_scripts_are_nonempty_and_shell_specific():
    assert "_tan_complete" in BASH_SCRIPT
    assert BASH_SCRIPT.startswith("# tan CLI bash completion")
    assert "#compdef tan" in ZSH_SCRIPT
    # `__fish_seen_subcommand_from`, not `__fish_use_subcommand`: the latter
    # was the command list's gate until tan-cli#503 measured it returning
    # false at a global flag's VALUE (`complete -C 'tan --sdk-root /x '`
    # returned nothing at all in fish 3.7.0), and no line uses it any more.
    assert "__fish_seen_subcommand_from" in FISH_SCRIPT
    # Every script ends in exactly one trailing newline (this command's own
    # `print(script)` adds the second one the oracle's stdout capture shows).
    for script in (BASH_SCRIPT, ZSH_SCRIPT, FISH_SCRIPT):
        assert script.endswith("\n")
        assert not script.endswith("\n\n")


def test_embedded_scripts_list_every_registered_subcommand():
    """Drift guard: every verb `tan.cli` registers must tab-complete on all
    three shells. Reads `tan.cli._SUBCOMMAND_NAMES` (a frozenset this file
    only imports, never edits) rather than hand-duplicating the 31-name list a
    third time -- the same reasoning the oracle's own
    `embedded_scripts_list_every_cli_command` gives for reading clap's command
    graph instead of a hand-kept copy. Word-boundary, not substring: a bare
    `.contains` would also match a name that is a fragment of an unrelated
    token (e.g. "run" inside a longer word)."""
    from tan.cli import _SUBCOMMAND_NAMES

    def script_lists(script: str, name: str) -> bool:
        tokens = set()
        current = []
        for ch in script:
            if ch.isalnum() or ch == "-":
                current.append(ch)
            else:
                if current:
                    tokens.add("".join(current))
                current = []
        if current:
            tokens.add("".join(current))
        return name in tokens

    missing = [
        (shell, name)
        for name in _SUBCOMMAND_NAMES
        for shell, script in (("bash", BASH_SCRIPT), ("zsh", ZSH_SCRIPT), ("fish", FISH_SCRIPT))
        if not script_lists(script, name)
    ]
    assert missing == []


# ---------------------------------------------------------------------------
# CLI surface -- success paths
# ---------------------------------------------------------------------------


def test_default_shell_is_bash_when_shell_flag_absent():
    result = runner.invoke(app, [])
    assert result.exit_code == 0
    assert result.stdout == BASH_SCRIPT + "\n"
    assert result.stderr == ""


@pytest.mark.parametrize(
    ("shell_arg", "expected"),
    [
        ("bash", BASH_SCRIPT),
        ("zsh", ZSH_SCRIPT),
        ("fish", FISH_SCRIPT),
        ("  ZSH  ", ZSH_SCRIPT),  # trimmed + lowercased, like the oracle
        ("Fish", FISH_SCRIPT),
    ],
)
def test_shell_flag_selects_the_right_script_text_mode(shell_arg, expected):
    """Text mode prints the script straight to stdout (the payload itself,
    not a `- <detail>` line on stderr) and nothing to stderr -- matching
    `completion.rs`'s own `println!` plus its comment on why: `eval "$(tan
    completion --shell zsh)"` and `> file` both read stdout."""
    result = runner.invoke(app, ["--shell", shell_arg])
    assert result.exit_code == 0
    assert result.stdout == expected + "\n"
    assert result.stderr == ""


def test_json_mode_success_envelope_matches_the_oracle_shape():
    result = runner.invoke(app, ["--shell", "zsh", "--format", "json"])
    assert result.exit_code == 0
    doc = json.loads(result.stdout)
    assert doc == {
        "command": "completion",
        "ok": True,
        "exitCode": 0,
        "project": {"root": None, "boardYaml": None},
        "data": {"schemaVersion": "1", "shell": "zsh", "script": ZSH_SCRIPT},
        "issues": [],
    }
    # No `sdk` key at all (absent, not null) -- completion resolves no checkout.
    assert "sdk" not in doc
    assert result.stderr == ""


def test_json_mode_default_shell_is_bash():
    result = runner.invoke(app, ["--format", "json"])
    doc = json.loads(result.stdout)
    assert doc["data"]["shell"] == "bash"
    assert doc["data"]["script"] == BASH_SCRIPT


# ---------------------------------------------------------------------------
# CLI surface -- the one failure mode: an unsupported --shell value
# ---------------------------------------------------------------------------


def test_unsupported_shell_text_mode_reports_to_stderr_and_exits_one():
    """Verbatim against the oracle: stdout stays EMPTY on this path (measured:
    `tan.exe completion --shell powershell` writes nothing to stdout), and the
    error line goes to stderr, matching every other command's text-mode error
    convention."""
    result = runner.invoke(app, ["--shell", "powershell"])
    assert result.exit_code == 1
    assert result.stdout == ""
    assert result.stderr == SHELL_UNSUPPORTED_TEXT_LINE + "\n"


def test_unsupported_shell_json_mode_matches_the_oracle_envelope():
    result = runner.invoke(app, ["--shell", "powershell", "--format", "json"])
    assert result.exit_code == 1
    doc = json.loads(result.stdout)
    assert doc == {
        "command": "completion",
        "ok": False,
        "exitCode": 1,
        "project": {"root": None, "boardYaml": None},
        # `shell` falls back to "bash" and `script` is empty on this path --
        # verbatim from the oracle's own error-path `CompletionData`.
        "data": {"schemaVersion": "1", "shell": "bash", "script": ""},
        "issues": [
            {
                "code": SHELL_UNSUPPORTED_CODE,
                "severity": "error",
                "message": SHELL_UNSUPPORTED_MESSAGE,
            }
        ],
    }


def test_blank_shell_value_is_also_unsupported():
    """A literal empty `--shell ""` is NOT "absent" (that is `None`, the
    no-flag-at-all case, which defaults to bash) -- `resolve_shell("")` trims
    to `""`, which matches none of `bash`/`zsh`/`fish`."""
    result = runner.invoke(app, ["--shell", ""])
    assert result.exit_code == 1
    assert result.stderr == SHELL_UNSUPPORTED_TEXT_LINE + "\n"


# ---------------------------------------------------------------------------
# Global-flag surface (clap `GlobalArgs`, `global = true`) -- accepted, unused
# ---------------------------------------------------------------------------


def test_every_global_flag_is_accepted_without_erroring():
    """`tan completion --ci` (etc.) must exit 0, not a Click usage error --
    clap accepts every one of these on every subcommand. Mirrors
    `clean_cmd.clean`'s identical precedent."""
    result = runner.invoke(
        app,
        [
            "--shell",
            "bash",
            "--project",
            "some/project",
            "--board-yaml",
            "some/board.yaml",
            "--sdk-root",
            "some/sdk",
            "--quiet",
            "--verbose",
            "--no-color",
            "--non-interactive",
            "--ci",
            "--target",
            "zephyr-conf",
            "--all",
        ],
    )
    assert result.exit_code == 0
    assert result.stdout == BASH_SCRIPT + "\n"


def test_empty_format_value_is_a_parse_error():
    """Verified against the oracle: `tan completion --format ""` exits 2 on
    the value itself ("a value is required for '--format <FORMAT>'"), not a
    silent fallback to text mode."""
    result = runner.invoke(app, ["--format", ""])
    assert result.exit_code == 2


def test_unrecognised_flag_is_a_usage_error():
    """Verified against the oracle: an unknown flag is `error: unexpected
    argument '--bogus' found`, exit 2 -- clap's own parse-error shape, which
    Click's default (undecorated) `app.command()` registration already
    reproduces without any `ignore_unknown_options` context setting."""
    result = runner.invoke(app, ["--bogus"])
    assert result.exit_code == 2


def test_unexpected_positional_is_a_usage_error():
    """Verified against the oracle: `tan completion badarg` is `error:
    unexpected argument 'badarg' found`, exit 2 -- `completion` takes no
    positional at all."""
    result = runner.invoke(app, ["badarg"])
    assert result.exit_code == 2


# ---------------------------------------------------------------------------
# `--format json` before the subcommand name (clap `global = true`)
# ---------------------------------------------------------------------------


def test_format_json_before_subcommand_reads_off_ctx_obj():
    """Mirrors `test_faultdecode_command.py`'s identical test: `cli.py`'s
    `root` callback stashes a leading `--format` on `ctx.obj`, and a command
    that has joined `_HONOURS_ROOT_FORMAT` reads it back. Confirmed live
    against the built oracle: `tan --format json completion --shell zsh` and
    `tan completion --shell zsh --format json` print byte-identical JSON at
    rc=0, because the oracle's clap `--format` is `global = true`. `cli.py`
    itself is not touched by this change -- see this module's own docstring --
    so this mounts the same throwaway root callback `test_faultdecode_
    command.py` uses rather than the real one."""
    root_app = typer.Typer()

    @root_app.callback(invoke_without_command=True)
    def _root(ctx: typer.Context, output_format: str = typer.Option(None, "--format")) -> None:
        ctx.obj = {"format": output_format}

    root_app.command("completion")(completion)
    result = runner.invoke(root_app, ["--format", "json", "completion", "--shell", "zsh"])
    assert result.exit_code == 0
    doc = json.loads(result.stdout)
    assert doc["data"]["shell"] == "zsh"
    assert doc["data"]["script"] == ZSH_SCRIPT


# ---------------------------------------------------------------------------
# tan-cli#503: `doctor` must not advertise flags the Python `doctor` rejects
# ---------------------------------------------------------------------------


def _case_arm_lines(script: str, label_line_substr: str) -> list[str]:
    """Collect a bash/zsh `case` arm IN FULL: the pattern label
    (e.g. `doctor)`) on its own line, then every following line through the
    terminating `;;`, inclusive.

    Filtering on the label alone (the pre-existing shape of this test) only
    ever matches the label line itself -- `doctor)` -- which never contains a
    flag name; the flags live on the NEXT line
    (`COMPREPLY=( $(compgen -W "$global_flags --build --fix" -- "$cur") )` in
    bash, `_arguments '--build[...]' ...` in zsh). A `"target-kind" not in
    line` assertion over label-only lines is true no matter what the arm body
    says, so it could not have caught the regression it was written for."""
    lines = script.splitlines()
    out: list[str] = []
    capturing = False
    for line in lines:
        if not capturing and label_line_substr in line:
            capturing = True
        if capturing:
            out.append(line)
            if ";;" in line:
                capturing = False
    return out


def test_doctor_completion_no_longer_offers_target_kind_or_server():
    """`doctor_cmd.py` deliberately did not port the debug half
    (`--target-kind`/`--server`) -- but the captured scripts still offered
    both, so accepting the completion's own suggestion was a guaranteed exit
    2 (`cli.parse-error`). `support-bundle`/`debug-config` genuinely still
    carry both and must be untouched.

    bash/zsh arms span two lines (label, then body) so the whole arm is
    collected via `_case_arm_lines`; fish's `complete -c tan -n
    '__fish_seen_subcommand_from doctor' -l ...` is a single line per flag
    and the substring filter already sees the flags directly."""
    doctor_lines = _case_arm_lines(BASH_SCRIPT, "doctor)")
    assert doctor_lines and any("compgen" in line for line in doctor_lines), doctor_lines
    doctor_lines += _case_arm_lines(ZSH_SCRIPT, "doctor)")
    assert any("_arguments" in line for line in doctor_lines), doctor_lines
    doctor_lines += [
        line
        for line in FISH_SCRIPT.splitlines()
        if "__fish_seen_subcommand_from doctor'" in line
    ]
    assert any("-l build" in line for line in doctor_lines), doctor_lines
    for line in doctor_lines:
        # Bare substrings, not "--target-kind"/"--server": fish spells its
        # flags `-l target-kind`/`-l server` (no double dash).
        assert "target-kind" not in line, line
        assert "server" not in line, line


def test_debug_config_and_support_bundle_still_keep_target_kind_and_server():
    """Control for the above: only `doctor` lost the two flags. bash/zsh spell
    the flag `--target-kind`; fish's `complete -l target-kind` spells it
    without the leading dashes."""
    assert "--target-kind" in BASH_SCRIPT
    assert "--server" in BASH_SCRIPT
    assert "--target-kind" in ZSH_SCRIPT
    assert "--server" in ZSH_SCRIPT
    assert "-l target-kind" in FISH_SCRIPT
    assert "-l server" in FISH_SCRIPT


def test_explain_completion_offers_the_code_flag():
    """`tan explain --code <ALP-Bxxx|ALP_ERR_*>` shipped in v0.6.0-rc1
    (`explain_cmd.py`'s `--code`), and all three emitted scripts offered only
    `--template` (tan-cli#834).

    `explain`'s own overview omits `--code` deliberately -- a line there would
    break the golden (`explain_cmd.py`'s `_overview()`) -- so the advertised
    surfaces are `--help`, the code-shaped-argument hint (`_code_hint`, which
    shipped in the same commit as `--code`), and this. Completion was the one
    still silent.

    Nothing regenerates per-command flags: the six markers `_fill_formats`
    splices refresh the `--format` value lists, `generate --target`'s list and
    the command NAMES -- never a command's own flags. A flag added to any
    command drifts silently until a test like this one names it."""
    bash_arm = _case_arm_lines(BASH_SCRIPT, "explain)")
    assert bash_arm, f"no `explain)` arm in the bash script:\n{BASH_SCRIPT}"
    assert any("--code" in line for line in bash_arm), bash_arm

    zsh_arm = _case_arm_lines(ZSH_SCRIPT, "explain)")
    assert zsh_arm, f"no `explain)` arm in the zsh script:\n{ZSH_SCRIPT}"
    assert any("--code" in line for line in zsh_arm), zsh_arm

    # fish spells its flags `-l code`, with no leading dashes.
    fish_lines = [
        line
        for line in FISH_SCRIPT.splitlines()
        if "__fish_seen_subcommand_from explain'" in line
    ]
    assert fish_lines, f"no `explain` completions in the fish script:\n{FISH_SCRIPT}"
    assert any("-l code" in line for line in fish_lines), fish_lines


def test_explain_completion_still_offers_the_template_flag():
    """Control for the above: `--code` is ADDED beside `--template`, never in
    place of it. `--template` is the flag `explain`'s overview does advertise,
    so losing it here would be the more visible regression of the two."""
    assert any("--template" in line for line in _case_arm_lines(BASH_SCRIPT, "explain)"))
    assert any("--template" in line for line in _case_arm_lines(ZSH_SCRIPT, "explain)"))
    assert any(
        "-l template" in line
        for line in FISH_SCRIPT.splitlines()
        if "__fish_seen_subcommand_from explain'" in line
    )


# ---------------------------------------------------------------------------
# tan-cli#503: bash/zsh `--format` completion must find the real subcommand
# even when a global flag (with a value) precedes it.
# ---------------------------------------------------------------------------


def _bash_complete(argv: list[str]) -> list[str]:
    """Drive the real emitted `BASH_SCRIPT` in a live bash and return
    `COMPREPLY` for completing the LAST word of `argv` (typically `""`, an
    about-to-be-typed value)."""
    import subprocess as sp

    words = " ".join(f'"{w}"' for w in argv)
    script = (
        BASH_SCRIPT
        + "\nCOMP_WORDS=(" + words + ")\n"
        + f"COMP_CWORD={len(argv) - 1}\n"
        + "COMPREPLY=()\n_tan_complete\nprintf '%s\\n' \"${COMPREPLY[@]}\"\n"
    )
    proc = sp.run(  # noqa: S603, S607 -- fixed script, no shell metacharacters
        ["bash", "-c", script], capture_output=True, text=True, timeout=10
    )
    return [line for line in proc.stdout.splitlines() if line]


@_functools.lru_cache(maxsize=1)
def _bash_available() -> bool:
    """`shutil.which("bash") is not None` is not proof bash is on this host --
    on Windows, `C:\\Windows\\System32\\bash.exe` is the WSL launcher stub,
    installed whenever the "Windows Subsystem for Linux" optional feature is
    enabled even with zero distributions registered. It is a real,
    `PATH`-resolvable, executable `bash.exe` that is NOT bash: run with `-c`,
    it ignores the command and prints a UTF-16LE "Windows Subsystem for Linux
    has no installed distributions..." banner instead, which `_bash_complete`
    would then misread as `COMPREPLY`. Spawn a trivial command and require the
    literal expected output before treating `bash` as usable, rather than
    trusting presence on `PATH`.

    A version floor is deliberately NOT part of this check, and that was
    measured rather than assumed. Retiring the Rust oracle (tan-cli#269)
    dropped `actions-rust-lang/setup-rust-toolchain@v1` from the macOS jobs,
    and with it the `brew install bash` its internal "Unbork mac" step ran, so
    from that PR onward macOS CI resolves `bash` to Apple's `/bin/bash`
    3.2.57 -- the last GPLv2 release -- where these tests had been running on
    Homebrew bash 5 all along. `BASH_SCRIPT` was then run under a locally
    built GNU bash 3.2.0 and under bash 5.2.21: every `COMPREPLY` this file
    asserts on came back identical on both, with empty stderr and rc 0. It
    uses nothing newer than bash 3.2 -- indexed arrays, `[[ ]]`, `local`,
    `$(( ))`, `compgen`; no `declare -A`, no `${var,,}`, no `mapfile`, no
    globstar. If that ever changes, this probe is the place to grow a
    capability check for the specific construct -- not a version compare (see
    `tests/installers/test_installer_release_layout.py`'s
    `_bash_setlocale_warning_probe` for the same rule applied to a behaviour
    bash 3.2 genuinely lacks)."""
    import shutil as _shutil
    import subprocess as _sp

    if _shutil.which("bash") is None:
        return False
    proc = None
    # tan-cli#725: two attempts, because the ONLY observed failure is cold
    # process start-up. The first spawn pays it (10.6s measured on a loaded
    # Windows host); a second is warm and returns in ~0.1s. Absorbing the
    # timeout without retrying would be a silent downgrade -- the 15 tests
    # this probe guards would SKIP on precisely the busy hosts that provoked
    # the bug, trading a loud collection abort for quietly untested code. A
    # healthy host never reaches the second attempt, so this costs nothing
    # where nothing is wrong.
    for attempt in range(2):
        try:
            proc = _sp.run(  # noqa: S603, S607 -- fixed args, no shell metacharacters
                ["bash", "-c", "echo tan-bash-ok"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            break
        except _sp.TimeoutExpired:
            if attempt == 0:
                continue
            proc = None
        except OSError:
            proc = None
        break
    if proc is None:
        # tan-cli#725: `TimeoutExpired` is a `SubprocessError`, NOT an
        # `OSError`, so the one failure mode `timeout=10` exists to bound was
        # the one this handler did not absorb. The probe runs at MODULE scope
        # (the `skipif` decorators below call it), so the escaping exception
        # aborted COLLECTION of this whole file: pytest exited 2 having run
        # nothing, emitting zero `FAILED` lines. A branch-vs-baseline diff
        # then reads every known failure as newly PASSING -- a far worse
        # outcome than a loud failure. Measured trigger: a cold Git Bash
        # spawn on a loaded Windows host took ~10.6s against this 10s budget
        # (0.10s once warm). Nothing wrong with bash; process start-up alone.
        #
        # Catching it is correct HERE because this is a host-capability
        # probe: "bash did not answer within the budget" and "no bash on
        # PATH" are the same answer to the only question asked -- can this
        # host usefully run the bash completion tests -- and that answer is
        # no, so skip. That is the OPPOSITE of the rule for production code,
        # where `test_diff_command.py`'s
        # `test_sdk_validator_timeout_refuses_instead_of_reporting_clean`
        # records a blanket `except ... SubprocessError` swallowing a timeout
        # as a MAJOR defect: a wedged validator must refuse, never fall back
        # to a clean result. Narrowed to `TimeoutExpired` rather than
        # `SubprocessError` so that distinction stays visible at the seam.
        return False
    return proc.returncode == 0 and "tan-bash-ok" in proc.stdout


@pytest.mark.skipif(
    not _bash_available(), reason="no real bash on this host (WSL launcher stub is not bash)"
)
def test_bash_format_completion_finds_the_subcommand_past_a_leading_global_flag():
    """`tan --sdk-root /x validate --format <TAB>` used to offer only `text
    json` (`${COMP_WORDS[1]}` was `--sdk-root`, matching no `case` arm), the
    exact misinformation tan-cli#403 exists to prevent -- and the exact
    pre-subcommand shape `output_format.py`'s docstring names as the one
    alp-sdk-vscode's `withSdkRoot` actually uses."""
    reply = _bash_complete(["tan", "--sdk-root", "/x", "validate", "--format", ""])
    assert "diagnostic-v1" in reply
    assert "sarif" in reply


@pytest.mark.skipif(not _bash_available(), reason="no bash on this host")
def test_bash_format_completion_still_works_with_no_leading_flag():
    """Regression guard: the ordinary, no-global-flag shape must still work
    after the scan replaces the fixed-index read."""
    reply = _bash_complete(["tan", "validate", "--format", ""])
    assert "diagnostic-v1" in reply
    assert "sarif" in reply


@pytest.mark.skipif(not _bash_available(), reason="no bash on this host")
def test_bash_format_completion_narrow_command_still_gets_the_narrow_list():
    reply = _bash_complete(["tan", "--sdk-root", "/x", "size", "--format", ""])
    assert reply == ["text", "json"]


@pytest.mark.skipif(not _bash_available(), reason="no bash on this host")
def test_bash_format_completion_does_not_leak_its_loop_variable():
    """The subcommand scan's `for vf in $value_flags` loop variable was
    missing from the function's `local` declaration, so it leaked into and
    clobbered a `$vf` already set in the sourcing interactive shell -- a real
    collision, not a hypothetical one: plenty of shells use short names like
    `vf` for their own state. `_bash_complete` sources `BASH_SCRIPT` and runs
    `_tan_complete` in a subshell that starts with `vf` pre-set; if the loop
    variable is still un-local'd, that subshell's `vf` comes back overwritten
    with the last/matched entry of `$value_flags` instead of its original
    value."""
    import subprocess as sp

    argv = ["tan", "--sdk-root", "/x", "validate", "--format", ""]
    words = " ".join(f'"{w}"' for w in argv)
    script = (
        'vf="untouched"\n'
        + BASH_SCRIPT
        + "\nCOMP_WORDS=(" + words + ")\n"
        + f"COMP_CWORD={len(argv) - 1}\n"
        + "COMPREPLY=()\n_tan_complete\nprintf 'vf=%s\\n' \"$vf\"\n"
    )
    proc = sp.run(  # noqa: S603, S607 -- fixed script, no shell metacharacters
        ["bash", "-c", script], capture_output=True, text=True, timeout=10
    )
    assert "vf=untouched" in proc.stdout.splitlines(), proc.stdout


# ---------------------------------------------------------------------------
# tan-cli#503: the zsh counterpart of the subcommand-scan loop above. Unlike
# `_bash_complete`, which drives the emitted script in a real `bash -c`, the
# zsh loop had no live-shell test at all -- every zsh assertion elsewhere in
# this file is a static string check on `ZSH_SCRIPT`'s text, never an actual
# `zsh` parsing and running it. This drives the REAL snippet (lines lifted
# verbatim out of `ZSH_SCRIPT`, not retyped) in a real `zsh`.
# ---------------------------------------------------------------------------


def _zsh_available() -> bool:
    """Unlike `bash` (see `_bash_available`), a plain `which` IS proof here:
    Windows has no `zsh.exe` launcher stub anywhere on the default `PATH` --
    the WSL-installer-provided stub is specifically `%SystemRoot%\\System32\\
    bash.exe` (and `wsl.exe`/`wslconfig.exe`), never a `zsh.exe`, and
    `windows-latest`/`macos-latest`/`ubuntu-latest` GitHub runner images carry
    no other program named `zsh` that could shadow the real one. Verified,
    not assumed."""
    import shutil as _shutil

    return _shutil.which("zsh") is not None


def _zsh_subcmd_scan_snippet() -> str:
    """The `subcmd` scan loop plus the `case "$subcmd"` that picks the wide
    vs narrow `--format` value list, extracted VERBATIM from the emitted
    `ZSH_SCRIPT` (between `local subcmd="" i=2` and the `_arguments -C` call
    that follows it) -- so this exercises the committed source, not a
    hand-written stand-in for it."""
    lines = ZSH_SCRIPT.splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip().startswith('local subcmd='))
    # `startswith`, not `in`: a comment a few lines ABOVE `start` also mentions
    # "`_arguments -C`" in prose, and a substring search from index 0 matches
    # that comment first, well before `start` -- yielding a negative/empty
    # slice and an always-empty snippet.
    end = next(
        i
        for i, line in enumerate(lines)
        if i > start and line.strip().startswith("_arguments -C")
    )
    return "\n".join(lines[start:end])


def _zsh_scan(argv: list[str]) -> tuple[str, bool]:
    """Run `_zsh_subcmd_scan_snippet()` in a real `zsh -f -c` and return
    `(subcmd, wide)`: `subcmd` is what the loop resolved `$words[i]` to, and
    `wide` is whether the `case "$subcmd"` arm it took spliced in the
    IDE-oriented `--format` values (`diagnostic-v1`/`sarif`)."""
    import subprocess as sp

    words = " ".join(f'"{w}"' for w in argv)
    body = _zsh_subcmd_scan_snippet()
    script = (
        "myfunc() {\n"
        f"  local -a words=({words})\n"
        "  local -a global_args=()\n"
        f"{body}\n"
        '  print -r -- "SUBCMD=$subcmd"\n'
        '  if [[ "${global_args[*]}" == *diagnostic-v1* ]]; then\n'
        '    print -r -- "WIDE=1"\n'
        "  else\n"
        '    print -r -- "WIDE=0"\n'
        "  fi\n"
        "}\nmyfunc\n"
    )
    proc = sp.run(  # noqa: S603, S607 -- fixed script, no shell metacharacters
        ["zsh", "-f", "-c", script], capture_output=True, text=True, timeout=10
    )
    assert proc.returncode == 0, proc.stderr
    fields = dict(line.split("=", 1) for line in proc.stdout.splitlines() if "=" in line)
    return fields.get("SUBCMD", ""), fields.get("WIDE") == "1"


@pytest.mark.skipif(not _zsh_available(), reason="no zsh on this host")
def test_zsh_format_completion_finds_the_subcommand_past_a_leading_global_flag():
    """`tan --sdk-root /x validate --format <TAB>` must still resolve
    `validate` as the subcommand (not `--sdk-root`, its value, or the empty
    about-to-be-typed word) and therefore pick the WIDE `--format` list."""
    subcmd, wide = _zsh_scan(["tan", "--sdk-root", "/x", "validate", "--format", ""])
    assert subcmd == "validate"
    assert wide is True


@pytest.mark.skipif(not _zsh_available(), reason="no zsh on this host")
def test_zsh_format_completion_still_works_with_no_leading_flag():
    """Regression guard: the ordinary, no-global-flag shape must still work
    after the scan replaces a fixed-index read."""
    subcmd, wide = _zsh_scan(["tan", "validate", "--format", ""])
    assert subcmd == "validate"
    assert wide is True


@pytest.mark.skipif(not _zsh_available(), reason="no zsh on this host")
def test_zsh_format_completion_narrow_command_still_gets_the_narrow_list():
    """`size` is not one of the IDE-oriented commands, so it must keep the
    narrow `text json` list even past a leading global flag."""
    subcmd, wide = _zsh_scan(["tan", "--sdk-root", "/x", "size", "--format", ""])
    assert subcmd == "size"
    assert wide is False


# ---------------------------------------------------------------------------
# tan-cli#503: fish `generate --target` must list every real target value.
# ---------------------------------------------------------------------------


def test_fish_target_completion_lists_every_valid_generate_target():
    from tan.commands.generate_cmd import (
        ALL_EMIT_MODES,
        COMPOSED_ROUTE_TABLE,
        IPC_CONTRACT_H,
        ZEPHYR_BOARD,
    )

    line = next(line for line in FISH_SCRIPT.splitlines() if line.startswith(
        "complete -c tan -l target -d"
    ))
    listed = set(line.rsplit("-a '", 1)[1].rstrip("'").split())
    expected = set(ALL_EMIT_MODES) | {ZEPHYR_BOARD, COMPOSED_ROUTE_TABLE, IPC_CONTRACT_H}
    assert listed == expected
    # The three the reviewer's evidence named as missing, by name (a stronger
    # assertion than the set-equality above, so a future refactor that
    # accidentally drops one still fails loudly here even if it also drops it
    # from `expected`'s own source).
    assert "os-topology" in listed
    assert "composed-route-table" in listed
    assert "ipc-contract-h" in listed


# ---------------------------------------------------------------------------
# tan-cli#503: bash's OTHER two fixed-index reads. The `--format` value list
# above was the first consumer of the subcommand scan; the `$cword -eq 1`
# subcommand gate and the per-command flag `case` still read
# `${COMP_WORDS[1]}`, so a leading global flag made both miss entirely. These
# were the frozen oracle's own bytes -- `crates/` was deleted in tan-cli#269,
# so this port owns them now.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _bash_available(), reason="no bash on this host")
def test_bash_completion_offers_subcommands_past_a_leading_global_flag():
    """`tan --sdk-root /x <TAB>` used to offer not one of the 31 subcommand
    names: the gate that emits them was `[[ $cword -eq 1 ]]`, and `--sdk-root
    /x` puts the cursor at word 3."""
    reply = _bash_complete(["tan", "--sdk-root", "/x", ""])
    assert "validate" in reply
    assert "size" in reply
    assert "faultdecode" in reply


@pytest.mark.skipif(not _bash_available(), reason="no bash on this host")
def test_bash_completion_offers_per_command_flags_past_a_leading_global_flag():
    """`tan --sdk-root /x size --<TAB>` used to fall to the `*)` arm, because
    `${COMP_WORDS[1]}` was `--sdk-root` rather than `size`, so none of
    `size`'s own three flags were offered."""
    reply = _bash_complete(["tan", "--sdk-root", "/x", "size", "--"])
    assert "--build-root" in reply
    assert "--board" in reply
    assert "--fail-over-budget" in reply


@pytest.mark.skipif(not _bash_available(), reason="no bash on this host")
def test_bash_completion_still_offers_subcommands_at_the_first_word():
    """Regression guard for the shape that already worked: replacing `[[
    $cword -eq 1 ]]` with the scan must not lose the ordinary `tan <TAB>`."""
    reply = _bash_complete(["tan", ""])
    assert "validate" in reply
    assert "faultdecode" in reply


@pytest.mark.skipif(not _bash_available(), reason="no bash on this host")
def test_bash_completion_offers_subcommands_past_a_valueless_global_flag():
    """`tan --verbose <TAB>`: a boolean global flag consumes no following
    word, so the very next word IS the subcommand slot. This shape was broken
    the same way (`$cword` is 2, not 1) and is fixed by the same scan."""
    reply = _bash_complete(["tan", "--verbose", ""])
    assert "validate" in reply
    assert "size" in reply


@pytest.mark.skipif(not _bash_available(), reason="no bash on this host")
def test_bash_completion_does_not_offer_subcommands_in_a_flag_value_slot():
    """`tan --sdk-root <TAB>` is completing `--sdk-root`'s VALUE -- a path --
    not a subcommand. The scan steps past the cursor word there, and that
    overshoot is what `at_value` detects: without it, the empty `$subcmd`
    would be read as "no subcommand typed yet" and the completion would start
    offering 31 command names where a directory belongs."""
    reply = _bash_complete(["tan", "--sdk-root", ""])
    assert "validate" not in reply
    assert "size" not in reply


@pytest.mark.skipif(not _bash_available(), reason="no bash on this host")
def test_bash_completion_offers_version_at_the_root_only():
    """`--version` is root-only and no subcommand prints a version for it.
    Measured on all 32: 29 answer Click's `No such option: --version
    (Possible options: --verbose)`; `lock` forwards it to `west`, which
    answers `unexpected arguments: ['--version']`; `quality` and `migrate`
    refuse earlier on their own required flag (`--profile`, and one of
    `--check`/`--preview`/`--apply`) and only reach `west` once that is
    supplied. The capture had it in the always-offered set, so tab-completion
    taught an argv no subcommand accepts."""
    assert "--version" in _bash_complete(["tan", ""])
    assert "--version" not in _bash_complete(["tan", "size", "--"])
    assert "--version" not in _bash_complete(["tan", "doctor", "--"])
    assert "--version" not in _bash_complete(["tan", "--sdk-root", "/x", "validate", "--"])
    # The control: `--verbose`, the flag Click suggests in its own rejection
    # message, IS accepted everywhere and must still be offered.
    assert "--verbose" in _bash_complete(["tan", "size", "--"])


@pytest.mark.skipif(not _bash_available(), reason="no bash on this host")
@pytest.mark.parametrize(
    "words, cword, wanted",
    [
        # `tan --sdk-root=/x size --<TAB>`: bash splits on $COMP_WORDBREAKS,
        # which contains `=`, so the function is handed SEVEN words, not five.
        (["tan", "--sdk-root", "=", "/x", "size", "--"], 5, "--build-root"),
        # `tan --sdk-root C:/proj size --<TAB>`: `:` is a wordbreak too.
        (["tan", "--sdk-root", "C", ":", "/proj", "size", "--"], 6, "--build-root"),
    ],
)
def test_bash_completion_survives_comp_wordbreaks_splitting(words, cword, wanted):
    """A skip-N positional scan lands on `=` or `:` and loses the subcommand.
    These `COMP_WORDS`/`COMP_CWORD` pairs are not constructed by hand -- they
    were captured from a real bash 5.2.21 under a pty by wrapping
    `_tan_complete` in a `complete -F` that dumps `${COMP_WORDS[*]}` and
    `$COMP_CWORD` before delegating.

    `--sdk-root=/x` is a supported argv, not a hypothetical: `tan
    --sdk-root=/nonexistent validate` parses and runs (exit 0, reporting no
    board.yaml)."""
    import subprocess as sp

    words_lit = " ".join(f'"{w}"' for w in words)
    script = (
        BASH_SCRIPT
        + "\nCOMP_WORDS=(" + words_lit + ")\n"
        + f"COMP_CWORD={cword}\n"
        + "COMPREPLY=()\n_tan_complete\nprintf '%s\\n' \"${COMPREPLY[@]}\"\n"
    )
    proc = sp.run(  # noqa: S603, S607 -- fixed script, no shell metacharacters
        ["bash", "-c", script], capture_output=True, text=True, timeout=10
    )
    reply = [line for line in proc.stdout.splitlines() if line]
    assert wanted in reply


@pytest.mark.skipif(not _bash_available(), reason="no bash on this host")
def test_bash_completion_offers_commands_after_an_equals_joined_flag():
    """`tan --sdk-root=/x <TAB>` -- the value is complete, so a subcommand may
    follow and the 32 names belong here. Captured split: `(tan --sdk-root =
    /x "")`, `COMP_CWORD=4`."""
    reply = _bash_complete(["tan", "--sdk-root", "=", "/x", ""])
    assert "validate" in reply
    assert "size" in reply


@pytest.mark.skipif(not _bash_available(), reason="no bash on this host")
def test_bash_completion_treats_the_split_equals_as_a_value_slot():
    """`tan --sdk-root=<TAB>` splits to `(tan --sdk-root = "")`, so `prev` is
    the separator rather than the flag. Still a value position: offering 32
    command names where a path belongs is the wrong answer either way."""
    reply = _bash_complete(["tan", "--sdk-root", "=", ""])
    assert "validate" not in reply
    assert "--project" in reply


@pytest.mark.skipif(not _bash_available(), reason="no bash on this host")
@pytest.mark.parametrize(
    "words",
    [
        # `tan --sdk-root size doctor --<TAB>` -- space form.
        ["tan", "--sdk-root", "size", "doctor", "--"],
        # `tan --sdk-root=size doctor --<TAB>` -- captured from real bash as
        # `(tan --sdk-root = size doctor --)`, `COMP_CWORD=5`. This is the
        # shape the `=`-separator skip exists for, and the ONLY one: with a
        # value that is not a command name, the bare-word fallthrough already
        # steps over it, so a mutant that deletes the skip survives every
        # other case. Measured both ways -- with the skip this completes
        # `--build`; without it, `--build-root --fail-over-budget`, i.e. the
        # value `size` taken for the subcommand.
        ["tan", "--sdk-root", "=", "size", "doctor", "--"],
    ],
    ids=["space", "equals"],
)
def test_bash_completion_prefers_a_positional_value_over_a_name_collision(words):
    """The scan steps over a value-taking flag's value POSITIONALLY before it
    ever consults the command list, so a `--sdk-root` whose value happens to
    be spelled like a subcommand still resolves the real subcommand.
    Membership alone would have answered `size`."""
    reply = _bash_complete(words)
    assert "--build" in reply
    assert "--fix" in reply
    assert "--build-root" not in reply  # i.e. it did not resolve `size`
    assert "--fail-over-budget" not in reply


def test_zsh_args_state_dispatches_on_the_scanned_subcommand():
    """tan-cli#503 / zsh. The `args` state dispatched on `$words[2]`, but in
    that state `_arguments` has REINDEXED `words`: `words[1]` is the
    subcommand and `words[2]` is the word being completed. Instrumented in
    zsh 5.9 under a pty (a `print -r` of the live state appended to a file
    from inside the `args` arm of the EMITTED script):

        ARGS-STATE words=(size --) CURRENT=2 words2=[--] subcmd_scan=[size]

    So `$words[2]` was `--` and matched no arm -- all 21 fell through to
    `*)`. Driven end to end in the same zsh, before -> after:

        tan size --<TAB>                  12 global flags
                                       -> + --board --build-root --fail-over-budget
        tan doctor --<TAB>                12 global flags
                                       -> + --build --fix
        tan --sdk-root /x size --<TAB>    12 global flags
                                       -> + --board --build-root --fail-over-budget

    `$subcmd` is the value the same probe printed as correct, and it is the
    one bash uses, so the two shells now agree by construction. This assertion
    is static because a compinit-under-a-pty harness is not something to run
    in CI; the live run above is what established the behaviour."""
    lines = ZSH_SCRIPT.splitlines()
    args_arm = next(i for i, line in enumerate(lines) if line.strip() == "args)")
    dispatch = next(
        line.strip() for line in lines[args_arm:] if line.strip().startswith("case ")
    )
    assert dispatch == "case $subcmd in"
    assert "case $words[2] in" not in ZSH_SCRIPT


def test_bash_subcommand_flag_list_matches_the_declared_global_surface():
    """The bash script's per-subcommand `$global_flags` must equal the one
    table the parser itself reads (`tan.core.global_flags.GLOBAL_FLAGS`) plus
    `--format` and `--help`, which are declared separately -- and `$root_flags`
    must add exactly `--version` on top. Pinning it to `GLOBAL_FLAGS` rather
    than to a retyped literal is what stops the script and the parser drifting
    a second time."""
    from tan.core.global_flags import GLOBAL_FLAGS

    def _list(name: str) -> list[str]:
        line = next(
            line for line in BASH_SCRIPT.splitlines()
            if line.strip().startswith(f"local {name}=")
        )
        return line.split('="', 1)[1].rstrip('"').split()

    global_flags = _list("global_flags")
    assert set(global_flags) == set(GLOBAL_FLAGS) | {"--format", "--help"}
    assert "--version" not in global_flags
    assert _list("root_flags") == ["$global_flags", "--version"]


def test_zsh_offers_version_at_the_root_only():
    """zsh's `$global_args` is spliced into every per-subcommand `_arguments`
    arm, so an entry there is offered on all 32. `--version` is now spliced
    into the root `_arguments -C` call instead, and appears nowhere else."""
    lines = ZSH_SCRIPT.splitlines()
    carriers = [line for line in lines if "--version[Show version]" in line]
    assert len(carriers) == 1, carriers
    assert carriers[0].strip().startswith("_arguments -C")
    assert carriers[0].rstrip().endswith("'--version[Show version]'")
    # Control: `--help` IS accepted on every subcommand and must stay in the
    # per-arm set.
    assert any(line.strip() == "'--help[Show help]'" for line in lines)


def test_fish_offers_version_at_the_root_only():
    """fish's flag completions are unconditional unless given an `-n`
    condition; `--version` now carries the same "no subcommand typed yet"
    condition as the command list.

    Driven in fish 3.7.0: `complete -C 'tan --'` lists
    `--version\\tShow version`, and `complete -C 'tan size --'` matches
    `version` zero times."""
    line = next(
        line for line in FISH_SCRIPT.splitlines() if line.endswith("-l version -d 'Show version'")
    )
    assert line == (
        f"complete -c tan -n 'not __fish_seen_subcommand_from {_COMMAND_NAMES}' "
        "-l version -d 'Show version'"
    )
    # Control: `--help` is accepted everywhere and stays unconditional.
    assert "complete -c tan -l help -d 'Show help'" in FISH_SCRIPT


def test_fish_offers_the_command_list_past_a_global_flag_value():
    """tan-cli#503 / fish. The command list was gated on
    `__fish_use_subcommand`, whose own body returns 1 at the first token that
    does not start with `-` -- and a global flag's VALUE is exactly that. So
    with `tan --sdk-root /x ` typed, the list was suppressed; and because fish
    offers options only when the current token starts with `-`, the result was
    NOTHING at all, not even a flag.

    Measured in fish 3.7.0 with the emitted script and the real `__fish_*`
    helpers, `complete -C '...' | count`:

        tan                      32 -> 32
        tan --sdk-root /x         0 -> 32
        tan --sdk-root=/x         0 -> 32
        tan --verbose             0 -> 32
        tan validate              0 ->  0   (control: not re-offered)

    `__fish_seen_subcommand_from` scans every word for a member of the list,
    so a flag value no longer suppresses it. The residual case it shares with
    the rest of the script: a value that IS a command name (`tan --sdk-root
    size `) suppresses the list -- the same trade-off every other
    `__fish_seen_subcommand_from` line in this script already makes."""
    line = next(
        line for line in FISH_SCRIPT.splitlines() if line.startswith("complete -c tan -n") and " -a '" in line
    )
    assert line == (
        f"complete -c tan -n 'not __fish_seen_subcommand_from {_COMMAND_NAMES}' "
        f"-a '{_COMMAND_NAMES}'"
    )
    assert "__fish_use_subcommand" not in FISH_SCRIPT


def test_spliced_command_names_match_the_registered_command_surface():
    """`_COMMAND_NAMES` is spliced into fish twice (the `-a` list and the
    `not __fish_seen_subcommand_from` condition), so it must not drift from
    what `tan.cli` actually registers. `completion_cmd` cannot import
    `tan.cli` (import cycle -- see its docstring); this test can."""
    from tan.cli import _SUBCOMMAND_NAMES

    assert set(_COMMAND_NAMES.split()) == set(_SUBCOMMAND_NAMES)
    assert len(_COMMAND_NAMES.split()) == 31


def test_subcommand_format_overrides_a_leading_root_format():
    """`--format` declared after the subcommand name still wins over a
    leading root-position value, matching `debug_config_cmd.debug_config`'s
    identical `output_format or ctx.obj...` precedence (here spelled `is not
    None`, per this file's own fixed version of that fallback)."""
    root_app = typer.Typer()

    @root_app.callback(invoke_without_command=True)
    def _root(ctx: typer.Context, output_format: str = typer.Option(None, "--format")) -> None:
        ctx.obj = {"format": output_format}

    root_app.command("completion")(completion)
    result = runner.invoke(
        root_app, ["--format", "json", "completion", "--shell", "bash", "--format", "text"]
    )
    assert result.exit_code == 0
    assert result.stdout == BASH_SCRIPT + "\n"
