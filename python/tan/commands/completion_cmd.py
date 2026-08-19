# SPDX-License-Identifier: Apache-2.0
"""`tan completion` -- emit a shell completion script for bash, zsh, or fish.

Mirrors `crates/tan-cli/src/commands/completion.rs`. The three scripts below
are embedded verbatim: byte-for-byte captures of the reference Rust oracle's
own `data.script` field (`target/debug/tan.exe completion --shell
<bash|zsh|fish> --format json`), the same "captured, not generated" contract
the oracle's own module docstring describes (there it is `include_str!` over a
committed `.bash`/`.zsh`/`.fish` file; here it is a literal, since this unit's
file allowlist is `completion_cmd.py` alone).

**One deliberate exception to that, tan-cli#403: the `--format` value lists.**
The captures hardcoded `text json` in all three scripts, so `tan validate
--format <TAB>` offered two of the four values `validate` really accepts and
actively taught an IDE integrator that `diagnostic-v1` and `sarif` do not
exist. Those lists are now placeholders spliced from `tan.output_format`'s
enums by `_fill_formats` -- the same declarations the parser and `--help`
derive from, so the surfaces cannot disagree again. Everything else in the
scripts is still the oracle's own bytes; the emitted `data.script` therefore
diverges from v0.4.1's exactly where v0.4.1 was wrong about this port's own
format/target surface.

**A second splice, tan-cli#503: `generate --target`'s value list.** The fish
capture hardcoded 9 of the 12 real `--target` values, silently missing
`os-topology` (a member of the default/`--all` set, so a bare `tan generate`
runs it), `composed-route-table` and `ipc-contract-h` (both explicit-only, but
real). Also spliced from `generate_cmd`'s own tables now, same mechanism and
same reasoning as `--format` above. bash and zsh publish no `--target` value
list at all, so fish was (and is) the only surface this touches.

**A third, hand-edited exception, tan-cli#503: `doctor` drops
`--target-kind`/`--server`.** The oracle's `doctor` declared both
(`crates/tan-cli/src/cli.rs`), so the captured scripts still offer them, but
`doctor_cmd.py` deliberately did NOT port that debug half (see its own module
docstring) -- `support-bundle` and `debug-config` genuinely still take both.
Completing a flag `tan doctor` rejects at exit 2 is worse than not offering
it, so the three `doctor` arms had the two flags (and, in zsh/fish, their
value lists) removed by hand.

**A fourth, hand-edited exception, tan-cli#503: none of the three scripts
found the subcommand reliably.** All three defects are the same shape -- a
positional or state assumption that a real invocation breaks -- and all three
were measured in a real shell (bash 5.2.21, zsh 5.9 under a pty, fish 3.7.0
via `complete -C`), before and after.

- **bash keyed every decision off `${COMP_WORDS[1]}`**, which is the
  subcommand only when nothing precedes it. Before: `tan --sdk-root /x <TAB>`
  offered not one of the 31 command names (the `$cword -eq 1` gate missed) and
  `tan --sdk-root /x size --<TAB>` offered no
  `--build-root`/`--board`/`--fail-over-budget` (the per-command `case`
  missed). Both now read one `$subcmd` scanned once at the top of
  `_tan_complete`. That scan also has to survive `$COMP_WORDBREAKS`: bash
  re-splits argv on `=` and `:`, so `--sdk-root=/x` reaches the function as
  three words and `C:/proj` as three more -- see the scan's own comment.
- **zsh's `args`-state `case $words[2]` never matched anything.** In that
  state `_arguments` has REINDEXED `words`, so `words[1]` is the subcommand
  and `words[2]` is the word being completed: instrumented in zsh 5.9,
  `tan size --<TAB>` arrived with `words=(size --)`, `CURRENT=2`. All 21 arms
  were unreachable and every subcommand fell to `*)` -- `tan size --<TAB>`
  offered the 12 global flags and none of `size`'s own three, `tan doctor
  --<TAB>` offered neither `--build` nor `--fix`. Now `case $subcmd`, the same
  value bash uses, which that probe confirmed is in scope and correct there
  (`subcmd_scan=size`).
- **fish's command list was gated on `__fish_use_subcommand`**, which returns
  false at the first non-switch token -- and a global flag's VALUE is one. So
  `complete -C 'tan --sdk-root /x '` returned NOTHING at all: no command
  names, and no flags either, because fish offers options only when the
  current token starts with `-`. Now gated on `not
  __fish_seen_subcommand_from <the 31>`, which ignores flag values. (fish's
  per-command flags were already correct -- `__fish_seen_subcommand_from`
  scans every word -- so only the command-list half was broken.)

The `$cword -eq 1` gate, the per-command `case`es and the fish condition were
all the oracle's own captured bytes; `crates/` was deleted in tan-cli#269, so
this port owns them now and they are fixed rather than preserved.

**A fifth, hand-edited exception, tan-cli#503: `--version` is offered at the
root only.** All three captures listed it in the always-available flag set, so
it was offered on all 31 subcommands, and no subcommand prints a version for
it: measured, 29 answer Click's `No such option: --version (Possible options:
--verbose)`, `lock` forwards it to `west` (`unexpected arguments:
['--version']`), and `quality`/`migrate` refuse earlier on their own required
flag and only reach `west` once that is supplied. It is root-only by design
(`cli.py`: "it lives on `Cli` directly in clap, not `GlobalArgs`, and is
root-only on both sides already"), and `tan.core.global_flags.GLOBAL_FLAGS` --
the one table the parser and `accept_global_flags` both read -- does not
contain it. bash splits `root_flags` out of `global_flags`; zsh splices it
into the root `_arguments -C` call and no arm; fish gates it on the same
`not __fish_seen_subcommand_from` condition as the command list. Inherited
from the oracle's capture, same as the reads above, and owned here for the
same reason.

**Why hand-captured and not Typer/Click's own shell-completion machinery.**
Typer ships one (`click.shell_completion`, gated off here via `app =
typer.Typer(add_completion=False)` in `cli.py`), but it is not a substitute:
it activates through a completely different mechanism -- sourcing eval output
from an `_TAN_COMPLETE=<shell>_source tan` environment-variable trigger
Click's own dispatcher special-cases at import time, not a static script this
command prints -- and it introspects THIS PROCESS's live Click command tree
rather than emitting the oracle's fixed command/flag tables. Even
functionally equivalent tab-completion from it would not reproduce
`data.script` byte-for-byte, which is the wire contract this command's JSON
envelope carries (an extension or script that diffs/hashes that field would
see every invocation as a regression). Hand-captured scripts are therefore
the only way to be a faithful port here, not a shortcut around one.

Every failure is an envelope, never a traceback: the ONLY failure this
command has is an unsupported `--shell` value, mirroring `resolve_shell` in
the Rust 1:1 (default `bash`; trim + lowercase; anything else is
`completion.shell-unsupported`, exit `RUNTIME_FAILURE`). There is no project
resolution, no SDK checkout, no filesystem read and no subprocess -- `project`
stays `null`/`null` in every envelope, matching the oracle's `null_project()`.
"""

