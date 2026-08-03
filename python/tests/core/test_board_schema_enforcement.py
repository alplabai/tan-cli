# SPDX-License-Identifier: Apache-2.0
"""The board.yaml JSON-Schema pass -- `loader.iter_schema_errors` and the
`loader._validate_board` message it feeds (tan-cli#410).

Those two functions are the WHOLE of the schema gate: `_validate_board`
consults nothing else, and `load_board_yaml` runs it on every load, so every
planner-driven command (`build`, `generate`, `kconfig`, `validate`, ...) goes
through them. Until this module they had ZERO tests -- `grep -rn
"schema validation failed" tests/` and `grep -rn "iter_schema_errors" tests/`
both came back empty. Measured on a copy of the tree, replacing
`iter_schema_errors`' body with `return []` moved NOTHING: same 22 failures,
same 2241 passes, identical FAILED sets, and the five suites that read as the
planner's coverage (`test_project_loader.py`, `test_planner_root.py`,
`test_kconfig_symbols.py`, `test_som_metadata.py`,
`test_build_planner_python.py`) stayed 42-passed either way.

What that silence costs is not a worse message. `python/pyproject.toml` states
that invariants I-02 (no top-level `os:`) and I-03 (`core_entry` is closed) are
ENFORCED here and nowhere else, so with the gate disabled a board.yaml carrying
a top-level `os:` -- the exact thing I-02 forbids -- plans and builds as if it
were valid, with no envelope error at all. The wrong-typed case degrades
instead of vanishing: `cores: 7` stops raising `OrchestratorError: board.yaml
schema validation failed:\n  - cores: 7 is not of type 'object'` and starts
raising a raw `AttributeError: 'int' object has no attribute 'keys'`, which
`tan kconfig` then hands to alp-sdk-vscode verbatim in `issues[].message` under
an unchanged `command`/`ok`/`exitCode`/`kconfig.emit-failed` -- nothing at the
envelope level marks the change.

So every test here is written to FAIL under that exact mutation
(`iter_schema_errors` -> `return []`), which is the acceptance bar tan-cli#410
sets. Cases 1-4 assert the offending PATH inside the message rather than just
the exception type, because the path is the part a user acts on.

Binding an alp-sdk checkout is required, and deliberately not faked: the point
is the REAL `metadata/schemas/board.schema.json`, whose `additionalProperties:
false` at the root and inside `$defs/core_entry` is where I-02 and I-03
actually live. Same requirement, same env vars, as `test_sdk_revision_gate.py`
and `test_project_loader.py`.
"""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest
import yaml

from tests.conftest import sdk_root

SDK = sdk_root()

_SKIP_REASON = (
    "set ALP_SDK_ROOT to an alp-sdk checkout so tan.planner can bind a "
    "root and import (same requirement as the parity suite)"
)

#: The prefix `_validate_board` puts in front of every schema refusal, and the
#: shape of each line under it. Spelled once, asserted by every case: this
#: string is what a caller (and, through `issues[].message`, the extension)
#: actually reads.
_PREFIX = "board.yaml schema validation failed:"


@pytest.fixture(scope="module")
def loader():
    """`tan.planner.loader`, with the SDK root bound first.

    `tan/planner/paths.py` freezes `REPO = sdk_root()` at import and half the
    package takes `metadata_root: Path = METADATA_ROOT` as a default argument,
    so the bind has to happen before the first `tan.planner` import -- hence a
    fixture rather than a module-level `from tan.planner import loader`.
    """
    if SDK is None:
        pytest.skip(reason=_SKIP_REASON)
    from tan.planner_root import bind_sdk_root

    bind_sdk_root(SDK)
    from tan.planner import loader as loader_mod

    return loader_mod


def _board(tmp_path: Path, text: str) -> Path:
    """A self-authored board.yaml, never an in-tree alp-sdk example: this
    module's coverage must not depend on a file path inside a second,
    independently moving repo (the argument `test_sdk_revision_gate.py`'s own
    fixture makes)."""
    path = tmp_path / "board.yaml"
    path.write_text(text, encoding="utf-8")
    return path


