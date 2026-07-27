// SPDX-License-Identifier: Apache-2.0
//! Debug launch-config generation — a port of TS `createDebugProfile` +
//! `debugProfileToLaunchDraft` + `createLaunchPreview` and the `launchJsonCore`
//! merge plan. Produces VS Code `launch.json` drafts for `alp debug-config`.
//!
//! Relies on `serde_json`'s `preserve_order` feature so emitted JSON keeps the
//! same key order as the TypeScript CLI.

use serde_json::{Value, json};

use crate::debug::{DebugServerKind, DebugTargetKind, is_server_supported_for_target};

fn server_label(server: DebugServerKind) -> &'static str {
    match server {
        DebugServerKind::Jlink => "J-Link",
        DebugServerKind::Openocd => "OpenOCD",
        DebugServerKind::Pyocd => "pyOCD",
        DebugServerKind::Gdbserver => "gdbserver",
        DebugServerKind::None => "local",
    }
}

/// Build the VS Code launch configuration draft for a target/server, mirroring
/// TS `createDebugProfile` → `debugProfileToLaunchDraft`. Errors (with the TS
/// message) when the server is not valid for the target.
///
/// `pre_launch_task` names the VS Code task to run before the session starts,
/// and is emitted ONLY when the caller supplies one. Every draft used to carry
/// a hardcoded `preLaunchTask` (`alp: build active target` and three siblings)
/// that **nothing in any of the three repos defines** — no `tasks.json`, no
/// `TaskProvider` registration in the extension. VS Code resolves
/// `preLaunchTask` before launching, fails to find the task, and aborts
/// pre-launch, so the session never starts: a `launch.json` that reads
/// perfectly and cannot run. Build-before-debug is still the behaviour we want,
/// which is why the capability survives as an opt-in rather than a deletion —
/// but it can only be the default once something actually provides the task,
/// and the NAME then belongs to whoever registered the provider, not to us.
pub fn create_launch_draft(
    target: DebugTargetKind,
    server: DebugServerKind,
    pre_launch_task: Option<&str>,
) -> Result<Value, String> {
    if !is_server_supported_for_target(target, server) {
        return Err(format!(
            "Unsupported debug backend '{}' for target '{}'.",
            server.as_str(),
            target.as_str()
        ));
    }

    let draft = match target {
        DebugTargetKind::ZephyrMcu => {
            let name = format!("ALP: Zephyr Debug ({})", server_label(server));
            match server {
                DebugServerKind::Openocd => json!({
                    "name": name,
                    "type": "cortex-debug",
                    "request": "launch",
                    "cwd": "${workspaceFolder}",
                    "executable": "${workspaceFolder}/build/app/zephyr/zephyr.elf",
                    "runToEntryPoint": "main",
                    "preLaunchTask": pre_launch_task,
                    "svdFile": "<resolved-svd>",
                    "svdPath": "<resolved-svd>",
                    "servertype": "openocd",
                    "configFiles": ["<resolved-openocd-board-cfg>"],
                }),
                DebugServerKind::Pyocd => json!({
                    "name": name,
                    "type": "cortex-debug",
                    "request": "launch",
                    "cwd": "${workspaceFolder}",
                    "executable": "${workspaceFolder}/build/app/zephyr/zephyr.elf",
                    "runToEntryPoint": "main",
                    "preLaunchTask": pre_launch_task,
                    "svdFile": "<resolved-svd>",
                    "svdPath": "<resolved-svd>",
                    "servertype": "pyocd",
                    "targetId": "<resolved-target-id>",
                }),
                _ => json!({
                    "name": name,
                    "type": "cortex-debug",
                    "request": "launch",
                    "cwd": "${workspaceFolder}",
                    "executable": "${workspaceFolder}/build/app/zephyr/zephyr.elf",
                    "runToEntryPoint": "main",
                    "preLaunchTask": pre_launch_task,
                    "svdFile": "<resolved-svd>",
                    "svdPath": "<resolved-svd>",
                    "servertype": "jlink",
                    "device": "<resolved-device>",
                    "interface": "swd",
                }),
            }
        }
        DebugTargetKind::BaremetalMcu => json!({
            "name": format!("ALP: Baremetal Debug ({})", server_label(server)),
            "type": "cortex-debug",
            "request": "launch",
            "servertype": server.as_str(),
            "cwd": "${workspaceFolder}",
            "executable": "${workspaceFolder}/build/baremetal/app.elf",
            "device": "<resolved-device>",
            "interface": "swd",
            "preLaunchTask": pre_launch_task,
            "svdFile": "<resolved-svd>",
            "svdPath": "<resolved-svd>",
        }),
        DebugTargetKind::YoctoUserspace => json!({
            "name": "ALP: Yocto Remote Debug",
            "type": "cppdbg",
            "request": "launch",
            "program": "${workspaceFolder}/build/yocto/app",
            "cwd": "${workspaceFolder}",
            "MIMode": "gdb",
            "miDebuggerServerAddress": "<host>:<port>",
            "miDebuggerPath": "<resolved-gdb>",
            "setupCommands": [{ "text": "-enable-pretty-printing" }],
            "preLaunchTask": pre_launch_task,
        }),
        DebugTargetKind::NativeHost => json!({
            "name": "ALP: Native Sim Debug",
            "type": "codelldb",
            "request": "launch",
            "program": "${workspaceFolder}/build/native_sim/zephyr/zephyr.exe",
            "cwd": "${workspaceFolder}",
            "preLaunchTask": pre_launch_task,
        }),
    };
    Ok(drop_absent_pre_launch_task(draft))
}

