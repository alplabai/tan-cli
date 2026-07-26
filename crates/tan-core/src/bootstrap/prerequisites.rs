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

use std::collections::BTreeMap;

use serde::Serialize;

use crate::debug::{DoctorCheck, DoctorReport, DoctorStatus, append_doctor_check};

/// One missing host prerequisite, in the form a consumer can act on.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct MissingPrerequisite {
    /// The tool name, exactly as the manifest's `prerequisites.<os>` lists it.
    pub tool: String,
    /// The install command for this host, from the manifest's
    /// `prerequisites.install.<os>`, or `None` when it lists none for this tool.
    /// NEVER prose — a consumer renders this as something it can run, so an
    /// unlisted tool must be `null`, not advice.
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

/// The structured per-tool half of a refusal, over the manifest's
/// `prerequisites.install` map already resolved FOR THIS HOST
/// ([`InstallCommands::for_host`](super::manifest::InstallCommands::for_host)).
///
/// Shared by both refusals below, which differ only in their prose: the tool
/// list, the command lookup and the `null` rule are identical, and two copies of
/// them is one drift away from the two platforms disagreeing about what the
/// machine half of the same verdict looks like.
///
/// The `None` is load-bearing, not a shrug. This value reaches the envelope's
/// `missingPrerequisites[].command`, which a consumer renders as something it can
/// RUN. The generic "install `x` and put it on PATH" advice for a tool the
/// manifest lists no command for is human prose and belongs in the printed line
/// ([`hint_line`]) only — prose in a runnable-command field is a button that
/// fails.
fn structured_missing(
    missing: &[&str],
    install: &BTreeMap<String, String>,
) -> Vec<MissingPrerequisite> {
    missing
        .iter()
        .map(|tool| MissingPrerequisite {
            tool: (*tool).to_string(),
            command: install.get(*tool).cloned(),
        })
        .collect()
}

/// The printed report line for one missing Windows prerequisite, over this host's
/// resolved `prerequisites.install` map. A tool the manifest lists no command for
/// gets generic advice rather than being dropped from the report — which is why
/// this is a separate function from [`structured_missing`] and not an `unwrap_or`
/// over the same lookup. The rendering (two-space indent, `  ->  ` with two
/// spaces each side) is `bootstrap.ps1`'s and must stay byte-identical.
pub fn hint_line(tool: &str, install: &BTreeMap<String, String>) -> String {
    match install.get(tool) {
        Some(command) => format!("  {tool}  ->  {command}"),
        None => format!("  {tool}  ->  install `{tool}` and put it on PATH"),
    }
}

/// The Windows refusal for a non-empty list of missing tools: the header, one
/// [`hint_line`] each, the reopen-PowerShell tail, and the matching structured
/// entries. `bootstrap.ps1`'s `$Prereqs` loop.
///
/// `install` is the caller's host-resolved map, NOT `InstallCommands` plus a
/// host: resolving once at the call site is what makes it impossible to look a
/// tool up in the wrong OS's table.
pub fn windows_refusal(missing: &[&str], install: &BTreeMap<String, String>) -> PrereqFailure {
    let mut lines = vec!["Missing required tools:".to_string()];
    lines.extend(missing.iter().map(|tool| hint_line(tool, install)));
    lines.push("Install the tools above (then reopen PowerShell) and re-run.".to_string());
    PrereqFailure {
        code: "prerequisites-missing",
        lines,
        missing: structured_missing(missing, install),
    }
}

/// The POSIX refusal: one line naming the tools, plus the same structured
/// entries Windows gets.
///
/// The LINE stays `bootstrap.sh`'s — the oracle names the tools and nothing else,
/// so nothing is appended to it. What changed under alp-sdk#959 is the structured
/// half: `command` was `None` for every POSIX tool because no install commands
/// existed on this side at all, and `prerequisites.install.linux`/`.macos` now
/// supply real ones (`sudo apt-get install -y cmake` / `brew install cmake`).
/// Which of the two the caller resolved is `install`'s business, not this
/// function's — the apt-vs-brew split is exactly why the manifest does not key
/// these under one `posix`.
pub fn posix_refusal(missing: &[&str], install: &BTreeMap<String, String>) -> PrereqFailure {
    PrereqFailure {
        code: "prerequisites-missing",
        lines: vec![format!(
            "Missing required tools: {}.  Install them and re-run.",
            missing.join(" ")
        )],
        missing: structured_missing(missing, install),
    }
}

