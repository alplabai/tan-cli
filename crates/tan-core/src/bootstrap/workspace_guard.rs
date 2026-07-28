// SPDX-License-Identifier: Apache-2.0
//! The workspace-parent guard (tan-cli#185): whether `tan bootstrap` may
//! write `zephyr/`, `modules/`, `.west/` and the venv into the checkout's
//! parent directory without asking first.
//!
//! `west init -l <alp-sdk>` forces the west topdir to be the checkout's
//! PARENT (#769) — that placement is not tan's to change. What IS tan's to
//! decide is whether that parent is a safe place to spray a multi-gigabyte
//! tree into unannounced. This module is the pure predicate; the directory
//! listing that feeds it, the `.west` type check, the interactive prompt, and
//! the actual move all stay IO and live in `tan-cli`'s `commands::bootstrap`.

use std::path::Path;

/// The dedicated subdirectory `tan bootstrap` offers to relocate the checkout
/// into when the guard fires and the customer accepts (or `--workspace` names
/// nothing more specific): `<parent>/alp-workspace/<checkout-name>`. Not a
/// detection heuristic — the guard never keys off a directory NAME to decide
/// whether to fire (see [`parent_needs_workspace_guard`]) — this is only the
/// name tan itself chooses for the new home it builds, so the log line and
/// the actual `mkdir` can never drift apart.
pub const DEFAULT_WORKSPACE_DIR_NAME: &str = "alp-workspace";

/// The top-level entry name `venv.dirName` produces in the parent's listing —
/// its first path component. `venv.dirName` may nest the venv (e.g.
/// `tools/.venv`, tan-cli#161's manifest-escape test fixture), in which case
/// only `tools` ever shows up one level down; the venv's own path never does.
fn venv_top_level_entry(venv_dir_name: &str) -> Option<String> {
    Path::new(venv_dir_name)
        .components()
        .next()
        .map(|c| c.as_os_str().to_string_lossy().into_owned())
}

