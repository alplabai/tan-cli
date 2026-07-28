// SPDX-License-Identifier: Apache-2.0
//! `tan sdk <list|install|current|switch>` — manage local SDK installs.
//!
//! Mirrors TS `runSdkCommand`: `list` queries the GitHub releases API,
//! `install <version>` git-clones into the cache, `current` reports the active
//! SDK, and `switch <version|path>` repoints it. Network/filesystem effects
//! live here; parsing + readiness logic is in `tan-core::sdk`.

use std::path::{Path, PathBuf};
use std::process::Command;

use serde::Serialize;
use tan_core::{
    GITHUB_RELEASES_URL, ManifestReconcile, SdkReadinessReport, SdkReadinessState, SdkRelease,
    SdkSourceTier, check_sdk_readiness, describe_network_error, is_plain_relative,
    parse_remote_sdk_releases, resolve_sdk_version_root, workspace_needs_bootstrap,
};

use super::CommandRun;
use super::bootstrap::{reconcile_west_manifest_path_for_switch, workspace_synced_to};
use crate::cli::{GlobalArgs, SdkArgs};
use crate::envelope::{Envelope, Issue, Project};
use crate::exit::ExitCode;
use crate::util::generated_at_iso;

/// Clone source for `sdk install` — the upstream `alp-sdk` git repository.
const SDK_GIT_URL: &str = "https://github.com/alplabai/alp-sdk.git";

/// Envelope `data` payload for `sdk list`: the queried releases.
#[derive(Serialize)]
struct ListData {
    subcommand: &'static str,
    releases: Vec<SdkRelease>,
}

/// Envelope `data` payload for `sdk install`: installed version, on-disk path, and readiness.
#[derive(Serialize)]
struct InstallData {
    subcommand: &'static str,
    version: String,
    /// Filesystem path the SDK was cloned to (serialized as `sdkPath`).
    #[serde(rename = "sdkPath")]
    sdk_path: String,
    readiness: SdkReadinessReport,
    /// Whether this install was also selected as the active SDK (tan-cli#162 —
    /// `install` used to leave nothing selected, and `sdk current` sent the
    /// user back to `install` with no exit). `false` on every failure path and
    /// whenever an SDK was ALREADY active (install never overrides a selection
    /// the user or another tool made on purpose); additive field, no existing
    /// consumer reads its absence.
    selected: bool,
}

/// Envelope `data` payload for `sdk current`: the active SDK, if any, its
/// readiness, and which precedence tier produced it.
#[derive(Serialize)]
struct CurrentData {
    subcommand: &'static str,
    /// Active SDK path, or `None` when no SDK is configured (serialized as `sdkPath`).
    #[serde(rename = "sdkPath")]
    sdk_path: Option<String>,
    readiness: Option<SdkReadinessReport>,
    /// Precedence tier that produced `sdkPath` (serialized as `sourceTier`):
    /// `sdkRootFlag` > `projectPin` > `globalDefault` > `discovery` > `none`.
    #[serde(rename = "sourceTier")]
    source_tier: SdkSourceTier,
}

/// Envelope `data` payload for `sdk switch`: the new active path and its resolved version.
#[derive(Serialize)]
struct SwitchData {
    subcommand: &'static str,
    /// Path the active-SDK pointer now references (serialized as `sdkPath`).
    #[serde(rename = "sdkPath")]
    sdk_path: String,
    version: Option<String>,
    /// Which pointer was written: `"global"` (`~/.alp/sdk-default`, from
    /// `--global`) or `"project"` (`<workspace>/.alp/sdk-path`). Lets a JSON
    /// consumer distinguish the scope without parsing the human text line.
    scope: &'static str,
}

/// Dispatches `tan sdk` to the matching subcommand handler; unknown subcommands fail.
pub fn run(g: &GlobalArgs, args: &SdkArgs) -> CommandRun {
    match args.subcommand.as_deref() {
        Some("list") => run_list(g),
        Some("install") => run_install(g, args),
        Some("current") => run_current(g),
        Some("switch") => run_switch(g, args),
        other => {
            let sub = other.unwrap_or("(none)");
            emit_failure(
                g,
                ListData {
                    subcommand: "list",
                    releases: Vec::new(),
                },
                ExitCode::RuntimeFailure,
                "unknown-subcommand",
                format!("Unknown sdk subcommand: {sub}"),
                vec![
                    format!("sdk: unknown subcommand '{sub}'."),
                    "Available subcommands: list, install <version>, current, switch <version>"
                        .to_string(),
                ],
            )
        }
    }
}

// ── tan sdk list ────────────────────────────────────────────────────────────

/// Fetches the GitHub releases and renders them as a table; surfaces fetch errors as a failure.
fn run_list(g: &GlobalArgs) -> CommandRun {
    let pb = crate::progress::spinner(g, "Fetching Alp SDK releases…");
    let result = fetch_releases(crate::http::agent(), GITHUB_RELEASES_URL);
    pb.finish_and_clear();
    let releases = match result {
        Ok(r) => r,
        Err(message) => {
            return emit_failure(
                g,
                ListData {
                    subcommand: "list",
                    releases: Vec::new(),
                },
                ExitCode::RuntimeFailure,
                "fetch-failed",
                message.clone(),
                vec!["sdk list: failed to fetch releases.".to_string(), message],
            );
        }
    };
    let text = format_release_table(&releases);
    emit_success(
        g,
        ListData {
            subcommand: "list",
            releases,
        },
        ExitCode::Success,
        Vec::new(),
        text,
    )
}

/// GETs the releases API at `url` through `agent` and parses the JSON into
/// `SdkRelease`s via `tan-core`.
///
/// Production passes [`crate::http::agent`] and [`GITHUB_RELEASES_URL`], rather
/// than the bare `ureq::get(GITHUB_RELEASES_URL)` this used to call: that
/// default agent ignores both `ALL_PROXY`/`HTTPS_PROXY` and the OS trust store,
/// so this command was unusable behind a corporate proxy or a TLS-intercepting
/// middlebox. `describe_network_error` then names that as the likely cause
/// instead of leaving the user with a raw transport error.
///
/// Both are parameters rather than hard-wired *so that the choice of agent is
/// under test*: this is the single line that connects the whole shared-agent
/// change to the binary, and with it hard-wired, reverting it to the bare
/// `ureq::get` — undoing proxy support, the CA merge and the timeout at once —
/// left the suite entirely green.
fn fetch_releases(agent: &ureq::Agent, url: &str) -> Result<Vec<SdkRelease>, String> {
    let response = agent
        .get(url)
        .set("User-Agent", "tan-cli/0")
        .set("Accept", "application/vnd.github+json")
        .set("X-GitHub-Api-Version", "2022-11-28")
        .call()
        .map_err(|e| describe_network_error(&e.to_string(), crate::http::proxy_configured()))?;
    let value: serde_json::Value = response.into_json().map_err(|e| e.to_string())?;
    parse_remote_sdk_releases(&value)
}

/// Formats releases into human-readable lines (tag, publish date, flags, truncated notes).
fn format_release_table(releases: &[SdkRelease]) -> Vec<String> {
    if releases.is_empty() {
        return vec!["No SDK releases found.".to_string()];
    }
    let mut lines = vec![format!("Alp SDK releases ({})", releases.len())];
    for rel in releases {
        let date: String = rel.published_at.chars().take(10).collect();
        let notes = if rel.release_notes_summary.is_empty() {
            String::new()
        } else {
            format!("   {}", truncate(&rel.release_notes_summary, 60))
        };
        // #122: GitHub's draft/prerelease flags were parsed but never shown —
        // a terminal reader asking "what is the latest SDK?" could not tell a
        // release candidate or an unpublished draft from a real release.
        // Placed after `notes` (not between the fixed-width date and notes)
        // so the notes column start stays put regardless of flag width.
        let flags = match (rel.draft, rel.prerelease) {
            (true, true) => " [draft, prerelease]",
            (true, false) => " [draft]",
            (false, true) => " [prerelease]",
            (false, false) => "",
        };
        lines.push(format!("  {:<12} {date}{notes}{flags}", rel.tag));
    }
    lines
}

// ── tan sdk install <version> ────────────────────────────────────────────────

