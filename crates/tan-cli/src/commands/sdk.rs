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
    GITHUB_RELEASES_URL, SdkReadinessReport, SdkReadinessState, SdkRelease, SdkSourceTier,
    check_sdk_readiness, describe_network_error, is_plain_relative, parse_remote_sdk_releases,
};

use super::CommandRun;
use super::bootstrap::{SwitchReconcile, reconcile_west_manifest_path_for_switch};
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
    let pb = crate::progress::spinner(g, "Fetching ALP SDK releases…");
    let result = fetch_releases();
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

/// GETs the GitHub releases API and parses the JSON into `SdkRelease`s via `tan-core`.
///
/// Goes through [`crate::http::agent`] rather than the bare `ureq::get` this used
/// to call: that default agent ignores both `HTTPS_PROXY`/`HTTP_PROXY` and the OS
/// trust store, so this command was unusable behind a corporate proxy or a
/// TLS-intercepting middlebox. `describe_network_error` then names that as the
/// likely cause instead of leaving the user with a raw transport error.
fn fetch_releases() -> Result<Vec<SdkRelease>, String> {
    let response = crate::http::agent()
        .get(GITHUB_RELEASES_URL)
        .set("User-Agent", "tan-cli/0")
        .set("Accept", "application/vnd.github+json")
        .set("X-GitHub-Api-Version", "2022-11-28")
        .call()
        .map_err(|e| describe_network_error(&e.to_string(), crate::http::proxy_configured()))?;
    let value: serde_json::Value = response.into_json().map_err(|e| e.to_string())?;
    parse_remote_sdk_releases(&value)
}

