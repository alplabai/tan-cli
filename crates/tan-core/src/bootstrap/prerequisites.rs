// SPDX-License-Identifier: Apache-2.0
//! The pure half of `tan bootstrap`'s prerequisite gate: what a refusal SAYS,
//! and its per-tool machine form.
//!
//! The probing half — "is `ninja` on PATH", "does `py -3` actually run" — is
//! `tan-cli`'s (`commands::bootstrap::steps`), because it touches PATH and
//! spawns interpreters. Everything here takes the already-decided list of
//! missing tools and renders it, so both platforms' wording is unit-testable
//! from either host with no PATH involved at all.
//!
//! Message strings come from the parity oracles (`bootstrap.sh`'s prerequisite
//! loop, `bootstrap.ps1`'s `$Prereqs`). Their whitespace is load-bearing twice
//! over: a human reads the lines, and `commands::bootstrap`'s `failure()` joins
//! them with a single space into the envelope's issue message.

use serde::Serialize;

/// One missing host prerequisite, in the form a consumer can act on.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct MissingPrerequisite {
    /// The tool name, exactly as the manifest's `prerequisites.<os>` lists it.
    pub tool: String,
    /// The install command for this host, or `None` when tan knows no command
    /// for this tool. NEVER prose — a consumer renders this as something it
    /// can run, so an unknown tool must be `null`, not advice.
    pub command: Option<String>,
}

/// A refused prerequisite gate: the envelope issue code, the message lines
/// verbatim, and the structured per-tool form of those lines.
///
/// The structured half exists because the envelope's issue message is
/// `lines.join(" ")` and an install command contains the same spaces the join
/// used — the split is not recoverable, so a consumer that wants "which tool,
/// which command" has to be handed it rather than left to re-parse prose
/// (alp-sdk-vscode#347 proved that parse dead and deleted it).
///
/// The code is per-refusal rather than one blanket `prerequisites-missing`
/// because the two Python-floor refusals have no missing TOOL at all — a
/// `{tool, command}` pair cannot represent "the Python you have is 3.9". A
/// consumer keying on `prerequisites-missing` would otherwise get an empty
/// array against a fully actionable message.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PrereqFailure {
    /// The `bootstrap.<code>` suffix this refusal reports under.
    pub code: &'static str,
    /// The fatal message lines, verbatim (the scripts' own wording).
    pub lines: Vec<String>,
    /// Empty for the refusals that have no missing TOOL (the Python floor).
    pub missing: Vec<MissingPrerequisite>,
}

/// The `winget install` one-liner for a missing Windows prerequisite
/// (`bootstrap.ps1`'s `$Prereqs`), or `None` when tan knows no command for the
/// tool. The TOOL LIST is a manifest fact (`prerequisites.windows`); these
/// commands are not in the manifest, so they stay keyed by tool name here.
///
/// The `None` is load-bearing, not a shrug. This value reaches the envelope's
/// `missingPrerequisites[].command`, which a consumer renders as something it
/// can RUN. The generic "install `x` and put it on PATH" advice for an unlisted
/// tool is human prose and belongs in the printed line ([`hint_line`]) only —
/// prose in a runnable-command field is a button that fails.
pub fn install_command(tool: &str) -> Option<String> {
    match tool {
        "git" => Some("winget install -e --id Git.Git".to_string()),
        "cmake" => Some("winget install -e --id Kitware.CMake".to_string()),
        "python" => Some("winget install -e --id Python.Python.3.12".to_string()),
        "ninja" => Some("winget install -e --id Ninja-build.Ninja".to_string()),
        _ => None,
    }
}

/// The printed report line for one missing Windows prerequisite. An unlisted
/// tool gets generic advice rather than being dropped from the report — which
/// is why this is a separate function from [`install_command`] and not an
/// `unwrap_or` over it. The rendering (two-space indent, `  ->  ` with two
/// spaces each side) is `bootstrap.ps1`'s and must stay byte-identical.
pub fn hint_line(tool: &str) -> String {
    match install_command(tool) {
        Some(command) => format!("  {tool}  ->  {command}"),
        None => format!("  {tool}  ->  install `{tool}` and put it on PATH"),
    }
}

