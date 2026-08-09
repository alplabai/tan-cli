# SPDX-License-Identifier: Apache-2.0
"""`tan.core.system_manifest`: the tolerant reader, the version guard, the
additive-field passthrough, and the I-18 artefact resolution `tan image` and
`tan size` share.

Every message shape here was diffed against the Rust binary
(`crates/tan-core/src/system_manifest.rs` + the two commands' `manifest-invalid`
issues) -- see `tests/parity/test_image_size_oracle.py` for the live comparison.
"""
import os

import pytest

from tan.core.system_manifest import (
    SYSTEM_MANIFEST_SCHEMA_VERSION,
    SliceRunResult,
    SystemManifestError,
    anchor_under,
    overlay_run_results_raw,
    parse_system_manifest,
    parse_system_manifest_raw,
    raw_passthrough,
    serialize_system_manifest_raw,
    slice_build_dir,
    slice_build_dir_or_default,
    slice_elf_candidates,
    slice_footprint_dirs,
)

# The real `--emit system-manifest` output for the heterogeneous AEN701 example:
# an `off` A-core slice with no flash wiring, two Zephyr slices with it, and a
# helper whose `flash_args` is the bare string "TBD" plus an undeclared `note` --
# every tolerant-reader corner case at once.
AEN701 = """
schema_version: 1
generated_by: scripts/alp_orchestrate.py
hw_info:
  sku: E1M-AEN701
  som_hw_rev: r1
  silicon: alif:ensemble:e7
slices:
- core_id: a32_cluster
  os: 'off'
  app: alp-image-edge
  status: pending
- core_id: m55_hp
  os: zephyr
  app: ./src
  status: pending
  flash_method: zephyr_west_flash
ipc: []
helper_mcus:
- name: cc3501e_otp
  chip: cc3501e
  firmware_path: TBD
  note: populated when the upstream firmware release lands
boot_order: []
"""


def test_parses_the_real_aen701_manifest():
    manifest = parse_system_manifest(AEN701)
    assert manifest.schema_version == SYSTEM_MANIFEST_SCHEMA_VERSION
    assert manifest.sku == "E1M-AEN701"
    assert [s["core_id"] for s in manifest.slices] == ["a32_cluster", "m55_hp"]
    # The `off` slice omits flash_method -- tolerated, not rejected.
    assert "flash_method" not in manifest.slices[0]
    assert manifest.helper_mcus[0]["chip"] == "cc3501e"


def test_unknown_additive_fields_are_ignored_not_rejected():
    # The v1 stability policy (alp-sdk#106): a newer SDK's additive block must not
    # break an older tan. The helper's undeclared `note` above is the same rule.
    manifest = parse_system_manifest(AEN701 + "future_block:\n  anything: 1\n")
    assert len(manifest.slices) == 2


def test_unsupported_schema_version_is_refused_with_the_oracle_message():
    with pytest.raises(SystemManifestError) as err:
        parse_system_manifest("schema_version: 2\nslices: []\n")
    assert err.value.message == (
        "unsupported system-manifest schema_version 2 (this CLI consumes v1); "
        "upgrade the CLI or the SDK so the versions match"
    )


def test_schema_version_beyond_u32_is_refused_not_truncated():
    # 2**32 + 1 is congruent to 1 mod 2**32, so a truncating guard accepts it.
    with pytest.raises(SystemManifestError):
        parse_system_manifest("schema_version: 4294967297\nslices: []\n")


def test_missing_schema_version_is_a_parse_error():
    with pytest.raises(SystemManifestError) as err:
        parse_system_manifest("slices: []\n")
    assert err.value.message == (
        "system-manifest is not valid YAML: missing field `schema_version`"
    )


def test_scalar_root_document_names_the_struct():
    with pytest.raises(SystemManifestError) as err:
        parse_system_manifest("just-a-string\n")
    assert err.value.message == (
        'system-manifest is not valid YAML: invalid type: string "just-a-string", '
        "expected struct SystemManifest"
    )


