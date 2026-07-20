// SPDX-License-Identifier: Apache-2.0
//! Consumer-mechanism env gap-filler for the `--native` executor: the
//! `ZEPHYR_BASE` / `EXTRA_ZEPHYR_MODULES` keys the plan deliberately does NOT
//! carry, filled in only when the plan didn't pin them ("plan wins / CLI fills
//! gaps").

use std::path::Path;

/// Consumer-mechanism env the plan deliberately does NOT carry, filled in as a
/// gap-filler so `tan build` runs a plan slice with no manual setup:
///   * `ZEPHYR_BASE` — the resolved workspace's zephyr. Per ADR-0020 the plan
///     never emits this; it is pure consumer mechanism, always hand-derived.
///   * `EXTRA_ZEPHYR_MODULES` — the alp-sdk checkout, so `west build -b
///     <alp-board>` finds the SDK's boards. This now comes FROM the plan's
///     `env_append_path`; the hand-derived value is only a FALLBACK for an older
///     plan that carries neither the slice-env pin nor the `env_append_path`
///     entry (plan wins / CLI fills gaps).
///
/// Never overrides a key the plan's slice env pins.
///
/// `inherited` is the parent-process env lookup (same shape as the one
/// `assemble_slice_env` already threads through for `env_append_path`
/// seeding). The EXTRA_ZEPHYR_MODULES gap-filler used to return the bare SDK
/// root, and `assemble_slice_env`'s gap-filler merge OVERWRITES rather than
/// appends — so a developer's own `export EXTRA_ZEPHYR_MODULES=/my/module`
/// was silently replaced by just the SDK root on any pre-ADR-0020 plan. Seed
/// from `inherited` and append the SDK root via the same `apply_env_append`
/// (path-list-separator, de-dup) the plan-driven path uses, so the two paths
/// no longer disagree on whether an inherited value survives.
pub(super) fn zephyr_env_overrides(
    zephyr_base: Option<&Path>,
    sdk_root: Option<&Path>,
    slice_env: &std::collections::BTreeMap<String, String>,
    env_append_path: &std::collections::BTreeMap<String, Vec<String>>,
    inherited: impl Fn(&str) -> Option<String>,
) -> Vec<(&'static str, String)> {
    let mut out = Vec::new();
    if !slice_env.contains_key("ZEPHYR_BASE") {
        if let Some(base) = zephyr_base {
            out.push(("ZEPHYR_BASE", base.to_string_lossy().into_owned()));
        }
    }
    // Plan wins: skip the hand-derived value when the plan carries
    // EXTRA_ZEPHYR_MODULES (as a slice-env pin or an env_append_path entry).
    if !slice_env.contains_key("EXTRA_ZEPHYR_MODULES")
        && !env_append_path.contains_key("EXTRA_ZEPHYR_MODULES")
    {
        if let Some(sdk) = sdk_root {
            let sdk = sdk.to_string_lossy().into_owned();
            let mut base: Vec<(String, String)> = inherited("EXTRA_ZEPHYR_MODULES")
                .filter(|v| !v.is_empty())
                .map(|v| vec![("EXTRA_ZEPHYR_MODULES".to_string(), v)])
                .unwrap_or_default();
            tan_core::plan_exec::apply_env_append(
                &mut base,
                &[("EXTRA_ZEPHYR_MODULES".to_string(), vec![sdk])]
                    .into_iter()
                    .collect(),
            );
            if let Some((_, joined)) = base.into_iter().next() {
                out.push(("EXTRA_ZEPHYR_MODULES", joined));
            }
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    fn slice_env(pairs: &[(&str, &str)]) -> std::collections::BTreeMap<String, String> {
        pairs
            .iter()
            .map(|(k, v)| (k.to_string(), v.to_string()))
            .collect()
    }

    fn append_map(pairs: &[(&str, &[&str])]) -> std::collections::BTreeMap<String, Vec<String>> {
        pairs
            .iter()
            .map(|(k, vs)| (k.to_string(), vs.iter().map(|s| s.to_string()).collect()))
            .collect()
    }

    /// No ambient env for every existing test — they exercise the "nothing
    /// inherited" case that already passed before this fix.
    fn no_inherited(_: &str) -> Option<String> {
        None
    }

    #[test]
    fn env_overrides_set_base_and_modules_when_absent() {
        let got = zephyr_env_overrides(
            Some(Path::new("/ws/zephyr")),
            Some(Path::new("/sdk")),
            &slice_env(&[("ALP_SDK_ROOT", "/sdk")]),
            &append_map(&[]),
            no_inherited,
        );
        assert_eq!(
            got,
            vec![
                ("ZEPHYR_BASE", "/ws/zephyr".to_string()),
                ("EXTRA_ZEPHYR_MODULES", "/sdk".to_string()),
            ]
        );
    }

    #[test]
    fn env_overrides_respect_plan_pinned_keys() {
        // The plan already pins both -> nothing is overridden.
        let got = zephyr_env_overrides(
            Some(Path::new("/ws/zephyr")),
            Some(Path::new("/sdk")),
            &slice_env(&[
                ("ZEPHYR_BASE", "/pinned"),
                ("EXTRA_ZEPHYR_MODULES", "/pinned-mod"),
            ]),
            &append_map(&[]),
            no_inherited,
        );
        assert!(got.is_empty());
    }

    #[test]
    fn env_overrides_skip_extra_modules_when_plan_appends_it() {
        // Plan wins / CLI fills gaps: the plan carries EXTRA_ZEPHYR_MODULES in
        // env_append_path, so the CLI must NOT hand-derive it — but ZEPHYR_BASE
        // (which the plan never carries) is still filled in.
        let got = zephyr_env_overrides(
            Some(Path::new("/ws/zephyr")),
            Some(Path::new("/sdk")),
            &slice_env(&[]),
            &append_map(&[("EXTRA_ZEPHYR_MODULES", &["/plan/sdk"])]),
            no_inherited,
        );
        assert_eq!(got, vec![("ZEPHYR_BASE", "/ws/zephyr".to_string())]);
    }

    #[test]
    fn env_overrides_empty_when_nothing_resolved() {
        assert!(
            zephyr_env_overrides(None, None, &slice_env(&[]), &append_map(&[]), no_inherited)
                .is_empty()
        );
    }

    #[test]
    fn env_overrides_extend_rather_than_replace_an_inherited_extra_zephyr_modules() {
        // Regression: this gap-filler used to return the bare SDK root, and
        // `assemble_slice_env`'s gap-filler merge OVERWRITES the var outright —
        // so a developer's own `export EXTRA_ZEPHYR_MODULES=/home/u/my-module`
        // vanished from the build on any plan that didn't itself pin the key.
        let got = zephyr_env_overrides(
            None,
            Some(Path::new("/sdk")),
            &slice_env(&[]),
            &append_map(&[]),
            |k| (k == "EXTRA_ZEPHYR_MODULES").then(|| "/home/u/my-module".to_string()),
        );
        let sep = if cfg!(windows) { ';' } else { ':' };
        assert_eq!(
            got,
            vec![(
                "EXTRA_ZEPHYR_MODULES",
                format!("/home/u/my-module{sep}/sdk")
            )]
        );
    }
}
