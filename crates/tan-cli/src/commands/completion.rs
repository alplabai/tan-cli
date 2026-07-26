// SPDX-License-Identifier: Apache-2.0
//! `tan completion` — emit a shell completion script.
//!
//! Parity with TS `runCompletionCommand`: the scripts are embedded verbatim
//! via `include_str!` so the contract envelope's `script` field is exact. They
//! started as byte-for-byte captures of the reference TS CLI's output (12
//! commands); native-only commands (`build`, `flash`, `run`, …) have since
//! been added by hand, so `embedded_scripts_list_every_cli_command` below
//! checks them against `cli::Command` on every test run instead of trusting
//! them to stay in sync.

use super::CommandRun;
use crate::cli::{CompletionArgs, GlobalArgs};
use crate::envelope::{Envelope, Issue, Project};
use crate::exit::ExitCode;

/// Verbatim bash completion script, captured from the reference TS CLI.
const BASH_SCRIPT: &str = include_str!("completion_scripts/bash.bash");
/// Verbatim zsh completion script, captured from the reference TS CLI.
const ZSH_SCRIPT: &str = include_str!("completion_scripts/zsh.zsh");
/// Verbatim fish completion script, captured from the reference TS CLI.
const FISH_SCRIPT: &str = include_str!("completion_scripts/fish.fish");

/// Envelope `data` payload for `completion`: the resolved shell and its script.
#[derive(serde::Serialize)]
struct CompletionData {
    /// Data schema version, serialized as `schemaVersion`; always `"1"`.
    #[serde(rename = "schemaVersion")]
    schema_version: String,
    /// Resolved shell name (`bash`/`zsh`/`fish`).
    shell: String,
    /// The emitted completion script body.
    script: String,
}

/// Build an empty `Project` (no root / `board.yaml`); `completion` is project-agnostic.
fn null_project() -> Project {
    Project {
        root: None,
        board_yaml: None,
    }
}

/// Mirror TS `resolveShell`: default `bash`; trim + lowercase; else unsupported.
fn resolve_shell(raw: Option<&str>) -> Option<&'static str> {
    match raw.unwrap_or("bash").trim().to_ascii_lowercase().as_str() {
        "bash" => Some("bash"),
        "zsh" => Some("zsh"),
        "fish" => Some("fish"),
        _ => None,
    }
}

/// Select the embedded completion script for `shell`; unknown shells fall back to bash.
fn script_for(shell: &str) -> &'static str {
    match shell {
        "zsh" => ZSH_SCRIPT,
        "fish" => FISH_SCRIPT,
        _ => BASH_SCRIPT,
    }
}