/// Formats releases into human-readable lines (tag, publish date, truncated notes).
fn format_release_table(releases: &[SdkRelease]) -> Vec<String> {
    if releases.is_empty() {
        return vec!["No SDK releases found.".to_string()];
    }
    let mut lines = vec![format!("ALP SDK releases ({})", releases.len())];
    for rel in releases {
        let date: String = rel.published_at.chars().take(10).collect();
        let notes = if rel.release_notes_summary.is_empty() {
            String::new()
        } else {
            format!("   {}", truncate(&rel.release_notes_summary, 60))
        };
        lines.push(format!("  {:<12} {date}{notes}", rel.tag));
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
    let text = format_readiness_block(&format!("SDK {version} installed"), &readiness);
    // A Missing readiness used to reach the envelope only as this text block,
    // which `--format json` throws away entirely — the JSON consumer (the
    // vscode extension) got exitCode 0/ok:true with an empty `issues` array
    // for an SDK clone that is not actually usable. Surface it as a real Issue
    // too, not just prose.
    let issues = if readiness.state == SdkReadinessState::Missing {
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
    emit_success(
        g,
        InstallData {
            subcommand: "install",
            version,
            sdk_path: dest_str,
            readiness,
        },
        exit,
        issues,
        text,
    )
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
    // command later; git uses its own trust store, so the fix is on the user's
    // side (`http.sslCAInfo`), which is exactly what the sentence points at.
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
    let readiness = sdk_path.as_deref().map(readiness_for);

    let text = match (&sdk_path, &readiness) {
        (Some(_), Some(report)) => format_readiness_block("Active SDK", report),
        _ => vec![
            "No active SDK configured for this workspace.".to_string(),
            "Run 'tan sdk install <version>' to get started.".to_string(),
        ],
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
    // Which pointer this switch targets — echoed in every `SwitchData` envelope
    // so a JSON consumer can tell a `--global` switch from a project one.
    let scope = if args.global { "global" } else { "project" };
    let Some(version_or_path) = args.arg.clone() else {
        return emit_failure(
            g,
            SwitchData {
                subcommand: "switch",
                sdk_path: String::new(),
                version: None,
                scope,
            },
            ExitCode::RuntimeFailure,
            "missing-version",
            "Version or path argument is required.".to_string(),
            vec![
                "sdk switch: version or path argument is required.".to_string(),
                "Usage: tan sdk switch <version|path>".to_string(),
            ],
        );
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
    let sdk_path = if is_plain_relative(Path::new(&version_or_path)) {
        Path::new(&default_cache_root())
            .join(&version_or_path)
            .to_string_lossy()
            .to_string()
    } else {
        tan_core::normalize_path(&crate::util::cli_workspace_root(g).join(&version_or_path))
            .to_string_lossy()
            .to_string()
    };

    if !Path::new(&sdk_path).exists() {
        return emit_failure(
            g,
            SwitchData {
                subcommand: "switch",
                sdk_path: sdk_path.clone(),
                version: None,
                scope,
            },
            ExitCode::RuntimeFailure,
            "path-not-found",
            format!("SDK path not found: {sdk_path}"),
            vec![
                format!("sdk switch: SDK path does not exist: {sdk_path}"),
                "Run 'tan sdk install <version>' first.".to_string(),
            ],
        );
    }

    // The path can exist without being an SDK checkout (wrong nesting level,
    // an unpacked release archive, a typo'd sibling dir). Pinning it anyway
    // reported success while `resolve_sdk_root` treats a pointer without the
    // loader script as best-effort and silently falls through to
    // auto-discovery — every later command then builds/flashes against a
    // DIFFERENT SDK than the one the user was just told they switched to.
    if !crate::util::has_loader_script(Path::new(&sdk_path)) {
        return emit_failure(
            g,
            SwitchData {
                subcommand: "switch",
                sdk_path: sdk_path.clone(),
                version: None,
                scope,
            },
            ExitCode::RuntimeFailure,
            "not-an-sdk-checkout",
            format!(
                "{sdk_path} does not look like an alp-sdk checkout (missing scripts/alp_project.py)."
            ),
            vec![
                format!("sdk switch: {sdk_path} does not look like an alp-sdk checkout."),
                "Expected scripts/alp_project.py under the given path.".to_string(),
            ],
        );
    }

    let pointer_path = switch_pointer_path(
        &crate::util::cli_workspace_root(g)
            .join(".alp")
            .join("sdk-path"),
        &global_default_pointer_path(),
        args.global,
    );
    if let Err(message) = write_sdk_pointer(&pointer_path, &sdk_path) {
        return emit_failure(
            g,
            SwitchData {
                subcommand: "switch",
                sdk_path: sdk_path.clone(),
                version: None,
                scope,
            },
            ExitCode::RuntimeFailure,
            "switch-failed",
            message.clone(),
            vec![
                "sdk switch: failed to update active SDK pointer.".to_string(),
                message,
            ],
        );
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
    match reconcile_west_manifest_path_for_switch(&sdk_path) {
        Some(SwitchReconcile::Rewrote {
            config_path,
            old_rel,
            new_rel,
        }) => {
            // A reconcile firing at all means this topdir's `.west/` was read
            // successfully and its `path` did NOT already name `sdk_path` --
            // i.e. the workspace under it was bootstrapped for a DIFFERENT SDK.
            // `west update` never ran against the newly selected one's manifest,
            // so the checked-out zephyr/HAL trees still reflect the old SDK; a
            // bare "switched" success here would read as nothing left to do.
            let old_existed = Path::new(&sdk_path)
                .parent()
                .is_some_and(|topdir| topdir.join(&old_rel).exists());
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
            text.push(
                "  next    run `tan bootstrap` to sync the workspace to the new SDK.".to_string(),
            );
            issues.push(Issue {
                code: "sdk.bootstrap-recommended".to_string(),
                severity: "warning".to_string(),
                message: "this topdir's workspace was bootstrapped for a different SDK version; \
                          run `tan bootstrap` to sync it to the newly selected one."
                    .to_string(),
            });
        }
        Some(SwitchReconcile::Blocked {
            config_path,
            old_rel,
        }) => {
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
        None => {}
    }

    emit_success(
        g,
        SwitchData {
            subcommand: "switch",
            sdk_path,
            version: readiness.version,
            scope,
        },
        ExitCode::Success,
        issues,
        text,
    )
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
}
