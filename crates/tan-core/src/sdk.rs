// SPDX-License-Identifier: Apache-2.0
//! SDK release catalogue + local readiness — a port of the IO-free parts of TS
//! `@alp-sdk/core/sdk/service`. Network (GitHub) and filesystem effects stay in
//! the CLI; this module parses the releases payload and inspects a local SDK
//! path through injected predicates.

use serde::Serialize;
use serde_json::Value;

/// GitHub Releases API endpoint for the `alplabai/alp-sdk` repository.
pub const GITHUB_RELEASES_URL: &str = "https://api.github.com/repos/alplabai/alp-sdk/releases";

const LOADER_SCRIPT_RELATIVE: &str = "scripts/alp_project.py";
const VERSION_FILE_RELATIVE: &str = "VERSION";
const METADATA_DIR_RELATIVE: &str = "metadata";

/// A single SDK release entry parsed from the GitHub Releases API.
#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct SdkRelease {
    /// Release tag name (e.g. `v1.5.0`).
    pub tag: String,
    /// ISO-8601 publish timestamp from GitHub.
    pub published_at: String,
    /// URL of the source tarball for this release.
    pub tarball_url: String,
    /// First paragraph of the release body (compact headline).
    pub release_notes_summary: String,
    /// Full release body (Markdown), for an expandable changelog.
    pub release_notes: String,
    /// Whether GitHub has this release marked as a draft (unpublished).
    /// Defaults to `false` when the field is absent or not a boolean —
    /// absent means "not flagged", never a reason to drop the release.
    pub draft: bool,
    /// Whether GitHub has this release marked as a pre-release. Same
    /// missing/non-boolean default as `draft`.
    pub prerelease: bool,
}

/// Append the likely environmental cause to a raw network error.
///
/// rustls reports a middlebox-signed certificate as a bare "tls connection init
/// failed: invalid peer certificate: UnknownIssuer", and ureq reports an
/// unreachable proxy as a bare connect error. Both read to a user as "the
/// network is down", so they go hunting in the wrong place — the actual answer
/// is almost always "your corporate CA is not in this machine's trust store" or
/// "your proxy env vars are wrong". alp-sdk ADR 0021 makes this explicit for the
/// download path: a failure behind a TLS-intercepting middlebox must say
/// *proxy/CA interference*, never blame the payload. One sentence appended to
/// the error we already surface — deliberately not a diagnostic-code scheme.
///
/// `proxy_configured` is the one bit the error string cannot carry: an
/// unreachable, refused or firewalled proxy fails as a plain `ConnectionFailed`
/// that never contains the word "proxy" (ureq only says it for the CONNECT
/// stage), and that is the single most common misconfiguration. The caller reads
/// the environment (IO) and tells us; `tan-cli`'s `http::proxy_configured` is it.
pub fn describe_network_error(error: &str, proxy_configured: bool) -> String {
    let lower = error.to_ascii_lowercase();
    // Names only the variables that actually steer these requests. Both paths
    // that reach here are `https://` — the in-process API GET and `sdk install`'s
    // `git clone` — and neither `tan` nor git applies `HTTP_PROXY` to an https
    // URL, so naming it sent the user to edit a variable that changes nothing.
    // `NO_PROXY` is named instead because it is the knob that *fixes* this exact
    // failure when the host is reachable directly.
    let proxy_hint = "Check ALL_PROXY/HTTPS_PROXY/NO_PROXY — the configured proxy refused or could not complete the connection.";
    // Named-proxy first: a proxy CONNECT/auth failure that also mentions TLS is
    // still a proxy problem to look at first, so it must beat the certificate
    // arm. A certificate/TLS failure comes next even when a proxy IS set — the
    // middlebox is reachable, it is its CA that is untrusted, and pointing at
    // the proxy env vars there would send the user to the wrong knob. Only then
    // do we treat a bare connect failure as the proxy's fault, and only when
    // there is a proxy to blame.
    let hint = if lower.contains("proxy") {
        Some(proxy_hint)
    } else if lower.contains("certificate") || lower.contains("tls") {
        Some(
            "This is usually a TLS-intercepting proxy or a corporate CA that this machine does not trust, not a broken connection.",
        )
    } else if proxy_configured && lower.contains("connect") {
        Some(proxy_hint)
    } else {
        None
    };
    match hint {
        Some(hint) => format!("{error} {hint}"),
        None => error.to_string(),
    }
}