def test_slice_missing_core_id_fails_the_whole_document():
    # serde does too: a partial read would make tan act on a manifest the shipped
    # binary refuses outright.
    with pytest.raises(SystemManifestError) as err:
        parse_system_manifest("schema_version: 1\nslices:\n- os: zephyr\n")
    assert "slices[0]: missing field `core_id`" in err.value.message


@pytest.mark.parametrize(
    "document",
    [
        "schema_version: 1\nslices: [\n",  # syntax error
        "schema_version: 1\nslices: notalist\n",
        "schema_version: 1\nslices:\n- notamapping\n",
        "schema_version: 1\nhelper_mcus:\n- name: x\n",  # missing `chip`
        "schema_version: 1\nipc:\n- name: x\n",  # missing `kind`
        "schema_version: true\nslices: []\n",
        "schema_version: '1'\nslices: []\n",
        "",  # empty document -> None root
        "[]\n",
    ],
)
def test_every_malformed_shape_raises_the_typed_error_never_something_else(document):
    with pytest.raises(SystemManifestError):
        parse_system_manifest(document)


# The two directions of serde_yaml's leniency around string fields and sequences.
# Every one of these was determined by running the compiled binary, not inferred:
# guessing gets both directions wrong, and each wrong guess is a document one
# implementation accepts and the other refuses.


@pytest.mark.parametrize(
    "document",
    [
        # `Vec<_>` fields: present-but-not-a-sequence, `~` INCLUDED.
        "schema_version: 1\nslices: ~\n",
        "schema_version: 1\nipc: nope\n",
        "schema_version: 1\nhelper_mcus: nope\n",
        "schema_version: 1\nboot_order: notalist\n",
        "schema_version: 1\nboot_order: {a: 1}\n",
        "schema_version: 1\nstorage: nope\n",
        "schema_version: 1\nipc:\n- name: n\n  kind: k\n  endpoints: nope\n",
        # `hw_info` is a struct: only a mapping (or absence) will do, and `~` is
        # NOT accepted here even though it is for a string field.
        "schema_version: 1\nhw_info: notamapping\n",
        "schema_version: 1\nhw_info: [1]\n",
        "schema_version: 1\nhw_info: ~\n",
        # A string field given a sequence or a mapping.
        "schema_version: 1\nhw_info:\n  sku: [1]\n",
        "schema_version: 1\nslices:\n- core_id: {a: 1}\n  os: zephyr\n",
        "schema_version: 1\nslices:\n- core_id: c\n  os: zephyr\n  build_dir: [1]\n",
    ],
)
def test_a_sequence_or_struct_field_given_the_wrong_shape_is_refused(document):
    with pytest.raises(SystemManifestError):
        parse_system_manifest(document)


def test_a_string_field_keeps_the_scalars_raw_text():
    """serde resolves a scalar against its TARGET type, so a `String` field gets
    the scalar's RAW TEXT -- not YAML's resolved value stringified.

    `str(yaml.safe_load("0x10"))` is `"16"`, `str(None)` is `"None"`,
    `str(1.50)` is `"1.5"`: three different values than the oracle for the same
    document, and `build_dir`/`output_artefact`/`firmware_path` are PATHS.
    """
    manifest = parse_system_manifest(
        "schema_version: 1\ngenerated_by: 7\nhw_info:\n  sku: 0x10\n  silicon: 1.50\n"
        "slices:\n- core_id: 007\n  os: zephyr\n  build_dir: 0o17\n  status: yes\n"
        "helper_mcus:\n- name: 1:30\n  chip: .inf\n"
    )
    assert manifest.sku == "0x10"
    assert manifest.root["hw_info"]["silicon"] == "1.50"
    assert manifest.root["generated_by"] == "7"
    assert manifest.slices[0]["core_id"] == "007"
    assert manifest.slices[0]["build_dir"] == "0o17"
    assert manifest.slices[0]["status"] == "yes"
    assert manifest.helper_mcus[0]["name"] == "1:30"
    assert manifest.helper_mcus[0]["chip"] == ".inf"


