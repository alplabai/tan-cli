// SPDX-License-Identifier: Apache-2.0
//! `tan image` — assemble a flashable-image bundle from `build/system-manifest.yaml`.
//! The IO/subprocess side of the port: tar+gzip each built slice's `build_dir`
//! into `image-bundle/slices/<core>-<os>.tar.gz`, copy each helper-MCU firmware
//! into `image-bundle/helper-mcus/`, and write `image-bundle/bundle-manifest.json`.
//! Every non-IO decision (bundle shape, key order, forward-slash artefact paths,
//! the ok-slice gate) is pure in `tan_core::image_bundle`.
//!
//! Faithful to the retired `west alp-image` / `scripts/.../alp_image.py`; under
//! ADR-0020 Phase 4 `tan` owns this, the Python is retired. Deliberate
//! divergences, all flagged in the port spec:
//!   - the positional `app_path` is optional and folds into tan's resolved
//!     workspace when omitted (`--project` works); an explicit positional wins.
//!   - the helper entry carries `chip` instead of the perpetually-null `role`.
//!   - artefact paths are forced forward-slash (the Python `\` on Windows was a
//!     latent cross-platform bug in the machine contract).
//!   - the vestigial `find_sdk_root()` gate is dropped — image needs no SDK.
//!   - an IO failure returns WriteFailure(3) (tan idiom) rather than unwinding as
//!     a Python traceback → rc 1.

use std::collections::HashSet;
use std::fs::File;
use std::io::{self, Read};
use std::path::{Path, PathBuf};

use flate2::Compression;
use flate2::write::GzEncoder;
use sha2::{Digest, Sha256};

use tan_core::image_bundle::{
    BUNDLE_DIR, BUNDLE_MANIFEST, BundleHelperEntry, BundleManifest, BundleSliceEntry, HELPERS_DIR,
    MANIFEST_FILE, SLICES_DIR, assemble_bundle_manifest, helper_artefact_rel, raw_passthrough,
    slice_archive_name, slice_artefact_rel, slice_should_bundle,
};
use tan_core::system_manifest::{HelperMcu, Slice, parse_system_manifest};

use super::CommandRun;
use crate::cli::{GlobalArgs, ImageArgs};
use crate::envelope::{Envelope, Issue, Project};
use crate::exit::ExitCode;
use crate::util::resolve_cli_project_context;

/// A bundle-assembly IO failure: the envelope issue code + message.
struct FailCtx {
    code: &'static str,
    message: String,
}

impl FailCtx {
    fn write(message: String) -> Self {
        FailCtx {
            code: "image.bundle-write-failed",
            message,
        }
    }
}

/// A non-fatal bundle-assembly notice: a slice or helper silently left OUT of
/// the bundle (missing build_dir, unbuildable archive name, missing firmware).
/// Carries its own issue code, unlike the plain `String` this replaced, so
/// `success_run` can promote it into a JSON `Issue` instead of only a text
/// line — see the fix at `success_run` for why that distinction matters.
struct Notice {
    code: &'static str,
    message: String,
}

/// `tan image` entry.
pub fn run(g: &GlobalArgs, args: &ImageArgs) -> CommandRun {
    let context = resolve_cli_project_context(g);
    let project = Project {
        root: context.workspace_root.clone(),
        board_yaml: context.board_yaml_path.clone(),
    };

    let cwd = std::env::current_dir().unwrap_or_else(|_| PathBuf::from("."));

    // App base: an explicit positional wins; else tan's resolved workspace
    // (which already folded in `--project`); else CWD.
    let app_base = match args.app_path.as_deref() {
        Some(p) => cwd.join(p),
        None => context
            .west_cwd
            .clone()
            .or_else(|| context.workspace_root.clone())
            .map(PathBuf::from)
            .unwrap_or_else(|| cwd.clone()),
    };

    // build_root: absolute as-is, relative against CWD; default `<app_base>/build`.
    let build_root = match args.build_root.as_deref() {
        Some(raw) => {
            let p = Path::new(raw);
            if p.is_absolute() {
                p.to_path_buf()
            } else {
                cwd.join(p)
            }
        }
        None => app_base.join("build"),
    };

    // Load + version-guard the manifest — faithful hard errors (exit 1).
    let manifest_path = build_root.join(MANIFEST_FILE);
    let yaml = match std::fs::read_to_string(&manifest_path) {
        Ok(y) => y,
        Err(_) => {
            return error_run(
                g,
                project,
                ExitCode::RuntimeFailure,
                "image.manifest-unavailable",
                format!(
                    "system-manifest.yaml not found at {}; run `tan build` first.",
                    manifest_path.display()
                ),
            );
        }
    };
    let manifest = match parse_system_manifest(&yaml) {
        Ok(m) => m,
        Err(e) => {
            return error_run(
                g,
                project,
                ExitCode::RuntimeFailure,
                "image.manifest-invalid",
                format!("{}: {e}", manifest_path.display()),
            );
        }
    };

    match assemble_bundle(&build_root, &manifest, &yaml) {
        Ok((notices, bundle, bundle_dir)) => success_run(g, project, notices, bundle, bundle_dir),
        Err(fail) => error_run(g, project, ExitCode::WriteFailure, fail.code, fail.message),
    }
}