/// Parse the GitHub Releases API payload into typed releases (mirror of
/// `listRemoteSdkReleases`'s mapping). Errors on a non-array response.
pub fn parse_remote_sdk_releases(raw: &Value) -> Result<Vec<SdkRelease>, String> {
    let Some(items) = raw.as_array() else {
        return Err("Alp SDK: unexpected response shape from GitHub Releases API.".to_string());
    };

    Ok(items
        .iter()
        .filter(|item| item.get("tag_name").and_then(Value::as_str).is_some())
        .map(|item| {
            let str_field = |key: &str| {
                item.get(key)
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .to_string()
            };
            let body = item.get("body").and_then(Value::as_str).unwrap_or("");
            let bool_field = |key: &str| item.get(key).and_then(Value::as_bool).unwrap_or(false);
            SdkRelease {
                tag: str_field("tag_name"),
                published_at: str_field("published_at"),
                tarball_url: str_field("tarball_url"),
                release_notes_summary: extract_first_paragraph(body),
                release_notes: body.trim().to_string(),
                draft: bool_field("draft"),
                prerelease: bool_field("prerelease"),
            }
        })
        .collect())
}

fn extract_first_paragraph(body: &str) -> String {
    let trimmed = body.trim();
    if trimmed.is_empty() {
        return String::new();
    }
    match trimmed.find("\n\n") {
        Some(idx) => trimmed[..idx].trim().to_string(),
        None => trimmed.to_string(),
    }
}

/// Overall readiness of a local SDK path.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum SdkReadinessState {
    /// Loader script present and no issues found.
    Ready,
    /// Loader script present but some non-fatal issues exist.
    Partial,
    /// Loader script absent — not a valid SDK root.
    Missing,
}

/// Result of inspecting a local SDK path for readiness.
#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct SdkReadinessReport {
    /// The inspected SDK root path.
    pub sdk_path: String,
    /// Trimmed contents of the `VERSION` file, if present and non-empty.
    pub version: Option<String>,
    /// Whether `scripts/alp_project.py` exists under the SDK root.
    pub loader_script_present: bool,
    /// Whether the `metadata/` directory exists under the SDK root.
    pub metadata_present: bool,
    /// Computed readiness verdict.
    pub state: SdkReadinessState,
    /// Human-readable descriptions of any problems found.
    pub issues: Vec<String>,
}

/// Inspect a local SDK path (mirror of `checkSdkReadiness`). `path_exists` and
/// `read_file` are injected so this stays pure/testable.
pub fn check_sdk_readiness(
    sdk_path: &str,
    path_exists: impl Fn(&str) -> bool,
    read_file: impl Fn(&str) -> Option<String>,
) -> SdkReadinessReport {
    let mut issues = Vec::new();
    let join = |rel: &str| join_path(sdk_path, rel);

    let loader_script_present = path_exists(&join(LOADER_SCRIPT_RELATIVE));
    if !loader_script_present {
        issues.push(format!(
            "scripts/alp_project.py not found — \"{sdk_path}\" is not a valid ALP SDK root."
        ));
    }

    let metadata_present = path_exists(&join(METADATA_DIR_RELATIVE));
    if !metadata_present {
        issues.push("metadata/ directory is missing.".to_string());
    }

    let mut version: Option<String> = None;
    let version_file = join(VERSION_FILE_RELATIVE);
    if path_exists(&version_file) {
        match read_file(&version_file) {
            Some(contents) => {
                let trimmed = contents.trim();
                version = if trimmed.is_empty() {
                    None
                } else {
                    Some(trimmed.to_string())
                };
            }
            None => issues.push("VERSION file could not be read.".to_string()),
        }
    }

    let state = if !loader_script_present {
        SdkReadinessState::Missing
    } else if !issues.is_empty() {
        SdkReadinessState::Partial
    } else {
        SdkReadinessState::Ready
    };

    SdkReadinessReport {
        sdk_path: sdk_path.to_string(),
        version,
        loader_script_present,
        metadata_present,
        state,
        issues,
    }
}

