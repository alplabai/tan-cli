// SPDX-License-Identifier: Apache-2.0
//! Host-environment doctor checks — the facts about the MACHINE that decide
//! whether a Zephyr toolchain can be provisioned on it at all, and whether the
//! builds it produces will fail for reasons that read as unrelated compiler
//! noise (alp-sdk ADR 0021, "Cross-cutting requirements").
//!
//! Pure, and every input is a parameter. The probes behind these facts — a
//! Windows registry read, `std::env::consts::ARCH`, a `USERPROFILE`/`HOME`
//! lookup — live in `tan-cli`'s `commands::doctor`, because they are IO and
//! because a host-conditional check tested only on whichever host CI happens to
//! run is not tested: on a clean `linux-x86_64` runner every branch below
//! except `Pass` would be dead code. Driving [`host_environment_checks`] with
//! all four hosts and both registry states is what makes them reachable.
//!
//! These land on PLAIN `tan doctor`, not `--build`, for the reason #81 settled
//! for `hostPrerequisites`: they are host facts needing no `board.yaml`, no
//! workspace and no SDK, and ADR 0021's Lane 1 P0a runs `tan doctor` *before*
//! anything project-shaped exists. `--build` is the project-shaped report and
//! its `BuildToolProbe` already answers a different question — "is the Zephyr
//! SDK installed *here*" — where this answers "can one be installed here at
//! all". Reporting either fact under two names is the trap #81 documents.

use crate::debug::{DoctorCheck, DoctorStatus};

/// The host platforms the pinned Zephyr SDK publishes a build for.
///
/// VERIFIED, not assumed. The SDK version is whatever the pinned Zephyr tree's
/// `SDK_VERSION` file says, which is `1.0.1` for both pins alp-sdk currently
/// carries (`west.yml` reads `zephyr: v4.4.0` on `main` and `v4.4.1` on `dev`)
/// — so the host set below is the same either way. The
/// `zephyrproject-rtos/sdk-ng` `v1.0.1` release publishes exactly four host
/// artifact families —
/// `hosttools_{linux-aarch64,linux-x86_64,macos-aarch64}.tar.xz` +
/// `hosttools_windows-x86_64.7z`, with `toolchain_gnu_*`, `toolchain_llvm_*`
/// and the `zephyr-sdk-1.0.1_*` bundles carrying the same four tokens and no
/// others.
///
/// Two absences matter and they are NOT the same absence:
///
/// * `windows-arm64` has never been published, at any release.
/// * `macos-x86_64` WAS published through `v0.17.4` and was dropped in
///   `v1.0.0`. Pinning back is not an escape — the pinned Zephyr requires
///   `1.0.1` — which is why [`zephyr_sdk_host_check`] gives an Intel Mac a
///   different remedy than a Windows-on-ARM host rather than the same one.
///
/// Spelled in the SDK's own tokens (`x86_64`, not `x64`), which are the same
/// spellings `std::env::consts::{OS, ARCH}` uses — so [`zephyr_sdk_host_tag`]
/// is a `-` join and not a translation table. That is a spelling coincidence
/// and nothing more: `ARCH` is the arch tan was COMPILED for, which is not the
/// machine's whenever tan is running emulated, and the caller must resolve the
/// real one ([`arch_for_image_file_machine`], [`arch_for_proc_translated`]).
pub const ZEPHYR_SDK_HOSTS: [&str; 4] = [
    "linux-aarch64",
    "linux-x86_64",
    "macos-aarch64",
    "windows-x86_64",
];

/// The Zephyr SDK release `west sdk install --version` needs, for the SAME
/// pin [`ZEPHYR_SDK_HOSTS`]'s doc comment verifies (`1.0.1`, current for both
/// of alp-sdk's `main`/`dev` Zephyr pins). One constant so a fix string naming
/// the version (tan-cli#160) and this doc's own claim about it cannot drift
/// apart silently.
///
/// The full command this feeds — `west sdk install --version 1.0.1 -t
/// arm-zephyr-eabi` — is what got the alp-sdk#855 fresh-host run past tan-cli#160
/// (its own words: *"I got past it with `west sdk install --version 1.0.1 -t
/// arm-zephyr-eabi` — knowledge tan never provided"*), and it is the same
/// command this repo's own "Live state" notes already record as e2e-verified
/// against a real west workspace. tan does not run it — `west sdk install`
/// downloads and extracts a toolchain, which is squarely `west`'s job once a
/// workspace exists, not a hidden side effect of a read-only `doctor` report —
/// it only prints the exact, pinned command precisely enough to act on.
pub const ZEPHYR_SDK_INSTALL_VERSION: &str = "1.0.1";

