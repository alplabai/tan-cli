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
//!
//! tan-cli #89: an unresolved `${TOOLCHAIN_ROOT}` splits into two outcomes
//! depending on WHERE it survives substitution. `boardYaml` and
//! `sharedArtefacts[]` have no owning slice — no dispatch seam to route a
//! skip/fail decision to — so they keep the hard [`PlanTokenError::UnresolvedToolchainRoot`]
//! this pass always raised. A slice field, though, has an owning slice AND an
//! owning dispatch seam (`executionPolicy.missingTool`, the same knob a
//! missing `bitbake` already uses): this is a HOST-provisioning fact, not a
//! plan/version bug, so this pass reports it as a [`DemotedSlice`] instead of
//! erroring the whole plan, and leaves the skip-vs-fail call to the caller at
//! dispatch time. [`PlanTokenError::LeftoverToken`] is unaffected either way —
//! an unknown token is a version/bug fact, never demoted, always plan-fatal.

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

/// A slice whose fields still name `${TOOLCHAIN_ROOT}` with no host value.
/// Reported, not erred: the CALLER routes it to `executionPolicy.missingTool`
/// at dispatch — this pass has no business deciding skip vs fail, only
/// noticing the condition and naming where it hit.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DemotedSlice {
    /// Index into `plan.slices` — the caller's dispatch loop is index-keyed,
    /// so this is what actually routes the demotion to the right slice.
    pub slice_index: usize,
    /// Carried alongside the index purely as a belt-and-braces check: if a
    /// future change reorders/filters `plan.slices` between this pass and
    /// dispatch, an index-only signal would silently misattribute the
    /// demotion to the wrong slice. `core_id` lets the caller assert it still
    /// matches instead of trusting the index blind.
    pub core_id: String,
    /// First offending field path, e.g. `slices[1].env.ZEPHYR_SDK_INSTALL_DIR`.
    /// Only the first is kept — this pass still substitutes (and length-guards)
    /// every other field in the slice, but one name is enough to point a user
    /// at the fix, and a demoted slice never dispatches anyway.
    pub field: String,
}

/// ONE blind string-substitution pass over every path-bearing string field of
/// `plan`, swapping the four literal tokens for `values`. A no-op —
/// byte-identical clone, empty demotion list — when `plan.plan_path_mode` is
/// absent (legacy plans, i.e. every plan the SDK emits today).
/// [`PlanTokenError::UnknownPlanPathMode`] when it's present but not exactly
/// `"tokened"`.
///
/// NO arg-parsing: every `command.args` entry and `cwd`, every slice `env`
/// value, every `envAppendPath` value, `boardYaml`, and every
/// [`GeneratedFile`]'s `path`/`contents` (config + shared artefacts) are
/// plain string-replaced — matching the plan's "command shape is not frozen"
/// contract (`build_plan.rs`). After substitution, any leftover
/// `${...}`-shaped token anywhere touched fails the whole pass loudly —
/// EXCEPT a leftover `${TOOLCHAIN_ROOT}` confined to a slice's own fields,
/// which has an owning dispatch seam to route to instead (see the module
/// doc): that slice is reported via the returned [`DemotedSlice`] list and
/// its `configArtefacts` are stripped (nothing will ever consume them this
/// run — see `materialise_plan`'s doc for why writing them would be unsafe),
/// but the plan as a whole still succeeds. The same token surviving in
/// `boardYaml`/`sharedArtefacts[]` (no owning slice) is still the hard
/// [`PlanTokenError::UnresolvedToolchainRoot`] this pass always raised.
pub fn substitute_plan_tokens(
    plan: &BuildPlan,
    values: &TokenValues,
) -> Result<(BuildPlan, Vec<DemotedSlice>), PlanTokenError> {
    match plan.plan_path_mode.as_deref() {
        None => return Ok((plan.clone(), Vec::new())),
        Some(PLAN_PATH_MODE_TOKENED) => {}
        Some(other) => return Err(PlanTokenError::UnknownPlanPathMode(other.to_string())),
    }

    let mut out = plan.clone();
    // Hard site 1/2: no owning slice to demote to.
    out.board_yaml = sub_field(values, "boardYaml", &plan.board_yaml)?;
    let mut demoted = Vec::new();
    for (i, slice) in out.slices.iter_mut().enumerate() {
        if let Some(field) = substitute_slice(values, i, slice)? {
            demoted.push(DemotedSlice {
                slice_index: i,
                core_id: slice.core_id.clone(),
                field,
            });
            // Strip AFTER the slice's fields (including these artefacts'
            // own contents) have been fully scanned — a ${UNKNOWN} inside a
            // demoted slice's configArtefact contents must still hard-fail
            // as LeftoverToken before we ever get here to clear it.
            slice.config_artefacts.clear();
        }
    }
    // Hard site 2/2: sharedArtefacts are cross-slice — same "no owning
    // slice" reasoning as boardYaml above.
    for (i, art) in out.shared_artefacts.iter_mut().enumerate() {
        substitute_artefact(values, &format!("sharedArtefacts[{i}]"), art)?;
    }
    Ok((out, demoted))
}

