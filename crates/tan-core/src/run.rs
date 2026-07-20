// SPDX-License-Identifier: Apache-2.0
//! Pure post-build decision logic for `tan run` — decide, once the build step
//! has finished, whether to execute the produced `native_sim` binary (a host
//! target), flash a hardware target, or stop at build-only. No IO: the
//! subprocess spawn + the filesystem probe live in `tan-cli`'s `commands/run`.
//!
//! `run` never re-derives the build/flash engines — it orchestrates
//! `build::run_build` then, based on this decision, `native_sim` exec or the
//! native `flash` path. The host-vs-hardware discriminator is the presence of a
//! runnable `native_sim` binary: a project whose `board.yaml` targets
//! `native_sim` produces one, located from the post-build `system-manifest.yaml`
//! (the same contract `flash`/`size`/`image` read). Flashing real silicon is the
//! dangerous path (the AEN wrong-runner history), so it is NEVER the default: a
//! hardware target flashes only on an explicit `--flash`; without it `run`
//! builds and reports, leaving the board untouched.

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
    /// A hardware target with an explicit `--flash` — flash the built image.
    Flash,
    /// A hardware target WITHOUT `--flash` — build succeeded, but programming the
    /// board needs explicit consent: report + stop, leave the hardware untouched.
    BuildOnly,
}

/// Decide what `tan run` does once the build step finished. Flashing hardware is
/// opt-in (`flash_requested`), never the default.
///
/// - build failed              -> [`RunAction::BuildFailed`] (never run/flash)
/// - a `native_sim` binary      -> [`RunAction::ExecuteNative`] (host target)
/// - hardware + `--flash`       -> [`RunAction::Flash`]
/// - hardware, no `--flash`     -> [`RunAction::BuildOnly`] (report; do NOT flash)
pub fn decide_run_action(
    build_ok: bool,
    native_sim_present: bool,
    flash_requested: bool,
) -> RunAction {
    if !build_ok {
        return RunAction::BuildFailed;
    }
    if native_sim_present {
        return RunAction::ExecuteNative;
    }
    if flash_requested {
        return RunAction::Flash;
    }
    RunAction::BuildOnly
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
        .find(|s| s.board.as_deref() == Some(NATIVE_SIM_BOARD))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::system_manifest::parse_system_manifest;

    #[test]
    fn build_failure_short_circuits_before_run_or_flash() {
        // A failed build never reaches native exec or flash, whatever the rest.
        assert_eq!(
            decide_run_action(false, false, false),
            RunAction::BuildFailed
        );
        assert_eq!(decide_run_action(false, true, true), RunAction::BuildFailed);
    }

    #[test]
    fn native_sim_binary_present_executes() {
        // A host target (native_sim binary produced) runs it — regardless of
        // the flash flag (native_sim is not flashable).
        assert_eq!(
            decide_run_action(true, true, false),
            RunAction::ExecuteNative
        );
        assert_eq!(
            decide_run_action(true, true, true),
            RunAction::ExecuteNative
        );
    }

    #[test]
    fn hardware_flashes_only_with_explicit_flag() {
        // No native_sim binary + `--flash` -> flash. This is the ONLY path that
        // programs hardware, and it demands the explicit opt-in.
        assert_eq!(decide_run_action(true, false, true), RunAction::Flash);
    }

    #[test]
    fn hardware_without_flash_is_build_only_never_flashes() {
        // The safety default: a bare `tan run` on a hardware project builds and
        // reports — it must NOT silently program the board.
        assert_eq!(decide_run_action(true, false, false), RunAction::BuildOnly);
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
}