/// The already-probed host facts the checks below decide on. One struct rather
/// than four arguments, mirroring [`BuildToolProbe`](crate::BuildToolProbe):
/// the caller does the IO, this module does the judging.
#[derive(Debug, Clone, Copy)]
pub struct HostEnvProbe<'a> {
    /// `std::env::consts::OS` — `windows` / `macos` / `linux`.
    pub os: &'a str,
    /// `std::env::consts::ARCH` — `x86_64` / `aarch64`.
    pub arch: &'a str,
    /// Windows `HKLM\SYSTEM\CurrentControlSet\Control\FileSystem\LongPathsEnabled`.
    ///
    /// `Some(false)` covers BOTH an explicit `0` and an absent value, because
    /// absent IS the disabled default and reporting "unknown" for the most
    /// common Windows state would make the check useless. `None` means the read
    /// itself failed, and is only reachable on Windows — a non-Windows host
    /// carries `None` too, but never reaches the check (see below).
    pub long_paths_enabled: Option<bool>,
    /// The resolved home directory, or `None` when neither `USERPROFILE` nor
    /// `HOME` is set.
    pub home: Option<&'a str>,
}

/// `<os>-<arch>`, in the sdk-ng release-asset spelling.
pub fn zephyr_sdk_host_tag(os: &str, arch: &str) -> String {
    format!("{os}-{arch}")
}

// ── Resolving the MACHINE's arch, not the binary's ───────────────────────────
//
// `std::env::consts::ARCH` is a compile-time constant: it is the target the
// binary was built for. Both of tan's release assets make that the wrong
// answer on a real host.
//
// * `x86_64-pc-windows-msvc` on Windows-on-ARM. Windows emulates x64
//   transparently, so this is the LIKELIEST way tan runs there — and it would
//   report `windows-x86_64`, a served host, making the `windows-aarch64` arm
//   that motivates this whole check near-unreachable.
// * `x86_64-apple-darwin` under Rosetta on Apple silicon. That reports
//   `macos-x86_64` and fails a perfectly served machine, telling its owner to
//   go find a Linux box.
//
// Both are recoverable from the OS at runtime, and both mappings are pure, so
// they live here with the rest of the decisions rather than inside the
// `#[cfg]`-gated FFI where no test on any host could reach them.

/// Windows `IMAGE_FILE_MACHINE_AMD64` — the value `IsWow64Process2` reports in
/// `native_machine` on an x64 host.
pub const IMAGE_FILE_MACHINE_AMD64: u16 = 0x8664;

/// Windows `IMAGE_FILE_MACHINE_ARM64` — reported on a Windows-on-ARM host,
/// including when the calling process is an emulated x64 binary. This value IS
/// the check: it is the only way to tell that host apart from a native x64 one.
pub const IMAGE_FILE_MACHINE_ARM64: u16 = 0xAA64;

/// The arch token for a Windows `IsWow64Process2` `native_machine` value, or
/// `None` for a machine type tan has no token for (in which case the caller
/// keeps its compile-time fallback rather than inventing one).
pub fn arch_for_image_file_machine(native_machine: u16) -> Option<&'static str> {
    match native_machine {
        IMAGE_FILE_MACHINE_AMD64 => Some("x86_64"),
        IMAGE_FILE_MACHINE_ARM64 => Some("aarch64"),
        _ => None,
    }
}

