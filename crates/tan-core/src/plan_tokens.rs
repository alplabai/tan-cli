// SPDX-License-Identifier: Apache-2.0
//! Build-plan token substitution (alp-sdk #865 follow-up, "hermetic build
//! plans"). A **tokened** plan (`planPathMode: "tokened"`) carries literal
//! placeholders — `${SDK_ROOT}` / `${PROJECT_ROOT}` / `${PYTHON}` /
//! `${TOOLCHAIN_ROOT}` — in its path-bearing string fields instead of baking
//! in the emitting machine's absolute paths. This module is the CONSUMER
//! side: ONE blind string-substitution pass swapping the tokens for tan's
//! already-resolved values, plus the guards that keep a wrong substitution
//! from silently building the wrong image.
//!
//! Pure — no IO. The caller resolves the SDK checkout root, the project root
//! (the `board.yaml` directory), the planner-venv Python and the toolchain
//! root exactly ONCE and hands them in as [`TokenValues`]; `tan-cli` owns
//! invoking `git` for the `sdkCommit` check ([`sdk_commit_mismatches`] only
//! compares strings).
//!
//! A plan without `planPathMode: "tokened"` — every plan the SDK emits
//! today — is untouched by [`substitute_plan_tokens`]: a byte-identical
//! no-op, so this ships safely ahead of the SDK's emit flip.

use crate::build_plan::{BuildPlan, BuildSlice, GeneratedFile, ToolStep};
use crate::path_guard::normalize;
use std::path::Path;

/// The `planPathMode` value marking a plan whose path-bearing strings carry
/// the substitution tokens below, rather than the emitting machine's
/// absolute paths. Any other value (or the field's absence) is legacy —
/// [`substitute_plan_tokens`] leaves the plan untouched.
pub const PLAN_PATH_MODE_TOKENED: &str = "tokened";

/// alp-sdk checkout root token.
pub const TOKEN_SDK_ROOT: &str = "${SDK_ROOT}";
/// `board.yaml`'s directory token.
pub const TOKEN_PROJECT_ROOT: &str = "${PROJECT_ROOT}";
/// Planner-venv Python interpreter token.
pub const TOKEN_PYTHON: &str = "${PYTHON}";
/// Toolchain-install-root token (ADR 0021, tan-cli #84). The SDK emits this
/// instead of a host-absolute toolchain path so the executor — the only side
/// that knows what is installed on THIS host — resolves it at materialise
/// time, and `PATH` is never mutated for toolchain discovery.
pub const TOKEN_TOOLCHAIN_ROOT: &str = "${TOOLCHAIN_ROOT}";

/// tan's already-resolved substitution values. Resolve each exactly ONCE for
/// the whole plan — never re-resolved per-slice. A second, independent
/// `sdk_root` resolution landing a different checkout mid-plan is exactly the
/// "two-SDK split-brain" this pass (together with [`sdk_commit_mismatches`])
/// exists to rule out.
#[derive(Debug, Clone, Copy)]
pub struct TokenValues<'a> {
    /// Substituted for every `${SDK_ROOT}` occurrence.
    pub sdk_root: &'a str,
    /// Substituted for every `${PROJECT_ROOT}` occurrence.
    pub project_root: &'a str,
    /// Substituted for every `${PYTHON}` occurrence.
    pub python: &'a str,
    /// Substituted for every `${TOOLCHAIN_ROOT}` occurrence — `None` (or
    /// blank) when this host has no toolchain root resolved at all.
    ///
    /// Unresolved is NOT an error by itself: every plan the SDK emits today
    /// names no `${TOOLCHAIN_ROOT}`, and those must keep building on a host
    /// with no detectable toolchain install. So resolution is **lazy** — the
    /// absence only becomes [`PlanTokenError::UnresolvedToolchainRoot`] when
    /// a plan actually uses the token. Blank is folded into `None` on
    /// purpose: substituting `""` would turn `${TOOLCHAIN_ROOT}/bin/cmake`
    /// into the bare `/bin/cmake` and sail past the leftover-token guard with
    /// nothing left to catch — the same hole `sdk_root` refuses.
    pub toolchain_root: Option<&'a str>,
}

