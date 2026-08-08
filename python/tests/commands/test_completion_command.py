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
    assert "__fish_use_subcommand" in FISH_SCRIPT
    # Every script ends in exactly one trailing newline (this command's own
    # `print(script)` adds the second one the oracle's stdout capture shows).
    for script in (BASH_SCRIPT, ZSH_SCRIPT, FISH_SCRIPT):
        assert script.endswith("\n")
        assert not script.endswith("\n\n")


def test_embedded_scripts_list_every_registered_subcommand():
    """Drift guard: every verb `tan.cli` registers must tab-complete on all
    three shells. Reads `tan.cli._SUBCOMMAND_NAMES` (a frozenset this file
    only imports, never edits) rather than hand-duplicating the 32-name list a
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
    trusting presence on `PATH`."""
    import shutil as _shutil
    import subprocess as _sp

    if _shutil.which("bash") is None:
        return False
    try:
        proc = _sp.run(  # noqa: S603, S607 -- fixed args, no shell metacharacters
            ["bash", "-c", "echo tan-bash-ok"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except OSError:
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
