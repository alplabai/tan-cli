# SPDX-License-Identifier: Apache-2.0
"""Manifest data model for .alpmodel packages (canonical Python form).

`Target.caveats` is ADDITIVE AND OPTIONAL ON THE WIRE, deliberately (see
`_target_dict`): it is emitted only when non-empty, so a package with no
caveated target is byte-identical to one this module produced before the
field existed -- including alp-sdk's three committed C-test fixtures
(`tests/fixtures/alpmodel/minimal.alpmodel`,
`tests/unit/alpmodel_reader/src/fixture.h`, `tests/yocto/onnx_cpu_fixture.h`,
written by `tan.model._gen_fixture`), which therefore did NOT have to be
regenerated for it.

WHY IT IS SAFE TO ADD WITHOUT MOVING `package.CONTAINER_VERSION` -- measured,
not assumed. alp-sdk's on-device reader (`src/common/alp_model.c`,
`alp_model_parse`) walks the manifest key-by-key and ends BOTH its top-level
map loop and its per-target map loop with `else { ok = zcbor_any_skip(zs,
NULL); }`, so a key it does not know is skipped, not mis-indexed and not
rejected. `zcbor_any_skip` recurses through a nested list/map on a LOCAL state
copy (`zcbor_decode.c`), drawing nothing from the `zcbor_state_t zs[8]` backup
budget that reader sizes for its own explicit nesting. Compiled natively
against real zcbor and fed a container carrying this exact per-target
`caveats` list, that reader returns ALP_OK with every existing field
byte-identical. Bumping the CONTAINER_VERSION instead would make the same
reader return ALP_ERR_VERSION (-11) for every fielded device -- a breaking
change, for a field no on-device reader needs to read.

Keep the wire shape FLAT (a list of text strings). The reader's skip path
recurses in C per nesting level; a caveat payload has no reason to nest, and a
deeply nested one would spend stack the on-device reader never budgeted for.
"""
from __future__ import annotations
import json as _json
import cbor2 as _cbor2
from dataclasses import dataclass, field, asdict

MANIFEST_SCHEMA_VERSION = 1

_TENSOR_KEYS = {"dtype", "rank", "shape", "scale", "zp"}
_TARGET_KEYS = {"backend", "silicon_ref", "blob_format", "accel_config",
                "arena", "requires", "blob", "compiler_version", "caveats"}
_COV_KEYS = {"backend", "accel_config", "status", "reason"}

# The blob formats the SDK can describe, as of today. NOT enforced at decode:
# `_TARGET_TYPES["blob_format"]` checks only that the value is a `str`, and
# nothing compares a decoded value against this set, so a manifest naming an
# unlisted format decodes clean. Kept as a real constant rather than prose
# because it is the list a writer should pick from -- but do not read it as a
# guard (tan-cli#1074).
VALID_BLOB_FORMATS = frozenset({"vela_tflite", "tflite", "drpai_dir", "dxnn", "onnx"})


def _check_type(v: object, expected: type | tuple[type, ...], type_desc: str,
                 label: str, context: str) -> None:
    """The single type gate every guard in this module routes through --
    shared by `_required_field` (a mapping key that must be there),
    `_optional_field` (one that may not be) and `_check_element_types` (a
    list element), so the curated message shape and the `bool` rule below are
    written once rather than per call site.

    `bool` is rejected whenever `expected` names `int` but not `bool` itself
    (tan-cli#1058 review round 3): Python's `bool` is an `int` subclass, so
    `isinstance(True, int)` is `True` and a plain `isinstance` check would
    otherwise let `arena=True` / `requires["sram_kib"]=True` / `shape=[True]`
    decode silently -- re-emitted on the CBOR wire as byte `0xf5` (major type
    7, "true"), not an unsigned int -- straight into
    `src/backends/inference/alp_model_select.c`'s fit gate as
    `t->req_sram_kib`. A mechanism check here, not a per-field one, so every
    current and future `(int, ...)` entry in `_TENSOR_TYPES`/`_TARGET_TYPES`/
    `_REQUIRES_TYPES`/`_COVERAGE_TYPES`/`_TENSOR_ELEMENT_TYPES`/
    `_TARGET_ELEMENT_TYPES` inherits it automatically. The one comparison in
    this module that does NOT route through here is `from_dict`'s
    schema-version gate, which therefore restates the rule in its own terms
    (tan-cli#1064)."""
    expected_types = expected if isinstance(expected, tuple) else (expected,)
    if not isinstance(v, expected) or (isinstance(v, bool) and bool not in expected_types):
        where = f" in {context}" if context else ""
        raise ValueError(
            f"malformed .alpmodel manifest: {label} must be {type_desc}, got {type(v).__name__}{where}"
        )