/// The Windows refusal for a non-empty list of missing tools: the header, one
/// [`hint_line`] each, the reopen-PowerShell tail, and the matching structured
/// entries. `bootstrap.ps1`'s `$Prereqs` loop.
pub fn windows_refusal(missing: &[&str]) -> PrereqFailure {
    let mut lines = vec!["Missing required tools:".to_string()];
    lines.extend(missing.iter().map(|tool| hint_line(tool)));
    lines.push("Install the tools above (then reopen PowerShell) and re-run.".to_string());
    PrereqFailure {
        code: "prerequisites-missing",
        lines,
        missing: missing
            .iter()
            .map(|tool| MissingPrerequisite {
                tool: (*tool).to_string(),
                command: install_command(tool),
            })
            .collect(),
    }
}

/// The POSIX refusal: one line naming the tools, and command-less entries.
///
/// `command: None` for EVERY tool, deliberately — this side has no install
/// commands to give, and neither has `bootstrap.sh`, which just names them.
/// They arrive with alp-sdk#949's `prerequisites.install.posix` manifest key;
/// until then a command here could only be invented, and an invented one is
/// worse than `null` (the consumer renders it as a runnable button).
pub fn posix_refusal(missing: &[&str]) -> PrereqFailure {
    PrereqFailure {
        code: "prerequisites-missing",
        lines: vec![format!(
            "Missing required tools: {}.  Install them and re-run.",
            missing.join(" ")
        )],
        missing: missing
            .iter()
            .map(|tool| MissingPrerequisite {
                tool: (*tool).to_string(),
                command: None,
            })
            .collect(),
    }
}

/// Windows: `python` is on PATH but did not run (the Microsoft Store alias
/// prints nothing — `bootstrap.ps1`'s `$PyVer` check).
///
/// Its own code, not `prerequisites-missing`: there is no missing tool to
/// report here and no `{tool, command}` pair that could carry the fix.
pub fn windows_python_not_runnable() -> PrereqFailure {
    PrereqFailure {
        code: "python-not-runnable",
        lines: vec![
            "python did not run (Windows Store alias?).  Install real Python: winget install -e \
             --id Python.Python.3.12, reopen PowerShell, re-run."
                .to_string(),
        ],
        missing: Vec::new(),
    }
}

/// Windows: a working interpreter that is below the manifest's
/// `pythonMinVersion` floor. Also tool-less — the tool IS there, it is the
/// wrong version.
pub fn python_too_old(found: (u32, u32), floor: (u32, u32)) -> PrereqFailure {
    let (min_major, min_minor) = floor;
    PrereqFailure {
        code: "python-too-old",
        lines: vec![format!(
            "Python {}.{} found; the SDK tooling needs >= {min_major}.{min_minor} (winget install \
             -e --id Python.Python.3.12).",
            found.0, found.1
        )],
        missing: Vec::new(),
    }
}

