// SPDX-License-Identifier: Apache-2.0
//! Locate WHERE inside a launch.json's own raw bytes a single `configurations`
//! entry lives, so a write can splice just that entry in place of parsing the
//! whole document into a `Value` and re-serializing it (tan-cli#182).
//!
//! `debug_launch::parse_launch_json_or_default` already answers "what does
//! this file mean" (via `strip_jsonc` + `serde_json`) — that pass is unchanged
//! and still owns validation. This module answers a narrower question: "where,
//! in the ORIGINAL text, do the bytes for element `N` of the top-level
//! `configurations` array begin and end". Scanning is intentionally a mirror
//! of `strip_jsonc`'s string/comment tracking (same `//`, `/* */`, and
//! in-string rules) so the two never disagree about what counts as structure —
//! but nothing here builds a `Value`; it only ever returns byte offsets into
//! the caller's own string.
//!
//! The payoff: a caller that copies `original[..start]` and `original[end..]`
//! verbatim around a replacement, or inserts new bytes at a single point for
//! an append, never touches a comment, a trailing comma, or a leading BOM
//! that sits outside the edited span — there is no re-serialization step for
//! those bytes to survive.
//!
//! [`locate_configuration_edit`] returns `None` whenever the raw text doesn't
//! confidently resolve to a `"configurations": [ ... ]` array (missing key,
//! wrong shape, or any scan mismatch) — the caller's documented fallback is
//! the old whole-document re-serialize, which is always safe (if lossy of
//! comments), so a locator miss degrades gracefully rather than risking a
//! malformed write.

use serde_json::Value;

/// Where to make the edit, expressed as byte offsets into the ORIGINAL text.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum SpliceEdit {
    /// Replace the existing configuration object spanning `[start, end)`
    /// (`start` is the byte offset of its `{`; `end` is one past its matching
    /// `}`) with the new entry, reindented using `indent`.
    Replace {
        start: usize,
        end: usize,
        indent: String,
    },
    /// Insert a new configuration entry immediately after byte offset `after`
    /// (either the end of the array's last element, or the position right
    /// after its `[` when the array is empty). `needs_leading_comma` is
    /// `true` when the array already has an element there that isn't
    /// followed by a trailing comma.
    Append {
        after: usize,
        needs_leading_comma: bool,
        indent: String,
        /// Set only when appending into an array collapsed onto one line
        /// (`"configurations": []` — VS Code's own stock template, the
        /// single most common real input). `original` supplies no line break
        /// of its own before the `]` in that case, so without this the
        /// freshly appended entry's own trailing newline drags the `]` down
        /// to column 0 instead of leaving it aligned with the array's own
        /// line (tan-cli#182 review finding #5). `None` for every other
        /// append site, where `original` already carries the closing
        /// bracket's formatting and nothing extra is needed.
        closing_indent: Option<String>,
    },
}