#: Valid enough to reach every later stage of the loader, so each case below
#: differs from a LOADABLE board by exactly the one violation under test.
_VALID = "som:\n  sku: E1M-AEN801\ncores:\n  m55_hp:\n    app: ./src\n"


# ---------------------------------------------------------------------------
# The four refusals, through the real front door (`load_board_yaml`).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "case, text, offending_line",
    [
        (
            "I-02",
            "som:\n  sku: E1M-AEN801\nos: zephyr\ncores:\n  m55_hp:\n    app: ./src\n",
            "  - <root>: Additional properties are not allowed ('os' was unexpected)",
        ),
        (
            "I-03",
            "som:\n  sku: E1M-AEN801\ncores:\n  m55_hp:\n    app: ./src\n    apps: ./other\n",
            "  - cores/m55_hp: Additional properties are not allowed ('apps' was unexpected)",
        ),
        (
            "missing-required",
            "som:\n  sku: E1M-AEN801\n",
            "  - <root>: 'cores' is a required property",
        ),
        (
            "wrong-typed",
            "som:\n  sku: E1M-AEN801\ncores: 7\n",
            "  - cores: 7 is not of type 'object'",
        ),
    ],
)
def test_a_schema_violation_refuses_naming_its_path(loader, tmp_path, case, text, offending_line):
    """Verbatim: the prefix, and the `  - <loc>: <message>` line.

    The location half is the load-bearing half. `<root>` vs `cores/m55_hp` is
    what tells a user WHERE to look, and `_validate_board` is the only thing
    that renders it (`"/".join(...) or "<root>"`) -- a schema error object on
    its own carries a deque, not that string.

    `wrong-typed` is also the case that does not merely go quiet without the
    gate: `cores: 7` falls through to `AttributeError: 'int' object has no
    attribute 'keys'`, so this line is what stands between a user and a
    Python-internals traceback in the extension's problems panel.
    """
    with pytest.raises(loader.OrchestratorError) as excinfo:
        loader.load_board_yaml(_board(tmp_path, text))

    message = str(excinfo.value)
    assert message.startswith(_PREFIX), message
    assert offending_line in message.splitlines(), message


def test_the_message_is_the_prefix_then_one_indented_line_per_error(loader, tmp_path):
    """The whole rendered shape, not just one line of it.

    Pinned because the two halves have different owners: the prefix and the
    `  - ` indent come from `_validate_board`, while the count of lines comes
    from how many errors `iter_schema_errors` returned. A board with more than
    one violation is what proves the join renders EVERY error rather than the
    first -- reporting one violation at a time turns a five-mistake board.yaml
    into five edit/run cycles.
    """
    text = (
        "som:\n  sku: E1M-AEN801\n"
        "cores:\n  m55_hp:\n    app: ./src\n    apps: ./other\n"
        "storage:\n  - 7\n"
    )
    with pytest.raises(loader.OrchestratorError) as excinfo:
        loader.load_board_yaml(_board(tmp_path, text))

    lines = str(excinfo.value).splitlines()
    assert lines[0] == _PREFIX
    assert len(lines) > 2, lines
    for line in lines[1:]:
        assert line.startswith("  - "), line
        # `<loc>: <message>` -- the separator has to survive, or the location
        # and the complaint run together into one unparseable string.
        assert ": " in line[4:], line
    unexpected = "Additional properties are not allowed ('apps' was unexpected)"
    assert f"  - cores/m55_hp: {unexpected}" in lines
    assert "  - storage/0: 7 is not of type 'object'" in lines


def test_a_valid_board_raises_nothing_from_the_schema_pass(loader, tmp_path):
    """The gate must refuse the four boards above WITHOUT refusing a real one:
    a validator wired to the wrong schema, or to a dialect that rejects the
    schema's own keywords, would fail every board and would still make the
    four cases above pass.
    """
    project = loader.load_board_yaml(_board(tmp_path, _VALID))
    assert project.sku == "E1M-AEN801"


# ---------------------------------------------------------------------------
# `iter_schema_errors` itself: ORDERING, and the stringify that makes it safe.
# ---------------------------------------------------------------------------


