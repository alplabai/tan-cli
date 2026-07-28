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
            let name = format!("Alp: Zephyr Debug ({})", server_label(server));
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
            "name": format!("Alp: Baremetal Debug ({})", server_label(server)),
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
            "name": "Alp: Yocto Remote Debug",
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
            "name": "Alp: Native Sim Debug",
            // `lldb`, not `codelldb`, because CodeLLDB's own manifest says so:
            // `vadimcn.vscode-lldb` v1.12.2 declares
            // `contributes.debuggers[0].type = "lldb"`. `codelldb` is the
            // extension's marketplace NAME; no extension registers it as a
            // debug type, so VS Code refused every session outright with
            // `Configured debug type 'codelldb' is not supported.` (#104).
            // native_sim is the only target reachable with no probe and no
            // board — the first debugging experience a customer has.
            "type": "lldb",
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

    // `executable` (cortex-debug) / `program` (cppdbg, lldb) name the same
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
    /// The legacy `"ALP: ..."` name of an entry that was adopted onto the
    /// current `"Alp: ..."` name this run (see [`legacy_name`]). `None` on
    /// every other path, including when a current-named entry already existed
    /// (that case never looks for a legacy counterpart at all — see
    /// [`create_launch_json_write_plan`]).
    pub migrated_from: Option<String>,
}

/// The pre-#155 spelling of a current launch-configuration name, or `None` if
/// `next_name` does not use the current `"Alp: "` prefix (defensive: every
/// name [`create_launch_draft`] emits does).
///
/// Deliberately narrow: this computes the ONE legacy string that corresponds
/// to `next_name` — one of the four names this crate ever emits — rather than
/// matching any configuration whose name happens to start with `"ALP: "`. A
/// customer's own unrelated entry that happens to start with those letters
/// (`"ALP: My Custom Config"`) is not one of the four and is never touched.
fn legacy_name(next_name: &str) -> Option<String> {
    next_name
        .strip_prefix("Alp: ")
        .map(|rest| format!("ALP: {rest}"))
}

/// Whether a string is one of OUR "nobody filled this in yet" markers.
///
/// The two brace styles in a launch configuration mean opposite things, and
/// that is the trap:
///
/// - `${…}` is a VS Code **variable substitution** (`${workspaceFolder}`,
///   `${env:HOME}`). VS Code expands it itself at launch, so it is fully
///   resolved as far as we are concerned. No angle bracket is involved.
/// - `<…>` is ours — `<resolved-device>`, `<resolved-svd>`,
///   `<resolved-openocd-board-cfg>`, `<resolved-gdb>`, `<resolved-target-id>`,
///   and the two-token `<host>:<port>`. Nothing expands these; handed to a
///   debug adapter verbatim they are a literal device name / path / TCP
///   address.
///
/// So the test is any angle-bracket token, NOT a `<resolved-` prefix. The
/// prefix test passed `<host>:<port>` as a real address — the hole
/// alp-sdk-vscode found, where a yocto profile reported launchable with an
/// unusable gdbserver address. Same hole here: `has_placeholder` in
/// `debug-config` suppressed the "still needs resolution" note on exactly
/// that config.
pub fn is_unresolved_placeholder(value: &str) -> bool {
    // Equivalent to the TS `/<[^<>]*>/`: after splitting on `<`, no fragment
    // can contain another `<`, so a fragment holding a `>` IS a `<…>` token.
    value.split('<').skip(1).any(|rest| rest.contains('>'))
}

fn is_unresolved(value: &Value) -> bool {
    value.as_str().is_some_and(is_unresolved_placeholder)
}

fn is_resolved(value: Option<&Value>) -> bool {
    value
        .and_then(Value::as_str)
        .is_some_and(|s| !is_unresolved_placeholder(s))
}

