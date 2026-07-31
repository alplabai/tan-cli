# SPDX-License-Identifier: Apache-2.0
"""`tan examples` -- the oracle's own unit tests, ported.

Every case below has a named twin in `crates/tan-cli/src/commands/examples.rs`
or `crates/tan-core/src/wizard/{filesystem,service/example_catalog}.rs`. Two
things the `examples-catalog` conformance golden cannot reach are pinned here
instead: the EMPTY-catalogue path (the golden ships a populated fixture SDK) and
the text renderer (the golden runs `--format json`).
"""
from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from tan.cli import app
from tan.commands.examples_cmd import (
    Example,
    discover_examples,
    example_description_from_readme,
    example_id_from_source_dir,
    example_matches_filter,
    example_title_from_readme,
    render_examples_text,
    strip_markdown_noise,
)

runner = CliRunner()


def entry(id_: str, title: str, description: str = "") -> Example:
    return Example(id=id_, source_dir=id_, title=title, description=description)


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="")


# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------


def test_discover_finds_dirs_with_board_yaml_sorted(tmp_path):
    write(tmp_path / "audio/i2s-tone/board.yaml", "x")
    write(tmp_path / "audio/i2s-tone/README.md", "# I2S Tone\n\nPlays a tone.")
    # A directory with no board.yaml is not an example.
    write(tmp_path / "audio/no-board/notes.txt", "x")
    write(tmp_path / "peripheral-io/hello/board.yaml", "x")

    found = discover_examples(tmp_path)
    assert [e.source_dir for e in found] == ["audio/i2s-tone", "peripheral-io/hello"]
    assert found[0].title == "I2S Tone"
    assert found[0].description == "Plays a tone."
    # No README at all -> title falls back, description is "" not None.
    assert found[1].title == "Hello"
    assert found[1].description == ""


def test_discover_missing_root_is_empty_not_an_error(tmp_path):
    assert discover_examples(tmp_path / "nope") == []


def test_discover_survives_a_non_utf8_readme(tmp_path):
    """The recurring bug class: a byte sequence no codec accepts must degrade to
    "no README", never raise out of discovery."""
    write(tmp_path / "audio/broken/board.yaml", "x")
    (tmp_path / "audio/broken/README.md").write_bytes(b"# Fine\n\n\xff\xfe raw bytes\n")
    found = discover_examples(tmp_path)
    assert [e.source_dir for e in found] == ["audio/broken"]
    # Unreadable README == absent README, so the title is the leaf fallback.
    assert found[0].title == "Broken"
    assert found[0].description == ""


def test_discover_only_goes_two_levels_deep(tmp_path):
    """`<category>/<name>` and no further -- a board.yaml three levels down is a
    project's own subdirectory, not a catalogue entry."""
    write(tmp_path / "a/b/c/board.yaml", "x")
    assert discover_examples(tmp_path) == []


# --------------------------------------------------------------------------
# README-derived metadata
# --------------------------------------------------------------------------


def test_title_prefers_first_heading():
    assert example_title_from_readme("# I2S Tone\n\nbody", "audio/i2s-tone") == "I2S Tone"
    assert example_title_from_readme("## Sub\n", "a/b") == "Sub"


def test_title_falls_back_to_title_cased_leaf():
    assert example_title_from_readme(None, "audio/i2s-tone") == "I2s Tone"
    assert example_title_from_readme("no heading here", "peripheral-io/uart-echo") == "Uart Echo"


def test_description_skips_headings_blockquotes_and_badges():
    readme = "# Title\n\n> untested\n\n[![badge](x)]\n\nReads and plays a tone."
    assert example_description_from_readme(readme) == "Reads and plays a tone."
    assert example_description_from_readme(None) == ""


def test_id_equals_source_dir():
    assert example_id_from_source_dir("audio/i2s-tone") == "audio/i2s-tone"


# --------------------------------------------------------------------------
# Filtering
# --------------------------------------------------------------------------


def test_filter_matches_id_or_title_case_insensitively():
    hello = entry("peripheral-io/hello-world", "Hello World")
    assert example_matches_filter(hello, "HELLO")
    assert example_matches_filter(hello, "peripheral-io")
    assert not example_matches_filter(hello, "rpmsg")

    rpmsg = entry("multicore/rpmsg-v2n", "RPMsg V2N")
    assert example_matches_filter(rpmsg, "v2n")
    assert not example_matches_filter(rpmsg, "hello")


# --------------------------------------------------------------------------
# Text rendering
# --------------------------------------------------------------------------