/// `json!` renders a `None` task name as `null`, and a literal
/// `"preLaunchTask": null` is worse than no key at all (VS Code's schema
/// rejects it). Drop the key instead — `shift_remove`, not `remove`, because
/// under `preserve_order` the latter is a swap-remove that would scramble the
/// key order the whole module exists to preserve.
fn drop_absent_pre_launch_task(mut draft: Value) -> Value {
    if let Some(map) = draft.as_object_mut() {
        if map.get("preLaunchTask") == Some(&Value::Null) {
            map.shift_remove("preLaunchTask");
        }
    }
    draft
}

/// What a real build knows about itself, filled in over the draft's
/// `<resolved-…>` placeholders (#66). Every field is optional: pre-build, or
/// against a Zephyr that reshaped `runners.yaml`, nothing resolves and the
/// draft keeps the placeholder it has always had.
#[derive(Debug, Default, Clone, PartialEq, Eq)]
pub struct LaunchResolution {
    /// The slice's real ELF — per-core, not the fixed `build/app/...` guess.
    pub executable: Option<String>,
    /// J-Link device name (`args.jlink --device`).
    pub device: Option<String>,
    /// The toolchain GDB the build was made against (`config.gdb`).
    pub gdb_path: Option<String>,
    /// The OpenOCD binary the build resolved (`config.openocd`).
    pub server_path: Option<String>,
    /// OpenOCD script search directories (`config.openocd_search`).
    pub search_dirs: Vec<String>,
    /// OpenOCD config files (`args.openocd --config`, all of them).
    pub config_files: Vec<String>,
    /// pyOCD target id (`args.pyocd --target`).
    pub target_id: Option<String>,
    /// SVD path, when one is ever resolvable. Nothing produces this yet —
    /// alp-sdk#948 has to ship the files first.
    pub svd: Option<String>,
}

impl LaunchResolution {
    /// Whether anything at all resolved — the caller uses this to decide
    /// between keeping the "still needs resolution" note and dropping it.
    pub fn is_empty(&self) -> bool {
        *self == Self::default()
    }
}

/// Overwrite a draft's placeholders with what [`LaunchResolution`] knows.
///
/// Only keys the draft already carries are replaced, so a target that never had
/// a `device` does not grow one; the two genuinely new keys (`gdbPath`, and
/// `serverpath`/`searchDir`) are inserted only when they resolved and only for
/// the adapter that understands them.
///
/// The `svdFile`/`svdPath` placeholders are REMOVED when no SVD resolved. A
/// missing key costs the peripheral view; a path that doesn't exist makes
/// cortex-debug fail on start, which is strictly worse than not offering the
/// view — and no SVD is resolvable today (alp-sdk#948).
pub fn apply_launch_resolution(draft: &mut Value, resolution: &LaunchResolution) {
    let Some(map) = draft.as_object_mut() else {
        return;
    };
    let is_cortex = map.get("type").and_then(Value::as_str) == Some("cortex-debug");

    // `executable` (cortex-debug) / `program` (cppdbg, codelldb) name the same
    // thing under different adapters; replace whichever this draft uses.
    if let Some(exe) = &resolution.executable {
        for key in ["executable", "program"] {
            if map.contains_key(key) {
                map.insert(key.to_string(), json!(exe));
            }
        }
    }
    if let Some(device) = &resolution.device {
        if map.contains_key("device") {
            map.insert("device".to_string(), json!(device));
        }
    }
    if let Some(target_id) = &resolution.target_id {
        if map.contains_key("targetId") {
            map.insert("targetId".to_string(), json!(target_id));
        }
    }
    if !resolution.config_files.is_empty() && map.contains_key("configFiles") {
        map.insert("configFiles".to_string(), json!(resolution.config_files));
    }
    if let Some(gdb) = &resolution.gdb_path {
        // cppdbg spells it `miDebuggerPath` and already carries the key;
        // cortex-debug's `gdbPath` is additive.
        if map.contains_key("miDebuggerPath") {
            map.insert("miDebuggerPath".to_string(), json!(gdb));
        } else if is_cortex {
            map.insert("gdbPath".to_string(), json!(gdb));
        }
    }
    if is_cortex && map.get("servertype").and_then(Value::as_str) == Some("openocd") {
        if let Some(server) = &resolution.server_path {
            map.insert("serverpath".to_string(), json!(server));
        }
        if !resolution.search_dirs.is_empty() {
            map.insert("searchDir".to_string(), json!(resolution.search_dirs));
        }
    }
    match &resolution.svd {
        Some(svd) => {
            for key in ["svdFile", "svdPath"] {
                if map.contains_key(key) {
                    map.insert(key.to_string(), json!(svd));
                }
            }
        }
        None => {
            for key in ["svdFile", "svdPath"] {
                if map.get(key).and_then(Value::as_str) == Some("<resolved-svd>") {
                    // `shift_remove`, not `remove`: under `preserve_order`
                    // the latter is a SWAP-remove, so dropping the two svd
                    // keys mid-object dragged the last two keys up into their
                    // slots. Every emitted zephyr-mcu config came out with
                    // `interface`/`device`/`servertype` in scrambled order —
                    // harmless to the debug adapter, but this module's stated
                    // contract is that the key order matches the TS CLI's.
                    map.shift_remove(key);
                }
            }
        }
    }
}