/// Read the active-SDK pointer (`.alp/sdk-path`) under `workspace_root`, if any
/// (mirror of `resolveActiveSdk`).
pub fn resolve_active_sdk(
    workspace_root: &str,
    path_exists: impl Fn(&str) -> bool,
    read_file: impl Fn(&str) -> Option<String>,
) -> Option<String> {
    let pointer = join_path(workspace_root, ".alp/sdk-path");
    if !path_exists(&pointer) {
        return None;
    }
    let raw = read_file(&pointer)?;
    let parsed: Value = serde_json::from_str(&raw).ok()?;
    parsed
        .get("sdkPath")
        .and_then(Value::as_str)
        .map(str::to_string)
}

/// Read the machine-global default-SDK pointer (`<home_alp_dir>/sdk-default`),
/// if any — same `{sdkPath}` JSON shape as [`resolve_active_sdk`]'s project
/// pointer, written by `tan sdk switch --global`. `path_exists`/`read_file` are
/// injected so this stays pure/testable.
pub fn resolve_global_default_sdk(
    home_alp_dir: &str,
    path_exists: impl Fn(&str) -> bool,
    read_file: impl Fn(&str) -> Option<String>,
) -> Option<String> {
    let pointer = join_path(home_alp_dir, "sdk-default");
    if !path_exists(&pointer) {
        return None;
    }
    let raw = read_file(&pointer)?;
    let parsed: Value = serde_json::from_str(&raw).ok()?;
    parsed
        .get("sdkPath")
        .and_then(Value::as_str)
        .map(str::to_string)
}

/// Resolve a BARE version argument (`tan sdk switch v0.13.0`) to an on-disk
/// path by trying each candidate cache root in order and taking the first that
/// actually holds an SDK checkout for that version.
///
/// One root — `~/.alp/sdk-cache`, tan's own install target — used to be the
/// only one consulted, which is why #62's fix could never fire for the layout
/// that reported it: those SDKs live under `~/.alp/sdk` (the VS Code
/// extension's install root), so `tan sdk switch v0.13.0` resolved
/// `~/.alp/sdk-cache/v0.13.0`, found nothing, and failed with `path-not-found`
/// before any reconciliation was reachable. Nothing on this machine DECLARES a
/// cache root; the authoritative record of where its SDKs actually live is the
/// active-SDK pointer's own parent directory, which the caller passes as a
/// lower-precedence root.
///
/// Deterministic by construction: roots are tried in the given order and the
/// FIRST that holds a real checkout wins, so the same command on the same disk
/// always resolves the same path. `is_sdk` (not mere existence) is the test, so
/// a leftover empty directory in an earlier root cannot shadow a real checkout
/// in a later one. When no root holds it, `roots[0]` is returned so the
/// `path-not-found` message names the canonical location rather than the last
/// long-shot guess.
pub fn resolve_sdk_version_root(
    version: &str,
    roots: &[String],
    is_sdk: impl Fn(&str) -> bool,
) -> String {
    let Some(first) = roots.first() else {
        return version.to_string();
    };
    roots
        .iter()
        .map(|root| join_path(root, version))
        .find(|candidate| is_sdk(candidate))
        .unwrap_or_else(|| join_path(first, version))
}

