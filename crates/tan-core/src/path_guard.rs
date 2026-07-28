// SPDX-License-Identifier: Apache-2.0
//! Pure path-shape guards for every consumer that lets *external data* name a
//! path it will then write to, remove, or archive.
//!
//! Tolerant reading and validated acting are different policies. The build-plan
//! and system-manifest parsers are deliberately lenient (correct for reading),
//! but their strings flow into `remove_dir_all`, `File::create` and archive
//! entry names. The shape check belongs here — once, at the seam — not
//! re-derived at each call site, which is how the same guard came to be applied
//! to `sharedArtefacts` but not to `cwd`, and to `--from-example` but not to
//! `--name`.

use std::path::{Component, Path, PathBuf};

/// Lexically normalize a path (collapse `.` and `..`) without touching the
/// filesystem.
pub fn normalize(path: &Path) -> PathBuf {
    let mut out = PathBuf::new();
    for component in path.components() {
        match component {
            Component::CurDir => {}
            Component::ParentDir => {
                out.pop();
            }
            other => out.push(other.as_os_str()),
        }
    }
    out
}

/// True when `path` is safe to `join` onto a trusted base: relative, and built
/// only from ordinary names.
///
/// `Path::is_absolute()` is NOT this check. On Windows it is
/// `has_root() && prefix().is_some()`, so it returns **false** for `/x`, `\x`
/// and `C:x` — none of which contain a `ParentDir` component either. Every one
/// of them makes `base.join(p)` discard part or all of `base`
/// ([`PathBuf::push`] documents this), so a guard written as
/// `is_absolute() || has ParentDir` lets a path escape the tree it was supposed
/// to stay inside — on the one platform where nothing tested it.
///
/// An empty path and a bare `.` are rejected too: both make `base.join(p)`
/// resolve to `base` itself, so a caller that means "a file under base" would
/// instead act on base.
pub fn is_plain_relative(path: &Path) -> bool {
    let mut components = path.components().peekable();
    if components.peek().is_none() {
        return false; // "" and "." both yield no components
    }
    components.all(|c| matches!(c, Component::Normal(_)))
}

/// Resolve a user-supplied `--workspace <path>` value against `cwd` into an
/// absolute, textually-normalized directory — validation only: no filesystem
/// writes, and no requirement that the directory exist yet.
///
/// `Err` for:
///   * an empty/whitespace-only value — `--workspace ""`, the classic
///     unset-`$WS`-variable shell accident, must never silently resolve to
///     `cwd` itself. The caller (`tan bootstrap`'s workspace-parent guard,
///     tan-cli#185) renames a customer's git checkout onto the result, so a
///     wrong guess here is not a recoverable mistake the way a misread would
///     be;
///   * a value that `has_root()` but is not `is_absolute()` — on Windows
///     that is `/x` or `\x` (see [`is_plain_relative`]'s doc for why
///     `is_absolute()` alone under-detects this). An MSYS-style `/e/foo/ws`
///     silently resolved to whichever drive the process happened to be
///     running from — proven live to move a checkout to a drive nobody
///     named. Refused outright rather than guessed at.
///
/// A genuinely relative value (`ws`, `../ws`) is joined onto `cwd`; an
/// already-absolute one (`C:\ws`, `/opt/ws`) is normalized and returned as
/// given.
pub fn resolve_workspace_target(raw: &str, cwd: &Path) -> Result<PathBuf, String> {
    let trimmed = raw.trim();
    if trimmed.is_empty() {
        return Err("--workspace requires a non-empty path".to_string());
    }
    let candidate = Path::new(trimmed);
    if candidate.is_absolute() {
        return Ok(normalize(candidate));
    }
    if candidate.has_root() {
        return Err(format!(
            "--workspace '{trimmed}' has a root but no drive, which is ambiguous on this host \
             (it would resolve against whichever drive the process happens to be running \
             from); pass a full absolute path instead"
        ));
    }
    Ok(normalize(&cwd.join(candidate)))
}

