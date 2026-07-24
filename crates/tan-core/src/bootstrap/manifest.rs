// SPDX-License-Identifier: Apache-2.0
//! CONSUMER side of `alp-sdk/metadata/bootstrap.json` — the single source of
//! truth for the workspace-assembly FACTS that `scripts/bootstrap.sh`,
//! `scripts/bootstrap.ps1` and tan all need (alp-sdk#917). The manifest's own
//! `_comment` names tan as a required consumer, and
//! `scripts/check_bootstrap_manifest.py` is its drift gate — but that gate does
//! NOT scan tan-cli, so hand-ported constants here would desync silently.
//! Reading the manifest is what keeps us honest.
//!
//! Facts only. Control flow (`$ZEPHYR_BASE` reuse/rejection, venv idempotency,
//! `.west` branching) stays CODE in each executor, per the manifest's contract.
//!
//! Version-skew guard, matching how tan already treats the build-plan
//! (`build_plan::parse_build_plan`): an ABSENT manifest falls back to the
//! hand-ported constants (a legacy SDK predating #917 — the supported path
//! while #917 is unmerged); a manifest with an UNSUPPORTED `schemaVersion` is a
//! hard error naming the version. Silently falling back there is exactly the
//! RFC #843 drift this whole architecture exists to kill.

use serde::Deserialize;

use super::{VenvLayout, WEST_REQUIREMENT, ZEPHYR_VERSION, venv_layout};

/// The only `schemaVersion` this consumer understands
/// (`metadata/schemas/bootstrap-v1.schema.json` pins it to `const: 1`).
pub const BOOTSTRAP_MANIFEST_SCHEMA_VERSION: u32 = 1;

/// Manifest path relative to the SDK checkout root.
pub const BOOTSTRAP_MANIFEST_REL_PATH: &str = "metadata/bootstrap.json";

/// Workspace-topdir token. Its sibling `${SDK_ROOT}` is
/// [`crate::plan_tokens::TOKEN_SDK_ROOT`] — deliberately the same convention as
/// the build-plan's `planPathMode: tokened`.
///
/// `plan_tokens::substitute_plan_tokens` is NOT reused: it walks `BuildPlan`
/// structs and its `TokenValues` triple is `${SDK_ROOT}`/`${PROJECT_ROOT}`/
/// `${PYTHON}` — a different shape and a different token set (there is no
/// `${WORKSPACE_DIR}` there, and no `${PROJECT_ROOT}`/`${PYTHON}` here). Only
/// the shared token spelling is imported.
pub const TOKEN_WORKSPACE_DIR: &str = "${WORKSPACE_DIR}";

/// Why a manifest was rejected.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum BootstrapManifestError {
    /// `schemaVersion` is not [`BOOTSTRAP_MANIFEST_SCHEMA_VERSION`].
    UnsupportedSchemaVersion(u64),
    /// The document is not valid JSON, or a required field is missing/mistyped.
    Malformed(String),
}

impl std::fmt::Display for BootstrapManifestError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            BootstrapManifestError::UnsupportedSchemaVersion(found) => write!(
                f,
                "{BOOTSTRAP_MANIFEST_REL_PATH} declares schemaVersion {found}, but this `tan` \
                 supports only {BOOTSTRAP_MANIFEST_SCHEMA_VERSION}. Update `tan`, or pin an SDK \
                 whose bootstrap manifest this version understands."
            ),
            BootstrapManifestError::Malformed(detail) => {
                write!(
                    f,
                    "{BOOTSTRAP_MANIFEST_REL_PATH} could not be read: {detail}"
                )
            }
        }
    }
}

/// `${SDK_ROOT}` / `${WORKSPACE_DIR}` substitution values.
///
/// `workspace_dir` is applied at RENDER time rather than baked in at load
/// time, because workspace selection can repoint it afterwards (adopting a
/// compatible `$ZEPHYR_BASE` tree). `bootstrap.sh` re-substitutes on every
/// `print_env_lines` call for exactly this reason; `bootstrap.ps1` binds
/// `$EnvPairs` once BEFORE selection, so its closing "Next steps" prints the
/// pre-reuse path — we follow bash, which is the correct one.
#[derive(Debug, Clone, Copy)]
pub struct Tokens<'a> {
    /// Substituted for `${SDK_ROOT}`.
    pub sdk_root: &'a str,
    /// Substituted for `${WORKSPACE_DIR}`.
    pub workspace_dir: &'a str,
}