def _required_field(d: dict, key: str, expected: type | tuple[type, ...], type_desc: str,
                     *, context: str = "") -> object:
    """Look up a required field in a decoded `.alpmodel` manifest document (a
    JSON object or CBOR map that has already passed the document-level
    `isinstance(d, dict)` guard in `from_json`/`from_cbor`), raising a
    curated `ValueError` naming the field instead of letting the bare
    subscript this replaces escape as a raw `KeyError` (field absent) or,
    worse, being handed to a coercing constructor that manufactures a wrong
    value from the wrong type (`from_cbor`'s old `bytes(d["src_sha"])`
    zero-filled/byte-wise-coerced an int/list/bool `src_sha` into a bogus
    hash with no diagnostic at all -- tan-cli#1023 review round 2, the
    document-level guard stopped one field short of `name`/`src_sha`
    themselves).

    `context`, when given, is appended to both messages as "in <context>" --
    `_decode_list_field` below passes e.g. `"targets[2]"` so a malformed
    nested element's error names WHICH element, the same way the top-level
    calls (no `context`) name the document itself. Genuinely the same check
    both shapes need: `d` here is a decoded sub-document (a `targets[]`
    element) exactly as much as it is the top-level manifest.

    `.alpmodel` packages are machine-generated CBOR/JSON, not hand-authored
    documents like `board.yaml` -- but `package.py`'s own bounds-checked
    header reads already treat their bytes as wire data, not trusted
    first-party output, and a corrupted `src_sha` (the model-source identity
    field written by `build.py:222`) is exactly the class of corruption this
    container's own bounds checks exist to catch. Fail loudly here, the same
    way, rather than degrade silently.

    The type half of the check lives in `_check_type` above, shared with
    `_optional_field` and `_check_element_types`."""
    if key not in d:
        where = f" in {context}" if context else ""
        raise ValueError(f"malformed .alpmodel manifest: missing required field {key!r}{where}")
    _check_type(d[key], expected, type_desc, f"field {key!r}", context)
    return d[key]


def _optional_field(d: dict, key: str, expected: type | tuple[type, ...], type_desc: str,
                     *, context: str = "") -> object | None:
    """`_required_field`'s sibling for a field that is OPTIONAL on the wire:
    ABSENT is fine (the dataclass default stands), PRESENT-but-wrong-typed
    raises exactly the curated `ValueError` a required field would
    (tan-cli#1056).

    A separate helper rather than a flag on `_required_field`, because the
    two differ ONLY in the missing-key branch and overloading that branch
    would make each call site's intent depend on an argument instead of on
    which function it names.

    Returns `None` for an absent field. That is unambiguous rather than
    merely convenient: no `expected` in this module admits `NoneType`, so a
    field that is PRESENT and `None` raises above and never reaches the
    return -- the caller can read `None` as "absent" without a sentinel."""
    if key not in d:
        return None
    _check_type(d[key], expected, type_desc, f"field {key!r}", context)
    return d[key]


def _pick(d: dict, keys: set) -> dict:
    return {k: d[k] for k in keys if k in d}   # drop unknown keys + tolerate missing-known