def test_a_null_is_the_raw_text_for_a_required_field_and_absent_for_an_optional_one():
    """The split that makes `_SLICE_REQUIRED_STRINGS` vs `_SLICE_OPTIONAL_STRINGS`
    load-bearing, both halves verified against the binary:

    `os` is a plain `String`, so `os: ~` is the STRING `"~"`. `build_dir` is an
    `Option<String>`, so `build_dir: ~` is ABSENT -- `deserialize_option` sees the
    null first. Backwards, this puts a slice's build dir at the literal path `~`.
    """
    manifest = parse_system_manifest(
        "schema_version: 1\nhw_info:\n  sku: ~\n  silicon: null\nslices:\n"
        "- core_id: c\n  os: ~\n  build_dir: ~\n  output_artefact: null\n"
    )
    assert manifest.sku == "~"
    assert manifest.slices[0]["os"] == "~"
    assert "build_dir" not in manifest.slices[0]
    assert "output_artefact" not in manifest.slices[0]
    assert "silicon" not in manifest.root["hw_info"]


def test_the_yaml_12_core_schema_governs_the_passthrough_not_pyyamls_yaml_11():
    """`raw_passthrough` feeds `data.hw_info` verbatim, so it must resolve scalars
    the way serde_yaml's `Value` does -- YAML 1.2 CORE, not PyYAML's YAML 1.1.

    All five rows were diffed against the binary. The `date` row is not cosmetic:
    PyYAML resolves it to a `datetime.date`, which is not JSON-serializable, so
    `tan image` exited 3 on a document the oracle bundles at 0.
    """
    hw_info, _ = raw_passthrough(
        "schema_version: 1\nhw_info:\n"
        "  a: yes\n  b: 007\n  c: 1:30\n  d: 2024-01-01\n  e: 0o17\n  f: 0xA5\n"
        "  g: 1.50\n  h: ~\n  i: 'off'\n"
    )
    assert hw_info == {
        "a": "yes",           # 1.1 boolean -> 1.2 string
        "b": "007",           # 1.1 int 7   -> 1.2 string
        "c": "1:30",          # 1.1 int 90  -> 1.2 string
        "d": "2024-01-01",    # 1.1 date    -> 1.2 string
        "e": 15,              # 1.1 string  -> 1.2 int
        "f": 165,             # int in both
        "g": 1.5,
        "h": None,
        "i": "off",
    }


def test_the_int_resolver_is_serde_yamls_parse_signed_int_not_the_spec_regex():
    """tan-cli#499 defect 10. The `_CORE_RESOLVERS` int row transcribed the YAML
    1.2 SPEC core schema, which puts the sign OUTSIDE the radix alternation and
    has no `0b`. serde_yaml's `parse_signed_int` strips a leading `+`/`-` BEFORE
    testing the prefixes and does accept `0b`, so exactly two shapes -- a binary
    literal, and a sign-prefixed `0x`/`0o`/`0b` -- stayed STRINGS here while
    `tan 0.4.1` produced integers.

    Every expectation below was measured on the oracle
    (`tan image --format json --build-root br`, `data.hw_info`), not derived
    from the spec text. It reaches two surfaces: `data.hw_info` /
    `data.boot_order` on stdout AND the persisted `bundle-manifest.json` for
    `tan image`, and `parse_system_manifest_raw` -> `serialize_system_manifest_raw`
    for `tan build`'s post-build manifest rewrite.
    """
    hw_info, boot_order = raw_passthrough(
        "schema_version: 1\nhw_info:\n"
        "  mask: 0b1010\n  off: -0x1E\n  pos: +0x1E\n  noct: -0o17\n"
        "  posoct: +0o17\n  negbin: -0b11\n  posbin: +0b11\n  hexzero: 0x0\n"
        "  eeprom:\n    straps: 0b0101\n"
        "boot_order: [0b11, 0x10]\n"
    )
    assert hw_info == {
        "mask": 10,
        "off": -30,
        "pos": 30,
        "noct": -15,
        "posoct": 15,
        "negbin": -3,
        "posbin": 3,
        "hexzero": 0,
        "eeprom": {"straps": 5},
    }
    # Two different TYPES inside one list was the worst of it.
    assert boot_order == [3, 16]