/// The static advisory notes attached to a launch preview (TS `createLaunchPreview`).
pub fn launch_preview_notes() -> Vec<String> {
    vec![
        "This is a draft launch configuration generated by tan.".to_string(),
        "Placeholder fields such as <resolved-device> still need project-specific resolution."
            .to_string(),
        "The long-term target is to resolve these values from the shared debug model.".to_string(),
    ]
}

/// The `launch.json`-shaped preview document: `{version, configurations:[draft]}`.
pub fn launch_preview_document(draft: Value) -> Value {
    json!({
        "version": "0.2.0",
        "configurations": [draft],
    })
}

/// Result of merging a draft into `launch.json`: the serialized document plus
/// whether a same-named configuration was overwritten.
#[derive(Debug)]
pub struct LaunchJsonWritePlan {
    /// Pretty-printed `launch.json` content, trailing newline included.
    pub content: String,
    /// `true` if an existing same-named configuration was replaced; `false` if appended.
    pub replaced: bool,
}

/// Merge `draft` into an existing `launch.json` (or a fresh document), merging
/// over any configuration with the same `name`. Mirrors TS
/// `createLaunchJsonWritePlan`.
///
/// It MERGES rather than replaces because this runs before every session while
/// the configuration names are fixed per target/server, so a wholesale replace
/// hands the user's own file back to them with their work undone. See
/// `merge_configuration` for the rule.
pub fn create_launch_json_write_plan(
    existing_content: Option<&str>,
    draft: &Value,
) -> Result<LaunchJsonWritePlan, String> {
    let mut document = parse_launch_json_or_default(existing_content)?;
    let next_name = configuration_name(draft)?;

    let configs = document
        .get_mut("configurations")
        .and_then(Value::as_array_mut)
        .expect("configurations is always an array");

    let existing_index = configs
        .iter()
        .position(|c| c.get("name").and_then(Value::as_str) == Some(next_name));

    let replaced = match existing_index {
        Some(index) => {
            configs[index] = merge_configuration(&configs[index], draft);
            true
        }
        None => {
            configs.push(draft.clone());
            false
        }
    };

    let content = format!(
        "{}\n",
        serde_json::to_string_pretty(&document).expect("launch document is serializable")
    );
    Ok(LaunchJsonWritePlan { content, replaced })
}

/// Merge the freshly generated configuration OVER the one already in the file
/// instead of replacing it.
///
/// The rule is narrow: **an incoming `<placeholder>` never overwrites a value
/// that is already resolved.** A customer told to hand-fill
/// `"device": "AE822F4M55_HP"` used to get it silently reset to
/// `"<resolved-device>"` on their next F5 — data loss on their own file, with no
/// confirm and no backup, and an unexitable loop around the advice they had just
/// been given.
///
/// Everything else still refreshes — `type`, `executable`, `cwd`, `servertype` —
/// so a repair such as `codelldb` → `lldb` still lands on an existing entry.
/// Keys the user added that this crate never writes (`serverArgs`, …) survive
/// untouched, and key order follows the existing entry with any new key
/// appended (`serde_json`'s `preserve_order` makes that hold).
///
/// Port of TS `mergeConfiguration` in
/// `packages/alp-core/src/debug/launchJsonCore.ts`.
fn merge_configuration(existing: &Value, next: &Value) -> Value {
    let (Some(existing_map), Some(next_map)) = (existing.as_object(), next.as_object()) else {
        // A non-object entry under this name is not something to merge into.
        return next.clone();
    };
    let mut merged = existing_map.clone();
    for (key, value) in next_map {
        merged.insert(key.clone(), merge_value(existing_map.get(key), value));
    }
    Value::Object(merged)
}

/// One key's worth of the merge rule above. `existing` is `None` when the key is
/// new, in which case the incoming value always wins.
fn merge_value(existing: Option<&Value>, next: &Value) -> Value {
    // Arrays (cortex-debug `configFiles`, OpenOCD `searchDir`): an
    // all-placeholder incoming list keeps the existing list WHOLE — a
    // hand-added second `.cfg` would otherwise be lost to a per-index merge
    // against a one-element draft. A mixed list still merges per element, so a
    // resolved entry this crate computed wins.
    if let (Some(next_items), Some(existing_items)) =
        (next.as_array(), existing.and_then(Value::as_array))
    {
        if !next_items.is_empty()
            && !existing_items.is_empty()
            && next_items.iter().all(is_unresolved_string)
        {
            return Value::Array(existing_items.clone());
        }
        return Value::Array(
            next_items
                .iter()
                .enumerate()
                .map(|(index, entry)| merge_value(existing_items.get(index), entry))
                .collect(),
        );
    }

    match existing {
        Some(existing) if is_unresolved_string(next) && is_resolved_string(existing) => {
            existing.clone()
        }
        _ => next.clone(),
    }
}