def _check_nested_types(value: dict, inner_types: dict[str, tuple[type | tuple[type, ...], str]],
                         context: str) -> None:
    """One level of `_required_field` under a field `_decode_list_field` has
    already confirmed is a `dict` (`value`) -- see `nested_types` on that
    function and `_REQUIRES_TYPES` below for the why. Only keys present in
    `inner_types` AND in `value` are checked; a missing inner key is left
    alone, matching `_pick`'s tolerate-missing-known convention elsewhere in
    this module."""
    for inner_key, (inner_expected, inner_desc) in inner_types.items():
        if inner_key in value:
            _required_field(value, inner_key, inner_expected, inner_desc, context=context)


def _check_element_types(value: list, expected: type | tuple[type, ...], type_desc: str,
                          context: str) -> None:
    """`_check_nested_types`'s list-shaped sibling: one level of `_check_type`
    under a field `_check_element_fields` has already confirmed is a `list`
    (tan-cli#1063). The dict helper above takes a per-KEY map; a list has no
    keys, so this one takes a single ELEMENT type and names the offending
    index where the dict side names the key -- `element 1 must be an int, got
    str in inputs[0].shape` is the exact counterpart of `field 'sram_kib'
    must be an int, got str in targets[0].requires`.

    Every element is checked, not just the first: a `shape` whose second
    entry is the corrupted one is as wrong as one whose first is."""
    for i, elem in enumerate(value):
        _check_type(elem, expected, type_desc, f"element {i}", context)


def _check_element_fields(elem: dict, item_keys: set, required: frozenset,
                           field_types: dict[str, tuple[type | tuple[type, ...], str]],
                           nested_types: dict | None, element_types: dict | None,
                           context: str) -> None:
    """Guard ONE decoded element of a nested list field.

    `required` fields first (alphabetically, so a message names the same
    field it always has), then the OPTIONAL remainder of `item_keys` --
    `Target.compiler_version`/`caveats` are carried through to the
    constructor by `_pick` but sit in no `required` set, so before
    tan-cli#1056 nothing type-checked them at all and
    `{"compiler_version": 12345, "caveats": "not-a-list"}` constructed
    silently.

    `field_types` gives each field a REAL `expected` type, not the
    accepts-anything `object` an earlier version passed (tan-cli#1049).
    `nested_types` routes an already-checked-`dict` value through
    `_check_nested_types` (see `_REQUIRES_TYPES`); `element_types` routes an
    already-checked-`list` value through `_check_element_types` (see
    `_TENSOR_ELEMENT_TYPES`, tan-cli#1063)."""
    for field_name in sorted(required) + sorted(item_keys - required):
        expected, type_desc = field_types[field_name]
        if field_name in required:
            value = _required_field(elem, field_name, expected, type_desc, context=context)
        else:
            value = _optional_field(elem, field_name, expected, type_desc, context=context)
            if value is None:
                continue
        inner_types = nested_types.get(field_name) if nested_types else None
        if inner_types:
            _check_nested_types(value, inner_types, context=f"{context}.{field_name}")
        elem_type = element_types.get(field_name) if element_types else None
        if elem_type:
            _check_element_types(value, elem_type[0], elem_type[1],
                                  context=f"{context}.{field_name}")