/// Substitute every field of one slice, leniently for `${TOOLCHAIN_ROOT}`
/// (see the module doc): returns the first field that still names it
/// unresolved, if any, so the caller can build a [`DemotedSlice`] — but a
/// `${UNKNOWN}` anywhere in the slice still propagates
/// [`PlanTokenError::LeftoverToken`] immediately via `?`, ending the scan
/// right there (that condition is plan-fatal regardless of which field hits
/// it first, so there's no reason to keep going once it's found).
fn substitute_slice(
    values: &TokenValues,
    i: usize,
    slice: &mut BuildSlice,
) -> Result<Option<String>, PlanTokenError> {
    // Keeps only the FIRST unresolved-toolchain field name; every field below
    // still gets substituted (and LeftoverToken-scanned) regardless, so a bug
    // in a LATER field of an already-demoted slice is never masked.
    let mut demoted_field: Option<String> = None;

    let field = format!("slices[{i}].buildDir");
    let (sub, unresolved) = sub_field_lenient(values, &field, &slice.build_dir)?;
    slice.build_dir = sub;
    record_first(field, unresolved, &mut demoted_field);

    // Before `command`/`env` (matching the module doc's "env + command.args"
    // ordering): a slice's own config is the more natural first thing to
    // name in a demotion message, and processing order only affects WHICH
    // field name is reported first, never whether the scan is complete.
    for (j, art) in slice.config_artefacts.iter_mut().enumerate() {
        let base = format!("slices[{i}].configArtefacts[{j}]");
        if let Some(field) = substitute_artefact_lenient(values, &base, art)? {
            record_first(field, true, &mut demoted_field);
        }
    }

    for (key, value) in slice.env.iter_mut() {
        let field = format!("slices[{i}].env.{key}");
        let (sub, unresolved) = sub_field_lenient(values, &field, value)?;
        *value = sub;
        record_first(field, unresolved, &mut demoted_field);
    }

    for (key, list) in slice.env_append_path.iter_mut() {
        for (k, value) in list.iter_mut().enumerate() {
            let field = format!("slices[{i}].envAppendPath.{key}[{k}]");
            let (sub, unresolved) = sub_field_lenient(values, &field, value)?;
            *value = sub;
            record_first(field, unresolved, &mut demoted_field);
        }
    }

    if let Some(cmd) = slice.command.as_mut() {
        if let Some(field) = substitute_command_lenient(values, i, cmd)? {
            record_first(field, true, &mut demoted_field);
        }
    }

    Ok(demoted_field)
}

/// Record `field` as the demoted field IFF `unresolved` and nothing has been
/// recorded yet — the shared "keep only the first" rule used by every field
/// loop in [`substitute_slice`].
fn record_first(field: String, unresolved: bool, out: &mut Option<String>) {
    if unresolved && out.is_none() {
        *out = Some(field);
    }
}