/// Which precedence tier produced the active SDK path — `tan sdk current
/// --json`'s `sourceTier`, and the precedence `tan init` pins the new project
/// against.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "camelCase")]
pub enum SdkSourceTier {
    /// `--sdk-root` — terminal and returned as-is even when invalid, so the
    /// caller's own loader-script check fails loudly on a bad path instead of
    /// silently falling through to a lower tier.
    SdkRootFlag,
    /// The workspace pin (`<workspace_root>/.alp/sdk-path`, `tan sdk switch`).
    ProjectPin,
    /// The machine-global pin (`~/.alp/sdk-default`, `tan sdk switch --global`).
    GlobalDefault,
    /// Sibling/workspace auto-discovery — no explicit flag or pin at all.
    Discovery,
    /// Nothing resolved at any tier.
    None,
}

/// Resolve the SDK path across the full four-tier precedence chain, given each
/// tier's already-computed lookup result (`None` when that tier has nothing to
/// offer — e.g. no pointer file, or a pointer whose target lacks the loader
/// script). Precedence: `sdk_root_flag` > `project_pin` > `global_default` >
/// `discovery`. Pure — every input is a plain value, no filesystem access.
pub fn resolve_sdk_source_tier(
    sdk_root_flag: Option<&str>,
    project_pin: Option<&str>,
    global_default: Option<&str>,
    discovery: Option<&str>,
) -> (Option<String>, SdkSourceTier) {
    if let Some(path) = sdk_root_flag {
        return (Some(path.to_string()), SdkSourceTier::SdkRootFlag);
    }
    if let Some(path) = project_pin {
        return (Some(path.to_string()), SdkSourceTier::ProjectPin);
    }
    if let Some(path) = global_default {
        return (Some(path.to_string()), SdkSourceTier::GlobalDefault);
    }
    if let Some(path) = discovery {
        return (Some(path.to_string()), SdkSourceTier::Discovery);
    }
    (None, SdkSourceTier::None)
}