/// Run `tan completion`: resolve `--shell` and emit its script, or fail with
/// `completion.shell-unsupported` (exit `RuntimeFailure`) for an unsupported shell.
pub fn run(g: &GlobalArgs, args: &CompletionArgs) -> CommandRun {
    let Some(shell) = resolve_shell(args.shell.as_deref()) else {
        let issues = vec![Issue {
            code: "completion.shell-unsupported".to_string(),
            severity: "error".to_string(),
            message: "Unsupported shell. Allowed values: bash, zsh, fish.".to_string(),
        }];
        let data = CompletionData {
            schema_version: "1".to_string(),
            shell: "bash".to_string(),
            script: String::new(),
        };
        let text = if g.is_json() {
            Vec::new()
        } else {
            vec!["completion: unsupported shell. Use --shell bash|zsh|fish.".to_string()]
        };
        let json = g.is_json().then(|| {
            Envelope::new(
                "completion",
                null_project(),
                data,
                issues,
                ExitCode::RuntimeFailure.code(),
            )
            .to_json()
        });
        return CommandRun {
            exit: ExitCode::RuntimeFailure,
            text,
            json,
        };
    };

    let script = script_for(shell).to_string();
    if !g.is_json() {
        // The script IS the payload (README: "`tan completion --shell zsh`
        // emits a completion script"), and the only sane way to consume it is
        // `eval "$(tan completion --shell zsh)"` / `> file` stdout capture.
        // `main::emit` sends every `CommandRun::text` line to STDERR (correct
        // for status/diagnostic prose elsewhere), so routing the script
        // through `text` made both the eval and the redirect capture nothing
        // while the script scrolled past looking like an error dump. Print
        // the payload straight to stdout instead, and leave `text` empty so
        // nothing doubles up on stderr.
        println!("{script}");
    }
    let text = Vec::new();
    let data = CompletionData {
        schema_version: "1".to_string(),
        shell: shell.to_string(),
        script,
    };
    let json = g.is_json().then(|| {
        Envelope::new(
            "completion",
            null_project(),
            data,
            Vec::new(),
            ExitCode::Success.code(),
        )
        .to_json()
    });

    CommandRun {
        exit: ExitCode::Success,
        text,
        json,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::cli::Format;

    #[test]
    fn resolve_shell_defaults_and_normalizes() {
        assert_eq!(resolve_shell(None), Some("bash"));
        assert_eq!(resolve_shell(Some("  ZSH ")), Some("zsh"));
        assert_eq!(resolve_shell(Some("fish")), Some("fish"));
        assert_eq!(resolve_shell(Some("tcsh")), None);
    }

    #[test]
    fn scripts_are_nonempty_and_shell_specific() {
        assert!(BASH_SCRIPT.contains("_tan_complete"));
        assert!(ZSH_SCRIPT.contains("#compdef tan"));
        assert!(FISH_SCRIPT.contains("__fish_use_subcommand"));
        // The captured zsh script collapses the `_arguments -C` continuations.
        assert!(ZSH_SCRIPT.contains("_arguments -C     '1:command:->command'"));
    }

    /// Word-boundary substring check: a bare `.contains` would also match a
    /// command name that is a fragment of an unrelated token, silently hiding
    /// a real drift.
    fn script_lists(script: &str, name: &str) -> bool {
        script
            .split(|c: char| !c.is_ascii_alphanumeric() && c != '-')
            .any(|tok| tok == name)
    }

    /// The scripts used to be a frozen 12-command capture of the retired TS
    /// CLI while `cli::Command` grew to 31 variants, so `tan build<TAB>` (and
    /// every command added since) offered nothing on any shell. Read the
    /// authoritative command list straight off clap's own command graph
    /// (never hand-duplicate it here) so this fails the moment a future
    /// `Command` variant ships without a matching completion entry.
    #[test]
    fn embedded_scripts_list_every_cli_command() {
        use clap::CommandFactory;
        let root = crate::cli::Cli::command();
        let missing: Vec<String> = root
            .get_subcommands()
            .map(|c| c.get_name().to_string())
            .filter(|name| {
                !script_lists(BASH_SCRIPT, name)
                    || !script_lists(ZSH_SCRIPT, name)
                    || !script_lists(FISH_SCRIPT, name)
            })
            .collect();
        assert!(
            missing.is_empty(),
            "completion scripts missing commands: {missing:?}"
        );
    }

    // -----------------------------------------------------------------
    // Flag-parity gate (issue #92): `--core` went missing from all three
    // scripts across #66, and only a reviewer reading the raw diff caught
    // it; #85 added the entries but no mechanism, so the next flag would
    // have drifted the same way. A round-2 adversarial review then found
    // the mechanism itself was too weak to trust:
    //  - `Cli::command()` is UNBUILT: clap defers adding `--help`/
    //    `--version` to `_build_self`, so an unbuilt command's
    //    `get_arguments()` silently omits both — and `--version` is live
    //    on every subcommand (`propagate_version = true`), completable on
    //    none of them.
    //  - zsh's per-`case`-arm `_arguments` has NO inheritance from a shared
    //    "global" set — a global flag must be repeated in EVERY arm to be
    //    completable there, so a file-wide "appears somewhere in the file"
    //    check passed while most dedicated arms (bash's `completion`/`sdk`
    //    arms too) offered nothing for `--verbose`/`--project`/etc.
    //  - `--target`/`--all` are `GlobalArgs` (`global = true`, valid on
    //    every subcommand) but were hand-added only to `generate`'s section.
    // `clap::Command::build()` (`built_root` below) fixes the first bug
    // directly, and its side effect — running clap's OWN `global = true`
    // propagation — fixes the other two for free: `sub.get_arguments()` on
    // a built command already IS this subcommand's full expected flag set
    // (its own declared args + every propagated global + its own
    // `--help`/`--version`), so the per-arm check below needs no hand-kept
    // "these are the globals" list to drift out of sync with `GlobalArgs`
    // again. Checking BOTH directions (missing vs unexpected) also catches
    // a renamed/removed `#[arg(long)]` left stale in a script.
    //
    // The `DELIBERATE_OMISSIONS` escape hatch this gate used to carry is
    // gone: it held zero entries, and its one proposed candidate —
    // `renode --sim-mode` (cli.rs), DEFERRED and errors "not yet ported" —
    // isn't an omission at all. It's a real flag clap accepts (wired for
    // CLI surface stability per the doc comment on `RenodeArgs::sim_mode`),
    // so completing it is correct; erroring is a property of *running* it,
    // not of whether it should tab-complete. If a future flag genuinely
    // needs a script-side exception, reintroduce the mechanism then.
    //
    // A round-3 adversarial review then found the per-arm loop above STILL
    // exempted one thing: `root.get_subcommands()` only walks the 31
    // `Command` variants, so the root command's OWN `get_arguments()` (the
    // 11 `GlobalArgs` fields flattened directly onto `Cli`, plus its own
    // `--help`/`--version`) was never asserted at all. That's live and
    // user-visible before any subcommand word is typed: bash's `cword -eq 1`
    // branch used to offer only `$commands`, and zsh's root
    // `_arguments -C '1:command:->command' '*::arg:->args'` carried no
    // option specs — so `tan --ver<TAB>` completed nothing on either shell
    // (fish was already fine: its unconditioned lines fire at root position
    // too). Fixed by asserting root's flags the same both-directions way
    // below, and by splicing `$global_flags` / `"${global_args[@]}"` into
    // bash's root branch and zsh's root call so the assertion passes for
    // the right reason.
    // -----------------------------------------------------------------

    /// Build the full command graph. `Cli::command()` alone is UNBUILT —
    /// clap defers adding `--help`/`--version` and propagating every
    /// `global = true` `GlobalArgs` field onto each subcommand until
    /// `_build_self` runs (see the block comment above for why that
    /// matters here).
    fn built_root() -> clap::Command {
        use clap::CommandFactory;
        let mut root = crate::cli::Cli::command();
        root.build();
        root
    }

    /// Strip zsh `_arguments` spec metadata that isn't the flag name
    /// itself — the `[...]` help-text bracket, and the `:name:(values)`
    /// value-spec suffix up to the closing quote — before tokenizing.
    /// Without this, a flag mentioned only inside another flag's
    /// description (e.g. `'--expect[use --core for multicore]'`) tokenizes
    /// out as its own `--core` hit and silently satisfies the presence
    /// check for a flag the arm never actually declares (issue #92 MINOR 4,
    /// proven in an isolated harness). No-op on bash text: no `[`/`:`
    /// characters appear in a `compgen -W "..."` word list.
    fn strip_arg_spec_metadata(text: &str) -> String {
        let mut out = String::with_capacity(text.len());
        let mut in_brackets = false;
        let mut chars = text.chars().peekable();
        while let Some(c) = chars.next() {
            match c {
                '[' => in_brackets = true,
                ']' => in_brackets = false,
                ':' if !in_brackets => {
                    // Skip the value-spec suffix up to the closing quote —
                    // it's a completion action (`:format:(text json)`), not
                    // a flag name.
                    if let Some(quote) = chars.by_ref().find(|&next| next == '\'') {
                        out.push(quote);
                    }
                }
                _ if !in_brackets => out.push(c),
                _ => {}
            }
        }
        out
    }

    /// Pull `--flag` long-flag names out of bash/zsh script text. Both
    /// dialects always spell a flag as a literal `--name` substring
    /// (`compgen -W "--foo"` / `_arguments '--foo[...]'`), so the same
    /// alnum-or-dash tokenizer `script_lists` uses above picks the name out
    /// cleanly once `strip_arg_spec_metadata` has removed zsh's trailing
    /// description/value-spec text.
    fn dash_flags_in(text: &str) -> std::collections::HashSet<String> {
        strip_arg_spec_metadata(text)
            .split(|c: char| !c.is_ascii_alphanumeric() && c != '-')
            // bash's own `compgen -W "..." -- "$cur"` end-of-options marker
            // tokenizes to a bare "--", which `strip_prefix("--")` turns
            // into an empty-string "flag" — filter it, it names nothing.
            .filter_map(|tok| tok.strip_prefix("--").filter(|rest| !rest.is_empty()))
            .map(str::to_string)
            .collect()
    }

    /// Return the earlier of two substring matches. `.find(a).or_else(||
    /// find(b))` (the bug this replaced, issue #92 round-3 FINDING 2) always
    /// prefers `needle_a` whenever it occurs ANYWHERE in `haystack`, even
    /// when `needle_b` occurs first — so a fish line ordered `-a '...' -d
    /// '...'` would strip only the `-d` operand and leave the `-a` one
    /// (which can hide a flag name) untouched.
    fn earlier_match(haystack: &str, needle_a: &str, needle_b: &str) -> Option<usize> {
        match (haystack.find(needle_a), haystack.find(needle_b)) {
            (Some(a), Some(b)) => Some(a.min(b)),
            (Some(a), None) => Some(a),
            (None, Some(b)) => Some(b),
            (None, None) => None,
        }
    }

    /// Strip fish `-d '...'` / `-a '...'` operand strings before
    /// tokenizing — same reasoning and the same issue #92 MINOR 4 as
    /// `strip_arg_spec_metadata` above, applied to fish's own metadata
    /// shape (a description/value-list string trailing the flag, not a
    /// bracket).
    fn strip_fish_operands(line: &str) -> String {
        let mut out = String::new();
        let mut rest = line;
        while let Some(at) = earlier_match(rest, " -d '", " -a '") {
            let (head, tail) = rest.split_at(at);
            out.push_str(head);
            let (marker, after_marker) = tail.split_at(5); // " -d '" / " -a '"
            out.push_str(marker);
            rest = match after_marker.find('\'') {
                Some(end) => &after_marker[end..], // resume at the closing quote
                None => "",
            };
        }
        out.push_str(rest);
        out
    }

    /// Pull fish's `-l <name>` long-flag declarations out of script text.
    /// Fish spells a flag as a separate `-l` token followed by the bare
    /// name (no `--` prefix), unlike bash/zsh, so it needs its own
    /// extractor rather than `dash_flags_in`.
    fn fish_flags_in(text: &str) -> std::collections::HashSet<String> {
        let stripped = strip_fish_operands(text);
        let tokens: Vec<&str> = stripped.split_whitespace().collect();
        tokens
            .windows(2)
            .filter(|pair| pair[0] == "-l")
            .map(|pair| pair[1].to_string())
            .collect()
    }

    /// Split a POSIX shell `case ... esac` block's arms into
    /// `arm-name -> arm-body` (a pipe-separated header like
    /// `diff|presets|examples)` maps every name to the same body; `*)`
    /// maps under the literal key `"*"`). Scoping a flag lookup to the arm
    /// that actually runs for a subcommand — instead of the whole file —
    /// is what catches `renode` missing `--core` while `flash` has it.
    fn case_arms(case_body: &str) -> std::collections::HashMap<String, String> {
        let mut arms: std::collections::HashMap<String, String> = std::collections::HashMap::new();
        let mut current: Vec<String> = Vec::new();
        let mut body = String::new();
        for line in case_body.lines() {
            let trimmed = line.trim();
            let is_header = trimmed != ")"
                && trimmed.ends_with(')')
                && trimmed[..trimmed.len() - 1]
                    .chars()
                    .all(|c| c.is_ascii_alphanumeric() || c == '-' || c == '|' || c == '*');
            if is_header {
                current = trimmed
                    .trim_end_matches(')')
                    .split('|')
                    .map(str::to_string)
                    .collect();
                body.clear();
                continue;
            }
            if trimmed == ";;" {
                for name in &current {
                    arms.entry(name.clone()).or_default().push_str(&body);
                }
                current.clear();
                body.clear();
                continue;
            }
            body.push_str(line);
            body.push('\n');
        }
        arms
    }

    /// Resolve bash's `$global_flags` variable to its literal value. Every
    /// arm spells its globals as `$global_flags`, not the flag list itself
    /// (that indirection is the point — one place to keep the shared list,
    /// not 30-odd hand-duplicated copies) — resolve the reference before
    /// per-arm parsing so the checks below see real flag names instead of a
    /// bare variable name that would otherwise look like zero flags.
    fn resolve_bash_global_flags(script: &str) -> String {
        let marker = "local global_flags=\"";
        let start = script.find(marker).unwrap_or_else(|| {
            panic!("bash completion script layout changed: {marker:?} not found")
        }) + marker.len();
        let end = script[start..].find('"').unwrap_or_else(|| {
            panic!("bash completion script layout changed: unterminated global_flags")
        }) + start;
        script[start..end].to_string()
    }

    /// Extract the bash `case "${COMP_WORDS[1]}" in ... esac` block —
    /// bash's only case statement, so no nested-block bookkeeping needed.
    fn bash_case_arms(script: &str) -> std::collections::HashMap<String, String> {
        let marker = "case \"${COMP_WORDS[1]}\" in";
        let start = script.find(marker).unwrap_or_else(|| {
            panic!("bash completion script layout changed: {marker:?} not found")
        }) + marker.len();
        let mut body = String::new();
        for line in script[start..].lines() {
            if line.trim() == "esac" {
                break;
            }
            body.push_str(line);
            body.push('\n');
        }
        let resolved = body.replace("$global_flags", &resolve_bash_global_flags(script));
        case_arms(&resolved)
    }

    /// Extract bash's `if [[ $cword -eq 1 ]]; then ... fi` block — the arm
    /// that runs before any subcommand word has been typed, i.e. `tan
    /// --<TAB>`. Issue #92 round-3 FINDING 1: this arm used to offer only
    /// `$commands`, so no root flag was ever completable there even though
    /// clap accepts every `GlobalArgs` flag (plus `--help`/`--version`) at
    /// root.
    fn bash_root_flags(script: &str) -> std::collections::HashSet<String> {
        let marker = "if [[ $cword -eq 1 ]]; then";
        let start = script.find(marker).unwrap_or_else(|| {
            panic!("bash completion script layout changed: {marker:?} not found")
        }) + marker.len();
        let mut body = String::new();
        for line in script[start..].lines() {
            if line.trim() == "fi" {
                break;
            }
            body.push_str(line);
            body.push('\n');
        }
        let resolved = body.replace("$global_flags", &resolve_bash_global_flags(script));
        dash_flags_in(&resolved)
    }

    /// Resolve zsh's shared `global_args` array (spliced into every arm as
    /// `"${global_args[@]}"`) to its literal entries — same reasoning as
    /// `resolve_bash_global_flags` above, zsh's own indirection shape.
    fn resolve_zsh_global_args(script: &str) -> String {
        let marker = "global_args=(\n";
        let start = script.find(marker).unwrap_or_else(|| {
            panic!("zsh completion script layout changed: {marker:?} not found")
        }) + marker.len();
        let end_marker = "\n  )\n";
        let end = script[start..].find(end_marker).unwrap_or_else(|| {
            panic!("zsh completion script layout changed: closing global_args) not found")
        }) + start;
        script[start..end].to_string()
    }

    /// Extract zsh's inner `case $words[2] in ... esac` block (the per-
    /// subcommand dispatch nested inside the outer `case $state in`).
    /// Stopping at the FIRST `esac` after the marker is deliberate: that's
    /// the inner block's own close, which comes before the outer one.
    fn zsh_case_arms(script: &str) -> std::collections::HashMap<String, String> {
        let marker = "case $words[2] in";
        let start = script.find(marker).unwrap_or_else(|| {
            panic!("zsh completion script layout changed: {marker:?} not found")
        }) + marker.len();
        let mut body = String::new();
        for line in script[start..].lines() {
            if line.trim() == "esac" {
                break;
            }
            body.push_str(line);
            body.push('\n');
        }
        let resolved = body.replace("\"${global_args[@]}\"", &resolve_zsh_global_args(script));
        case_arms(&resolved)
    }

    /// Extract zsh's root `_arguments -C '1:command:->command' …` line —
    /// the dispatcher call that runs before `$words[2]` (a subcommand) has
    /// even been typed, i.e. `tan --<TAB>`. Unlike the per-subcommand arms
    /// in `case $words[2] in`, this line has no case-arm wrapper of its
    /// own, so pull it directly by its `1:command:->command` marker
    /// (unique: it's the literal state name only this line dispatches to).
    /// Issue #92 round-3 FINDING 1: this line used to carry no option
    /// specs at all, so no root flag was ever completable here.
    fn zsh_root_flags(script: &str) -> std::collections::HashSet<String> {
        let marker = "1:command:->command";
        let idx = script.find(marker).unwrap_or_else(|| {
            panic!("zsh completion script layout changed: {marker:?} not found")
        });
        let line_start = script[..idx].rfind('\n').map_or(0, |i| i + 1);
        let line_end = script[idx..].find('\n').map_or(script.len(), |i| idx + i);
        let line = &script[line_start..line_end];
        let resolved = line.replace("\"${global_args[@]}\"", &resolve_zsh_global_args(script));
        dash_flags_in(&resolved)
    }

    /// Look up the flags an arm map offers for `subcommand`, falling back
    /// to the `*)` arm when the script has no explicit case for it — that
    /// fallback is exactly what runs at completion time for a subcommand
    /// with no dedicated arm, so it's the correct section to check.
    fn arm_flags(
        arms: &std::collections::HashMap<String, String>,
        subcommand: &str,
    ) -> std::collections::HashSet<String> {
        let body = arms
            .get(subcommand)
            .or_else(|| arms.get("*"))
            .map(String::as_str)
            .unwrap_or("");
        dash_flags_in(body)
    }

    /// Fish has no case statement — each flag line carries its own
    /// `-n '__fish_seen_subcommand_from <names...>'` predicate (sometimes
    /// naming several subcommands at once, e.g. `image flash clean renode
    /// size`). Collect only the `-l` flags on lines whose predicate names
    /// `subcommand`.
    fn fish_subcommand_flags(script: &str, subcommand: &str) -> std::collections::HashSet<String> {
        let mut flags = std::collections::HashSet::new();
        for line in script.lines() {
            if !line.contains("__fish_seen_subcommand_from") {
                continue;
            }
            let Some(predicate) = line.split('\'').nth(1) else {
                continue;
            };
            let mut names = predicate.split_whitespace();
            names.next(); // the literal "__fish_seen_subcommand_from" test name
            if names.any(|n| n == subcommand) {
                flags.extend(fish_flags_in(line));
            }
        }
        flags
    }

    /// Fish spells "available on every subcommand" by never carrying a
    /// `__fish_seen_subcommand_from` predicate on that line (unlike bash's
    /// `$global_flags` var or zsh's shared array, which get spliced into
    /// each conditioned arm), so these lines apply everywhere
    /// unconditionally. The guard below IS exactly that literal substring
    /// check — not "no `-n` predicate at all" (a stricter reading this
    /// comment used to claim, issue #92 round-3 FINDING 3): fish.fish's
    /// `-n '__fish_use_subcommand'` line (the top-level command list) still
    /// has an `-n`, just not this one, and passes through uncounted here
    /// harmlessly because it declares no `-l` flag. If a future line ever
    /// gates a real `-l` flag behind some OTHER `-n` predicate, it would
    /// slip through as "unconditioned" too — revisit then. A subcommand's
    /// actual fish flag set is therefore this UNION `fish_subcommand_flags
    /// (name)`, never `fish_subcommand_flags` alone — that union is what
    /// `completion_scripts_match_clap_flags_exactly` below relies on.
    fn fish_unconditioned_flags(script: &str) -> std::collections::HashSet<String> {
        let mut flags = std::collections::HashSet::new();
        for line in script.lines() {
            if line.contains("__fish_seen_subcommand_from") {
                continue;
            }
            flags.extend(fish_flags_in(line));
        }
        flags
    }

    /// The gate itself: for every subcommand, clap's OWN idea of the
    /// `--long` flags it accepts there (`sub.get_arguments()` on a BUILT
    /// command — this subcommand's own args, every propagated `GlobalArgs`
    /// field, and its own `--help`/`--version`) must equal what each
    /// script offers in that subcommand's own section (falling back to the
    /// shared/`*` section for a subcommand with none). Checking both
    /// directions catches a flag the scripts forgot (issue #92 MAJOR 1-3)
    /// AND a stale one clap no longer accepts (issue #92 MINOR 5) in one
    /// pass.
    #[test]
    fn completion_scripts_match_clap_flags_exactly() {
        let root = built_root();
        let bash_arms = bash_case_arms(BASH_SCRIPT);
        let zsh_arms = zsh_case_arms(ZSH_SCRIPT);
        let fish_globals = fish_unconditioned_flags(FISH_SCRIPT);

        let mut failures = Vec::new();
        // `root.build()` inserts clap's own synthetic `help` pseudo-
        // subcommand (for `tan help <cmd>`) into `get_subcommands()` —
        // skip it, it isn't one of our `Command` variants and no script
        // lists it as a command.
        for sub in root
            .get_subcommands()
            .filter(|sub| sub.get_name() != "help")
        {
            let name = sub.get_name();
            let expected: std::collections::HashSet<String> = sub
                .get_arguments()
                .filter_map(|a| a.get_long().map(str::to_string))
                .collect();

            let actual_bash = arm_flags(&bash_arms, name);
            let actual_zsh = arm_flags(&zsh_arms, name);
            let actual_fish: std::collections::HashSet<String> = fish_globals
                .union(&fish_subcommand_flags(FISH_SCRIPT, name))
                .cloned()
                .collect();

            for (shell, actual) in [
                ("bash", &actual_bash),
                ("zsh", &actual_zsh),
                ("fish", &actual_fish),
            ] {
                for flag in expected.difference(actual) {
                    failures.push(format!("{name} --{flag} missing from: {shell}"));
                }
                for flag in actual.difference(&expected) {
                    failures.push(format!(
                        "{name} --{flag} in {shell} but clap doesn't accept it there (stale/renamed?)"
                    ));
                }
            }
        }

        // The root command itself — before any subcommand word is typed —
        // has its own `get_arguments()` too: the 11 `GlobalArgs` fields
        // flattened directly onto `Cli`, plus clap's own `--help`/
        // `--version`. The loop above never covers this (it only walks
        // `get_subcommands()`), which is exactly issue #92 round-3
        // FINDING 1: `tan --ver<TAB>` completed nothing on bash or zsh.
        let root_expected: std::collections::HashSet<String> = root
            .get_arguments()
            .filter_map(|a| a.get_long().map(str::to_string))
            .collect();
        let root_bash = bash_root_flags(BASH_SCRIPT);
        let root_zsh = zsh_root_flags(ZSH_SCRIPT);
        let root_fish = fish_unconditioned_flags(FISH_SCRIPT);
        for (shell, actual) in [
            ("bash", &root_bash),
            ("zsh", &root_zsh),
            ("fish", &root_fish),
        ] {
            for flag in root_expected.difference(actual) {
                failures.push(format!("root --{flag} missing from: {shell}"));
            }
            for flag in actual.difference(&root_expected) {
                failures.push(format!(
                    "root --{flag} in {shell} but clap doesn't accept it there (stale/renamed?)"
                ));
            }
        }

        assert!(
            failures.is_empty(),
            "\ncompletion/clap flag mismatch:\n{}",
            failures.join("\n")
        );
    }

    fn global(format: Format) -> GlobalArgs {
        GlobalArgs {
            project: None,
            board_yaml: None,
            sdk_root: None,
            target: None,
            all: false,
            format,
            verbose: false,
            quiet: false,
            no_color: true,
            non_interactive: false,
            ci: false,
        }
    }

    /// Regression for the completion script going to stdout: before the fix,
    /// a successful text-mode run returned the script split into `text`
    /// lines, which `main::emit` writes with `eprintln!` — so
    /// `eval "$(tan completion --shell zsh)"` and `... > file` both captured
    /// nothing. The payload now goes straight to stdout via `println!`
    /// inside `run`, so `text` must come back empty.
    #[test]
    fn text_mode_success_leaves_text_empty_script_goes_to_stdout() {
        let run = run(&global(Format::Text), &CompletionArgs { shell: None });
        assert_eq!(run.exit.code(), 0);
        assert!(run.text.is_empty());
    }
}