/// True when recursively removing `target` would take out far more than a build
/// tree, and the caller must refuse.
///
/// Rejects, on the normalized path:
///   * a filesystem or drive root (`/`, `C:\`, `\\server\share`) — no `Normal`
///     component at all;
///   * the project root itself, or any ancestor of it.
///
/// Deliberately does NOT require containment under the project root: a slice
/// that builds out of tree (a Yocto tmp dir at `/var/tmp/yocto`) is a supported
/// clean target. The rule is "not catastrophic", not "not outside".
pub fn is_unsafe_removal_target(project_root: &Path, target: &Path) -> bool {
    let target = normalize(target);
    if !target
        .components()
        .any(|c| matches!(c, Component::Normal(_)))
    {
        return true;
    }
    // `starts_with` on the project root is true when target IS the project root
    // or an ancestor of it — both would delete the user's sources.
    normalize(project_root).starts_with(&target)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn plain_relative_accepts_ordinary_paths() {
        assert!(is_plain_relative(Path::new("build/zephyr/zephyr.elf")));
        assert!(is_plain_relative(Path::new("alp.conf")));
    }

    #[test]
    fn plain_relative_rejects_every_escaping_shape() {
        // The `..` case — the only one the old guards covered.
        assert!(!is_plain_relative(Path::new("../x")));
        assert!(!is_plain_relative(Path::new("a/../../x")));
        // Posix-absolute. `is_absolute()` is FALSE for this on Windows.
        assert!(!is_plain_relative(Path::new("/etc/passwd")));
        // Empty and bare `.` both resolve `base.join(p)` back to `base`.
        assert!(!is_plain_relative(Path::new("")));
        assert!(!is_plain_relative(Path::new(".")));
    }

    #[cfg(windows)]
    #[test]
    fn plain_relative_rejects_windows_rooted_and_drive_relative() {
        for raw in [r"\windows\system32", r"C:foo"] {
            // Pins the premise of the defect: these are NOT `is_absolute()`,
            // which is exactly why the old `is_absolute() || ParentDir` guard
            // let them through — while `join` still discards the base.
            assert!(!Path::new(raw).is_absolute(), "{raw} unexpectedly absolute");
            assert!(!is_plain_relative(Path::new(raw)), "{raw} must be rejected");
            // ...and this is what makes it dangerous: the join escapes the base.
            assert!(
                !Path::new(r"E:\proj").join(raw).starts_with(r"E:\proj"),
                "{raw} unexpectedly nested under the base"
            );
        }
        assert!(!is_plain_relative(Path::new(r"C:\windows")));
    }

    #[test]
    fn workspace_target_rejects_empty_or_whitespace() {
        assert!(resolve_workspace_target("", Path::new("/cwd")).is_err());
        assert!(resolve_workspace_target("   ", Path::new("/cwd")).is_err());
    }

    #[cfg(not(windows))]
    #[test]
    fn workspace_target_resolves_relative_and_absolute_values_posix() {
        assert_eq!(
            resolve_workspace_target("ws", Path::new("/home/u")).unwrap(),
            PathBuf::from("/home/u/ws")
        );
        assert_eq!(
            resolve_workspace_target("../ws", Path::new("/home/u/proj")).unwrap(),
            PathBuf::from("/home/u/ws")
        );
        assert_eq!(
            resolve_workspace_target("/opt/ws", Path::new("/home/u")).unwrap(),
            PathBuf::from("/opt/ws")
        );
    }

    #[cfg(windows)]
    #[test]
    fn workspace_target_resolves_relative_and_absolute_values_windows() {
        assert_eq!(
            resolve_workspace_target("ws", Path::new(r"E:\home\u")).unwrap(),
            PathBuf::from(r"E:\home\u\ws")
        );
        assert_eq!(
            resolve_workspace_target(r"C:\opt\ws", Path::new(r"E:\home\u")).unwrap(),
            PathBuf::from(r"C:\opt\ws")
        );
    }

    #[cfg(windows)]
    #[test]
    fn workspace_target_rejects_a_drive_relative_root_rather_than_guessing() {
        // The proven bug: `/e/foo/ws` is not `is_absolute()` on Windows, so it
        // would otherwise resolve against whichever drive the process happens
        // to run from -- silently moving the checkout to the wrong place.
        assert!(!Path::new("/e/foo/ws").is_absolute());
        assert!(resolve_workspace_target("/e/foo/ws", Path::new(r"E:\cwd")).is_err());
        assert!(resolve_workspace_target(r"\temp\ws", Path::new(r"E:\cwd")).is_err());
    }

    #[test]
    fn unsafe_removal_rejects_project_root_and_ancestors() {
        let project = Path::new("/home/u/myapp");
        // The empty and `.` manifest values, once joined, ARE the project root.
        assert!(is_unsafe_removal_target(
            project,
            &project.join("") // == /home/u/myapp
        ));
        assert!(is_unsafe_removal_target(project, &project.join(".")));
        assert!(is_unsafe_removal_target(
            project,
            Path::new("/home/u/myapp")
        ));
        assert!(is_unsafe_removal_target(project, Path::new("/home/u")));
        assert!(is_unsafe_removal_target(project, Path::new("/home")));
        // `../../..` from the project root climbs to an ancestor.
        assert!(is_unsafe_removal_target(project, &project.join("../../..")));
    }

    #[test]
    fn unsafe_removal_rejects_filesystem_roots() {
        assert!(is_unsafe_removal_target(Path::new("/p"), Path::new("/")));
        assert!(is_unsafe_removal_target(
            Path::new("/p"),
            Path::new("/var/..")
        ));
    }

    #[test]
    fn unsafe_removal_allows_real_build_trees() {
        let project = Path::new("/home/u/myapp");
        assert!(!is_unsafe_removal_target(project, &project.join("build")));
        // Out-of-tree slice build dirs stay supported — the rule is
        // "not catastrophic", not "not outside".
        assert!(!is_unsafe_removal_target(
            project,
            Path::new("/var/tmp/yocto")
        ));
        // A sibling project is outside but not an ancestor: allowed.
        assert!(!is_unsafe_removal_target(
            project,
            Path::new("/home/u/other/build")
        ));
    }
}