impl TokenValues<'_> {
    /// The resolved toolchain root, treating a blank string as unresolved.
    fn toolchain_root(&self) -> Option<&str> {
        self.toolchain_root.filter(|root| !root.is_empty())
    }

    fn apply(&self, value: &str) -> String {
        let out = value
            .replace(TOKEN_SDK_ROOT, self.sdk_root)
            .replace(TOKEN_PROJECT_ROOT, self.project_root)
            .replace(TOKEN_PYTHON, self.python);
        match self.toolchain_root() {
            Some(root) => out.replace(TOKEN_TOOLCHAIN_ROOT, root),
            // Left in place deliberately: `sub_field`'s leftover guard turns
            // it into the named `UnresolvedToolchainRoot` error.
            None => out,
        }
    }
}

/// Why [`substitute_plan_tokens`] refused to hand back a plan.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum PlanTokenError {
    /// A `${...}`-shaped token survived substitution — an unknown token (a
    /// 5th token this CLI doesn't resolve), an unterminated `${` (truncation/
    /// typo), or a plan bug. Names both the field and the leftover token: an
    /// old tan on a tokened plan already fails at `west` with an unusable
    /// directory name, so this fails earlier and more clearly, at the one
    /// seam that knows the plan.
    #[error("plan field `{field}` still contains an unresolved token `{token}` after substitution")]
    LeftoverToken {
        /// Dotted/indexed path to the offending field (e.g.
        /// `slices[0].command.args[3]`).
        field: String,
        /// The leftover `${...}`-shaped (or unterminated `${`) substring
        /// found in that field.
        token: String,
    },
    /// The plan names `${TOOLCHAIN_ROOT}` but this host has no toolchain root
    /// resolved. Distinct from [`Self::LeftoverToken`] on purpose: the token
    /// is one tan KNOWS, so "upgrade tan" is the wrong advice — the fix is to
    /// install a toolchain or point tan at one. Reported rather than
    /// substituted blank, for the reason on [`TokenValues::toolchain_root`].
    #[error(
        "plan field `{field}` names `{token}` but no toolchain root is resolved on this host",
        token = TOKEN_TOOLCHAIN_ROOT
    )]
    UnresolvedToolchainRoot {
        /// Dotted/indexed path to the offending field (e.g.
        /// `slices[0].command.args[3]`).
        field: String,
    },
    /// `planPathMode` is present but isn't the one value this pass knows
    /// (`"tokened"`). Refusing loudly rather than either silently treating
    /// it as legacy (no substitution — tokens would ship to `west`/disk
    /// verbatim) or silently treating it as tokened (wrong values applied).
    #[error("unknown planPathMode `{0}` (only \"tokened\" is defined)")]
    UnknownPlanPathMode(String),
}

/// ONE blind string-substitution pass over every path-bearing string field of
/// `plan`, swapping the four literal tokens for `values`. A no-op —
/// byte-identical clone — when `plan.plan_path_mode` is absent (legacy plans,
/// i.e. every plan the SDK emits today). [`PlanTokenError::UnknownPlanPathMode`]
/// when it's present but not exactly `"tokened"`.
///
/// NO arg-parsing: every `command.args` entry and `cwd`, every slice `env`
/// value, every `envAppendPath` value, `boardYaml`, and every
/// [`GeneratedFile`]'s `path`/`contents` (config + shared artefacts) are
/// plain string-replaced — matching the plan's "command shape is not frozen"
/// contract (`build_plan.rs`). After substitution, any leftover
/// `${...}`-shaped token anywhere touched fails the whole pass loudly.
pub fn substitute_plan_tokens(
    plan: &BuildPlan,
    values: &TokenValues,
) -> Result<BuildPlan, PlanTokenError> {
    match plan.plan_path_mode.as_deref() {
        None => return Ok(plan.clone()),
        Some(PLAN_PATH_MODE_TOKENED) => {}
        Some(other) => return Err(PlanTokenError::UnknownPlanPathMode(other.to_string())),
    }

    let mut out = plan.clone();
    out.board_yaml = sub_field(values, "boardYaml", &plan.board_yaml)?;
    for (i, slice) in out.slices.iter_mut().enumerate() {
        substitute_slice(values, i, slice)?;
    }
    for (i, art) in out.shared_artefacts.iter_mut().enumerate() {
        substitute_artefact(values, &format!("sharedArtefacts[{i}]"), art)?;
    }
    Ok(out)
}