/// Join a path with a relative segment, normalizing the separator to the host.
fn join_path(base: &str, relative: &str) -> String {
    let mut path = std::path::PathBuf::from(base);
    for part in relative.split('/') {
        path.push(part);
    }
    path.to_string_lossy().to_string()
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn parses_releases_filtering_untagged() {
        let raw = json!([
            {"tag_name": "v1.5.0", "published_at": "2024-01-02T00:00:00Z", "tarball_url": "u", "body": "First line.\n\nrest"},
            {"published_at": "x"},
        ]);
        let releases = parse_remote_sdk_releases(&raw).unwrap();
        assert_eq!(releases.len(), 1);
        assert_eq!(releases[0].tag, "v1.5.0");
        assert_eq!(releases[0].release_notes_summary, "First line.");
        assert_eq!(releases[0].release_notes, "First line.\n\nrest");
        // #122: absent draft/prerelease keys must default to false, never drop
        // the release or panic.
        assert!(!releases[0].draft);
        assert!(!releases[0].prerelease);
    }

    #[test]
    fn parses_draft_and_prerelease_flags() {
        // #122: GitHub's `draft`/`prerelease` booleans must survive the parse
        // so a consumer can tell a release candidate or an unpublished draft
        // apart from a real latest release, instead of tan silently dropping
        // the fact.
        let raw = json!([
            {"tag_name": "v2.0.0-rc1", "published_at": "t", "tarball_url": "u", "body": "b",
             "draft": true, "prerelease": true},
        ]);
        let releases = parse_remote_sdk_releases(&raw).unwrap();
        assert_eq!(releases.len(), 1);
        assert!(releases[0].draft);
        assert!(releases[0].prerelease);
    }

    #[test]
    fn draft_and_prerelease_default_false_when_absent_or_non_boolean() {
        let raw = json!([
            {"tag_name": "v1.0.0", "published_at": "t", "tarball_url": "u", "body": "b"},
            {"tag_name": "v1.0.1", "published_at": "t", "tarball_url": "u", "body": "b",
             "draft": "yes", "prerelease": 1},
        ]);
        let releases = parse_remote_sdk_releases(&raw).unwrap();
        // Neither a missing key nor a non-boolean value drops the release.
        assert_eq!(releases.len(), 2);
        assert!(!releases[0].draft);
        assert!(!releases[0].prerelease);
        assert!(!releases[1].draft);
        assert!(!releases[1].prerelease);
    }

    #[test]
    fn sdk_release_json_keys_are_exactly_the_seven_fields() {
        // The membership test #122 asks for: pins the emitted per-release key
        // set so a later silent rename or drop (the #106 failure class) is
        // caught here, since no golden covers `sdk list` at all.
        let release = SdkRelease {
            tag: "v1.5.0".to_string(),
            published_at: "2024-01-02T00:00:00Z".to_string(),
            tarball_url: "u".to_string(),
            release_notes_summary: "s".to_string(),
            release_notes: "s".to_string(),
            draft: false,
            prerelease: true,
        };
        let value = serde_json::to_value(&release).unwrap();
        let keys: std::collections::BTreeSet<&str> = value
            .as_object()
            .unwrap()
            .keys()
            .map(String::as_str)
            .collect();
        assert_eq!(
            keys,
            std::collections::BTreeSet::from([
                "tag",
                "publishedAt",
                "tarballUrl",
                "releaseNotesSummary",
                "releaseNotes",
                "draft",
                "prerelease",
            ])
        );
    }

    #[test]
    fn network_error_names_the_likely_cause() {
        // The real rustls string a TLS-intercepting middlebox produces. Still the
        // CA answer WITH a proxy configured: the proxy answered, its CA is the
        // problem.
        for proxy_set in [false, true] {
            let tls = describe_network_error(
                "https://api.github.com/…: tls connection init failed: invalid peer certificate: UnknownIssuer",
                proxy_set,
            );
            assert!(tls.contains("corporate CA"), "{tls}");
        }
        // Proxy wins over the certificate arm — a proxy CONNECT failure that also
        // mentions TLS is still a proxy problem to go look at first.
        let proxy = describe_network_error("proxy: tls connection failed", false);
        assert!(proxy.contains("HTTPS_PROXY"), "{proxy}");
        assert!(!proxy.contains("corporate CA"), "{proxy}");
        // The most common misconfiguration of all: a proxy that is set but
        // unreachable/refused. ureq raises that as a plain ConnectionFailed with
        // no mention of a proxy anywhere — verbatim from `HTTPS_PROXY=http://127.0.0.1:9`.
        let refused = "https://api.github.com/repos/alplabai/alp-sdk/releases: Connection Failed: Connect error: No connection could be made because the target machine actively refused it. (os error 10061)";
        assert!(
            describe_network_error(refused, true).contains("HTTPS_PROXY"),
            "a refused proxy must be named"
        );
        // …and the identical string is left alone with no proxy configured: then
        // it really is just an unreachable host.
        assert_eq!(describe_network_error(refused, false), refused);
        // Anything else is passed through untouched — no invented diagnosis.
        assert_eq!(
            describe_network_error("dns: failed to lookup address", true),
            "dns: failed to lookup address"
        );
    }

    #[test]
    fn non_array_response_errors() {
        assert!(parse_remote_sdk_releases(&json!({"message": "rate limited"})).is_err());
    }

    #[test]
    fn readiness_missing_when_no_loader() {
        let report = check_sdk_readiness("/sdk", |_| false, |_| None);
        assert_eq!(report.state, SdkReadinessState::Missing);
        assert!(!report.loader_script_present);
    }

    #[test]
    fn readiness_ready_when_complete() {
        let report = check_sdk_readiness(
            "/sdk",
            |p| p.ends_with("alp_project.py") || p.ends_with("metadata") || p.ends_with("VERSION"),
            |_| Some("v1.5.0\n".to_string()),
        );
        assert_eq!(report.state, SdkReadinessState::Ready);
        assert_eq!(report.version.as_deref(), Some("v1.5.0"));
        assert!(report.issues.is_empty());
    }

    #[test]
    fn active_sdk_pointer_parsed() {
        let resolved = resolve_active_sdk(
            "/ws",
            |p| p.ends_with("sdk-path"),
            |_| Some("{\"sdkPath\": \"/cache/v1\"}".to_string()),
        );
        assert_eq!(resolved.as_deref(), Some("/cache/v1"));
    }

    #[test]
    fn global_default_pointer_parsed() {
        let resolved = resolve_global_default_sdk(
            "/home/.alp",
            |p| p.ends_with("sdk-default"),
            |_| Some("{\"sdkPath\": \"/cache/global\"}".to_string()),
        );
        assert_eq!(resolved.as_deref(), Some("/cache/global"));
    }

    #[test]
    fn global_default_absent_when_no_pointer_file() {
        let resolved = resolve_global_default_sdk("/home/.alp", |_| false, |_| None);
        assert_eq!(resolved, None);
    }

    #[test]
    fn source_tier_sdk_root_flag_wins_over_everything() {
        let (path, tier) = resolve_sdk_source_tier(
            Some("/explicit"),
            Some("/pin"),
            Some("/global"),
            Some("/discovered"),
        );
        assert_eq!(path.as_deref(), Some("/explicit"));
        assert_eq!(tier, SdkSourceTier::SdkRootFlag);
    }

    #[test]
    fn source_tier_project_pin_wins_over_global_and_discovery() {
        let (path, tier) =
            resolve_sdk_source_tier(None, Some("/pin"), Some("/global"), Some("/discovered"));
        assert_eq!(path.as_deref(), Some("/pin"));
        assert_eq!(tier, SdkSourceTier::ProjectPin);
    }

    #[test]
    fn source_tier_global_default_wins_over_discovery() {
        let (path, tier) =
            resolve_sdk_source_tier(None, None, Some("/global"), Some("/discovered"));
        assert_eq!(path.as_deref(), Some("/global"));
        assert_eq!(tier, SdkSourceTier::GlobalDefault);
    }

    #[test]
    fn source_tier_falls_through_to_discovery() {
        let (path, tier) = resolve_sdk_source_tier(None, None, None, Some("/discovered"));
        assert_eq!(path.as_deref(), Some("/discovered"));
        assert_eq!(tier, SdkSourceTier::Discovery);
    }

    #[test]
    fn source_tier_none_when_nothing_resolves() {
        let (path, tier) = resolve_sdk_source_tier(None, None, None, None);
        assert_eq!(path, None);
        assert_eq!(tier, SdkSourceTier::None);
    }

    /// Both roots of the layout that reported #62: tan's own install cache and
    /// the extension's install root, where the SDKs actually were.
    fn roots() -> Vec<String> {
        vec![
            "/home/u/.alp/sdk-cache".to_string(),
            "/home/u/.alp/sdk".to_string(),
        ]
    }

    #[test]
    fn version_root_falls_through_to_the_root_that_actually_holds_the_version() {
        // #62's layout: `~/.alp/sdk-cache` is empty, the SDKs live in
        // `~/.alp/sdk`. Resolving against the first root ONLY is what put the
        // bare-version form out of reach of the whole reconciliation.
        let real = join_path("/home/u/.alp/sdk", "v0.13.0");
        let got = resolve_sdk_version_root("v0.13.0", &roots(), |candidate| candidate == real);
        assert_eq!(got, real);
    }

    #[test]
    fn version_root_prefers_the_first_root_holding_the_version() {
        // Both roots hold it -> the earlier one wins, deterministically:
        // `tan sdk install v0.13.0 && tan sdk switch v0.13.0` must select the
        // checkout the install just wrote.
        let got = resolve_sdk_version_root("v0.13.0", &roots(), |_| true);
        assert_eq!(got, join_path("/home/u/.alp/sdk-cache", "v0.13.0"));
    }

    #[test]
    fn version_root_falls_back_to_the_canonical_root_when_nothing_holds_it() {
        // Nothing found anywhere -> the `path-not-found` message names the
        // canonical install location, not the last root tried.
        let got = resolve_sdk_version_root("v9.9.9", &roots(), |_| false);
        assert_eq!(got, join_path("/home/u/.alp/sdk-cache", "v9.9.9"));
    }
}