/// A string this crate considers FILLED IN. Mirrors TS `isResolvedValue`
/// (`/<[^<>]*>/`), which the preflight checks use too — the merge that protects
/// a user's hand-typed value must agree, key for key, with what preflight calls
/// unresolved. An empty string is not resolved (TS treats `""` as falsy).
fn is_resolved_string(value: &Value) -> bool {
    value
        .as_str()
        .is_some_and(|text| !text.is_empty() && !contains_placeholder(text))
}

/// A string that still needs resolving — including `""`, matching TS, where
/// `isUnresolvedString` is `typeof value === "string" && !isResolvedValue(value)`.
fn is_unresolved_string(value: &Value) -> bool {
    value.is_string() && !is_resolved_string(value)
}

/// `true` when `text` contains a `<…>` run with no nested angle bracket — the
/// Rust spelling of TS's `/<[^<>]*>/`. Catches `<resolved-device>` and the
/// two-placeholder `"<host>:<port>"`, and does NOT catch a lone `<` or `>`
/// (a `>` inside a path or a shell-ish `serverArgs` entry stays resolved).
fn contains_placeholder(text: &str) -> bool {
    let mut rest = text;
    while let Some(open) = rest.find('<') {
        let after = &rest[open + 1..];
        match after.find(['<', '>']) {
            // A closing bracket with nothing but plain text in between.
            Some(index) if after.as_bytes()[index] == b'>' => return true,
            // Another opening bracket: restart the scan from IT, so
            // `<<resolved-device>` still matches on the inner pair.
            Some(index) => rest = &after[index..],
            None => return false,
        }
    }
    false
}

/// Strip a leading UTF-8 BOM plus `//` / `/* */` comments from JSONC text,
/// then drop trailing commas before a closing `}`/`]`, so the result parses
/// as plain JSON. Scans char-by-char and tracks string state so a `//` or
/// trailing comma *inside* a quoted string value is never touched.
fn strip_jsonc(input: &str) -> String {
    let input = input.strip_prefix('\u{feff}').unwrap_or(input);

    let mut no_comments = String::with_capacity(input.len());
    let mut chars = input.chars().peekable();
    let mut in_string = false;
    let mut escaped = false;
    while let Some(c) = chars.next() {
        if in_string {
            no_comments.push(c);
            if escaped {
                escaped = false;
            } else if c == '\\' {
                escaped = true;
            } else if c == '"' {
                in_string = false;
            }
            continue;
        }
        match c {
            '"' => {
                in_string = true;
                no_comments.push(c);
            }
            '/' if chars.peek() == Some(&'/') => {
                chars.next();
                for c2 in chars.by_ref() {
                    if c2 == '\n' {
                        no_comments.push('\n');
                        break;
                    }
                }
            }
            '/' if chars.peek() == Some(&'*') => {
                chars.next();
                let mut prev = '\0';
                for c2 in chars.by_ref() {
                    if prev == '*' && c2 == '/' {
                        break;
                    }
                    prev = c2;
                }
            }
            _ => no_comments.push(c),
        }
    }

    let chars: Vec<char> = no_comments.chars().collect();
    let mut out = String::with_capacity(no_comments.len());
    let mut in_string = false;
    let mut escaped = false;
    let mut i = 0;
    while i < chars.len() {
        let c = chars[i];
        if in_string {
            out.push(c);
            if escaped {
                escaped = false;
            } else if c == '\\' {
                escaped = true;
            } else if c == '"' {
                in_string = false;
            }
            i += 1;
            continue;
        }
        if c == '"' {
            in_string = true;
            out.push(c);
            i += 1;
            continue;
        }
        if c == ',' {
            let mut j = i + 1;
            while j < chars.len() && chars[j].is_whitespace() {
                j += 1;
            }
            if j < chars.len() && (chars[j] == '}' || chars[j] == ']') {
                i += 1;
                continue; // drop the trailing comma
            }
        }
        out.push(c);
        i += 1;
    }
    out
}

fn parse_launch_json_or_default(content: Option<&str>) -> Result<Value, String> {
    // launch.json is JSONC in the wild, not strict JSON: VS Code's own "Add
    // Configuration" template opens with `//` comment lines, trailing commas
    // are common, and a Windows-authored file routinely carries a UTF-8 BOM
    // (str::trim does not strip U+FEFF). A strict serde_json::from_str used
    // to reject exactly the file VS Code itself writes, sending the user to
    // hand-edit a "valid" file. Strip BOM/comments/trailing commas first.
    let stripped = content.map(strip_jsonc);
    let trimmed = stripped.as_deref().map(str::trim).unwrap_or("");
    if trimmed.is_empty() {
        return Ok(json!({ "version": "0.2.0", "configurations": [] }));
    }

    let parsed: Value = serde_json::from_str(trimmed)
        .map_err(|_| "Alp: .vscode/launch.json is not valid JSON.".to_string())?;
    let Value::Object(mut candidate) = parsed else {
        return Err("Alp: .vscode/launch.json must be a JSON object.".to_string());
    };

    let version = match candidate.get("version").and_then(Value::as_str) {
        Some(v) if !v.trim().is_empty() => Value::String(v.to_string()),
        _ => Value::String("0.2.0".to_string()),
    };
    let configurations = match candidate.get("configurations") {
        Some(Value::Array(entries)) => {
            Value::Array(entries.iter().filter(|e| e.is_object()).cloned().collect())
        }
        _ => Value::Array(Vec::new()),
    };

    // {...candidate, version, configurations}: keep order, override the two keys.
    candidate.insert("version".to_string(), version);
    candidate.insert("configurations".to_string(), configurations);
    Ok(Value::Object(candidate))
}

