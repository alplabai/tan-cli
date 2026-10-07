# SPDX-License-Identifier: Apache-2.0
"""`tan.core.board_yaml_edit` (tan-cli#1286): the comment-preserving append to
`board.yaml`'s `models:`. The golden tests pin BYTE-for-byte preservation: the
result is the original with only the inserted lines added."""
from __future__ import annotations

import pytest
import yaml

from tan.core.board_yaml_edit import BoardEditRefused, append_models_entry, render_entry

ENTRY = {"name": "tiny", "source": "models/tiny.tflite"}
NEW = "  - name: tiny\n    source: models/tiny.tflite\n"


def test_append_after_last_item_keeps_every_other_byte():
    before = (
        "# header comment\n"
        "som:\n  sku: E1M-AEN801   # the SoM\n\n"
        "# models below\n"
        "models:\n"
        "  - name: a   # keep me\n"
        "    source: a.tflite\n"
        "    # inner comment\n"
        "  - name: b\n"
        "    source: b.tflite\n"
        "\n"
        "  # trailing list comment\n"
        "\n"
        "other: 1   # tail\n"
    )
    after = append_models_entry(before, ENTRY)
    cut = before.index("\n\n  # trailing list comment") + 1
    assert after == before[:cut] + NEW + before[cut:]


def test_item_indent_zero_is_matched():
    before = "som: {sku: X}\nmodels:\n- name: a\n  source: a.tflite\nnext: 1\n"
    after = append_models_entry(before, ENTRY)
    assert after == before.replace("next: 1", "- name: tiny\n  source: models/tiny.tflite\nnext: 1")


def test_absent_models_appends_a_block_at_the_end():
    before = "# c\nsom:\n  sku: X  # k\n"
    assert append_models_entry(before, ENTRY) == before + "models:\n" + NEW


def test_absent_models_and_no_trailing_newline():
    after = append_models_entry("som:\n  sku: X", ENTRY)
    assert after == "som:\n  sku: X\nmodels:\n" + NEW


def test_empty_models_key_with_comment_and_following_key():
    before = "models:   # none yet\n# note\nother: 1\n"
    after = append_models_entry(before, ENTRY)
    assert after == "models:   # none yet\n" + NEW + "# note\nother: 1\n"


def test_empty_flow_list_is_rewritten_on_that_one_line():
    before = "a: 1\nmodels: []  # none\nb: 2\n"
    after = append_models_entry(before, ENTRY)
    assert after == "a: 1\nmodels: # none\n" + NEW + "b: 2\n"


def test_crlf_is_preserved():
    before = "som:\r\n  sku: X\r\nmodels:\r\n  - name: a\r\n    source: a.tflite\r\n"
    after = append_models_entry(before, ENTRY)
    assert after == before + NEW.replace("\n", "\r\n")


def test_last_line_without_newline_gets_one():
    before = "models:\n  - name: a\n    source: a.tflite"
    assert append_models_entry(before, ENTRY) == before + "\n" + NEW


def test_leading_document_marker_is_fine():
    before = "---\nmodels:\n  - name: a\n    source: a.tflite\n"
    assert append_models_entry(before, ENTRY) == before + NEW


def test_compile_block_round_trips_and_yaml_bool_names_are_quoted():
    entry = {
        "name": "no",
        "source": "models/no.onnx",
        "compile": {
            "drpai": {"input_shape": [1, 3, 224, 224], "input_name": "images", "images": "cal/img"},
            "deepx_dxm1": {"config": "c.json", "calibration": "cal"},
        },
    }
    after = append_models_entry("models:\n  - name: a\n    source: a.tflite\n", entry)
    assert yaml.safe_load(after)["models"][1] == entry
    assert "name: 'no'" in after
    assert "input_shape: [1, 3, 224, 224]" in after


def test_render_entry_indent():
    assert render_entry(ENTRY, 4) == ["    - name: tiny", "      source: models/tiny.tflite"]


@pytest.mark.parametrize(
    "text",
    [
        "models: [{name: a, source: b}]\n",
        "models: &m\n  - name: a\n    source: b\n",
        "models: *m\n",
        '"models":\n  - name: a\n    source: b\n',
        "models:\n  - name: a\n    source: b\nmodels:\n  - name: c\n    source: d\n",
        "a: 1\n---\nmodels: []\n",
        "models:\n\t- name: a\n",
        "models:\n  key: value\n",
        "- just\n- a list\n",
        "models: [\n",
    ],
)
def test_ambiguous_shapes_are_refused(text):
    with pytest.raises(BoardEditRefused):
        append_models_entry(text, ENTRY)