from __future__ import annotations

import sys

import typer

from tan.commands.generate_cmd import (
    ALL_EMIT_MODES,
    COMPOSED_ROUTE_TABLE,
    IPC_CONTRACT_H,
    ZEPHYR_BOARD,
)
from tan.envelope import Envelope, Issue, Project, emit
from tan.exit_codes import ExitCode
from tan.output_format import (
    FORMAT_HELP,
    WIDE_FORMAT_COMMANDS,
    OutputFormat,
    ValidateOutputFormat,
    format_values,
    resolve_format,
)

#: `data.schemaVersion` for this command's payload.
DATA_SCHEMA_VERSION = "1"

#: The frozen issue code for an unsupported `--shell` value
#: (`contract/issue-codes.json`; `emittedBy` there already names this file).
SHELL_UNSUPPORTED_CODE = "completion.shell-unsupported"
#: Verbatim from `completion.rs`'s `Issue.message` -- the JSON-mode wording.
SHELL_UNSUPPORTED_MESSAGE = "Unsupported shell. Allowed values: bash, zsh, fish."
#: Verbatim from `completion.rs`'s `text` line -- the text-mode (stderr) wording.
SHELL_UNSUPPORTED_TEXT_LINE = "completion: unsupported shell. Use --shell bash|zsh|fish."

#: Where the captured scripts had a hardcoded `--format` value list, they now
#: carry a placeholder `_fill_formats` (below) substitutes from the CLI's own
#: enums -- tan-cli#403. The scripts are otherwise still byte-for-byte oracle
#: captures; these five markers are the ONLY derived text in them.
_FORMATS_MARK = "@FORMATS@"
_WIDE_FORMATS_MARK = "@WIDE_FORMATS@"
#: The commands with the wider domain, as a bash/zsh `case` alternation
#: (`a|b`) and as fish's space-separated argument list respectively.
_WIDE_COMMANDS_ALT_MARK = "@WIDE_COMMANDS_ALT@"
_WIDE_COMMANDS_LIST_MARK = "@WIDE_COMMANDS_LIST@"
#: `generate --target`'s value list (tan-cli#503): the fish capture hardcoded
#: 9 of the 12 real values, missing `os-topology` (a bare `tan generate`
#: default target) plus the explicit-only `composed-route-table` and
#: `ipc-contract-h`. Spliced from `generate_cmd`'s own tables, same as the
#: `--format` lists above, so this cannot go stale a second time.
_TARGETS_MARK = "@TARGETS@"
#: `ALL_EMIT_MODES` is the default/`--all` set; `ZEPHYR_BOARD`,
#: `COMPOSED_ROUTE_TABLE` and `IPC_CONTRACT_H` are the explicit-only targets
#: it excludes (`generate_cmd.EXPLICIT_ONLY_TARGETS`) but `--target` still
#: accepts by name.
_TARGET_VALUES = " ".join(
    (*ALL_EMIT_MODES, ZEPHYR_BOARD, COMPOSED_ROUTE_TABLE, IPC_CONTRACT_H)
)
#: The 31 subcommand names, in the captured scripts' own order (tan-cli#503).
#: fish needs them TWICE -- once as the command list it offers, and once as
#: the `not __fish_seen_subcommand_from ...` condition that decides whether a
#: subcommand has been typed yet -- so they are spliced rather than written
#: out a second time. `tests/commands/test_completion_command.py` diffs this
#: against `tan.cli._SUBCOMMAND_NAMES`; the module itself cannot import
#: `tan.cli` (import cycle -- see the docstring), the test can.
_COMMANDS_MARK = "@COMMANDS@"
_COMMAND_NAMES = (
    "validate generate init scaffold examples doctor completion diff presets "
    "pinmux explain inspect trace debug-config support-bundle sdk bootstrap "
    "build kconfig image flash run clean size migrate lock quality "
    "model monitor new-som faultdecode"
)


