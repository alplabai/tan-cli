// SPDX-License-Identifier: Apache-2.0
//! Example catalog (pure metadata derivation for `alp examples`) and board.yaml
//! SoM retargeting.

/// Derive a stable, unique example id from its `category/name` source dir.
/// The source dir is already unique across the catalog, so it doubles as the id.
pub fn example_id_from_source_dir(source_dir: &str) -> String {
    source_dir.to_string()
}

/// A human-readable title for an example, taken from its README's first markdown
/// heading (`# Title`), falling back to a title-cased leaf name.
pub fn example_title_from_readme(readme: Option<&str>, source_dir: &str) -> String {
    if let Some(text) = readme {
        for line in text.lines() {
            let trimmed = line.trim();
            if let Some(heading) = trimmed.strip_prefix('#') {
                let title = heading.trim_start_matches('#').trim();
                if !title.is_empty() {
                    return title.to_string();
                }
            }
        }
    }
    title_case_leaf(source_dir)
}

/// A one-line description for an example: the README's first prose line —
/// skipping blank lines, headings, blockquotes, badges/links, HTML, tables, list
/// markers, and horizontal rules. Empty string when there is none.
pub fn example_description_from_readme(readme: Option<&str>) -> String {
    let Some(text) = readme else {
        return String::new();
    };
    text.lines()
        .map(str::trim)
        .find(|line| is_readme_prose(line))
        .unwrap_or_default()
        .to_string()
}

/// True if `line` reads as normal prose usable as a one-line description —
/// excluding markdown structure (headings, blockquotes, badges/links, HTML,
/// tables, list/rule markers).
fn is_readme_prose(line: &str) -> bool {
    match line.as_bytes().first() {
        None => false,
        Some(b) => !matches!(
            b,
            b'#' | b'>' | b'<' | b'[' | b'!' | b'|' | b'-' | b'*' | b'+' | b'=' | b'`'
        ),
    }
}

/// Title-case the leaf segment of a `category/name` source dir, splitting on `-`
/// and `_`: `audio/i2s-tone` -> `I2s Tone`.
fn title_case_leaf(source_dir: &str) -> String {
    let leaf = source_dir.rsplit('/').next().unwrap_or(source_dir);
    leaf.split(['-', '_'])
        .filter(|w| !w.is_empty())
        .map(|w| {
            let mut chars = w.chars();
            match chars.next() {
                Some(first) => first.to_uppercase().collect::<String>() + chars.as_str(),
                None => String::new(),
            }
        })
        .collect::<Vec<_>>()
        .join(" ")
}

/// Rewrite the `som.sku` value in a board.yaml's raw text to `sku`, preserving
/// comments and formatting. Finds the top-level `som:` block and replaces the
/// first `sku:` line under it (keeping its indentation and any trailing inline
/// comment). Returns the text unchanged if no `som.sku` line is found. Used to
/// retarget an example onto the user's chosen SoM (`alp init --from-example --som`).
pub fn retarget_board_yaml_som(content: &str, sku: &str) -> String {
    let mut lines: Vec<String> = Vec::new();
    let mut in_som = false;
    let mut done = false;
    for line in content.lines() {
        if !done {
            let trimmed = line.trim_start();
            let is_top_level = !line.is_empty() && !line.starts_with([' ', '\t']);
            if is_top_level {
                // A new top-level key: entering `som:`, or leaving it.
                in_som = trimmed.starts_with("som:");
            } else if in_som && trimmed.starts_with("sku:") {
                // Replace ONLY the value token after `sku:`, leaving the rest
                // of the line -- the gap before a trailing comment included
                // -- byte-for-byte untouched. A prior version reconstructed
                // the whole tail as a fixed two-space gap + comment, which
                // silently collapsed a column-aligned inline comment (e.g.
                // `sku: E1M-AEN801           # ...`) even when `sku` was a
                // byte-exact no-op -- exactly the vendored `iot` scaffold's
                // som.sku line.
                let indent = &line[..line.len() - trimmed.len()];
                let after_key = &trimmed["sku:".len()..];
                let ws_len = after_key.len() - after_key.trim_start().len();
                let leading_ws = &after_key[..ws_len];
                let after_ws = &after_key[ws_len..];
                if after_ws.is_empty() || after_ws.starts_with('#') {
                    // No value token to replace -- `sku:` with nothing after
                    // it, or `sku:  # comment` with the comment but no value.
                    // Splicing at a fixed "first whitespace run" position
                    // here would either glue the value onto `sku:` with no
                    // separating space (read back as a scalar, not a mapping
                    // entry) or eat the `#` into the value. Insert the value
                    // with a single space and leave the rest of the line
                    // (any comment, and its gap) exactly as found.
                    lines.push(format!("{indent}sku: {sku}{after_key}"));
                } else {
                    let value_len = after_ws.find(char::is_whitespace).unwrap_or(after_ws.len());
                    let tail = &after_ws[value_len..];
                    lines.push(format!("{indent}sku:{leading_ws}{sku}{tail}"));
                }
                done = true;
                continue;
            }
        }
        lines.push(line.to_string());
    }
    let mut result = lines.join("\n");
    if content.ends_with('\n') {
        result.push('\n');
    }
    result
}