@pytest.mark.parametrize(
    "scalar",
    # Measured on the oracle: every one of these stays a STRING. This is why the
    # regex has to remain the gate -- PyYAML's `construct_yaml_int` strips `_`
    # and would resolve the underscore forms if it were asked.
    [
        "0B11", "0XA5", "0O17", "-0B11",   # uppercase prefix
        "0x1_F", "0b1_0", "0o1_7", "1_000", "0_1",  # underscores
        "0x", "0b", "0o", "-0x",           # bare prefix, no digits
        "0o8", "0xG", "0b2",               # digit outside the radix
        "007", "+007", "++5",              # leading zero / double sign
    ],
)
def test_the_widened_int_resolver_still_leaves_these_shapes_as_strings(scalar):
    hw_info, _ = raw_passthrough(f"schema_version: 1\nhw_info:\n  v: {scalar}\n")
    assert hw_info == {"v": scalar}


def test_an_integer_serde_yaml_cannot_hold_drops_the_whole_passthrough():
    # `serde_yaml::Value` spans i64::MIN..=u64::MAX; outside it the Value parse
    # fails and `raw_passthrough` yields its defaults, silently dropping BOTH
    # fields. Reproduced rather than "improved": Python's unbounded int would put a
    # number in bundle-manifest.json that no `JSON.parse` consumer can hold.
    assert raw_passthrough(
        "schema_version: 1\nhw_info:\n  v: 123456789012345678901234567890\n"
        "boot_order: [a]\n"
    ) == ({}, [])
    # ...and one INSIDE the range passes through untouched.
    assert raw_passthrough("schema_version: 1\nhw_info:\n  v: 18446744073709551615\n") == (
        {"v": 2**64 - 1},
        [],
    )


def test_unhashable_yaml_key_is_a_parse_error_not_a_traceback():
    # A YAML sequence used as a mapping key. The oracle tolerates it on read and
    # fails later at serialize; both ends emit a coded envelope, and the one thing
    # neither may do is let the exception escape.
    with pytest.raises(SystemManifestError):
        parse_system_manifest("schema_version: 1\n? [1, 2]\n: value\n")


def test_raw_passthrough_keeps_additive_fields():
    hw_info, boot_order = raw_passthrough(
        "schema_version: 1\nhw_info:\n  sku: E1M-AEN701\n  eeprom:\n    magic: 0xA5\n"
        "  future_key: keep-me\nboot_order:\n- m55_hp\n- m55_he\n"
    )
    # `0xA5` is an int in serde_yaml's Value too -- verified against the binary.
    assert hw_info["eeprom"] == {"magic": 165}
    assert hw_info["future_key"] == "keep-me"
    assert boot_order == ["m55_hp", "m55_he"]


@pytest.mark.parametrize(
    "document", ["", "schema_version: 1\nslices: []\n", "just-a-string\n", "{[\n"]
)
def test_raw_passthrough_never_raises_and_defaults_to_empty(document):
    # It is called on the error path with an empty string, so a throw here would
    # double-fault the envelope guard.
    assert raw_passthrough(document) == ({}, [])


# --------------------------------------------------------------------- paths


def test_anchor_under_leaves_an_absolute_path_alone():
    absolute = os.path.join(os.getcwd(), "x", "y.elf")
    assert anchor_under(absolute, os.path.join("some", "root")) == absolute


