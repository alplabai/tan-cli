# SPDX-License-Identifier: Apache-2.0
"""Gate: every literal `blob_format` a real producer writes into a `.alpmodel`
manifest must be a member of `tan.model.manifest.VALID_BLOB_FORMATS` -- the
set that `Manifest.from_dict` enforces on decode (tan-cli#1074).

## Why this exists

`test_manifest.py::test_every_valid_blob_format_round_trips_through_dict_and_cbor`
parametrizes over `sorted(manifest.VALID_BLOB_FORMATS)` -- the set under test.
It proves "everything in the set round-trips"; it can NEVER prove "every
producer's format is in the set", because removing a member from the set just
removes one parametrized case, not a failure. Measured directly: with
`"executorch"` deleted from `VALID_BLOB_FORMATS` -- the exact state `dev`
carried before this file's enforcement landed -- `tests/model` and
`tests/commands/test_model_command.py` both still passed, 0 failures. The
producer census tan-cli#1074 is built on is the whole safety argument for
enforcing the set at all, and nothing pinned it.

Worse, the failure this leaves open is near-silent, not loud: enforcement is
READ-side only (`Manifest.from_dict`); `build.py`'s write path never consults
`VALID_BLOB_FORMATS` at all, and `model_cmd._shipped_caveat_issues` downgrades
a failed manifest read-back to a `model.caveat-readback-failed` *warning* --
so `tan model build` writes the bad package and still exits 0. Only a later
`tan model check` / flash / deploy read hits the refusal.

## What this checks, and why it is not circular

This gate derives the PRODUCER side from the source tree, not from a second
hand-maintained literal list (that would just move the circularity from the
round-trip test to here). It AST-walks two known-real producer shapes on
`dev`, both established by the tan-cli#1074 census and independently
re-derived by PR #1098 review as complete (a repo-wide `git grep` over
`Blob(`, `format=`, `blob_format`, `alpmodel` found no other constructor):

1. `Blob(format="...")` in every `python/tan/model/adapters/*.py` (excluding
   `__init__.py`, which only declares the dataclass) -- the compiler-adapter
   emitters `build.py`'s `_ADAPTERS` registry drives at `tan model build`
   time. This is the shape whose drift (`executorch.py`'s `"executorch"`
   missing from the set) this whole enforcement effort exists to catch.
2. `Target(...)`'s `blob_format` (positional index 2, or the `blob_format=`
   keyword) in `python/tan/model/_gen_fixture.py` -- the OTHER real producer
   the adapters sweep above cannot see: `_onnx_cpu_manifest()`'s `"onnx"`,
   the issue #1254 regression fixture committed to alp-sdk as
   `tests/yocto/onnx_cpu_fixture.h` and decoded by the real on-device
   `alp_model_parse()`. Covering it closes the exact "a producer outside the
   swept directory drifts silently" gap the adapters-only sweep would
   otherwise still have -- the identical class of bug this gate exists to
   catch, just in the one other place it can occur.

Deliberately NOT scanned: `python/tests/**` (test files construct malformed
literals like `"vela_tflite_v2"` on purpose, to prove the decode-side
rejection -- see `test_manifest.py`), and `build.py:200`'s own
`Target(blob_format=blob.format, ...)`, whose value is `blob.format` (a
dynamic attribute read, not a string literal) and so is invisible to the
`ast.Constant` check below by construction -- it is exactly as it should be,
since `blob.format` is already covered by tracing IT back to whichever
adapter constructed the `Blob`.

Both producer shapes are resolved through the SAME two steps, deliberately
symmetric (PR #1098 review round 2 found the walk asymmetric: the `Target`
branch already read a positional argument, the `Blob` branch did not):

* the CALLEE may be a bare name (`Blob(...)`) or an attribute-qualified one
  (`adapters.Blob(...)`, `self._blob_cls(...)`) -- only the callee's own
  `.attr`/`.id` is compared, so an import alias or a qualified reference is
  not a way to hide a literal from this walk;
* the FORMAT argument may be passed by keyword (`format=`/`blob_format=`) or
  positionally, in the field's own dataclass slot (`Blob`'s `format` is
  field 0, `Target`'s `blob_format` is field 2) -- matching how a real
  `Target(...)` call in this same file already had to be read positionally.

If that argument is found but is NOT a literal string -- computed
(f-string, variable, attribute access, `**`-splat) -- this is a HARD
FAILURE, not a silent skip: a producer this gate cannot statically verify
is exactly as unsafe as one it never looked at, and a skip that reads as
"nothing to report" is the same false-confidence shape as the unenforced
`VALID_BLOB_FORMATS` this whole file exists to stop recurring. Extend this
gate (teach it the new shape) or make the producer's own construction a
literal -- do not silence the failure."""
from __future__ import annotations
import ast
from pathlib import Path
from tan.model.manifest import VALID_BLOB_FORMATS