fn configuration_name(configuration: &Value) -> Result<&str, String> {
    match configuration.get("name").and_then(Value::as_str) {
        Some(name) if !name.trim().is_empty() => Ok(name),
        _ => Err("Alp: debug launch draft is missing a valid name.".to_string()),
    }
}

#[cfg(test)]
mod resolution_tests {
    use super::*;
    use crate::debug::{DebugServerKind, DebugTargetKind};

    fn jlink_draft() -> Value {
        create_launch_draft(DebugTargetKind::ZephyrMcu, DebugServerKind::Jlink, None).unwrap()
    }

    #[test]
    fn fills_the_placeholders_a_build_can_answer() {
        let mut draft = jlink_draft();
        assert_eq!(draft["device"], "<resolved-device>");
        apply_launch_resolution(
            &mut draft,
            &LaunchResolution {
                executable: Some(
                    "${workspaceFolder}/build/m55_he-zephyr/build/zephyr/zephyr.elf".into(),
                ),
                device: Some("Cortex-M55".into()),
                gdb_path: Some(
                    "/zephyr-sdk-1.0.1/gnu/arm-zephyr-eabi/bin/arm-zephyr-eabi-gdb-py".into(),
                ),
                ..Default::default()
            },
        );
        assert_eq!(draft["device"], "Cortex-M55");
        assert_eq!(
            draft["executable"],
            "${workspaceFolder}/build/m55_he-zephyr/build/zephyr/zephyr.elf"
        );
        assert_eq!(
            draft["gdbPath"],
            "/zephyr-sdk-1.0.1/gnu/arm-zephyr-eabi/bin/arm-zephyr-eabi-gdb-py"
        );
    }

    #[test]
    fn an_unresolved_svd_key_is_dropped_not_left_pointing_nowhere() {
        // cortex-debug fails the whole session on an svdFile it cannot read;
        // an absent key only costs the peripheral view (alp-sdk#948).
        let mut draft = jlink_draft();
        assert_eq!(draft["svdFile"], "<resolved-svd>");
        apply_launch_resolution(&mut draft, &LaunchResolution::default());
        assert!(draft.get("svdFile").is_none());
        assert!(draft.get("svdPath").is_none());
        // …and the surviving keys keep their order: a swap-remove here used to
        // drag `interface` and `device` up into the two vacated slots.
        assert!(serde_json::to_string(&draft).unwrap().ends_with(
            "\"servertype\":\"jlink\",\"device\":\"<resolved-device>\",\"interface\":\"swd\"}"
        ));
    }

    #[test]
    fn openocd_gets_server_paths_and_every_config_file() {
        let mut draft =
            create_launch_draft(DebugTargetKind::ZephyrMcu, DebugServerKind::Openocd, None)
                .unwrap();
        apply_launch_resolution(
            &mut draft,
            &LaunchResolution {
                server_path: Some("/zephyr-sdk-1.0.1/hosttools/usr/bin/openocd".into()),
                search_dirs: vec![
                    "/zephyr-sdk-1.0.1/hosttools/opt/openocd/share/openocd/scripts".into(),
                ],
                config_files: vec![
                    "/boards/alp/board.cfg".into(),
                    "/boards/alp/extra.cfg".into(),
                ],
                ..Default::default()
            },
        );
        assert_eq!(
            draft["serverpath"],
            "/zephyr-sdk-1.0.1/hosttools/usr/bin/openocd"
        );
        assert_eq!(draft["searchDir"].as_array().unwrap().len(), 1);
        assert_eq!(draft["configFiles"].as_array().unwrap().len(), 2);
    }

    #[test]
    fn nothing_resolved_leaves_the_draft_as_it_was_apart_from_the_svd_keys() {
        // The pre-build path: the draft must still be emitted, unchanged.
        let mut draft = jlink_draft();
        let before = draft.clone();
        apply_launch_resolution(&mut draft, &LaunchResolution::default());
        assert_eq!(draft["device"], before["device"]);
        assert_eq!(draft["executable"], before["executable"]);
        assert!(draft.get("gdbPath").is_none(), "no gdb resolved -> no key");
        assert!(LaunchResolution::default().is_empty());
    }

