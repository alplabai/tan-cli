// SPDX-License-Identifier: Apache-2.0
//! Which runtimes a project actually puts in play, and whether that makes
//! `tan bootstrap` refusable on this host.
//!
//! `board.yaml` does NOT carry an `os:` per core in practice (the shipped
//! scaffolds carry `som.sku` + `cores:` and nothing else) — the runtime resolves
//! from the SoM topology in the SDK metadata: a topology entry with `board:` is
//! a Zephyr (Cortex-M) target, one with `machine:` is a Yocto (Cortex-A) one,
//! and anything else falls back to the shared core-id heuristic. A per-core
//! `os:` in `board.yaml` is an OVERRIDE, and in practice is used to DISABLE
//! (`a32_cluster: {os: "off"}` in the edge-ai scaffold).

use std::collections::BTreeSet;

use crate::model::BoardModel;
use crate::sdk_catalogue::TopologyCore;

/// The per-core `os:` value that takes a core OUT of play entirely.
pub const OS_OFF: &str = "off";

/// Host operating system, as far as the bootstrap flow cares.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum HostOs {
    /// Linux — the only host that can run a Yocto build.
    Linux,
    /// macOS.
    MacOs,
    /// Native Windows (the `bootstrap.ps1` flow).
    Windows,
    /// Anything else POSIX-ish (FreeBSD, …).
    Other,
}

impl HostOs {
    /// Classify a `std::env::consts::OS` value. Taken as a parameter rather than
    /// read from `cfg!` so both branches stay testable from either host.
    pub fn detect(target_os: &str) -> HostOs {
        match target_os {
            "linux" => HostOs::Linux,
            "macos" => HostOs::MacOs,
            "windows" => HostOs::Windows,
            _ => HostOs::Other,
        }
    }
}

/// The runtime one SoM-topology entry naturally runs: a `board:` is a Zephyr
/// (Cortex-M) target, a `machine:` is a Yocto (Cortex-A) one; otherwise fall
/// back to the shared silicon-class heuristic. Single owner of that mapping —
/// `tan presets` reports it and `tan bootstrap` gates on it, so it must not be
/// copy-pasted into either.
pub fn runtime_for_topology_core(core: &TopologyCore) -> &'static str {
    match (core.board.is_some(), core.machine.is_some()) {
        (true, _) => "zephyr",
        (_, true) => "yocto",
        _ => crate::wizard::infer_runtime_for_core_id(&core.id),
    }
}

/// The distinct runtimes a project puts in play, sorted.
///
/// When `board.yaml` declares a `cores:` block, that block IS the project's
/// core selection: each entry resolves through its explicit `os:` override
/// (`"off"` removes the core), else the matching topology entry, else the
/// core-id heuristic. With no `cores:` block, a v1 top-level `os:` wins, and
/// failing that the whole SoM topology is in play.
///
/// `topology` is empty when the SoM metadata could not be read — the result
/// then leans on explicit `os:`/heuristics only, and an empty result means
/// "unresolvable", which every caller must treat as "proceed".
pub fn in_play_runtimes(board: &BoardModel, topology: &[TopologyCore]) -> Vec<String> {
    let from_topology = |id: &str| -> &'static str {
        topology
            .iter()
            .find(|t| t.id == id)
            .map(runtime_for_topology_core)
            .unwrap_or_else(|| crate::wizard::infer_runtime_for_core_id(id))
    };
    let declared = |value: &Option<String>| -> Option<String> {
        value
            .as_deref()
            .map(str::trim)
            .filter(|s| !s.is_empty())
            .map(str::to_string)
    };

    let mut out: BTreeSet<String> = BTreeSet::new();
    match board.cores.as_ref().filter(|c| !c.is_empty()) {
        Some(cores) => {
            for (id, entry) in cores {
                match declared(&entry.os) {
                    Some(os) if os == OS_OFF => continue,
                    Some(os) => out.insert(os),
                    None => out.insert(from_topology(id).to_string()),
                };
            }
        }
        None => match declared(&board.os).filter(|os| os != OS_OFF) {
            Some(os) => {
                out.insert(os);
            }
            None => {
                for core in topology {
                    out.insert(runtime_for_topology_core(core).to_string());
                }
            }
        },
    }
    out.into_iter().collect()
}