/// The arch token for a macOS host, given `sysctl.proc_translated`.
///
/// `translated` is true only for an x86_64 binary running under Rosetta, which
/// exists on Apple silicon and nowhere else — so it is a direct statement that
/// the machine is `aarch64` however the binary was built. When it is false the
/// binary is running natively and the compile-time arch IS the machine's, which
/// is why an Intel Mac still reaches the `macos-x86_64` verdict it should.
pub fn arch_for_proc_translated(translated: bool, compiled_arch: &'static str) -> &'static str {
    if translated { "aarch64" } else { compiled_arch }
}

/// `ERROR_SUCCESS` from `winerror.h`. Declared here, not imported, so
/// [`classify_long_paths`] is reachable from a test on any host — the whole
/// point of splitting it out of the `#[cfg(windows)]` `unsafe` probe. `tan-cli`
/// static-asserts these against the real `windows-sys` values, so the two
/// cannot drift.
pub const WIN_ERROR_SUCCESS: u32 = 0;

/// `ERROR_FILE_NOT_FOUND` from `winerror.h`. See [`WIN_ERROR_SUCCESS`].
pub const WIN_ERROR_FILE_NOT_FOUND: u32 = 2;

/// The three-way verdict for a `RegGetValueW` read of `LongPathsEnabled`.
///
/// * success → the DWORD, as a bool.
/// * `ERROR_FILE_NOT_FOUND` → `Some(false)`. This covers an absent VALUE, which
///   is the Windows default-off state and by far the most common one, and also
///   an absent SUBKEY, which the API does not distinguish — verified against a
///   real host. Both are correctly "no long-path support to claim"; reporting
///   the common one as "could not read" would make the check useless on the
///   majority of Windows machines.
/// * anything else (an access denial, a value of the wrong type) → `None`, and
///   [`long_paths_check`] reports that as a `Warn` naming the uncertainty
///   rather than a blind `Pass`.
///
/// Pure and out here because it is ordinary branching that happened to be
/// sitting inside an `unsafe` `#[cfg(windows)]` function, where no test on any
/// host could reach it — leaving the exact three-way mapping this change argues
/// hardest about with zero coverage.
pub fn classify_long_paths(status: u32, data: u32) -> Option<bool> {
    match status {
        WIN_ERROR_SUCCESS => Some(data != 0),
        WIN_ERROR_FILE_NOT_FOUND => Some(false),
        _ => None,
    }
}

/// The three host-environment checks, hardest blocker first.
///
/// Order is load-bearing: `nextSteps` follows check order, so "this host has no
/// Zephyr SDK build at all" has to lead "your home path has a space in it" —
/// the second is irrelevant advice until the first is resolved.
///
/// `longPaths` is emitted on Windows ONLY. The registry value does not exist
/// elsewhere, and a check that renders "n/a" on two of three platforms is
/// noise; POSIX hosts have no `MAX_PATH` to cross.
pub fn host_environment_checks(probe: &HostEnvProbe) -> Vec<DoctorCheck> {
    let mut checks = vec![zephyr_sdk_host_check(probe.os, probe.arch)];
    if probe.os == "windows" {
        checks.push(long_paths_check(probe.long_paths_enabled));
    }
    checks.push(home_path_check(probe.home));
    checks
}

/// `zephyrSdkAvailableForHost` — does the pinned Zephyr SDK publish a host
/// build for this machine at all?
///
/// Named (tan-cli#160; was `zephyrSdkHost` through v0.4.0) so a `[+]` here
/// cannot be misread as "the toolchain is installed" — it answers a narrower
/// question, "COULD one be provisioned on this machine". The check that means
/// "IS one installed" is `--build`'s `zephyrSdk`, which is a hard `Fail` when
/// absent (#159/#166); the two must never share a name, or a `[+]` beside a
/// hard-failing `zephyrSdk` on the SAME report reads as a contradiction about
/// the very thing that cost the fresh-host run in alp-sdk#855 73 of its ~80
/// minutes.
///
/// **`Fail`, not `Warn`**, on every unserved host. This is the one check in the
/// group that means "the toolchain cannot run here", full stop: there is no
/// artifact to install, so `west sdk install` has nothing to fetch and no
/// amount of PATH or workspace fixing changes it. That is the same category as
/// a missing `ninja`, which `hostPrerequisites` already reports as `Fail`. The
/// remedy differs per host, so it is spelled per host rather than as one line.
pub fn zephyr_sdk_host_check(os: &str, arch: &str) -> DoctorCheck {
    let tag = zephyr_sdk_host_tag(os, arch);
    if ZEPHYR_SDK_HOSTS.contains(&tag.as_str()) {
        return DoctorCheck {
            name: "zephyrSdkAvailableForHost".to_string(),
            status: DoctorStatus::Pass,
            detail: format!("The Zephyr SDK publishes a host build for {tag}."),
            fix: None,
        };
    }

    let served = ZEPHYR_SDK_HOSTS.join(", ");
    let (detail, fix) = match tag.as_str() {
        // The ADR names this one explicitly and names its remedy: route to
        // WSL2-aarch64. A WSL2 distro on Windows-on-ARM is `linux-aarch64`,
        // which IS served — so this host has a first-class supported path, it
        // just is not the native one.
        "windows-aarch64" => (
            format!(
                "Windows on ARM ({tag}, `windows-arm64` in Zephyr's own naming): the Zephyr SDK \
                 has never published a host build for it. Served hosts are {served}. A native \
                 Windows build cannot be provisioned on this machine."
            ),
            "Build inside WSL2 instead: install a WSL2 Linux distribution (`wsl --install`), then \
             run `tan bootstrap` and `tan build` from inside it — a WSL2 distro on this hardware \
             is linux-aarch64, which the Zephyr SDK does publish."
                .to_string(),
        ),
        // NOT the same case, and deliberately not given the same remedy. There
        // is no WSL2 on macOS, and Rosetta translates x86_64 binaries FOR
        // Apple silicon, not aarch64 binaries for an Intel Mac — so the
        // macos-aarch64 artifact is unrunnable here rather than merely absent.
        "macos-x86_64" => (
            format!(
                "Intel Mac ({tag}): the Zephyr SDK published this host through 0.17.4 and dropped \
                 it in 1.0.0; the pinned SDK serves {served} only. macos-aarch64 is not a \
                 substitute — Rosetta translates x86_64 for Apple silicon, not the reverse — and \
                 macOS has no WSL2 equivalent to fall back to."
            ),
            "Build on a Linux host: a linux-x86_64 VM or container on this Mac, or a remote Linux \
             builder. Pinning an older Zephyr SDK is not an option — the pinned Zephyr requires \
             1.0.1, which is past the release that dropped macos-x86_64."
                .to_string(),
        ),
        _ => (
            format!("The Zephyr SDK publishes no host build for {tag}. Served hosts are {served}."),
            format!("Build on one of {served} — natively, or in a VM/container on this machine."),
        ),
    };

    DoctorCheck {
        name: "zephyrSdkAvailableForHost".to_string(),
        status: DoctorStatus::Fail,
        detail,
        fix: Some(fix),
    }
}

/// The registry path the [`long_paths_check`] verdict is about, named once so
/// the detail, the fix and the tan-cli probe cannot drift into three spellings.
pub const LONG_PATHS_KEY: &str = r"HKLM\SYSTEM\CurrentControlSet\Control\FileSystem";

/// `longPaths` — is Windows' `LongPathsEnabled` set?
///
/// **`Warn`, not `Fail`.** Windows 11 still ships this value off by default, so
/// `Fail` would exit 4 on essentially every stock Windows host — including the
/// many that build fine because their workspace sits at `C:\ws`. It is a
/// PROBABLE cause, not a proven blocker: whether a build crosses `MAX_PATH`
/// depends on how deep the workspace root already is. The check earns its place
/// by making the attribution possible at all — the failure it predicts arrives
/// as a CMake or compiler error about a file that plainly exists, which nobody
/// connects to a registry flag unaided.
///
/// `None` (the read failed) is also `Warn`, and says so rather than guessing.
/// A check that reports `Pass` for "I could not tell" is worse than no check.
pub fn long_paths_check(enabled: Option<bool>) -> DoctorCheck {
    let (status, detail, fix) = match enabled {
        Some(true) => (
            DoctorStatus::Pass,
            format!(r"Windows long paths are enabled ({LONG_PATHS_KEY}\LongPathsEnabled = 1)."),
            None,
        ),
        Some(false) => (
            DoctorStatus::Warn,
            // NOT a raw string, unlike the two arms around it: a `\`-newline
            // continuation is only honoured in a normal string literal, so a
            // raw one here would embed the backslash and the indentation
            // verbatim into the reported detail.
            format!(
                "Windows long paths are disabled ({LONG_PATHS_KEY}\\LongPathsEnabled is 0 or \
                 unset, the Windows default). A Zephyr build/ tree nests deep enough to cross the \
                 260-character MAX_PATH limit, and it surfaces as a CMake or compiler error about \
                 a file that exists."
            ),
            Some(enable_fix()),
        ),
        None => (
            DoctorStatus::Warn,
            format!(
                r"Could not read {LONG_PATHS_KEY}\LongPathsEnabled, so long-path support is unknown."
            ),
            Some(format!(
                r"Check it by hand: (Get-ItemProperty '{LONG_PATHS_KEY}').LongPathsEnabled — 1 is enabled."
            )),
        ),
    };
    DoctorCheck {
        name: "longPaths".to_string(),
        status,
        detail,
        fix,
    }
}

/// The elevated one-liner that sets `LongPathsEnabled`.
///
/// Elevation is unavoidable here — the value lives under `HKLM` — which is
/// exactly why this check is a `Warn` carrying a command rather than something
/// `--fix` would run. ADR 0021's Tier A promises zero elevation, and a doctor
/// that silently prompted for admin would break that promise; Tier B (prompt
/// once, skippable) is where this belongs, and Tier B is undecided.
fn enable_fix() -> String {
    format!(
        "Enable long paths in an ELEVATED PowerShell, then reopen your shell and VS Code so new \
         processes pick it up: New-ItemProperty -Path '{LONG_PATHS_KEY}' -Name LongPathsEnabled \
         -Value 1 -PropertyType DWORD -Force"
    )
}

/// `homePath` — does the home directory contain a space?
///
/// **`Warn`, not `Fail`.** A space in `C:\Users\Jane Doe` is a real historical
/// Zephyr breakage (unquoted paths through CMake/west/Kconfig), but most of the
/// chain quotes correctly now and plenty of hosts with a space build fine. It
/// is degraded-but-usable, which is what `Warn` means — and it is emphatically
/// not the same category as a host the toolchain cannot run on at all. Making
/// it `Fail` would exit 4 for every user whose Windows account is two words,
/// which is a large fraction of them.
///
/// All platforms, not Windows-only: a POSIX `/home/jane doe` breaks the same
/// way. Windows is merely where `%USERPROFILE%` is derived from a display name
/// and the user never chose it.
///
/// The detail names the ACTUAL path, because "your home has a space" without it
/// is a fact the user then has to go look up.
pub fn home_path_check(home: Option<&str>) -> DoctorCheck {
    let (status, detail, fix) = match home {
        Some(path) if path.contains(' ') => (
            DoctorStatus::Warn,
            format!(
                "Home directory contains a space: {path}. Zephyr's CMake/west/Kconfig chain has \
                 historically broken on unquoted paths, and a workspace created under it inherits \
                 the space."
            ),
            Some(
                "Create the workspace at a space-free path (e.g. C:\\alp or /opt/alp) and run tan \
                 from there with --project, rather than under the home directory."
                    .to_string(),
            ),
        ),
        Some(path) => (
            DoctorStatus::Pass,
            format!("Home directory has no spaces: {path}"),
            None,
        ),
        None => (
            DoctorStatus::Warn,
            "Could not resolve the home directory (neither USERPROFILE nor HOME is set)."
                .to_string(),
            Some(
                "Set HOME (or USERPROFILE on Windows) — tan resolves ~/.alp for the SDK cache and \
                 the global default-SDK pointer from it."
                    .to_string(),
            ),
        ),
    };
    DoctorCheck {
        name: "homePath".to_string(),
        status,
        detail,
        fix,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// A probe for a clean `linux-x86_64` host — the shape CI runs on, and
    /// therefore the ONE shape these tests must not be limited to.
    fn clean() -> HostEnvProbe<'static> {
        HostEnvProbe {
            os: "linux",
            arch: "x86_64",
            long_paths_enabled: None,
            home: Some("/home/jane"),
        }
    }

    /// The emitted check names, in order.
    fn check_names(probe: &HostEnvProbe) -> Vec<String> {
        host_environment_checks(probe)
            .into_iter()
            .map(|c| c.name)
            .collect()
    }

    #[test]
    fn every_served_host_passes_and_every_unserved_one_fails() {
        // The whole decision, driven over both sides. Deleting an entry from
        // `ZEPHYR_SDK_HOSTS`, or inverting the `contains` into a `!contains`,
        // has to fail here — the binary can only ever exercise the one row its
        // own host sits on.
        for tag in ZEPHYR_SDK_HOSTS {
            let (os, arch) = tag.split_once('-').expect("tag is os-arch");
            let check = zephyr_sdk_host_check(os, arch);
            assert_eq!(check.status, DoctorStatus::Pass, "{tag}");
            assert!(check.fix.is_none(), "{tag}");
            assert!(check.detail.contains(tag), "{}", check.detail);
        }
        for (os, arch) in [
            ("windows", "aarch64"),
            ("macos", "x86_64"),
            ("linux", "riscv64"),
        ] {
            let check = zephyr_sdk_host_check(os, arch);
            assert_eq!(check.status, DoctorStatus::Fail, "{os}-{arch}");
            assert!(check.fix.is_some(), "{os}-{arch}");
            assert_eq!(check.name, "zephyrSdkAvailableForHost");
        }
    }

    #[test]
    fn the_two_unserved_hosts_get_different_remedies() {
        // The correction this change exists to make: routing an Intel Mac to
        // WSL2 is advice that cannot be followed. Collapsing the two arms into
        // one shared message must fail HERE, and neither host is one CI runs
        // on, so nothing else would catch it.
        let win = zephyr_sdk_host_check("windows", "aarch64");
        let mac = zephyr_sdk_host_check("macos", "x86_64");
        let win_fix = win.fix.expect("windows-aarch64 is actionable");
        let mac_fix = mac.fix.expect("macos-x86_64 is actionable");

        assert!(win_fix.contains("WSL2"), "{win_fix}");
        assert!(win_fix.contains("linux-aarch64"), "{win_fix}");
        // Never the same string, and never the same route.
        assert_ne!(win_fix, mac_fix);
        assert!(
            !mac_fix.contains("WSL"),
            "macOS has no WSL2 to route to: {mac_fix}"
        );
        assert!(mac_fix.contains("Linux host"), "{mac_fix}");
        // Both messages name a VERIFIED fact about the release, not a vibe:
        // windows-arm64 never existed, macos-x86_64 was dropped in 1.0.0.
        assert!(win.detail.contains("never published"), "{}", win.detail);
        assert!(mac.detail.contains("0.17.4"), "{}", mac.detail);
        assert!(mac.detail.contains("1.0.0"), "{}", mac.detail);
    }

    #[test]
    fn the_host_set_is_the_verified_sdk_1_0_1_asset_list() {
        // Pinned as a literal so a "helpful" addition (macos-x86_64 back, or a
        // hopeful windows-arm64) is a deliberate edit against the release, not
        // a silent one. Sorted, so the joined `served` string in every message
        // is stable.
        assert_eq!(
            ZEPHYR_SDK_HOSTS,
            [
                "linux-aarch64",
                "linux-x86_64",
                "macos-aarch64",
                "windows-x86_64"
            ]
        );
        assert!(!ZEPHYR_SDK_HOSTS.contains(&"macos-x86_64"));
        assert!(!ZEPHYR_SDK_HOSTS.contains(&"windows-arm64"));
        assert!(!ZEPHYR_SDK_HOSTS.contains(&"windows-aarch64"));
        assert_eq!(zephyr_sdk_host_tag("windows", "x86_64"), "windows-x86_64");
    }

    #[test]
    fn long_paths_warns_when_disabled_and_when_unreadable_but_never_passes_blind() {
        let on = long_paths_check(Some(true));
        assert_eq!(on.status, DoctorStatus::Pass);
        assert!(on.fix.is_none());

        let off = long_paths_check(Some(false));
        assert_eq!(off.status, DoctorStatus::Warn);
        assert!(off.detail.contains("MAX_PATH"), "{}", off.detail);
        let fix = off.fix.clone().expect("a disabled flag is actionable");
        // The remedy has to be the runnable command AND say it needs elevation
        // — an unelevated `New-ItemProperty` on HKLM fails with an access
        // denial that reads like the command is wrong.
        assert!(fix.contains("ELEVATED"), "{fix}");
        assert!(fix.contains("New-ItemProperty"), "{fix}");
        assert!(fix.contains("LongPathsEnabled"), "{fix}");

        // "I could not tell" must never render as Pass. Swapping this arm to
        // Pass is the mutation that makes the whole check decorative.
        let unknown = long_paths_check(None);
        assert_eq!(unknown.status, DoctorStatus::Warn);
        assert!(unknown.detail.contains("unknown"), "{}", unknown.detail);
        assert!(unknown.fix.is_some());

        for check in [on, off, unknown] {
            assert_eq!(check.name, "longPaths");
            assert!(check.detail.contains(LONG_PATHS_KEY), "{}", check.detail);
        }
    }

    #[test]
    fn a_space_in_the_home_path_warns_and_names_the_path() {
        let spaced = home_path_check(Some(r"C:\Users\Jane Doe"));
        assert_eq!(spaced.status, DoctorStatus::Warn);
        // Reporting the ACTUAL path is the requirement; a generic "your home
        // has a space" leaves the user to go find out which one.
        assert!(
            spaced.detail.contains(r"C:\Users\Jane Doe"),
            "{}",
            spaced.detail
        );
        assert!(spaced.fix.is_some());

        // A space in home is NOT the same category as an unrunnable toolchain.
        assert_ne!(spaced.status, DoctorStatus::Fail);

        let clean = home_path_check(Some(r"C:\Users\jane"));
        assert_eq!(clean.status, DoctorStatus::Pass);
        assert!(clean.detail.contains(r"C:\Users\jane"));
        assert!(clean.fix.is_none());

        // POSIX too — this is not a Windows-only check, and hard-coding it to
        // one would leave `/home/jane doe` silently passing.
        assert_eq!(
            home_path_check(Some("/home/jane doe")).status,
            DoctorStatus::Warn
        );
        assert_eq!(
            home_path_check(Some("/home/jane")).status,
            DoctorStatus::Pass
        );

        let unset = home_path_check(None);
        assert_eq!(unset.status, DoctorStatus::Warn);
        assert!(unset.fix.is_some());
        for check in [spaced, clean, unset] {
            assert_eq!(check.name, "homePath");
        }
    }

    #[test]
    fn the_check_set_leads_with_the_hard_blocker_and_gates_long_paths_on_windows() {
        // `nextSteps` follows check order, so an unserved host has to lead a
        // home-path nit. Reordering the pushes must fail here.
        let unserved = HostEnvProbe {
            os: "macos",
            arch: "x86_64",
            home: Some("/Users/jane doe"),
            ..clean()
        };
        let names = check_names(&unserved);
        assert_eq!(names, ["zephyrSdkAvailableForHost", "homePath"]);

        // `longPaths` on Windows only: the registry value does not exist
        // elsewhere, so emitting it on Linux would be an invented verdict.
        // Deleting the `os == "windows"` guard shows up as a third name here.
        assert_eq!(
            host_environment_checks(&clean())
                .iter()
                .filter(|c| c.name == "longPaths")
                .count(),
            0
        );

        let windows = HostEnvProbe {
            os: "windows",
            arch: "x86_64",
            long_paths_enabled: Some(false),
            home: Some(r"C:\Users\jane"),
        };
        assert_eq!(
            check_names(&windows),
            ["zephyrSdkAvailableForHost", "longPaths", "homePath"]
        );
    }

    #[test]
    fn an_emulated_process_resolves_the_machines_arch_not_its_own() {
        // The defect these two mappings exist to prevent, driven from both
        // sides. tan ships x86_64-pc-windows-msvc AND x86_64-apple-darwin, so
        // `std::env::consts::ARCH` == "x86_64" is reachable on hardware that is
        // aarch64 in both OSes — and on Windows that is the LIKELIEST way tan
        // runs on ARM, since x64 emulation is transparent. Returning the
        // compile-time constant from either mapping fails here.
        assert_eq!(
            arch_for_image_file_machine(IMAGE_FILE_MACHINE_ARM64),
            Some("aarch64"),
            "an emulated x64 tan on Windows-on-ARM must report the machine"
        );
        assert_eq!(
            arch_for_image_file_machine(IMAGE_FILE_MACHINE_AMD64),
            Some("x86_64")
        );
        // A machine type tan has no token for is `None`, so the caller keeps
        // its fallback rather than inventing a tag that then fails a host for
        // being unserved when tan simply could not name it.
        assert_eq!(arch_for_image_file_machine(0x01c4), None);
        assert_eq!(IMAGE_FILE_MACHINE_ARM64, 0xAA64);
        assert_eq!(IMAGE_FILE_MACHINE_AMD64, 0x8664);

        // Rosetta runs x86_64 binaries on Apple silicon and nowhere else, so
        // "translated" IS a statement that the machine is aarch64.
        assert_eq!(arch_for_proc_translated(true, "x86_64"), "aarch64");
        // ... and a NATIVE x86_64 binary is a real Intel Mac, which must still
        // reach the macos-x86_64 verdict. Hard-coding "aarch64" to "fix" the
        // Rosetta case would silence the Intel-Mac check entirely.
        assert_eq!(arch_for_proc_translated(false, "x86_64"), "x86_64");
        assert_eq!(arch_for_proc_translated(false, "aarch64"), "aarch64");

        // End to end: the same host that passes when its arch is resolved
        // truthfully is the host that would FAIL on the compile-time constant.
        let real = HostEnvProbe {
            os: "macos",
            arch: arch_for_proc_translated(true, "x86_64"),
            ..clean()
        };
        assert_eq!(
            zephyr_sdk_host_check(real.os, real.arch).status,
            DoctorStatus::Pass
        );
        assert_eq!(
            zephyr_sdk_host_check("macos", "x86_64").status,
            DoctorStatus::Fail
        );
    }

    #[test]
    fn the_registry_status_maps_three_ways_and_never_guesses() {
        // Ordinary branching that used to sit inside the `#[cfg(windows)]`
        // `unsafe` probe, where no test on any host could reach it — so the
        // exact mapping this check argues hardest about had zero coverage and
        // a future "ACCESS_DENIED should count as disabled" edit would land
        // silently.
        assert_eq!(classify_long_paths(WIN_ERROR_SUCCESS, 1), Some(true));
        assert_eq!(classify_long_paths(WIN_ERROR_SUCCESS, 0), Some(false));
        // Any non-zero DWORD is enabled, not just 1.
        assert_eq!(classify_long_paths(WIN_ERROR_SUCCESS, 2), Some(true));
        // Absent value OR absent subkey — the API returns the same code for
        // both, and both mean there is no long-path support to claim.
        assert_eq!(
            classify_long_paths(WIN_ERROR_FILE_NOT_FOUND, 0),
            Some(false)
        );
        // ERROR_ACCESS_DENIED (5) and ERROR_INVALID_DATA (13): tan does not
        // know, and must say so rather than pass or fail blind. The `data`
        // buffer is untouched on these, so it must be IGNORED, not read.
        assert_eq!(classify_long_paths(5, 1), None);
        assert_eq!(classify_long_paths(13, 1), None);
        assert_eq!(WIN_ERROR_SUCCESS, 0);
        assert_eq!(WIN_ERROR_FILE_NOT_FOUND, 2);
    }

    #[test]
    fn a_fully_clean_host_reports_no_fix_at_all() {
        // The other direction: on a served host with a clean home and long
        // paths on, nothing here may manufacture a warning. An inverted
        // comparison anywhere above turns this green suite red.
        let probe = HostEnvProbe {
            os: "windows",
            arch: "x86_64",
            long_paths_enabled: Some(true),
            home: Some(r"C:\Users\jane"),
        };
        let checks = host_environment_checks(&probe);
        assert_eq!(checks.len(), 3);
        assert!(checks.iter().all(|c| c.status == DoctorStatus::Pass));
        assert!(checks.iter().all(|c| c.fix.is_none()));
    }
}
