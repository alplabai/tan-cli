// SPDX-License-Identifier: Apache-2.0
//! Pure build-plan *execution* decisions (ADR-0020) — the consumer-side logic
//! that turns the plan's `env_append_path` and `execution_policy` fields into
//! concrete env values and skip/fail dispositions. Kept here (not in the CLI's
//! `execute_slices`) because it is pure — no IO, no process spawning — so it is
//! unit-tested in `tan-core` where the plan types already live. The CLI's
//! executor just calls these; it owns the IO (spawning, path resolution).

use std::collections::BTreeMap;

use crate::build_plan::{ExecutionPolicy, PolicyAction};

/// The OS path-list separator (`;` on Windows, `:` elsewhere) — matches Python's
/// `os.pathsep`, which the SDK appenders (`_workspace.subprocess_env` /
/// `_alp_common.env_with_sdk`) use for a real OS path-list var (`PYTHONPATH`).
fn path_list_sep() -> char {
    if cfg!(windows) { ';' } else { ':' }
}

/// The join separator for one `envAppendPath` var — PER-KEY, not uniformly
/// `os.pathsep`: `EXTRA_ZEPHYR_MODULES` is a CMake list Zephyr's
/// `zephyr_module.py` splits on `;` on EVERY platform (never an OS path
/// list) — joining it with `:` on Linux/WSL fails `west build` configure
/// with "is not a valid zephyr module". Every other var is a real OS path
/// list, joined with `path_list_sep()` (matches the SDK appenders'
/// `os.pathsep`).
fn sep_for_key(var: &str) -> char {
    if var == "EXTRA_ZEPHYR_MODULES" {
        ';'
    } else {
        path_list_sep()
    }
}

/// Append each value to its env var using that var's join separator
/// (`sep_for_key`), skipping a value already present segment-wise (matches the
/// alp-sdk appenders). Operates on the materialised `(key, value)` env list: a
/// var absent from `base` is seeded from the appended values (the plan owns
/// it); a var present has the new segments appended after de-dup. Pure — the
/// caller seeds `base` (slice env + inherited process env) so appending
/// preserves rather than replaces an inherited value like `PYTHONPATH`.
pub fn apply_env_append(base: &mut Vec<(String, String)>, append: &BTreeMap<String, Vec<String>>) {
    for (var, values) in append {
        let sep = sep_for_key(var);
        let current = base.iter().find(|(k, _)| k == var).map(|(_, v)| v.clone());
        let mut segments: Vec<String> = match current.as_deref() {
            Some(v) if !v.is_empty() => v.split(sep).map(str::to_string).collect(),
            _ => Vec::new(),
        };
        for val in values {
            if !segments.iter().any(|s| s == val) {
                segments.push(val.clone());
            }
        }
        let joined = segments.join(&sep.to_string());
        if let Some(entry) = base.iter_mut().find(|(k, _)| k == var) {
            entry.1 = joined;
        } else {
            base.push((var.clone(), joined));
        }
    }
}

/// Assemble the final `(key, value)` env for a slice subprocess (pure): start
/// from the slice's verbatim `env`, seed any `env_append_path` var the slice
/// doesn't pin from `inherited` (so an appended value extends rather than
/// replaces e.g. an inherited `PYTHONPATH`), apply `env_append_path`
/// (append + de-dup), then merge the CLI's consumer-mechanism `gap_fillers`
/// (e.g. `ZEPHYR_BASE`) — which are pre-gated so "plan wins / CLI fills gaps".
/// IO (reading the process env, resolving paths) is the caller's; it hands the
/// results in via `inherited` and `gap_fillers`, keeping this testable.
pub fn assemble_slice_env(
    slice_env: &BTreeMap<String, String>,
    env_append_path: &BTreeMap<String, Vec<String>>,
    inherited: impl Fn(&str) -> Option<String>,
    gap_fillers: &[(&str, String)],
) -> Vec<(String, String)> {
    let mut env: Vec<(String, String)> = slice_env
        .iter()
        .map(|(k, v)| (k.clone(), v.clone()))
        .collect();
    for key in env_append_path.keys() {
        if !env.iter().any(|(k, _)| k == key) {
            if let Some(v) = inherited(key) {
                env.push((key.clone(), v));
            }
        }
    }
    apply_env_append(&mut env, env_append_path);
    for (key, value) in gap_fillers {
        if let Some(e) = env.iter_mut().find(|(k, _)| k == *key) {
            e.1 = value.clone();
        } else {
            env.push((key.to_string(), value.clone()));
        }
    }
    env
}