/// The host-compatibility verdict for the runtimes in play.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum YoctoGate {
    /// Proceed silently — Linux host, no Yocto core, or nothing resolvable.
    Clear,
    /// Proceed, but say so: a Yocto core is in play next to a Zephyr/baremetal
    /// one. Nothing bootstrap does is Yocto-specific (it is venv + west +
    /// Zephyr requirements), so the buildable cores still need it.
    Warn,
    /// Refuse: every core in play is Yocto and this host cannot build Yocto.
    Refuse,
}

/// Decide the gate. Refusal is deliberately narrow — only a project that is
/// *entirely* Yocto on a non-Linux host. Erring toward running is harmless
/// (bootstrap is idempotent); erring toward refusing bricks the command.
///
/// Note the refusal test is "every runtime in play is `yocto`" rather than
/// "none is `zephyr`/`baremetal`": an unrecognised `os:` string is an
/// unresolvable core, and unresolvable means proceed.
pub fn yocto_gate(runtimes: &[String], host: HostOs) -> YoctoGate {
    if host == HostOs::Linux || runtimes.is_empty() {
        return YoctoGate::Clear;
    }
    if runtimes.iter().all(|r| r == "yocto") {
        return YoctoGate::Refuse;
    }
    if runtimes.iter().any(|r| r == "yocto") {
        return YoctoGate::Warn;
    }
    YoctoGate::Clear
}

/// Refusal message for a Yocto-only project on a non-Linux host. Reuses
/// `doctor --build`'s `yoctoHost` wording verbatim so the two agree.
pub fn yocto_only_refusal() -> String {
    format!(
        "every core in this project targets Yocto. {} Re-run `tan bootstrap` inside WSL2 or on a \
         Linux host.",
        crate::build_readiness::YOCTO_HOST_DETAIL
    )
}

