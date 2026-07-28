// SPDX-License-Identifier: Apache-2.0
//! The workspace-parent guard (tan-cli#185): whether `tan bootstrap` may
//! write `zephyr/`, `modules/`, `.west/` and the venv into the checkout's
//! parent directory without asking first.
//!
//! `west init -l <alp-sdk>` forces the west topdir to be the checkout's
//! PARENT (#769) — that placement is not tan's to change. What IS tan's to
//! decide is whether that parent is a safe place to spray a multi-gigabyte
//! tree into unannounced. This module is the pure predicate; the directory
//! listing that feeds it, the interactive prompt, and the actual move all
//! stay IO and live in `tan-cli`'s `commands::bootstrap`.

/// The dedicated subdirectory `tan bootstrap` offers to relocate the checkout
/// into when the guard fires and the customer accepts (or `--workspace` names
/// nothing more specific): `<parent>/alp-workspace/<checkout-name>`. Not a
/// detection heuristic — the guard never keys off a directory NAME to decide
/// whether to fire (see [`parent_needs_workspace_guard`]) — this is only the
/// name tan itself chooses for the new home it builds, so the log line and
/// the actual `mkdir` can never drift apart.
pub const DEFAULT_WORKSPACE_DIR_NAME: &str = "alp-workspace";

/// Whether the checkout's parent directory needs the workspace-parent guard.
///
/// The trigger, verbatim (tan-cli#185): proceed silently when the parent
/// contains NOTHING BUT the checkout itself and/or an existing west
/// workspace; otherwise guard. Concretely:
///
///   - `entries` is the parent's DIRECT children only (one level, not
///     recursive) — what an existing west workspace looks like one level
///     down is out of scope; only whether `.west` is one of those children
///     matters.
///   - "An existing west workspace" is recognized by exactly ONE marker: a
///     `.west` entry. `west init -l` writes `<topdir>/.west/config` as its
///     very first act, so a parent that already has one is already the
///     topdir of a real (or interrupted) west workspace — every OTHER entry
///     there (`zephyr`, `modules`, `tools`, `bootloader`, the venv dir, a
///     stray `.venv-old`, …) is that workspace's own content, not "unrelated
///     content" the guard exists to catch. `.west` present is therefore
///     sufficient on its own; nothing else in `entries` is even inspected in
///     that case.
///   - Without a `.west` entry, the parent is judged purely on COUNT: any
///     entry besides the checkout itself trips the guard, dotfiles included.
///     The documented `mkdir alp && cd alp && git clone …` flow leaves the
///     parent holding literally one entry (the checkout), so it never trips
///     this; `$HOME` and `~/Downloads` always hold other entries, so they
///     always do.
///
/// Deliberately NOT a directory-name check (no `Downloads`/`Desktop`/…
/// list): a name list is locale-dependent and incomplete by construction —
/// see the issue for the worked argument. Nothing here even looks at a name
/// other than the checkout's own, and only to exclude it from the count.
pub fn parent_needs_workspace_guard(entries: &[String], checkout_name: &str) -> bool {
    if entries.iter().any(|entry| entry == ".west") {
        return false;
    }
    entries.iter().any(|entry| entry != checkout_name)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_parent_holding_only_the_checkout_never_guards() {
        // `mkdir alp && cd alp && git clone …` — the documented flow.
        assert!(!parent_needs_workspace_guard(
            &["alp-sdk".to_string()],
            "alp-sdk"
        ));
        // Empty is the same fact stated more strongly (the checkout itself
        // always shows up as an entry of its own parent in real use, but
        // nothing here should special-case that away).
        assert!(!parent_needs_workspace_guard(&[], "alp-sdk"));
    }

    #[test]
    fn a_parent_with_any_other_entry_guards_dotfiles_included() {
        assert!(parent_needs_workspace_guard(
            &["alp-sdk".to_string(), "Photos".to_string()],
            "alp-sdk"
        ));
        // A dotfile counts too — nothing here special-cases hidden entries;
        // a freshly `mkdir`ed dedicated directory has none to begin with.
        assert!(parent_needs_workspace_guard(
            &["alp-sdk".to_string(), ".DS_Store".to_string()],
            "alp-sdk"
        ));
        // $HOME / ~/Downloads: many unrelated entries, none named ".west".
        let home: Vec<String> = ["alp-sdk", ".bashrc", "Documents", "Downloads", "Music"]
            .iter()
            .map(|s| s.to_string())
            .collect();
        assert!(parent_needs_workspace_guard(&home, "alp-sdk"));
    }

    #[test]
    fn a_parent_already_holding_dot_west_never_guards_no_matter_what_else_is_there() {
        // A parent that is already a west topdir is already "an existing west
        // workspace" — every sibling entry is that workspace's own content.
        assert!(!parent_needs_workspace_guard(
            &[
                "alp-sdk".to_string(),
                ".west".to_string(),
                "zephyr".to_string(),
                "modules".to_string(),
                ".venv".to_string(),
                "tools".to_string(),
            ],
            "alp-sdk"
        ));
        // Even with content .west could never explain (an unrelated file
        // sitting alongside a real workspace) — the marker alone is enough,
        // by design: distinguishing "workspace clutter" from "unrelated
        // clutter" one level down is exactly the locale-dependent judgment
        // call this predicate refuses to make.
        assert!(!parent_needs_workspace_guard(
            &[
                "alp-sdk".to_string(),
                ".west".to_string(),
                "unrelated-photo.jpg".to_string(),
            ],
            "alp-sdk"
        ));
    }
}
