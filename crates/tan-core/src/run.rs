// SPDX-License-Identifier: Apache-2.0
//! Pure post-build decision logic for `tan run` — decide, once the build step
//! has finished, whether to execute the produced `native_sim` binary (a host
//! target) or flash a hardware target. No IO: the subprocess spawn + the
//! filesystem probe live in `tan-cli`'s `commands/run`.
//!
//! `run` never re-derives the build/flash engines — it orchestrates
//! `build::run_build` then, based on this decision, `native_sim` exec or the
//! native `flash` path. The host-vs-hardware discriminator is the presence of a
//! `native_sim` binary: a project whose `board.yaml` targets `native_sim`
//! produces one; a hardware project does not, so `run` flashes it. `--native`
//! (`force_native`) is a safety guard — it refuses to fall through to a flash
//! when the caller asked for a host run but none was produced.

use std::path::{Path, PathBuf};

/// The literal binary Zephyr's `native_sim` target produces — a fixed name on
/// every host OS (Zephyr's own native-target naming, not a Windows suffix),
/// matching the SDK build layout's `build / "zephyr" / "zephyr.exe"`.
pub const NATIVE_SIM_EXE: &str = "zephyr.exe";

/// Candidate build-dir names a `native_sim` core's output could land under,
/// checked in order: the SDK's legacy single-image layout (`native_sim`), then
/// the per-slice build-plan naming a `native_sim` core gets from `tan build`
/// (`<core_id>-<backend>`, i.e. `native_sim-zephyr`).
pub const NATIVE_SIM_BUILD_DIRS: [&str; 2] = ["native_sim", "native_sim-zephyr"];

/// What `tan run` does after its build step — the pure decision, decoupled from
/// the subprocess/filesystem work so it is unit-testable without a real build.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RunAction {
    /// The build failed — short-circuit: never run or flash, report the build.
    BuildFailed,
    /// A host target — execute the produced `native_sim` binary.
    ExecuteNative,
    /// A hardware target — flash the built image.
    Flash,
    /// `--native` was requested but the build produced no `native_sim` binary —
    /// report and stop, rather than silently flashing hardware.
    NoNativeBinary,
}

/// Decide what `tan run` does once the build step finished.
///
/// - build failed             -> [`RunAction::BuildFailed`] (never run/flash)
/// - a `native_sim` binary     -> [`RunAction::ExecuteNative`] (host target)
/// - `--native`, none produced -> [`RunAction::NoNativeBinary`] (do NOT flash)
/// - otherwise                 -> [`RunAction::Flash`] (hardware target)
pub fn decide_run_action(
    build_ok: bool,
    force_native: bool,
    native_sim_present: bool,
) -> RunAction {
    if !build_ok {
        return RunAction::BuildFailed;
    }
    if native_sim_present {
        return RunAction::ExecuteNative;
    }
    if force_native {
        return RunAction::NoNativeBinary;
    }
    RunAction::Flash
}

/// The candidate `native_sim` executable paths under a build base, in probe
/// order (see [`NATIVE_SIM_BUILD_DIRS`]). Pure — the caller does the `is_file`
/// probe; the first candidate that exists is the one to execute.
///
/// ponytail: assumes the SDK build layout `<base>/<dir>/zephyr/zephyr.exe`
/// (mirrors the reference `alp run`); a west build that nests its own `build/`
/// under the slice cwd would land the binary one level deeper — extend
/// `NATIVE_SIM_BUILD_DIRS` / the join if a plan ever ships that shape.
pub fn native_sim_exe_candidates(base: &Path) -> Vec<PathBuf> {
    NATIVE_SIM_BUILD_DIRS
        .iter()
        .map(|dir| base.join(dir).join("zephyr").join(NATIVE_SIM_EXE))
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn build_failure_short_circuits_before_run_or_flash() {
        // A failed build never reaches native exec or flash, regardless of the
        // other inputs.
        assert_eq!(
            decide_run_action(false, false, false),
            RunAction::BuildFailed
        );
        assert_eq!(decide_run_action(false, true, true), RunAction::BuildFailed);
    }

    #[test]
    fn native_sim_binary_present_executes() {
        // A host target (native_sim binary produced) runs it — not a flash.
        assert_eq!(
            decide_run_action(true, false, true),
            RunAction::ExecuteNative
        );
        // `--native` and a produced binary agree: still execute.
        assert_eq!(
            decide_run_action(true, true, true),
            RunAction::ExecuteNative
        );
    }

    #[test]
    fn hardware_target_flashes() {
        // No native_sim binary and no `--native` guard -> hardware -> flash.
        assert_eq!(decide_run_action(true, false, false), RunAction::Flash);
    }

    #[test]
    fn native_flag_without_binary_refuses_to_flash() {
        // `--native` asked for a host run but the build produced none: report,
        // do NOT fall through to flashing hardware.
        assert_eq!(
            decide_run_action(true, true, false),
            RunAction::NoNativeBinary
        );
    }

    #[test]
    fn exe_candidates_cover_both_layouts_in_order() {
        let base = Path::new("build");
        let got = native_sim_exe_candidates(base);
        assert_eq!(got.len(), 2);
        assert_eq!(
            got[0],
            Path::new("build")
                .join("native_sim")
                .join("zephyr")
                .join("zephyr.exe")
        );
        assert_eq!(
            got[1],
            Path::new("build")
                .join("native_sim-zephyr")
                .join("zephyr")
                .join("zephyr.exe")
        );
    }
}
