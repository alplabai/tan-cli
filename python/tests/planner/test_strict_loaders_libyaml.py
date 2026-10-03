# SPDX-License-Identifier: Apache-2.0
"""`tan/planner/strict_loaders.py` parses with libyaml (alp-sdk#2328).

Hand-ported from alp-sdk's `tests/scripts/test_strict_loaders.py` additions in
`169a9be3`: `_StrictLoader` subclasses `yaml.CSafeLoader` when PyYAML was built
with libyaml, and `fast_safe_load()` is exactly `yaml.safe_load` on the same
parser -- the lenient sibling `sdk_compat.load_family_table` reads through.
The C path must give exactly what the pure-Python one does: same value, or the
same exception class.

The module is loaded by file path rather than as `tan.planner.strict_loaders`:
it is a dependency-free leaf, and importing it through the package would run
`tan/planner/__init__`, which needs a bound alp-sdk checkout. Without one these
tests still run (the parity walk then covers this repo's own YAML only); with
`ALP_SDK_ROOT` bound they also walk every `metadata/**` YAML the SDK ships --
the files the planner actually reads.
"""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path
from types import ModuleType
from typing import Any, Callable

import pytest
import yaml

from tests.conftest import sdk_root

_PYTHON_DIR = Path(__file__).resolve().parents[2]
_MODULE_PATH = _PYTHON_DIR / "tan" / "planner" / "strict_loaders.py"


#: Captured at import time: the autouse discovery-env scrub deletes
#: `ALP_SDK_ROOT` before a test body runs (see tests/planner/_bound_sdk_fixture.py).
#: `tests.conftest.sdk_root`, not a local reader: a module-level `_bound_sdk`
#: here collided with the shared fixture name
#: (`test_a_shared_test_helper_is_defined_exactly_once`).
SDK = sdk_root()


@pytest.fixture(scope="module")
def sl() -> ModuleType:
    spec = importlib.util.spec_from_file_location("_tan_strict_loaders", _MODULE_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_loaders_use_libyaml_when_pyyaml_has_it(sl: ModuleType) -> None:
    if not yaml.__with_libyaml__:
        pytest.skip("PyYAML built without libyaml -- the pure-Python fallback applies")
    assert issubclass(sl._StrictLoader, yaml.CSafeLoader)
    assert sl.fast_safe_load("som: a\nlist: [1, 2]\n") == {"som": "a", "list": [1, 2]}


def test_fast_safe_load_keeps_the_last_duplicate_like_safe_load(sl: ModuleType) -> None:
    """Lenient on purpose: stdlib semantics, not `strict_yaml_load`'s rejection."""
    assert sl.fast_safe_load("som: a\nsom: b\n") == {"som": "b"}


def test_strict_rejection_message_is_unchanged_on_the_c_parser(sl: ModuleType) -> None:
    """libyaml's marks are 0-based exactly like the pure-Python parser's, so the
    1-based line/column in the message must not shift."""
    with pytest.raises(sl.DuplicateKeyError) as exc:
        sl.strict_yaml_load("a: 1\nsom: a\nsom: b\n", source="board.yaml")
    assert str(exc.value) == "board.yaml: duplicate key 'som' at line 3, column 1"


_MALFORMED = {
    "<duplicate key>": "som: a\nsom: b\n",
    "<duplicate key via merge>": "base: &b {x: 1}\nm:\n  <<: *b\n  y: 2\n  y: 3\n",
    "<tab indent>": "a:\n\tb: 1\n",
    "<unclosed flow sequence>": "a: [1, 2\nb: 3\n",
    "<two documents>": "a: 1\n---\nb: 2\n",
}


def _outcome(fn: Callable[[str], Any], text: str) -> tuple[str, Any]:
    try:
        return ("ok", fn(text))
    except Exception as e:  # noqa: BLE001 -- compare the failure CLASS, not text
        return ("raise", type(e).__name__)


def _tracked_yaml(root: Path, *pathspecs: str) -> list[Path]:
    out = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z", *pathspecs],
        capture_output=True, text=True, encoding="utf-8", check=True,
    ).stdout
    return [root / rel for rel in out.split("\0") if rel]


def test_c_loaders_match_the_pure_python_ones(sl: ModuleType) -> None:
    """Covers both `fast_safe_load` (vs `yaml.safe_load`) and `strict_yaml_load`
    (vs the same duplicate-key constructor on the pure-Python `SafeLoader`)."""

    class _PureStrict(yaml.SafeLoader):
        pass

    _PureStrict.add_constructor(
        yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
        sl._no_duplicates_mapping_constructor,
    )

    def pure_strict(text: str) -> Any:
        return yaml.load(text, Loader=_PureStrict)

    assert all(_outcome(sl.strict_yaml_load, t)[0] == "raise" for t in _MALFORMED.values()), (
        "a malformed fixture no longer raises -- it stopped testing the error path")

    files = _tracked_yaml(_PYTHON_DIR.parent, "*.yaml", "*.yml")
    if SDK is not None:
        files += _tracked_yaml(SDK, "metadata/*.yaml", "metadata/*.yml")
    assert len(files) > 10, files[:5]
    inputs = [(str(p), p.read_text(encoding="utf-8")) for p in files if p.is_file()]
    inputs += list(_MALFORMED.items())

    mismatches = []
    for name, text in inputs:
        if _outcome(sl.fast_safe_load, text) != _outcome(yaml.safe_load, text):
            mismatches.append(("fast_safe_load", name))
        if _outcome(sl.strict_yaml_load, text) != _outcome(pure_strict, text):
            mismatches.append(("strict_yaml_load", name))
    assert not mismatches, mismatches[:10]
