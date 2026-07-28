// SPDX-License-Identifier: Apache-2.0
//! `tan examples` — list the SDK's ready-made example projects as a catalog the
//! New Project flow can scaffold from via `tan init --from-example <sourceDir>`.
//!
//! Scans the resolved alp-sdk checkout's `examples/<category>/<name>/` tree for
//! directories carrying a `board.yaml`, deriving a display title/description from
//! each example's `README.md`. When no SDK root resolves, the catalog is empty —
//! not an error — so callers can render "no examples available" cleanly.

use tan_core::wizard::{
    discover_examples, example_description_from_readme, example_id_from_source_dir,
    example_title_from_readme,
};

use super::CommandRun;
use crate::cli::{ExamplesArgs, GlobalArgs};
use crate::envelope::{Envelope, Project};
use crate::exit::ExitCode;

/// One example project in the `examples` catalog envelope.
#[derive(serde::Serialize)]
struct ExampleEntry {
    /// Stable, unique id (equal to `source_dir`).
    id: String,
    /// `category/name` path relative to the SDK `examples/` root.
    #[serde(rename = "sourceDir")]
    source_dir: String,
    /// Human-readable title (README heading or title-cased leaf name).
    title: String,
    /// One-line description (README's first prose line; empty when absent).
    description: String,
}

/// JSON `data` payload of the `examples` envelope: the discovered example catalog.
#[derive(serde::Serialize)]
struct ExamplesData {
    /// Payload schema version (currently `"1"`).
    #[serde(rename = "schemaVersion")]
    schema_version: String,
    /// Every discovered example project, sorted by `source_dir`.
    examples: Vec<ExampleEntry>,
}

/// Run `tan examples`: resolve the SDK root, scan `<sdk>/examples` for example
/// projects, and emit them as a catalog. A missing SDK root yields an empty
/// catalog with a success exit. `--filter` (tan-cli#164) narrows both the text
/// listing and the JSON `data.examples[]` to entries whose `id`/`title` contain
/// the substring, case-insensitively.
pub fn run(g: &GlobalArgs, args: &ExamplesArgs) -> CommandRun {
    let workspace_root = crate::util::cli_workspace_root(g);
    let mut examples: Vec<ExampleEntry> = match crate::util::resolve_sdk_root(g, &workspace_root) {
        Some(sdk_root) => discover_examples(&sdk_root.join("examples"))
            .into_iter()
            .map(|d| ExampleEntry {
                id: example_id_from_source_dir(&d.source_dir),
                title: example_title_from_readme(d.readme.as_deref(), &d.source_dir),
                description: example_description_from_readme(d.readme.as_deref()),
                source_dir: d.source_dir,
            })
            .collect(),
        None => Vec::new(),
    };
    if let Some(filter) = args.filter.as_deref() {
        examples.retain(|e| example_matches_filter(e, filter));
    }

    let text = if g.is_json() {
        vec![]
    } else {
        render_examples_text(&examples, args.filter.as_deref(), g.verbose)
    };
    let data = ExamplesData {
        schema_version: "1".to_string(),
        examples,
    };
    let project = Project {
        root: None,
        board_yaml: None,
    };
    let json = g.is_json().then(|| {
        Envelope::new("examples", project, data, vec![], ExitCode::Success.code()).to_json()
    });
    CommandRun {
        exit: ExitCode::Success,
        text,
        json,
    }
}

/// Whether `entry` matches `filter` — a case-insensitive substring of either
/// `id` or `title`. Deliberately not `source_dir`/`description`: those are the
/// same string as `id` (bar id's `/`-vs-path spelling) or too free-form for a
/// quick catalog scan to key on.
fn example_matches_filter(entry: &ExampleEntry, filter: &str) -> bool {
    let needle = filter.to_lowercase();
    entry.id.to_lowercase().contains(&needle) || entry.title.to_lowercase().contains(&needle)
}