fn substitute_command_lenient(
    values: &TokenValues,
    i: usize,
    cmd: &mut ToolStep,
) -> Result<Option<String>, PlanTokenError> {
    let mut demoted_field: Option<String> = None;

    let field = format!("slices[{i}].command.cwd");
    let (sub, unresolved) = sub_field_lenient(values, &field, &cmd.cwd)?;
    cmd.cwd = sub;
    if unresolved && demoted_field.is_none() {
        demoted_field = Some(field);
    }

    for (k, arg) in cmd.args.iter_mut().enumerate() {
        let field = format!("slices[{i}].command.args[{k}]");
        let (sub, unresolved) = sub_field_lenient(values, &field, arg)?;
        *arg = sub;
        if unresolved && demoted_field.is_none() {
            demoted_field = Some(field);
        }
    }

    Ok(demoted_field)
}

/// The slice-owned variant of `substitute_artefact` below: `configArtefacts`
/// live inside a slice, so an unresolved `${TOOLCHAIN_ROOT}` in one is
/// demotable too (the whole artefact list is stripped from a demoted slice's
/// output plan by the caller) — a `${UNKNOWN}`, though, is still
/// [`PlanTokenError::LeftoverToken`], scanned for here BEFORE the caller ever
/// gets a chance to strip the list.
fn substitute_artefact_lenient(
    values: &TokenValues,
    field: &str,
    art: &mut GeneratedFile,
) -> Result<Option<String>, PlanTokenError> {
    let mut demoted_field: Option<String> = None;

    let path_field = format!("{field}.path");
    let (sub, unresolved) = sub_field_lenient(values, &path_field, &art.path)?;
    art.path = sub;
    if unresolved {
        demoted_field = Some(path_field);
    }

    let contents_field = format!("{field}.contents");
    let (sub, unresolved) = sub_field_lenient(values, &contents_field, &art.contents)?;
    art.contents = sub;
    if unresolved && demoted_field.is_none() {
        demoted_field = Some(contents_field);
    }

    Ok(demoted_field)
}

/// Plan-level sites (`boardYaml`, `sharedArtefacts[]`) have no owning slice to
/// route a missing-toolchain skip to, so they keep the hard, byte-identical
/// [`PlanTokenError::UnresolvedToolchainRoot`] this pass always raised —
/// thin wrapper over [`sub_field_lenient`] that turns "unresolved" back into
/// an error instead of a signal for the caller to act on.
fn sub_field(values: &TokenValues, field: &str, raw: &str) -> Result<String, PlanTokenError> {
    let (substituted, unresolved_toolchain) = sub_field_lenient(values, field, raw)?;
    if unresolved_toolchain {
        return Err(PlanTokenError::UnresolvedToolchainRoot {
            field: field.to_string(),
        });
    }
    Ok(substituted)
}