/// Whether the checkout's parent directory needs the workspace-parent guard.
///
/// The trigger, verbatim (tan-cli#185, refined by the #185 review): proceed
/// silently when the parent contains NOTHING BUT the checkout itself,
/// bootstrap's OWN venv, and/or an existing west workspace; otherwise guard.
/// Concretely:
///
///   - `entries` is the parent's DIRECT children only (one level, not
///     recursive) — what an existing west workspace looks like one level
///     down is out of scope; only whether `.west` is one of those children
///     matters.
///   - `dot_west_is_workspace` is a TYPED fact the caller computes with a
///     filesystem check (`<parent>/.west/config` readable), never inferred
///     from `entries` containing the literal string `".west"`: a plain FILE
///     or an empty directory happens to be named `.west` is not a workspace,
///     and letting the NAME alone answer that question was a false PROCEED —
///     `west init` itself then refused the very content the guard had waved
///     through. When `dot_west_is_workspace` is true, `west init -l` already
///     wrote `<topdir>/.west/config` as its very first act, so every OTHER
///     entry there (`zephyr`, `modules`, `tools`, the venv dir, a stray
///     `.venv-old`, …) is that workspace's own content, not "unrelated
///     content" the guard exists to catch — sufficient on its own; nothing
///     else in `entries` is even inspected in that case. When it is false, an
///     entry literally named `.west` is judged like any other foreign entry.
///   - `venv_dir_name` (the manifest's `venv.dirName`, e.g. `.venv`) is
///     bootstrap's OWN write, not foreign content: `ensure_venv` can create
///     it before `west init` ever writes `.west` (a network drop mid pip
///     install, or a `--no-west` run), and a retry over that exact state must
///     reach the venv-recovery path (`steps::ensure_venv`), not this guard.
///     Its top-level entry (see [`venv_top_level_entry`]) is excluded from
///     the count the same way the checkout itself is.
///   - Otherwise, the parent is judged purely on COUNT: any entry besides the
///     checkout and the venv trips the guard, dotfiles included. The
///     documented `mkdir alp && cd alp && git clone …` flow leaves the parent
///     holding literally one entry (the checkout), so it never trips this;
///     `$HOME` and `~/Downloads` always hold other entries, so they always
///     do.
///
/// Deliberately NOT a directory-name check (no `Downloads`/`Desktop`/…
/// list): a name list is locale-dependent and incomplete by construction —
/// see the issue for the worked argument. Nothing here even looks at a name
/// other than the checkout's and the venv's own, and only to exclude them
/// from the count.
pub fn parent_needs_workspace_guard(
    entries: &[String],
    checkout_name: &str,
    venv_dir_name: &str,
    dot_west_is_workspace: bool,
) -> bool {
    if dot_west_is_workspace {
        return false;
    }
    let venv_top_level = venv_top_level_entry(venv_dir_name);
    entries
        .iter()
        .any(|entry| entry != checkout_name && Some(entry.as_str()) != venv_top_level.as_deref())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_parent_holding_only_the_checkout_never_guards() {
        // `mkdir alp && cd alp && git clone …` — the documented flow.
        assert!(!parent_needs_workspace_guard(
            &["alp-sdk".to_string()],
            "alp-sdk",
            ".venv",
            false
        ));
        // Empty is the same fact stated more strongly (the checkout itself
        // always shows up as an entry of its own parent in real use, but
        // nothing here should special-case that away).
        assert!(!parent_needs_workspace_guard(
            &[],
            "alp-sdk",
            ".venv",
            false
        ));
    }

    #[test]
    fn a_parent_with_any_other_entry_guards_dotfiles_included() {
        assert!(parent_needs_workspace_guard(
            &["alp-sdk".to_string(), "Photos".to_string()],
            "alp-sdk",
            ".venv",
            false
        ));
        // A dotfile counts too — nothing here special-cases hidden entries;
        // a freshly `mkdir`ed dedicated directory has none to begin with.
        assert!(parent_needs_workspace_guard(
            &["alp-sdk".to_string(), ".DS_Store".to_string()],
            "alp-sdk",
            ".venv",
            false
        ));
        // $HOME / ~/Downloads: many unrelated entries, none named ".west".
        let home: Vec<String> = ["alp-sdk", ".bashrc", "Documents", "Downloads", "Music"]
            .iter()
            .map(|s| s.to_string())
            .collect();
        assert!(parent_needs_workspace_guard(
            &home, "alp-sdk", ".venv", false
        ));
    }

    #[test]
    fn a_parent_confirmed_a_real_west_workspace_never_guards_no_matter_what_else_is_there() {
        // A parent that is already a west topdir (the caller has ALREADY
        // confirmed `.west/config` is readable) is already "an existing west
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
            "alp-sdk",
            ".venv",
            true,
        ));
        // Even with content .west could never explain (an unrelated file
        // sitting alongside a real workspace) — the CONFIRMED marker alone is
        // enough, by design: distinguishing "workspace clutter" from
        // "unrelated clutter" one level down is exactly the locale-dependent
        // judgment call this predicate refuses to make.
        assert!(!parent_needs_workspace_guard(
            &[
                "alp-sdk".to_string(),
                ".west".to_string(),
                "unrelated-photo.jpg".to_string(),
            ],
            "alp-sdk",
            ".venv",
            true,
        ));
    }

    #[test]
    fn a_dot_west_entry_that_is_not_a_confirmed_workspace_guards_like_any_other_entry() {
        // The false PROCEED this predicate used to have: a plain FILE (or an
        // empty directory with no readable config) happens to be named
        // `.west`. The caller could not confirm it, so `dot_west_is_workspace`
        // is false, and the name is judged like any other foreign entry.
        assert!(parent_needs_workspace_guard(
            &["alp-sdk".to_string(), ".west".to_string()],
            "alp-sdk",
            ".venv",
            false,
        ));
    }

    #[test]
    fn bootstraps_own_venv_never_guards_even_before_dot_west_exists() {
        // The false GUARD: a bootstrap that created the venv but died before
        // `west init` ever wrote `.west` (a network drop mid pip install, or
        // `--no-west`) must not be refused on retry over its own state.
        assert!(!parent_needs_workspace_guard(
            &["alp-sdk".to_string(), ".venv".to_string()],
            "alp-sdk",
            ".venv",
            false,
        ));
        // A NESTED venv.dirName only ever shows up one level down as its
        // first component.
        assert!(!parent_needs_workspace_guard(
            &["alp-sdk".to_string(), "tools".to_string()],
            "alp-sdk",
            "tools/.venv",
            false,
        ));
        // A genuinely foreign entry still guards even with a venv present.
        assert!(parent_needs_workspace_guard(
            &[
                "alp-sdk".to_string(),
                ".venv".to_string(),
                "taxes.pdf".to_string(),
            ],
            "alp-sdk",
            ".venv",
            false,
        ));
    }
}
