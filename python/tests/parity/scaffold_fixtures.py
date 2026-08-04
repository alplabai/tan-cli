# SPDX-License-Identifier: Apache-2.0
"""A tree-shaped fixture store, for the one parity axis `oracle_fixtures`
cannot hold (tan-cli#409).

`oracle_fixtures.resolve` freezes an `(exit code, envelope dict)` pair keyed
off `PYTEST_CURRENT_TEST`. `test_scaffold_content_oracle_parity.py` compares
neither of those things: it diffs a whole scaffolded FILE TREE, and it does
so from TWO tests that share one memoised spawn per template
(`_TREE_CACHE`). Both facts break the node-keyed store:

* the compared value is `{relative_path: bytes}`, not an envelope;
* whichever of the two tests happens to run FIRST is the one that would
  resolve, so the fixture key would depend on collection order. A key that
  moves when a test is renamed, reordered, or run with `-k` is not a frozen
  answer.

So this keys by TEMPLATE ID -- the thing that actually determines the tree --
in its own file, and is deliberately tiny: one `resolve_tree`, with the same
three-mode contract `oracle_fixtures` has (replay / live / live+capture) so
there is one set of environment variables to know, not two.

## Why the values are TEXT and not base64

Measured on this tree, all six templates at `DEFAULT_SOM_SKU`: **40 files,
every one valid UTF-8, and not one containing the scratch directory it was
scaffolded into.** So there is nothing to scrub and nothing to encode, and
storing text keeps the committed fixture reviewable -- a diff that changes a
scaffolded `CMakeLists.txt` shows up as that CMake text changing, which is
the entire point of freezing it.

That is a measurement, not an assumption, so :func:`_as_text` FAILS the
capture on a byte sequence that is not UTF-8 rather than laundering it
through `errors="replace"`. If a template ever vendors a binary asset, the
capture stops and this decision gets revisited -- it does not silently
freeze a mangled file.

## Newlines

`json.dumps` round-trips ``\\n`` and ``\\r\\n`` distinctly, and the reader
below does no newline translation, so a CRLF-vs-LF divergence between the two
binaries still fails the comparison. That matters: `_read_tree`'s own
docstring says it reads raw bytes precisely so nothing launders a real
divergence, and this store must not undo that.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Callable

#: Same directory as the envelope store, so one `oracle_fixtures/` holds
#: everything a replay needs and `PROVENANCE.txt` can describe both.
FIXTURES_DIR = Path(__file__).resolve().parent / "oracle_fixtures"
FIXTURE_PATH = FIXTURES_DIR / "scaffold_trees.json"

#: The same two switches `oracle_fixtures` reads, deliberately -- one recipe
#: captures both stores. `CAPTURE` is meaningless without `LIVE` (there is
#: nothing to persist otherwise), so it is folded in here rather than left
#: for the call site.
LIVE = os.environ.get("TAN_PARITY_LIVE") == "1"
CAPTURE = LIVE and os.environ.get("TAN_PARITY_CAPTURE") == "1"

_CAPTURE_RECIPE = (
    "cargo build --bin tan && TAN_PARITY_LIVE=1 TAN_PARITY_CAPTURE=1 "
    "TAN_RUST_BINARY=<path> python -m pytest "
    "tests/parity/test_scaffold_content_oracle_parity.py"
)


def _load() -> dict[str, dict[str, str]]:
    if not FIXTURE_PATH.exists():
        return {}
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def _save(data: dict[str, dict[str, str]]) -> None:
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    # Sorted + trailing newline: a capture run must produce a MINIMAL diff, or
    # re-capturing one template rewrites the whole file and buries the change
    # that mattered.
    FIXTURE_PATH.write_text(
        json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _as_text(tree: dict[str, bytes], template_id: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for relative_path, raw in sorted(tree.items()):
        try:
            out[relative_path] = raw.decode("utf-8")
        except UnicodeDecodeError as err:
            raise AssertionError(
                f"--template {template_id} wrote {relative_path!r}, which is not "
                f"UTF-8 ({err}). This store holds text so the committed fixture "
                f"stays reviewable; a binary asset needs that decision revisited "
                f"(base64, or a separate blob store), not a lossy decode."
            ) from err
    return out


def resolve_tree(template_id: str, live_fn: Callable[[], dict[str, bytes]]) -> dict[str, bytes]:
    """The oracle's scaffolded tree for `template_id`, as
    `{relative_path: bytes}`.

    Frozen replay by default -- `live_fn` is never called, and no Rust binary
    is needed. `TAN_PARITY_LIVE=1` spawns the oracle instead; adding
    `TAN_PARITY_CAPTURE=1` also writes what it produced.

    Returns BYTES on every path, including replay, so the caller compares the
    same type in both modes. The text round trip lives entirely inside this
    function.
    """
    if LIVE:
        tree = live_fn()
        as_text = _as_text(tree, template_id)
        if CAPTURE:
            data = _load()
            data[template_id] = as_text
            _save(data)
        return {path: text.encode("utf-8") for path, text in as_text.items()}

    data = _load()
    if template_id not in data:
        raise KeyError(
            f"no frozen scaffold tree for --template {template_id!r} in "
            f"{FIXTURE_PATH.name}. Capture it against a built oracle: {_CAPTURE_RECIPE}"
        )
    return {path: text.encode("utf-8") for path, text in data[template_id].items()}


def frozen_template_ids() -> set[str]:
    """Which templates have a committed tree -- read by the freeze-completeness
    gate, so "is this case frozen?" is answered from the STORE rather than from
    a hand-maintained list that can drift away from it."""
    return set(_load())