/// POSIX: `python3` is on PATH but did not run — the only failure this port
/// adds over `bootstrap.sh`, which would have hit it one step later at
/// `python3 -m venv`.
pub fn posix_python_not_runnable() -> PrereqFailure {
    PrereqFailure {
        code: "python-not-runnable",
        lines: vec![
            "python3 is on PATH but did not run.  Install a working Python 3 and re-run."
                .to_string(),
        ],
        missing: Vec::new(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_known_windows_tool_renders_its_exact_winget_line_and_joined_message() {
        // The regression this whole structured-data change exists to prevent:
        // the envelope issue message is `lines.join(" ")`, so any drift in the
        // rendering (the two-space indent, the two spaces each side of `->`)
        // silently changes what every text-mode user and every consumer that
        // still reads the message sees. Pinned byte-for-byte, out of the real
        // assembly -- no PATH probe involved, so `ninja` being installed on the
        // test host cannot make this vanish.
        let refusal = windows_refusal(&["ninja"]);
        assert_eq!(refusal.code, "prerequisites-missing");
        assert_eq!(
            refusal.missing,
            vec![MissingPrerequisite {
                tool: "ninja".to_string(),
                command: Some("winget install -e --id Ninja-build.Ninja".to_string()),
            }]
        );
        let joined = refusal.lines.join(" ");
        assert_eq!(
            joined,
            "Missing required tools:   ninja  ->  winget install -e --id Ninja-build.Ninja \
             Install the tools above (then reopen PowerShell) and re-run."
        );
        assert_eq!(joined.len(), 138);
    }

    #[test]
    fn every_known_tool_keeps_its_winget_id() {
        assert_eq!(
            install_command("git").as_deref(),
            Some("winget install -e --id Git.Git")
        );
        assert_eq!(
            install_command("cmake").as_deref(),
            Some("winget install -e --id Kitware.CMake")
        );
        assert_eq!(
            install_command("python").as_deref(),
            Some("winget install -e --id Python.Python.3.12")
        );
        assert_eq!(
            install_command("ninja").as_deref(),
            Some("winget install -e --id Ninja-build.Ninja")
        );
    }

    #[test]
    fn an_unknown_tool_gets_advice_in_the_line_and_null_in_the_command() {
        // The pairing IS the correction: a refactor that leaked the generic
        // prose into `command` must fail here, because a consumer renders that
        // field as a button it can press.
        let refusal = windows_refusal(&["tan-no-such-tool-xyz"]);
        assert_eq!(
            refusal.lines[1],
            "  tan-no-such-tool-xyz  ->  install `tan-no-such-tool-xyz` and put it on PATH"
        );
        assert_eq!(refusal.missing[0].command, None);
        assert_eq!(install_command("tan-no-such-tool-xyz"), None);
    }

    #[test]
    fn the_windows_refusal_lists_every_missing_tool_in_order() {
        let refusal = windows_refusal(&["git", "ninja"]);
        assert_eq!(refusal.lines[0], "Missing required tools:");
        assert_eq!(
            refusal.lines[1],
            "  git  ->  winget install -e --id Git.Git"
        );
        assert_eq!(
            refusal.lines[2],
            "  ninja  ->  winget install -e --id Ninja-build.Ninja"
        );
        assert_eq!(
            refusal.lines[3],
            "Install the tools above (then reopen PowerShell) and re-run."
        );
        let tools: Vec<&str> = refusal.missing.iter().map(|m| m.tool.as_str()).collect();
        assert_eq!(tools, ["git", "ninja"]);
    }

    #[test]
    fn the_posix_refusal_is_one_line_and_never_carries_a_command() {
        // No install commands exist on this side yet (alp-sdk#949), and the
        // single-line wording is `bootstrap.sh`'s -- space-joined tool names,
        // TWO spaces before "Install".
        let refusal = posix_refusal(&["cmake", "ninja"]);
        assert_eq!(refusal.code, "prerequisites-missing");
        assert_eq!(
            refusal.lines,
            vec!["Missing required tools: cmake ninja.  Install them and re-run."]
        );
        assert!(refusal.missing.iter().all(|m| m.command.is_none()));
        assert_eq!(refusal.missing.len(), 2);
    }

    #[test]
    fn the_python_floor_refusals_carry_their_own_codes_and_no_tools() {
        // A `{tool, command}` pair cannot represent "the Python you have is
        // 3.9", so these must NOT report under `prerequisites-missing` -- a
        // consumer keying on that code would get an empty array against a
        // fully actionable message.
        for refusal in [windows_python_not_runnable(), posix_python_not_runnable()] {
            assert_eq!(refusal.code, "python-not-runnable");
            assert!(refusal.missing.is_empty());
        }
        let old = python_too_old((3, 9), (3, 10));
        assert_eq!(old.code, "python-too-old");
        assert!(old.missing.is_empty());
        assert_eq!(
            old.lines,
            vec![
                "Python 3.9 found; the SDK tooling needs >= 3.10 (winget install -e --id \
                 Python.Python.3.12)."
            ]
        );
    }
}
