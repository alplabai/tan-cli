// SPDX-License-Identifier: Apache-2.0
//! Pure INI-shaped read/rewrite of a west `topdir/.west/config`'s
//! `[manifest] path` value.
//!
//! `west init -l <sdk_root>` sets `topdir = dirname(sdk_root)` and writes
//! `[manifest] path = <basename of sdk_root>` into `topdir/.west/config`. But
//! `bootstrap.sh`'s "already initialised" branch runs `west update` WITHOUT
//! re-running `west init -l` (see alp-sdk `scripts/bootstrap.sh`), so a
//! `.west/config` left over from a *different* SDK checkout under the same
//! topdir (e.g. `tan sdk switch` between two `~/.alp/sdk-cache/<version>`
//! entries) silently keeps pulling the stale SDK's `west.yml` manifest.
//! `tan bootstrap` (`crates/tan-cli/src/commands/bootstrap.rs`) reconciles
//! that divergence before shelling `bootstrap.sh`; the IO (read/canonicalize/
//! write) lives there, this module only has the section-scoped text logic.

/// Returns the `[manifest]` section's `path = ` value, or `None` when the
/// config has no `[manifest]` section or no `path` key inside it.
///
/// Section-scoped: a `path = ` line under a different section (`[zephyr]`, …)
/// is never returned.
pub fn get_manifest_path(config: &str) -> Option<String> {
    manifest_path_line(config).map(|(_, value)| value.to_string())
}

/// Returns `config` with the `[manifest]` section's `path` line rewritten to
/// `new_rel`, preserving every other line (including comments, blank lines,
/// other sections, and a same-named `path` key elsewhere) byte-for-byte —
/// each line's own terminator (`\r\n`, `\n`, or none for a final newline-less
/// line) survives untouched, so a CRLF `.west/config` stays CRLF. Returns
/// `None` when there is no `[manifest] path` line to replace.
pub fn set_manifest_path(config: &str, new_rel: &str) -> Option<String> {
    let mut section = String::new();
    let mut out = String::with_capacity(config.len() + new_rel.len());
    let mut rewrote = false;
    for segment in config.split_inclusive('\n') {
        let (content, terminator) = split_terminator(segment);
        if let Some(name) = section_header(content) {
            section = name.to_string();
        } else if !rewrote
            && section == "manifest"
            && key_value(content).is_some_and(|(key, _)| key == "path")
        {
            out.push_str("path = ");
            out.push_str(new_rel);
            out.push_str(terminator);
            rewrote = true;
            continue;
        }
        out.push_str(segment);
    }
    rewrote.then_some(out)
}

/// Splits a `split_inclusive('\n')` segment into `(content, terminator)`:
/// `"foo\r\n" -> ("foo", "\r\n")`, `"foo\n" -> ("foo", "\n")`, `"foo" ->
/// ("foo", "")` (the final, newline-less segment). `.lines()` alone strips
/// the terminator entirely, which is fine for reading but would silently
/// flip every line of a CRLF file to LF on a `set_manifest_path` rewrite.
fn split_terminator(segment: &str) -> (&str, &str) {
    if let Some(stripped) = segment.strip_suffix("\r\n") {
        (stripped, "\r\n")
    } else if let Some(stripped) = segment.strip_suffix('\n') {
        (stripped, "\n")
    } else {
        (segment, "")
    }
}

/// Locates the `[manifest]` section's `path = ` line: `(line index, trimmed
/// value)`. INI comment lines (`#`/`;`) are skipped; sections are tracked by
/// the most recent `[section]` header line seen.
fn manifest_path_line(config: &str) -> Option<(usize, &str)> {
    let mut section = "";
    for (idx, line) in config.lines().enumerate() {
        if let Some(name) = section_header(line) {
            section = name;
            continue;
        }
        if section != "manifest" {
            continue;
        }
        if let Some((key, value)) = key_value(line) {
            if key == "path" {
                return Some((idx, value));
            }
        }
    }
    None
}