/// Do the filesystem work: mkdir the bundle tree, tar each ok slice, copy each
/// present helper firmware, and write `bundle-manifest.json`. Returns the text
/// skip notices, the assembled manifest, and the bundle dir.
fn assemble_bundle(
    build_root: &Path,
    manifest: &tan_core::system_manifest::SystemManifest,
    yaml: &str,
) -> Result<(Vec<Notice>, BundleManifest, PathBuf), FailCtx> {
    let bundle_dir = build_root.join(BUNDLE_DIR);
    let slices_dir = bundle_dir.join(SLICES_DIR);
    let helpers_dir = bundle_dir.join(HELPERS_DIR);
    for dir in [&bundle_dir, &slices_dir, &helpers_dir] {
        std::fs::create_dir_all(dir)
            .map_err(|e| FailCtx::write(format!("mkdir {}: {e}", dir.display())))?;
    }

    let mut notices: Vec<Notice> = Vec::new();
    let mut slice_entries: Vec<BundleSliceEntry> = Vec::new();
    for slice in &manifest.slices {
        if !slice_should_bundle(&slice.status) {
            continue;
        }
        match bundle_slice(slice, build_root, &slices_dir)? {
            SliceOutcome::Bundled(entry) => slice_entries.push(entry),
            SliceOutcome::BuildDirMissing => notices.push(Notice {
                code: "image.slice-skipped",
                message: format!("image: skipping {} (build_dir missing)", slice.core_id),
            }),
            // core_id/os failed the shared-seam path guard in
            // `slice_archive_name` (e.g. `../` or `C:/` from a hand-edited or
            // third-party manifest) — skip rather than write outside the
            // bundle. See image_bundle::slice_archive_name for the guard.
            SliceOutcome::UnsafeName => notices.push(Notice {
                code: "image.slice-unsafe-name",
                message: format!(
                    "image: skipping {} (core_id/os is not a safe archive name)",
                    slice.core_id
                ),
            }),
        }
    }

    let mut helper_entries: Vec<BundleHelperEntry> = Vec::new();
    // Zephyr helper builds routinely share the same firmware basename
    // (`zephyr.bin`), so two helper_mcus entries commonly flatten to the same
    // `helpers_dir` destination — see `bundle_helper` for the collision fix
    // this set enables.
    let mut used_helper_names: HashSet<String> = HashSet::new();
    for helper in &manifest.helper_mcus {
        match bundle_helper(helper, build_root, &helpers_dir, &mut used_helper_names)? {
            HelperOutcome::Bundled(entry) => helper_entries.push(entry),
            HelperOutcome::NotFound(path) => notices.push(Notice {
                code: "image.helper-skipped",
                message: format!("image: helper-mcu firmware not found at {path}; skipping"),
            }),
            HelperOutcome::Absent => {}
        }
    }

    let (hw_info, boot_order) = raw_passthrough(yaml);
    let bundle = assemble_bundle_manifest(hw_info, boot_order, slice_entries, helper_entries);

    let json = serde_json::to_string_pretty(&bundle)
        .map_err(|e| FailCtx::write(format!("serialize bundle-manifest.json: {e}")))?;
    let manifest_out = bundle_dir.join(BUNDLE_MANIFEST);
    std::fs::write(&manifest_out, format!("{json}\n"))
        .map_err(|e| FailCtx::write(format!("write {}: {e}", manifest_out.display())))?;

    Ok((notices, bundle, bundle_dir))
}