def test_text_render_lists_id_and_title_not_just_a_count():
    # Regression tan-cli#164: this used to print ONLY the count line.
    examples = [
        entry("peripheral-io/hello-world", "Hello World", "Blinks a pin."),
        entry("multicore/rpmsg-v2n", "RPMsg V2N", "A55+M33 messaging."),
    ]
    lines = render_examples_text(examples, None, False)
    assert lines[0] == "examples: 2 example project(s) available"
    assert "peripheral-io/hello-world" in lines[1] and "Hello World" in lines[1]
    assert "multicore/rpmsg-v2n" in lines[2] and "RPMsg V2N" in lines[2]
    assert "Blinks a pin." not in lines[1]


def test_verbose_appends_the_description_non_verbose_omits_it():
    examples = [entry("audio/i2s-tone", "I2S Tone", "Plays a tone.")]
    assert "Plays a tone." not in render_examples_text(examples, None, False)[1]
    assert "Plays a tone." in render_examples_text(examples, None, True)[1]


def test_empty_result_names_the_filter_that_produced_it():
    lines = render_examples_text([], "no-such-thing", False)
    assert len(lines) == 1
    assert "no-such-thing" in lines[0]


def test_empty_result_without_a_filter_points_at_sdk_root():
    lines = render_examples_text([], None, False)
    assert lines == [
        "examples: no example projects found (is the alp-sdk checkout "
        "resolvable? use --sdk-root)."
    ]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # Real title from alp-sdk's audio/audio-noise-suppression README.
        (
            "audio-noise-suppression  ![UNTESTED]"
            "(https://img.shields.io/badge/status-UNTESTED-yellow)",
            "audio-noise-suppression",
        ),
        (
            "On-silicon analog validation for the **E1M-AEN801** board",
            "On-silicon analog validation for the E1M-AEN801 board",
        ),
        ("Plays a tone.", "Plays a tone."),
        # A `![` with no closing `)` is not markdown -- kept, not eaten.
        ("weird ![ prefix", "weird ![ prefix"),
    ],
)
def test_strip_markdown_noise(raw, expected):
    assert strip_markdown_noise(raw) == expected


# --------------------------------------------------------------------------
# The envelope
# --------------------------------------------------------------------------


def _sdk_with_example(root):
    write(root / "scripts/alp_project.py", "# marker\n")
    write(root / "examples/multicore/rpmsg-v2n/board.yaml", "som:\n  sku: X\n")
    write(
        root / "examples/multicore/rpmsg-v2n/README.md",
        "# RPMsg V2N\n\nHeterogeneous A55 + M33 message passing.\n",
    )
    return root


def test_json_envelope_shape(tmp_path):
    sdk = _sdk_with_example(tmp_path / "sdk")
    result = runner.invoke(app, ["examples", "--sdk-root", str(sdk), "--format", "json"])
    assert result.exit_code == 0
    doc = json.loads(result.stdout)
    assert doc["command"] == "examples"
    assert doc["ok"] is True
    assert doc["project"] == {"root": None, "boardYaml": None}
    assert doc["issues"] == []
    assert doc["sdk"]["sourceTier"] == "sdkRootFlag"
    assert doc["data"] == {
        "schemaVersion": "1",
        "examples": [
            {
                "id": "multicore/rpmsg-v2n",
                "sourceDir": "multicore/rpmsg-v2n",
                "title": "RPMsg V2N",
                "description": "Heterogeneous A55 + M33 message passing.",
            }
        ],
    }
    # Field ORDER inside an entry is contract too -- the golden's key set is
    # order-insensitive, so pin it here (see contract/README.md's own note).
    assert list(doc["data"]["examples"][0]) == ["id", "sourceDir", "title", "description"]


def test_a_sdk_root_that_is_not_a_checkout_is_an_empty_catalogue_not_a_failure(tmp_path):
    """`--sdk-root` is TERMINAL (I-31): a bad value must NOT fall through to
    discovery and list some other checkout's examples. And an unresolved SDK
    omits the `sdk` key entirely -- absent, never null."""
    (tmp_path / "not-an-sdk").mkdir()
    result = runner.invoke(
        app, ["examples", "--sdk-root", str(tmp_path / "not-an-sdk"), "--format", "json"]
    )
    assert result.exit_code == 0
    doc = json.loads(result.stdout)
    assert doc["data"]["examples"] == []
    assert "sdk" not in doc


def test_bad_format_is_a_usage_error_not_a_traceback(tmp_path):
    result = runner.invoke(app, ["examples", "--format", "yaml"])
    assert result.exit_code == 2
    assert "Traceback" not in result.output