def _decode_list_field(d: dict, key: str, item_keys: set, required: frozenset, ctor,
                        field_types: dict[str, tuple[type | tuple[type, ...], str]],
                        nested_types: dict[str, dict[str, tuple[type | tuple[type, ...], str]]]
                        | None = None,
                        element_types: dict[str, tuple[type | tuple[type, ...], str]]
                        | None = None) -> list:
    """Decode a nested `.alpmodel` manifest list field (`inputs`/`outputs`/
    `targets`/`coverage`) into `ctor` instances, guarding both the field and
    each element the way `_required_field` guards a top-level one
    (tan-cli#1040). Before this, every reader built these lists straight out
    of `ctor(**t)` (`from_dict`) or `ctor(**_pick(t, item_keys))` (the old
    `from_cbor`) with no element-type or completeness check: a non-mapping
    element degrades `_pick` to `{}` rather than raising (`k in 'abc'` is a
    legal, false, substring check), so a raw `TypeError` out of the dataclass
    constructor was the first thing to notice, not a curated `ValueError` --
    `inputs=['abc']` -> `Tensor.__init__() missing 5 required positional
    arguments`; an incomplete `targets[]` entry names 7. `from_dict`'s own
    bare `Tensor(**t)` (no `_pick`) failed even earlier on the `**` unpack
    itself. One helper now closes both readers, since `from_cbor` routes
    through `from_dict`. The per-field work moved to `_check_element_fields`
    above -- see it for what `field_types`/`nested_types`/`element_types`
    each do.

    The field-level check below (is `d[key]` a list at all) is the same
    shape one step up: a `targets` value that decodes but isn't a list would
    otherwise reach `enumerate()` and, for a string, iterate its CHARACTERS
    as "elements" -- silently wrong, not a crash."""
    elems = d.get(key, [])
    if not isinstance(elems, list):
        raise ValueError(
            f"malformed .alpmodel manifest: field {key!r} must be a list, got {type(elems).__name__}"
        )
    out = []
    for i, elem in enumerate(elems):
        if not isinstance(elem, dict):
            raise ValueError(
                f"malformed .alpmodel manifest: {key}[{i}] must be a mapping, "
                f"got {type(elem).__name__}"
            )
        _check_element_fields(elem, item_keys, required, field_types,
                               nested_types, element_types, context=f"{key}[{i}]")
        out.append(ctor(**_pick(elem, item_keys)))
    return out


_TARGET_REQUIRED = frozenset(_TARGET_KEYS - {"compiler_version", "caveats"})

# Per-field `(expected type, type_desc)` for `_decode_list_field`'s element
# guard (tan-cli#1049) -- one entry per member of the `required` frozenset
# each caller below passes, so every required field of `Tensor`/`Target`/
# `Coverage` gets a real type check, not the `object`-accepts-anything one
# `_required_field`'s presence-only call used before -- `arena` and
# `requires["sram_kib"]` are exactly the figures
# `src/backends/inference/alp_model_select.c`'s fit gate reads, so a
# wrong-typed value there was the silent-wrong-value class, not a crash.
# `scale` accepts `(int, float)`: a whole-number scale is legal JSON/CBOR
# content (`1` is as valid as `1.0`) and the dataclass field is a plain
# `float` annotation, not an enforced constraint -- rejecting `int` here
# would be a new, undocumented restriction on well-formed input, not a
# defect fix.
_TENSOR_TYPES = {
    "dtype": (str, "a string"),
    "rank": (int, "an int"),
    "shape": (list, "a list"),
    "scale": ((int, float), "a number"),
    "zp": (int, "an int"),
}
_TARGET_TYPES = {
    "backend": (str, "a string"),
    "silicon_ref": (str, "a string"),
    "blob_format": (str, "a string"),
    "accel_config": (str, "a string"),
    "arena": (int, "an int"),
    "requires": (dict, "a mapping"),
    "blob": (int, "an int"),
    # OPTIONAL on the wire (`_TARGET_REQUIRED` subtracts both), so these two
    # reach `_check_element_fields` through `_optional_field`, not
    # `_required_field`: absent is legal, present-but-wrong-typed is not.
    # Before tan-cli#1056 they were in `_TARGET_KEYS` (so `_pick` handed them
    # to the constructor) but in no type map at all, so
    # `compiler_version=12345` / `caveats="not-a-list"` decoded silently.
    "compiler_version": (str, "a string"),
    "caveats": (list, "a list"),
}