    #[test]
    fn a_server_that_never_had_a_key_does_not_grow_one() {
        // A jlink draft has no `configFiles`; resolving OpenOCD values against
        // it must not invent the key.
        let mut draft = jlink_draft();
        apply_launch_resolution(
            &mut draft,
            &LaunchResolution {
                config_files: vec!["/boards/alp/board.cfg".into()],
                target_id: Some("cortex_m".into()),
                ..Default::default()
            },
        );
        assert!(draft.get("configFiles").is_none());
        assert!(draft.get("targetId").is_none());
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn unsupported_server_errors() {
        let err = create_launch_draft(DebugTargetKind::NativeHost, DebugServerKind::Jlink, None)
            .unwrap_err();
        assert!(err.contains("Unsupported debug backend 'jlink' for target 'native-host'"));
    }

    #[test]
    fn zephyr_jlink_draft_key_order_preserved() {
        let draft =
            create_launch_draft(DebugTargetKind::ZephyrMcu, DebugServerKind::Jlink, None).unwrap();
        let json = serde_json::to_string(&draft).unwrap();
        // Keys must stay in insertion order (preserve_order), not sorted.
        assert!(json.starts_with(
            "{\"name\":\"ALP: Zephyr Debug (J-Link)\",\"type\":\"cortex-debug\",\"request\":\"launch\""
        ));
        assert!(json.ends_with(
            "\"servertype\":\"jlink\",\"device\":\"<resolved-device>\",\"interface\":\"swd\"}"
        ));
    }

    #[test]
    fn zephyr_drafts_emit_svd_file_and_path_before_servertype() {
        for server in [
            DebugServerKind::Jlink,
            DebugServerKind::Openocd,
            DebugServerKind::Pyocd,
        ] {
            let draft = create_launch_draft(DebugTargetKind::ZephyrMcu, server, None).unwrap();
            assert_eq!(draft["svdFile"], "<resolved-svd>");
            assert_eq!(draft["svdPath"], "<resolved-svd>");
            // Key order: svdFile + svdPath close the gap the dropped
            // preLaunchTask left, and still sit before servertype.
            let json = serde_json::to_string(&draft).unwrap();
            assert!(json.contains(
                "\"runToEntryPoint\":\"main\",\"svdFile\":\"<resolved-svd>\",\"svdPath\":\"<resolved-svd>\",\"servertype\":"
            ));
        }
    }

    #[test]
    fn baremetal_draft_emits_svd_file_and_path_last() {
        let draft =
            create_launch_draft(DebugTargetKind::BaremetalMcu, DebugServerKind::Jlink, None)
                .unwrap();
        assert_eq!(draft["svdFile"], "<resolved-svd>");
        assert_eq!(draft["svdPath"], "<resolved-svd>");
        let json = serde_json::to_string(&draft).unwrap();
        assert!(json.ends_with(
            "\"interface\":\"swd\",\"svdFile\":\"<resolved-svd>\",\"svdPath\":\"<resolved-svd>\"}"
        ));
    }

    /// The Bug-1 regression: no profile may name a task by default, because
    /// nothing in tan-cli, alp-sdk-vscode or a generated project defines one.
    /// VS Code aborts pre-launch on an unresolvable `preLaunchTask`, so a
    /// default here means the emitted `launch.json` cannot start a session at
    /// all — and it looks perfectly fine while failing.
    #[test]
    fn no_profile_names_a_pre_launch_task_by_default() {
        for (target, server) in [
            (DebugTargetKind::ZephyrMcu, DebugServerKind::Jlink),
            (DebugTargetKind::ZephyrMcu, DebugServerKind::Openocd),
            (DebugTargetKind::ZephyrMcu, DebugServerKind::Pyocd),
            (DebugTargetKind::BaremetalMcu, DebugServerKind::Jlink),
            (DebugTargetKind::YoctoUserspace, DebugServerKind::Gdbserver),
            (DebugTargetKind::NativeHost, DebugServerKind::None),
        ] {
            let draft = create_launch_draft(target, server, None).unwrap();
            assert!(
                draft.get("preLaunchTask").is_none(),
                "{} + {}: preLaunchTask must be absent, not null, when unnamed",
                target.as_str(),
                server.as_str()
            );
            // Belt and braces: `null` would serialize as a key too.
            assert!(
                !serde_json::to_string(&draft)
                    .unwrap()
                    .contains("preLaunchTask")
            );
        }
    }

    /// The opt-in: a consumer that HAS registered a task provider names its own
    /// task, and gets it verbatim — no tan-owned string is baked in.
    #[test]
    fn an_opted_in_pre_launch_task_is_emitted_verbatim_in_place() {
        for (target, server) in [
            (DebugTargetKind::ZephyrMcu, DebugServerKind::Jlink),
            (DebugTargetKind::BaremetalMcu, DebugServerKind::Jlink),
            (DebugTargetKind::YoctoUserspace, DebugServerKind::Gdbserver),
            (DebugTargetKind::NativeHost, DebugServerKind::None),
        ] {
            let draft = create_launch_draft(target, server, Some("alpRun: build")).unwrap();
            assert_eq!(draft["preLaunchTask"], "alpRun: build");
        }
        // …and in its original position, not appended at the end.
        let draft = create_launch_draft(
            DebugTargetKind::ZephyrMcu,
            DebugServerKind::Jlink,
            Some("t"),
        )
        .unwrap();
        let json = serde_json::to_string(&draft).unwrap();
        assert!(json.contains(
            "\"runToEntryPoint\":\"main\",\"preLaunchTask\":\"t\",\"svdFile\":\"<resolved-svd>\""
        ));
    }

    #[test]
    fn non_mcu_drafts_omit_svd_fields() {
        for (target, server) in [
            (DebugTargetKind::YoctoUserspace, DebugServerKind::Gdbserver),
            (DebugTargetKind::NativeHost, DebugServerKind::None),
        ] {
            let draft = create_launch_draft(target, server, None).unwrap();
            assert!(draft.get("svdFile").is_none());
            assert!(draft.get("svdPath").is_none());
        }
    }

    /// Build a one-configuration `launch.json` around `configuration`, the way
    /// a user's file looks after a first write plus their own edits.
    fn launch_json_with(configuration: Value) -> String {
        serde_json::to_string(&json!({
            "version": "0.2.0",
            "configurations": [configuration],
        }))
        .unwrap()
    }

    /// The merged configuration under `name`, for asserting field by field.
    fn merged_configuration(content: &str, name: &str) -> Value {
        let document: Value = serde_json::from_str(content).unwrap();
        document["configurations"]
            .as_array()
            .unwrap()
            .iter()
            .find(|c| c["name"] == json!(name))
            .expect("configuration is present")
            .clone()
    }

    #[test]
    fn a_hand_resolved_value_survives_the_next_write() {
        // THE regression (#105): a customer is told to hand-fill `device`, and
        // the next F5 used to reset it to `<resolved-device>`.
        let draft =
            create_launch_draft(DebugTargetKind::ZephyrMcu, DebugServerKind::Jlink, None).unwrap();
        let mut existing = draft.clone();
        existing["device"] = json!("AE822F4M55_HP");
        // …while a field this crate DOES resolve is stale in their file.
        existing["type"] = json!("stale-adapter");

        let plan =
            create_launch_json_write_plan(Some(&launch_json_with(existing)), &draft).unwrap();
        assert!(plan.replaced);
        let merged = merged_configuration(&plan.content, draft["name"].as_str().unwrap());

        // The hand-typed value stands; the stale one refreshes.
        assert_eq!(merged["device"], json!("AE822F4M55_HP"));
        assert_eq!(merged["type"], json!("cortex-debug"));
        // A placeholder we could not resolve is still written where the user
        // has nothing of their own.
        assert_eq!(merged["svdFile"], json!("<resolved-svd>"));
    }

    #[test]
    fn keys_the_user_added_are_never_dropped() {
        let draft =
            create_launch_draft(DebugTargetKind::ZephyrMcu, DebugServerKind::Jlink, None).unwrap();
        let mut existing = draft.clone();
        existing["serverArgs"] = json!(["-select", "usb=603000869"]);
        existing["preLaunchTask"] = json!("my own build task");

        let plan =
            create_launch_json_write_plan(Some(&launch_json_with(existing)), &draft).unwrap();
        let merged = merged_configuration(&plan.content, draft["name"].as_str().unwrap());

        assert_eq!(merged["serverArgs"], json!(["-select", "usb=603000869"]));
        assert_eq!(merged["preLaunchTask"], json!("my own build task"));
    }

    #[test]
    fn an_all_placeholder_array_keeps_the_users_list_whole() {
        // `configFiles` is the case that bites: the draft carries ONE
        // placeholder, so a per-index merge would silently drop a second .cfg
        // the user added by hand.
        let draft = create_launch_draft(DebugTargetKind::ZephyrMcu, DebugServerKind::Openocd, None)
            .unwrap();
        let mut existing = draft.clone();
        existing["configFiles"] = json!(["board/alp_aen.cfg", "interface/cmsis-dap.cfg"]);

        let plan =
            create_launch_json_write_plan(Some(&launch_json_with(existing)), &draft).unwrap();
        let merged = merged_configuration(&plan.content, draft["name"].as_str().unwrap());

        assert_eq!(
            merged["configFiles"],
            json!(["board/alp_aen.cfg", "interface/cmsis-dap.cfg"])
        );
    }

    #[test]
    fn a_resolved_array_entry_still_wins_per_element() {
        let mut draft =
            create_launch_draft(DebugTargetKind::ZephyrMcu, DebugServerKind::Openocd, None)
                .unwrap();
        // A mixed incoming list: element 0 resolved by the build, element 1 not.
        draft["configFiles"] = json!([
            "/opt/openocd/board/real.cfg",
            "<resolved-openocd-board-cfg>"
        ]);
        let mut existing = draft.clone();
        existing["configFiles"] = json!(["board/stale.cfg", "interface/mine.cfg"]);

        let plan =
            create_launch_json_write_plan(Some(&launch_json_with(existing)), &draft).unwrap();
        let merged = merged_configuration(&plan.content, draft["name"].as_str().unwrap());

        assert_eq!(
            merged["configFiles"],
            json!(["/opt/openocd/board/real.cfg", "interface/mine.cfg"])
        );
    }

    #[test]
    fn placeholder_detection_matches_the_typescript_predicate() {
        assert!(is_unresolved_string(&json!("<resolved-device>")));
        // Two placeholders in one value — the Yocto `miDebuggerServerAddress`.
        assert!(is_unresolved_string(&json!("<host>:<port>")));
        // Empty is not "filled in" (TS treats "" as falsy in isResolvedValue).
        assert!(is_unresolved_string(&json!("")));
        assert!(is_resolved_string(&json!("AE822F4M55_HP")));
        // A lone angle bracket is not a placeholder: a real path keeps its
        // right to contain one.
        assert!(is_resolved_string(&json!("/opt/x>y/openocd.cfg")));
        assert!(is_resolved_string(&json!("2 < 3")));
        // Non-strings are neither.
        assert!(!is_unresolved_string(&json!(7)));
        assert!(!is_resolved_string(&json!(7)));
    }

    #[test]
    fn an_empty_existing_value_is_overwritten() {
        let draft =
            create_launch_draft(DebugTargetKind::ZephyrMcu, DebugServerKind::Jlink, None).unwrap();
        let mut existing = draft.clone();
        existing["device"] = json!("");

        let plan =
            create_launch_json_write_plan(Some(&launch_json_with(existing)), &draft).unwrap();
        let merged = merged_configuration(&plan.content, draft["name"].as_str().unwrap());

        // "" is not something the user resolved, so the placeholder replaces it
        // and preflight can name it.
        assert_eq!(merged["device"], json!("<resolved-device>"));
    }

    #[test]
    fn merge_keeps_the_existing_key_order_and_appends_new_keys() {
        let draft =
            create_launch_draft(DebugTargetKind::NativeHost, DebugServerKind::None, None).unwrap();
        // The user's file has our keys in a different order plus one of theirs.
        let existing = json!({
            "name": draft["name"],
            "cwd": "${workspaceFolder}",
            "myOwnKey": true,
            "type": "codelldb",
        });

        let plan =
            create_launch_json_write_plan(Some(&launch_json_with(existing)), &draft).unwrap();
        let merged = merged_configuration(&plan.content, draft["name"].as_str().unwrap());
        let keys: Vec<&str> = merged
            .as_object()
            .unwrap()
            .keys()
            .map(String::as_str)
            .collect();

        // Existing order first, then whatever the draft adds.
        assert_eq!(&keys[..4], &["name", "cwd", "myOwnKey", "type"]);
        assert!(keys.contains(&"request"));
        assert!(keys.contains(&"program"));
    }

    #[test]
    fn write_plan_appends_then_replaces_by_name() {
        let draft =
            create_launch_draft(DebugTargetKind::NativeHost, DebugServerKind::None, None).unwrap();
        let plan = create_launch_json_write_plan(None, &draft).unwrap();
        assert!(!plan.replaced);
        assert!(plan.content.ends_with("\n"));
        assert!(plan.content.contains("\"version\": \"0.2.0\""));

        // Re-applying the same-named config replaces it (still one entry).
        let plan2 = create_launch_json_write_plan(Some(&plan.content), &draft).unwrap();
        assert!(plan2.replaced);
        let doc: Value = serde_json::from_str(&plan2.content).unwrap();
        assert_eq!(doc["configurations"].as_array().unwrap().len(), 1);
    }

    #[test]
    fn preserves_unknown_top_level_keys() {
        let existing = "{\"version\":\"0.1.0\",\"inputs\":[],\"configurations\":[]}";
        let draft =
            create_launch_draft(DebugTargetKind::NativeHost, DebugServerKind::None, None).unwrap();
        let plan = create_launch_json_write_plan(Some(existing), &draft).unwrap();
        // `inputs` is preserved; version kept as the (valid) existing value.
        assert!(plan.content.contains("\"inputs\""));
        assert!(plan.content.contains("\"version\": \"0.1.0\""));
    }

    #[test]
    fn invalid_json_errors() {
        let err =
            create_launch_json_write_plan(Some("{not json"), &json!({"name": "x"})).unwrap_err();
        assert!(err.contains("not valid JSON"));
    }

    #[test]
    fn vscode_authored_jsonc_with_bom_and_comments_parses() {
        // The stock template VS Code's "Add Configuration" writes: a UTF-8
        // BOM, `//` comment lines, and a trailing comma. A strict
        // serde_json::from_str used to reject this file outright.
        let existing = "\u{feff}{\n  // Use IntelliSense to learn about possible attributes.\n  \"version\": \"0.2.0\",\n  \"configurations\": [],\n}\n";
        let draft =
            create_launch_draft(DebugTargetKind::NativeHost, DebugServerKind::None, None).unwrap();
        let plan = create_launch_json_write_plan(Some(existing), &draft).unwrap();
        assert!(!plan.replaced);
        let doc: Value = serde_json::from_str(&plan.content).unwrap();
        assert_eq!(doc["configurations"].as_array().unwrap().len(), 1);
    }

    #[test]
    fn block_comment_and_slash_inside_string_are_not_mistaken_for_comments() {
        let existing = "{\n  /* header */\n  \"version\": \"0.2.0\",\n  \"configurations\": [{\"name\": \"has // slashes /* not a comment */\"}]\n}";
        let doc = parse_launch_json_or_default(Some(existing)).unwrap();
        let configs = doc["configurations"].as_array().unwrap();
        assert_eq!(configs.len(), 1);
        assert_eq!(configs[0]["name"], "has // slashes /* not a comment */");
    }
}