/// Advisory for a mixed board on a non-Linux host: the Zephyr/baremetal cores
/// bootstrap normally, the Yocto one will need a Linux host to build.
pub fn yocto_mixed_warning() -> String {
    format!(
        "a Yocto core is in play. {} The Zephyr/baremetal cores bootstrap normally here.",
        crate::build_readiness::YOCTO_HOST_DETAIL
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::model::CoreEntry;
    use std::collections::BTreeMap;

    fn topology_core(id: &str, board: Option<&str>, machine: Option<&str>) -> TopologyCore {
        TopologyCore {
            id: id.to_string(),
            app: None,
            image: None,
            machine: machine.map(str::to_string),
            board: board.map(str::to_string),
            toolchain: None,
        }
    }

    /// The E1M-V2N101 topology as shipped: an A55 Yocto cluster + an M33 Zephyr
    /// system manager.
    fn v2n_topology() -> Vec<TopologyCore> {
        vec![
            topology_core("a55_cluster", None, Some("e1m-v2n101-a55")),
            topology_core(
                "m33_sm",
                Some("alp_e1m_v2n101_m33_sm/r9a09g056n48gbg/cm33"),
                None,
            ),
        ]
    }

    fn board_with_cores(entries: &[(&str, Option<&str>)]) -> BoardModel {
        let mut cores = BTreeMap::new();
        for (id, os) in entries {
            cores.insert(
                (*id).to_string(),
                CoreEntry {
                    os: os.map(str::to_string),
                    ..CoreEntry::default()
                },
            );
        }
        BoardModel {
            cores: Some(cores),
            ..BoardModel::default()
        }
    }

    #[test]
    fn topology_board_is_zephyr_machine_is_yocto_else_heuristic() {
        assert_eq!(
            runtime_for_topology_core(&topology_core("m33_sm", Some("b"), None)),
            "zephyr"
        );
        assert_eq!(
            runtime_for_topology_core(&topology_core("a55_cluster", None, Some("m"))),
            "yocto"
        );
        // Neither key -> the shared core-id heuristic.
        assert_eq!(
            runtime_for_topology_core(&topology_core("a32_cluster", None, None)),
            "yocto"
        );
        assert_eq!(
            runtime_for_topology_core(&topology_core("m55_he", None, None)),
            "zephyr"
        );
    }

    #[test]
    fn declared_cores_resolve_through_the_topology() {
        let board = board_with_cores(&[("m33_sm", None)]);
        assert_eq!(in_play_runtimes(&board, &v2n_topology()), vec!["zephyr"]);
    }

    #[test]
    fn os_off_takes_a_core_out_of_play() {
        // The shipped edge-ai scaffold: `a55_cluster: {os: "off"}` + `m33_sm`.
        let board = board_with_cores(&[("a55_cluster", Some("off")), ("m33_sm", None)]);
        assert_eq!(in_play_runtimes(&board, &v2n_topology()), vec!["zephyr"]);
    }

    #[test]
    fn a_mixed_board_reports_both_runtimes() {
        let board = board_with_cores(&[("a55_cluster", None), ("m33_sm", None)]);
        assert_eq!(
            in_play_runtimes(&board, &v2n_topology()),
            vec!["yocto", "zephyr"]
        );
    }

    #[test]
    fn no_cores_block_falls_back_to_the_whole_topology_then_the_v1_os() {
        let bare = BoardModel::default();
        assert_eq!(
            in_play_runtimes(&bare, &v2n_topology()),
            vec!["yocto", "zephyr"]
        );
        // v1 top-level `os:` wins when there is no per-core block.
        let v1 = BoardModel {
            os: Some("baremetal".to_string()),
            ..BoardModel::default()
        };
        assert_eq!(in_play_runtimes(&v1, &v2n_topology()), vec!["baremetal"]);
        // Nothing resolvable at all -> empty (callers must proceed).
        assert!(in_play_runtimes(&bare, &[]).is_empty());
    }

    #[test]
    fn yocto_only_refuses_off_linux_and_never_on_linux() {
        let yocto_only = vec!["yocto".to_string()];
        assert_eq!(yocto_gate(&yocto_only, HostOs::Windows), YoctoGate::Refuse);
        assert_eq!(yocto_gate(&yocto_only, HostOs::MacOs), YoctoGate::Refuse);
        assert_eq!(yocto_gate(&yocto_only, HostOs::Other), YoctoGate::Refuse);
        assert_eq!(yocto_gate(&yocto_only, HostOs::Linux), YoctoGate::Clear);
    }

    #[test]
    fn mixed_warns_and_zephyr_only_is_clear() {
        let mixed = vec!["yocto".to_string(), "zephyr".to_string()];
        assert_eq!(yocto_gate(&mixed, HostOs::Windows), YoctoGate::Warn);
        assert_eq!(yocto_gate(&mixed, HostOs::Linux), YoctoGate::Clear);

        let zephyr = vec!["zephyr".to_string()];
        assert_eq!(yocto_gate(&zephyr, HostOs::Windows), YoctoGate::Clear);
        // Baremetal alongside Yocto is still buildable here -> warn, not refuse.
        let baremetal = vec!["baremetal".to_string(), "yocto".to_string()];
        assert_eq!(yocto_gate(&baremetal, HostOs::MacOs), YoctoGate::Warn);
    }

    #[test]
    fn unresolvable_projects_always_proceed() {
        assert_eq!(yocto_gate(&[], HostOs::Windows), YoctoGate::Clear);
        // An unrecognised `os:` is unresolvable, not a refusal.
        let odd = vec!["yocto".to_string(), "something-else".to_string()];
        assert_eq!(yocto_gate(&odd, HostOs::Windows), YoctoGate::Warn);
    }

    #[test]
    fn refusal_and_warning_reuse_the_doctor_wording() {
        assert!(yocto_only_refusal().contains(crate::build_readiness::YOCTO_HOST_DETAIL));
        assert!(yocto_only_refusal().contains("WSL2"));
        assert!(yocto_mixed_warning().contains(crate::build_readiness::YOCTO_HOST_DETAIL));
    }

    #[test]
    fn host_detect_maps_the_std_os_consts() {
        assert_eq!(HostOs::detect("linux"), HostOs::Linux);
        assert_eq!(HostOs::detect("macos"), HostOs::MacOs);
        assert_eq!(HostOs::detect("windows"), HostOs::Windows);
        assert_eq!(HostOs::detect("freebsd"), HostOs::Other);
    }
}