# One level deeper than `_TARGET_TYPES["requires"]`, which only guards the
# container is a mapping at all (tan-cli#1049 review round 2). `sram_kib` is
# the other half of the same fit-gate figure `arena` is -- when present it
# must be an `int`, the same way `arena` itself already is. `op_features` is
# not checked here: nothing downstream reads it as anything but an opaque
# list yet, so adding a shape requirement for it now would be a new,
# undocumented restriction rather than closing a known gap.
_REQUIRES_TYPES = {
    "sram_kib": (int, "an int"),
}
# Element types for a field whose own declared type is `list` -- the entries
# above guard the CONTAINER only, which is the same shape tan-cli#1049 closed
# for `Target.requires` one level down (tan-cli#1063). `shape` feeds
# tensor-shape maths downstream the way `requires["sram_kib"]` feeds
# `src/backends/inference/alp_model_select.c`'s fit gate, so
# `shape=["a","b"]` / `[None,None]` / `[{"a":1}]` / `[True,False]` were all
# silent-wrong values, not crashes. `caveats` is `list[str]` that `tan model
# build` and `tan model check --exact` render as customer-readable text, so a
# non-string element degrades a diagnostic surface silently (tan-cli#1056).
#
# `op_features` inside `requires` deliberately gets no entry here, for the
# same reason `_REQUIRES_TYPES` omits it: nothing downstream reads it as
# anything but an opaque list yet, so a shape requirement would be a new,
# undocumented restriction rather than a closed gap.
_TENSOR_ELEMENT_TYPES = {
    "shape": (int, "an int"),
}
_TARGET_ELEMENT_TYPES = {
    "caveats": (str, "a string"),
}
_COVERAGE_TYPES = {
    "backend": (str, "a string"),
    "accel_config": (str, "a string"),
    "status": (str, "a string"),
    "reason": (str, "a string"),
}


def _json_default(d: dict) -> dict:
    out = dict(d)
    out["src_sha"] = d["src_sha"].hex()          # bytes -> hex string for JSON
    return out


def _target_dict(t: "Target") -> dict:
    """One target's wire form: `asdict`, minus an EMPTY `caveats`.

    Dropping the empty case is what keeps this field additive in the strict
    sense -- a package whose targets carry no caveat encodes to exactly the
    bytes it did before `caveats` existed, so no committed fixture, no shipped
    package and no on-device footprint moves for a field that has nothing to
    say. Absent means "no caveats"; every reader must treat it that way (the
    `_pick`-based `_decode_list_field` both `from_json` and `from_cbor` route
    through already does, and so does `Target.caveats`'s own default)."""
    d = asdict(t)
    if not d["caveats"]:
        del d["caveats"]
    return d


@dataclass
class Tensor:
    dtype: str
    rank: int
    shape: list[int]
    scale: float
    zp: int


@dataclass
class Target:
    backend: str            # cpu | ethos_u | drpai | deepx_dxm1
    silicon_ref: str        # e.g. "alif:ensemble:e8" or "*"
    blob_format: str        # conventionally one of VALID_BLOB_FORMATS; not enforced
    accel_config: str       # "" when N/A
    arena: int
    requires: dict[str, object]  # {"sram_kib": int, "op_features": list[str]}
    blob: int               # index into the package blob table
    compiler_version: str = ""   # e.g. "vela 4.1.0" | "passthrough"; "" when unknown
    # The compiler's OWN unresolved caveats about the blob shipped for THIS
    # target -- each already a complete, customer-readable sentence, carried
    # verbatim from `adapters.Blob.caveats`. Today that is vela's "Compilation
    # may be invalid or non-optimal" verdict when it fell back to its BUILT-IN
    # default profile, whose memory model may not be the module's.
    #
    # This field exists because `arena` and `requires["sram_kib"]` beside it
    # are exactly the figures alp-sdk's on-device selector consumes
    # (`return e->arena_sram_kib == 0u || t->req_sram_kib <= e->arena_sram_kib;`,
    # src/backends/inference/alp_model_select.c) -- so a blob compiled against
    # a memory model the module does not have must not be able to reach a
    # board with the package silent about it. `tan model check --exact` was
    # already surfacing these; `tan model build` dropped them on the floor,
    # which is the path that actually ships bytes to hardware.
    #
    # It is DIAGNOSTIC, never a substitute for refusing: a figure that cannot
    # be derived is refused at the adapter
    # (`ethos_u._refuse_zero_sram_footprint`), not caveated and shipped.
    # Empty is omitted from the wire form entirely -- see `_target_dict`.
    caveats: list[str] = field(default_factory=list)


