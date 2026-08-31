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

# The blob formats the SDK can describe. A real constant, not a comment, so a
# new backend cannot silently invent a format string.
VALID_BLOB_FORMATS = frozenset({"vela_tflite", "tflite", "drpai_dir", "dxnn", "onnx"})


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

    `bool` is rejected whenever `expected` names `int` but not `bool` itself
    (tan-cli#1058 review round 3): Python's `bool` is an `int` subclass, so
    `isinstance(True, int)` is `True` and the plain `isinstance` check below
    would otherwise let `arena=True` / `requires["sram_kib"]=True` decode
    silently -- re-emitted on the CBOR wire as byte `0xf5` (major type 7,
    "true"), not an unsigned int -- straight into
    `src/backends/inference/alp_model_select.c`'s fit gate as `t->req_sram_kib`.
    A mechanism check here, not a per-field one, so every current and future
    `(int, ...)` entry in `_TENSOR_TYPES`/`_TARGET_TYPES`/`_REQUIRES_TYPES`/
    `_COVERAGE_TYPES` inherits it automatically."""
    where = f" in {context}" if context else ""
    if key not in d:
        raise ValueError(f"malformed .alpmodel manifest: missing required field {key!r}{where}")
    v = d[key]
    expected_types = expected if isinstance(expected, tuple) else (expected,)
    wrong_type = not isinstance(v, expected) or (isinstance(v, bool) and bool not in expected_types)
    if wrong_type:
        raise ValueError(
            f"malformed .alpmodel manifest: field {key!r} must be {type_desc}, got {type(v).__name__}{where}"
        )
    return v


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


def _decode_list_field(d: dict, key: str, item_keys: set, required: frozenset, ctor,
                        field_types: dict[str, tuple[type | tuple[type, ...], str]],
                        nested_types: dict[str, dict[str, tuple[type | tuple[type, ...], str]]]
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
    through `from_dict`.
    `field_types` gives `_required_field` a REAL `expected` type per field,
    not the accepts-anything `object` an earlier version passed (tan-cli#1049
    -- see `_TENSOR_TYPES`/`_TARGET_TYPES`/`_COVERAGE_TYPES` below for the
    why/what). Only `required` fields are checked; `compiler_version`/
    `caveats` are a separate gap (tan-cli#1056). `nested_types` routes a
    field's already-checked-`dict` value through `_check_nested_types` --
    see `_REQUIRES_TYPES` below for the why.

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
        for field_name in sorted(required):
            expected, type_desc = field_types[field_name]
            value = _required_field(elem, field_name, expected, type_desc, context=f"{key}[{i}]")
            inner_types = nested_types.get(field_name) if nested_types else None
            if inner_types:
                _check_nested_types(value, inner_types, context=f"{key}[{i}].{field_name}")
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
    blob_format: str        # one of VALID_BLOB_FORMATS
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
        if v != MANIFEST_SCHEMA_VERSION:
            raise ValueError(f"unsupported manifest version {v!r}; expected {MANIFEST_SCHEMA_VERSION}")
        return cls(
            name=d["name"],
            src_sha=d["src_sha"],  # raw bytes pass-through; JSON/text callers must decode to bytes first
            inputs=_decode_list_field(d, "inputs", _TENSOR_KEYS, _TENSOR_KEYS, Tensor, _TENSOR_TYPES),
            outputs=_decode_list_field(d, "outputs", _TENSOR_KEYS, _TENSOR_KEYS, Tensor, _TENSOR_TYPES),
            targets=_decode_list_field(d, "targets", _TARGET_KEYS, _TARGET_REQUIRED, Target, _TARGET_TYPES,
                                        nested_types={"requires": _REQUIRES_TYPES}),
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

    @classmethod
    def from_cbor(cls, blob: bytes) -> "Manifest":
        d = _cbor2.loads(blob)
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
