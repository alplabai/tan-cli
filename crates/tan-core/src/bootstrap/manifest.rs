// SPDX-License-Identifier: Apache-2.0
//! CONSUMER side of `alp-sdk/metadata/bootstrap.json` — the single source of
//! truth for the workspace-assembly FACTS that `scripts/bootstrap.sh`,
//! `scripts/bootstrap.ps1` and tan all need (alp-sdk#917). The manifest's own
//! `_comment` names tan as a real reader of those facts "since tan-cli PR #55"
//! rather than an INTENDED future consumer — `parse_bootstrap_manifest` below
//! IS that reader. `scripts/check_bootstrap_manifest.py` is the manifest's
//! drift gate, but that gate does NOT scan tan-cli, so hand-ported constants
//! here would desync silently. Reading the manifest is what keeps us honest;
//! the vendored fixture plus `tests/parity/bootstrap_manifest_parity.py` are
//! what catch the [`fallback_facts`] constants going stale behind it.
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

use std::collections::BTreeMap;

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
    /// Human-readable explanation, safe to print verbatim. An ARRAY of lines,
    /// not one paragraph: the schema's `minItems: 1` says so, and both scripts
    /// print one line per element (`for line in "${HINT_<OS>_NOTE[@]}"` /
    /// `foreach ($line in $HintWindowsNote)`) so an aligned package -> API
    /// mapping survives instead of collapsing into an unwrapped ~200-380 char
    /// line.
    pub note: Vec<String>,
    /// A copy-pasteable install command, or `None` where the OS has none.
    pub command: Option<String>,
}

/// A manual, one-time install step a script cannot automate for itself (a
/// GUI/EXE installer, or a step that only makes sense from an
/// already-assembled workspace) — distinct from [`NativeLibHint`], which is an
/// apt/brew/pkg-manager fact bootstrap CAN hand the user verbatim. No
/// `command` field: there is nothing copy-pasteable about "run this
/// installer" (`metadata/schemas/bootstrap-v1.schema.json`'s
/// `manualInstallHints.windows` has no `command` property at all).
#[derive(Debug, Clone, PartialEq, Eq, Deserialize)]
pub struct ManualInstallHint {
    /// Human-readable lines, safe to print verbatim.
    pub note: Vec<String>,
}

/// `manifest.json`'s `manualInstallHints` (alp-sdk#917 review item 7 moved the
/// Arm-GNU-Toolchain/Zephyr-SDK sentence OUT of `nativeLibHints.windows.note`,
/// where it used to print under an "OPTIONAL NATIVE LIBRARIES" heading it never
/// belonged under).
///
/// This said "Windows-only today … POSIX hosts have no manual-install fact, so
/// this doesn't grow a `linux`/`macos` field until one exists". **That stopped
/// being true at alp-sdk v0.14.0**, which added `manualInstallHints.posix.note`
/// — the POSIX counterpart, telling a Linux/macOS customer that the Zephyr SDK
/// is a separate manual `west sdk install`.
///
/// Both are read now (tan-cli#230). `posix` is OPTIONAL on the wire, and its
/// absence has to stay clean: every SDK before v0.14.0 declares only `windows`,
/// and a required field here would turn each of them into a hard
/// `ValidationFailure` that `tan build` inherits through auto-bootstrap — the
/// same trap `prerequisites.install` and `prerequisites.macos` already document.
#[derive(Debug, Clone, PartialEq, Eq, Deserialize)]
pub struct ManualInstallHints {
    /// `manualInstallHints.windows`.
    pub windows: ManualInstallHint,
    /// `manualInstallHints.posix`, or `None` when the manifest declares none
    /// (every SDK before alp-sdk v0.14.0). `None` renders nothing at all, which
    /// is exactly what those SDKs did before this field existed.
    #[serde(default)]
    pub posix: Option<ManualInstallHint>,
}