@dataclass
class Coverage:
    """A backend the package did NOT include a blob for, and why."""

    backend: str
    accel_config: str
    status: str             # compiled | skipped | incompatible
    reason: str


@dataclass
class Manifest:
    name: str
    src_sha: bytes
    inputs: list[Tensor] = field(default_factory=list)
    outputs: list[Tensor] = field(default_factory=list)
    targets: list[Target] = field(default_factory=list)
    coverage: list[Coverage] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "v": MANIFEST_SCHEMA_VERSION,
            "name": self.name,
            "src_sha": self.src_sha,
            "inputs": [asdict(t) for t in self.inputs],
            "outputs": [asdict(t) for t in self.outputs],
            "targets": [_target_dict(t) for t in self.targets],
            "coverage": [asdict(c) for c in self.coverage],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Manifest":
        # Both `from_json` and `from_cbor` route through here (tan-cli#1039)
        # -- the version check and the guarded nested-list decode below are
        # therefore each written ONCE and enforced on both reader paths,
        # rather than the version check living only in the JSON-era code
        # this method used to be (`from_cbor` built a `Manifest` directly
        # and never called this method at all, so a future manifest version
        # read by an older `tan` over CBOR -- the PRODUCTION reader path for
        # `.alpmodel` packages -- was silently parsed as v1 instead of
        # refused).
        v = d.get("v", MANIFEST_SCHEMA_VERSION)
        # `type(v) is not int`, NOT `isinstance` (tan-cli#1064): `bool` is an
        # `int` subclass and `True == 1`, so the bare comparison accepted
        # `v=True` as v1 -- and float `1.0` too. This is the one comparison in
        # this module that reads a decoded value without routing through
        # `_required_field`/`_check_type`, so it restates their `bool` rule in
        # its own terms rather than inheriting it. A future manifest version
        # written as `1.0` or `True` is a manifest this reader does not
        # understand, and must be refused as loudly as `v=2` already was.
        if type(v) is not int or v != MANIFEST_SCHEMA_VERSION:
            raise ValueError(f"unsupported manifest version {v!r}; expected {MANIFEST_SCHEMA_VERSION}")
        return cls(
            name=d["name"],
            src_sha=d["src_sha"],  # raw bytes pass-through; JSON/text callers must decode to bytes first
            inputs=_decode_list_field(d, "inputs", _TENSOR_KEYS, _TENSOR_KEYS, Tensor, _TENSOR_TYPES,
                                       element_types=_TENSOR_ELEMENT_TYPES),
            outputs=_decode_list_field(d, "outputs", _TENSOR_KEYS, _TENSOR_KEYS, Tensor, _TENSOR_TYPES,
                                        element_types=_TENSOR_ELEMENT_TYPES),
            targets=_decode_list_field(d, "targets", _TARGET_KEYS, _TARGET_REQUIRED, Target, _TARGET_TYPES,
                                        nested_types={"requires": _REQUIRES_TYPES},
                                        element_types=_TARGET_ELEMENT_TYPES),
            coverage=_decode_list_field(d, "coverage", _COV_KEYS, _COV_KEYS, Coverage, _COVERAGE_TYPES),
        )

    def to_json(self) -> str:
        return _json.dumps(_json_default(self.to_dict()), indent=2)

    @classmethod
    def from_json(cls, text: str) -> "Manifest":
        d = _json.loads(text)
        if not isinstance(d, dict):
            # Mirrors the `preset` guard in `targets.resolve_targets`
            # (tan-cli#1018): a `.alpmodel` manifest that decodes but is
            # not a JSON object (e.g. a bare list or a bare scalar) must
            # not reach the bare `d["src_sha"]` subscript below -- that
            # raises a raw TypeError ("list indices must be integers or
            # slices, not str" / "string indices must be integers, not
            # 'str'") instead of a curated error a caller can distinguish
            # from any other TypeError in the call stack (tan-cli#1023).
            # `package.py`'s own bounds-checked reads of the untrusted
            # header already treat manifest bytes as wire data, not
            # trusted first-party output -- a corrupt/adversarial manifest
            # fails loudly here for the same reason, rather than being
            # silently tolerated.
            raise ValueError(
                f"malformed .alpmodel manifest: expected a JSON object, got {type(d).__name__}"
            )
        # `d["name"]`/`d["src_sha"]` were still bare here even after the
        # document-level guard above -- a manifest missing either raised a
        # raw KeyError, and a non-string `src_sha` raised a raw
        # `TypeError: fromhex() argument must be str, not int` out of
        # `bytes.fromhex` below, neither distinguishable from an unrelated
        # bug in the call stack (tan-cli#1023 review round 2). `_required_field`
        # closes both the same way the document guard above does.
        _required_field(d, "name", str, "a string")
        src_sha_hex = _required_field(d, "src_sha", str, "a hex string")
        try:
            d["src_sha"] = bytes.fromhex(src_sha_hex)
        except ValueError as exc:
            raise ValueError(f"malformed .alpmodel manifest: field 'src_sha' is not valid hex: {exc}") from exc
        return cls.from_dict(d)

    def to_cbor(self) -> bytes:
        return _cbor2.dumps(self.to_dict())

    # tan-cli#1055: cbor2's decode exceptions (`CBORDecodeEOF` and its
    # siblings, common base `CBORDecodeError`) are NOT `ValueError`
    # subclasses, while `from_json`'s `json.JSONDecodeError` IS -- so a caller
    # catching `ValueError` around a `.alpmodel` read, the contract every other
    # guard in this module exists to honour, caught a malformed JSON manifest
    # and missed a malformed CBOR one, on the PRODUCTION reader path.
    # `package.read_package`/`read_manifest_file` bounds-check the manifest
    # region's offset and length (tan-cli#1045) but never that its bytes are
    # well-formed CBOR: a bit flip, a truncated write or plain non-CBOR bytes
    # land in the `except` below.
    @classmethod
    def from_cbor(cls, blob: bytes) -> "Manifest":
        try:
            d = _cbor2.loads(blob)
        except _cbor2.CBORDecodeError as exc:
            raise ValueError(
                f"malformed .alpmodel manifest: not valid CBOR "
                f"({type(exc).__name__}: {exc})"
            ) from exc
        if not isinstance(d, dict):
            # See `from_json` above (tan-cli#1023): guard before the bare
            # `d["name"]` subscript below, reached from both
            # `package.read_manifest_file` and `package.read_package`.
            raise ValueError(
                f"malformed .alpmodel manifest: expected a CBOR map, got {type(d).__name__}"
            )
        # Same gap as `from_json` above, plus a worse failure mode: CBOR
        # preserves `src_sha`'s wire type (unlike JSON's hex string), so an
        # unguarded `bytes(d["src_sha"])` didn't raise at all for a
        # wrong-typed `src_sha` -- `bytes(int)` is a zero-fill constructor
        # and `bytes(list[int])` a byte-wise one, so a manifest whose
        # `src_sha` CBOR major type got flipped from byte-string to
        # unsigned-int/array/bool silently produced a `Manifest` carrying an
        # INVENTED source hash (tan-cli#1023 review round 2, finding 1).
        # `_required_field` requires the real wire type before `bytes()`
        # ever runs on it.
        _required_field(d, "name", str, "a string")
        src_sha_raw = _required_field(d, "src_sha", (bytes, bytearray), "a byte string")
        # Normalise in place (bytearray -> bytes, mirroring `from_json`'s
        # hex-string -> bytes normalisation two methods up) and hand off to
        # `from_dict` -- which is what actually enforces `MANIFEST_SCHEMA_VERSION`
        # and guards the nested `inputs`/`outputs`/`targets`/`coverage`
        # decodes (tan-cli#1039, tan-cli#1040). Before this, `from_cbor` built
        # the `Manifest` directly and never reached either check: CBOR is the
        # PRODUCTION reader path for `.alpmodel` packages, so the stricter
        # checks used to sit on the path that matters less.
        d["src_sha"] = bytes(src_sha_raw)
        return cls.from_dict(d)
