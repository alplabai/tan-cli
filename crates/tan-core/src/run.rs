// SPDX-License-Identifier: Apache-2.0
//! Pure post-build decision logic for `tan run` — decide, once the build step
//! has finished, whether to execute the produced `native_sim` binary (a host
//! target), flash a hardware target, or stop at build-only. No IO: the
//! subprocess spawn + the filesystem probe live in `tan-cli`'s `commands/run`.
//!
//! `run` never re-derives the build/flash engines — it orchestrates
//! `build::run_build_native_outcome` then, based on this decision, `native_sim`
//! exec or the native `flash` path. Flashing real silicon is the dangerous path
//! (the AEN wrong-runner history), so it is NEVER the default: a hardware
//! target flashes only on an explicit `--flash`; without it `run` builds and
//! reports, leaving the board untouched.
//!
//! **Root cause this module fixes (third attempt):** the host-vs-hardware
//! discriminator (`native_sim_target`) and the manifest-write outcome
//! (`manifest_written`) both come from the build `tan run` JUST RAN, in
//! memory — never from re-reading `system-manifest.yaml` off disk afterward.
//! `write_post_build_manifest` (`tan-cli`'s `build/execute/manifest.rs`) is a
//! best-effort file write that can fail independently of the SDK's
//! `--emit system-manifest` projection it fetched first (a Windows sharing
//! violation, a `create_dir_all` failure — not the emit itself), so an
//! EARLIER run's manifest (a different, possibly hardware, target) can still
//! be sitting in `build/` when THIS run's file write silently fails. Two
//! earlier attempts at this fix kept re-reading that file and then belted the
//! staleness with an mtime "freshness" check — mtime is leaky (a sub-2s
//! inter-invocation window slips through) and a no-`--flash` arm still read
//! (and could execute) a stale host binary regardless of freshness. Deciding
//! from the in-memory build outcome instead removes the stale-read
//! possibility entirely, rather than adding another belt on top of it.
//! `native_sim_target: Option<bool>` is `None` exactly when this run's build
//! could not establish a target at all (the SDK emit itself failed to
//! produce/parse) — treated as unconfirmed, not as "not native_sim".

use crate::system_manifest::{Slice, SystemManifest};

/// The literal binary Zephyr's `native_sim` target produces — a fixed name on
/// every host OS (Zephyr's own native-target naming, not a Windows suffix). It
/// sits beside `zephyr.elf` in the build's `zephyr/` output dir.
pub const NATIVE_SIM_EXE: &str = "zephyr.exe";

/// The Zephyr board target of a `native_sim` slice — the robust discriminator
/// for the host build (an actual `board` value from the manifest, not a
/// build-dir-name guess).
pub const NATIVE_SIM_BOARD: &str = "native_sim";

/// What `tan run` does after its build step — the pure decision, decoupled from
/// the subprocess/filesystem work so it is unit-testable without a real build.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RunAction {
    /// The build failed — short-circuit: never run or flash, report the build.
    BuildFailed,
    /// A host target — execute the produced `native_sim` binary.
    ExecuteNative,
    /// A hardware target with an explicit `--flash`, and THIS run's manifest
    /// write is confirmed to have succeeded — flash the built image.
    Flash,
    /// A hardware target WITHOUT `--flash` — build succeeded, but programming the
    /// board needs explicit consent: report + stop, leave the hardware untouched.
    BuildOnly,
    /// `--flash` was requested, but this run's OWN build outcome does not
    /// confirm it is safe to flash: either the target could not be
    /// established at all (`native_sim_target == None`, the SDK emit failed),
    /// or it is hardware but THIS run's manifest file write failed
    /// (`manifest_written == false`). Refuse rather than flash from a build
    /// that can't vouch for itself. Board untouched, same as `BuildOnly`, but
    /// reported as an error: the caller explicitly asked to flash and that
    /// request could not be honored safely.
    ManifestStale,
}