#[cfg(test)]
mod example_catalog_tests {
    use super::{
        example_description_from_readme, example_id_from_source_dir, example_title_from_readme,
    };

    #[test]
    fn title_from_readme_prefers_first_heading() {
        assert_eq!(
            example_title_from_readme(Some("# I2S Tone\n\nbody"), "audio/i2s-tone"),
            "I2S Tone"
        );
        assert_eq!(example_title_from_readme(Some("## Sub\n"), "a/b"), "Sub");
    }

    #[test]
    fn title_falls_back_to_title_cased_leaf() {
        assert_eq!(
            example_title_from_readme(None, "audio/i2s-tone"),
            "I2s Tone"
        );
        assert_eq!(
            example_title_from_readme(Some("no heading here"), "peripheral-io/uart-echo"),
            "Uart Echo"
        );
    }

    #[test]
    fn description_skips_headings_blockquotes_and_badges() {
        let readme = "# Title\n\n> untested\n\n[![badge](x)]\n\nReads and plays a tone.";
        assert_eq!(
            example_description_from_readme(Some(readme)),
            "Reads and plays a tone."
        );
        assert_eq!(example_description_from_readme(None), "");
    }

    #[test]
    fn id_equals_source_dir() {
        assert_eq!(
            example_id_from_source_dir("audio/i2s-tone"),
            "audio/i2s-tone"
        );
    }

    #[test]
    fn retarget_som_rewrites_only_the_som_sku() {
        let src = "# header\nsom:\n  sku: E1M-AEN701\npreset: e1m-evk\n";
        assert_eq!(
            super::retarget_board_yaml_som(src, "E1M-AEN801"),
            "# header\nsom:\n  sku: E1M-AEN801\npreset: e1m-evk\n"
        );
        // No som.sku → unchanged.
        assert_eq!(
            super::retarget_board_yaml_som("foo: bar\n", "X"),
            "foo: bar\n"
        );
    }

    #[test]
    fn retarget_som_preserves_a_column_aligned_trailing_comment_when_the_sku_is_unchanged() {
        // Regression: a prior version reconstructed the sku line's tail as a
        // fixed two-space gap + comment, so a byte-exact no-op (retargeting
        // onto the SAME sku) still silently collapsed a column-aligned
        // inline comment -- exactly what the vendored `iot` scaffold's
        // `som.sku` line has.
        let src = "som:\n  sku: E1M-AEN801           # Alif Ensemble E8\npreset: e1m-evk\n";
        assert_eq!(super::retarget_board_yaml_som(src, "E1M-AEN801"), src);
    }

    #[test]
    fn retarget_som_inserts_a_separating_space_for_a_valueless_sku_line() {
        // Regression: a valueless `sku:` line has no whitespace run after the
        // key, so the value-token splice used to glue the replacement
        // directly onto `sku:` with no space -- `sku:E1M-AEN801` reads back
        // as a plain YAML scalar, not a `sku` mapping entry.
        let src = "som:\n  sku:\npreset: e1m-evk\n";
        assert_eq!(
            super::retarget_board_yaml_som(src, "E1M-AEN801"),
            "som:\n  sku: E1M-AEN801\npreset: e1m-evk\n"
        );
    }

    #[test]
    fn retarget_som_preserves_a_comment_on_a_valueless_sku_line() {
        // Regression: with no value before the comment, the splice's
        // "first whitespace run" search matched inside the comment text
        // itself, so the leading `#` was overwritten by the new value and
        // the rest of the comment text rode along after it.
        let src = "som:\n  sku:  # TODO: fill in\npreset: e1m-evk\n";
        assert_eq!(
            super::retarget_board_yaml_som(src, "E1M-AEN801"),
            "som:\n  sku: E1M-AEN801  # TODO: fill in\npreset: e1m-evk\n"
        );
    }
}