/// Clones the requested SDK version into the cache (unless already present) and reports readiness.
/// A `Missing` readiness fails both the process exit and the JSON envelope (`ok`/`exitCode`/`issues`).
fn run_install(g: &GlobalArgs, args: &SdkArgs) -> CommandRun {
    let Some(version) = args.arg.clone() else {
        return emit_failure(
            g,
            InstallData {
                subcommand: "install",
                version: String::new(),
                sdk_path: String::new(),
                readiness: empty_readiness(""),
                selected: false,
            },
            ExitCode::RuntimeFailure,
            "missing-version",
            "Version argument is required.".to_string(),
            vec![
                "sdk install: version argument is required.".to_string(),
                "Usage: tan sdk install <version>".to_string(),
            ],
        );
    };

    // `version` is untrusted CLI/extension input joined onto the cache root
    // below, not a path itself. `Path::new(&cache_root).join(&version)` with an
    // unvalidated `version` lets `tan sdk install ../../x` (or an absolute /
    // UNC value) write the clone outside the cache root entirely — `is_absolute()`
    // alone also misses Windows-rooted/drive-relative shapes (see
    // tan_core::path_guard). Reject anything that isn't a single plain path
    // segment before it ever reaches `Path::join`.
    if !is_plain_relative(Path::new(&version)) {
        return emit_failure(
            g,
            InstallData {
                subcommand: "install",
                version: version.clone(),
                sdk_path: String::new(),
                readiness: empty_readiness(""),
                selected: false,
            },
            ExitCode::RuntimeFailure,
            "invalid-version",
            format!("Invalid SDK version: {version:?} (must not be a path)."),
            vec![format!(
                "sdk install: invalid version {version:?} — must not contain '/', '\\', '..' or a drive/UNC prefix."
            )],
        );
    }

    let cache_root = args.destination.clone().unwrap_or_else(default_cache_root);
    let dest_path = Path::new(&cache_root).join(&version);
    let dest_str = dest_path.to_string_lossy().to_string();

    let already_installed = dest_path.join("scripts").join("alp_project.py").exists();
    if !already_installed {
        let pb = crate::progress::spinner(g, &format!("Cloning alp-sdk {version}…"));
        let clone_result = git_clone(&version, &dest_path);
        pb.finish_and_clear();
        if let Err(message) = clone_result {
            return emit_failure(
                g,
                InstallData {
                    subcommand: "install",
                    version: version.clone(),
                    sdk_path: dest_str.clone(),
                    readiness: empty_readiness(&dest_str),
                    selected: false,
                },
                ExitCode::RuntimeFailure,
                "install-failed",
                message.clone(),
                vec!["sdk install: installation failed.".to_string(), message],
            );
        }
    }

    let readiness = readiness_for(&dest_str);
    let exit = if readiness.state == SdkReadinessState::Missing {
        ExitCode::RuntimeFailure
    } else {
        ExitCode::Success
    };
    let mut text = format_readiness_block(&format!("SDK {version} installed"), &readiness);
    // A Missing readiness used to reach the envelope only as this text block,
    // which `--format json` throws away entirely — the JSON consumer (the
    // vscode extension) got exitCode 0/ok:true with an empty `issues` array
    // for an SDK clone that is not actually usable. Surface it as a real Issue
    // too, not just prose.
    let mut issues = if readiness.state == SdkReadinessState::Missing {
        vec![Issue {
            code: "sdk.install-not-ready".to_string(),
            severity: "error".to_string(),
            message: if readiness.issues.is_empty() {
                format!("SDK {version} was installed but is not ready to use.")
            } else {
                format!(
                    "SDK {version} was installed but is not ready to use: {}",
                    readiness.issues.join("; ")
                )
            },
        }]
    } else {
        Vec::new()
    };

    // tan-cli#162: `install` used to leave the active-SDK pointer untouched, so
    // `sdk current` (the very next command a new user runs) reported "no
    // active SDK" and sent them back to `install` -- a loop with no exit.
    // Auto-select ONLY when nothing is active yet: an install alongside an
    // SDK the user (or another tool) already selected on purpose must not
    // silently repoint them. Reuses `do_switch` -- the exact repair
    // alp-sdk-vscode#388 now shells `tan sdk switch` for -- rather than a
    // second, parallel copy of the pointer write + `.west/config`
    // reconciliation; a caller that already has an active SDK (e.g. the
    // extension, which switches explicitly after every install) sees no
    // difference. Best-effort: a failed auto-select does not fail the
    // install itself, which already succeeded.
    let mut selected = false;
    if exit == ExitCode::Success {
        let workspace_root = crate::util::cli_workspace_root(g);
        let (active, _) = crate::util::resolve_sdk_tiered(g, &workspace_root);
        if active.is_none() {
            let switch_args = SdkArgs {
                subcommand: Some("switch".to_string()),
                arg: Some(version.clone()),
                destination: args.destination.clone(),
                global: false,
            };
            selected = fold_auto_select(&mut text, &mut issues, do_switch(g, &switch_args));
        }
    }

    emit_success(
        g,
        InstallData {
            subcommand: "install",
            version,
            sdk_path: dest_str,
            readiness,
            selected,
        },
        exit,
        issues,
        text,
    )
}

/// Fold an auto-select attempt's outcome into `install`'s own report — a
/// trailing blank line, then either the switch's full text/issues or a
/// best-effort warning naming why it did not select. `selected` (`install`'s
/// own return) is this call's `bool` result.
///
/// PURE, and split out purely so tan-cli#162's actual new behavior is
/// unit-testable directly: the GATE around this call (`resolve_sdk_tiered(..)
/// .is_none()`) depends on this machine's real `~/.alp` (global-default and
/// discovery both read it) the same way `resolve_sdk_tiered`'s own tests
/// already avoid asserting on for exactly that reason — so testing the merge
/// logic here, against a hand-built [`SwitchOutcome`]/[`SwitchFailure`], is
/// what actually exercises tan-cli#162's new code without coupling a test's
/// pass/fail to whatever SDK happens to be configured on the machine running
/// it.
fn fold_auto_select(
    text: &mut Vec<String>,
    issues: &mut Vec<Issue>,
    outcome: Result<SwitchOutcome, Box<SwitchFailure>>,
) -> bool {
    match outcome {
        Ok(switched) => {
            text.push(String::new());
            text.extend(switched.text);
            // tan-cli#160/#161/#162 review: the auto-select always writes the
            // PROJECT (cwd-scoped) pointer, never `--global` -- so a
            // first-time user who runs `install` from a scratch directory
            // before any project exists gets a selection that later commands
            // run from a different directory will not see (`tan sdk current`
            // there reports "nothing selected" again). `switch`'s own text
            // ("Switched project SDK to ...") does not say that scoping is
            // directory-bound; state it here so this is not a silent trap.
            if switched.data.scope == "project" {
                text.push(
                    "  note    scoped to this directory -- run future commands from here, \
                     or `tan sdk switch --global` to select machine-wide."
                        .to_string(),
                );
            }
            issues.extend(switched.issues);
            true
        }
        Err(failure) => {
            text.push(String::new());
            text.push(format!(
                "  warn    installed, but could not select it automatically: {}",
                failure.message
            ));
            text.push("  next    run `tan sdk switch` yourself.".to_string());
            issues.push(Issue {
                code: format!("sdk.{}", failure.code),
                severity: "warning".to_string(),
                message: failure.message,
            });
            false
        }
    }
}

/// Shallow-clones `SDK_GIT_URL` at the `version` tag into `dest`; returns git's stderr on failure.
fn git_clone(version: &str, dest: &Path) -> Result<(), String> {
    if let Some(parent) = dest.parent() {
        std::fs::create_dir_all(parent).map_err(|e| e.to_string())?;
    }
    // Capture output (--quiet) rather than inherit: the spinner owns the
    // terminal while cloning, and JSON/non-interactive runs stay output-free.
    // git's stderr is surfaced verbatim only on failure — but git clones over
    // HTTPS through the same middlebox `sdk list` hits, and its own wording
    // ("SSL certificate problem: unable to get local issuer certificate", or the
    // schannel equivalent) reads as a broken download too. Same hint, one
    // command later. It deliberately names no specific knob: git has its own
    // trust store and its own `http.sslCAInfo`, which would be the wrong advice
    // on the in-process `sdk list` path that shares this sentence.
    let output = Command::new("git")
        .args([
            "clone",
            "--quiet",
            "--branch",
            version,
            "--depth",
            "1",
            SDK_GIT_URL,
        ])
        .arg(dest)
        .output()
        .map_err(|e| format!("Alp: git clone failed to start: {e}"))?;
    if !output.status.success() {
        let detail = String::from_utf8_lossy(&output.stderr);
        let detail = detail.trim();
        if detail.is_empty() {
            return Err(format!(
                "Alp: git clone failed with exit code {}.",
                output
                    .status
                    .code()
                    .map(|c| c.to_string())
                    .unwrap_or_else(|| "unknown".to_string())
            ));
        }
        return Err(describe_network_error(
            &format!("Alp: git clone failed: {detail}"),
            crate::http::proxy_configured(),
        ));
    }
    Ok(())
}

// ── tan sdk current ──────────────────────────────────────────────────────────

/// Resolves the active SDK for the current workspace across the full
/// four-tier precedence chain (`--sdk-root` > project pin > global default >
/// discovery) and reports its path, readiness, and the winning tier.
fn run_current(g: &GlobalArgs) -> CommandRun {
    // Every other reader of `.alp/sdk-path` resolves it under
    // `cli_workspace_root` (cwd joined with `--project`) — this used to read
    // from the bare process cwd, so `tan --project ./firmware sdk current`
    // reported the repo-root pointer instead of the one `build`/`flash`/etc.
    // actually consult for that project.
    let workspace_root = crate::util::cli_workspace_root(g);
    let (sdk_path, source_tier) = crate::util::resolve_sdk_tiered(g, &workspace_root);
    // `resolve_sdk_tiered` no longer records itself (its OTHER call site,
    // `switch_cache_roots`, only wants this as a candidate cache root, not the
    // envelope's "active SDK") — this is the one place that result actually
    // IS the active SDK the command reports, so it records here.
    if let Some(root) = &sdk_path {
        crate::sdk_report::record(root, source_tier);
    }
    let readiness = sdk_path.as_deref().map(readiness_for);

    let text = match (&sdk_path, &readiness) {
        (Some(_), Some(report)) => format_readiness_block("Active SDK", report),
        // tan-cli#162: this used to point EVERY "nothing selected" case back at
        // `install` -- including the one `install` itself had just left the
        // user in, a loop with no exit. `install` auto-selects when it can
        // (above), but a cache populated by an OLDER tan, a different
        // `--destination`, or a switch made in another directory (selection is
        // directory-scoped -- a real, separate gap, not fixed here) still
        // lands here with something installed and nothing said about it.
        // `switch`, not `install` again, whenever the cache can already answer
        // "installed" for at least one version.
        _ => no_active_sdk_text(&cached_sdk_versions()),
    };
    emit_success(
        g,
        CurrentData {
            subcommand: "current",
            sdk_path,
            readiness,
            source_tier,
        },
        ExitCode::Success,
        Vec::new(),
        text,
    )
}

// ── tan sdk switch <version|path> ────────────────────────────────────────────