/// Merge one incoming value over what the file already holds.
///
/// The whole rule: **an incoming unresolved `<…>` placeholder never overwrites
/// a concrete existing value.** That is also what tells "the customer set this
/// deliberately" apart from "this is our old output" — our output for a field
/// we could not resolve is *literally* an angle-bracket token, so anything
/// concrete in the file is either the customer's or a real value we computed.
/// The inverse still works: whenever this run CAN resolve a field, the
/// incoming value is concrete and overwrites unconditionally, so a stale value
/// that is now wrong is still updateable.
fn merge_value(existing: Option<&Value>, next: &Value) -> Value {
    if let (Value::Array(next_items), Some(Value::Array(existing_items))) = (next, existing) {
        // cortex-debug `configFiles`: an all-placeholder incoming list keeps
        // the existing list WHOLE, or a hand-added second `.cfg` is lost to a
        // per-index merge against a one-element draft. A mixed list still
        // merges per element, so an entry we did resolve wins.
        if !next_items.is_empty()
            && !existing_items.is_empty()
            && next_items.iter().all(is_unresolved)
        {
            return Value::Array(existing_items.clone());
        }
        return Value::Array(
            next_items
                .iter()
                .enumerate()
                .map(|(i, item)| merge_value(existing_items.get(i), item))
                .collect(),
        );
    }
    if is_unresolved(next) && is_resolved(existing) {
        existing.expect("is_resolved implies Some").clone()
    } else {
        next.clone()
    }
}

/// Merge the freshly generated configuration OVER the one already in the file
/// (see [`merge_value`] for the rule), instead of replacing it.
///
/// This runs before every session and the configuration names are fixed per
/// target/server, so a wholesale replace meant a customer told to hand-fill
/// `"device": "AE822F4M55_HP"` got it reset to `"<resolved-device>"` on their
/// next F5 — data loss on their own file, no confirm, no backup, and an
/// unexitable loop around the advice we had just given them (#105).
///
/// Key order follows the existing entry with new keys appended (insert over a
/// clone, under `preserve_order`), and keys the customer added that we never
/// write (`preLaunchTask`, `serverArgs`, …) are left untouched because only
/// the draft's own keys are visited.
fn merge_configuration(existing: &Value, next: &Value) -> Value {
    let (Some(existing_map), Some(next_map)) = (existing.as_object(), next.as_object()) else {
        return next.clone();
    };
    let mut merged = existing_map.clone();
    for (key, value) in next_map {
        let m = merge_value(existing_map.get(key), value);
        merged.insert(key.clone(), m);
    }
    Value::Object(merged)
}