_MODEL_ROOT = Path(__file__).resolve().parents[2] / "tan" / "model"
_ADAPTERS_DIR = _MODEL_ROOT / "adapters"
_GEN_FIXTURE = _MODEL_ROOT / "_gen_fixture.py"


def _string_constant(node: ast.expr) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _callee_name(func: ast.expr) -> str | None:
    """Resolves a call's callee to its bare name for both an ordinary
    `Blob(...)` (an `ast.Name`) and an attribute-qualified
    `adapters.Blob(...)` (an `ast.Attribute`) -- PR #1098 review round 2
    found the walk below dropped the second shape entirely by requiring
    `isinstance(node.func, ast.Name)`."""
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _format_argument(node: ast.Call, keyword: str, position: int) -> ast.expr | None:
    """The format-carrying argument expression for one `Blob`/`Target` call,
    by keyword first (the shape every current producer uses), else the same
    field's own positional slot (`Blob`'s `format` is dataclass field 0,
    `Target`'s `blob_format` is field 2) -- the `Target` branch already had
    to read positionally; the `Blob` branch did not, which is exactly the
    asymmetry PR #1098 review round 2 caught (`Blob("positional_fmt", b"")`
    is an ordinary construction, not an exotic one)."""
    for kw in node.keywords:
        if kw.arg == keyword:
            return kw.value
    if len(node.args) > position:
        return node.args[position]
    return None


def _literal_blob_formats(path: Path) -> list[tuple[str, int]]:
    """Every literal `blob_format` string `path` constructs, as
    `(value, lineno)` pairs -- see the module docstring above for the two
    producer shapes and the callee/argument resolution both go through.

    Raises (does not silently skip) if a `Blob`/`Target` call supplies a
    format argument that is present but NOT a literal string -- see the
    module docstring's "HARD FAILURE" paragraph for why a silent skip is
    the wrong default here."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _callee_name(node.func)
        if name == "Blob":
            arg = _format_argument(node, "format", 0)
        elif name == "Target":
            arg = _format_argument(node, "blob_format", 2)
        else:
            continue
        if arg is None:
            continue
        value = _string_constant(arg)
        if value is None:
            raise AssertionError(
                f"{path}:{node.lineno} constructs {name}(...) with a "
                f"non-literal format/blob_format argument -- this gate "
                f"cannot verify it statically. Make the argument a literal "
                f"string, or extend this gate to resolve the new shape "
                f"(see the module docstring's \"HARD FAILURE\" paragraph)."
            )
        found.append((value, node.lineno))
    return found


def _producer_files() -> list[Path]:
    adapters = sorted(p for p in _ADAPTERS_DIR.glob("*.py") if p.name != "__init__.py")
    return adapters + [_GEN_FIXTURE]


def test_every_producer_literal_blob_format_is_in_valid_blob_formats():
    unknown = []
    for path in _producer_files():
        for value, lineno in _literal_blob_formats(path):
            if value not in VALID_BLOB_FORMATS:
                unknown.append(f"{path.relative_to(_MODEL_ROOT.parents[1])}:{lineno} "
                                f"writes blob_format {value!r}, not in VALID_BLOB_FORMATS "
                                f"{sorted(VALID_BLOB_FORMATS)}")
    assert unknown == [], (
        "a real .alpmodel producer emits a blob_format that Manifest.from_dict "
        "would refuse to read back -- add it to VALID_BLOB_FORMATS (tan.model.manifest) "
        "if it is a real, intended format, or fix the producer if it is not:\n  "
        + "\n  ".join(unknown)
    )


def test_producer_sweep_finds_the_known_producers():
    """Pins the census itself, not just the membership check above -- a typo
    in `_ADAPTERS_DIR`/`_GEN_FIXTURE`, or an adapter file renamed out from
    under the glob, would make `test_every_producer_literal_blob_format_is_in_valid_blob_formats`
    vacuously pass by finding nothing to check. Fails loudly instead."""
    all_found = {value for path in _producer_files() for value, _ in _literal_blob_formats(path)}
    assert all_found == {"tflite", "vela_tflite", "drpai_dir", "dxnn", "executorch", "onnx"}, (
        "the producer sweep found a different set of literal blob_formats than "
        "this test pins -- if this is a LEGITIMATE new producer, add its format "
        "to this literal set (and to VALID_BLOB_FORMATS in tan.model.manifest, "
        "if it is not there already); if the sweep found nothing new, a producer "
        "file was probably renamed or moved out from under _ADAPTERS_DIR/"
        "_GEN_FIXTURE above"
    )