/// Render the text-mode listing (tan-cli#164 — this used to be a bare count,
/// never the list `--from-example` needs a source dir from). One `id  title`
/// line per example, `id`-column aligned to the longest id in THIS (possibly
/// filtered) result set; `--verbose` appends the description.
fn render_examples_text(
    examples: &[ExampleEntry],
    filter: Option<&str>,
    verbose: bool,
) -> Vec<String> {
    if examples.is_empty() {
        return vec![match filter {
            Some(f) => format!("examples: no example projects match --filter {f:?}."),
            None => "examples: no example projects found (is the alp-sdk checkout resolvable? \
                     use --sdk-root)."
                .to_string(),
        }];
    }
    let id_width = examples.iter().map(|e| e.id.len()).max().unwrap_or(0);
    let mut lines = vec![format!(
        "examples: {} example project(s) available",
        examples.len()
    )];
    for e in examples {
        let mut line = format!("  {:id_width$}  {}", e.id, e.title);
        if verbose && !e.description.is_empty() {
            line.push_str("   -- ");
            line.push_str(&e.description);
        }
        lines.push(line);
    }
    lines
}

#[cfg(test)]
mod tests {
    use super::{ExampleEntry, ExamplesData, example_matches_filter, render_examples_text};

    /// Pins the byte-fixed populated-catalog shape (camelCase `sourceDir`, field
    /// order) that the empty-catalog contract fixture cannot exercise and that the
    /// extension's `data.examples[].sourceDir` consumer depends on.
    #[test]
    fn examples_data_serializes_with_camelcase_source_dir_and_field_order() {
        let data = ExamplesData {
            schema_version: "1".to_string(),
            examples: vec![ExampleEntry {
                id: "audio/i2s-tone".to_string(),
                source_dir: "audio/i2s-tone".to_string(),
                title: "I2S Tone".to_string(),
                description: "Plays a tone.".to_string(),
            }],
        };
        let json = serde_json::to_string(&data).unwrap();
        assert_eq!(
            json,
            r#"{"schemaVersion":"1","examples":[{"id":"audio/i2s-tone","sourceDir":"audio/i2s-tone","title":"I2S Tone","description":"Plays a tone."}]}"#
        );
    }

    fn entry(id: &str, title: &str, description: &str) -> ExampleEntry {
        ExampleEntry {
            id: id.to_string(),
            source_dir: id.to_string(),
            title: title.to_string(),
            description: description.to_string(),
        }
    }

    #[test]
    fn filter_matches_id_or_title_case_insensitively() {
        let hello = entry("peripheral-io/hello-world", "Hello World", "");
        assert!(example_matches_filter(&hello, "HELLO"));
        assert!(example_matches_filter(&hello, "peripheral-io"));
        assert!(!example_matches_filter(&hello, "rpmsg"));

        let rpmsg = entry("multicore/rpmsg-v2n", "RPMsg V2N", "");
        assert!(example_matches_filter(&rpmsg, "v2n"));
        assert!(!example_matches_filter(&rpmsg, "hello"));
    }

    #[test]
    fn text_render_lists_id_and_title_not_just_a_count() {
        // Regression: tan-cli#164 -- `tan examples` used to print ONLY the
        // count line below, never the entries `--from-example` needs an id
        // from.
        let examples = vec![
            entry("peripheral-io/hello-world", "Hello World", "Blinks a pin."),
            entry("multicore/rpmsg-v2n", "RPMsg V2N", "A55+M33 messaging."),
        ];
        let lines = render_examples_text(&examples, None, false);
        assert_eq!(lines[0], "examples: 2 example project(s) available");
        assert!(
            lines[1].contains("peripheral-io/hello-world") && lines[1].contains("Hello World"),
            "{lines:?}"
        );
        assert!(
            lines[2].contains("multicore/rpmsg-v2n") && lines[2].contains("RPMsg V2N"),
            "{lines:?}"
        );
        // Non-verbose: the description must NOT leak into the line.
        assert!(!lines[1].contains("Blinks a pin."), "{lines:?}");
    }

    #[test]
    fn verbose_appends_the_description_non_verbose_omits_it() {
        let examples = vec![entry("audio/i2s-tone", "I2S Tone", "Plays a tone.")];
        let quiet = render_examples_text(&examples, None, false);
        let verbose = render_examples_text(&examples, None, true);
        assert!(!quiet[1].contains("Plays a tone."), "{quiet:?}");
        assert!(verbose[1].contains("Plays a tone."), "{verbose:?}");
    }

    #[test]
    fn empty_result_names_the_filter_that_produced_it() {
        let lines = render_examples_text(&[], Some("no-such-thing"), false);
        assert_eq!(lines.len(), 1);
        assert!(lines[0].contains("no-such-thing"), "{lines:?}");
    }
}