def test_errors_come_back_sorted_by_stringified_absolute_path(loader, tmp_path):
    """Ordering is a contract, not an accident: `iter_schema_errors`' own
    docstring says one schema file, one dialect and one ORDERING so "every
    consumer reports identical violations in identical order".
    `jsonschema.iter_errors` yields in schema-traversal order, which is not
    stable to edit.

    Errors here span an array INDEX (`storage/0`, `storage/10`) and sibling
    KEYS, which is the mix the sort key exists to handle. Note `storage/10`
    sorting BEFORE `storage/2`: the key stringifies every path part, so array
    indices order lexically. That is a real cost of the stringify and it is
    pinned here rather than left for someone to discover -- see the next test
    for what it buys.
    """
    items = "\n".join(f"  - {i}" for i in range(11))
    text = f"som:\n  sku: E1M-AEN801\ncores:\n  m55_hp:\n    app: ./src\nstorage:\n{items}\n"
    data = yaml.safe_load(_board(tmp_path, text).read_text(encoding="utf-8"))

    errors = loader.iter_schema_errors(data)
    locations = ["/".join(str(p) for p in e.absolute_path) for e in errors]

    assert locations == sorted(locations)
    assert locations[:4] == ["storage/0", "storage/1", "storage/10", "storage/2"]


def test_mixed_int_and_str_path_parts_sort_instead_of_raising(loader, tmp_path):
    """The reason the sort key stringifies at all.

    `absolute_path` is a deque mixing ints (array indices, and integer object
    keys -- YAML happily parses `1:` as one) with strs. Two errors whose paths
    diverge at the same depth on that boundary make a RAW list comparison
    raise `TypeError: '<' not supported between instances of 'str' and 'int'`,
    which surfaces as a planner crash rather than a schema refusal.

    Driven through `schema_path` against a minimal schema because the shipped
    `board.schema.json` cannot express it: its `cores` closes with
    `additionalProperties: false`, so an integer core id is reported AT
    `cores` and never descends into a path that carries the int. The
    stringify is written against a shape the current schema file happens not
    to reach -- which is exactly why nothing was exercising it.
    """
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {"cores": {"type": "object", "additionalProperties": {"type": "object"}}},
    }
    schema_path = tmp_path / "mixed-path.schema.json"
    schema_path.write_text(json.dumps(schema), encoding="utf-8")
    data = yaml.safe_load("cores:\n  1: not-an-object\n  m55_hp: also-not-an-object\n")

    errors = loader.iter_schema_errors(data, schema_path)

    assert [list(e.absolute_path) for e in errors] == [["cores", 1], ["cores", "m55_hp"]]
    with pytest.raises(TypeError):
        sorted(errors, key=lambda e: list(e.absolute_path))


# ---------------------------------------------------------------------------
# The dialect.
# ---------------------------------------------------------------------------


def test_the_schema_file_declares_the_dialect_the_validator_uses(loader):
    """`load_board_schema` reads the file; `iter_schema_errors` hard-codes
    `Draft202012Validator`. Nothing ties the two together, so this does --
    a schema re-declared under another `$schema` would otherwise be validated
    under 2020-12 rules regardless, silently.
    """
    schema = loader.load_board_schema()
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"


def test_a_2020_12_only_keyword_is_enforced_not_ignored(loader, tmp_path):
    """A dialect swap must not be able to pass silently.

    `prefixItems` is the discriminator: it is 2020-12's tuple validation, and
    a Draft-07 validator does not know the keyword at all -- an unknown
    keyword is IGNORED, so the identical document validates clean there.
    Asserting both halves is the point; asserting only that the error appears
    would still pass under a dialect that happened to keep the keyword.
    """
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {"cores": {"type": "array", "prefixItems": [{"type": "string"}]}},
    }
    schema_path = tmp_path / "dialect.schema.json"
    schema_path.write_text(json.dumps(schema), encoding="utf-8")
    data = {"cores": [7]}

    errors = loader.iter_schema_errors(data, schema_path)

    assert [(list(e.absolute_path), e.message) for e in errors] == [
        (["cores", 0], "7 is not of type 'string'")
    ]
    assert list(jsonschema.Draft7Validator(schema).iter_errors(data)) == []