/// Resolve the executor's disposition for a degenerate-slice condition (missing
/// tool / null command / unknown backend): honor `execution_policy`'s entry
/// (`pick`) when present, else fall back to `default` — the CLI's built-in
/// behavior for an older plan that omits the policy.
pub fn resolve_action(
    policy: Option<&ExecutionPolicy>,
    pick: fn(&ExecutionPolicy) -> Option<PolicyAction>,
    default: PolicyAction,
) -> PolicyAction {
    policy.and_then(pick).unwrap_or(default)
}

/// What the executor should do with a slice's build dir before dispatching it
/// (issue #52): keep it, or wipe it (`Pristine`) because it was configured
/// against a different SDK root than the one this run resolved. West itself
/// refuses to reconfigure a build dir whose CMake cache is bound to a
/// different source tree (`FATAL ERROR: refusing to proceed without --force`)
/// — a real failure that reads to the user as a bare "terminated with exit
/// code: 1" with the actual cause buried in scrollback.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SdkStampAction {
    /// Leave the build dir alone — either it matches the current SDK root, or
    /// one of the guards below means this run cannot trust the check.
    Keep,
    /// Wipe the build dir before the tool runs; it was configured against a
    /// different (or unrecorded) SDK root.
    Pristine,
}

/// Decide [`SdkStampAction`] for one slice (pure — the caller resolves every
/// input from disk/argv). `cached` is the `<cwd>/build/.tan-sdk-root` stamp
/// tan itself wrote on a previous dispatch (`None` = absent, either a
/// pre-feature build dir or one that never got past `create_dir_all`);
/// `current` is this run's resolved `--sdk-root` (`None` only when it could
/// not be resolved at all — never wipe on that, there is nothing to compare
/// against); `cache_configured` is whether `<cwd>/build/CMakeCache.txt`
/// exists (a dir west has never configured has nothing to go stale).
///
/// The two bool guards mirror `resolve_zephyr_artefact`'s own refusal to trust
/// a build dir it cannot resolve: `build_dir_overridden` — a `-d`/
/// `--build-dir` in the slice's argv means west wrote somewhere this function
/// cannot know, so it must not touch `<cwd>/build` at all; `cwd_under_build_root`
/// — confines the wipe target to under `CONSUMER_BUILD_ROOT` (`build`), so a
/// plan cwd of `src/` (still `is_plain_relative`, but not under `build/`)
/// cannot put the wipe target at `<project>/src/build`, which may hold files
/// the build never created.
///
/// A missing stamp on an already-configured dir (`cached: None`,
/// `cache_configured: true`) reads as stale, deliberately: it is the only way
/// a build dir that predates this feature (like the one issue #52 reported)
/// ever self-heals, since a dir that fails to build never earns a stamp.
pub fn sdk_stamp_action(
    cached: Option<&str>,
    current: Option<&str>,
    cache_configured: bool,
    build_dir_overridden: bool,
    cwd_under_build_root: bool,
) -> SdkStampAction {
    if build_dir_overridden || !cwd_under_build_root || !cache_configured {
        return SdkStampAction::Keep;
    }
    match current {
        None => SdkStampAction::Keep,
        Some(current) if cached == Some(current) => SdkStampAction::Keep,
        Some(_) => SdkStampAction::Pristine,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn append_map(pairs: &[(&str, &[&str])]) -> BTreeMap<String, Vec<String>> {
        pairs
            .iter()
            .map(|(k, vs)| (k.to_string(), vs.iter().map(|s| s.to_string()).collect()))
            .collect()
    }

    #[test]
    fn apply_env_append_appends_and_dedups() {
        // A value already present segment-wise is not doubled; a new value is
        // appended with the OS path-list separator.
        let sep = path_list_sep();
        let mut base = vec![("PYTHONPATH".to_string(), format!("/a{sep}/b"))];
        apply_env_append(&mut base, &append_map(&[("PYTHONPATH", &["/b", "/c"])]));
        assert_eq!(
            base,
            vec![("PYTHONPATH".to_string(), format!("/a{sep}/b{sep}/c"))]
        );
    }

    #[test]
    fn apply_env_append_extra_zephyr_modules_always_uses_semicolon() {
        // EXTRA_ZEPHYR_MODULES is a CMake list -- `;`-joined on EVERY
        // platform, never `path_list_sep()` (which is `:` on Linux/WSL and
        // would produce a value Zephyr's zephyr_module.py can't split).
        let mut base = vec![("EXTRA_ZEPHYR_MODULES".to_string(), "/a".to_string())];
        apply_env_append(&mut base, &append_map(&[("EXTRA_ZEPHYR_MODULES", &["/b"])]));
        assert_eq!(
            base,
            vec![("EXTRA_ZEPHYR_MODULES".to_string(), "/a;/b".to_string())]
        );
    }

    #[test]
    fn apply_env_append_seeds_absent_var() {
        // A var absent from base is seeded from the appended values (plan owns it).
        let mut base: Vec<(String, String)> = Vec::new();
        apply_env_append(
            &mut base,
            &append_map(&[("EXTRA_ZEPHYR_MODULES", &["/sdk"])]),
        );
        assert_eq!(
            base,
            vec![("EXTRA_ZEPHYR_MODULES".to_string(), "/sdk".to_string())]
        );
    }

    #[test]
    fn assemble_slice_env_appends_dedups_seeds_and_fills_gaps() {
        let sep = path_list_sep();
        let slice_env: BTreeMap<String, String> =
            [("ALP_SDK_ROOT".to_string(), "/sdk".to_string())]
                .into_iter()
                .collect();
        // Plan appends /sdk/scripts to PYTHONPATH (not pinned in slice env) and
        // seeds EXTRA_ZEPHYR_MODULES (absent from both slice env and inherited).
        let append: BTreeMap<String, Vec<String>> = [
            ("PYTHONPATH".to_string(), vec!["/sdk/scripts".to_string()]),
            (
                "EXTRA_ZEPHYR_MODULES".to_string(),
                vec!["/plan/sdk".to_string()],
            ),
        ]
        .into_iter()
        .collect();
        // Inherited PYTHONPATH must be preserved (extended, not replaced).
        let inherited = |k: &str| (k == "PYTHONPATH").then(|| "/inh".to_string());
        // Gap-filler: ZEPHYR_BASE (plan never carries it) is filled; the
        // gap-filler for a var the plan already set would overwrite — here only
        // ZEPHYR_BASE is passed (EXTRA_ZEPHYR_MODULES was gated out upstream).
        let gaps = [("ZEPHYR_BASE", "/ws/zephyr".to_string())];
        let env = assemble_slice_env(&slice_env, &append, inherited, &gaps);

        let get = |k: &str| env.iter().find(|(kk, _)| kk == k).map(|(_, v)| v.clone());
        assert_eq!(get("ALP_SDK_ROOT"), Some("/sdk".to_string()));
        assert_eq!(get("PYTHONPATH"), Some(format!("/inh{sep}/sdk/scripts")));
        assert_eq!(get("EXTRA_ZEPHYR_MODULES"), Some("/plan/sdk".to_string()));
        assert_eq!(get("ZEPHYR_BASE"), Some("/ws/zephyr".to_string()));
    }

    #[test]
    fn resolve_action_honors_policy_with_none_fallback() {
        let policy = ExecutionPolicy {
            unknown_backend: None,
            missing_tool: Some(PolicyAction::Fail),
            null_command: Some(PolicyAction::Skip),
        };
        // Present entry wins over the default.
        assert_eq!(
            resolve_action(Some(&policy), |p| p.missing_tool, PolicyAction::Skip),
            PolicyAction::Fail
        );
        assert_eq!(
            resolve_action(Some(&policy), |p| p.null_command, PolicyAction::Fail),
            PolicyAction::Skip
        );
        // Absent entry -> default.
        assert_eq!(
            resolve_action(Some(&policy), |p| p.unknown_backend, PolicyAction::Skip),
            PolicyAction::Skip
        );
        // None policy (older plan) -> default.
        assert_eq!(
            resolve_action(None, |p| p.missing_tool, PolicyAction::Skip),
            PolicyAction::Skip
        );
    }

    #[test]
    fn sdk_stamp_action_keeps_a_matching_stamp() {
        assert_eq!(
            sdk_stamp_action(
                Some("/sdk/v0.13.0"),
                Some("/sdk/v0.13.0"),
                true,
                false,
                true
            ),
            SdkStampAction::Keep
        );
    }

    #[test]
    fn sdk_stamp_action_pristines_a_mismatched_stamp() {
        // The reported case: v0.11.0 -> v0.13.0.
        assert_eq!(
            sdk_stamp_action(
                Some("/sdk/v0.11.0"),
                Some("/sdk/v0.13.0"),
                true,
                false,
                true
            ),
            SdkStampAction::Pristine
        );
    }

    #[test]
    fn sdk_stamp_action_pristines_a_missing_stamp_on_an_already_configured_dir() {
        // Deliberate: the ONLY way a pre-feature build dir (never stamped, but
        // already has a CMakeCache.txt) ever self-heals.
        assert_eq!(
            sdk_stamp_action(None, Some("/sdk/v0.13.0"), true, false, true),
            SdkStampAction::Pristine
        );
    }

    #[test]
    fn sdk_stamp_action_keeps_when_no_cache_configured_yet() {
        // Nothing to go stale — a first-ever configure, not a switch.
        assert_eq!(
            sdk_stamp_action(None, Some("/sdk/v0.13.0"), false, false, true),
            SdkStampAction::Keep
        );
    }

    #[test]
    fn sdk_stamp_action_keeps_when_build_dir_is_overridden() {
        // West wrote somewhere this function cannot know (-d/--build-dir) —
        // mirrors resolve_zephyr_artefact's own refusal to trust that case.
        assert_eq!(
            sdk_stamp_action(Some("/sdk/v0.11.0"), Some("/sdk/v0.13.0"), true, true, true),
            SdkStampAction::Keep
        );
    }

    #[test]
    fn sdk_stamp_action_keeps_when_cwd_is_outside_the_build_root() {
        // A plan cwd of "src/" would put the wipe target at
        // <project>/src/build, which may hold files the build never created.
        assert_eq!(
            sdk_stamp_action(
                Some("/sdk/v0.11.0"),
                Some("/sdk/v0.13.0"),
                true,
                false,
                false
            ),
            SdkStampAction::Keep
        );
    }

    #[test]
    fn sdk_stamp_action_never_wipes_on_an_unresolved_current_sdk_root() {
        // current: None means this run could not resolve --sdk-root at all —
        // there is nothing to compare the stamp against.
        assert_eq!(
            sdk_stamp_action(Some("/sdk/v0.11.0"), None, true, false, true),
            SdkStampAction::Keep
        );
    }
}