/// `prerequisites.install` — one shell install command per prerequisite tool,
/// per OS (alp-sdk#959, ADR 0021 Lane 1 P0b). THE source of what
/// `data.missingPrerequisites[].command` carries. tan used to keep its own
/// `winget` table beside this fact with nothing holding the two in step; the
/// manifest's `install` subtree is drift-gated on the producer side by
/// `check_bootstrap_manifest.py`'s `_check_install_commands`, a hardcoded table
/// here was gated by nothing at all.
///
/// Keyed `linux`/`macos`/`windows`, NOT the `posix`/`windows` split
/// `prerequisites` itself uses — an `apt-get`-shaped command and a `brew`-shaped
/// one cannot share one `posix` key without electing one distro's package
/// manager the de-facto POSIX standard, and the manifest's own schema says so at
/// length. That asymmetry is reconciled in exactly ONE place,
/// [`InstallCommands::for_host`], keyed off the HOST rather than off whichever
/// tool list was checked.
///
/// A MAP per OS, not a list: a tool with no command is the normal case (`west` is
/// pip-installed into the venv, `bitbake` is a whole host-package set) and must
/// reach the envelope as `command: null`. Prose in that field is a button that
/// fails.
#[derive(Debug, Clone, PartialEq, Eq, Default, Deserialize)]
pub struct InstallCommands {
    /// `prerequisites.install.linux`, keyed by tool name (the schema requires
    /// its keys to equal `prerequisites.posix`).
    #[serde(default)]
    pub linux: BTreeMap<String, String>,
    /// `prerequisites.install.macos` — keyed by `prerequisites.posix` too, since
    /// that is the one tool list both non-Windows hosts check.
    #[serde(default)]
    pub macos: BTreeMap<String, String>,
    /// `prerequisites.install.windows`, keyed by `prerequisites.windows`.
    #[serde(default)]
    pub windows: BTreeMap<String, String>,
}

/// What a host with no `install` entry resolves to. A `static` (`BTreeMap::new`
/// is `const`) so [`InstallCommands::for_host`] stays infallible and every
/// caller can look up unconditionally.
///
/// The host this serves is `HostOs::Other` — a POSIX host that is neither Linux
/// nor macOS (FreeBSD, illumos). The manifest has no entry for it and is not
/// going to grow one, so every tool there reports `command: null`, which is
/// exactly what every POSIX host reported before #959. The alternatives are both
/// worse than the `null`: a panic, or handing a BSD user a `brew install` line
/// resolved from the nearest OS key.
static NO_INSTALL_COMMANDS: BTreeMap<String, String> = BTreeMap::new();

impl InstallCommands {
    /// The tool -> command map for this host.
    ///
    /// THE one place the manifest's `linux`/`macos`/`windows` install keying is
    /// reconciled with `prerequisites`' `posix`/`windows` tool-list keying.
    /// Callers resolve once, by host, and hand the resolved map down — so no
    /// caller can look a tool up in the wrong OS's table (a `posix` refusal on
    /// macOS getting Linux's `apt-get` lines), and no second copy of this match
    /// exists to drift.
    pub fn for_host(&self, host: super::HostOs) -> &BTreeMap<String, String> {
        match host {
            super::HostOs::Linux => &self.linux,
            super::HostOs::MacOs => &self.macos,
            super::HostOs::Windows => &self.windows,
            super::HostOs::Other => &NO_INSTALL_COMMANDS,
        }
    }
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
    /// `prerequisites.macos`, or EMPTY when the manifest does not declare one
    /// (every SDK before v0.14.0). Empty means "fall back to
    /// [`prerequisites_posix`](BootstrapFacts::prerequisites_posix)" — see
    /// [`BootstrapFacts::prerequisites`].
    pub prerequisites_macos: Vec<String>,
    /// `prerequisites.windows`.
    pub prerequisites_windows: Vec<String>,
    /// `prerequisites.pythonMinVersion`, as `(major, minor)`.
    pub python_min_version: (u32, u32),
    /// `prerequisites.install` — the per-OS install one-liner for each tool in
    /// the two lists above.
    pub install: InstallCommands,
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
    /// `manualInstallHints`.
    pub manual_install_hints: ManualInstallHints,
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