def _fill_formats(template: str) -> str:
    """Splice the declared `--format`/`--target` value lists into one captured
    script.

    The oracle generated its completions from the same clap `ValueEnum` that
    fed its parser and its `--help`, so all three surfaces agreed by
    construction. This port emits TEXT rather than walking a live command
    graph (`completion_cmd` cannot import `tan.cli` -- that is an import
    cycle), so the equivalent guarantee is: never type a value list into the
    script text, splice it from the enum, and let
    `tests/commands/test_output_format.py` diff the emitted script against
    what the parser really accepts, per command.
    """
    return (
        template.replace(_FORMATS_MARK, " ".join(format_values(OutputFormat)))
        .replace(_WIDE_FORMATS_MARK, " ".join(format_values(ValidateOutputFormat)))
        .replace(_WIDE_COMMANDS_ALT_MARK, "|".join(WIDE_FORMAT_COMMANDS))
        .replace(_WIDE_COMMANDS_LIST_MARK, " ".join(WIDE_FORMAT_COMMANDS))
        .replace(_TARGETS_MARK, _TARGET_VALUES)
        .replace(_COMMANDS_MARK, _COMMAND_NAMES)
    )


#: Verbatim bash completion script, captured from the reference oracle.
_BASH_TEMPLATE = """# tan CLI bash completion
_tan_complete() {
  local cur prev words cword

  COMPREPLY=()
  cur="${COMP_WORDS[COMP_CWORD]}"
  prev="${COMP_WORDS[COMP_CWORD-1]}"
  cword=${COMP_CWORD}

  local commands="validate generate init scaffold examples doctor completion diff presets pinmux explain inspect trace debug-config support-bundle sdk bootstrap build kconfig image flash run clean size migrate lock quality model monitor new-som faultdecode"
  # `--version` is deliberately NOT in `global_flags` but IS in `root_flags`
  # (tan-cli#503): it is root-only, and no subcommand prints a version for it
  # -- see the module docstring's fifth exception for the measurement on all
  # 32. Known and NOT fixed here: the root list is offered wherever no
  # subcommand has been typed, yet the bare root command accepts far less than
  # it offers (`tan --sdk-root /x --version`, `tan --verbose --version` and
  # `tan --ci --version` all exit 2 -- `cli._reorder_global_flags` relocates a
  # global flag ONTO the subcommand, and there is none here). Narrowing that
  # list changes what `tan <TAB>` offers, which is beyond tan-cli#503's
  # report, and it behaves identically before and after this change.
  local global_flags="--project --board-yaml --sdk-root --target --all --format --verbose --quiet --no-color --non-interactive --ci --help"
  local root_flags="$global_flags --version"

  # ONE subcommand scan, three consumers below (tan-cli#503). A fixed word
  # index is only the subcommand when nothing precedes it -- but every global
  # flag is typeable BEFORE the subcommand (`tan --sdk-root /x validate
  # --format <TAB>`, the exact pre-subcommand shape `output_format.py`'s
  # docstring names as the one alp-sdk-vscode's `withSdkRoot` actually uses),
  # so word 1 there is `--sdk-root`. That mis-selected the `--format` value
  # list (fixed already), AND made `$cword -eq 1` miss, so `tan --sdk-root /x
  # <TAB>` offered not one subcommand name, AND made the per-command `case`
  # miss, so `tan --sdk-root /x size --<TAB>` offered no `--build-root`.
  # Scan for the real subcommand instead of assuming its position: skip every
  # global flag that consumes a following value, and the first bare word left
  # is the subcommand (or "" if none was typed yet).
  #
  # bash does NOT hand this function argv: it hands it argv re-split on
  # `$COMP_WORDBREAKS`, which contains `=` and `:` by default. Measured in
  # bash 5.2.21 under a pty, `tan --sdk-root=/x size --<TAB>` arrives as
  # `(tan --sdk-root = /x size --)` with `COMP_CWORD=5`, and
  # `tan --sdk-root C:/proj size --<TAB>` as `(tan --sdk-root C : /proj size
  # --)` with `COMP_CWORD=6`. Plain skip-N arithmetic lands on `=` or `:` in
  # both, so the scan has to step over a `=` that follows a value flag, and
  # to treat a bare word as the subcommand only when it IS one of the 31.
  # `--sdk-root=/x` is a supported argv (`tan --sdk-root=/nonexistent
  # validate` parses and runs), so this is not a hypothetical shape.
  local value_flags="--project --board-yaml --sdk-root --target --format"
  local subcmd="" at_value=0 i=1 w skip vf c
  while [[ $i -lt $cword ]]; do
    w="${COMP_WORDS[$i]}"
    if [[ "$w" == --*=* ]]; then
      # Self-contained; only reachable with a `$COMP_WORDBREAKS` that drops
      # `=`, since the default splits this into three words instead.
      i=$((i + 1))
      continue
    fi
    if [[ "$w" == --* ]]; then
      skip=0
      for vf in $value_flags; do
        [[ "$w" == "$vf" ]] && skip=1 && break
      done
      # `--flag` `=` `value`: step over the separator as well as the value.
      [[ $skip -eq 1 && "${COMP_WORDS[$((i + 1))]}" == "=" ]] && skip=2
      i=$((i + 1 + skip))
      continue
    fi
    for c in $commands; do
      [[ "$w" == "$c" ]] && subcmd="$w" && break
    done
    [[ -n "$subcmd" ]] && break
    # A bare word that is not a command name is a wordbreak fragment of some
    # value (`C`, `:`, `/proj`); step over it rather than mistake it for the
    # subcommand. The positional skip above still protects a value that
    # happens to BE a command name (`tan --sdk-root size validate`).
    i=$((i + 1))
  done
  # The word being completed sits in a flag's VALUE slot (`tan --sdk-root
  # <TAB>` -- a path, and `tan --sdk-root=<TAB>`, whose `prev` is the split-off
  # `=`), not the subcommand slot. Offering 31 command names there would be a
  # new wrong answer, so this shape keeps the pre-existing fall-through.
  for vf in $value_flags; do
    [[ "$prev" == "$vf" ]] && at_value=1 && break
  done
  [[ "$prev" == "=" ]] && at_value=1

  if [[ "$prev" == "--format" ]]; then
    local formats="@FORMATS@"
    case "$subcmd" in
      @WIDE_COMMANDS_ALT@) formats="@WIDE_FORMATS@" ;;
    esac
    COMPREPLY=( $(compgen -W "$formats" -- "$cur") )
    return
  fi

  if [[ "$prev" == "--shell" ]]; then
    COMPREPLY=( $(compgen -W "bash zsh fish" -- "$cur") )
    return
  fi

  if [[ -z "$subcmd" && $at_value -eq 0 ]]; then
    COMPREPLY=( $(compgen -W "$commands $root_flags" -- "$cur") )
    return
  fi

  case "$subcmd" in
    validate)
      COMPREPLY=( $(compgen -W "$global_flags --offline" -- "$cur") )
      ;;
    generate)
      COMPREPLY=( $(compgen -W "$global_flags --force --core" -- "$cur") )
      ;;
    explain)
      COMPREPLY=( $(compgen -W "$global_flags --template" -- "$cur") )
      ;;
    init)
      COMPREPLY=( $(compgen -W "$global_flags --template --from-example --name --destination --som --cores --preview --force" -- "$cur") )
      ;;
    scaffold)
      COMPREPLY=( $(compgen -W "$global_flags --template --name --destination --preview --force" -- "$cur") )
      ;;
    diff|presets)
      COMPREPLY=( $(compgen -W "$global_flags" -- "$cur") )
      ;;
    examples)
      COMPREPLY=( $(compgen -W "$global_flags --filter" -- "$cur") )
      ;;
    completion)
      COMPREPLY=( $(compgen -W "$global_flags --shell" -- "$cur") )
      ;;
    pinmux)
      COMPREPLY=( $(compgen -W "$global_flags --sku --family" -- "$cur") )
      ;;
    doctor)
      COMPREPLY=( $(compgen -W "$global_flags --build --fix" -- "$cur") )
      ;;
    inspect)
      COMPREPLY=( $(compgen -W "$global_flags --path --show-origin" -- "$cur") )
      ;;
    trace)
      COMPREPLY=( $(compgen -W "$global_flags --path" -- "$cur") )
      ;;
    debug-config)
      COMPREPLY=( $(compgen -W "$global_flags --target-kind --server --core --pre-launch-task --svd --preview" -- "$cur") )
      ;;
    support-bundle)
      COMPREPLY=( $(compgen -W "$global_flags --destination --target-kind --server --path" -- "$cur") )
      ;;
    sdk)
      COMPREPLY=( $(compgen -W "$global_flags list install current switch --destination --global" -- "$cur") )
      ;;
    bootstrap)
      COMPREPLY=( $(compgen -W "$global_flags --no-pip --no-west --print-env --allow-partial --workspace" -- "$cur") )
      ;;
    build)
      COMPREPLY=( $(compgen -W "$global_flags --plan --plan-from --materialise --native --manifest --manifest-from --no-auto-bootstrap --pristine" -- "$cur") )
      ;;
    kconfig)
      COMPREPLY=( $(compgen -W "$global_flags --core" -- "$cur") )
      ;;
    image)
      COMPREPLY=( $(compgen -W "$global_flags --build-root" -- "$cur") )
      ;;
    flash)
      COMPREPLY=( $(compgen -W "$global_flags --build-root --dry-run --core --helper --skip-missing-tools" -- "$cur") )
      ;;
    run)
      COMPREPLY=( $(compgen -W "$global_flags --flash --core" -- "$cur") )
      ;;
    clean)
      COMPREPLY=( $(compgen -W "$global_flags --build-root --dry-run" -- "$cur") )
      ;;
    size)
      COMPREPLY=( $(compgen -W "$global_flags --build-root --board --fail-over-budget" -- "$cur") )
      ;;
    *)
      COMPREPLY=( $(compgen -W "$global_flags" -- "$cur") )
      ;;
  esac
}

complete -F _tan_complete tan
"""