/// Merge `draft` into an existing `launch.json` (or a fresh document), merging
/// key-by-key over any configuration with the same `name`. Mirrors TS
/// `createLaunchJsonWritePlan`.
///
/// #133 (reopened): the #155 rename to `"Alp: ..."` left any entry still
/// spelled `"ALP: ..."` orphaned — nothing matched it by exact name any more,
/// so it silently stopped receiving merges. That is not cosmetic: the
/// orphaned entry is exactly where a customer's own hand-resolved fields
/// (`device`, …) already lived, and the maintained entry kept its
/// placeholder. So a MISS on the current name now falls through to a search
/// for that one legacy counterpart ([`legacy_name`]) before giving up and
/// appending fresh, and a hit there is folded in via the SAME
/// [`merge_configuration`] a same-named update already uses — the customer's
/// hand-filled values survive onto the correctly-named entry exactly the way
/// they survive a normal re-run, and the entry is renamed in place rather than
/// duplicated.
///
/// This only ever fires on a MISS against the current name. When a
/// current-named entry already exists — whether or not a legacy one *also*
/// still sits in the file (a workspace that ran a pre-#155 `tan` and then a
/// post-#155 one) — this function takes the ordinary same-name-replace path
/// and never looks for a legacy counterpart at all. The legacy entry is left
/// exactly as it is: nothing decides which of two possibly-hand-edited
/// entries is authoritative, so nothing is merged or deleted on this run's
/// say-so. That is not an oversight; see `crates/tan-core/src/debug_launch.rs`
/// test `both_a_current_and_a_legacy_entry_leaves_the_legacy_one_untouched`.
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

    let (replaced, migrated_from) = match existing_index {
        Some(index) => {
            configs[index] = merge_configuration(&configs[index], draft);
            (true, None)
        }
        None => {
            let legacy_index = legacy_name(next_name).and_then(|legacy| {
                configs
                    .iter()
                    .position(|c| c.get("name").and_then(Value::as_str) == Some(legacy.as_str()))
            });
            match legacy_index {
                Some(index) => {
                    let from = configs[index]
                        .get("name")
                        .and_then(Value::as_str)
                        .map(str::to_string);
                    configs[index] = merge_configuration(&configs[index], draft);
                    (true, from)
                }
                None => {
                    configs.push(draft.clone());
                    (false, None)
                }
            }
        }
    };

    let content = format!(
        "{}\n",
        serde_json::to_string_pretty(&document).expect("launch document is serializable")
    );
    Ok(LaunchJsonWritePlan {
        content,
        replaced,
        migrated_from,
    })
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
            "{\"name\":\"Alp: Zephyr Debug (J-Link)\",\"type\":\"cortex-debug\",\"request\":\"launch\""
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

    /// Every debug type tan can emit, and the extension that actually
    /// contributes it — verified against each extension's own `package.json`
    /// `contributes.debuggers[].type`:
    ///
    /// | emitted type   | extension            | version verified |
    /// |----------------|----------------------|------------------|
    /// | `cortex-debug` | `marus25.cortex-debug` | v1.12.1        |
    /// | `cppdbg`       | `ms-vscode.cpptools`   | v1.23.6        |
    /// | `lldb`         | `vadimcn.vscode-lldb`  | v1.12.2        |
    ///
    /// These three are exactly the extensions `debug::doctor` declares and
    /// checks for. A table can be wrong; a table is at least checkable, which
    /// is more than `"codelldb"` ever was — that value named an extension
    /// rather than a type and VS Code refused every native_sim session for the
    /// whole life of the code (#104).
    const CONTRIBUTED_DEBUG_TYPES: [(&str, &str); 3] = [
        ("cortex-debug", "marus25.cortex-debug v1.12.1"),
        ("cppdbg", "ms-vscode.cpptools v1.23.6"),
        ("lldb", "vadimcn.vscode-lldb v1.12.2"),
    ];

    /// Adding a `DebugTargetKind` fails to compile here until it is also added
    /// to `every_emitted_debug_type_is_one_an_extension_contributes` below.
    fn _target_kinds_are_all_covered(target: DebugTargetKind) {
        match target {
            DebugTargetKind::ZephyrMcu
            | DebugTargetKind::BaremetalMcu
            | DebugTargetKind::YoctoUserspace
            | DebugTargetKind::NativeHost => {}
        }
    }

    #[test]
    fn every_emitted_debug_type_is_one_an_extension_contributes() {
        for target in [
            DebugTargetKind::ZephyrMcu,
            DebugTargetKind::BaremetalMcu,
            DebugTargetKind::YoctoUserspace,
            DebugTargetKind::NativeHost,
        ] {
            for &server in crate::debug::server_choices_for_target(target) {
                let draft = create_launch_draft(target, server, None).unwrap();
                let emitted = draft["type"].as_str().expect("every draft names a type");
                assert!(
                    CONTRIBUTED_DEBUG_TYPES.iter().any(|(t, _)| *t == emitted),
                    "{} + {} emits debug type '{}', which no extension tan's \
                     doctor declares contributes. VS Code answers 'Configured \
                     debug type '{}' is not supported.' and the session never \
                     starts. Known-good: {:?}",
                    target.as_str(),
                    server.as_str(),
                    emitted,
                    emitted,
                    CONTRIBUTED_DEBUG_TYPES,
                );
            }
        }
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

    /// #133: tan used to spell every launch-configuration `name` `"ALP: …"`
    /// while `alp-sdk-vscode` spells the same four `"Alp: …"`. Both sides merge
    /// by exact `name`, so the two spellings never matched and a workspace that
    /// had seen both tools ended up with two of every debug configuration. This
    /// simulates a `launch.json` the extension already wrote (its own draft,
    /// `Alp:`-spelled, never anything tan produced) and asserts tan's own write
    /// merges into it rather than appending a duplicate. Pre-fix, when
    /// `create_launch_draft` still emitted `"ALP: Native Sim Debug"`, this name
    /// mismatch would have made `plan.replaced` false and left two entries.
    #[test]
    fn an_existing_extension_written_configuration_is_merged_not_duplicated() {
        let existing = existing_with(json!({
            "name": "Alp: Native Sim Debug",
            "type": "lldb",
            "request": "launch",
            "program": "${workspaceFolder}/build/native_sim/zephyr/zephyr.exe",
            "cwd": "${workspaceFolder}",
        }));
        let draft =
            create_launch_draft(DebugTargetKind::NativeHost, DebugServerKind::None, None).unwrap();
        assert_eq!(draft["name"], "Alp: Native Sim Debug");

        let plan = create_launch_json_write_plan(Some(&existing), &draft).unwrap();
        assert!(
            plan.replaced,
            "an existing Alp:-named configuration must be merged, not duplicated"
        );
        let doc: Value = serde_json::from_str(&plan.content).unwrap();
        assert_eq!(
            doc["configurations"].as_array().unwrap().len(),
            1,
            "duplicated instead of merging: {}",
            plan.content
        );
    }

    /// #133 reopened: the vscode drive that reopened this issue, reproduced.
    /// Only the legacy `"ALP: Zephyr Debug (J-Link)"` entry exists, carrying
    /// the customer's own hand-filled `"device": "AE822F4M55_HP"`. A test that
    /// only checked the name changed would pass while still stranding that
    /// value — so this asserts BOTH: the file ends with one, correctly-named
    /// entry, AND the hand-filled device is the one that survives onto it
    /// (not the fresh `"<resolved-device>"` placeholder the draft carries).
    #[test]
    fn only_a_legacy_alp_entry_migrates_and_keeps_the_hand_filled_device() {
        let existing = existing_with(json!({
            "name": "ALP: Zephyr Debug (J-Link)",
            "type": "cortex-debug",
            "request": "launch",
            "cwd": "${workspaceFolder}",
            "executable": "${workspaceFolder}/build/app/zephyr/zephyr.elf",
            "servertype": "jlink",
            "device": "AE822F4M55_HP",
            "interface": "swd",
        }));
        let draft =
            create_launch_draft(DebugTargetKind::ZephyrMcu, DebugServerKind::Jlink, None).unwrap();
        assert_eq!(draft["name"], "Alp: Zephyr Debug (J-Link)");
        assert_eq!(draft["device"], "<resolved-device>");

        let plan = create_launch_json_write_plan(Some(&existing), &draft).unwrap();
        assert_eq!(
            plan.migrated_from.as_deref(),
            Some("ALP: Zephyr Debug (J-Link)")
        );
        assert!(plan.replaced);

        let doc: Value = serde_json::from_str(&plan.content).unwrap();
        let configs = doc["configurations"].as_array().unwrap();
        assert_eq!(
            configs.len(),
            1,
            "the legacy entry must be adopted in place, not left behind \
             alongside a new one: {}",
            plan.content
        );
        assert_eq!(configs[0]["name"], "Alp: Zephyr Debug (J-Link)");
        assert_eq!(
            configs[0]["device"], "AE822F4M55_HP",
            "the customer's hand-filled device must survive the migration, \
             not be reset to the fresh placeholder: {}",
            plan.content
        );
    }

    /// #177 review finding #2: every migration test above pins only `device`
    /// -- an unresolved-placeholder-backed field, which is the ONE thing the
    /// merge protects. None pins the other side of the same trade-off: a
    /// hand-tuned field the draft does NOT leave as a `<resolved-…>`
    /// placeholder (`cwd`, `executable`, `runToEntryPoint`) is refreshed to
    /// this run's fresh value on migration, exactly like an ordinary
    /// same-name re-run -- migration is not a "preserve everything" mode.
    /// Reproduces the exact #177 review transcript (hand-tuned `cwd`,
    /// `executable`, `runToEntryPoint` on the legacy entry) so both
    /// directions are asserted on the SAME input rather than assumed from
    /// `merge_configuration`'s ordinary-re-run behaviour.
    #[test]
    fn legacy_migration_refreshes_tan_owned_fields_while_keeping_the_hand_filled_placeholder_backed_ones()
     {
        let existing = existing_with(json!({
            "name": "ALP: Zephyr Debug (J-Link)",
            "type": "cortex-debug",
            "request": "launch",
            "cwd": "${workspaceFolder}/app",
            "executable": "${workspaceFolder}/build/m55_hp/zephyr/zephyr.elf",
            "runToEntryPoint": "app_main",
            "servertype": "jlink",
            "device": "AE822F4M55_HP",
            "interface": "swd",
        }));
        let draft =
            create_launch_draft(DebugTargetKind::ZephyrMcu, DebugServerKind::Jlink, None).unwrap();

        let plan = create_launch_json_write_plan(Some(&existing), &draft).unwrap();
        assert_eq!(
            plan.migrated_from.as_deref(),
            Some("ALP: Zephyr Debug (J-Link)")
        );
        let merged = merged_config(&plan);

        // Placeholder-backed field: the hand-filled value survives.
        assert_eq!(merged["device"], "AE822F4M55_HP");
        // tan-owned fields, none of them placeholder-backed in the draft:
        // refreshed to this run's fresh values, same as any ordinary re-run.
        assert_eq!(merged["cwd"], "${workspaceFolder}");
        assert_eq!(
            merged["executable"],
            "${workspaceFolder}/build/app/zephyr/zephyr.elf"
        );
        assert_eq!(merged["runToEntryPoint"], "main");
    }

    /// #133 reopened, "the dangerous branch": both a maintained `"Alp: ..."`
    /// entry (placeholder device, per the reported transcript) AND the legacy
    /// `"ALP: ..."` entry (the customer's real hand-filled device) exist at
    /// once — a workspace that ran a pre-#155 `tan` and then a post-#155 one.
    /// The decision here is to touch neither entry beyond the ordinary
    /// same-name merge of the maintained one: nothing decides which of two
    /// possibly-hand-edited entries is authoritative, so migrating or deleting
    /// either would risk destroying user data on a guess. This asserts BOTH
    /// entries still exist afterwards and the legacy one is BYTE-IDENTICAL to
    /// what the customer had — proving it truly was not touched, not merely
    /// that its `device` field looks unchanged.
    #[test]
    fn both_a_current_and_a_legacy_entry_leaves_the_legacy_one_untouched() {
        let legacy = json!({
            "name": "ALP: Zephyr Debug (J-Link)",
            "type": "cortex-debug",
            "request": "launch",
            "device": "AE822F4M55_HP",
            "servertype": "jlink",
        });
        let existing = serde_json::to_string_pretty(&json!({
            "version": "0.2.0",
            "configurations": [
                json!({
                    "name": "Alp: Zephyr Debug (J-Link)",
                    "type": "cortex-debug",
                    "request": "launch",
                    "cwd": "${workspaceFolder}",
                    "executable": "${workspaceFolder}/build/app/zephyr/zephyr.elf",
                    "servertype": "jlink",
                    "device": "<resolved-device>",
                    "interface": "swd",
                }),
                legacy.clone(),
            ],
        }))
        .unwrap();
        let draft =
            create_launch_draft(DebugTargetKind::ZephyrMcu, DebugServerKind::Jlink, None).unwrap();

        let plan = create_launch_json_write_plan(Some(&existing), &draft).unwrap();
        assert!(
            plan.replaced,
            "the exact-name match on the maintained entry must still fire"
        );
        assert_eq!(
            plan.migrated_from, None,
            "a current-named entry already existed, so no legacy search must \
             have run at all"
        );

        let doc: Value = serde_json::from_str(&plan.content).unwrap();
        let configs = doc["configurations"].as_array().unwrap();
        assert_eq!(
            configs.len(),
            2,
            "both entries must still exist -- deleting either is a data-loss \
             decision this run must not make on a guess: {}",
            plan.content
        );
        let found_legacy = configs
            .iter()
            .find(|c| c["name"] == "ALP: Zephyr Debug (J-Link)")
            .expect("the legacy entry must still be present");
        assert_eq!(
            found_legacy, &legacy,
            "the legacy entry must be byte-identical to what the customer had \
             -- not merely 'device unchanged' -- proving it was not touched: {}",
            plan.content
        );
    }

    /// A legacy entry for a DIFFERENT target/server than the one being
    /// written this run must not be mistaken for this draft's counterpart:
    /// `"ALP: Zephyr Debug (J-Link)"` sits in the file while this run writes
    /// `"Alp: Native Sim Debug"`. Per-draft scoping means the legacy entry is
    /// left alone and the new draft is appended fresh, same as if the legacy
    /// entry were not there at all.
    #[test]
    fn a_legacy_entry_for_a_different_target_is_left_alone() {
        let existing = existing_with(json!({
            "name": "ALP: Zephyr Debug (J-Link)",
            "type": "cortex-debug",
            "device": "AE822F4M55_HP",
        }));
        let draft =
            create_launch_draft(DebugTargetKind::NativeHost, DebugServerKind::None, None).unwrap();
        assert_eq!(draft["name"], "Alp: Native Sim Debug");

        let plan = create_launch_json_write_plan(Some(&existing), &draft).unwrap();
        assert_eq!(plan.migrated_from, None);
        assert!(!plan.replaced);

        let doc: Value = serde_json::from_str(&plan.content).unwrap();
        let configs = doc["configurations"].as_array().unwrap();
        assert_eq!(configs.len(), 2, "{}", plan.content);
        let untouched = configs
            .iter()
            .find(|c| c["name"] == "ALP: Zephyr Debug (J-Link)")
            .expect("the unrelated legacy entry must still be present");
        assert_eq!(untouched["device"], "AE822F4M55_HP");
    }

    /// The narrow-match-rule guard: a customer's OWN config that happens to
    /// start with `"ALP: "` but is not one of the four names this crate ever
    /// emitted (`"ALP: My Custom Config"`) must never be mistaken for a legacy
    /// counterpart of anything tan writes, even though a naive `starts_with("ALP:
    /// ")` scan would match it. Proves the rule is "the one legacy spelling of
    /// THIS draft's name", not "anything ALP-prefixed".
    #[test]
    fn an_unrelated_alp_prefixed_user_config_is_never_mistaken_for_the_legacy_name() {
        let existing = existing_with(json!({
            "name": "ALP: My Custom Config",
            "type": "cortex-debug",
            "device": "hand-picked-value",
        }));
        let draft =
            create_launch_draft(DebugTargetKind::NativeHost, DebugServerKind::None, None).unwrap();

        let plan = create_launch_json_write_plan(Some(&existing), &draft).unwrap();
        assert_eq!(plan.migrated_from, None);
        assert!(!plan.replaced);

        let doc: Value = serde_json::from_str(&plan.content).unwrap();
        let configs = doc["configurations"].as_array().unwrap();
        assert_eq!(configs.len(), 2, "{}", plan.content);
        let untouched = configs
            .iter()
            .find(|c| c["name"] == "ALP: My Custom Config")
            .expect("the unrelated user config must still be present");
        assert_eq!(untouched["device"], "hand-picked-value");
    }

    /// Serialize `{version, configurations:[config]}` as an existing file.
    fn existing_with(config: Value) -> String {
        serde_json::to_string_pretty(&json!({
            "version": "0.2.0",
            "configurations": [config],
        }))
        .unwrap()
    }

    /// Re-read the single merged configuration out of a write plan.
    fn merged_config(plan: &LaunchJsonWritePlan) -> Value {
        let doc: Value = serde_json::from_str(&plan.content).unwrap();
        let configs = doc["configurations"].as_array().unwrap();
        assert_eq!(configs.len(), 1);
        configs[0].clone()
    }

    #[test]
    fn a_hand_filled_value_survives_the_next_write_while_stale_fields_refresh() {
        // The #105 report: the customer is told to fill in `device`, does, and
        // the next F5 writes `<resolved-device>` back over it.
        let existing = existing_with(json!({
            "name": "Alp: Zephyr Debug (J-Link)",
            "type": "cortex-debug",
            "request": "launch",
            "cwd": "${workspaceFolder}",
            "executable": "${workspaceFolder}/build/stale/zephyr.elf",
            "device": "AE822F4M55_HP",
        }));
        let draft =
            create_launch_draft(DebugTargetKind::ZephyrMcu, DebugServerKind::Jlink, None).unwrap();
        assert_eq!(draft["device"], "<resolved-device>");

        let plan = create_launch_json_write_plan(Some(&existing), &draft).unwrap();
        assert!(plan.replaced);
        let merged = merged_config(&plan);
        assert_eq!(merged["device"], "AE822F4M55_HP");
        // …and everything this run DOES know still refreshes.
        assert_eq!(
            merged["executable"],
            "${workspaceFolder}/build/app/zephyr/zephyr.elf"
        );
        // Key order follows the existing entry, new keys appended.
        let keys: Vec<&str> = merged
            .as_object()
            .unwrap()
            .keys()
            .map(String::as_str)
            .collect();
        assert_eq!(
            &keys[..6],
            &["name", "type", "request", "cwd", "executable", "device"]
        );
    }

    #[test]
    fn a_stale_value_is_still_updateable_and_a_broken_type_is_still_repaired() {
        // The inverse of the rule above: a concrete incoming value overwrites
        // unconditionally, so a value that is now WRONG is not frozen in — the
        // `codelldb` → `lldb` repair (#104) has to land on an entry that
        // already exists, which is every entry after the first write.
        let existing = existing_with(json!({
            "name": "Alp: Native Sim Debug",
            "type": "codelldb",
            "request": "launch",
            "program": "${workspaceFolder}/build/native_sim/zephyr/zephyr.exe",
            "cwd": "${workspaceFolder}",
        }));
        let mut draft =
            create_launch_draft(DebugTargetKind::NativeHost, DebugServerKind::None, None).unwrap();
        apply_launch_resolution(
            &mut draft,
            &LaunchResolution {
                executable: Some("${workspaceFolder}/build/native_sim-zephyr/zephyr.exe".into()),
                ..Default::default()
            },
        );

        let merged =
            merged_config(&create_launch_json_write_plan(Some(&existing), &draft).unwrap());
        assert_eq!(merged["type"], "lldb");
        assert_eq!(
            merged["program"],
            "${workspaceFolder}/build/native_sim-zephyr/zephyr.exe"
        );
    }

    #[test]
    fn a_hand_typed_gdbserver_address_survives_the_host_port_placeholder() {
        // `<host>:<port>` carries no `<resolved-` prefix — the exact hole the
        // extension found. A prefix-only predicate calls it a real address and
        // overwrites the one the customer typed.
        let existing = existing_with(json!({
            "name": "Alp: Yocto Remote Debug",
            "type": "cppdbg",
            "miDebuggerServerAddress": "192.168.10.42:3333",
            "miDebuggerPath": "/opt/gdb/bin/aarch64-poky-linux-gdb",
        }));
        let draft = create_launch_draft(
            DebugTargetKind::YoctoUserspace,
            DebugServerKind::Gdbserver,
            None,
        )
        .unwrap();
        assert_eq!(draft["miDebuggerServerAddress"], "<host>:<port>");

        let merged =
            merged_config(&create_launch_json_write_plan(Some(&existing), &draft).unwrap());
        assert_eq!(merged["miDebuggerServerAddress"], "192.168.10.42:3333");
        assert_eq!(
            merged["miDebuggerPath"],
            "/opt/gdb/bin/aarch64-poky-linux-gdb"
        );
    }

    #[test]
    fn an_all_placeholder_config_files_list_keeps_the_existing_list_whole() {
        // A per-index merge against the one-element draft would drop the
        // customer's hand-added second `.cfg` entirely.
        let existing = existing_with(json!({
            "name": "Alp: Zephyr Debug (OpenOCD)",
            "type": "cortex-debug",
            "servertype": "openocd",
            "configFiles": ["/boards/alp/board.cfg", "/boards/alp/extra.cfg"],
        }));
        let draft = create_launch_draft(DebugTargetKind::ZephyrMcu, DebugServerKind::Openocd, None)
            .unwrap();
        assert_eq!(
            draft["configFiles"],
            json!(["<resolved-openocd-board-cfg>"])
        );

        let merged =
            merged_config(&create_launch_json_write_plan(Some(&existing), &draft).unwrap());
        assert_eq!(
            merged["configFiles"],
            json!(["/boards/alp/board.cfg", "/boards/alp/extra.cfg"])
        );

        // A list this run DID resolve still replaces it.
        let mut resolved_draft = draft.clone();
        apply_launch_resolution(
            &mut resolved_draft,
            &LaunchResolution {
                config_files: vec!["/zephyr/boards/real.cfg".into()],
                ..Default::default()
            },
        );
        let merged = merged_config(
            &create_launch_json_write_plan(Some(&existing), &resolved_draft).unwrap(),
        );
        assert_eq!(merged["configFiles"], json!(["/zephyr/boards/real.cfg"]));
    }

    #[test]
    fn keys_the_customer_added_that_tan_never_writes_are_untouched() {
        let existing = existing_with(json!({
            "name": "Alp: Native Sim Debug",
            "type": "lldb",
            "preLaunchTask": "my own build task",
            "initCommands": ["settings set target.x86-disassembly-flavor intel"],
        }));
        let draft =
            create_launch_draft(DebugTargetKind::NativeHost, DebugServerKind::None, None).unwrap();
        assert!(draft.get("preLaunchTask").is_none());

        let merged =
            merged_config(&create_launch_json_write_plan(Some(&existing), &draft).unwrap());
        assert_eq!(merged["preLaunchTask"], "my own build task");
        assert_eq!(
            merged["initCommands"],
            json!(["settings set target.x86-disassembly-flavor intel"])
        );
    }

    #[test]
    fn placeholder_predicate_separates_our_markers_from_vs_code_substitutions() {
        for ours in [
            "<resolved-device>",
            "<resolved-openocd-board-cfg>",
            "<host>:<port>",
        ] {
            assert!(is_unresolved_placeholder(ours), "{ours} is a placeholder");
        }
        for theirs in [
            "${workspaceFolder}/build/app/zephyr/zephyr.elf",
            "AE822F4M55_HP",
            "192.168.10.42:3333",
            "",
        ] {
            assert!(
                !is_unresolved_placeholder(theirs),
                "{theirs} is a real value"
            );
        }
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