/// Repoints the active SDK to `<version|path>` (a bare version segment resolves under the SDK
/// cache root, anything path-shaped resolves under the workspace root — absolute as-is),
/// verifying the path exists and writing the pointer file.
fn run_switch(g: &GlobalArgs, args: &SdkArgs) -> CommandRun {
    match do_switch(g, args) {
        Ok(outcome) => emit_success(
            g,
            outcome.data,
            ExitCode::Success,
            outcome.issues,
            outcome.text,
        ),
        Err(failure) => emit_failure(
            g,
            failure.data,
            ExitCode::RuntimeFailure,
            failure.code,
            failure.message,
            failure.text,
        ),
    }
}

/// The facts + side effects of a `tan sdk switch <version|path>`, before either
/// caller decides how to report it: the real `run_switch` (an envelope of its
/// own), and `run_install`'s auto-select (tan-cli#162 — nothing was selecting
/// a freshly installed SDK, and `tan sdk current` sent the user right back to
/// `install`). One resolver, pointer write and `.west/config` reconciliation
/// for both, so they cannot disagree about what "switched" means; also what
/// keeps this COMPOSING with alp-sdk-vscode#388's own explicit
/// `tan sdk switch` shell-out instead of growing a second copy of it.
struct SwitchOutcome {
    data: SwitchData,
    /// Everything `run_switch` alone would have printed, header line included
    /// (`"Switched {scope} SDK to {version}."` plus the `  path`/`  state`/
    /// `  info`/`  warn`/`  next` detail lines) — a caller folding this into a
    /// larger report appends it as-is rather than re-deriving the header.
    text: Vec<String>,
    issues: Vec<Issue>,
}

/// A refused switch, in the shape [`emit_failure`] (or a caller building its
/// own envelope) needs.
struct SwitchFailure {
    data: SwitchData,
    code: &'static str,
    message: String,
    text: Vec<String>,
}

fn do_switch(g: &GlobalArgs, args: &SdkArgs) -> Result<SwitchOutcome, Box<SwitchFailure>> {
    // Which pointer this switch targets — echoed in every `SwitchData` envelope
    // so a JSON consumer can tell a `--global` switch from a project one.
    let scope = if args.global { "global" } else { "project" };
    let Some(version_or_path) = args.arg.clone() else {
        return Err(Box::new(SwitchFailure {
            data: SwitchData {
                subcommand: "switch",
                sdk_path: String::new(),
                version: None,
                scope,
            },
            code: "missing-version",
            message: "Version or path argument is required.".to_string(),
            text: vec![
                "sdk switch: version or path argument is required.".to_string(),
                "Usage: tan sdk switch <version|path>".to_string(),
            ],
        }));
    };

    // `g.sdk_root` (--sdk-root) is an unrelated global flag meaning "the SDK
    // root other commands should read"; it used to be consulted here via
    // `unwrap_or_else`, so that closure — and the `version_or_path` argument
    // the user just typed — never ran whenever --sdk-root was set.
    // `tan --sdk-root X sdk switch Y` silently re-pinned X and reported
    // success switching to Y. The positional argument must always resolve.
    //
    // The discriminator used to be `Path::new(&version_or_path).is_absolute()`,
    // which only catches absolute paths and misses the documented
    // `<version|path>` *relative* path form entirely: `tan sdk switch
    // ../alp-sdk` fell into the version branch and was joined onto the cache
    // root as `~/.alp/sdk-cache/../alp-sdk` — the sibling checkout the user
    // meant was never consulted. On Windows `is_absolute()` is additionally
    // false for `\some\path` and `C:foo`, which the cache-join branch also
    // then mangled. `is_plain_relative` is the same "bare version segment"
    // shape check `run_install` already uses; anything that isn't one plain
    // segment is a path and is resolved against the workspace root instead —
    // an absolute argument survives that `join` unchanged (`PathBuf::push`
    // discards the base for an absolute pushed path), so the two branches
    // collapse cleanly into one `join` + `normalize_path`.
    //
    // The bare-version branch used to join ONE root, `default_cache_root()`.
    // That is why #62's fix could not reach the layout that reported it: those
    // SDKs live under `~/.alp/sdk` (the extension's install root), so `tan sdk
    // switch v0.13.0` resolved a `~/.alp/sdk-cache/v0.13.0` that does not exist
    // and failed with `path-not-found` before any reconciliation. It now tries
    // every root this machine is known to keep SDKs in (see
    // [`switch_cache_roots`]); `is_plain_relative` above still guarantees the
    // argument is a single path segment, so none of those joins can escape.
    let sdk_path = if is_plain_relative(Path::new(&version_or_path)) {
        resolve_sdk_version_root(
            &version_or_path,
            &switch_cache_roots(g, args),
            |candidate| crate::util::has_loader_script(Path::new(candidate)),
        )
    } else {
        tan_core::normalize_path(&crate::util::cli_workspace_root(g).join(&version_or_path))
            .to_string_lossy()
            .to_string()
    };

    if !Path::new(&sdk_path).exists() {
        return Err(Box::new(SwitchFailure {
            data: SwitchData {
                subcommand: "switch",
                sdk_path: sdk_path.clone(),
                version: None,
                scope,
            },
            code: "path-not-found",
            message: format!("SDK path not found: {sdk_path}"),
            text: vec![
                format!("sdk switch: SDK path does not exist: {sdk_path}"),
                "Run 'tan sdk install <version>' first.".to_string(),
            ],
        }));
    }

    // The path can exist without being an SDK checkout (wrong nesting level,
    // an unpacked release archive, a typo'd sibling dir). Pinning it anyway
    // reported success while `resolve_sdk_root` treats a pointer without the
    // loader script as best-effort and silently falls through to
    // auto-discovery — every later command then builds/flashes against a
    // DIFFERENT SDK than the one the user was just told they switched to.
    if !crate::util::has_loader_script(Path::new(&sdk_path)) {
        return Err(Box::new(SwitchFailure {
            data: SwitchData {
                subcommand: "switch",
                sdk_path: sdk_path.clone(),
                version: None,
                scope,
            },
            code: "not-an-sdk-checkout",
            message: format!(
                "{sdk_path} does not look like an alp-sdk checkout (missing scripts/alp_project.py)."
            ),
            text: vec![
                format!("sdk switch: {sdk_path} does not look like an alp-sdk checkout."),
                "Expected scripts/alp_project.py under the given path.".to_string(),
            ],
        }));
    }

    let pointer_path = switch_pointer_path(
        &crate::util::cli_workspace_root(g)
            .join(".alp")
            .join("sdk-path"),
        &global_default_pointer_path(),
        args.global,
    );
    if let Err(message) = write_sdk_pointer(&pointer_path, &sdk_path) {
        return Err(Box::new(SwitchFailure {
            data: SwitchData {
                sdk_path: sdk_path.clone(),
                subcommand: "switch",
                version: None,
                scope,
            },
            code: "switch-failed",
            message: message.clone(),
            text: vec![
                "sdk switch: failed to update active SDK pointer.".to_string(),
                message,
            ],
        }));
    }

    let readiness = readiness_for(&sdk_path);
    let scope_label = if args.global {
        "machine-global default"
    } else {
        "project"
    };
    let mut text = vec![
        format!(
            "Switched {scope_label} SDK to {}.",
            readiness
                .version
                .clone()
                .unwrap_or_else(|| sdk_path.clone())
        ),
        format!("  path    {sdk_path}"),
        format!("  state   {}", state_label(readiness.state)),
    ];

    // #62: the pointer write above only repoints the ACTIVE-SDK pointer
    // (`.alp/sdk-path` / `~/.alp/sdk-default`); it never touches
    // `<topdir>/.west/config`'s OWN manifest pointer, which `west` reads
    // directly and independently. Left alone, a `.west/config` written by a
    // PRIOR bootstrap/switch under the same topdir keeps naming that old SDK
    // checkout -- silently, until something needs the workspace (#62's `west
    // flash` fell back to an unrelated Zephyr tree and failed with `unknown
    // runner`). Warn, never fail: `sdk switch` is exactly the command a user
    // runs to escape a broken workspace pointer, so hard-failing here would
    // block the escape hatch -- worse than the staleness it replaces. Safe to
    // call unconditionally: a no-op whenever the pointer already matches.
    let mut issues: Vec<Issue> = Vec::new();
    let outcome = reconcile_west_manifest_path_for_switch(&sdk_path);
    match &outcome {
        ManifestReconcile::Rewrote {
            config_path,
            old_rel,
            new_rel,
        } => {
            let old_existed = Path::new(&sdk_path)
                .parent()
                .is_some_and(|topdir| topdir.join(old_rel).exists());
            // Distinguish "pointed at a still-present sibling SDK checkout" from
            // the state #62 reported -- `path` naming a directory that no longer
            // exists on disk at all, which the reporter called unambiguously
            // broken -- rather than folding both into one generic line.
            let message = if old_existed {
                format!(
                    "reconciled {} manifest.path {old_rel} -> {new_rel} (it named a different SDK \
                     checkout under this topdir, #31)",
                    config_path.display()
                )
            } else {
                format!(
                    "reconciled {} manifest.path {old_rel} -> {new_rel} ({old_rel} no longer exists \
                     on disk -- the workspace's only manifest pointer was unambiguously broken, #62)",
                    config_path.display()
                )
            };
            text.push(format!("  info    {message}"));
            issues.push(Issue {
                code: "sdk.west-config-reconciled".to_string(),
                severity: "warning".to_string(),
                message,
            });
        }
        ManifestReconcile::Blocked {
            config_path,
            old_rel,
        } => {
            // `old_rel` exists on disk but is not an alp-sdk checkout -- a real,
            // unrelated directory (e.g. a plain Zephyr workspace) sharing this
            // topdir with the SDK just switched to. Rewriting it would hand
            // that workspace's OWN manifest pointer to an SDK it has nothing to
            // do with, so this is left untouched; surface it rather than stay
            // silent, since the divergence is real even if this switch isn't
            // the one that should fix it.
            let message = format!(
                "{}'s manifest.path names {old_rel}, which is not an alp-sdk checkout -- left \
                 untouched rather than repointing a directory this switch has nothing to do with.",
                config_path.display()
            );
            text.push(format!("  warn    {message}"));
            issues.push(Issue {
                code: "sdk.west-config-not-reconciled".to_string(),
                severity: "warning".to_string(),
                message,
            });
        }
        ManifestReconcile::Failed {
            config_path,
            old_rel,
            reason,
        } => {
            // The repair did NOT happen. Every failure arm used to look exactly
            // like "already correct" from here, so the user was told the switch
            // was clean while `west` kept resolving its manifest from the stale
            // pointer -- the #62 breakage, now merely unreported.
            //
            // `warning` at exit 0, matching this repo's two closest analogues
            // (`clean.remove-failed`, `build.sdk-switch-pristine-failed`): a
            // best-effort repair that failed while the command itself carried
            // on. NOT `error`, tempting as it is -- `Envelope::new` derives
            // `ok` from the exit code, and the extension's `classifyOutcome`
            // derives severity from `ok` alone, so an `error` beside `ok:true`
            // is not merely unprecedented here, it is a shape no consumer
            // reads. The exit code stays 0 deliberately: `sdk switch` is the
            // command a user runs to escape a broken workspace, and failing it
            // would block the escape hatch.
            let message = format!(
                "{} -- `west` will keep resolving the manifest from the stale pointer. Close \
                 anything holding the file open (or clear its read-only flag) and run `tan \
                 bootstrap`.",
                tan_core::describe_reconcile_failure(config_path, old_rel.as_deref(), reason)
            );
            text.push(format!("  warn    {message}"));
            issues.push(Issue {
                code: "sdk.west-config-reconcile-failed".to_string(),
                severity: "warning".to_string(),
                message,
            });
        }
        ManifestReconcile::NotApplicable | ManifestReconcile::AlreadyMatches => {}
    }

    // The `tan bootstrap` advice used to be latched to the rewrite above having
    // FIRED, so a second `sdk switch` -- pointer already correct, `topdir/zephyr`
    // and `modules/` still the previous SDK's trees -- went silent exactly when
    // the user had not acted on it yet. It is derived from workspace state
    // instead: the pointer (via `outcome`) AND whether a `tan bootstrap`
    // `west update` ever ran against THIS SDK (the record beside `.west/config`).
    if workspace_needs_bootstrap(&outcome, workspace_synced_to(&sdk_path)) {
        // Two wordings, both assertable. A diverged pointer PROVES the workspace
        // belongs to another SDK; a matching one with no sync record only means
        // we cannot confirm it -- claiming more there would be a guess (every
        // workspace bootstrapped before the record existed looks like this).
        let message = match &outcome {
            ManifestReconcile::AlreadyMatches => format!(
                "this topdir's workspace has no record of being synced to {sdk_path}; run `tan \
                 bootstrap` to bring its zephyr/ and modules/ trees to the selected SDK."
            ),
            _ => "this topdir's workspace was bootstrapped for a different SDK version; run `tan \
                  bootstrap` to sync it to the newly selected one."
                .to_string(),
        };
        text.push(
            "  next    run `tan bootstrap` to sync the workspace to the new SDK.".to_string(),
        );
        issues.push(Issue {
            code: "sdk.bootstrap-recommended".to_string(),
            severity: "warning".to_string(),
            message,
        });
    }

    Ok(SwitchOutcome {
        data: SwitchData {
            subcommand: "switch",
            sdk_path,
            version: readiness.version,
            scope,
        },
        text,
        issues,
    })
}