fn substitute_slice(
    values: &TokenValues,
    i: usize,
    slice: &mut BuildSlice,
) -> Result<(), PlanTokenError> {
    slice.build_dir = sub_field(values, &format!("slices[{i}].buildDir"), &slice.build_dir)?;

    for (j, art) in slice.config_artefacts.iter_mut().enumerate() {
        substitute_artefact(values, &format!("slices[{i}].configArtefacts[{j}]"), art)?;
    }

    if let Some(cmd) = slice.command.as_mut() {
        substitute_command(values, i, cmd)?;
    }

    for (key, value) in slice.env.iter_mut() {
        *value = sub_field(values, &format!("slices[{i}].env.{key}"), value)?;
    }

    for (key, list) in slice.env_append_path.iter_mut() {
        for (k, value) in list.iter_mut().enumerate() {
            *value = sub_field(
                values,
                &format!("slices[{i}].envAppendPath.{key}[{k}]"),
                value,
            )?;
        }
    }
    Ok(())
}

fn substitute_command(
    values: &TokenValues,
    i: usize,
    cmd: &mut ToolStep,
) -> Result<(), PlanTokenError> {
    cmd.cwd = sub_field(values, &format!("slices[{i}].command.cwd"), &cmd.cwd)?;
    for (k, arg) in cmd.args.iter_mut().enumerate() {
        *arg = sub_field(values, &format!("slices[{i}].command.args[{k}]"), arg)?;
    }
    Ok(())
}

fn substitute_artefact(
    values: &TokenValues,
    field: &str,
    art: &mut GeneratedFile,
) -> Result<(), PlanTokenError> {
    art.path = sub_field(values, &format!("{field}.path"), &art.path)?;
    art.contents = sub_field(values, &format!("{field}.contents"), &art.contents)?;
    Ok(())
}

/// Substitute `values` into `raw`, then refuse the result if a
/// `${...}`-shaped token still remains (the "fail loudly" guard). A leftover
/// `${TOOLCHAIN_ROOT}` gets its own error: tan knows that token, it just has
/// no value for it on this host, and the two need different advice.
fn sub_field(values: &TokenValues, field: &str, raw: &str) -> Result<String, PlanTokenError> {
    let substituted = values.apply(raw);
    match find_brace_token(&substituted) {
        Some(token) if token == TOKEN_TOOLCHAIN_ROOT && values.toolchain_root().is_none() => {
            Err(PlanTokenError::UnresolvedToolchainRoot {
                field: field.to_string(),
            })
        }
        Some(token) => Err(PlanTokenError::LeftoverToken {
            field: field.to_string(),
            token,
        }),
        None => Ok(substituted),
    }
}

/// First `${...}`-shaped substring in `value`, if any. An unterminated `${`
/// (no closing `}` — truncation or a typo) still counts: it's not one of the
/// four known tokens either, so it must not ship silently.
fn find_brace_token(value: &str) -> Option<String> {
    let start = value.find("${")?;
    let rest = &value[start..];
    match rest.find('}') {
        Some(end) => Some(rest[..=end].to_string()),
        None => Some(rest.to_string()),
    }
}