/// Substitute `values` into `raw`, then scan the WHOLE result for every
/// remaining `${...}`-shaped token — not just the first, unlike the
/// single-token check this replaced (tan-cli #89): a field can carry BOTH an
/// unresolved `${TOOLCHAIN_ROOT}` and a genuinely unknown token
/// (`"${TOOLCHAIN_ROOT}/x/${UNKNOWN}"`), and the unknown one must still fail
/// loudly even when it comes second — an unknown token is a version/bug fact
/// and outranks a provisioning fact. Returns the substituted string plus
/// whether an unresolved `${TOOLCHAIN_ROOT}` was seen; the caller (`sub_field`
/// vs `substitute_slice`) decides whether that's plan-fatal or demotable.
fn sub_field_lenient(
    values: &TokenValues,
    field: &str,
    raw: &str,
) -> Result<(String, bool), PlanTokenError> {
    let substituted = values.apply(raw);
    let mut unresolved_toolchain = false;
    let mut offset = 0;
    while let Some((start, token)) = find_brace_token_from(&substituted, offset) {
        if token != TOKEN_TOOLCHAIN_ROOT {
            return Err(PlanTokenError::LeftoverToken {
                field: field.to_string(),
                token,
            });
        }
        // Reaching here at all means `values.toolchain_root()` is `None`:
        // `apply()` already replaced every `${TOOLCHAIN_ROOT}` occurrence
        // when a value WAS resolved, so one surviving to this scan can only
        // be the unresolved case.
        unresolved_toolchain = true;
        // Advance past THIS match and keep scanning — a later unknown token
        // in the same field must still be found, not masked by stopping at
        // the first (known) one.
        offset = start + token.len();
    }
    Ok((substituted, unresolved_toolchain))
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

/// First `${...}`-shaped substring in `value` at or after byte offset
/// `offset`, together with its start offset (so the caller can advance past
/// it and keep scanning). An unterminated `${` (no closing `}` — truncation
/// or a typo) still counts and ends the scan there: nothing after an
/// unterminated brace could itself be a well-formed further token.
fn find_brace_token_from(value: &str, offset: usize) -> Option<(usize, String)> {
    let start = offset + value.get(offset..)?.find("${")?;
    let rest = &value[start..];
    match rest.find('}') {
        Some(end) => Some((start, rest[..=end].to_string())),
        None => Some((start, rest.to_string())),
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
        let (out, demoted) =
            substitute_plan_tokens(&plan, &values()).expect("substitution must succeed");
        assert!(demoted.is_empty(), "no slice names TOOLCHAIN_ROOT here");

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
        let (out, demoted) =
            substitute_plan_tokens(&plan, &values()).expect("substitution must succeed");
        // A RESOLVED toolchain root substitutes normally — nothing survives
        // to be demoted.
        assert!(demoted.is_empty());

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
        let (out, demoted) = substitute_plan_tokens(&plan, &values_without_toolchain())
            .expect("an unused token must not require a value");
        assert_eq!(out.board_yaml, "/work/proj/board.yaml");
        assert!(demoted.is_empty());
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
        let (out, demoted) =
            substitute_plan_tokens(&plan, &values()).expect("no-op must not error");
        assert_eq!(plan, out);
        assert!(demoted.is_empty());
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

    /// A two-slice tokened plan: slice 0 names `${TOOLCHAIN_ROOT}` in its
    /// `env` (and carries a `configArtefacts` entry with no token, to prove
    /// it gets stripped regardless); slice 1 names no `${TOOLCHAIN_ROOT}` at
    /// all. tan-cli #89's whole point: an unresolved toolchain root confined
    /// to slice 0 must not cost slice 1 (or the plan as a whole) anything.
    const TWO_SLICE_TOKENED_PLAN: &str = r#"{
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
            "args": ["build"],
            "cwd": "build/m33_sm-zephyr"
          },
          "env": { "ZEPHYR_SDK_INSTALL_DIR": "${TOOLCHAIN_ROOT}" }
        },
        {
          "coreId": "m55_hp",
          "backend": "zephyr",
          "buildDir": "build/m55_hp-zephyr",
          "command": { "tool": "west", "args": ["build"], "cwd": "build/m55_hp-zephyr" },
          "env": { "ALP_SDK_ROOT": "${SDK_ROOT}" }
        }
      ],
      "sharedArtefacts": []
    }"#;

    #[test]
    fn slice_confined_toolchain_root_demotes_the_slice_without_failing_the_plan() {
        let plan = parse_build_plan(TWO_SLICE_TOKENED_PLAN).unwrap();
        let (out, demoted) = substitute_plan_tokens(&plan, &values_without_toolchain())
            .expect("a slice-confined unresolved token must not fail the plan");

        assert_eq!(
            demoted,
            vec![DemotedSlice {
                slice_index: 0,
                core_id: "m33_sm".to_string(),
                field: "slices[0].env.ZEPHYR_SDK_INSTALL_DIR".to_string(),
            }]
        );
        // Stripped: nothing left for materialise to write with a live token
        // in path or contents (see substitute_plan_tokens' doc + the #89
        // MATERIALISE DECISION).
        assert!(out.slices[0].config_artefacts.is_empty());
        // The literal token survives in the demoted slice's OTHER fields —
        // fenced by the executor's index check, never dispatched.
        assert_eq!(
            out.slices[0]
                .env
                .get("ZEPHYR_SDK_INSTALL_DIR")
                .map(String::as_str),
            Some("${TOOLCHAIN_ROOT}")
        );
        // Slice 1 is untouched by slice 0's demotion — fully substituted.
        assert_eq!(
            out.slices[1].env.get("ALP_SDK_ROOT").map(String::as_str),
            Some("/opt/alp-sdk")
        );
    }

    #[test]
    fn shared_artefact_toolchain_root_is_still_the_hard_error() {
        // sharedArtefacts are cross-slice — no owning slice to demote to —
        // so this stays the plan-fatal UnresolvedToolchainRoot, same as
        // boardYaml.
        let json = TOKENED_PLAN.replace(
            r#""path": "build/generated/alp/system_ipc.h", "contents": "/* built under ${PROJECT_ROOT} */\n""#,
            r#""path": "build/generated/alp/system_ipc.h", "contents": "${TOOLCHAIN_ROOT}\n""#,
        );
        let plan = parse_build_plan(&json).unwrap();
        let err = substitute_plan_tokens(&plan, &values_without_toolchain())
            .expect_err("an unresolved toolchain root in sharedArtefacts must be refused");
        assert_eq!(
            err,
            PlanTokenError::UnresolvedToolchainRoot {
                field: "sharedArtefacts[0].contents".to_string()
            }
        );
    }

    #[test]
    fn unknown_token_elsewhere_in_an_otherwise_demotable_slice_is_still_a_hard_error() {
        // Slice 0 would be demotable (env names TOOLCHAIN_ROOT) but ALSO
        // carries a genuinely unknown token in its command args — demotion
        // must never swallow a real bug elsewhere in the same slice.
        let json = TWO_SLICE_TOKENED_PLAN.replace(
            r#""args": ["build"],
            "cwd": "build/m33_sm-zephyr""#,
            r#""args": ["build", "${UNKNOWN}"],
            "cwd": "build/m33_sm-zephyr""#,
        );
        let plan = parse_build_plan(&json).unwrap();
        let err = substitute_plan_tokens(&plan, &values_without_toolchain())
            .expect_err("an unknown token must still hard-fail the plan");
        match err {
            PlanTokenError::LeftoverToken { field, token } => {
                assert_eq!(field, "slices[0].command.args[1]");
                assert_eq!(token, "${UNKNOWN}");
            }
            other => panic!("expected LeftoverToken, got {other:?}"),
        }
    }

    #[test]
    fn a_toolchain_root_followed_by_an_unknown_token_in_one_field_reports_the_unknown_token() {
        // A single field carrying BOTH tokens: the known-but-unresolved one
        // must not stop the scan before the genuinely unknown one is found —
        // an unknown token outranks a provisioning fact (module doc).
        let json = TWO_SLICE_TOKENED_PLAN.replace(
            r#""ZEPHYR_SDK_INSTALL_DIR": "${TOOLCHAIN_ROOT}""#,
            r#""ZEPHYR_SDK_INSTALL_DIR": "${TOOLCHAIN_ROOT}/x/${UNKNOWN}""#,
        );
        let plan = parse_build_plan(&json).unwrap();
        let err = substitute_plan_tokens(&plan, &values_without_toolchain())
            .expect_err("the trailing unknown token must still be caught");
        match err {
            PlanTokenError::LeftoverToken { field, token } => {
                assert_eq!(field, "slices[0].env.ZEPHYR_SDK_INSTALL_DIR");
                assert_eq!(token, "${UNKNOWN}");
            }
            other => panic!("expected LeftoverToken, got {other:?}"),
        }
    }

    #[test]
    fn demoted_slice_config_artefact_contents_with_unknown_token_is_still_a_hard_error() {
        // The MATERIALISE DECISION's load-bearing ordering: the LeftoverToken
        // scan of configArtefacts contents happens BEFORE the caller ever
        // clears the list on demotion — an unknown token in there must not
        // be silently discarded along with the rest of the artefact.
        let json = TWO_SLICE_TOKENED_PLAN.replace(
            r#""contents": "CONFIG_GPIO=y\n""#,
            r#""contents": "${UNKNOWN}\n""#,
        );
        let plan = parse_build_plan(&json).unwrap();
        let err = substitute_plan_tokens(&plan, &values_without_toolchain())
            .expect_err("an unknown token in configArtefacts contents must still hard-fail");
        match err {
            PlanTokenError::LeftoverToken { field, token } => {
                assert_eq!(field, "slices[0].configArtefacts[0].contents");
                assert_eq!(token, "${UNKNOWN}");
            }
            other => panic!("expected LeftoverToken, got {other:?}"),
        }
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