    /// The prerequisite tool list for this host. The lists genuinely differ and
    /// the manifest records that faithfully rather than unifying them — so does
    /// this.
    ///
    /// Takes the HOST, not `is_windows`, since alp-sdk v0.14.0. That release
    /// added `xz` and `wget` to `prerequisites.posix` AND added a separate
    /// `prerequisites.macos` that omits them. Keying off a bool would have
    /// handed macOS the POSIX list and made `tan bootstrap` refuse outright on a
    /// stock macOS host, which ships neither `wget` nor a standalone `xz` — a
    /// hard refusal on a supported host, introduced by a re-vendor, for tools
    /// the SDK itself does not ask macOS for.
    ///
    /// An EMPTY `prerequisites_macos` means the manifest declared none, which is
    /// every SDK before v0.14.0, and macOS then reads `posix` exactly as it
    /// always did. So the fallback is the old behaviour, not a guess.
    pub fn prerequisites(&self, host: super::HostOs) -> &[String] {
        match host {
            super::HostOs::Windows => &self.prerequisites_windows,
            super::HostOs::MacOs if !self.prerequisites_macos.is_empty() => {
                &self.prerequisites_macos
            }
            _ => &self.prerequisites_posix,
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
    manual_install_hints: ManualInstallHints,
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
    /// OPTIONAL, and its ABSENCE has to keep meaning "use `posix`" — see
    /// [`BootstrapFacts::prerequisites`] for why that is load-bearing rather
    /// than tidy. Arrived at alp-sdk v0.14.0 alongside `xz`/`wget` being added
    /// to `posix`; every SDK before that declares only `posix` for both
    /// POSIX hosts, and a required field here would turn each of them into a
    /// hard `ValidationFailure` that `tan build` inherits through
    /// auto-bootstrap, exactly as the `install` note below describes.
    #[serde(default)]
    macos: Vec<String>,
    python_min_version: String,
    /// OPTIONAL on the wire, deliberately. `install` arrived under alp-sdk#959
    /// with `schemaVersion` still `1` (the schema pins it `const: 1` and now
    /// lists `install` as required, so an `install`-less v1 manifest can only be
    /// a tree that predates #959). Absence must therefore be a clean `None`: a
    /// required field here would make every such tree a hard
    /// `ExitCode::ValidationFailure` that `tan build` and `tan run` inherit
    /// through auto-bootstrap.
    install: Option<InstallCommands>,
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
    // `venv.dirName` joins straight onto `workspace_dir` and the join's result
    // is later handed to `remove_dir_all` when a stale venv is recreated
    // (tan-cli#161) — an unvalidated `..`-bearing or absolute value would let
    // the manifest name an arbitrary removal/write target outside the
    // workspace. Reject the shape here, the one seam every consumer of
    // `venv_dir_name` reads through, rather than re-deriving the check at each
    // call site (see `tan_core::path_guard`'s own module doc).
    if !crate::path_guard::is_plain_relative(std::path::Path::new(&doc.venv.dir_name)) {
        return Err(BootstrapManifestError::Malformed(format!(
            "venv.dirName `{}` is not a plain relative path",
            doc.venv.dir_name
        )));
    }
    Ok(BootstrapFacts {
        zephyr_version: doc.zephyr.version,
        zephyr_requirements_path: doc.zephyr.requirements_path,
        venv_dir_name: doc.venv.dir_name,
        venv_posix_bin_dir: doc.venv.posix_bin_dir,
        venv_windows_bin_dir: doc.venv.windows_bin_dir,
        prerequisites_posix: doc.prerequisites.posix,
        prerequisites_macos: doc.prerequisites.macos,
        prerequisites_windows: doc.prerequisites.windows,
        python_min_version,
        install: resolve_install_commands(doc.prerequisites.install),
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
        manual_install_hints: doc.manual_install_hints,
        from_manifest: true,
    })
}

/// The install one-liners as `metadata/bootstrap.json` carries them, hand-ported
/// like every other constant in [`fallback_facts`] and pinned equal to the
/// vendored fixture by `the_fallback_matches_the_real_manifest_field_for_field`.
/// That test is the whole reason tan can read these from the manifest and still
/// serve an SDK that has none — which is why there is no longer a SECOND,
/// ungated `winget` table anywhere in this workspace.
///
/// Its own function because it has TWO callers: the whole-manifest fallback
/// below, and [`resolve_install_commands`]'s gap-fill for when the `install` key
/// is absent entirely (a manifest predating alp-sdk#959) or serves an OS with an
/// empty map. One transcription, one gate over it.
fn fallback_install_commands() -> InstallCommands {
    let map = |pairs: &[(&str, &str)]| -> BTreeMap<String, String> {
        pairs
            .iter()
            .map(|(tool, command)| ((*tool).to_string(), (*command).to_string()))
            .collect()
    };
    InstallCommands {
        linux: map(&[
            ("git", "sudo apt-get install -y git"),
            ("cmake", "sudo apt-get install -y cmake"),
            ("python3", "sudo apt-get install -y python3"),
            // Zephyr's default CMake generator on every host, so its absence
            // stops `west build` outright rather than degrading it. Transcribed
            // from alp-sdk's manifest (alp-sdk#971/#981, merged as d6fd3a18);
            // note the PACKAGE name differs from the binary name, which is
            // exactly why this lives in data rather than being guessed.
            ("ninja", "sudo apt-get install -y ninja-build"),
            // `xz`/`wget` joined `prerequisites.posix` at alp-sdk v0.14.0. Same
            // package-name-differs-from-binary-name point as `ninja` above:
            // the binary is `xz`, the package is `xz-utils`.
            ("xz", "sudo apt-get install -y xz-utils"),
            ("wget", "sudo apt-get install -y wget"),
        ]),
        macos: map(&[
            ("git", "brew install git"),
            ("cmake", "brew install cmake"),
            ("python3", "brew install python3"),
            ("ninja", "brew install ninja"),
            // Present even though `prerequisites.macos` does NOT list `xz`/`wget`
            // — the manifest declares these commands for macOS regardless, and
            // this table is byte-pinned to it. A user who needs them (a
            // POSIX-list code path, or an SDK predating `prerequisites.macos`)
            // gets the `brew` line rather than Linux's `apt-get`.
            ("xz", "brew install xz"),
            ("wget", "brew install wget"),
        ]),
        windows: map(&[
            ("git", "winget install -e --id Git.Git"),
            ("cmake", "winget install -e --id Kitware.CMake"),
            ("python", "winget install -e --id Python.Python.3.12"),
            ("ninja", "winget install -e --id Ninja-build.Ninja"),
        ]),
    }
}

/// `prerequisites.install` as parsed, with each EMPTY per-OS map replaced by
/// [`fallback_install_commands`]'s.
///
/// Plan wins / CLI fills gaps — the same rule tan applies to a build-plan key an
/// older producer omits. A manifest that predates alp-sdk#959 carries no
/// `install` at all, and reporting `command: null` for a Windows `ninja` whose
/// winget id tan has always known would be a straight regression against what it
/// printed before this contract existed. A manifest that DOES serve an OS wins
/// outright for it, per tool — including a tool it deliberately omits.
///
/// PER OS, not whole-subtree, because the two absences are indistinguishable
/// after serde: each map carries `#[serde(default)]`, so `install: {}` — or one
/// carrying `windows` alone — parses clean and an `Option::unwrap_or_else` over
/// the SUBTREE would hand the absent OSes empty maps. On Windows that is the real
/// pre-#959 loss: all four `winget` lines vanish, and
/// `windows_python_not_runnable`/`python_too_old` degrade to their command-less
/// prose. Emptiness is the signal because there is no other one — a served OS map
/// is never legitimately empty (the producer's schema requires its keys to equal
/// `prerequisites.<os>`, which is never an empty list).
///
/// Degrade, do not refuse. Every shape handled here is OUT of contract today —
/// the schema requires all three OS keys — and a `ValidationFailure` on a
/// manifest field would reach `tan build` and `tan run` through auto-bootstrap.
/// A stale command from tan's own drift-gated constants is strictly better than
/// bricking the build command over a key the producer cannot currently emit
/// wrong.
fn resolve_install_commands(declared: Option<InstallCommands>) -> InstallCommands {
    let fallback = fallback_install_commands();
    let Some(declared) = declared else {
        return fallback;
    };
    let fill = |declared: BTreeMap<String, String>, fallback: BTreeMap<String, String>| {
        if declared.is_empty() {
            fallback
        } else {
            declared
        }
    };
    InstallCommands {
        linux: fill(declared.linux, fallback.linux),
        macos: fill(declared.macos, fallback.macos),
        windows: fill(declared.windows, fallback.windows),
    }
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
        // `ninja` is POSIX too, not Windows-only. Zephyr picks Ninja as its
        // default CMake generator on every host, so a POSIX box without it
        // fails `west build` with a CMake error naming nothing useful. The
        // asymmetry here was the fallback half of alp-sdk#971/#981: the
        // manifest was fixed upstream (d6fd3a18) and the fixture re-vendored,
        // while THIS hand-ported copy -- the one a legacy SDK with no manifest
        // actually uses -- still said Windows-only.
        prerequisites_posix: owned(&["git", "cmake", "python3", "ninja", "xz", "wget"]),
        prerequisites_macos: owned(&["git", "cmake", "python3", "ninja"]),
        prerequisites_windows: owned(&["git", "cmake", "python", "ninja"]),
        python_min_version: min_python,
        install: fallback_install_commands(),
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
        // The note arrays are transcribed VERBATIM, including the intra-line
        // padding that aligns the `->` column: the manifest is what carries the
        // alignment, and re-wrapping it here would make the fallback print
        // differently from the manifest path for the same SDK.
        hint_linux: NativeLibHint {
            note: owned(&[
                "libmosquitto-dev  -> alp_mqtt_* (cleartext + TLS)",
                "libasound2-dev    -> alp_audio_*",
                "libssl-dev        -> alp_hash_* / alp_aead_* / alp_random_bytes",
            ]),
            command: Some(
                "sudo apt-get install -y libmosquitto-dev libasound2-dev libssl-dev pkg-config"
                    .to_string(),
            ),
        },
        hint_macos: NativeLibHint {
            note: owned(&[
                "Equivalents via Homebrew:",
                "mosquitto  -> alp_mqtt_* (cleartext + TLS)",
                "macOS uses CoreAudio rather than ALSA, so the Yocto audio backend doesn't apply \
                 on macOS hosts.",
                "OpenSSL ships with macOS.",
            ]),
            command: Some("brew install mosquitto pkg-config".to_string()),
        },
        hint_windows: NativeLibHint {
            note: owned(&[
                "Under Git Bash / MSYS2 the Yocto-side backends aren't intended to run -- the \
                 canonical use is WSL2 + Ubuntu with the linux command above; skip this step on \
                 native Windows.",
            ]),
            command: None,
        },
        // Moved OUT of hint_windows.note (alp-sdk#917 review item 7): that
        // sentence is a manual-install fact, not a native-library one, and
        // printing it there is exactly the "OPTIONAL NATIVE LIBRARIES"
        // mis-framing the review caught. Grew from that one terse sentence to
        // five elements in alp-sdk#961 (Arm-toolchain scoping, which folded the
        // installer URL + PATH tip in here) and #967 (dtc/gperf settled) --
        // which is what let `blocks::optional_libs_block` drop its hardcoded
        // Arm/Zephyr-SDK block for issue #82.
        manual_install_hints: ManualInstallHints {
            windows: ManualInstallHint {
                note: owned(&[
                    "The Zephyr SDK (`west sdk install`) is a separate, manual, one-time install \
                     on native Windows -- not auto-installed by bootstrap.ps1. It is the one \
                     every Zephyr-on-M customer needs: it provides the `arm-zephyr-eabi` cross \
                     toolchain the real-silicon build (`west build` / `west flash`) actually \
                     uses. Run it from your west workspace's top-level directory -- the alp-sdk \
                     checkout's parent directory -- after this script completes.",
                    "7-Zip must already be on PATH before running `west sdk install` on native \
                     Windows: west delegates .7z extraction to patoolib, which shells out to an \
                     external 7z/7za/7zr/7zz/7zzs/unar binary and has no pure-Python fallback.",
                    "The Zephyr SDK's native-Windows hosttools bundle ships neither `dtc` nor \
                     `gperf` (verified: `hosttools_windows-x86_64.7z`, sdk-ng v1.0.1, \
                     sha256-checked against upstream's own sha256.sum -- 1486 entries via `7z l`, \
                     zero dtc/gperf/device-tree matches -- while the equivalent Linux hosttools \
                     archive does ship `dtc`). Both are separate, manual installs on native \
                     Windows if you need them (see docs/cross-platform-setup.md); WARN-only in \
                     `alp doctor` (`_check_dtc` / `_check_gperf`) -- not required by \
                     bootstrap.ps1.",
                    "The Arm GNU Toolchain (`arm-none-eabi-gcc`) is a SEPARATE manual install, \
                     needed by three opt-in paths -- rebuilding the GD32 bridge firmware \
                     (custom-carrier bring-up or bridge recovery), building the CC3501E bridge \
                     firmware's silicon-free stub target (its production image builds with TI \
                     ticlang, not this toolchain), or hand-writing bare-metal firmware for a real \
                     M-class core -- most customers never touch any of them, since the GD32G553 \
                     ships pre-flashed by Alp Lab (rebuilding it is optional and fully open, see \
                     docs/gd32-bridge.md). Installer EXE: \
                     https://developer.arm.com/downloads/-/arm-gnu-toolchain-downloads (tick 'Add \
                     path to environment variable' during install).",
                    "native_sim / Yocto need WSL2 (docs/cross-platform-setup.md section 5).",
                ]),
            },
            posix: Some(ManualInstallHint {
                note: owned(&[
                    "The Zephyr SDK (`west sdk install`) is a separate, manual, one-time install on Linux/macOS -- not auto-installed by bootstrap.sh. It is the one every Zephyr-on-M customer needs: it provides the `arm-zephyr-eabi` cross toolchain the real-silicon build (`west build` / `west flash`) actually uses. Run it from your west workspace's top-level directory -- the alp-sdk checkout's parent directory -- after this script completes, e.g. `west sdk install --gnu-toolchains arm-zephyr-eabi --no-hosttools --install-dir \"$PWD/zephyr-sdk\"`; see docs/getting-started.md for the full one-liner and the `tan sdk switch` step that pins it per project.",
                    "The Arm GNU Toolchain (`arm-none-eabi-gcc`) is a SEPARATE manual install, needed by three opt-in paths -- rebuilding the GD32 bridge firmware (custom-carrier bring-up or bridge recovery), building the CC3501E bridge firmware's silicon-free stub target (its production image builds with TI ticlang, not this toolchain), or hand-writing bare-metal firmware for a real M-class core -- most customers never touch any of them, since the GD32G553 ships pre-flashed by Alp Lab (rebuilding it is optional and fully open, see docs/gd32-bridge.md). See docs/cross-platform-setup.md section 2.3 (Linux) / 3.4 (macOS) for the apt/brew/curl install.",
                    "`west sdk install` may print \"could not find a 'file' executable, falling back to guess mime type by file extension\" -- patool's extension-based fallback works fine without it; this is WARN-only, not a bootstrap.sh prerequisite. Install `file` (`apt-get install -y file` / it ships by default on macOS) only to silence the message.",
                ]),
            }),
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
        assert_eq!(facts.zephyr_version, "v4.4.1");
        assert_eq!(
            facts.zephyr_requirements_path,
            "zephyr/scripts/requirements.txt"
        );
        assert_eq!(facts.venv_dir_name, ".venv");
        assert_eq!(facts.venv_posix_bin_dir, "bin");
        assert_eq!(facts.venv_windows_bin_dir, "Scripts");
        // `ninja` joined the POSIX list in alp-sdk#971/#981 (d6fd3a18), then
        // `xz`/`wget` at v0.14.0; this fixture was re-vendored past both. The
        // expectations move because the upstream manifest moved, not to make the
        // suite green.
        assert_eq!(
            facts.prerequisites_posix,
            ["git", "cmake", "python3", "ninja", "xz", "wget"]
        );
        // v0.14.0 added a macOS-specific list that deliberately OMITS `xz` and
        // `wget` — stock macOS ships neither, and the SDK does not ask for them
        // there. Asserted separately from `posix` precisely because reading the
        // wrong one of the two makes `tan bootstrap` refuse on a supported host.
        assert_eq!(
            facts.prerequisites_macos,
            ["git", "cmake", "python3", "ninja"]
        );
        assert_eq!(
            facts.prerequisites_windows,
            ["git", "cmake", "python", "ninja"]
        );
        assert_eq!(facts.python_min_version, (3, 10));
        // alp-sdk#959: the install one-liners are a manifest fact now, keyed per
        // OS and per tool. Every tool in the two lists above has one, on all
        // three served OSes -- the producer's schema requires exactly that, and a
        // gap would silently become a `command: null` in the envelope.
        for (host, tools) in [
            (super::super::HostOs::Linux, &facts.prerequisites_posix),
            // macOS reads its OWN list now (v0.14.0), so this checks the list
            // that host actually gates on -- checking `posix` here would assert
            // install commands for tools macOS is never asked for.
            (super::super::HostOs::MacOs, &facts.prerequisites_macos),
            (super::super::HostOs::Windows, &facts.prerequisites_windows),
        ] {
            let commands = facts.install.for_host(host);
            for tool in tools {
                assert!(commands.contains_key(tool), "{host:?} {tool}");
            }
            // This used to also assert `commands.len() == tools.len()`, i.e. no
            // command without a matching prerequisite. v0.14.0 broke that for
            // macOS legitimately: `install.macos` carries `xz` and `wget` while
            // `prerequisites.macos` asks for neither, so the map is a SUPERSET of
            // the list (6 vs 4). Keeping the equality would force a choice
            // between deleting real commands and asserting a falsehood.
            //
            // The containment above is the invariant that matters. A MISSING
            // command becomes `command: null` in the envelope and costs a
            // customer their Fix button; a SPARE one is a remedy nobody is
            // currently sent to, which costs nothing. Linux and Windows are still
            // exact, asserted here, so the relaxation is scoped to the one host
            // it is true for rather than applied everywhere.
            if host != super::super::HostOs::MacOs {
                assert_eq!(
                    commands.len(),
                    tools.len(),
                    "{host:?} has a command with no matching prerequisite"
                );
            }
        }
        // Pin the superset explicitly, so the relaxation above cannot quietly
        // widen: exactly two spare macOS commands, exactly the two v0.14.0 added.
        assert_eq!(
            facts.install.macos.len(),
            facts.prerequisites_macos.len() + 2
        );
        for spare in ["xz", "wget"] {
            assert!(facts.install.macos.contains_key(spare), "{spare}");
            assert!(
                !facts.prerequisites_macos.iter().any(|t| t == spare),
                "{spare}"
            );
        }
        assert_eq!(
            facts.install.linux.get("cmake").map(String::as_str),
            Some("sudo apt-get install -y cmake")
        );
        assert_eq!(
            facts.install.macos.get("cmake").map(String::as_str),
            Some("brew install cmake")
        );
        assert_eq!(
            facts.install.windows.get("ninja").map(String::as_str),
            Some("winget install -e --id Ninja-build.Ninja")
        );
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
        // #917 review item 7: this sentence lives in `manualInstallHints` now,
        // NOT in `nativeLibHints.windows.note` (which is asserted separately
        // below to prove the two fields didn't just merge back together).
        assert_eq!(
            facts.hint_windows.note,
            vec![
                "Under Git Bash / MSYS2 the Yocto-side backends aren't intended to run -- the \
                 canonical use is WSL2 + Ubuntu with the linux command above; skip this step on \
                 native Windows."
                    .to_string()
            ]
        );
        // Grew from one terse sentence to five elements in alp-sdk#961/#967.
        // Their exact bytes are already pinned twice over — by
        // `the_fallback_matches_the_real_manifest_field_for_field` below and by
        // `tests/parity/bootstrap_manifest_parity.py`'s byte-diff — so assert
        // the DISCRIMINATING parts here instead of a third 2 kB transcription.
        // The two checked below are precisely the facts that let
        // `blocks::optional_libs_block` drop its hardcoded block (issue #82):
        // the workspace locator #961 kept as prose, and the Arm installer URL
        // with its PATH tip.
        let manual = &facts.manual_install_hints.windows.note;
        assert_eq!(manual.len(), 5);
        assert!(
            manual[0].starts_with("The Zephyr SDK (`west sdk install`) is a separate, manual,"),
            "{}",
            manual[0]
        );
        assert!(
            manual[0].contains(
                "Run it from your west workspace's top-level directory -- the alp-sdk checkout's \
                 parent directory -- after this script completes."
            ),
            "{}",
            manual[0]
        );
        assert!(
            manual[3].contains(
                "https://developer.arm.com/downloads/-/arm-gnu-toolchain-downloads (tick 'Add \
                 path to environment variable' during install)."
            ),
            "{}",
            manual[3]
        );
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
    fn a_manifest_predating_the_install_key_parses_and_keeps_its_commands() {
        // Two facts in one, and both are load-bearing.
        //
        // `install` arrived under alp-sdk#959 at an UNCHANGED `schemaVersion: 1`,
        // so a tree checked out between #917 and #959 has a perfectly valid v1
        // manifest with no `install` key. Modelling the field as required would
        // make that tree a hard ValidationFailure -- which `tan build`/`tan run`
        // inherit through auto-bootstrap, i.e. a manifest field would brick the
        // build command.
        //
        // And absence gap-fills from the fallback constants rather than emptying
        // the map: dropping tan's long-standing `ninja -> winget …` on those
        // trees is the exact regression issue #90 refused to ship, and it is what
        // lets the hardcoded table be DELETED rather than kept as a second
        // source. A manifest that carries `install` still wins outright.
        let mut doc: serde_json::Value = serde_json::from_str(REAL_MANIFEST).unwrap();
        doc["prerequisites"]
            .as_object_mut()
            .unwrap()
            .remove("install")
            .expect("the fixture must carry `install` for this test to mean anything");
        let facts = parse_bootstrap_manifest(&doc.to_string())
            .expect("an install-less v1 manifest is legal, not a refusal");
        assert!(facts.from_manifest);
        assert_eq!(facts.install, fallback_facts((3, 10)).install);
        assert_eq!(
            facts.install.windows.get("ninja").map(String::as_str),
            Some("winget install -e --id Ninja-build.Ninja")
        );
    }

    #[test]
    fn an_install_object_that_serves_no_os_gap_fills_the_ones_it_skipped() {
        // The gap-fill is PER OS, not whole-subtree, and these two shapes are
        // why: every per-OS map carries `#[serde(default)]`, so `install: {}` and
        // a single-OS `install` both parse CLEAN, and an `Option`-level
        // `unwrap_or_else` over the subtree would leave the absent OSes EMPTY --
        // no gap-fill, no warning. On Windows that is the entire pre-#959 loss:
        // all four `winget` lines gone, and `windows_python_not_runnable` /
        // `python_too_old` degraded to their command-less prose.
        //
        // Both shapes are out of contract today (the producer's schema requires
        // all three OS keys), so this DEGRADES rather than refusing -- a
        // ValidationFailure here reaches `tan build`/`tan run` through
        // auto-bootstrap.
        let fallback = fallback_facts((3, 10)).install;

        // `install: {}` — every OS gap-fills, so nothing is lost against a
        // manifest with no `install` key at all.
        let mut doc: serde_json::Value = serde_json::from_str(REAL_MANIFEST).unwrap();
        doc["prerequisites"]["install"] = serde_json::json!({});
        let facts = parse_bootstrap_manifest(&doc.to_string())
            .expect("out of contract is not the same as unparseable");
        assert_eq!(facts.install, fallback);

        // One OS served: it wins OUTRIGHT (not topped up per tool -- a command
        // the producer deliberately omits must stay `null`), and only the two it
        // skipped gap-fill.
        let mut doc: serde_json::Value = serde_json::from_str(REAL_MANIFEST).unwrap();
        doc["prerequisites"]["install"] =
            serde_json::json!({"windows": {"ninja": "choco install ninja"}});
        let facts = parse_bootstrap_manifest(&doc.to_string())
            .expect("out of contract is not the same as unparseable");
        assert_eq!(
            facts.install.windows.get("ninja").map(String::as_str),
            Some("choco install ninja")
        );
        assert_eq!(facts.install.windows.get("git"), None);
        assert_eq!(facts.install.linux, fallback.linux);
        assert_eq!(facts.install.macos, fallback.macos);
    }

    #[test]
    fn install_commands_resolve_by_host_and_never_guess_for_an_unserved_one() {
        // The OS-key asymmetry: `prerequisites` is keyed posix/windows, `install`
        // linux/macos/windows. Resolving on the TOOL LIST's key would hand every
        // macOS user Linux's `apt-get` lines.
        let install = fallback_facts((3, 10)).install;
        assert_eq!(
            install
                .for_host(super::super::HostOs::Linux)
                .get("git")
                .map(String::as_str),
            Some("sudo apt-get install -y git")
        );
        assert_eq!(
            install
                .for_host(super::super::HostOs::MacOs)
                .get("git")
                .map(String::as_str),
            Some("brew install git")
        );
        assert_eq!(
            install
                .for_host(super::super::HostOs::Windows)
                .get("git")
                .map(String::as_str),
            Some("winget install -e --id Git.Git")
        );
        // A POSIX host that is neither Linux nor macOS has no manifest entry and
        // never will: empty, so every lookup is `None` -> `command: null`, the
        // same thing every POSIX host reported before #959. Not a panic, and not
        // the nearest OS's commands.
        assert!(install.for_host(super::super::HostOs::Other).is_empty());
        // A tool the manifest lists no command for is `None`, not prose.
        assert_eq!(
            install.for_host(super::super::HostOs::Windows).get("west"),
            None
        );
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
    fn a_venv_dir_name_that_escapes_the_workspace_is_rejected() {
        // tan-cli#161 review: `venv.dirName` joins onto `workspace_dir` and,
        // when a stale venv is found, the join's result is later fed to
        // `remove_dir_all`. A manifest value of `".."` would make that target
        // the west topdir's PARENT -- reject the shape before it ever reaches
        // a caller, rather than letting the removal decide.
        for bad in ["..", "../elsewhere", "/etc", "."] {
            let doc = REAL_MANIFEST.replace("\".venv\"", &format!("\"{bad}\""));
            let err = parse_bootstrap_manifest(&doc).unwrap_err();
            assert!(
                matches!(err, BootstrapManifestError::Malformed(ref m) if m.contains("venv.dirName")),
                "{bad}: {err}"
            );
        }
        // A nested-but-plain value stays accepted (dirName is not required to
        // be a single segment).
        let doc = REAL_MANIFEST.replace("\".venv\"", "\"tools/.venv\"");
        let facts = parse_bootstrap_manifest(&doc).expect("plain relative dirName must parse");
        assert_eq!(facts.venv_dir_name, "tools/.venv");
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