/// The split-brain guard: whether a `--plan-from` plan's `sdkCommit` (when
/// present) mismatches the resolved SDK checkout's actual HEAD. Compares by
/// common-length prefix so a short (`git rev-parse --short HEAD`, 7-12 hex
/// chars) and a full 40-char SHA both compare correctly; case-insensitive
/// (git hex is lowercase, but tolerate a mixed-case caller). Either side
/// blank — an older plan without `sdkCommit`, or a caller that could not
/// resolve `git rev-parse` (no `.git`, `git` missing) — is "no signal", never
/// a mismatch: this guard only fires on an ACTUAL disagreement between two
/// known commits, never on the absence of one.
pub fn sdk_commit_mismatches(plan_commit: &str, resolved_commit: &str) -> bool {
    let (a, b) = (plan_commit.trim(), resolved_commit.trim());
    if a.is_empty() || b.is_empty() {
        return false;
    }
    // Byte-slice, not `str` char-boundary slicing: `n` (a byte count) can
    // land inside a multi-byte UTF-8 char (e.g. a corrupt/non-hex commit
    // string), which `&a[..n]` panics on but `a.as_bytes()[..n]` never does.
    let n = a.len().min(b.len());
    !a.as_bytes()[..n].eq_ignore_ascii_case(&b.as_bytes()[..n])
}