/// The three `bundle_slice` outcomes: bundled, no candidate build_dir (a
/// faithful skip), or a core_id/os the shared path guard refused to turn into
/// an archive filename.
enum SliceOutcome {
    Bundled(BundleSliceEntry),
    BuildDirMissing,
    UnsafeName,
}

/// Tar+gzip one ok slice's build_dir into `slices/<core>-<os>.tar.gz`. Skips
/// (with a distinct outcome, not an IO error) when the slice's `build_dir` is
/// falsy/not a directory, or when `core_id`/`os` fail the archive-name guard.
fn bundle_slice(
    slice: &Slice,
    build_root: &Path,
    slices_dir: &Path,
) -> Result<SliceOutcome, FailCtx> {
    let build_dir = match slice.build_dir.as_deref().filter(|s| !s.is_empty()) {
        Some(bd) => {
            let p = Path::new(bd);
            if p.is_absolute() {
                p.to_path_buf()
            } else {
                build_root.join(p)
            }
        }
        None => return Ok(SliceOutcome::BuildDirMissing),
    };
    if !build_dir.is_dir() {
        return Ok(SliceOutcome::BuildDirMissing);
    }

    // `slice_archive_name` is the shared seam that rejects a `core_id`/`os`
    // shaped like `../../x` or `C:/x` — without this, `slices_dir.join(..)`
    // escaped the bundle dir and `tar_gzip_dir`'s `remove_file` unlinked an
    // arbitrary `*.tar.gz` outside the build tree. Skip rather than IO-error.
    let Some(archive_name) = slice_archive_name(&slice.core_id, &slice.os) else {
        return Ok(SliceOutcome::UnsafeName);
    };
    let archive = slices_dir.join(&archive_name);
    tar_gzip_dir(&build_dir, &archive)?;
    let (sha256, size) = sha256_and_size(&archive)?;
    Ok(SliceOutcome::Bundled(BundleSliceEntry {
        core_id: slice.core_id.clone(),
        os: slice.os.clone(),
        artefact: slice_artefact_rel(&slice.core_id, &slice.os)
            .expect("already validated by slice_archive_name above"),
        sha256,
        size,
    }))
}

/// The three faithful helper-firmware paths: copied, present-but-not-a-file
/// (skip WITH notice), or absent/empty (silent skip).
enum HelperOutcome {
    Bundled(BundleHelperEntry),
    NotFound(String),
    Absent,
}

/// Copy one helper's firmware into `helper-mcus/<basename>` when it is an
/// existing file. A missing/empty `firmware_path` is silently absent; a
/// present-but-nonexistent one (e.g. the literal `TBD`) is `NotFound`.
///
/// `used_names` tracks every destination basename already claimed in this
/// run: Zephyr's default build layout puts every helper's firmware at the
/// same basename (`zephyr.bin`), flattened from different `firmware_path`s
/// by `fw.file_name()`. Without this, the second helper's copy silently
/// overwrote the first's file on disk, and the first helper's already-hashed
/// `sha256`/`artefact` entry then pointed at bytes that were no longer there.
fn bundle_helper(
    helper: &HelperMcu,
    build_root: &Path,
    helpers_dir: &Path,
    used_names: &mut HashSet<String>,
) -> Result<HelperOutcome, FailCtx> {
    let raw = match helper.firmware_path.as_deref().filter(|s| !s.is_empty()) {
        Some(p) => p,
        None => return Ok(HelperOutcome::Absent),
    };
    // Absolute as-is, relative anchored to the build root (where the manifest lives).
    let fw = {
        let p = Path::new(raw);
        if p.is_absolute() {
            p.to_path_buf()
        } else {
            build_root.join(p)
        }
    };
    if !fw.is_file() {
        return Ok(HelperOutcome::NotFound(raw.to_string()));
    }
    let basename = fw
        .file_name()
        .map(|n| n.to_string_lossy().into_owned())
        .unwrap_or_else(|| "firmware.bin".to_string());
    // Disambiguate on collision (`2-zephyr.bin`, `3-zephyr.bin`, ...) instead
    // of silently overwriting the previous helper's copy.
    let mut dest_name = basename.clone();
    let mut n = 1u32;
    while used_names.contains(&dest_name) {
        n += 1;
        dest_name = format!("{n}-{basename}");
    }
    used_names.insert(dest_name.clone());

    let dst = helpers_dir.join(&dest_name);
    std::fs::copy(&fw, &dst)
        .map_err(|e| FailCtx::write(format!("copy {} -> {}: {e}", fw.display(), dst.display())))?;
    let (sha256, size) = sha256_and_size(&dst)?;
    Ok(HelperOutcome::Bundled(BundleHelperEntry {
        name: Some(helper.name.clone()),
        chip: Some(helper.chip.clone()),
        artefact: helper_artefact_rel(&dest_name),
        sha256,
        size,
    }))
}