/// Windows: `python` is on PATH but did not run (the Microsoft Store alias
/// prints nothing — `bootstrap.ps1`'s `$PyVer` check).
///
/// Its own code, not `prerequisites-missing`: there is no missing tool to
/// report here and no `{tool, command}` pair that could carry the fix. The
/// install command therefore reaches the user through the PROSE — which is
/// exactly why the ID in it has to come from `prerequisites.install.windows`
/// like every other one. A hardcoded `Python.Python.3.12` here would be a second
/// copy of a manifest fact sitting beside a correct read of it, invisible to both
/// drift gates; the manifest pin moving would then fix
/// `missingPrerequisites[].command` and leave this sentence stale.
///
/// Windows-only, so the key is `python` (`prerequisites.windows`' spelling), not
/// POSIX's `python3`.
pub fn windows_python_not_runnable(install: &BTreeMap<String, String>) -> PrereqFailure {
    PrereqFailure {
        code: "python-not-runnable",
        lines: vec![match install.get("python") {
            Some(command) => format!(
                "python did not run (Windows Store alias?).  Install real Python: {command}, \
                 reopen PowerShell, re-run."
            ),
            // Only reachable for an out-of-contract manifest: the producer's
            // schema requires `install.windows`' keys to equal
            // `prerequisites.windows`, which lists `python`. Degrade the sentence
            // rather than inventing a package ID for it.
            None => "python did not run (Windows Store alias?).  Install a real Python 3, reopen \
                     PowerShell, re-run."
                .to_string(),
        }],
        missing: Vec::new(),
    }
}

/// Windows: a working interpreter that is below the manifest's
/// `pythonMinVersion` floor. Also tool-less — the tool IS there, it is the
/// wrong version — so the install command travels in the prose, from the same
/// manifest key [`windows_python_not_runnable`] reads.
pub fn python_too_old(
    found: (u32, u32),
    floor: (u32, u32),
    install: &BTreeMap<String, String>,
) -> PrereqFailure {
    let (min_major, min_minor) = floor;
    let verdict = format!(
        "Python {}.{} found; the SDK tooling needs >= {min_major}.{min_minor}",
        found.0, found.1
    );
    PrereqFailure {
        code: "python-too-old",
        lines: vec![match install.get("python") {
            Some(command) => format!("{verdict} ({command})."),
            None => format!("{verdict}."),
        }],
        missing: Vec::new(),
    }
}

/// The envelope form of a refusal's missing-tool list: `None` when the refusal
/// names no tool.
///
/// `[]` is NEVER a value here. The two Python-floor refusals reach this with an
/// empty vec, and `[]` on the wire would spell "checked, nothing missing" —
/// which is exactly what a run that found the list clean reports, as `null`.
/// One fact, one spelling.
///
/// Shared by `bootstrap`'s `data.missingPrerequisites` and `doctor`'s: the rule
/// has to be identical in both envelopes or a consumer that learned it from one
/// command gets it wrong on the other.
pub fn reported_missing(missing: Vec<MissingPrerequisite>) -> Option<Vec<MissingPrerequisite>> {
    (!missing.is_empty()).then_some(missing)
}