/// Cache roots a BARE `tan sdk switch <version>` may resolve against, most
/// authoritative first. `resolve_sdk_version_root` takes the first that holds
/// a real checkout of that version, so order is the whole design:
///
/// 1. `--destination` — the user said, on this very command line, where their
///    SDKs are. Nothing outranks that. (It was `install`-only before; a flag
///    that names the cache root is exactly what the version form needs, and
///    `install --destination X` then `switch --destination X` now agree.)
/// 2. `default_cache_root()` (`~/.alp/sdk-cache`) — where `tan sdk install`
///    puts a clone, so `install v0.13.0 && switch v0.13.0` selects the checkout
///    the install just wrote, never a same-named one elsewhere.
/// 3. The parent of the CURRENTLY ACTIVE SDK. There is no config that declares
///    a cache root, so the only authoritative record of where this machine's
///    SDKs actually live is where the active one sits — `~/.alp/sdk` for #62's
///    reporter, whose SDKs the extension installed. Resolved through the same
///    four-tier chain (`--sdk-root` > project pin > global default > discovery)
///    every other command uses, rather than a second answer to "which SDK".
///
/// Deliberately NOT a filesystem search: three named roots, tried in a fixed
/// order, each of which the user can point at. A `--sdk-root` that names a
/// non-checkout still cannot hijack the argument — it only contributes its
/// parent as a candidate, and a candidate only wins by holding a real checkout.
fn switch_cache_roots(g: &GlobalArgs, args: &SdkArgs) -> Vec<String> {
    let mut roots = Vec::new();
    if let Some(destination) = args.destination.as_deref().map(str::trim) {
        if !destination.is_empty() {
            roots.push(destination.to_string());
        }
    }
    roots.push(default_cache_root());
    let (active, _) = crate::util::resolve_sdk_tiered(g, &crate::util::cli_workspace_root(g));
    if let Some(parent) = active
        .as_deref()
        .map(Path::new)
        .and_then(Path::parent)
        .map(|p| p.to_string_lossy().to_string())
    {
        roots.push(parent);
    }
    roots
}

/// Writes the active-SDK pointer JSON (`sdkPath` + `updatedAt`) to `path`,
/// creating its parent directory as needed. Shared by the project-scoped
/// pointer (`<workspace_root>/.alp/sdk-path`), the machine-global default
/// (`sdk switch --global`, `~/.alp/sdk-default`), and `tan init`'s post-write
/// SDK pin.
pub(crate) fn write_sdk_pointer(path: &Path, sdk_path: &str) -> Result<(), String> {
    if let Some(dir) = path.parent() {
        std::fs::create_dir_all(dir).map_err(|e| e.to_string())?;
    }
    let pointer = serde_json::json!({ "sdkPath": sdk_path, "updatedAt": generated_at_iso() });
    let content = format!(
        "{}\n",
        serde_json::to_string_pretty(&pointer).expect("pointer is serializable")
    );
    std::fs::write(path, content).map_err(|e| e.to_string())
}

/// Picks which pointer file `sdk switch` should write: the machine-global
/// default when `--global` was given, else the project-scoped pin. Pure
/// selection over two already-resolved paths so it is unit-testable without
/// touching the real home directory (`global_pointer` is derived from
/// [`global_default_pointer_path`], which reads `USERPROFILE`/`HOME`).
fn switch_pointer_path(project_pointer: &Path, global_pointer: &Path, global: bool) -> PathBuf {
    if global {
        global_pointer.to_path_buf()
    } else {
        project_pointer.to_path_buf()
    }
}

// ── helpers ───────────────────────────────────────────────────────────────

/// Runs `tan-core`'s readiness check against `sdk_path` using real filesystem probes.
fn readiness_for(sdk_path: &str) -> SdkReadinessReport {
    check_sdk_readiness(
        sdk_path,
        |p| Path::new(p).exists(),
        |p| std::fs::read_to_string(p).ok(),
    )
}

/// Placeholder `Missing` readiness report for failure envelopes where no real check ran.
fn empty_readiness(sdk_path: &str) -> SdkReadinessReport {
    SdkReadinessReport {
        sdk_path: sdk_path.to_string(),
        version: None,
        loader_script_present: false,
        metadata_present: false,
        state: SdkReadinessState::Missing,
        issues: Vec::new(),
    }
}

/// Default SDK cache directory: `~/.alp/sdk-cache` (uses `USERPROFILE` on Windows).
fn default_cache_root() -> String {
    crate::util::home_alp_dir()
        .join("sdk-cache")
        .to_string_lossy()
        .to_string()
}

/// Directory names directly under [`default_cache_root`] that hold a real SDK
/// checkout (`scripts/alp_project.py` present), sorted for a stable message.
/// tan-cli#162: what tells `sdk current`'s "nothing selected" message a
/// populated cache from a genuinely empty one, so it can say `switch` instead
/// of sending the user back to `install`. Deliberately does not guess which
/// one to switch TO (this codebase's own `toolchain::ToolchainRoot::Ambiguous`
/// makes the same call for the Zephyr SDK) — it only lists what exists and
/// lets the user choose.
fn cached_sdk_versions() -> Vec<String> {
    cached_sdk_versions_in(&default_cache_root())
}

/// `sdk current`'s "nothing selected" message (tan-cli#162): `switch`, not
/// `install` again, whenever `cached` says at least one version already sits
/// in the cache. PURE over the already-scanned list, so the actual new
/// wording decision is testable without touching this machine's real
/// `~/.alp/sdk-cache`.
fn no_active_sdk_text(cached: &[String]) -> Vec<String> {
    if cached.is_empty() {
        vec![
            "No active SDK configured for this workspace.".to_string(),
            "Run 'tan sdk install <version>' to get started.".to_string(),
        ]
    } else {
        vec![
            "No active SDK configured for this workspace.".to_string(),
            format!("Already installed: {}", cached.join(", ")),
            "Run 'tan sdk switch <version>' to select one.".to_string(),
        ]
    }
}

/// [`cached_sdk_versions`]'s scan, over an explicit root — split out purely so
/// it is testable against a controlled temp directory rather than this
/// machine's real `~/.alp/sdk-cache`.
fn cached_sdk_versions_in(cache_root: &str) -> Vec<String> {
    let Ok(entries) = std::fs::read_dir(cache_root) else {
        return Vec::new();
    };
    let mut versions: Vec<String> = entries
        .flatten()
        .filter(|entry| crate::util::has_loader_script(&entry.path()))
        .filter_map(|entry| entry.file_name().into_string().ok())
        .collect();
    versions.sort();
    versions
}