impl Tokens<'_> {
    /// One blind substitution pass, mirroring both scripts' `tok()`/
    /// `Resolve-BootstrapToken`.
    pub fn apply(&self, value: &str) -> String {
        value
            .replace(crate::plan_tokens::TOKEN_SDK_ROOT, self.sdk_root)
            .replace(TOKEN_WORKSPACE_DIR, self.workspace_dir)
    }
}

/// A per-OS optional-native-libs hint.
#[derive(Debug, Clone, PartialEq, Eq, Deserialize)]
pub struct NativeLibHint {
    /// Human-readable explanation, safe to print verbatim.
    pub note: String,
    /// A copy-pasteable install command, or `None` where the OS has none.
    pub command: Option<String>,
}

/// The workspace-assembly facts, however they were obtained: parsed from
/// `metadata/bootstrap.json`, or reconstructed from the hand-ported constants
/// when the SDK predates alp-sdk#917.
///
/// Deliberately ONE shape for both sources so the executor never branches on
/// provenance — only [`BootstrapFacts::from_manifest`] records which it was,
/// for the envelope.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct BootstrapFacts {
    /// `zephyr.version` — the pin the workspace-reuse test compares against.
    pub zephyr_version: String,
    /// `zephyr.requirementsPath` — workspace-relative, POSIX-style.
    pub zephyr_requirements_path: String,
    /// `venv.dirName`.
    pub venv_dir_name: String,
    /// `venv.posixBinDir`.
    pub venv_posix_bin_dir: String,
    /// `venv.windowsBinDir`.
    pub venv_windows_bin_dir: String,
    /// `prerequisites.posix`.
    pub prerequisites_posix: Vec<String>,
    /// `prerequisites.windows`.
    pub prerequisites_windows: Vec<String>,
    /// `prerequisites.pythonMinVersion`, as `(major, minor)`.
    pub python_min_version: (u32, u32),
    /// `west.pipSpec` — the requirement handed to `pip install --upgrade`.
    pub west_pip_spec: String,
    /// `west.initArgs` (the repo-root argument is appended by the executor).
    pub west_init_args: Vec<String>,
    /// `west.updateArgs`.
    pub west_update_args: Vec<String>,
    /// `west.exportArgs`.
    pub west_export_args: Vec<String>,
    /// `west.extensionGuardCommand` — the substring grepped in `west help`.
    pub west_extension_guard: String,
    /// `pip.bootstrapUpgrade`.
    pub pip_bootstrap_upgrade: Vec<String>,
    /// `pip.sdkExtras`.
    pub pip_sdk_extras: Vec<String>,
    /// `pip.editableInstall`, still tokened.
    pub pip_editable_install: String,
    /// `env`, ordered, still tokened.
    pub env: Vec<(String, String)>,
    /// `nativeLibHints.linux`.
    pub hint_linux: NativeLibHint,
    /// `nativeLibHints.macos`.
    pub hint_macos: NativeLibHint,
    /// `nativeLibHints.windows`.
    pub hint_windows: NativeLibHint,
    /// True when these came from the SDK's manifest rather than the fallback
    /// constants.
    pub from_manifest: bool,
}

impl BootstrapFacts {
    /// The venv executable sub-directory for this host.
    pub fn venv_bin_dir(&self, is_windows: bool) -> &str {
        if is_windows {
            &self.venv_windows_bin_dir
        } else {
            &self.venv_posix_bin_dir
        }
    }

    /// The prerequisite tool list for this host. The two lists genuinely differ
    /// (Windows adds `ninja`) and the manifest records that faithfully rather
    /// than unifying them — so does this.
    pub fn prerequisites(&self, is_windows: bool) -> &[String] {
        if is_windows {
            &self.prerequisites_windows
        } else {
            &self.prerequisites_posix
        }
    }

    /// The optional-native-libs hint for this host.
    pub fn native_lib_hint(&self, host: super::HostOs) -> Option<&NativeLibHint> {
        match host {
            super::HostOs::Linux => Some(&self.hint_linux),
            super::HostOs::MacOs => Some(&self.hint_macos),
            super::HostOs::Windows => Some(&self.hint_windows),
            // bootstrap.sh's `*)` arm: no hint, just the not-detected line.
            super::HostOs::Other => None,
        }
    }
}