def test_anchor_under_joins_a_relative_path_onto_the_build_root():
    assert anchor_under("a/b.elf", os.path.join("R", "br")) == os.path.join(
        "R", "br", "a/b.elf"
    )


def test_join_does_not_re_render_a_posix_root_in_platform_separators():
    # `Path("C:/a/b") / "c"` rewrites the whole path; Rust's `PathBuf::join` only
    # appends. Both values reach `data.slices[].notes[]`, so the difference is
    # part of the machine contract.
    assert slice_build_dir_or_default(
        {"core_id": "m55_hp", "os": "zephyr"}, "R/posix/root"
    ) == os.path.join("R/posix/root", "m55_hp-zephyr")


def test_slice_build_dir_is_none_without_a_usable_key():
    for slice_ in ({}, {"build_dir": ""}, {"build_dir": None}, {"build_dir": 7}):
        assert slice_build_dir(slice_, "R") is None


def test_slice_build_dir_or_default_uses_the_canonical_name():
    assert slice_build_dir_or_default(
        {"core_id": "m55_he", "os": "zephyr"}, "R"
    ) == os.path.join("R", "m55_he-zephyr")


def test_recorded_output_artefact_is_used_alone_no_nesting_probe():
    # `tan build` only ever writes it already-resolved, so second-guessing it
    # would ignore a deliberate `-d`/`--build-dir` override.
    assert slice_elf_candidates(
        {"core_id": "m55_hp", "os": "zephyr", "output_artefact": "art/z.elf"}, "R"
    ) == [os.path.join("R", "art/z.elf")]


def test_i18_nesting_is_probed_after_the_plain_path_never_before():
    # I-18: `west build` is emitted with no `-d`, so its tree lands in
    # `<build_dir>/build/`. The un-nested path MUST come first -- that ordering is
    # what makes every input the oracle measures measure identically here.
    candidates = slice_elf_candidates({"core_id": "c", "os": "zephyr"}, "R")
    assert candidates == [
        os.path.join("R", "c-zephyr", "zephyr", "zephyr.elf"),
        os.path.join("R", "c-zephyr", "build", "zephyr", "zephyr.elf"),
    ]
    assert slice_footprint_dirs({"core_id": "c", "os": "zephyr"}, "R") == [
        os.path.join("R", "c-zephyr"),
        os.path.join("R", "c-zephyr", "build"),
    ]


# --------------------------------------------------------------- write path


# AEN701 with an rpmsg carve-out (fields `IpcLink` doesn't model) and an
# `hw_info.eeprom` block (fields `HwInfo` doesn't model) -- the two additive
# shapes a lossy typed re-serializer would silently drop, mirrored from
# `crates/tan-core/src/system_manifest.rs`'s own `manifest_with_additive_
# fields` test fixture.
_AEN701_WITH_ADDITIVE_FIELDS = AEN701.replace(
    "ipc: []\n",
    "ipc:\n- name: rpmsg0\n  kind: rpmsg\n  endpoints: [a55, m33]\n"
    "  shm_addr: 0x48000000\n  shm_size: 0x100000\n",
).replace("hw_info:\n", "hw_info:\n  eeprom:\n    magic: 0xA5\n")


def test_parse_system_manifest_raw_rejects_unsupported_schema_version():
    with pytest.raises(SystemManifestError):
        parse_system_manifest_raw(AEN701.replace("schema_version: 1", "schema_version: 2"))


def test_parse_system_manifest_raw_rejects_missing_schema_version():
    with pytest.raises(SystemManifestError):
        parse_system_manifest_raw("slices: []\n")