#: Verbatim zsh completion script, captured from the reference oracle.
_ZSH_TEMPLATE = """#compdef tan

_tan() {
  local -a commands
  commands=(
    'validate:Validate board.yaml config'
    'generate:Generate derived artifacts'
    'init:Initialize a starter project'
    'scaffold:Scaffold module files'
    'examples:List SDK example projects'
    'doctor:Run debug and environment checks'
    'completion:Generate shell completion script'
    'diff:Show board normalization diff'
    'presets:List SDK presets'
    'pinmux:Show pinmux capability table'
    'explain:Explain templates and targets'
    'inspect:Inspect effective resolved values'
    'trace:Trace generation decisions'
    'debug-config:Generate a launch.json debug configuration'
    'support-bundle:Export support bundle payload'
    'sdk:Manage local SDK installs'
    'bootstrap:Set up the SDK build environment'
    'build:Build the project natively'
    'kconfig:Show the board-scoped Kconfig symbol menu'
    'image:Assemble a flashable image bundle'
    'flash:Flash slices and helper MCUs onto the device'
    'run:Build then run the project'
    'clean:Remove the build dir and state cache'
    'size:Report per-slice firmware footprint'
    'migrate:Migrate board.yaml to the current schema'
    'lock:Pin/lock library dependencies'
    'quality:Run board.yaml quality checks'
    'model:Compile and package board.yaml models'
    'monitor:Open a serial console to the board'
    'new-som:Scaffold a new SoM metadata skeleton'
    'faultdecode:Decode an ARM Cortex-M fault dump'
  )

  # Every flag `GlobalArgs` marks `global = true` (cli.rs) is accepted by
  # clap on EVERY subcommand — AND on the root command itself, before any
  # subcommand word is even typed — so every arm below splices this in, and
  # so does the root `_arguments -C` call a few lines down. Unlike bash's
  # single `$global_flags` string var, zsh's per-arm `_arguments` has no
  # inheritance of its own (issue #92 MAJOR 2) — a flag left out of an arm
  # here is simply not completable for that subcommand (or, left out of the
  # root call, not completable at `tan --<TAB>` before a subcommand: issue
  # #92 round-3 FINDING 1).
  #
  # `--version` is the ONE flag that is not in that set (tan-cli#503): it is
  # root-only, refused by all 31 subcommands, so it is spliced into the root
  # `_arguments -C` call below and into no arm.
  local -a global_args
  global_args=(
    '--project[Project root]:path:_files -/'
    '--board-yaml[board.yaml path]:path:_files'
    '--sdk-root[SDK root]:path:_files -/'
    '--target[Generation target]'
    '--all[Generate all targets]'
    '--verbose[Verbose output]'
    '--quiet[Quiet output]'
    '--no-color[Disable color output]'
    '--non-interactive[Disable prompts]'
    '--ci[CI mode]'
    '--help[Show help]'
  )

  # `--format` is appended rather than listed above because its value list is
  # not the same for every command (tan-cli#403): `validate` also completes
  # the two IDE-oriented documents. Chosen here, once, so every `_arguments`
  # call below still splices exactly one `${global_args[@]}` and no arm can
  # drift.
  #
  # `$words[2]` is only the subcommand when nothing precedes it -- but
  # `--format` is itself a global flag typeable BEFORE the subcommand
  # (`tan --sdk-root /x validate --format <TAB>`), so this read at the
  # function top (before `_arguments` reindexes `words`) used to silently
  # fall back to the narrow list for that shape (tan-cli#503). Scan for the
  # real subcommand: skip every global flag that consumes a following value,
  # and the first bare word left is the subcommand.
  local subcmd="" i=2
  while (( i <= ${#words[@]} )); do
    case "$words[i]" in
      --project|--board-yaml|--sdk-root|--target|--format) (( i += 2 )); continue ;;
      --*) (( i += 1 )); continue ;;
    esac
    subcmd="$words[i]"
    break
  done
  case "$subcmd" in
    @WIDE_COMMANDS_ALT@) global_args+=('--format[Output format]:format:(@WIDE_FORMATS@)') ;;
    *) global_args+=('--format[Output format]:format:(@FORMATS@)') ;;
  esac

  _arguments -C     '1:command:->command'     '*::arg:->args'     "${global_args[@]}" '--version[Show version]'

  case $state in
    command)
      _describe 'command' commands
      ;;
    args)
      # `$subcmd`, not `$words[2]` (tan-cli#503). In this state `_arguments`
      # has REINDEXED `words` so that `words[1]` is the subcommand and
      # `words[2]` is the word being completed -- measured in zsh 5.9 under a
      # pty, `tan size --<TAB>` reaches here with `words=(size --)` and
      # `CURRENT=2`, so `$words[2]` was `--`. It never matched a subcommand
      # name, so all 21 arms below were unreachable and every command fell to
      # `*)`: `tan size --<TAB>` offered the 12 global flags and none of
      # `--build-root`/`--board`/`--fail-over-budget`. `$subcmd` is computed
      # from the ORIGINAL `words` at the top of this function and is still in
      # scope here (same probe: `subcmd_scan=size`), so it is right both for
      # this reindexing and for a leading global flag.
      case $subcmd in
        validate)
          _arguments '--offline[Offline structural validation only]' "${global_args[@]}"
          ;;
        completion)
          _arguments '--shell[Shell type]:shell:(bash zsh fish)' "${global_args[@]}"
          ;;
        generate)
          _arguments '--force[Overwrite existing files]' '--core[Core id (zephyr-board target)]' "${global_args[@]}"
          ;;
        explain)
          _arguments '--template[Template id]' "${global_args[@]}"
          ;;
        examples)
          _arguments '--filter[Substring match on id/title]' "${global_args[@]}"
          ;;
        init)
          _arguments '--template[Template id]' '--from-example[Example source dir]' '--name[Name value]' '--destination[Output directory]:path:_files -/' '--som[SoM SKU]' '--cores[Cores list]' '--preview[Preview only]' '--force[Overwrite existing files]' "${global_args[@]}"
          ;;
        scaffold)
          _arguments '--template[Template id]' '--name[Name value]' '--destination[Output directory]:path:_files -/' '--preview[Preview only]' '--force[Overwrite existing files]' "${global_args[@]}"
          ;;
        pinmux)
          _arguments '--sku[SoM SKU]' '--family[Pinmux family]' "${global_args[@]}"
          ;;
        doctor)
          _arguments '--build[Build readiness preflight]' '--fix[Auto-repair a fixable blocker]' "${global_args[@]}"
          ;;
        inspect)
          _arguments '--path[Field path]' '--show-origin[Include source metadata]' "${global_args[@]}"
          ;;
        trace)
          _arguments '--path[Field path]' "${global_args[@]}"
          ;;
        debug-config)
          _arguments '--target-kind[Debug target]:target:(zephyr-mcu baremetal-mcu yocto-userspace native-host)' '--server[Debug server]:server:(jlink openocd pyocd gdbserver none)' '--core[Build slice core id]' '--pre-launch-task[VS Code task to run before launching]' '--svd[Path to a user-supplied SVD for the peripheral view]:svd:_files -g "*.svd"' '--preview[Preview only]' "${global_args[@]}"
          ;;
        support-bundle)
          _arguments '--destination[Output directory]:path:_files -/' '--target-kind[Debug target]:target:(zephyr-mcu baremetal-mcu yocto-userspace native-host)' '--server[Debug server]:server:(jlink openocd pyocd gdbserver none)' '--path[Field path]' "${global_args[@]}"
          ;;
        sdk)
          _arguments '1:subcommand:(list install current switch)' '--destination[Cache root]:path:_files -/' '--global[Pin the machine-global default]' "${global_args[@]}"
          ;;
        bootstrap)
          _arguments '--no-pip[Skip pip install]' '--no-west[Skip west init/update]' '--print-env[Print environment lines only]' '--allow-partial[Report success despite a failed dependency install]' '--workspace[Build the workspace at this path]:path:_files -/' "${global_args[@]}"
          ;;
        build)
          _arguments '--plan[Show the build plan]' '--plan-from[Read build plan from file]:path:_files' '--materialise[Materialise plan files]' '--native[Build natively]' '--manifest[Show the system manifest]' '--manifest-from[Read manifest from file]:path:_files' '--no-auto-bootstrap[Never bootstrap implicitly]' '--pristine[Force-wipe build dirs before dispatch]' "${global_args[@]}"
          ;;
        kconfig)
          _arguments '--core[Core id to scope the menu to]' "${global_args[@]}"
          ;;
        image)
          _arguments '--build-root[Override build root]:path:_files -/' "${global_args[@]}"
          ;;
        flash)
          _arguments '--build-root[Override build root]:path:_files -/' '--dry-run[Print planned commands only]' '--core[Flash only this core]' '--helper[Flash only this helper MCU]' '--skip-missing-tools[Skip entries with no tool on PATH]' "${global_args[@]}"
          ;;
        run)
          _arguments '--flash[Flash the board after building]' '--core[Flash only this core]' "${global_args[@]}"
          ;;
        clean)
          _arguments '--build-root[Override build root]:path:_files -/' '--dry-run[List targets without removing]' "${global_args[@]}"
          ;;
        size)
          _arguments '--build-root[Override build root]:path:_files -/' '--board[Override SoM SKU]' '--fail-over-budget[Exit non-zero over budget]' "${global_args[@]}"
          ;;
        *)
          _arguments "${global_args[@]}"
          ;;
      esac
      ;;
  esac
}

compdef _tan tan
"""