/// Wire shape of `metadata/bootstrap.json`. Only the fields tan consumes are
/// modelled; `_comment` and anything the SDK adds later are ignored.
#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
struct ManifestDoc {
    zephyr: ZephyrDoc,
    venv: VenvDoc,
    prerequisites: PrerequisitesDoc,
    west: WestDoc,
    pip: PipDoc,
    /// `serde_json`'s `preserve_order` feature keeps JSON object order, which
    /// is what makes the rendered `export`/`$env:` lines come out in the
    /// manifest's declared order like both scripts do.
    env: serde_json::Map<String, serde_json::Value>,
    native_lib_hints: NativeLibHintsDoc,
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
struct ZephyrDoc {
    version: String,
    requirements_path: String,
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
struct VenvDoc {
    dir_name: String,
    posix_bin_dir: String,
    windows_bin_dir: String,
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
struct PrerequisitesDoc {
    posix: Vec<String>,
    windows: Vec<String>,
    python_min_version: String,
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
struct WestDoc {
    pip_spec: String,
    init_args: Vec<String>,
    update_args: Vec<String>,
    export_args: Vec<String>,
    extension_guard_command: String,
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
struct PipDoc {
    bootstrap_upgrade: Vec<String>,
    sdk_extras: Vec<String>,
    editable_install: String,
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
struct NativeLibHintsDoc {
    linux: NativeLibHint,
    macos: NativeLibHint,
    windows: NativeLibHint,
}

/// Parse `metadata/bootstrap.json`. Pure — the caller reads the file and
/// decides what an absent file means (see [`fallback_facts`]).
pub fn parse_bootstrap_manifest(text: &str) -> Result<BootstrapFacts, BootstrapManifestError> {
    // Read `schemaVersion` on its own FIRST: a future manifest may legitimately
    // reshape fields this consumer would otherwise fail to deserialize, and the
    // user deserves "unsupported version N", not "missing field `foo`".
    let probe: serde_json::Value =
        serde_json::from_str(text).map_err(|e| BootstrapManifestError::Malformed(e.to_string()))?;
    match probe
        .get("schemaVersion")
        .and_then(serde_json::Value::as_u64)
    {
        Some(v) if v == BOOTSTRAP_MANIFEST_SCHEMA_VERSION as u64 => {}
        Some(other) => return Err(BootstrapManifestError::UnsupportedSchemaVersion(other)),
        None => {
            return Err(BootstrapManifestError::Malformed(
                "missing `schemaVersion`".to_string(),
            ));
        }
    }
    let doc: ManifestDoc =
        serde_json::from_str(text).map_err(|e| BootstrapManifestError::Malformed(e.to_string()))?;
    let python_min_version =
        parse_min_version(&doc.prerequisites.python_min_version).ok_or_else(|| {
            BootstrapManifestError::Malformed(format!(
                "prerequisites.pythonMinVersion `{}` is not MAJOR.MINOR",
                doc.prerequisites.python_min_version
            ))
        })?;
    Ok(BootstrapFacts {
        zephyr_version: doc.zephyr.version,
        zephyr_requirements_path: doc.zephyr.requirements_path,
        venv_dir_name: doc.venv.dir_name,
        venv_posix_bin_dir: doc.venv.posix_bin_dir,
        venv_windows_bin_dir: doc.venv.windows_bin_dir,
        prerequisites_posix: doc.prerequisites.posix,
        prerequisites_windows: doc.prerequisites.windows,
        python_min_version,
        west_pip_spec: doc.west.pip_spec,
        west_init_args: doc.west.init_args,
        west_update_args: doc.west.update_args,
        west_export_args: doc.west.export_args,
        west_extension_guard: doc.west.extension_guard_command,
        pip_bootstrap_upgrade: doc.pip.bootstrap_upgrade,
        pip_sdk_extras: doc.pip.sdk_extras,
        pip_editable_install: doc.pip.editable_install,
        env: doc
            .env
            .into_iter()
            .map(|(k, v)| (k, v.as_str().unwrap_or_default().to_string()))
            .collect(),
        hint_linux: doc.native_lib_hints.linux,
        hint_macos: doc.native_lib_hints.macos,
        hint_windows: doc.native_lib_hints.windows,
        from_manifest: true,
    })
}

/// `"3.10"` -> `(3, 10)`.
fn parse_min_version(raw: &str) -> Option<(u32, u32)> {
    let (major, minor) = raw.trim().split_once('.')?;
    Some((major.trim().parse().ok()?, minor.trim().parse().ok()?))
}

/// The hand-ported facts, used when the SDK has no `metadata/bootstrap.json`
/// (any release predating alp-sdk#917 — today, every released SDK).
///
/// These are the LAST-KNOWN values, transcribed from the pre-#917 revision of
/// the two bootstrap scripts. They are the documented fallback ONLY: when the
/// manifest exists it wins outright, so a pin bump on the SDK side reaches tan
/// without a tan release. `check_bootstrap_manifest.py` does not scan this
/// crate, so treat every literal below as stale-by-default.
pub fn fallback_facts(min_python: (u32, u32)) -> BootstrapFacts {
    let owned = |items: &[&str]| items.iter().map(|s| (*s).to_string()).collect();
    let posix = venv_layout(false);
    let windows = venv_layout(true);
    BootstrapFacts {
        zephyr_version: ZEPHYR_VERSION.to_string(),
        zephyr_requirements_path: "zephyr/scripts/requirements.txt".to_string(),
        venv_dir_name: ".venv".to_string(),
        venv_posix_bin_dir: posix.bin_dir.to_string(),
        venv_windows_bin_dir: windows.bin_dir.to_string(),
        prerequisites_posix: owned(&["git", "cmake", "python3"]),
        prerequisites_windows: owned(&["git", "cmake", "python", "ninja"]),
        python_min_version: min_python,
        west_pip_spec: WEST_REQUIREMENT.to_string(),
        west_init_args: owned(&["init", "-l"]),
        west_update_args: owned(&["update", "--narrow", "-o=--depth=1"]),
        west_export_args: owned(&["zephyr-export"]),
        west_extension_guard: "alp-migrate".to_string(),
        pip_bootstrap_upgrade: owned(&["pip", "wheel"]),
        pip_sdk_extras: owned(&["jsonschema", "imgtool"]),
        pip_editable_install: crate::plan_tokens::TOKEN_SDK_ROOT.to_string(),
        env: vec![
            (
                "ZEPHYR_BASE".to_string(),
                format!("{TOKEN_WORKSPACE_DIR}/zephyr"),
            ),
            ("ZEPHYR_TOOLCHAIN_VARIANT".to_string(), "zephyr".to_string()),
        ],
        hint_linux: NativeLibHint {
            note: "Optional native libraries unlock the Yocto-side backends: libmosquitto-dev -> \
                   alp_mqtt_* (cleartext + TLS), libasound2-dev -> alp_audio_*, libssl-dev -> \
                   alp_hash_* / alp_aead_* / alp_random_bytes."
                .to_string(),
            command: Some(
                "sudo apt-get install -y libmosquitto-dev libasound2-dev libssl-dev pkg-config"
                    .to_string(),
            ),
        },
        hint_macos: NativeLibHint {
            note: "Equivalents via Homebrew. macOS uses CoreAudio rather than ALSA, so the Yocto \
                   audio backend doesn't apply on macOS hosts. OpenSSL ships with macOS."
                .to_string(),
            command: Some("brew install mosquitto pkg-config".to_string()),
        },
        hint_windows: NativeLibHint {
            note: "Under Git Bash / MSYS2 the Yocto-side backends aren't intended to run -- the \
                   canonical use is WSL2 + Ubuntu with the linux command above; skip this step on \
                   native Windows. The Arm GNU Toolchain and the Zephyr SDK (`west sdk install`) \
                   are separate manual, one-time installs on native Windows -- not auto-installed \
                   by bootstrap.ps1; native_sim / Yocto need WSL2 \
                   (docs/cross-platform-setup.md section 5)."
                .to_string(),
            command: None,
        },
        from_manifest: false,
    }
}

/// The venv executable names for whichever bin dir actually won. Both bootstrap
/// scripts pick the bin dir by which one EXISTS (see `bootstrap.sh`'s `VBIN`
/// assignment), so a `Scripts/` venv created under git-bash keeps working on a
/// POSIX host — the names have to follow that choice, not the host.
pub fn venv_exe_names(bin_dir: &str, facts: &BootstrapFacts) -> VenvLayout {
    if bin_dir == facts.venv_windows_bin_dir {
        venv_layout(true)
    } else {
        venv_layout(false)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The real `alp-sdk/metadata/bootstrap.json` (alp-sdk#917), vendored so
    /// the consumer is tested against the actual producer output.
    const REAL_MANIFEST: &str =
        include_str!("../../../../contract/fixtures/bootstrap/manifest.json");

    #[test]
    fn parses_every_field_of_the_real_manifest() {
        let facts = parse_bootstrap_manifest(REAL_MANIFEST).expect("real manifest must parse");
        assert_eq!(facts.zephyr_version, "v4.4.0");
        assert_eq!(
            facts.zephyr_requirements_path,
            "zephyr/scripts/requirements.txt"
        );
        assert_eq!(facts.venv_dir_name, ".venv");
        assert_eq!(facts.venv_posix_bin_dir, "bin");
        assert_eq!(facts.venv_windows_bin_dir, "Scripts");
        assert_eq!(facts.prerequisites_posix, ["git", "cmake", "python3"]);
        assert_eq!(
            facts.prerequisites_windows,
            ["git", "cmake", "python", "ninja"]
        );
        assert_eq!(facts.python_min_version, (3, 10));
        // The pin the coordinator's original brief got wrong: west IS pinned now.
        assert_eq!(facts.west_pip_spec, "west>=0.14.0");
        assert_eq!(facts.west_init_args, ["init", "-l"]);
        assert_eq!(
            facts.west_update_args,
            ["update", "--narrow", "-o=--depth=1"]
        );
        assert_eq!(facts.west_export_args, ["zephyr-export"]);
        assert_eq!(facts.west_extension_guard, "alp-migrate");
        assert_eq!(facts.pip_bootstrap_upgrade, ["pip", "wheel"]);
        assert_eq!(facts.pip_sdk_extras, ["jsonschema", "imgtool"]);
        assert_eq!(facts.pip_editable_install, "${SDK_ROOT}");
        assert_eq!(
            facts.env,
            vec![
                (
                    "ZEPHYR_BASE".to_string(),
                    "${WORKSPACE_DIR}/zephyr".to_string()
                ),
                ("ZEPHYR_TOOLCHAIN_VARIANT".to_string(), "zephyr".to_string()),
            ]
        );
        assert!(
            facts
                .hint_linux
                .command
                .as_deref()
                .unwrap()
                .starts_with("sudo apt-get")
        );
        assert_eq!(facts.hint_windows.command, None);
        assert!(facts.from_manifest);
    }

    #[test]
    fn the_fallback_matches_the_real_manifest_field_for_field() {
        // The whole point of the fallback: a legacy SDK must bootstrap the same
        // way the manifest describes. Any drift here is a stale transcription.
        let manifest = parse_bootstrap_manifest(REAL_MANIFEST).unwrap();
        let fallback = fallback_facts(manifest.python_min_version);
        assert_eq!(
            BootstrapFacts {
                from_manifest: true,
                ..fallback.clone()
            },
            manifest,
            "fallback constants have drifted from metadata/bootstrap.json"
        );
        assert!(!fallback.from_manifest);
    }

    #[test]
    fn an_unsupported_schema_version_is_a_hard_error_not_a_fallback() {
        // RFC #843: silently falling back to hand-ported behaviour on skew is
        // exactly the drift the contract exists to prevent.
        let doc = REAL_MANIFEST.replace("\"schemaVersion\": 1", "\"schemaVersion\": 2");
        let err = parse_bootstrap_manifest(&doc).unwrap_err();
        assert_eq!(err, BootstrapManifestError::UnsupportedSchemaVersion(2));
        assert!(err.to_string().contains("schemaVersion 2"), "{err}");
    }

    #[test]
    fn malformed_and_versionless_documents_report_why() {
        assert!(matches!(
            parse_bootstrap_manifest("{not json"),
            Err(BootstrapManifestError::Malformed(_))
        ));
        assert!(matches!(
            parse_bootstrap_manifest("{}"),
            Err(BootstrapManifestError::Malformed(_))
        ));
        // Right version, missing body -> malformed, not "unsupported".
        assert!(matches!(
            parse_bootstrap_manifest("{\"schemaVersion\": 1}"),
            Err(BootstrapManifestError::Malformed(_))
        ));
    }

    #[test]
    fn tokens_substitute_both_placeholders() {
        let tokens = Tokens {
            sdk_root: "/ws/alp-sdk",
            workspace_dir: "/ws",
        };
        assert_eq!(tokens.apply("${SDK_ROOT}"), "/ws/alp-sdk");
        assert_eq!(tokens.apply("${WORKSPACE_DIR}/zephyr"), "/ws/zephyr");
        assert_eq!(tokens.apply("zephyr"), "zephyr");
    }

    #[test]
    fn venv_exe_names_follow_the_bin_dir_that_won_not_the_host() {
        let facts = fallback_facts((3, 10));
        // A `Scripts/` venv (created under git-bash) resolved on a POSIX host
        // must still use the .exe names -- bootstrap.sh picks VBIN by which
        // directory exists, so the names have to follow that choice.
        assert_eq!(venv_exe_names("Scripts", &facts).python, "python.exe");
        assert_eq!(venv_exe_names("bin", &facts).west, "west");
    }
}
