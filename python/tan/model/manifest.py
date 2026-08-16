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


def _pick(d: dict, keys: set) -> dict:
    return {k: d[k] for k in keys if k in d}   # drop unknown keys + tolerate missing-known


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
    `_pick`-based `from_cbor` below already does, and so does
    `Target.caveats`'s own default)."""
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
        v = d.get("v", MANIFEST_SCHEMA_VERSION)
        if v != MANIFEST_SCHEMA_VERSION:
            raise ValueError(f"unsupported manifest version {v!r}; expected {MANIFEST_SCHEMA_VERSION}")
        return cls(
            name=d["name"],
            src_sha=d["src_sha"],  # raw bytes pass-through; JSON/text callers must decode to bytes first
            inputs=[Tensor(**t) for t in d.get("inputs", [])],
            outputs=[Tensor(**t) for t in d.get("outputs", [])],
            targets=[Target(**t) for t in d.get("targets", [])],
            coverage=[Coverage(**c) for c in d.get("coverage", [])],
        )

    def to_json(self) -> str:
        return _json.dumps(_json_default(self.to_dict()), indent=2)

    @classmethod
    def from_json(cls, text: str) -> "Manifest":
        d = _json.loads(text)
        d["src_sha"] = bytes.fromhex(d["src_sha"])
        return cls.from_dict(d)

    def to_cbor(self) -> bytes:
        return _cbor2.dumps(self.to_dict())

    @classmethod
    def from_cbor(cls, blob: bytes) -> "Manifest":
        d = _cbor2.loads(blob)
        return cls(
            name=d["name"],
            src_sha=bytes(d["src_sha"]),
            inputs=[Tensor(**_pick(t, _TENSOR_KEYS)) for t in d.get("inputs", [])],
            outputs=[Tensor(**_pick(t, _TENSOR_KEYS)) for t in d.get("outputs", [])],
            targets=[Target(**_pick(t, _TARGET_KEYS)) for t in d.get("targets", [])],
            coverage=[Coverage(**_pick(c, _COV_KEYS)) for c in d.get("coverage", [])],
        )