#: Verbatim fish completion script, captured from the reference oracle.
_FISH_TEMPLATE = """complete -c tan -f
complete -c tan -n 'not __fish_seen_subcommand_from @COMMANDS@' -a '@COMMANDS@'
complete -c tan -l project -d 'Project root'
complete -c tan -l board-yaml -d 'board.yaml path'
complete -c tan -l sdk-root -d 'SDK root path'
complete -c tan -l target -d 'Generation target' -a '@TARGETS@'
complete -c tan -l all -d 'Generate all targets'
complete -c tan -n 'not __fish_seen_subcommand_from @WIDE_COMMANDS_LIST@' -l format -d 'Output format' -a '@FORMATS@'
complete -c tan -n '__fish_seen_subcommand_from @WIDE_COMMANDS_LIST@' -l format -d 'Output format' -a '@WIDE_FORMATS@'
complete -c tan -l verbose -d 'Verbose output'
complete -c tan -l quiet -d 'Quiet output'
complete -c tan -l no-color -d 'Disable color output'
complete -c tan -l non-interactive -d 'Disable prompts'
complete -c tan -l ci -d 'CI mode'
complete -c tan -l help -d 'Show help'
complete -c tan -n 'not __fish_seen_subcommand_from @COMMANDS@' -l version -d 'Show version'
complete -c tan -n '__fish_seen_subcommand_from validate' -l offline -d 'Offline structural validation only'
complete -c tan -n '__fish_seen_subcommand_from generate' -l force -d 'Overwrite existing files'
complete -c tan -n '__fish_seen_subcommand_from generate' -l core -d 'Core id (zephyr-board target)'
complete -c tan -n '__fish_seen_subcommand_from explain' -l template -d 'Template id'
complete -c tan -n '__fish_seen_subcommand_from examples' -l filter -d 'Substring match on id/title'
complete -c tan -n '__fish_seen_subcommand_from init scaffold' -l template -d 'Template id'
complete -c tan -n '__fish_seen_subcommand_from init' -l from-example -d 'Example source dir'
complete -c tan -n '__fish_seen_subcommand_from init scaffold' -l name -d 'Name value'
complete -c tan -n '__fish_seen_subcommand_from init scaffold' -l destination -d 'Destination path'
complete -c tan -n '__fish_seen_subcommand_from init' -l som -d 'SoM SKU'
complete -c tan -n '__fish_seen_subcommand_from init' -l cores -d 'Cores list'
complete -c tan -n '__fish_seen_subcommand_from init scaffold' -l preview -d 'Preview only'
complete -c tan -n '__fish_seen_subcommand_from init scaffold' -l force -d 'Overwrite existing files'
complete -c tan -n '__fish_seen_subcommand_from pinmux' -l sku -d 'SoM SKU'
complete -c tan -n '__fish_seen_subcommand_from pinmux' -l family -d 'Pinmux family'
complete -c tan -n '__fish_seen_subcommand_from doctor' -l build -d 'Build readiness preflight'
complete -c tan -n '__fish_seen_subcommand_from doctor' -l fix -d 'Auto-repair a fixable blocker'
complete -c tan -n '__fish_seen_subcommand_from inspect trace support-bundle' -l path -d 'Field path'
complete -c tan -n '__fish_seen_subcommand_from inspect' -l show-origin -d 'Include source metadata'
complete -c tan -n '__fish_seen_subcommand_from debug-config' -l target-kind -d 'Debug target kind' -a 'zephyr-mcu baremetal-mcu yocto-userspace native-host'
complete -c tan -n '__fish_seen_subcommand_from debug-config' -l server -d 'Debug server' -a 'jlink openocd pyocd gdbserver none'
complete -c tan -n '__fish_seen_subcommand_from debug-config' -l core -d 'Build slice core id'
complete -c tan -n '__fish_seen_subcommand_from debug-config' -l pre-launch-task -d 'VS Code task to run before launching'
complete -c tan -n '__fish_seen_subcommand_from debug-config' -l svd -r -d 'Path to a user-supplied SVD for the peripheral view'
complete -c tan -n '__fish_seen_subcommand_from debug-config' -l preview -d 'Preview only'
complete -c tan -n '__fish_seen_subcommand_from support-bundle' -l destination -d 'Destination path'
complete -c tan -n '__fish_seen_subcommand_from support-bundle' -l target-kind -d 'Debug target kind' -a 'zephyr-mcu baremetal-mcu yocto-userspace native-host'
complete -c tan -n '__fish_seen_subcommand_from support-bundle' -l server -d 'Debug server' -a 'jlink openocd pyocd gdbserver none'
complete -c tan -n '__fish_seen_subcommand_from completion' -l shell -d 'Shell type' -a 'bash zsh fish'
complete -c tan -n '__fish_seen_subcommand_from sdk' -a 'list install current switch'
complete -c tan -n '__fish_seen_subcommand_from sdk' -l destination -d 'Cache root'
complete -c tan -n '__fish_seen_subcommand_from sdk' -l global -d 'Pin the machine-global default'
complete -c tan -n '__fish_seen_subcommand_from bootstrap' -l no-pip -d 'Skip pip install'
complete -c tan -n '__fish_seen_subcommand_from bootstrap' -l no-west -d 'Skip west init/update'
complete -c tan -n '__fish_seen_subcommand_from bootstrap' -l print-env -d 'Print environment lines only'
complete -c tan -n '__fish_seen_subcommand_from bootstrap' -l allow-partial -d 'Report success despite a failed dependency install'
complete -c tan -n '__fish_seen_subcommand_from bootstrap' -l workspace -d 'Build the workspace at this path'
complete -c tan -n '__fish_seen_subcommand_from build' -l plan -d 'Show the build plan'
complete -c tan -n '__fish_seen_subcommand_from build' -l plan-from -d 'Read build plan from file'
complete -c tan -n '__fish_seen_subcommand_from build' -l materialise -d 'Materialise plan files'
complete -c tan -n '__fish_seen_subcommand_from build' -l native -d 'Build natively'
complete -c tan -n '__fish_seen_subcommand_from build' -l manifest -d 'Show the system manifest'
complete -c tan -n '__fish_seen_subcommand_from build' -l manifest-from -d 'Read manifest from file'
complete -c tan -n '__fish_seen_subcommand_from build' -l no-auto-bootstrap -d 'Never bootstrap implicitly'
complete -c tan -n '__fish_seen_subcommand_from build' -l pristine -d 'Force-wipe build dirs before dispatch'
complete -c tan -n '__fish_seen_subcommand_from kconfig' -l core -d 'Core id to scope the menu to'
complete -c tan -n '__fish_seen_subcommand_from image flash clean size' -l build-root -d 'Override build root'
complete -c tan -n '__fish_seen_subcommand_from flash' -l dry-run -d 'Print planned commands only'
complete -c tan -n '__fish_seen_subcommand_from flash' -l core -d 'Flash only this core'
complete -c tan -n '__fish_seen_subcommand_from flash' -l helper -d 'Flash only this helper MCU'
complete -c tan -n '__fish_seen_subcommand_from flash' -l skip-missing-tools -d 'Skip entries with no tool on PATH'
complete -c tan -n '__fish_seen_subcommand_from run' -l flash -d 'Flash the board after building'
complete -c tan -n '__fish_seen_subcommand_from run' -l core -d 'Flash only this core'
complete -c tan -n '__fish_seen_subcommand_from clean' -l dry-run -d 'List targets without removing'
complete -c tan -n '__fish_seen_subcommand_from size' -l board -d 'Override SoM SKU'
complete -c tan -n '__fish_seen_subcommand_from size' -l fail-over-budget -d 'Exit non-zero over budget'
"""