/// Decide what `tan run` does once the build step finished — entirely from
/// signals THIS build produced in memory, never from a post-hoc disk read (see
/// the module doc for why that used to be the actual defect).
///
/// - `native_sim_target` is `Some(true)`/`Some(false)` when this run's build
///   established a target (from the SDK's freshly-emitted system-manifest
///   projection, independent of whether the on-disk file write then
///   succeeded), or `None` when it could not (the emit itself failed/didn't
///   parse) — an unconfirmed target, not "not native_sim".
/// - `manifest_written` is whether THIS run's `system-manifest.yaml` file
///   write actually succeeded — the "write definitely succeeded" signal that
///   gates `--flash` for a confirmed hardware target (a failed write means
///   `flash` would next read a stale/wrong file).
///
/// Rules:
/// - build failed                                -> [`RunAction::BuildFailed`]
/// - host target (`Some(true)`)                   -> [`RunAction::ExecuteNative`]
///   (never `Flash`: native_sim is not flashable, regardless of `--flash`/
///   `manifest_written`)
/// - hardware (`Some(false)`) + `--flash` + write ok -> [`RunAction::Flash`]
/// - hardware (`Some(false)`) + `--flash`, write failed -> [`RunAction::ManifestStale`]
/// - hardware (`Some(false)`), no `--flash`          -> [`RunAction::BuildOnly`]
/// - unknown (`None`) + `--flash`                    -> [`RunAction::ManifestStale`] (refuse)
/// - unknown (`None`), no `--flash`                  -> [`RunAction::BuildOnly`]
pub fn decide_run_action(
    build_ok: bool,
    native_sim_target: Option<bool>,
    flash_requested: bool,
    manifest_written: bool,
) -> RunAction {
    if !build_ok {
        return RunAction::BuildFailed;
    }
    match native_sim_target {
        // A host target is never flashable — settled by THIS run's own build
        // outcome, so a failed/absent manifest FILE write can't flip a host
        // build into `Flash` (the actual A1 defect: an earlier attempt's
        // re-read of a stale on-disk manifest let exactly that happen).
        Some(true) => RunAction::ExecuteNative,
        Some(false) if flash_requested => {
            if manifest_written {
                RunAction::Flash
            } else {
                RunAction::ManifestStale
            }
        }
        Some(false) => RunAction::BuildOnly,
        // Unknown target: this run's build could not confirm host vs.
        // hardware at all. Flashing blind is never acceptable; a bare run
        // still gets to report + stop like any other hardware-shaped build.
        None if flash_requested => RunAction::ManifestStale,
        None => RunAction::BuildOnly,
    }
}

/// The `native_sim` slice in a post-build manifest, if any — identified by its
/// Zephyr board target [`NATIVE_SIM_BOARD`]. The runnable native_sim executable
/// is a sibling of this slice's built `zephyr.elf` (both land in the build's
/// `zephyr/` output dir), so the caller resolves the slice's `output_artefact`
/// through the shared artefact resolver and swaps the filename for
/// [`NATIVE_SIM_EXE`]. Pure: no IO.
pub fn native_sim_slice(manifest: &SystemManifest) -> Option<&Slice> {
    manifest
        .slices
        .iter()
        .find(|s| s.board.as_deref().is_some_and(is_native_sim_board))
}

/// True for the bare `native_sim` board AND Zephyr's qualified board form
/// (`native_sim/native/64`, SoC/variant-qualified). Exact-matching only the
/// bare name let a qualified board name defeat the host-vs-hardware
/// discriminator on a perfectly fresh manifest, routing a genuine host target
/// into `RunAction::Flash`. `"native_simulated_foo"` deliberately does NOT
/// match — the required `/` after the prefix anchors it to Zephyr's actual
/// board-qualifier syntax instead of an arbitrary substring.
fn is_native_sim_board(board: &str) -> bool {
    board == NATIVE_SIM_BOARD || board.starts_with("native_sim/")
}