/// Find where the `index`-th configuration entry lives in `original`'s raw
/// bytes (`index = None` means "no match — append a fresh entry instead").
///
/// `index` counts ONLY object-shaped elements of the top-level
/// `configurations` array, in order — the exact same filter
/// `parse_launch_json_or_default` applies before handing the array to
/// `create_launch_json_write_plan`, so an index computed against that
/// filtered `Value` array can be passed straight through here.
pub fn locate_configuration_edit(original: &str, index: Option<usize>) -> Option<SpliceEdit> {
    let s = Scanner::new(original);
    let array_open = find_configurations_array(&s)?;

    let mut cursor = array_open + 1;
    let mut object_spans: Vec<(usize, usize)> = Vec::new();
    let mut last_element_end: Option<usize> = None;
    // Whether the LAST element seen was followed by a comma before `]`, and
    // (when so) the token position right after that comma — the existing
    // comma already separates "the last element" from "whatever comes next",
    // so appending there reuses it as our own leading separator instead of
    // emitting a second one right after it (`,,`).
    let mut trailing_comma = false;
    let mut after_trailing_comma = 0usize;
    loop {
        cursor = s.skip_ws_and_comments(cursor);
        if s.char_at(cursor) == Some(']') {
            break;
        }
        let elem_start = cursor;
        let is_object = s.char_at(cursor) == Some('{');
        let elem_end = s.skip_value(cursor)?;
        if is_object {
            object_spans.push((elem_start, elem_end));
        }
        last_element_end = Some(elem_end);
        cursor = s.skip_ws_and_comments(elem_end);
        match s.char_at(cursor) {
            Some(',') => {
                cursor += 1;
                // Provisional: disproved below if another element follows —
                // the NEXT iteration's own comma-or-']' check overwrites both
                // of these before they are ever read again.
                trailing_comma = true;
                after_trailing_comma = cursor;
            }
            Some(']') => {
                trailing_comma = false;
                break;
            }
            _ => return None,
        }
    }

    match index {
        Some(i) => {
            let &(start_tok, end_tok) = object_spans.get(i)?;
            let start = s.byte_at(start_tok);
            let end = s.byte_at(end_tok);
            let indent = indent_before(original, start);
            Some(SpliceEdit::Replace { start, end, indent })
        }
        None => match last_element_end {
            Some(end_tok) => {
                // A pre-existing trailing comma already separates the last
                // element from our new one, so insert AFTER it (not before —
                // inserting before would leave the two elements adjacent
                // with no separator, and strand the comma as a second,
                // now-orphaned trailing comma of its own); otherwise insert
                // right after the element and supply the separator ourselves.
                let (after_tok, needs_leading_comma) = if trailing_comma {
                    (after_trailing_comma, false)
                } else {
                    (end_tok, true)
                };
                let after = s.byte_at(after_tok);
                let indent = object_spans
                    .last()
                    .map(|&(os, _)| indent_before(original, s.byte_at(os)))
                    .unwrap_or_else(default_indent);
                Some(SpliceEdit::Append {
                    after,
                    needs_leading_comma,
                    indent,
                    closing_indent: None,
                })
            }
            None => {
                // Empty array: insert right after the `[`. Indent one level
                // deeper than the array's OWN line (not the flat default),
                // so the entry lines up under whatever indentation width the
                // rest of the file already uses.
                let after = s.byte_at(array_open + 1);
                let array_line_indent = indent_before(original, s.byte_at(array_open));
                let entry_indent = format!("{array_line_indent}    ");
                // `cursor` still holds the position of the closing `]` (the
                // loop above broke on it before ever entering the element
                // branch): collapsed onto one line (`[]`, no whitespace
                // between the brackets) means `original` supplies no line
                // break of its own for `]` to land on, so supply the array's
                // own indent for it explicitly. An already-multi-line empty
                // array (`[\n]`) keeps its own existing formatting untouched.
                let closing_indent = (cursor == array_open + 1).then_some(array_line_indent);
                Some(SpliceEdit::Append {
                    after,
                    needs_leading_comma: false,
                    indent: entry_indent,
                    closing_indent,
                })
            }
        },
    }
}

/// Apply a located edit to `original`, inserting `entry` (pretty-printed and
/// reindented to match where it lands) and returning the full new document.
/// Every byte of `original` outside the edited span is copied through
/// unchanged — that is the whole point: no re-serialization pass runs over
/// them, so a comment, a trailing comma, or a BOM sitting anywhere else in
/// the file survives byte-for-byte.
pub fn apply_edit(original: &str, edit: &SpliceEdit, entry: &Value) -> String {
    let pretty = serde_json::to_string_pretty(entry).expect("configuration entry is serializable");
    let eol = dominant_eol(original);
    match edit {
        SpliceEdit::Replace { start, end, indent } => {
            let mut out = String::with_capacity(original.len() + pretty.len());
            out.push_str(&original[..*start]);
            out.push_str(&reindent(&pretty, indent, eol));
            out.push_str(&original[*end..]);
            out
        }
        SpliceEdit::Append {
            after,
            needs_leading_comma,
            indent,
            closing_indent,
        } => {
            let mut out = String::with_capacity(original.len() + pretty.len() + indent.len() + 8);
            out.push_str(&original[..*after]);
            if *needs_leading_comma {
                out.push(',');
            }
            out.push_str(eol);
            out.push_str(indent);
            out.push_str(&reindent(&pretty, indent, eol));
            out.push_str(eol);
            if let Some(closing_indent) = closing_indent {
                out.push_str(closing_indent);
            }
            out.push_str(&original[*after..]);
            out
        }
    }
}