/// Guard 3: whether `${PROJECT_ROOT}` (the resolved `board.yaml`'s
/// directory) diverges from the executor's actual base dir (`west_cwd ||
/// workspace_root`, see `tan-cli`'s `native::base_dir`). They're the same
/// directory only in the default config (`board.yaml` at the workspace root,
/// no `west_cwd` override) — when a plan is tokened, substituting
/// `${PROJECT_ROOT}` from one and executing slices under the other would
/// silently build against the wrong tree.
pub fn project_root_diverges_from_exec_base(project_root: &str, exec_base: &str) -> bool {
    let a = normalize(Path::new(project_root));
    let b = normalize(Path::new(exec_base));
    if cfg!(windows) {
        // Lexical `normalize` doesn't fold drive-letter case (`Prefix::Disk(b'E')
        // != Disk(b'e')`), so `--board-yaml e:/…` vs `--project E:/…` — the same
        // NTFS path — would otherwise false-fail this guard.
        a.to_string_lossy().to_ascii_lowercase() != b.to_string_lossy().to_ascii_lowercase()
    } else {
        a != b
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::build_plan::parse_build_plan;

    const TOKENED_PLAN: &str = r#"{
      "schemaVersion": 1,
      "planPathMode": "tokened",
      "boardYaml": "${PROJECT_ROOT}/board.yaml",
      "sku": "E1M-V2N101",
      "buildRoot": "build",
      "slices": [
        {
          "coreId": "m33_sm",
          "backend": "zephyr",
          "buildDir": "build/m33_sm-zephyr",
          "configArtefacts": [
            { "path": "build/m33_sm-zephyr/alp.conf", "contents": "CONFIG_GPIO=y\n" }
          ],
          "command": {
            "tool": "west",
            "args": [
              "build", "-b", "alif_e7_dk_rtss_hp", "${PROJECT_ROOT}/app",
              "--", "-DPython3_EXECUTABLE=${PYTHON}",
              "-DSB_CONF_FILE=${SDK_ROOT}/zephyr/sysbuild/v2n/sysbuild.conf;${PROJECT_ROOT}/build/alp_sysbuild.conf"
            ],
            "cwd": "build/m33_sm-zephyr"
          },
          "env": { "ALP_SDK_ROOT": "${SDK_ROOT}" },
          "envAppendPath": {
            "EXTRA_ZEPHYR_MODULES": ["${SDK_ROOT}"],
            "PYTHONPATH": ["${SDK_ROOT}/scripts"]
          }
        }
      ],
      "sharedArtefacts": [
        { "path": "build/generated/alp/system_ipc.h", "contents": "/* built under ${PROJECT_ROOT} */\n" }
      ],
      "warnings": []
    }"#;

    fn values() -> TokenValues<'static> {
        TokenValues {
            sdk_root: "/opt/alp-sdk",
            project_root: "/work/proj",
            python: "/work/proj/.venv/bin/python",
            toolchain_root: Some("/opt/zephyr-sdk-1.0.1"),
        }
    }

    /// The same values with no toolchain root resolved — the shape of a host
    /// that has an SDK checkout and a venv but no detectable toolchain.
    fn values_without_toolchain() -> TokenValues<'static> {
        TokenValues {
            toolchain_root: None,
            ..values()
        }
    }

    #[test]
    fn tokened_plan_substitutes_every_token_everywhere() {
        let plan = parse_build_plan(TOKENED_PLAN).unwrap();
        let out = substitute_plan_tokens(&plan, &values()).expect("substitution must succeed");

        assert_eq!(out.board_yaml, "/work/proj/board.yaml");
        let slice = &out.slices[0];
        assert_eq!(
            slice.env.get("ALP_SDK_ROOT").map(String::as_str),
            Some("/opt/alp-sdk")
        );
        assert_eq!(
            slice.env_append_path["EXTRA_ZEPHYR_MODULES"],
            vec!["/opt/alp-sdk".to_string()]
        );
        assert_eq!(
            slice.env_append_path["PYTHONPATH"],
            vec!["/opt/alp-sdk/scripts".to_string()]
        );
        let cmd = slice.command.as_ref().unwrap();
        assert!(cmd.args.contains(&"/work/proj/app".to_string()));
        assert!(
            cmd.args
                .contains(&"-DPython3_EXECUTABLE=/work/proj/.venv/bin/python".to_string())
        );
        // The two-roots-in-one-arg case: a single `;`-joined SB_CONF_FILE arg
        // carries BOTH the SDK family base and the project's own sysbuild
        // overlay — both segments must be substituted correctly.
        assert!(cmd.args.contains(&
            "-DSB_CONF_FILE=/opt/alp-sdk/zephyr/sysbuild/v2n/sysbuild.conf;/work/proj/build/alp_sysbuild.conf"
                .to_string()
        ));
        assert_eq!(
            out.shared_artefacts[0].contents,
            "/* built under /work/proj */\n"
        );
    }

    #[test]
    fn toolchain_root_token_substitutes_in_args_and_env() {
        let json = TOKENED_PLAN.replace(
            r#""env": { "ALP_SDK_ROOT": "${SDK_ROOT}" }"#,
            r#""env": { "ALP_SDK_ROOT": "${SDK_ROOT}", "ZEPHYR_SDK_INSTALL_DIR": "${TOOLCHAIN_ROOT}" }"#,
        )
        .replace(
            r#""-DPython3_EXECUTABLE=${PYTHON}""#,
            r#""-DPython3_EXECUTABLE=${PYTHON}", "-DCMAKE_MAKE_PROGRAM=${TOOLCHAIN_ROOT}/hosttools/usr/bin/ninja""#,
        );
        let plan = parse_build_plan(&json).unwrap();
        let out = substitute_plan_tokens(&plan, &values()).expect("substitution must succeed");

        let slice = &out.slices[0];
        assert_eq!(
            slice.env.get("ZEPHYR_SDK_INSTALL_DIR").map(String::as_str),
            Some("/opt/zephyr-sdk-1.0.1")
        );
        assert!(slice.command.as_ref().unwrap().args.contains(
            &"-DCMAKE_MAKE_PROGRAM=/opt/zephyr-sdk-1.0.1/hosttools/usr/bin/ninja".to_string()
        ));
    }

    #[test]
    fn plan_that_never_names_toolchain_root_builds_without_one() {
        // Lazy resolution: every plan the SDK emits today names no
        // `${TOOLCHAIN_ROOT}`, so an unresolved toolchain root must NOT fail
        // them — the refusal below fires only on actual use.
        let plan = parse_build_plan(TOKENED_PLAN).unwrap();
        let out = substitute_plan_tokens(&plan, &values_without_toolchain())
            .expect("an unused token must not require a value");
        assert_eq!(out.board_yaml, "/work/proj/board.yaml");
    }

    #[test]
    fn unresolved_toolchain_root_is_refused_not_substituted_empty() {
        // The `${SDK_ROOT}` hole, re-run for this token: substituting `""`
        // would turn `${TOOLCHAIN_ROOT}/bin/cmake` into the bare `/bin/cmake`
        // with no token left for the leftover guard to catch.
        let json = TOKENED_PLAN.replace(
            r#""boardYaml": "${PROJECT_ROOT}/board.yaml""#,
            r#""boardYaml": "${TOOLCHAIN_ROOT}/board.yaml""#,
        );
        let plan = parse_build_plan(&json).unwrap();
        let err = substitute_plan_tokens(&plan, &values_without_toolchain())
            .expect_err("an unresolved toolchain root must be refused");
        assert_eq!(
            err,
            PlanTokenError::UnresolvedToolchainRoot {
                field: "boardYaml".to_string()
            }
        );
    }

    #[test]
    fn blank_toolchain_root_is_treated_as_unresolved() {
        // `Some("")` is the shape a caller that degrades a failed lookup to
        // an empty string would hand in — it must not substitute.
        let json = TOKENED_PLAN.replace(
            r#""boardYaml": "${PROJECT_ROOT}/board.yaml""#,
            r#""boardYaml": "${TOOLCHAIN_ROOT}/board.yaml""#,
        );
        let plan = parse_build_plan(&json).unwrap();
        let values = TokenValues {
            toolchain_root: Some(""),
            ..values()
        };
        let err = substitute_plan_tokens(&plan, &values)
            .expect_err("a blank toolchain root must be refused");
        assert_eq!(
            err,
            PlanTokenError::UnresolvedToolchainRoot {
                field: "boardYaml".to_string()
            }
        );
    }

    #[test]
    fn legacy_plan_without_plan_path_mode_is_byte_identical_no_op() {
        let legacy = r#"{
          "schemaVersion": 1,
          "boardYaml": "/proj/board.yaml",
          "sku": "E1M-AEN701",
          "buildRoot": "build",
          "slices": [
            { "coreId": "m55_hp", "backend": "zephyr", "buildDir": "build/m55_hp-zephyr",
              "command": { "tool": "west", "args": ["build"], "cwd": "build/m55_hp-zephyr" },
              "env": { "ALP_SDK_ROOT": "/sdk" } }
          ],
          "sharedArtefacts": []
        }"#;
        let plan = parse_build_plan(legacy).unwrap();
        let out = substitute_plan_tokens(&plan, &values()).expect("no-op must not error");
        assert_eq!(plan, out);
    }

    #[test]
    fn leftover_unknown_token_after_substitution_errors() {
        let json = TOKENED_PLAN.replace(
            r#""boardYaml": "${PROJECT_ROOT}/board.yaml""#,
            r#""boardYaml": "${UNKNOWN}/board.yaml""#,
        );
        let plan = parse_build_plan(&json).unwrap();
        let err = substitute_plan_tokens(&plan, &values()).expect_err("unknown token must fail");
        match err {
            PlanTokenError::LeftoverToken { field, token } => {
                assert_eq!(field, "boardYaml");
                assert_eq!(token, "${UNKNOWN}");
            }
            other => panic!("expected LeftoverToken, got {other:?}"),
        }
    }

    #[test]
    fn unterminated_token_is_a_leftover_too() {
        // A truncated/typo'd `${SDK_ROOT` (missing `}`) is not one of the
        // three known tokens either — must not ship to disk/west silently.
        let json = TOKENED_PLAN.replace(
            r#""boardYaml": "${PROJECT_ROOT}/board.yaml""#,
            r#""boardYaml": "${SDK_ROOT/board.yaml""#,
        );
        let plan = parse_build_plan(&json).unwrap();
        let err =
            substitute_plan_tokens(&plan, &values()).expect_err("unterminated token must fail");
        match err {
            PlanTokenError::LeftoverToken { field, token } => {
                assert_eq!(field, "boardYaml");
                assert_eq!(token, "${SDK_ROOT/board.yaml");
            }
            other => panic!("expected LeftoverToken, got {other:?}"),
        }
    }

    #[test]
    fn unknown_plan_path_mode_is_a_hard_error() {
        let json = TOKENED_PLAN.replace(r#""tokened""#, r#""tokened-v2""#);
        let plan = parse_build_plan(&json).unwrap();
        let err =
            substitute_plan_tokens(&plan, &values()).expect_err("unknown mode must be refused");
        assert_eq!(
            err,
            PlanTokenError::UnknownPlanPathMode("tokened-v2".to_string())
        );
    }

    #[test]
    fn sdk_commit_mismatch_detects_short_vs_full_sha_disagreement() {
        assert!(sdk_commit_mismatches("deadbee", "1234567abcdef"));
        // A short plan commit that's a prefix of the resolved full SHA agrees.
        assert!(!sdk_commit_mismatches(
            "deadbee",
            "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
        ));
        // Case-insensitive.
        assert!(!sdk_commit_mismatches("DEADBEE", "deadbeefdead"));
    }

    #[test]
    fn sdk_commit_mismatch_is_no_signal_when_either_side_is_blank() {
        // No plan sdkCommit (older plan) — never a mismatch.
        assert!(!sdk_commit_mismatches("", "deadbee"));
        // git rev-parse unavailable (no .git, no git binary) — never a mismatch.
        assert!(!sdk_commit_mismatches("deadbee", ""));
        assert!(!sdk_commit_mismatches("", ""));
    }

    #[test]
    fn sdk_commit_mismatch_does_not_panic_on_a_multi_byte_char_at_the_boundary() {
        // Regression: byte-length `n` used to slice `&str` directly, which
        // panics ("not a char boundary") when `n` lands mid-way through a
        // multi-byte UTF-8 char. Must compare (and return a sensible bool),
        // never panic — `panic = "abort"` in this workspace's release profile
        // would take the whole process down.
        assert!(sdk_commit_mismatches("000000\u{e9}", "0000000"));
    }

    #[test]
    fn project_root_diverges_from_exec_base_default_config_agrees() {
        // Default config: board.yaml at the workspace root, no west_cwd
        // override — PROJECT_ROOT (board.yaml's parent) equals the exec base.
        assert!(!project_root_diverges_from_exec_base(
            "/work/proj",
            "/work/proj"
        ));
        // Trailing-slash / dotted forms normalize the same way.
        assert!(!project_root_diverges_from_exec_base(
            "/work/proj/",
            "/work/proj"
        ));
    }

    #[test]
    fn project_root_diverges_from_exec_base_nested_board_yaml() {
        // `--board-yaml examples/foo/board.yaml` nests PROJECT_ROOT under the
        // workspace root, while the exec base (west_cwd || workspace_root)
        // stays the workspace root — a real divergence.
        assert!(project_root_diverges_from_exec_base(
            "/work/proj/examples/foo",
            "/work/proj"
        ));
    }

    #[cfg(windows)]
    #[test]
    fn project_root_diverges_from_exec_base_folds_windows_drive_letter_case() {
        // Regression: lexical `normalize` doesn't fold `Prefix::Disk` case, so
        // `e:/work/proj` (--board-yaml) vs `E:/work/proj` (--project) — the
        // same NTFS path — used to false-fail this guard.
        assert!(!project_root_diverges_from_exec_base(
            "e:/work/proj",
            "E:/work/proj"
        ));
    }
}