/// Recursively tar+gzip `src_dir` into `dest` with the tar root arcname =
/// basename(src_dir). Overwrites (unlinks) any existing archive first.
fn tar_gzip_dir(src_dir: &Path, dest: &Path) -> Result<(), FailCtx> {
    if dest.exists() {
        std::fs::remove_file(dest)
            .map_err(|e| FailCtx::write(format!("unlink {}: {e}", dest.display())))?;
    }
    let arcname = src_dir
        .file_name()
        .map(|n| n.to_string_lossy().into_owned())
        .unwrap_or_else(|| "build".to_string());
    let file = File::create(dest)
        .map_err(|e| FailCtx::write(format!("create {}: {e}", dest.display())))?;
    let encoder = GzEncoder::new(file, Compression::default());
    let mut builder = tar::Builder::new(encoder);
    builder
        .append_dir_all(&arcname, src_dir)
        .map_err(|e| FailCtx::write(format!("tar {}: {e}", src_dir.display())))?;
    let encoder = builder
        .into_inner()
        .map_err(|e| FailCtx::write(format!("finish tar {}: {e}", dest.display())))?;
    encoder
        .finish()
        .map_err(|e| FailCtx::write(format!("gzip {}: {e}", dest.display())))?;
    Ok(())
}

/// Streaming lowercase-hex SHA-256 + byte length of a file (matches Python
/// `hashlib.sha256().hexdigest()` over the same bytes).
fn sha256_and_size(path: &Path) -> Result<(String, u64), FailCtx> {
    sha256_file(path)
        .and_then(|sha| {
            let size = std::fs::metadata(path)?.len();
            Ok((sha, size))
        })
        .map_err(|e| FailCtx::write(format!("hash {}: {e}", path.display())))
}

/// Streaming SHA-256 of a file's bytes, 65536-byte reads, lowercase hex.
fn sha256_file(path: &Path) -> io::Result<String> {
    let mut file = File::open(path)?;
    let mut hasher = Sha256::new();
    let mut buf = [0u8; 65536];
    loop {
        let n = file.read(&mut buf)?;
        if n == 0 {
            break;
        }
        hasher.update(&buf[..n]);
    }
    Ok(hasher
        .finalize()
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect())
}

/// Success `CommandRun`: text = skip notices + a ready line; JSON = the standard
/// envelope with the bundle manifest as `data`.
fn success_run(
    g: &GlobalArgs,
    project: Project,
    notices: Vec<Notice>,
    bundle: BundleManifest,
    bundle_dir: PathBuf,
) -> CommandRun {
    if g.is_json() {
        // Every notice here is an artefact silently left OUT of the bundle
        // (missing build_dir, unsafe core_id/os, missing helper firmware).
        // The old code hard-coded `Vec::new()` on this branch, so `--format
        // json` (what the vscode extension always uses) reported ok:true /
        // issues:[] for a bundle missing a slice or helper, with nothing in
        // the envelope distinguishing that from a complete bundle.
        let issues: Vec<Issue> = notices
            .iter()
            .map(|n| Issue {
                code: n.code.to_string(),
                severity: "warning".to_string(),
                message: n.message.clone(),
            })
            .collect();
        let json = Envelope::new("image", project, &bundle, issues, 0).to_json();
        CommandRun {
            exit: ExitCode::Success,
            text: Vec::new(),
            json: Some(json),
        }
    } else {
        let mut text: Vec<String> = notices.into_iter().map(|n| n.message).collect();
        text.push(format!("image: bundle ready at {}", bundle_dir.display()));
        CommandRun {
            exit: ExitCode::Success,
            text,
            json: None,
        }
    }
}