/// The line ending already dominant in `original` — `\r\n` if it contains at
/// least one, `\n` otherwise — so a spliced-in entry's own newlines match its
/// neighbours instead of leaving a mixed-EOL file behind on a CRLF-authored
/// (Windows-default) `launch.json` (tan-cli#182 review finding #4).
/// `serde_json::to_string_pretty` and this module's own literals are
/// otherwise LF-only regardless of platform.
fn dominant_eol(original: &str) -> &'static str {
    if original.contains("\r\n") {
        "\r\n"
    } else {
        "\n"
    }
}

/// serde_json's own pretty printer starts every nested line 2 spaces deeper
/// than its parent; a brand-new array element one level under
/// `"configurations": [` (itself one level under the document root) lands at
/// 4 spaces with no existing sibling to copy a style from.
fn default_indent() -> String {
    "    ".to_string()
}

/// Re-prefix every line of `pretty` AFTER the first with `indent`, joined
/// with `eol` rather than a bare `\n`, so a block that
/// `serde_json::to_string_pretty` rendered LF-only and starting at column 0
/// nests correctly, in the file's own line-ending style, wherever it is
/// spliced in. The first line is left alone: for a [`SpliceEdit::Replace`] it
/// takes the position of the old `{`, which already sits after whatever
/// indentation preceded it in `original`; for an [`SpliceEdit::Append`] the
/// caller pushes `indent` itself before calling.
fn reindent(pretty: &str, indent: &str, eol: &str) -> String {
    let mut lines = pretty.lines();
    let mut out = String::from(lines.next().unwrap_or(""));
    for line in lines {
        out.push_str(eol);
        out.push_str(indent);
        out.push_str(line);
    }
    out
}

/// The whitespace-only run from the start of `text`'s line containing
/// `byte_pos` up to `byte_pos` itself, if it IS whitespace-only; otherwise the
/// [`default_indent`]. Used to match a new/replaced entry's indentation to
/// its neighbours instead of always falling back to the default.
fn indent_before(text: &str, byte_pos: usize) -> String {
    let before = &text[..byte_pos];
    let line_start = before.rfind('\n').map(|i| i + 1).unwrap_or(0);
    let candidate = &before[line_start..];
    if !candidate.is_empty() && candidate.chars().all(|c| c == ' ' || c == '\t') {
        candidate.to_string()
    } else {
        default_indent()
    }
}

/// Scan the top-level object for a `"configurations"` key and return the
/// token index of its value's opening `[`, or `None` if the key is absent, is
/// not the very next thing after `:`, or the document doesn't open with `{`
/// at all (every case the caller treats as "fall back to full re-serialize").
///
/// A SECOND top-level `"configurations"` key also returns `None`: JSON
/// doesn't forbid a duplicate key, and `serde_json` (like VS Code's own
/// `jsonc-parser`) resolves one to its LAST occurrence, but this scan would
/// otherwise hand back the FIRST — splicing into the array nothing downstream
/// actually reads, while `existing_index` (computed against the parsed,
/// last-wins `Value`) silently addresses the other one (tan-cli#182 review
/// finding #3). Bailing here routes a document with this pre-existing defect
/// through the same safe whole-document fallback every other unrecognised
/// shape already takes, rather than guessing which array is authoritative.
fn find_configurations_array(s: &Scanner<'_>) -> Option<usize> {
    let mut p = s.skip_ws_and_comments(0);
    if s.char_at(p) != Some('{') {
        return None;
    }
    p += 1;
    let mut found: Option<usize> = None;
    loop {
        p = s.skip_ws_and_comments(p);
        match s.char_at(p) {
            Some('}') => return found,
            Some(',') => {
                p += 1;
            }
            Some('"') => {
                let key_start = p;
                let key_end = s.skip_string(p)?;
                let key_text = s.slice(key_start + 1, key_end - 1);
                p = s.skip_ws_and_comments(key_end);
                if s.char_at(p) != Some(':') {
                    return None;
                }
                p = s.skip_ws_and_comments(p + 1);
                if key_text == "configurations" {
                    if found.is_some() {
                        return None;
                    }
                    if s.char_at(p) != Some('[') {
                        return None;
                    }
                    found = Some(p);
                }
                p = s.skip_value(p)?;
            }
            _ => return None,
        }
    }
}

/// Char-indexed view over the source text, with the same string/comment
/// tracking `strip_jsonc` uses (so the two can never disagree about what
/// counts as JSON structure vs. quoted text vs. a comment). Positions used
/// throughout this module (`p`/`cursor`/`start_tok`/…) are indices into
/// `toks`, NOT byte offsets — [`Scanner::byte_at`] converts at the boundary
/// where a result is handed back to the caller.
struct Scanner<'a> {
    toks: Vec<(usize, char)>,
    text: &'a str,
}