/// Swap a native_sim slice's `output_artefact` for the runnable
/// [`NATIVE_SIM_EXE`] that sits beside it. Pure: no IO, nothing is probed.
///
/// A manifest NEVER records the `.exe`. `resolve_zephyr_artefact`
/// (`tan-cli`'s `build/execute/manifest.rs`) is the only writer of
/// `output_artefact` in tan — alp-sdk is planner/emit-only and writes none —
/// and it stores `<slice-cwd>/build/zephyr/zephyr.elf` unconditionally, for
/// every zephyr slice including native_sim. There is no `.exe` branch. So
/// every consumer that wants the host runnable has to make this swap, and
/// they must all make the SAME one: `tan run` carrying its own copy of the
/// rule while `tan debug-config` took the artefact verbatim is exactly how
/// #83 landed a `program` pointing at an ARM-less-but-unlaunchable
/// `zephyr.elf`. One function, both callers.
///
/// Splits on the separator the path itself uses rather than going through
/// `Path::with_file_name`, which on Windows rejoins with `\` and would turn a
/// `/`-authored manifest path into a mixed `a/b\zephyr.exe`.
pub fn native_sim_exe_beside(elf: &str) -> String {
    match elf.rfind(['/', '\\']) {
        Some(sep) => format!("{}{NATIVE_SIM_EXE}", &elf[..=sep]),
        None => NATIVE_SIM_EXE.to_string(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::system_manifest::parse_system_manifest;

    #[test]
    fn build_failure_short_circuits_before_run_or_flash() {
        // A failed build never reaches native exec or flash, whatever the rest.
        assert_eq!(
            decide_run_action(false, Some(false), false, true),
            RunAction::BuildFailed
        );
        assert_eq!(
            decide_run_action(false, Some(true), true, true),
            RunAction::BuildFailed
        );
        assert_eq!(
            decide_run_action(false, None, true, false),
            RunAction::BuildFailed
        );
    }

    #[test]
    fn native_sim_target_executes_regardless_of_flash_or_manifest_written() {
        // A host target (this run's build says native_sim) runs it —
        // regardless of the flash flag or whether the manifest FILE write
        // succeeded (native_sim is not flashable either way).
        assert_eq!(
            decide_run_action(true, Some(true), false, true),
            RunAction::ExecuteNative
        );
        assert_eq!(
            decide_run_action(true, Some(true), true, true),
            RunAction::ExecuteNative
        );
        assert_eq!(
            decide_run_action(true, Some(true), true, false),
            RunAction::ExecuteNative
        );
    }

    #[test]
    fn hardware_flashes_only_with_explicit_flag_and_confirmed_write() {
        // No native_sim target + `--flash` + a confirmed manifest write -> flash.
        // This is the ONLY path that programs hardware, and it demands both the
        // explicit opt-in AND proof this run's own write succeeded.
        assert_eq!(
            decide_run_action(true, Some(false), true, true),
            RunAction::Flash
        );
    }

    #[test]
    fn hardware_without_flash_is_build_only_regardless_of_manifest_written() {
        // The safety default: a bare `tan run` on a hardware project builds and
        // reports — it must NOT silently program the board. Manifest-write
        // success is irrelevant here: the dangerous action is flashing, not
        // building/reporting.
        assert_eq!(
            decide_run_action(true, Some(false), false, true),
            RunAction::BuildOnly
        );
        assert_eq!(
            decide_run_action(true, Some(false), false, false),
            RunAction::BuildOnly
        );
    }

    #[test]
    fn flash_refused_when_hardware_manifest_write_failed() {
        // A confirmed hardware target, but THIS run's manifest FILE write
        // failed: refuse rather than let `flash::run` read whatever an
        // EARLIER run (or nothing) left on disk.
        assert_eq!(
            decide_run_action(true, Some(false), true, false),
            RunAction::ManifestStale
        );
    }

    #[test]
    fn flash_refused_when_target_unknown() {
        // The SDK emit itself failed/didn't parse this run — no target signal
        // at all. `--flash` must refuse rather than guess; a bare run still
        // reports + stops like any other unconfirmed build.
        assert_eq!(
            decide_run_action(true, None, true, true),
            RunAction::ManifestStale
        );
        assert_eq!(
            decide_run_action(true, None, true, false),
            RunAction::ManifestStale
        );
    }

    #[test]
    fn build_only_when_target_unknown_and_no_flash() {
        assert_eq!(
            decide_run_action(true, None, false, true),
            RunAction::BuildOnly
        );
        assert_eq!(
            decide_run_action(true, None, false, false),
            RunAction::BuildOnly
        );
    }

    /// Root-cause regression (A1 — the third attempt's exact reachable
    /// failure): `board.yaml` edited to `native_sim`, an EARLIER hardware
    /// build's manifest still on disk, THIS run's native_sim build succeeds
    /// but its post-build manifest FILE write fails (`manifest_written =
    /// false`). `--flash` must never reach `RunAction::Flash` — the in-memory
    /// `native_sim_target` this run's build already established (`Some(true)`)
    /// settles it before `manifest_written` is even consulted. The two
    /// earlier (reverted) attempts re-read the stale on-disk hardware
    /// manifest here and routed this exact case to `Flash`.
    #[test]
    fn a1_native_sim_target_with_failed_manifest_write_never_flashes() {
        assert_ne!(
            decide_run_action(true, Some(true), true, false),
            RunAction::Flash
        );
        assert_eq!(
            decide_run_action(true, Some(true), true, false),
            RunAction::ExecuteNative
        );
    }

    /// Root-cause regression (A2): `board.yaml` edited to a hardware target,
    /// a stale native_sim manifest + `zephyr.exe` left on disk from an
    /// earlier run, bare `tan run` (no `--flash`). THIS run's build says
    /// hardware (`Some(false)`) — the decision must stop at `BuildOnly`,
    /// never `ExecuteNative`, whatever a stale on-disk file might still
    /// claim (this layer never reads one).
    #[test]
    fn a2_hardware_target_bare_run_never_executes_native() {
        assert_eq!(
            decide_run_action(true, Some(false), false, true),
            RunAction::BuildOnly
        );
        assert_ne!(
            decide_run_action(true, Some(false), false, true),
            RunAction::ExecuteNative
        );
    }

    const MANIFEST_NATIVE: &str = r#"
schema_version: 1
hw_info:
  sku: E1M-AEN701
slices:
- core_id: native_sim
  os: zephyr
  board: native_sim
  status: ok
  output_artefact: build/native_sim-zephyr/build/zephyr/zephyr.elf
  build_dir: build/native_sim-zephyr/build
ipc: []
helper_mcus: []
boot_order: []
"#;

    const MANIFEST_HARDWARE: &str = r#"
schema_version: 1
hw_info:
  sku: E1M-AEN701
slices:
- core_id: m55_hp
  os: zephyr
  board: alp_e1m_aen701_m55_hp
  status: ok
ipc: []
helper_mcus: []
boot_order: []
"#;

    #[test]
    fn native_sim_slice_found_by_board_target() {
        let m = parse_system_manifest(MANIFEST_NATIVE).unwrap();
        let slice = native_sim_slice(&m).expect("native_sim slice");
        assert_eq!(slice.core_id, "native_sim");
        assert_eq!(
            slice.output_artefact.as_deref(),
            Some("build/native_sim-zephyr/build/zephyr/zephyr.elf")
        );
    }

    #[test]
    fn native_sim_slice_absent_for_a_hardware_manifest() {
        let m = parse_system_manifest(MANIFEST_HARDWARE).unwrap();
        assert!(native_sim_slice(&m).is_none());
    }

    /// Fable-found residual: Zephyr's qualified board form (`native_sim/native/64`)
    /// must still be recognised as the host target on a perfectly fresh
    /// manifest — the old exact match defeated the discriminator entirely for
    /// this (real, Zephyr-native) board-name shape.
    #[test]
    fn native_sim_slice_found_for_qualified_board_form() {
        let manifest = "schema_version: 1\nhw_info:\n  sku: S\nslices:\n- core_id: \
                         native_sim\n  os: zephyr\n  board: native_sim/native/64\n  status: \
                         ok\nipc: []\nhelper_mcus: []\nboot_order: []\n";
        let m = parse_system_manifest(manifest).unwrap();
        assert!(native_sim_slice(&m).is_some());
    }

    /// A board name that merely starts with the `native_sim` text but isn't
    /// the real qualifier form must NOT match — the prefix check requires the
    /// `/` so it can't be defeated by an unrelated substring collision either.
    #[test]
    fn native_sim_slice_rejects_unrelated_prefix_collision() {
        let manifest = "schema_version: 1\nhw_info:\n  sku: S\nslices:\n- core_id: \
                         c1\n  os: zephyr\n  board: native_simulated_soc\n  status: \
                         ok\nipc: []\nhelper_mcus: []\nboot_order: []\n";
        let m = parse_system_manifest(manifest).unwrap();
        assert!(native_sim_slice(&m).is_none());
    }

    #[test]
    fn native_sim_exe_replaces_only_the_elf_filename() {
        // The shape `resolve_zephyr_artefact` actually writes.
        assert_eq!(
            native_sim_exe_beside("/ws/build/native_sim-zephyr/build/zephyr/zephyr.elf"),
            format!("/ws/build/native_sim-zephyr/build/zephyr/{NATIVE_SIM_EXE}")
        );
        // A bare filename has no directory to preserve; a path already naming
        // the exe is idempotent; a `\`-authored path keeps its own separator
        // instead of coming back mixed.
        assert_eq!(native_sim_exe_beside("zephyr.elf"), NATIVE_SIM_EXE);
        assert_eq!(native_sim_exe_beside("a/zephyr.exe"), "a/zephyr.exe");
        assert_eq!(
            native_sim_exe_beside(r"C:\ws\build\zephyr\zephyr.elf"),
            r"C:\ws\build\zephyr\zephyr.exe"
        );
    }
}