/// Machine-global default-SDK pointer file, written by `tan sdk switch
/// --global` and read by every command's `globalDefault` resolution tier.
fn global_default_pointer_path() -> PathBuf {
    crate::util::home_alp_dir().join("sdk-default")
}

/// Maps a `SdkReadinessState` to its lowercase display label.
fn state_label(state: SdkReadinessState) -> &'static str {
    match state {
        SdkReadinessState::Ready => "ready",
        SdkReadinessState::Partial => "partial",
        SdkReadinessState::Missing => "missing",
    }
}

/// Renders a readiness report as text lines (header, path, version, state, and any issues).
fn format_readiness_block(header: &str, report: &SdkReadinessReport) -> Vec<String> {
    let mut lines = vec![
        header.to_string(),
        format!("  path    {}", report.sdk_path),
        format!(
            "  version {}",
            report
                .version
                .clone()
                .unwrap_or_else(|| "(unknown)".to_string())
        ),
        format!("  state   {}", state_label(report.state)),
    ];
    if !report.issues.is_empty() {
        lines.push("  issues:".to_string());
        for issue in &report.issues {
            lines.push(format!("    - {issue}"));
        }
    }
    lines
}

/// Truncates `text` to at most `max` characters (no ellipsis); char-aware, not byte-based.
fn truncate(text: &str, max: usize) -> String {
    if text.chars().count() <= max {
        text.to_string()
    } else {
        text.chars().take(max).collect()
    }
}

/// Empty `Project` for envelopes — `sdk` commands are not scoped to a board.yaml.
fn null_project() -> Project {
    Project {
        root: None,
        board_yaml: None,
    }
}

/// Builds a `CommandRun` for the "success" path (the operation itself ran, but
/// `exit` may still report a failure — e.g. `install` on a Missing readiness):
/// text in non-JSON mode, an envelope in JSON mode carrying `issues` and the
/// SAME `exit` code the process exits with.
fn emit_success<T: Serialize>(
    g: &GlobalArgs,
    data: T,
    exit: ExitCode,
    issues: Vec<Issue>,
    text_lines: Vec<String>,
) -> CommandRun {
    let text = if g.is_json() { Vec::new() } else { text_lines };
    // Used to hardcode `ExitCode::Success.code()` here regardless of `exit`
    // ("mirrors TS createEnvelope") — so `sdk install` on a Missing readiness
    // exited the process with 1 while the JSON envelope claimed
    // `ok:true, exitCode:0, issues:[]`. `Envelope::new` derives `ok` from the
    // exit code it is given, so passing the real `exit` here keeps `ok` and
    // `exitCode` in agreement, matching every other command's envelope.
    let json = g
        .is_json()
        .then(|| Envelope::new("sdk", null_project(), data, issues, exit.code()).to_json());
    CommandRun { exit, text, json }
}