impl<'a> Scanner<'a> {
    fn new(text: &'a str) -> Self {
        Scanner {
            toks: text.char_indices().collect(),
            text,
        }
    }

    fn byte_at(&self, p: usize) -> usize {
        self.toks.get(p).map(|&(b, _)| b).unwrap_or(self.text.len())
    }

    fn char_at(&self, p: usize) -> Option<char> {
        self.toks.get(p).map(|&(_, c)| c)
    }

    fn slice(&self, from_tok: usize, to_tok: usize) -> &'a str {
        &self.text[self.byte_at(from_tok)..self.byte_at(to_tok)]
    }

    /// Skip whitespace, a stray BOM, and `//`/`/* */` comments starting at
    /// `p`; returns the resulting position.
    fn skip_ws_and_comments(&self, mut p: usize) -> usize {
        loop {
            match self.char_at(p) {
                Some(c) if c.is_whitespace() || c == '\u{feff}' => p += 1,
                Some('/') if self.char_at(p + 1) == Some('/') => {
                    p += 2;
                    while let Some(c) = self.char_at(p) {
                        p += 1;
                        if c == '\n' {
                            break;
                        }
                    }
                }
                Some('/') if self.char_at(p + 1) == Some('*') => {
                    p += 2;
                    loop {
                        match self.char_at(p) {
                            None => break,
                            Some('*') if self.char_at(p + 1) == Some('/') => {
                                p += 2;
                                break;
                            }
                            _ => p += 1,
                        }
                    }
                }
                _ => return p,
            }
        }
    }

    /// Skip a JSON string starting at `p` (which must be its opening `"`);
    /// returns the position just past the closing `"`, or `None` if it never
    /// closes.
    fn skip_string(&self, p: usize) -> Option<usize> {
        let mut q = p + 1;
        loop {
            match self.char_at(q)? {
                '\\' => q += 2,
                '"' => return Some(q + 1),
                _ => q += 1,
            }
        }
    }

    /// Skip a balanced `{...}` or `[...]` region starting at `p` (its opening
    /// bracket); returns the position just past the matching close, tracking
    /// nested brackets/strings/comments so one inside a string or comment is
    /// never mistaken for real structure. `None` if it never balances.
    fn skip_balanced(&self, p: usize) -> Option<usize> {
        let mut depth = 0u32;
        let mut q = p;
        loop {
            match self.char_at(q)? {
                '"' => q = self.skip_string(q)?,
                '/' if matches!(self.char_at(q + 1), Some('/') | Some('*')) => {
                    q = self.skip_ws_and_comments(q);
                }
                '{' | '[' => {
                    depth += 1;
                    q += 1;
                }
                '}' | ']' => {
                    depth -= 1;
                    q += 1;
                    if depth == 0 {
                        return Some(q);
                    }
                }
                _ => q += 1,
            }
        }
    }

    /// Skip one JSON value (string/object/array/number/true/false/null)
    /// starting at `p`; returns the position just past it, or `None` on a
    /// value that never terminates.
    fn skip_value(&self, p: usize) -> Option<usize> {
        match self.char_at(p)? {
            '"' => self.skip_string(p),
            '{' | '[' => self.skip_balanced(p),
            _ => {
                // number / true / false / null: run until a structural
                // delimiter. A `/` is checked separately so a comment with no
                // preceding space (`nullfoo//c`) is never folded into it.
                let mut q = p;
                while let Some(c) = self.char_at(q) {
                    if c == ',' || c == '}' || c == ']' || c.is_whitespace() {
                        break;
                    }
                    if c == '/' && matches!(self.char_at(q + 1), Some('/') | Some('*')) {
                        break;
                    }
                    q += 1;
                }
                (q != p).then_some(q)
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn locates_the_single_configuration_object() {
        let text = "{\n  \"version\": \"0.2.0\",\n  \"configurations\": [\n    {\"name\": \"a\"}\n  ]\n}\n";
        let edit = locate_configuration_edit(text, Some(0)).expect("should locate");
        match edit {
            SpliceEdit::Replace { start, end, .. } => {
                assert_eq!(&text[start..end], "{\"name\": \"a\"}");
            }
            other => panic!("expected Replace, got {other:?}"),
        }
    }

    #[test]
    fn replace_preserves_every_byte_outside_the_span() {
        let text = "\u{feff}{\n  // top comment\n  \"configurations\": [\n    // above\n    {\n      \"name\": \"a\", // trailing\n      \"device\": \"x\"\n    },\n    {\n      \"name\": \"b\"\n    }\n  ]\n}\n";
        let edit = locate_configuration_edit(text, Some(1)).expect("should locate the 2nd entry");
        let entry = json!({"name": "b", "type": "lldb"});
        let out = apply_edit(text, &edit, &entry);
        // Everything up to the 2nd object is untouched: BOM, top comment, the
        // first entry (including its own comments), and the leading comma.
        let prefix_end = text.find("{\n      \"name\": \"b\"").unwrap();
        assert_eq!(&out[..prefix_end], &text[..prefix_end]);
        assert!(out.contains("// top comment"));
        assert!(out.contains("// above"));
        assert!(out.contains("// trailing"));
        assert!(out.contains("\"type\": \"lldb\""));
        assert!(out.ends_with("  ]\n}\n"));
    }

    #[test]
    fn append_after_a_trailing_comma_adds_no_second_comma() {
        let text = "{\"configurations\": [\n  {\"name\": \"a\"},\n]}";
        let edit = locate_configuration_edit(text, None).expect("should locate the append point");
        match &edit {
            SpliceEdit::Append {
                needs_leading_comma,
                ..
            } => assert!(!needs_leading_comma, "a trailing comma already exists"),
            other => panic!("expected Append, got {other:?}"),
        }
        let out = apply_edit(text, &edit, &json!({"name": "b"}));
        let doc: Value = serde_json::from_str(&out).expect("must still be valid JSON");
        assert_eq!(doc["configurations"].as_array().unwrap().len(), 2);
        assert!(out.contains("\"name\": \"a\""));
    }

    #[test]
    fn an_unbalanced_brace_inside_a_comment_does_not_confuse_the_depth_count() {
        // A `/* ... */` comment holding a LONE, unbalanced `}` inside an
        // earlier element: a scan that didn't recognise the comment (walked
        // it char-by-char instead) would read that `}` as closing the
        // element early, at the wrong offset — a BALANCED pair inside a
        // comment nets to zero and would pass even with comment recognition
        // disabled entirely, so this is the case that actually pins it.
        let text = "{\"configurations\": [{\"a\": 1 /* stray } */ }, {\"name\": \"b\"}]}";
        let edit = locate_configuration_edit(text, Some(1)).expect("must still locate index 1");
        match edit {
            SpliceEdit::Replace { start, end, .. } => {
                assert_eq!(&text[start..end], "{\"name\": \"b\"}");
            }
            other => panic!("expected Replace, got {other:?}"),
        }
    }

    #[test]
    fn a_string_containing_a_brace_character_does_not_confuse_the_depth_count() {
        // Same hazard, for a `{`/`}` inside a plain JSON string value rather
        // than a comment — and, as above, UNBALANCED (a lone `}`, no
        // matching `{`), since a balanced pair nets to zero and would pass
        // even with string recognition disabled entirely.
        let text = "{\"configurations\": [{\"a\": \"stray }\"}, {\"name\": \"b\"}]}";
        let edit = locate_configuration_edit(text, Some(1)).expect("must still locate index 1");
        match edit {
            SpliceEdit::Replace { start, end, .. } => {
                assert_eq!(&text[start..end], "{\"name\": \"b\"}");
            }
            other => panic!("expected Replace, got {other:?}"),
        }
    }

    #[test]
    fn an_escaped_quote_inside_a_string_does_not_end_the_string_early() {
        let text =
            "{\"configurations\": [{\"a\": \"quote: \\\" still inside\"}, {\"name\": \"b\"}]}";
        let edit = locate_configuration_edit(text, Some(1)).expect("must still locate index 1");
        match edit {
            SpliceEdit::Replace { start, end, .. } => {
                assert_eq!(&text[start..end], "{\"name\": \"b\"}");
            }
            other => panic!("expected Replace, got {other:?}"),
        }
    }

    #[test]
    fn append_without_a_trailing_comma_inserts_one() {
        let text = "{\"configurations\": [\n  {\"name\": \"a\"}\n]}";
        let edit = locate_configuration_edit(text, None).unwrap();
        match &edit {
            SpliceEdit::Append {
                needs_leading_comma,
                ..
            } => assert!(*needs_leading_comma),
            other => panic!("expected Append, got {other:?}"),
        }
        let out = apply_edit(text, &edit, &json!({"name": "b"}));
        let doc: Value = serde_json::from_str(&out).unwrap();
        assert_eq!(doc["configurations"].as_array().unwrap().len(), 2);
    }

    #[test]
    fn append_into_an_empty_array() {
        let text = "{\"configurations\": []}";
        let edit = locate_configuration_edit(text, None).unwrap();
        match &edit {
            SpliceEdit::Append {
                needs_leading_comma,
                ..
            } => assert!(!needs_leading_comma),
            other => panic!("expected Append, got {other:?}"),
        }
        let out = apply_edit(text, &edit, &json!({"name": "b"}));
        let doc: Value = serde_json::from_str(&out).unwrap();
        assert_eq!(doc["configurations"].as_array().unwrap().len(), 1);
    }

    #[test]
    fn missing_configurations_key_returns_none() {
        let text = "{\"version\": \"0.2.0\"}";
        assert_eq!(locate_configuration_edit(text, None), None);
        assert_eq!(locate_configuration_edit(text, Some(0)), None);
    }

    #[test]
    fn configurations_not_an_array_returns_none() {
        let text = "{\"configurations\": {}}";
        assert_eq!(locate_configuration_edit(text, None), None);
    }

    #[test]
    fn out_of_range_index_returns_none() {
        let text = "{\"configurations\": [{\"name\": \"a\"}]}";
        assert_eq!(locate_configuration_edit(text, Some(5)), None);
    }

    #[test]
    fn a_configurations_looking_key_nested_in_another_object_is_not_mistaken_for_the_top_level_one()
    {
        // "configurations" only means the top-level array; a same-named key
        // buried inside some other top-level object's value must be skipped
        // over as a plain value, not treated as the array we're hunting for.
        let text = "{\"unrelated\": {\"configurations\": \"nope\"}, \"configurations\": [{\"name\": \"a\"}]}";
        let edit = locate_configuration_edit(text, Some(0)).expect("should find the real one");
        match edit {
            SpliceEdit::Replace { start, end, .. } => {
                assert_eq!(&text[start..end], "{\"name\": \"a\"}");
            }
            other => panic!("expected Replace, got {other:?}"),
        }
    }

    #[test]
    fn a_non_object_element_is_skipped_and_never_counted_as_an_object_span() {
        // Not a realistic launch.json, but the scanner must not crash or
        // miscount an array holding a stray non-object entry.
        let text = "{\"configurations\": [\"stray\", {\"name\": \"a\"}, {\"name\": \"b\"}]}";
        let edit = locate_configuration_edit(text, Some(1)).expect("2nd object is index 1");
        match edit {
            SpliceEdit::Replace { start, end, .. } => {
                assert_eq!(&text[start..end], "{\"name\": \"b\"}");
            }
            other => panic!("expected Replace, got {other:?}"),
        }
    }

    #[test]
    fn indentation_is_copied_from_a_neighbouring_entry() {
        let text =
            "{\n  \"configurations\": [\n        {\n          \"name\": \"a\"\n        }\n  ]\n}\n";
        let edit = locate_configuration_edit(text, Some(0)).unwrap();
        match edit {
            SpliceEdit::Replace { indent, .. } => assert_eq!(indent, "        "),
            other => panic!("expected Replace, got {other:?}"),
        }
    }

    /// tan-cli#182 review finding #3: a document with two top-level
    /// `"configurations"` keys is ambiguous -- `serde_json` resolves it to the
    /// LAST one, so an `existing_index` computed against the parsed `Value`
    /// would address the SECOND array, while a scan that stopped at the first
    /// match would splice into the FIRST. That mismatch destroys whichever
    /// entry happened to sit in the array actually spliced, and leaves the
    /// other array untouched and stale. Bailing to `None` here is what routes
    /// the caller to the safe (if lossy) whole-document fallback instead.
    #[test]
    fn a_duplicate_top_level_configurations_key_returns_none() {
        let text = "{\"configurations\": [{\"name\": \"DECOY\"}], \"version\": \"0.2.0\", \
                     \"configurations\": [{\"name\": \"real\"}]}";
        assert_eq!(locate_configuration_edit(text, None), None);
        assert_eq!(locate_configuration_edit(text, Some(0)), None);
    }

    /// tan-cli#182 review finding #4: a splice into a CRLF-authored document
    /// must not leave the entry's own newlines as bare LF -- that strands a
    /// mixed-EOL file behind (Windows is tan's primary platform, and this
    /// repo's own standing weak spot).
    #[test]
    fn replace_matches_the_documents_own_crlf_line_endings() {
        let text = "{\r\n  \"configurations\": [\r\n    {\r\n      \"name\": \"a\",\r\n      \"device\": \"old\"\r\n    }\r\n  ]\r\n}\r\n";
        let edit = locate_configuration_edit(text, Some(0)).unwrap();
        let out = apply_edit(text, &edit, &json!({"name": "a", "device": "new"}));
        assert!(!out.contains("\r\n\r"), "no doubled CR: {out:?}");
        // Every line break in the spliced entry itself is `\r\n`, not a bare
        // `\n` -- i.e. there are no lone LFs anywhere in the output.
        let lone_lf = out
            .as_bytes()
            .windows(2)
            .enumerate()
            .filter(|(_, w)| w[1] == b'\n' && w[0] != b'\r')
            .count();
        assert_eq!(lone_lf, 0, "a bare LF survived the splice: {out:?}");
        let doc: Value = serde_json::from_str(&out.replace("\r\n", "\n")).unwrap();
        assert_eq!(doc["configurations"][0]["device"], "new");
    }

    /// tan-cli#182 review finding #4, the append counterpart: an append into a
    /// CRLF document must not introduce bare LFs either.
    #[test]
    fn append_matches_the_documents_own_crlf_line_endings() {
        let text = "{\r\n  \"configurations\": [\r\n    {\"name\": \"a\"}\r\n  ]\r\n}\r\n";
        let edit = locate_configuration_edit(text, None).unwrap();
        let out = apply_edit(text, &edit, &json!({"name": "b"}));
        let lone_lf = out
            .as_bytes()
            .windows(2)
            .enumerate()
            .filter(|(_, w)| w[1] == b'\n' && w[0] != b'\r')
            .count();
        assert_eq!(lone_lf, 0, "a bare LF survived the append: {out:?}");
        let doc: Value = serde_json::from_str(&out.replace("\r\n", "\n")).unwrap();
        assert_eq!(doc["configurations"].as_array().unwrap().len(), 2);
    }

    /// tan-cli#182 review finding #5: appending into an array collapsed on
    /// one line (`"configurations": []`, the VS Code stock template's own
    /// shape) must indent the new entry one level under the array's OWN
    /// indentation, not the flat 4-space default -- and must not strand the
    /// closing `]` at column 0.
    #[test]
    fn append_into_an_empty_array_on_a_four_space_file_indents_one_level_deeper() {
        let text = "{\n    \"version\": \"0.2.0\",\n    \"configurations\": []\n}\n";
        let edit = locate_configuration_edit(text, None).unwrap();
        match &edit {
            SpliceEdit::Append {
                indent,
                closing_indent,
                ..
            } => {
                assert_eq!(
                    indent, "        ",
                    "one level deeper than the 4-space array line"
                );
                assert_eq!(closing_indent.as_deref(), Some("    "));
            }
            other => panic!("expected Append, got {other:?}"),
        }
        let out = apply_edit(text, &edit, &json!({"name": "b"}));
        assert_eq!(
            out,
            "{\n    \"version\": \"0.2.0\",\n    \"configurations\": [\n        {\n          \"name\": \"b\"\n        }\n    ]\n}\n"
        );
        let doc: Value = serde_json::from_str(&out).unwrap();
        assert_eq!(doc["configurations"].as_array().unwrap().len(), 1);
    }

    /// The companion case: an empty array already spread across its own lines
    /// (not collapsed) is left to its existing formatting -- `closing_indent`
    /// must be `None`, matching every other append site.
    #[test]
    fn append_into_an_empty_array_already_spread_across_lines_gets_no_closing_indent() {
        let text = "{\n  \"configurations\": [\n  ]\n}\n";
        let edit = locate_configuration_edit(text, None).unwrap();
        match &edit {
            SpliceEdit::Append { closing_indent, .. } => assert_eq!(*closing_indent, None),
            other => panic!("expected Append, got {other:?}"),
        }
    }
}
