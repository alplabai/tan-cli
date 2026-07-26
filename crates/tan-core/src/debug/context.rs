// SPDX-License-Identifier: Apache-2.0
//! Runtime-capability + workspace-context adapters: the host-tooling probe
//! structs and the `board.yaml`-existence workspace context fed to the doctor.

use serde::Serialize;

use crate::project::ProjectContext;

/// Which VS Code debugger extensions are installed in the host.
#[derive(Debug, Clone, Copy, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct DebuggerExtensionsState {
    /// `marus25.cortex-debug` is installed.
    pub cortex_debug: bool,
    /// `mcu-debug.peripheral-viewer` is installed.
    pub peripheral_viewer: bool,
    /// `mcu-debug.memory-view` is installed.
    pub memory_view: bool,
    /// `ms-vscode.cpptools` is installed.
    pub cpp_tools: bool,
    /// `vadimcn.vscode-lldb` is installed.
    #[serde(rename = "codeLLDB")]
    pub code_lldb: bool,
}

/// Probed availability of debug tooling on the host (PATH lookups).
///
/// No `python_available`: the retired `python` check was its only reader, and
/// `hostPrerequisites` now probes the interpreter off the manifest's
/// prerequisite list (with a `pythonMinVersion` floor the bare PATH lookup
/// never had).
#[derive(Debug, Clone)]
pub struct DebugRuntimeCapabilities {
    /// Resolved J-Link GDB-server executable name, if found.
    pub jlink_executable: Option<String>,
    /// Resolved OpenOCD executable name, if found.
    pub open_ocd_executable: Option<String>,
    /// Resolved pyOCD executable name, if found.
    pub pyocd_executable: Option<String>,
    /// Resolved GDB executable name, if found.
    pub gdb_executable: Option<String>,
    /// Resolved LLDB executable name, if found.
    pub lldb_executable: Option<String>,
}

/// Context fed to `build_doctor_report`. Serialized (camelCase) only inside the
/// support-bundle file; the doctor/inspect envelopes emit derived reports, not
/// the context itself.
#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct DebugWorkspaceContext {
    /// ISO-8601 timestamp the context was created.
    pub generated_at: String,
    /// Active workspace folder, if any.
    pub workspace_root: Option<String>,
    /// Resolved alp-sdk checkout root.
    pub sdk_root: Option<String>,
    /// Resolved `board.yaml` path.
    pub board_yaml_path: Option<String>,
    /// Working directory used for `west` commands.
    pub west_cwd: Option<String>,
    /// Python interpreter used for loader/validation scripts.
    pub python_binary: String,
    /// Whether `board.yaml` exists at the resolved path (probed).
    pub board_yaml_exists: bool,
    /// Installed-extension state for the host.
    pub debugger_extensions: DebuggerExtensionsState,
}

/// Mirror of TS `createDebugWorkspaceContext`: `board.yaml` existence is probed
/// via the injected predicate only when a path resolved.
pub fn create_debug_workspace_context(
    project: &ProjectContext,
    generated_at: String,
    board_yaml_exists: impl Fn(&str) -> bool,
    debugger_extensions: DebuggerExtensionsState,
) -> DebugWorkspaceContext {
    let exists = match &project.board_yaml_path {
        Some(path) => board_yaml_exists(path),
        None => false,
    };
    DebugWorkspaceContext {
        generated_at,
        workspace_root: project.workspace_root.clone(),
        sdk_root: project.sdk_root.clone(),
        board_yaml_path: project.board_yaml_path.clone(),
        west_cwd: project.west_cwd.clone(),
        python_binary: project.python_binary.clone(),
        board_yaml_exists: exists,
        debugger_extensions,
    }
}

/// Mirror of TS `collectRuntimeCapabilitiesFromCommands`.
///
/// Takes no `ProjectContext` any more: the only field it ever read was
/// `python_binary`, for the `python_available` flag the retired `python` check
/// consumed.
pub fn collect_runtime_capabilities_from_commands(
    command_on_path: impl Fn(&str) -> bool,
) -> DebugRuntimeCapabilities {
    let first_available = |commands: &[&str]| -> Option<String> {
        commands
            .iter()
            .find(|c| command_on_path(c))
            .map(|c| (*c).to_string())
    };

    DebugRuntimeCapabilities {
        jlink_executable: first_available(&["JLinkGDBServerCL", "JLinkGDBServer"]),
        open_ocd_executable: first_available(&["openocd"]),
        pyocd_executable: first_available(&["pyocd"]),
        gdb_executable: first_available(&["gdb", "arm-none-eabi-gdb"]),
        lldb_executable: first_available(&["lldb-dap", "lldb"]),
    }
}