/// Builds a `CommandRun` for the failure path: emits error text or a JSON envelope carrying a
/// single `sdk.<code>` issue and the given exit code.
fn emit_failure<T: Serialize>(
    g: &GlobalArgs,
    data: T,
    exit: ExitCode,
    code: &str,
    message: String,
    text_lines: Vec<String>,
) -> CommandRun {
    let issues = vec![Issue {
        code: format!("sdk.{code}"),
        severity: "error".to_string(),
        message,
    }];
    let text = if g.is_json() { Vec::new() } else { text_lines };
    let json = g
        .is_json()
        .then(|| Envelope::new("sdk", null_project(), data, issues, exit.code()).to_json());
    CommandRun { exit, text, json }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::cli::Format;

    /// Fresh temp dir for one test, tagged and pid-scoped like the other
    /// command test suites in this crate (clean.rs, flash/mod.rs, …).
    fn tmp(tag: &str) -> PathBuf {
        let d = std::env::temp_dir().join(format!("tan-sdk-{tag}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&d);
        std::fs::create_dir_all(&d).unwrap();
        d
    }

    /// Turns `dir` into a directory `has_loader_script` accepts.
    fn make_sdk_root(dir: &Path) {
        std::fs::create_dir_all(dir.join("scripts")).unwrap();
        std::fs::write(dir.join("scripts").join("alp_project.py"), "").unwrap();
    }

    /// `project: Some(<absolute path>)` makes `cli_workspace_root` resolve to
    /// exactly that directory regardless of the real process cwd — `PathBuf::join`
    /// discards `cwd` when the pushed path is absolute.
    fn global(project: &Path, sdk_root: Option<&Path>, format: Format) -> GlobalArgs {
        GlobalArgs {
            project: Some(project.to_string_lossy().into_owned()),
            board_yaml: None,
            sdk_root: sdk_root.map(|p| p.to_string_lossy().into_owned()),
            target: None,
            all: false,
            format,
            verbose: false,
            quiet: false,
            no_color: false,
            non_interactive: false,
            ci: false,
        }
    }

    fn json_data(run: &CommandRun) -> serde_json::Value {
        serde_json::from_str(run.json.as_deref().expect("json envelope")).unwrap()
    }

    #[test]
    fn install_rejects_path_shaped_version_before_touching_disk() {
        // Regression for the supply-chain finding: `run_install` used to do
        // `Path::new(&cache_root).join(&version)` with an unvalidated `version`,
        // so `tan sdk install ../../x` wrote the clone outside the cache root.
        let ws = tmp("install-traversal");
        let g = global(&ws, None, Format::Json);
        let run = run_install(
            &g,
            &SdkArgs {
                subcommand: Some("install".to_string()),
                arg: Some("../../evil".to_string()),
                destination: None,
                global: false,
            },
        );
        assert_eq!(run.exit, ExitCode::RuntimeFailure);
        let data = json_data(&run);
        assert_eq!(data["issues"][0]["code"], "sdk.invalid-version");
        // Nothing was created — the guard fired before any fs/network I/O.
        assert!(!ws.join("../../evil").exists());
    }

    #[test]
    fn switch_uses_the_positional_argument_even_when_sdk_root_is_set() {
        // Regression: the relative-path branch used to be
        // `g.sdk_root.clone().unwrap_or_else(|| … version_or_path …)`, so the
        // positional argument was only used when --sdk-root was ABSENT. With
        // --sdk-root set, `sdk switch <version>` silently re-pinned the old
        // root instead of resolving the version the user asked for.
        let ws = tmp("switch-arg-not-discarded");
        let other_root = tmp("switch-arg-other-root");
        make_sdk_root(&other_root);
        let g = global(&ws, Some(&other_root), Format::Json);
        let run = run_switch(
            &g,
            &SdkArgs {
                subcommand: Some("switch".to_string()),
                arg: Some("does-not-exist-anywhere".to_string()),
                destination: None,
                global: false,
            },
        );
        // Resolves under the cache root and (as expected) doesn't exist —
        // the important assertion is WHICH path it tried, not that it succeeds.
        assert_eq!(run.exit, ExitCode::RuntimeFailure);
        let data = json_data(&run);
        let sdk_path = data["data"]["sdkPath"].as_str().unwrap();
        assert!(
            sdk_path.contains("does-not-exist-anywhere"),
            "sdk_path should resolve the positional argument, got {sdk_path}"
        );
        assert_ne!(sdk_path, other_root.to_string_lossy());
    }

    #[test]
    fn switch_resolves_relative_path_form_against_workspace_not_cache_root() {
        // Regression: the discriminator used to be `Path::new(&version_or_path)
        // .is_absolute()`, so a relative *path* (as opposed to a bare version
        // segment) fell into the version branch and was joined onto the SDK
        // cache root instead of resolved as the sibling checkout it names —
        // `tan sdk switch ../alp-sdk` pinned `<cache_root>/../alp-sdk`, not the
        // directory next to the workspace.
        let ws = tmp("switch-relpath-ws");
        let sibling = ws.parent().unwrap().join(format!(
            "tan-sdk-switch-relpath-sibling-{}",
            std::process::id()
        ));
        let _ = std::fs::remove_dir_all(&sibling);
        make_sdk_root(&sibling);
        let g = global(&ws, None, Format::Json);

        let run = run_switch(
            &g,
            &SdkArgs {
                subcommand: Some("switch".to_string()),
                arg: Some(format!(
                    "../{}",
                    sibling.file_name().unwrap().to_string_lossy()
                )),
                destination: None,
                global: false,
            },
        );
        assert_eq!(run.exit, ExitCode::Success);
        let data = json_data(&run);
        let sdk_path = data["data"]["sdkPath"].as_str().unwrap();
        assert_eq!(
            Path::new(sdk_path),
            tan_core::normalize_path(&sibling),
            "relative path form must resolve the sibling checkout, not join onto the cache root"
        );
        assert!(!sdk_path.contains("sdk-cache"));

        let _ = std::fs::remove_dir_all(&sibling);
    }

    #[test]
    fn switch_refuses_a_path_that_is_not_an_sdk_checkout() {
        // Regression: `run_switch` used to gate only on `Path::exists()`, so
        // pointing it at any existing directory "succeeded" and silently wrote
        // a pointer that every consumer's `has_loader_script` check then
        // ignores, falling back to auto-discovery of a DIFFERENT SDK.
        let ws = tmp("switch-not-sdk-ws");
        let not_an_sdk = tmp("switch-not-sdk-target");
        let g = global(&ws, None, Format::Json);
        let run = run_switch(
            &g,
            &SdkArgs {
                subcommand: Some("switch".to_string()),
                arg: Some(not_an_sdk.to_string_lossy().into_owned()),
                destination: None,
                global: false,
            },
        );
        assert_eq!(run.exit, ExitCode::RuntimeFailure);
        let data = json_data(&run);
        assert_eq!(data["issues"][0]["code"], "sdk.not-an-sdk-checkout");
        // No pointer was written.
        assert!(!ws.join(".alp").join("sdk-path").exists());
    }

    #[test]
    fn switch_reconciles_a_stale_west_config_pointer() {
        // Regression for #62: `write_sdk_pointer` only repoints the
        // ACTIVE-SDK pointer (`.alp/sdk-path`); `run_switch` used to return
        // right after that, never touching `<topdir>/.west/config`'s OWN
        // manifest pointer, which `west` reads directly. Two SDK versions
        // sharing one topdir (like `~/.alp/sdk-cache/*`) -- switching from
        // the first to the second must reconcile it, same as `tan bootstrap`
        // already does (#31).
        let ws = tmp("switch-reconcile-ws");
        let topdir = tmp("switch-reconcile-topdir");
        let old_sdk = topdir.join("v0.11.0");
        make_sdk_root(&old_sdk); // still present -- the benign-divergence case.
        let new_sdk = topdir.join("v0.13.0");
        make_sdk_root(&new_sdk);
        let west_dir = topdir.join(".west");
        std::fs::create_dir_all(&west_dir).unwrap();
        std::fs::write(
            west_dir.join("config"),
            "[manifest]\npath = v0.11.0\nfile = west.yml\n",
        )
        .unwrap();
        let g = global(&ws, None, Format::Json);

        let run = run_switch(
            &g,
            &SdkArgs {
                subcommand: Some("switch".to_string()),
                arg: Some(new_sdk.to_string_lossy().into_owned()),
                destination: None,
                global: false,
            },
        );

        assert_eq!(run.exit, ExitCode::Success);
        assert_eq!(
            std::fs::read_to_string(west_dir.join("config")).unwrap(),
            "[manifest]\npath = v0.13.0\nfile = west.yml\n",
            "switch must rewrite the topdir's .west/config, not just the active-SDK pointer"
        );
        let data = json_data(&run);
        let codes: Vec<String> = data["issues"]
            .as_array()
            .unwrap()
            .iter()
            .map(|issue| issue["code"].as_str().unwrap().to_string())
            .collect();
        assert!(codes.contains(&"sdk.west-config-reconciled".to_string()));
        assert!(
            codes.contains(&"sdk.bootstrap-recommended".to_string()),
            "a reconciled pointer means this topdir was bootstrapped for the OLD SDK -- the \
             result must name `tan bootstrap` as the next step, not report a bare success"
        );
    }

    #[test]
    fn switch_reconcile_names_a_stale_path_that_no_longer_exists_as_unambiguously_broken() {
        // The exact state #62 reported: `.west/config`'s `path` names a
        // version pruned from the cache entirely -- not just "a different
        // but still-present sibling". Worth a distinct wording rather than
        // folding both cases into one generic reconciliation line.
        let ws = tmp("switch-reconcile-pruned-ws");
        let topdir = tmp("switch-reconcile-pruned-topdir");
        let new_sdk = topdir.join("v0.13.0");
        make_sdk_root(&new_sdk);
        // deliberately NOT creating topdir/v0.11.0 -- it was pruned from the cache.
        let west_dir = topdir.join(".west");
        std::fs::create_dir_all(&west_dir).unwrap();
        std::fs::write(
            west_dir.join("config"),
            "[manifest]\npath = v0.11.0\nfile = west.yml\n",
        )
        .unwrap();
        let g = global(&ws, None, Format::Json);

        let run = run_switch(
            &g,
            &SdkArgs {
                subcommand: Some("switch".to_string()),
                arg: Some(new_sdk.to_string_lossy().into_owned()),
                destination: None,
                global: false,
            },
        );

        assert_eq!(run.exit, ExitCode::Success);
        let data = json_data(&run);
        let message = data["issues"]
            .as_array()
            .unwrap()
            .iter()
            .find(|issue| issue["code"] == "sdk.west-config-reconciled")
            .expect("expected a west-config-reconciled issue")["message"]
            .as_str()
            .unwrap()
            .to_string();
        assert!(
            message.contains("no longer exists on disk"),
            "expected the unambiguously-broken wording (#62), got: {message}"
        );
    }

    #[test]
    fn switch_does_not_repoint_a_west_config_naming_a_real_non_sdk_directory() {
        // A plain Zephyr workspace sharing a topdir with the alp-sdk checkout
        // being switched to: `.west/config` names `zephyr` (a real, unrelated
        // directory with no `scripts/alp_project.py`), not a stale alp-sdk
        // sibling. Rewriting it to point at the SDK would silently repoint a
        // workspace this switch has no business touching -- reconciliation
        // must be gated on the OLD target actually being an alp-sdk checkout
        // (or missing entirely, #62's reported state), not fire unconditionally
        // whenever the names diverge.
        let ws = tmp("switch-guard-real-dir-ws");
        let topdir = tmp("switch-guard-real-dir-topdir");
        std::fs::create_dir_all(topdir.join("zephyr")).unwrap();
        let new_sdk = topdir.join("alp-sdk");
        make_sdk_root(&new_sdk);
        let west_dir = topdir.join(".west");
        std::fs::create_dir_all(&west_dir).unwrap();
        let original = "[manifest]\npath = zephyr\nfile = west.yml\n";
        std::fs::write(west_dir.join("config"), original).unwrap();
        let g = global(&ws, None, Format::Json);

        let run = run_switch(
            &g,
            &SdkArgs {
                subcommand: Some("switch".to_string()),
                arg: Some(new_sdk.to_string_lossy().into_owned()),
                destination: None,
                global: false,
            },
        );

        assert_eq!(run.exit, ExitCode::Success);
        assert_eq!(
            std::fs::read_to_string(west_dir.join("config")).unwrap(),
            original,
            "an unrelated real directory's manifest.path must never be repointed by sdk switch"
        );
        let data = json_data(&run);
        let codes: Vec<String> = data["issues"]
            .as_array()
            .unwrap()
            .iter()
            .map(|issue| issue["code"].as_str().unwrap().to_string())
            .collect();
        assert!(codes.contains(&"sdk.west-config-not-reconciled".to_string()));
        assert!(!codes.contains(&"sdk.west-config-reconciled".to_string()));
    }

    /// Every issue code from a run's envelope.
    fn issue_codes(run: &CommandRun) -> Vec<String> {
        json_data(run)["issues"]
            .as_array()
            .unwrap()
            .iter()
            .map(|issue| issue["code"].as_str().unwrap().to_string())
            .collect()
    }

    /// A `sdk switch <arg>` run against `g`, the shape every test below drives.
    fn switch(g: &GlobalArgs, arg: &str) -> CommandRun {
        run_switch(
            g,
            &SdkArgs {
                subcommand: Some("switch".to_string()),
                arg: Some(arg.to_string()),
                destination: None,
                global: false,
            },
        )
    }

    #[test]
    fn switch_resolves_a_bare_version_in_the_root_the_active_sdk_lives_in() {
        // #62's layout, and why its fix could not fire there: the reporter's
        // SDKs sit under `~/.alp/sdk` (the extension's install root), not
        // `~/.alp/sdk-cache`. Joining the cache root ALONE made `tan sdk switch
        // v0.13.0` fail with `path-not-found` on a version that is right there
        // on disk -- so the whole `.west/config` reconciliation was unreachable
        // for exactly the users who reported needing it.
        //
        // The version is pid-tagged so the developer's REAL `~/.alp/sdk-cache`
        // (an earlier, higher-precedence root) cannot happen to hold it.
        let ws = tmp("switch-version-root-ws");
        let root = tmp("switch-version-root-cache");
        let version = format!("v0.13.0-tan-test-{}", std::process::id());
        let active = root.join("v0.11.0");
        make_sdk_root(&active);
        let wanted = root.join(&version);
        make_sdk_root(&wanted);
        // `--sdk-root` names the ACTIVE SDK; its parent is the root this
        // machine actually keeps SDKs in.
        let g = global(&ws, Some(&active), Format::Json);

        let run = switch(&g, &version);

        assert_eq!(run.exit, ExitCode::Success);
        assert_eq!(
            Path::new(json_data(&run)["data"]["sdkPath"].as_str().unwrap()),
            wanted,
            "a bare version must resolve in the root the active SDK lives in, not only in \
             ~/.alp/sdk-cache"
        );
    }

    #[test]
    fn switch_tries_the_destination_root_first_but_only_for_a_real_checkout() {
        // Pins the CLI's root LIST, which the pure `resolve_sdk_version_root`
        // tests cannot see: that `--destination` is in it at all (it was
        // install-only, and `--help` now promises otherwise), that it comes
        // FIRST, and that the probe is `has_loader_script` rather than
        // `exists` -- with `exists` a husk directory in an earlier root wins
        // and the command dies on `not-an-sdk-checkout` over a real checkout
        // sitting in a later one.
        let ws = tmp("switch-destination-root-ws");
        let root_a = tmp("switch-destination-root-a"); // --destination
        let root_b = tmp("switch-destination-root-b"); // parent of the active SDK
        let version = format!("v0.13.0-tan-test-{}", std::process::id());
        // A husk in the FIRST root, a real checkout in the last.
        std::fs::create_dir_all(root_a.join(&version)).unwrap();
        make_sdk_root(&root_b.join(&version));
        let active = root_b.join("v0.11.0");
        make_sdk_root(&active);
        let g = global(&ws, Some(&active), Format::Json);
        let switch_to = |version: &str| {
            run_switch(
                &g,
                &SdkArgs {
                    subcommand: Some("switch".to_string()),
                    arg: Some(version.to_string()),
                    destination: Some(root_a.to_string_lossy().into_owned()),
                    global: false,
                },
            )
        };

        let husk = switch_to(&version);
        assert_eq!(husk.exit, ExitCode::Success);
        assert_eq!(
            Path::new(json_data(&husk)["data"]["sdkPath"].as_str().unwrap()),
            root_b.join(&version),
            "an empty directory in an earlier root must not shadow a real checkout in a later one"
        );

        // Same command once the destination root holds a REAL checkout: it now
        // outranks both the cache root and the active SDK's own root.
        make_sdk_root(&root_a.join(&version));
        let real = switch_to(&version);
        assert_eq!(real.exit, ExitCode::Success);
        assert_eq!(
            Path::new(json_data(&real)["data"]["sdkPath"].as_str().unwrap()),
            root_a.join(&version),
            "--destination must be consulted, and consulted before every other root"
        );
    }

    #[test]
    fn switch_surfaces_a_west_config_it_could_not_rewrite() {
        // Gap 2: every failure arm of the reconcile used to return the same
        // `None` as "already correct", so a `.west/config` that could not be
        // rewritten -- read-only, or (routinely, on Windows) held open by
        // another process -- reported a clean switch over a workspace still
        // pointing at the old SDK. Occupying the atomic temp path with a
        // directory forces that failure portably.
        let ws = tmp("switch-reconcile-failed-ws");
        let topdir = tmp("switch-reconcile-failed-topdir");
        let new_sdk = topdir.join("v0.13.0");
        make_sdk_root(&new_sdk);
        let west_dir = topdir.join(".west");
        std::fs::create_dir_all(&west_dir).unwrap();
        let original = "[manifest]\npath = v0.11.0\nfile = west.yml\n";
        std::fs::write(west_dir.join("config"), original).unwrap();
        std::fs::create_dir_all(west_dir.join(format!("config.{}.tan-tmp", std::process::id())))
            .unwrap();
        let g = global(&ws, None, Format::Json);

        let run = switch(&g, &new_sdk.to_string_lossy());

        // The switch itself succeeded (the active-SDK pointer IS written) --
        // hard-failing would block the escape hatch. But the workspace is still
        // broken and the envelope has to say so.
        assert_eq!(run.exit, ExitCode::Success);
        assert_eq!(
            std::fs::read_to_string(west_dir.join("config")).unwrap(),
            original
        );
        let data = json_data(&run);
        let codes = issue_codes(&run);
        assert!(
            codes.contains(&"sdk.west-config-reconcile-failed".to_string()),
            "a rewrite that did not happen must not look like a no-op: {codes:?}"
        );
        // `ok` is derived from the exit code, and the extension derives an
        // issue's presentation from `ok` alone -- an `error` beside `ok:true`
        // is a shape no consumer reads. Best-effort-repair-failed is a
        // `warning` here, as it is for `clean.remove-failed`.
        let failure = data["issues"]
            .as_array()
            .unwrap()
            .iter()
            .find(|issue| issue["code"] == "sdk.west-config-reconcile-failed")
            .expect("the reconcile-failure issue");
        assert_eq!(data["ok"], true);
        assert_eq!(failure["severity"], "warning");
        assert!(
            codes.contains(&"sdk.bootstrap-recommended".to_string()),
            "a pointer still naming another SDK is stale whatever any sync record says"
        );
    }

    #[test]
    fn switch_keeps_recommending_bootstrap_until_the_workspace_is_actually_synced() {
        // Gap 3: the advice used to be latched to the rewrite FIRING, so the
        // second `tan sdk switch v0.13.0` -- config already reconciled by the
        // first, `zephyr/` and `modules/` still the old SDK's trees -- said
        // nothing at all. It is derived from workspace state now, so it keeps
        // firing until a `tan bootstrap` `west update` actually syncs the trees.
        let ws = tmp("switch-advice-state-ws");
        let topdir = tmp("switch-advice-state-topdir");
        let new_sdk = topdir.join("v0.13.0");
        make_sdk_root(&new_sdk);
        let west_dir = topdir.join(".west");
        std::fs::create_dir_all(&west_dir).unwrap();
        // Already reconciled -- exactly the state a second switch sees.
        std::fs::write(
            west_dir.join("config"),
            "[manifest]\npath = v0.13.0\nfile = west.yml\n",
        )
        .unwrap();
        let g = global(&ws, None, Format::Json);

        let before = switch(&g, &new_sdk.to_string_lossy());
        assert_eq!(before.exit, ExitCode::Success);
        assert!(
            issue_codes(&before).contains(&"sdk.bootstrap-recommended".to_string()),
            "a matching pointer over unsynced trees is still a stale workspace"
        );

        // What `tan bootstrap` records once its `west update` has run.
        crate::commands::bootstrap::record_workspace_sdk(&topdir, &new_sdk.to_string_lossy());

        let after = switch(&g, &new_sdk.to_string_lossy());
        assert_eq!(after.exit, ExitCode::Success);
        assert!(
            !issue_codes(&after).contains(&"sdk.bootstrap-recommended".to_string()),
            "a workspace provably synced to this SDK must not be nagged"
        );
    }

    #[test]
    fn switch_and_current_use_project_scoped_workspace_root_not_process_cwd() {
        // Regression: the project pointer writer/`run_current` used to resolve
        // `.alp/sdk-path` from the bare process cwd, while every consumer
        // (`resolve_sdk_root`, `resolve_cli_project_context`) resolves it under
        // `cli_workspace_root` (cwd joined with --project). With --project set,
        // the pointer was written where nothing reads it.
        let ws = tmp("switch-current-project-scoped");
        let sdk_root = tmp("switch-current-sdk-root");
        make_sdk_root(&sdk_root);
        let g = global(&ws, None, Format::Json);

        let switched = run_switch(
            &g,
            &SdkArgs {
                subcommand: Some("switch".to_string()),
                arg: Some(sdk_root.to_string_lossy().into_owned()),
                destination: None,
                global: false,
            },
        );
        assert_eq!(switched.exit, ExitCode::Success);
        assert_eq!(
            json_data(&switched)["data"]["scope"],
            "project",
            "a non-global switch must report project scope in the envelope"
        );
        assert!(
            ws.join(".alp").join("sdk-path").exists(),
            "pointer should be written under the --project workspace root"
        );

        let current = run_current(&g);
        let data = json_data(&current);
        assert_eq!(
            data["data"]["sdkPath"].as_str().unwrap(),
            sdk_root.to_string_lossy()
        );
        assert_eq!(data["data"]["sourceTier"], "projectPin");
    }

    #[test]
    fn current_reports_sdk_root_flag_tier_when_set() {
        // `--sdk-root` is terminal and wins over any project pin — `sourceTier`
        // must reflect that even though no `tan sdk switch` ever ran.
        let ws = tmp("current-sdk-root-flag-ws");
        let sdk_root = tmp("current-sdk-root-flag-root");
        make_sdk_root(&sdk_root);
        let g = global(&ws, Some(&sdk_root), Format::Json);

        let current = run_current(&g);
        let data = json_data(&current);
        assert_eq!(
            data["data"]["sdkPath"].as_str().unwrap(),
            sdk_root.to_string_lossy()
        );
        assert_eq!(data["data"]["sourceTier"], "sdkRootFlag");
    }

    #[test]
    fn switch_pointer_path_selects_global_or_project() {
        // Pure selection over two already-resolved paths — deliberately not
        // exercised end-to-end through `run_switch --global`, which would have
        // to write under the REAL `USERPROFILE`/`HOME` (`global_default_pointer_path`
        // is not injectable); this covers the branch without ever touching the
        // real home directory from a test.
        let project = Path::new("/ws/.alp/sdk-path");
        let global = Path::new("/home/.alp/sdk-default");
        assert_eq!(switch_pointer_path(project, global, false), project);
        assert_eq!(switch_pointer_path(project, global, true), global);
    }

    /// One-shot loopback HTTP server: accepts a single connection, drains the
    /// request and writes `body` back as a `200`. Returns the `host:port` to
    /// aim at.
    ///
    /// Deliberately hand-rolled rather than a mock-HTTP crate — it is a dozen
    /// lines against `std::net`, and the point of the test is which *agent and
    /// URL* `fetch_releases` uses, which no mocking layer can observe for us.
    fn serve_one_response(body: &'static str) -> String {
        let listener = std::net::TcpListener::bind("127.0.0.1:0").expect("bind a loopback port");
        let addr = listener
            .local_addr()
            .expect("the bound address")
            .to_string();
        std::thread::spawn(move || {
            let Ok((mut stream, _)) = listener.accept() else {
                return;
            };
            // Read *something* first: writing the response while the request is
            // still in flight can RST the connection on Windows before ureq has
            // finished sending, which would surface as a flaky transport error.
            let mut scratch = [0u8; 2048];
            let _ = std::io::Read::read(&mut stream, &mut scratch);
            let response = format!(
                "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
                body.len()
            );
            let _ = std::io::Write::write_all(&mut stream, response.as_bytes());
        });
        addr
    }

    #[test]
    fn fetch_releases_requests_the_agent_and_url_it_is_given() {
        // The seam this covers: `run_list` handing `fetch_releases` the shared
        // agent. Hard-wire `ureq::get(GITHUB_RELEASES_URL)` back into the body
        // and this test stops seeing the canned tag — it either reaches the real
        // api.github.com (whose releases are not `v9.9.9`) or fails outright on
        // a runner with no network. Either way it is this assertion that says so.
        let body = concat!(
            r#"[{"tag_name":"v9.9.9","published_at":"2026-01-02T03:04:05Z","#,
            r#""tarball_url":"https://example.invalid/v9.9.9.tar.gz","#,
            r#""body":"Head line.\n\nDetail that is not the summary.","#,
            r#""draft":true,"prerelease":true}]"#
        );
        let addr = serve_one_response(body);
        // A no-proxy agent with a short cap: the environment must not steer a
        // loopback request, and a wedged accept must fail the test rather than
        // hang `cargo test`.
        let agent = crate::http::build_agent(std::time::Duration::from_secs(10), None);

        let releases = fetch_releases(&agent, &format!("http://{addr}/releases"))
            .expect("the canned 200 parses");

        assert_eq!(releases.len(), 1);
        assert_eq!(releases[0].tag, "v9.9.9");
        assert_eq!(releases[0].published_at, "2026-01-02T03:04:05Z");
        // First paragraph only — proves the `tan-core` parse ran on OUR body.
        assert_eq!(releases[0].release_notes_summary, "Head line.");
        // #122: draft/prerelease must travel the full parse path over a real
        // socket, not just through the in-process unit test.
        assert!(releases[0].draft);
        assert!(releases[0].prerelease);
    }

    #[test]
    fn format_release_table_shows_draft_and_prerelease_flags() {
        // #122: a terminal reader of `tan sdk list` must be able to see that a
        // release is flagged — the table must not silently drop what
        // `fetch_releases` already parsed.
        let releases = vec![
            SdkRelease {
                tag: "v1.5.0".to_string(),
                published_at: "2024-01-02T00:00:00Z".to_string(),
                tarball_url: "u".to_string(),
                release_notes_summary: String::new(),
                release_notes: String::new(),
                draft: false,
                prerelease: false,
            },
            SdkRelease {
                tag: "v2.0.0-rc1".to_string(),
                published_at: "2024-02-03T00:00:00Z".to_string(),
                tarball_url: "u".to_string(),
                release_notes_summary: String::new(),
                release_notes: String::new(),
                draft: false,
                prerelease: true,
            },
            SdkRelease {
                tag: "v3.0.0-draft".to_string(),
                published_at: "2024-03-04T00:00:00Z".to_string(),
                tarball_url: "u".to_string(),
                release_notes_summary: String::new(),
                release_notes: String::new(),
                draft: true,
                prerelease: false,
            },
            SdkRelease {
                tag: "v4.0.0-rc1".to_string(),
                published_at: "2024-04-05T00:00:00Z".to_string(),
                tarball_url: "u".to_string(),
                release_notes_summary: String::new(),
                release_notes: String::new(),
                draft: true,
                prerelease: true,
            },
        ];
        let lines = format_release_table(&releases);
        assert!(lines[1].contains("v1.5.0") && !lines[1].contains('['));
        assert!(lines[2].contains("v2.0.0-rc1") && lines[2].contains("[prerelease]"));
        assert!(lines[3].contains("v3.0.0-draft") && lines[3].contains("[draft]"));
        assert!(lines[4].contains("v4.0.0-rc1") && lines[4].contains("[draft, prerelease]"));
    }

    // ── tan-cli#162: install selects, current recommends switch ──────────────

    #[test]
    fn fold_auto_select_appends_the_switch_text_and_reports_true_on_success() {
        let mut text = vec!["SDK v0.13.0 installed".to_string()];
        let mut issues: Vec<Issue> = Vec::new();
        let outcome = Ok(SwitchOutcome {
            data: SwitchData {
                subcommand: "switch",
                sdk_path: "/cache/v0.13.0".to_string(),
                version: Some("0.13.0".to_string()),
                scope: "project",
            },
            text: vec!["Switched project SDK to 0.13.0.".to_string()],
            issues: vec![Issue {
                code: "sdk.bootstrap-recommended".to_string(),
                severity: "warning".to_string(),
                message: "run tan bootstrap".to_string(),
            }],
        });
        let selected = fold_auto_select(&mut text, &mut issues, outcome);
        assert!(selected);
        assert_eq!(
            text,
            vec![
                "SDK v0.13.0 installed".to_string(),
                String::new(),
                "Switched project SDK to 0.13.0.".to_string(),
                "  note    scoped to this directory -- run future commands from here, or \
                 `tan sdk switch --global` to select machine-wide."
                    .to_string(),
            ]
        );
        assert_eq!(issues.len(), 1);
        assert_eq!(issues[0].code, "sdk.bootstrap-recommended");
    }

    #[test]
    fn fold_auto_select_omits_the_directory_note_for_a_global_switch() {
        // A `--global` auto-select pins a machine-wide pointer, so the
        // directory-scoping caveat above does not apply to it.
        let mut text = vec!["SDK v0.13.0 installed".to_string()];
        let mut issues: Vec<Issue> = Vec::new();
        let outcome = Ok(SwitchOutcome {
            data: SwitchData {
                subcommand: "switch",
                sdk_path: "/cache/v0.13.0".to_string(),
                version: Some("0.13.0".to_string()),
                scope: "global",
            },
            text: vec!["Switched global SDK to 0.13.0.".to_string()],
            issues: vec![],
        });
        fold_auto_select(&mut text, &mut issues, outcome);
        assert!(
            !text.iter().any(|l| l.contains("scoped to this directory")),
            "{text:?}"
        );
    }

    #[test]
    fn fold_auto_select_reports_false_and_a_prefixed_warning_on_failure() {
        let mut text = vec!["SDK v0.13.0 installed".to_string()];
        let mut issues: Vec<Issue> = Vec::new();
        let outcome: Result<SwitchOutcome, Box<SwitchFailure>> = Err(Box::new(SwitchFailure {
            data: SwitchData {
                subcommand: "switch",
                sdk_path: String::new(),
                version: None,
                scope: "project",
            },
            code: "switch-failed",
            message: "failed to update active SDK pointer".to_string(),
            text: vec!["ignored -- install builds its own text".to_string()],
        }));
        let selected = fold_auto_select(&mut text, &mut issues, outcome);
        assert!(!selected);
        // The install-side install still succeeded -- only ONE new warning
        // line naming why the auto-select didn't happen, plus a next step.
        assert_eq!(text.len(), 4, "{text:?}");
        assert!(
            text[2].contains("could not select it automatically")
                && text[2].contains("failed to update active SDK pointer"),
            "{text:?}"
        );
        assert!(text[3].contains("tan sdk switch"), "{text:?}");
        assert_eq!(issues.len(), 1);
        // Prefixed with `sdk.` -- every other issue this file emits is.
        assert_eq!(issues[0].code, "sdk.switch-failed");
        assert_eq!(issues[0].severity, "warning");
    }

    #[test]
    fn cached_sdk_versions_in_lists_only_real_checkouts_sorted() {
        let root = tmp("cached-versions");
        make_sdk_root(&root.join("v0.13.0"));
        make_sdk_root(&root.join("v0.9.0"));
        // Not a real checkout -- must be excluded, not just skipped-with-a-warning.
        std::fs::create_dir_all(root.join("not-an-sdk")).unwrap();
        // A stray FILE beside the version directories -- `read_dir` yields it,
        // `has_loader_script` must reject it without panicking.
        std::fs::write(root.join("README.txt"), "").unwrap();

        let versions = cached_sdk_versions_in(&root.to_string_lossy());
        assert_eq!(versions, vec!["v0.13.0".to_string(), "v0.9.0".to_string()]);

        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn cached_sdk_versions_in_is_empty_for_a_missing_root() {
        assert!(cached_sdk_versions_in("/tan-cli-test-no-such-cache-root-xyz").is_empty());
    }

    #[test]
    fn no_active_sdk_text_recommends_install_only_when_the_cache_is_genuinely_empty() {
        let empty = no_active_sdk_text(&[]);
        assert!(empty.iter().any(|l| l.contains("sdk install")), "{empty:?}");
        assert!(!empty.iter().any(|l| l.contains("sdk switch")), "{empty:?}");
    }

    #[test]
    fn no_active_sdk_text_recommends_switch_and_names_the_cache_when_populated() {
        // Regression: tan-cli#162 -- this used to say `install` even when the
        // cache already held the version `install` had just written, sending
        // the user back into the command that put them here.
        let populated = no_active_sdk_text(&["v0.13.0".to_string(), "v0.9.0".to_string()]);
        assert!(
            populated.iter().any(|l| l.contains("sdk switch")),
            "{populated:?}"
        );
        assert!(
            populated
                .iter()
                .any(|l| l.contains("v0.13.0") && l.contains("v0.9.0")),
            "{populated:?}"
        );
        assert!(
            !populated.iter().any(|l| l.contains("sdk install")),
            "{populated:?}"
        );
    }

    #[test]
    fn install_leaves_an_existing_selection_alone() {
        // tan-cli#162's auto-select must not fire when something is ALREADY
        // active -- installing a second version must never silently repoint a
        // selection the user (or another tool) made on purpose. Deterministic
        // via `--sdk-root`, which `resolve_sdk_tiered` honours unconditionally
        // as the top tier regardless of the real machine's `~/.alp` state (see
        // `resolve_sdk_tiered_prefers_sdk_root_flag`) -- the "nothing active"
        // branch is covered instead by `fold_auto_select`'s own tests above,
        // which do not depend on this host's real SDK configuration at all.
        let ws = tmp("install-auto-select-already-active");
        let cache_root = tmp("install-auto-select-cache");
        let already_active = tmp("install-auto-select-existing-sdk");
        make_sdk_root(&already_active);
        // The version this install "clones" -- pre-seeded so `run_install`
        // takes its `already_installed` shortcut and never touches the network.
        make_sdk_root(&cache_root.join("v9.9.9"));

        let g = global(&ws, Some(&already_active), Format::Json);
        let run = run_install(
            &g,
            &SdkArgs {
                subcommand: Some("install".to_string()),
                arg: Some("v9.9.9".to_string()),
                destination: Some(cache_root.to_string_lossy().into_owned()),
                global: false,
            },
        );
        assert_eq!(run.exit, ExitCode::Success);
        let data = json_data(&run);
        // An SDK was already active (the `--sdk-root` flag) -- install must
        // not silently repoint it.
        assert_eq!(data["data"]["selected"], false, "{data}");

        let _ = std::fs::remove_dir_all(&ws);
        let _ = std::fs::remove_dir_all(&cache_root);
        let _ = std::fs::remove_dir_all(&already_active);
    }
}