#: The three scripts as EMITTED: the captures above with their `--format`
#: value lists spliced in from the CLI's own enums (tan-cli#403).
BASH_SCRIPT = _fill_formats(_BASH_TEMPLATE)
ZSH_SCRIPT = _fill_formats(_ZSH_TEMPLATE)
FISH_SCRIPT = _fill_formats(_FISH_TEMPLATE)


def resolve_shell(raw: str | None) -> str | None:
    """Mirror Rust's `resolve_shell`: default `bash`; trim + lowercase; else
    `None` (unsupported)."""
    normalized = (raw if raw is not None else "bash").strip().lower()
    if normalized in ("bash", "zsh", "fish"):
        return normalized
    return None


def script_for(shell: str) -> str:
    """Select the embedded script for a resolved `shell` name. Mirrors Rust's
    `script_for`, including its fallback: an unrecognised value (unreachable
    from `completion()` below, since that already rejected it) falls back to
    bash rather than raising."""
    if shell == "zsh":
        return ZSH_SCRIPT
    if shell == "fish":
        return FISH_SCRIPT
    return BASH_SCRIPT


def _null_project() -> Project:
    """`completion` is project-agnostic: no root, no board.yaml, ever."""
    return Project(root=None, board_yaml=None)


def completion(
    ctx: typer.Context,
    shell: str = typer.Option(
        None,
        "--shell",
        metavar="SHELL",
        help="Target shell (bash, zsh, or fish). Defaults to bash.",
    ),
    project: str = typer.Option(
        None, "--project", metavar="PATH", help="Project root (defaults to '.')."
    ),
    board_yaml: str = typer.Option(
        None, "--board-yaml", metavar="PATH", help="Explicit board.yaml path."
    ),
    sdk_root: str = typer.Option(
        None, "--sdk-root", metavar="PATH", help="alp-sdk checkout root."
    ),
    output_format: OutputFormat = typer.Option(None, "--format", help=FORMAT_HELP),
    quiet: bool = typer.Option(False, "--quiet", hidden=True),
    verbose: bool = typer.Option(False, "--verbose", hidden=True),
    no_color: bool = typer.Option(False, "--no-color", hidden=True),
    non_interactive: bool = typer.Option(False, "--non-interactive", hidden=True),
    ci: bool = typer.Option(False, "--ci", hidden=True),
    target: str = typer.Option(None, "--target", hidden=True),
    all_targets: bool = typer.Option(False, "--all", hidden=True),
) -> None:
    """Emit a shell completion script (bash, zsh, or fish)."""
    # `project`/`board_yaml`/`sdk_root` are clap `GlobalArgs` (`global = true`)
    # this command never reads -- `completion` is project-agnostic on the
    # oracle too (see `_null_project` above). `quiet`/`verbose`/`no_color`/
    # `non_interactive`/`ci`/`target`/`all_targets` are the rest of that same
    # set: declared ONLY so the argv surface matches clap (`tan completion
    # --ci` must exit 0, not a Click usage error), never read. Mirrors
    # `clean_cmd.clean`'s identical block.
    del project, board_yaml, sdk_root
    del quiet, verbose, no_color, non_interactive, ci, target, all_targets

    # `--format` is accepted BEFORE the subcommand too (clap's `global =
    # true`; verified against the oracle: `tan --format json completion
    # --shell zsh` reaches this command and emits the envelope). The root
    # callback (`cli.py`) records a leading value on `ctx.obj`; this option
    # overrides it when repeated after the subcommand name -- that precedence
    # is `resolve_format`'s, written once for the eleven commands that honour
    # the leading position. An explicit `--format ""` still exits 2 (measured
    # against the oracle: `tan completion --format ""` -> rc 2); since
    # tan-cli#403 the declared enum refuses it at PARSE time, so no truthiness
    # test in this body can mistake it for an absent flag.
    resolved_format = resolve_format(output_format, ctx.obj, choices=OutputFormat)
    json_mode = resolved_format == "json"

    resolved_shell = resolve_shell(shell)
    if resolved_shell is None:
        if json_mode:
            emit(
                Envelope(
                    "completion",
                    _null_project(),
                    {"schemaVersion": DATA_SCHEMA_VERSION, "shell": "bash", "script": ""},
                    [Issue(SHELL_UNSUPPORTED_CODE, "error", SHELL_UNSUPPORTED_MESSAGE)],
                    ExitCode.RUNTIME_FAILURE,
                )
            )
        else:
            # stderr, like every other command's text-mode error line; stdout
            # stays empty on this path (matches the oracle, measured).
            print(SHELL_UNSUPPORTED_TEXT_LINE, file=sys.stderr)
        raise typer.Exit(int(ExitCode.RUNTIME_FAILURE))

    script = script_for(resolved_shell)
    if json_mode:
        emit(
            Envelope(
                "completion",
                _null_project(),
                {"schemaVersion": DATA_SCHEMA_VERSION, "shell": resolved_shell, "script": script},
                [],
                ExitCode.SUCCESS,
            )
        )
    else:
        # The script IS the payload (README: "tan completion --shell zsh
        # emits a completion script"), and the only sane way to consume it is
        # `eval "$(tan completion --shell zsh)"` / `> file` stdout capture --
        # so print it straight to stdout, not through the stderr-only
        # text-line convention every other command's text mode uses. Mirrors
        # `completion.rs`'s own `println!` plus its comment on why.
        print(script)
    raise typer.Exit(int(ExitCode.SUCCESS))