def test_models_prefix_keys_are_not_the_models_key():
    before = "models_extra: 1\nmodels:\n  - name: a\n    source: b\n"
    assert append_models_entry(before, ENTRY) == before + NEW


def test_unrenderable_values_refuse():
    with pytest.raises(BoardEditRefused):
        append_models_entry("a: 1\n", {"name": "x", "source": "a\nb"})


@pytest.mark.parametrize(
    "text",
    [
        "﻿models: []\nfoo: 1\n" + "models: []\n",  # real duplicate
        "? models\n: []\n",
        "? models\nfoo: 1\n",
        "a: 1\na: 2\n",
    ],
)
def test_duplicate_or_complex_key_shapes_are_refused(text):
    with pytest.raises(BoardEditRefused):
        append_models_entry(text, ENTRY)


def test_bom_before_models_key_is_matched_not_duplicated():
    before = "﻿models: []\nfoo: 1\n"
    after = append_models_entry(before, ENTRY)
    assert after == "﻿models:\n" + NEW + "foo: 1\n"
    assert yaml.safe_load(after)["models"] == [ENTRY]
    before = "﻿models:\nfoo: 1\n"
    assert append_models_entry(before, ENTRY) == "﻿models:\n" + NEW + "foo: 1\n"


def test_bom_with_absent_models_appends_once():
    after = append_models_entry("﻿a: 1\n", ENTRY)
    assert after == "﻿a: 1\nmodels:\n" + NEW


@pytest.mark.parametrize("value", ["~", "null", "Null"])
def test_null_models_is_treated_as_empty(value):
    after = append_models_entry(f"models: {value}\nb: 2\n", ENTRY)
    assert after == "models:\n" + NEW + "b: 2\n"


def test_directive_start_and_trailing_end_marker_are_single_document():
    before = "%YAML 1.2\n---\na: 1\n...\n"
    after = append_models_entry(before, ENTRY)
    assert after == "%YAML 1.2\n---\na: 1\nmodels:\n" + NEW + "...\n"


def test_second_document_is_refused():
    with pytest.raises(BoardEditRefused):
        append_models_entry("a: 1\n---\nb: 2\n", ENTRY)


def test_content_after_document_end_is_refused():
    with pytest.raises(BoardEditRefused):
        append_models_entry("a: 1\n...\nb: 2\n", ENTRY)


def test_nan_in_the_board_does_not_trip_the_equality_guard():
    before = "x: .nan\nmodels: []\n"
    after = append_models_entry(before, ENTRY)
    assert after == "x: .nan\nmodels:\n" + NEW


def test_recursive_alias_is_refused_not_crashed():
    with pytest.raises(BoardEditRefused):
        append_models_entry("a: &a [*a]\n", ENTRY)


# --- parity with the planner's strict loader (loaded by file path: core may
# --- not import tan.planner, but the two must accept/reject the same shapes)

import importlib.util  # noqa: E402
from pathlib import Path  # noqa: E402

from tan.core.board_yaml_edit import _strict_load  # noqa: E402

_SPEC = importlib.util.spec_from_file_location(
    "planner_strict_loaders",
    Path(__file__).resolve().parents[2] / "tan" / "planner" / "strict_loaders.py",
)
_PLANNER = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_PLANNER)

_CORPUS = [
    "a: 1\nb: 2\n",
    "a: 1\na: 2\n",
    "models: []\nmodels: []\n",
    "base: &b {x: 1}\nd:\n  <<: *b\n  x: 2\n",
    "d:\n  <<: [{x: 1}, {x: 2}]\n",
    "d:\n  x: 1\n  y:\n    z: 1\n    z: 2\n",
    "? [1, 2]\n: v\n",
    "? {a: 1}\n: v\n",
    "- a\n- b\n",
    "k: &a [*a]\n",
    "",
    "a: [unclosed\n",
]


@pytest.mark.parametrize("text", _CORPUS)
def test_strict_load_agrees_with_the_planner_strict_loader(text):
    def outcome(fn):
        try:
            return ("ok", fn(text))
        except Exception:  # noqa: BLE001 -- only accept/reject matters
            return ("rejected", None)

    planner = outcome(lambda t: _PLANNER.strict_yaml_load(t))
    ours = outcome(lambda t: _strict_load(t, "board.yaml"))
    assert planner[0] == ours[0], text
    if planner[0] == "ok":
        assert planner[1] == ours[1]