/// The `doctor` verdict for a prerequisite probe: the check `tan doctor`
/// reports so a missing `ninja` is visible WITHOUT running `tan bootstrap`
/// (alp-sdk ADR 0021, Lane 1 P0a — the extension gates the bootstrap terminal
/// on this instead of letting it die as "failed to launch (exit code: 1)").
///
/// `Fail`, not `Warn`, on every refusal: each one is a hard blocker for
/// building, and bootstrap itself refuses to run against exactly these. There
/// is no degraded-but-usable middle here.
///
/// One check for all three refusals, so the wording that reaches a human is the
/// SAME string bootstrap would have printed (`lines.join(" ")`, as `failure()`
/// joins it). The machine half — which tool, which install command — travels
/// separately in `data.missingPrerequisites` via [`reported_missing`], because
/// that join is not recoverable from the prose.
///
/// `from_manifest` is [`BootstrapFacts::from_manifest`](super::BootstrapFacts):
/// the detail names which tool list was actually checked, the same fact
/// bootstrap's envelope reports as `factsFromManifest`. A `doctor` run with no
/// resolvable SDK still checks the host — against tan's built-in list — and has
/// to say so rather than imply it read the SDK's.
///
/// `manifest_error` is the version-skew / parse guard's `Err` message, when an
/// SDK resolved but its `metadata/bootstrap.json` was REFUSED. Reporting that as
/// a plain fallback would be the worst outcome available: `tan doctor` is the
/// command a user runs to find out why `tan bootstrap` refuses, and bootstrap's
/// verdict on the very same message is a fatal `ValidationFailure`. Doctor does
/// not repeat that verdict — it is a read-only report and the tool list it fell
/// back to is still a real answer — but it must not report a bare `Pass` either,
/// so a rejected manifest is at minimum a `Warn` naming the rejection verbatim.
/// A refusal still outranks it: a missing `ninja` is the more urgent fact.
pub fn doctor_prerequisite_check(
    refusal: Option<&PrereqFailure>,
    checked: &[String],
    from_manifest: bool,
    manifest_error: Option<&str>,
) -> DoctorCheck {
    let source = if from_manifest {
        "facts from alp-sdk metadata/bootstrap.json"
    } else {
        "facts from tan's built-in fallback list"
    };
    let (status, detail, fix) = match refusal {
        None => (
            DoctorStatus::Pass,
            format!("{} present ({source})", checked.join(", ")),
            None,
        ),
        Some(failure) => (
            DoctorStatus::Fail,
            format!("{} ({source})", failure.lines.join(" ")),
            Some("Install the missing prerequisites, then run `tan bootstrap`.".to_string()),
        ),
    };
    let (status, detail, fix) = match manifest_error {
        None => (status, detail, fix),
        Some(message) => (
            // `max` in severity terms, spelled out: a refusal stays `Fail`.
            if status == DoctorStatus::Fail {
                DoctorStatus::Fail
            } else {
                DoctorStatus::Warn
            },
            format!("{detail} — {BOOTSTRAP_MANIFEST_REJECTED_PREFIX}{message}"),
            // The refusal's own fix wins when there is one; the manifest is the
            // secondary problem then.
            fix.or_else(|| Some(BOOTSTRAP_MANIFEST_REJECTED_FIX.to_string())),
        ),
    };
    DoctorCheck {
        name: "hostPrerequisites".to_string(),
        status,
        detail,
        fix,
    }
}

/// The detail-string marker for a refused manifest. Its own constant so the
/// wiring test can assert the message SURVIVED into the check rather than
/// re-spelling the sentence.
pub const BOOTSTRAP_MANIFEST_REJECTED_PREFIX: &str = "metadata/bootstrap.json rejected: ";

/// The fix prose for a refused manifest. Its own constant because BOTH doctor
/// modes report the rejection and must word it identically: plain `doctor` folds
/// it into `hostPrerequisites` (above), and `tan doctor --build` — which has no
/// such check to hang it off, but IS the mode alp-sdk-vscode shells for
/// `runToolchainFix` — raises its own `bootstrapManifest` warning. Two spellings
/// of one verdict is the drift `BOOTSTRAP_MANIFEST_REJECTED_PREFIX` already
/// exists to prevent on the detail half.
pub const BOOTSTRAP_MANIFEST_REJECTED_FIX: &str = "Update `tan` or pin an SDK whose metadata/bootstrap.json this version understands; \
     `tan bootstrap` will refuse outright until then.";