def test_overlay_sets_status_and_leaves_unmatched_slices_untouched():
    raw = parse_system_manifest_raw(AEN701)
    overlay_run_results_raw(
        raw,
        [
            SliceRunResult("m55_hp", "ok", "build/m55_hp-zephyr/build/zephyr/zephyr.elf",
                            "build/m55_hp-zephyr/build", None),
            # A result for a core not in the manifest is a no-op.
            SliceRunResult("ghost", "ok"),
        ],
    )
    hp = next(s for s in raw["slices"] if s["core_id"] == "m55_hp")
    assert hp["status"] == "ok"
    assert hp["output_artefact"] == "build/m55_hp-zephyr/build/zephyr/zephyr.elf"
    assert hp["build_dir"] == "build/m55_hp-zephyr/build"

    # The unmatched `off` slice is untouched.
    a32 = next(s for s in raw["slices"] if s["core_id"] == "a32_cluster")
    assert a32["status"] == "pending"


def test_overlay_duplicate_core_id_results_the_first_wins():
    """`by_core = {r.core_id: r for r in results}` would let the LAST
    duplicate win; the oracle's `results.iter().find(|(cid, ..)| cid ==
    core_id)` (system_manifest.rs:332-333) takes the FIRST. Unreachable with
    today's real callers (one result per slice) but a silent semantic flip
    in a ported pure function otherwise."""
    raw = parse_system_manifest_raw(AEN701)
    overlay_run_results_raw(
        raw,
        [
            SliceRunResult("m55_hp", "ok", "build/first.elf"),
            SliceRunResult("m55_hp", "failed", "build/second.elf"),
        ],
    )
    hp = next(s for s in raw["slices"] if s["core_id"] == "m55_hp")
    assert hp["status"] == "ok"
    assert hp["output_artefact"] == "build/first.elf"


def test_overlay_none_fields_preserve_the_plan_time_value():
    raw = parse_system_manifest_raw(AEN701)
    raw["slices"][1]["output_artefact"] = "build/plan-time.elf"
    overlay_run_results_raw(raw, [SliceRunResult("m55_hp", "ok")])
    assert raw["slices"][1]["output_artefact"] == "build/plan-time.elf"
    assert raw["slices"][1]["status"] == "ok"


def test_overlay_threads_the_reason_through():
    raw = parse_system_manifest_raw(AEN701)
    overlay_run_results_raw(
        raw, [SliceRunResult("m55_hp", "skipped", reason="no command")]
    )
    hp = next(s for s in raw["slices"] if s["core_id"] == "m55_hp")
    assert hp["reason"] == "no command"


def test_overlay_and_serialize_round_trips_through_reparse():
    raw = parse_system_manifest_raw(AEN701)
    overlay_run_results_raw(raw, [SliceRunResult("m55_hp", "ok")])
    out = serialize_system_manifest_raw(raw)
    reparsed = parse_system_manifest(out)
    hp = next(s for s in reparsed.slices if s["core_id"] == "m55_hp")
    assert hp["status"] == "ok"


def test_serialize_raw_preserves_additive_fields_ipc_carveout_and_eeprom():
    # The defect the raw seam exists to avoid: a lossy typed re-serializer
    # would silently drop `shm_addr`/`shm_size`/`eeprom` here.
    raw = parse_system_manifest_raw(_AEN701_WITH_ADDITIVE_FIELDS)
    overlay_run_results_raw(
        raw, [SliceRunResult("m55_hp", "ok", "build/m55_hp-zephyr/build/zephyr/zephyr.elf")]
    )
    out = serialize_system_manifest_raw(raw)

    assert "shm_addr" in out
    assert "shm_size" in out
    assert "eeprom" in out

    reparsed = parse_system_manifest(out)
    hp = next(s for s in reparsed.slices if s["core_id"] == "m55_hp")
    assert hp["status"] == "ok"
    assert hp["output_artefact"] == "build/m55_hp-zephyr/build/zephyr/zephyr.elf"
    a32 = next(s for s in reparsed.slices if s["core_id"] == "a32_cluster")
    assert a32["status"] == "pending"