/// A hard error `CommandRun`, text or JSON per format. JSON carries an empty
/// bundle manifest as `data` so the envelope shape stays stable.
fn error_run(
    g: &GlobalArgs,
    project: Project,
    exit: ExitCode,
    code: &str,
    message: String,
) -> CommandRun {
    let issues = vec![Issue {
        code: code.to_string(),
        severity: "error".to_string(),
        message: message.clone(),
    }];
    if g.is_json() {
        let (hw_info, boot_order) = raw_passthrough("");
        let empty = assemble_bundle_manifest(hw_info, boot_order, Vec::new(), Vec::new());
        let json = Envelope::new("image", project, &empty, issues, exit.code()).to_json();
        CommandRun {
            exit,
            text: Vec::new(),
            json: Some(json),
        }
    } else {
        CommandRun {
            exit,
            text: vec![format!("image: {message}")],
            json: None,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::cli::Format;

    fn tmp(tag: &str) -> PathBuf {
        let d = std::env::temp_dir().join(format!("tan-image-{tag}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&d);
        std::fs::create_dir_all(&d).unwrap();
        d
    }

    fn global(format: Format) -> GlobalArgs {
        GlobalArgs {
            project: None,
            board_yaml: None,
            sdk_root: None,
            target: None,
            all: false,
            format,
            verbose: false,
            quiet: false,
            no_color: true,
            non_interactive: false,
            ci: false,
        }
    }

    fn image_args(build_root: &Path) -> ImageArgs {
        ImageArgs {
            app_path: None,
            build_root: Some(build_root.to_string_lossy().into_owned()),
        }
    }

    #[test]
    fn missing_manifest_exits_one() {
        let build_root = tmp("nomani"); // exists, no manifest
        let run = run(&global(Format::Text), &image_args(&build_root));
        assert_eq!(run.exit, ExitCode::RuntimeFailure);
        assert!(
            run.text
                .iter()
                .any(|l| l.contains("system-manifest.yaml not found"))
        );
        std::fs::remove_dir_all(&build_root).unwrap();
    }

    #[test]
    fn missing_manifest_json_carries_issue_code() {
        let build_root = tmp("nomani-json");
        let run = run(&global(Format::Json), &image_args(&build_root));
        assert_eq!(run.exit, ExitCode::RuntimeFailure);
        let doc: serde_json::Value = serde_json::from_str(run.json.as_deref().unwrap()).unwrap();
        assert_eq!(doc["command"], "image");
        assert_eq!(doc["ok"], false);
        assert_eq!(doc["issues"][0]["code"], "image.manifest-unavailable");
        std::fs::remove_dir_all(&build_root).unwrap();
    }

    #[test]
    fn all_pending_slices_yields_empty_bundle_rc_zero() {
        let build_root = tmp("allpending");
        std::fs::write(
            build_root.join("system-manifest.yaml"),
            "\
schema_version: 1
hw_info:
  sku: E1M-AEN701
slices:
- core_id: m55_hp
  os: zephyr
  build_dir: m55_hp-zephyr
  status: pending
helper_mcus:
- name: cc3501e_otp
  chip: cc3501e
  firmware_path: TBD
boot_order: []
",
        )
        .unwrap();
        let run = run(&global(Format::Json), &image_args(&build_root));
        assert_eq!(run.exit, ExitCode::Success);
        let doc: serde_json::Value = serde_json::from_str(run.json.as_deref().unwrap()).unwrap();
        assert_eq!(doc["data"]["schema_version"], 1);
        assert_eq!(doc["data"]["generated_by"], "tan image");
        assert_eq!(doc["data"]["slices"].as_array().unwrap().len(), 0);
        assert_eq!(doc["data"]["helper_mcus"].as_array().unwrap().len(), 0);
        // bundle-manifest.json was still written.
        assert!(
            build_root
                .join("image-bundle")
                .join("bundle-manifest.json")
                .is_file()
        );
        std::fs::remove_dir_all(&build_root).unwrap();
    }

    #[test]
    fn ok_slice_is_tarred_and_hashed() {
        let build_root = tmp("okslice");
        // A built slice with a real build_dir holding one file.
        let bd = build_root.join("m55_hp-zephyr");
        std::fs::create_dir_all(bd.join("zephyr")).unwrap();
        std::fs::write(bd.join("zephyr").join("zephyr.elf"), b"ELFDATA").unwrap();
        std::fs::write(
            build_root.join("system-manifest.yaml"),
            "\
schema_version: 1
hw_info:
  sku: E1M-AEN701
  eeprom:
    magic: keep
slices:
- core_id: m55_hp
  os: zephyr
  build_dir: m55_hp-zephyr
  status: ok
helper_mcus: []
boot_order: []
",
        )
        .unwrap();
        let run = run(&global(Format::Json), &image_args(&build_root));
        assert_eq!(run.exit, ExitCode::Success);
        let doc: serde_json::Value = serde_json::from_str(run.json.as_deref().unwrap()).unwrap();
        let slice = &doc["data"]["slices"][0];
        assert_eq!(slice["core_id"], "m55_hp");
        assert_eq!(slice["artefact"], "slices/m55_hp-zephyr.tar.gz");
        assert_eq!(slice["sha256"].as_str().unwrap().len(), 64);
        // hw_info additive key survived the raw passthrough.
        assert_eq!(doc["data"]["hw_info"]["eeprom"]["magic"], "keep");
        // The archive is on disk.
        assert!(
            build_root
                .join("image-bundle")
                .join("slices")
                .join("m55_hp-zephyr.tar.gz")
                .is_file()
        );
        std::fs::remove_dir_all(&build_root).unwrap();
    }

    #[test]
    fn ok_slice_missing_build_dir_gets_skip_notice() {
        let build_root = tmp("nobuilddir");
        std::fs::write(
            build_root.join("system-manifest.yaml"),
            "\
schema_version: 1
hw_info: {}
slices:
- core_id: m55_hp
  os: zephyr
  build_dir: does-not-exist
  status: ok
helper_mcus: []
boot_order: []
",
        )
        .unwrap();
        let run = run(&global(Format::Text), &image_args(&build_root));
        assert_eq!(run.exit, ExitCode::Success);
        assert!(
            run.text
                .iter()
                .any(|l| l.contains("skipping m55_hp") && l.contains("build_dir missing")),
            "{:?}",
            run.text
        );
        std::fs::remove_dir_all(&build_root).unwrap();
    }

    #[test]
    fn present_helper_firmware_is_copied_and_hashed() {
        let build_root = tmp("helperfw");
        std::fs::write(build_root.join("gd32_bridge.bin"), b"BRIDGEFW").unwrap();
        std::fs::write(
            build_root.join("system-manifest.yaml"),
            "\
schema_version: 1
hw_info: {}
slices: []
helper_mcus:
- name: gd32_bridge
  chip: gd32g553
  firmware_path: gd32_bridge.bin
boot_order: []
",
        )
        .unwrap();
        let run = run(&global(Format::Json), &image_args(&build_root));
        assert_eq!(run.exit, ExitCode::Success);
        let doc: serde_json::Value = serde_json::from_str(run.json.as_deref().unwrap()).unwrap();
        let helper = &doc["data"]["helper_mcus"][0];
        assert_eq!(helper["name"], "gd32_bridge");
        assert_eq!(helper["chip"], "gd32g553");
        assert_eq!(helper["artefact"], "helper-mcus/gd32_bridge.bin");
        assert_eq!(helper["size"], 8);
        assert!(
            build_root
                .join("image-bundle")
                .join("helper-mcus")
                .join("gd32_bridge.bin")
                .is_file()
        );
        std::fs::remove_dir_all(&build_root).unwrap();
    }

    #[test]
    fn ok_slice_missing_build_dir_gets_skip_issue_in_json() {
        let build_root = tmp("nobuilddir-json");
        std::fs::write(
            build_root.join("system-manifest.yaml"),
            "\
schema_version: 1
hw_info: {}
slices:
- core_id: m55_hp
  os: zephyr
  build_dir: does-not-exist
  status: ok
helper_mcus: []
boot_order: []
",
        )
        .unwrap();
        let run = run(&global(Format::Json), &image_args(&build_root));
        assert_eq!(run.exit, ExitCode::Success);
        let doc: serde_json::Value = serde_json::from_str(run.json.as_deref().unwrap()).unwrap();
        // ok:true is still correct -- a missing build_dir is a soft skip, not
        // a command failure -- but before the fix `issues` was hard-coded
        // empty on this branch, so an incomplete bundle was indistinguishable
        // from a complete one in the mode the vscode extension consumes.
        assert_eq!(doc["ok"], true);
        assert_eq!(doc["issues"][0]["code"], "image.slice-skipped");
        assert_eq!(doc["issues"][0]["severity"], "warning");
        std::fs::remove_dir_all(&build_root).unwrap();
    }

    #[test]
    fn unsafe_core_id_is_skipped_not_written_outside_bundle() {
        let build_root = tmp("unsafecore");
        let bd = build_root.join("bd");
        std::fs::create_dir_all(bd.join("zephyr")).unwrap();
        std::fs::write(bd.join("zephyr").join("zephyr.elf"), b"ELFDATA").unwrap();
        std::fs::write(
            build_root.join("system-manifest.yaml"),
            "\
schema_version: 1
hw_info: {}
slices:
- core_id: \"../../../../escape\"
  os: zephyr
  build_dir: bd
  status: ok
helper_mcus: []
boot_order: []
",
        )
        .unwrap();
        let run = run(&global(Format::Json), &image_args(&build_root));
        assert_eq!(run.exit, ExitCode::Success);
        let doc: serde_json::Value = serde_json::from_str(run.json.as_deref().unwrap()).unwrap();
        // Without the `slice_archive_name` guard this core_id would have
        // `.join()`ed past `image-bundle/slices/` and unlinked/overwritten an
        // arbitrary `*.tar.gz`. It must not be bundled at all...
        assert_eq!(doc["data"]["slices"].as_array().unwrap().len(), 0);
        // ...and the skip must be visible in the JSON envelope, not just text.
        assert_eq!(doc["issues"][0]["code"], "image.slice-unsafe-name");
        // Nothing was written into slices/ for it.
        let slices_dir = build_root.join("image-bundle").join("slices");
        assert_eq!(std::fs::read_dir(&slices_dir).unwrap().count(), 0);
        std::fs::remove_dir_all(&build_root).unwrap();
    }

    #[test]
    fn helper_basename_collision_does_not_overwrite() {
        let build_root = tmp("helpercollide");
        std::fs::create_dir_all(build_root.join("gd32")).unwrap();
        std::fs::create_dir_all(build_root.join("cc35")).unwrap();
        // Both helpers build to Zephyr's default `zephyr.bin` basename --
        // the collision this loses without the fix in `bundle_helper`.
        std::fs::write(build_root.join("gd32").join("zephyr.bin"), b"GD32FIRMWARE").unwrap();
        std::fs::write(
            build_root.join("cc35").join("zephyr.bin"),
            b"CC3501EFIRMWARE",
        )
        .unwrap();
        std::fs::write(
            build_root.join("system-manifest.yaml"),
            "\
schema_version: 1
hw_info: {}
slices: []
helper_mcus:
- name: gd32_bridge
  chip: gd32g553
  firmware_path: gd32/zephyr.bin
- name: cc3501e_otp
  chip: cc3501e
  firmware_path: cc35/zephyr.bin
boot_order: []
",
        )
        .unwrap();
        let run = run(&global(Format::Json), &image_args(&build_root));
        assert_eq!(run.exit, ExitCode::Success);
        let doc: serde_json::Value = serde_json::from_str(run.json.as_deref().unwrap()).unwrap();
        let helpers = doc["data"]["helper_mcus"].as_array().unwrap();
        assert_eq!(helpers.len(), 2);
        let artefacts: Vec<&str> = helpers
            .iter()
            .map(|h| h["artefact"].as_str().unwrap())
            .collect();
        // Distinct destinations -- the second helper no longer overwrites the first.
        assert_ne!(artefacts[0], artefacts[1], "{artefacts:?}");
        assert_ne!(helpers[0]["sha256"], helpers[1]["sha256"]);
        // Each recorded sha256 matches the bytes actually on disk at its own
        // artefact path (the pre-fix bug: the first entry's sha256 matched
        // nothing once the second copy overwrote it).
        let bundle_root = build_root.join("image-bundle");
        for h in helpers {
            let rel = h["artefact"].as_str().unwrap();
            let bytes = std::fs::read(bundle_root.join(rel)).unwrap();
            let mut hasher = Sha256::new();
            hasher.update(&bytes);
            let hex: String = hasher
                .finalize()
                .iter()
                .map(|b| format!("{b:02x}"))
                .collect();
            assert_eq!(h["sha256"], hex, "artefact {rel}");
        }
        std::fs::remove_dir_all(&build_root).unwrap();
    }
}