/// Fold a finished `hostPrerequisites` check into the report: record the
/// machine-readable half of the same verdict, then append the check through
/// [`append_doctor_check`] (which counts it and re-derives `next_steps`).
///
/// Pure, and in `tan-core`, precisely because the probe that produces `check`
/// is not. When this lived inline next to the PATH walk, its test could only
/// ever exercise whichever branch the test host happened to be in — a clean
/// host, always `Pass` — and the refusal path was dead code at test time.
/// Deleting the whole `missing_prerequisites` assignment, or swapping the
/// `Pass`/`Fail` summary arms (which would exit 4 on every clean host), both
/// left the suite green. Taking the verdict as a PARAMETER is what makes those
/// mutations fail, so all three outcomes are tested below rather than one.
pub fn apply_prerequisite_check(
    report: &mut DoctorReport,
    check: DoctorCheck,
    missing: Vec<MissingPrerequisite>,
) {
    report.missing_prerequisites = reported_missing(missing);
    append_doctor_check(
        &mut report.summary,
        &mut report.checks,
        &mut report.next_steps,
        check,
    );
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
    use super::super::HostOs;
    use super::super::manifest::fallback_facts;
    use super::*;

    /// The REAL install commands for one host — the fallback constants, which
    /// `manifest::tests::the_fallback_matches_the_real_manifest_field_for_field`
    /// pins byte-equal to the vendored `metadata/bootstrap.json`. So every
    /// command asserted below is the producer's own string, not a literal these
    /// tests invented, and a manifest edit that these tests would contradict
    /// fails there.
    fn install(host: HostOs) -> BTreeMap<String, String> {
        fallback_facts((3, 10)).install.for_host(host).clone()
    }

    #[test]
    fn a_known_windows_tool_renders_its_exact_winget_line_and_joined_message() {
        // The regression this whole structured-data change exists to prevent:
        // the envelope issue message is `lines.join(" ")`, so any drift in the
        // rendering (the two-space indent, the two spaces each side of `->`)
        // silently changes what every text-mode user and every consumer that
        // still reads the message sees. Pinned byte-for-byte, out of the real
        // assembly -- no PATH probe involved, so `ninja` being installed on the
        // test host cannot make this vanish.
        let refusal = windows_refusal(&["ninja"], &install(HostOs::Windows));
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
    fn every_host_gets_its_own_package_managers_command_for_the_same_tool() {
        // What issue #90 is: the command used to come from a hardcoded `winget`
        // table, so `cmake` read the same on every host and every POSIX entry
        // reported `null`. It comes from `prerequisites.install.<os>` now, so the
        // three hosts must render three DIFFERENT commands for one tool -- and
        // handing a macOS user Linux's `apt-get` line (the bug a `posix`-keyed
        // lookup would cause) fails right here.
        let command_for = |host: HostOs| {
            let refusal = if host == HostOs::Windows {
                windows_refusal(&["cmake"], &install(host))
            } else {
                posix_refusal(&["cmake"], &install(host))
            };
            refusal.missing[0].command.clone()
        };
        assert_eq!(
            command_for(HostOs::Linux).as_deref(),
            Some("sudo apt-get install -y cmake")
        );
        assert_eq!(
            command_for(HostOs::MacOs).as_deref(),
            Some("brew install cmake")
        );
        assert_eq!(
            command_for(HostOs::Windows).as_deref(),
            Some("winget install -e --id Kitware.CMake")
        );
        // A POSIX host that is neither: no manifest entry, so `null` -- what
        // every POSIX host got before #959. Never a wrong-OS command.
        assert_eq!(command_for(HostOs::Other), None);
    }

    #[test]
    fn an_unknown_tool_gets_advice_in_the_line_and_null_in_the_command() {
        // The pairing IS the correction: a refactor that leaked the generic
        // prose into `command` must fail here, because a consumer renders that
        // field as a button it can press.
        let commands = install(HostOs::Windows);
        let refusal = windows_refusal(&["tan-no-such-tool-xyz"], &commands);
        assert_eq!(
            refusal.lines[1],
            "  tan-no-such-tool-xyz  ->  install `tan-no-such-tool-xyz` and put it on PATH"
        );
        assert_eq!(refusal.missing[0].command, None);
        assert_eq!(commands.get("tan-no-such-tool-xyz"), None);
    }

    #[test]
    fn the_windows_refusal_lists_every_missing_tool_in_order() {
        let refusal = windows_refusal(&["git", "ninja"], &install(HostOs::Windows));
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
    fn the_posix_refusal_stays_one_line_but_now_carries_real_commands() {
        // The PROSE is `bootstrap.sh`'s and does not grow: space-joined tool
        // names, TWO spaces before "Install", one line. The oracle prints no
        // per-tool commands and neither may this -- alp-sdk#959 changed what the
        // STRUCTURED half carries, not what a POSIX user reads.
        let refusal = posix_refusal(&["cmake", "ninja"], &install(HostOs::Linux));
        assert_eq!(refusal.code, "prerequisites-missing");
        assert_eq!(
            refusal.lines,
            vec!["Missing required tools: cmake ninja.  Install them and re-run."]
        );
        // Issue #90's "also still true": every POSIX entry reported
        // `command: null` because this side had no install commands at all. It
        // has them now -- per tool, so `ninja` (which `prerequisites.posix` does
        // not even list, hence no `install.linux` entry) stays `null` while
        // `cmake` becomes runnable.
        assert_eq!(
            refusal.missing,
            vec![
                MissingPrerequisite {
                    tool: "cmake".to_string(),
                    command: Some("sudo apt-get install -y cmake".to_string()),
                },
                MissingPrerequisite {
                    tool: "ninja".to_string(),
                    command: None,
                },
            ]
        );
    }

    #[test]
    fn reported_missing_never_spells_a_clean_check_as_an_empty_array() {
        // `[]` would mean "checked, nothing missing" -- which is what a clean
        // run reports as `null`. Both bootstrap's envelope and doctor's key off
        // this one function so the two cannot drift into two spellings.
        assert_eq!(reported_missing(Vec::new()), None);
        assert_eq!(
            reported_missing(python_too_old((3, 9), (3, 10), &install(HostOs::Windows)).missing),
            None
        );
        assert_eq!(
            reported_missing(windows_refusal(&["ninja"], &install(HostOs::Windows)).missing)
                .map(|m| m.len()),
            Some(1)
        );
    }

    #[test]
    fn the_doctor_check_fails_on_every_refusal_and_names_its_facts_source() {
        // The gap this closes: plain `tan doctor` never mentioned a missing
        // `ninja` at all, so the extension had nothing to render and the user
        // met it as "failed to launch (exit code: 1)" from the bootstrap
        // terminal instead (ADR 0021 P0a).
        let checked = vec!["git".to_string(), "ninja".to_string()];
        let pass = doctor_prerequisite_check(None, &checked, true, None);
        assert_eq!(pass.name, "hostPrerequisites");
        assert_eq!(pass.status, DoctorStatus::Pass);
        assert_eq!(
            pass.detail,
            "git, ninja present (facts from alp-sdk metadata/bootstrap.json)"
        );
        assert!(pass.fix.is_none());

        // No resolvable SDK -> still a real host check, against tan's own list,
        // and the detail must SAY so rather than imply it read the SDK's.
        let fallback = doctor_prerequisite_check(None, &checked, false, None);
        assert!(
            fallback
                .detail
                .ends_with("(facts from tan's built-in fallback list)"),
            "{}",
            fallback.detail
        );

        // Every refusal is a hard blocker -- including the two Python-floor
        // ones, which name no tool and so can only be actioned through the
        // message. The wording is bootstrap's own, space-joined exactly as the
        // envelope's issue message joins it.
        for refusal in [
            windows_refusal(&["ninja"], &install(HostOs::Windows)),
            posix_refusal(&["cmake"], &install(HostOs::Linux)),
            windows_python_not_runnable(&install(HostOs::Windows)),
            python_too_old((3, 9), (3, 10), &install(HostOs::Windows)),
        ] {
            let check = doctor_prerequisite_check(Some(&refusal), &checked, true, None);
            assert_eq!(check.status, DoctorStatus::Fail);
            assert!(check.fix.is_some());
            assert!(
                check.detail.starts_with(&refusal.lines.join(" ")),
                "{}",
                check.detail
            );
        }
    }

    #[test]
    fn a_rejected_manifest_is_named_in_the_check_instead_of_being_swallowed() {
        // `load_facts` returns a hard `Err` naming WHY a present-but-skewed
        // `metadata/bootstrap.json` was refused -- for `tan bootstrap` that is a
        // fatal ValidationFailure. Doctor used to `.ok()` the message away and
        // report a plain `Pass` whose detail was byte-identical to the no-SDK
        // case, so the one command a user runs to find out why bootstrap refuses
        // was the command hiding the reason.
        let checked = vec!["git".to_string()];
        let skew = "metadata/bootstrap.json declares schemaVersion 2, but this `tan` supports \
                    only 1.";

        let warned = doctor_prerequisite_check(None, &checked, false, Some(skew));
        assert_eq!(warned.status, DoctorStatus::Warn);
        assert!(warned.detail.contains(skew), "{}", warned.detail);
        assert!(
            warned
                .detail
                .contains(BOOTSTRAP_MANIFEST_REJECTED_PREFIX)
                // The fallback list is still what was checked, and must still
                // say so -- the two facts are reported together, not swapped.
                && warned.detail.contains("built-in fallback list"),
            "{}",
            warned.detail
        );
        assert!(warned.fix.is_some());

        // A real refusal outranks it: a missing `ninja` stays the `Fail`, and
        // keeps ITS fix (install the tools), with the manifest as the tail.
        let refused = doctor_prerequisite_check(
            Some(&windows_refusal(&["ninja"], &install(HostOs::Windows))),
            &checked,
            false,
            Some(skew),
        );
        assert_eq!(refused.status, DoctorStatus::Fail);
        assert!(refused.detail.contains(skew));
        assert_eq!(
            refused.fix.as_deref(),
            Some("Install the missing prerequisites, then run `tan bootstrap`.")
        );
    }

    /// A `DoctorReport` with nothing counted yet, so each case below asserts on
    /// exactly what `apply_prerequisite_check` did.
    fn empty_doctor_report() -> DoctorReport {
        DoctorReport {
            generated_at: "1970-01-01T00:00:00.000Z".to_string(),
            target_kind: crate::debug::DebugTargetKind::NativeHost,
            server: crate::debug::DebugServerKind::None,
            summary: crate::debug::DoctorSummary {
                pass: 0,
                warn: 0,
                fail: 0,
            },
            checks: Vec::new(),
            next_steps: Vec::new(),
            missing_prerequisites: None,
        }
    }

    #[test]
    fn a_clean_host_counts_a_pass_and_names_no_tool() {
        let checked = vec!["git".to_string()];
        let mut report = empty_doctor_report();
        apply_prerequisite_check(
            &mut report,
            doctor_prerequisite_check(None, &checked, true, None),
            Vec::new(),
        );

        // Swapping the summary arms would exit 4 on every clean host; this is
        // the assertion that catches it.
        assert_eq!(
            (
                report.summary.pass,
                report.summary.warn,
                report.summary.fail
            ),
            (1, 0, 0)
        );
        assert_eq!(report.checks.len(), 1);
        assert_eq!(report.checks[0].name, "hostPrerequisites");
        // `null`, never `[]` -- see `reported_missing`.
        assert_eq!(report.missing_prerequisites, None);
    }

    #[test]
    fn a_tool_naming_refusal_counts_a_fail_and_carries_the_pairs() {
        let checked = vec!["git".to_string(), "ninja".to_string()];
        let refusal = windows_refusal(&["ninja"], &install(HostOs::Windows));
        let mut report = empty_doctor_report();
        apply_prerequisite_check(
            &mut report,
            doctor_prerequisite_check(Some(&refusal), &checked, true, None),
            refusal.missing.clone(),
        );

        assert_eq!(
            (
                report.summary.pass,
                report.summary.warn,
                report.summary.fail
            ),
            (0, 0, 1)
        );
        // Deleting the structured half -- the whole point of the change -- has
        // to fail HERE, not go unnoticed because the host happened to be clean.
        assert_eq!(
            report.missing_prerequisites,
            Some(vec![MissingPrerequisite {
                tool: "ninja".to_string(),
                command: Some("winget install -e --id Ninja-build.Ninja".to_string()),
            }])
        );
        // The check arrives AFTER the report builder computed `next_steps`, so
        // routing this through `append_doctor_check` (rather than a bare
        // `checks.push`) is what puts its fix in front of the user.
        assert_eq!(
            report.next_steps,
            vec!["Install the missing prerequisites, then run `tan bootstrap`."]
        );
    }

    #[test]
    fn a_python_floor_refusal_counts_a_fail_but_still_names_no_tool() {
        // The asymmetric case: fully actionable through the MESSAGE, with no
        // `{tool, command}` pair that could carry it -- so `Fail` with `null`.
        let checked = vec!["python".to_string()];
        let refusal = python_too_old((3, 9), (3, 10), &install(HostOs::Windows));
        let mut report = empty_doctor_report();
        apply_prerequisite_check(
            &mut report,
            doctor_prerequisite_check(Some(&refusal), &checked, true, None),
            refusal.missing.clone(),
        );

        assert_eq!(
            (
                report.summary.pass,
                report.summary.warn,
                report.summary.fail
            ),
            (0, 0, 1)
        );
        assert_eq!(report.missing_prerequisites, None);
        assert!(report.checks[0].detail.contains("Python 3.9 found"));
    }

    #[test]
    fn the_python_floor_refusals_carry_their_own_codes_and_no_tools() {
        // A `{tool, command}` pair cannot represent "the Python you have is
        // 3.9", so these must NOT report under `prerequisites-missing` -- a
        // consumer keying on that code would get an empty array against a
        // fully actionable message.
        for refusal in [
            windows_python_not_runnable(&install(HostOs::Windows)),
            posix_python_not_runnable(),
        ] {
            assert_eq!(refusal.code, "python-not-runnable");
            assert!(refusal.missing.is_empty());
        }
        let old = python_too_old((3, 9), (3, 10), &install(HostOs::Windows));
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