/// `[section]` -> `Some("section")` (trimmed); anything else -> `None`.
fn section_header(line: &str) -> Option<&str> {
    let trimmed = line.trim();
    trimmed
        .strip_prefix('[')
        .and_then(|rest| rest.strip_suffix(']'))
        .map(str::trim)
}

/// `key = value` -> `Some(("key", "value"))`, both trimmed. `None` for blank
/// lines, comment lines (`#`/`;`), and lines with no `=`.
fn key_value(line: &str) -> Option<(&str, &str)> {
    let trimmed = line.trim_start();
    if trimmed.is_empty() || trimmed.starts_with('#') || trimmed.starts_with(';') {
        return None;
    }
    let eq = line.find('=')?;
    let key = line[..eq].trim();
    if key.is_empty() {
        return None;
    }
    Some((key, line[eq + 1..].trim()))
}

#[cfg(test)]
mod tests {
    use super::*;

    const CONFIG: &str = "[manifest]\npath = alp-sdk\nfile = west.yml\n[zephyr]\nbase = zephyr\n";

    #[test]
    fn get_returns_the_manifest_path() {
        assert_eq!(get_manifest_path(CONFIG), Some("alp-sdk".to_string()));
    }

    #[test]
    fn get_ignores_a_same_named_key_in_another_section() {
        let config = "[zephyr]\npath = should-not-count\n[manifest]\npath = alp-sdk\n";
        assert_eq!(get_manifest_path(config), Some("alp-sdk".to_string()));
    }

    #[test]
    fn get_is_none_without_a_manifest_section() {
        assert_eq!(get_manifest_path("[zephyr]\nbase = zephyr\n"), None);
    }

    #[test]
    fn set_rewrites_only_the_manifest_path_line() {
        let rewritten = set_manifest_path(CONFIG, "alp-sdk-v0.7.0").unwrap();
        assert_eq!(
            rewritten,
            "[manifest]\npath = alp-sdk-v0.7.0\nfile = west.yml\n[zephyr]\nbase = zephyr\n"
        );
    }

    #[test]
    fn set_leaves_a_same_named_path_under_another_section_untouched() {
        let config = "[manifest]\npath = old-sdk\n[zephyr]\npath = zephyr\n";
        let rewritten = set_manifest_path(config, "new-sdk").unwrap();
        assert_eq!(
            rewritten,
            "[manifest]\npath = new-sdk\n[zephyr]\npath = zephyr\n"
        );
    }

    #[test]
    fn set_returns_none_without_a_manifest_path_line() {
        assert_eq!(set_manifest_path("[zephyr]\nbase = zephyr\n", "x"), None);
        assert_eq!(
            set_manifest_path("[manifest]\nfile = west.yml\n", "x"),
            None
        );
    }

    #[test]
    fn round_trip_preserves_every_other_line() {
        let config = "# top comment\n[manifest]\n; a comment\npath = old\nfile = west.yml\n\n[zephyr]\nbase = zephyr\n";
        let rewritten = set_manifest_path(config, "new").unwrap();
        assert_eq!(
            rewritten,
            "# top comment\n[manifest]\n; a comment\npath = new\nfile = west.yml\n\n[zephyr]\nbase = zephyr\n"
        );
    }

    #[test]
    fn set_without_trailing_newline_does_not_add_one() {
        let rewritten = set_manifest_path("[manifest]\npath = old", "new").unwrap();
        assert_eq!(rewritten, "[manifest]\npath = new");
    }

    #[test]
    fn set_preserves_crlf_on_every_line_including_the_rewritten_one() {
        // A Windows-primary repo: `.west/config` commonly has `\r\n`.
        // `.lines()`-then-rejoin-with-`\n` would silently flip the whole
        // file to LF-only; the rewritten line must keep `\r\n` too.
        let config = "[manifest]\r\npath = old\r\nfile = west.yml\r\n";
        let rewritten = set_manifest_path(config, "new").unwrap();
        assert_eq!(rewritten, "[manifest]\r\npath = new\r\nfile = west.yml\r\n");
    }
}
