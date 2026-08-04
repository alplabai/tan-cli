# SPDX-License-Identifier: Apache-2.0
"""Non-finite floats never reach the wire as `Infinity` / `-Infinity` / `NaN`
(tan-cli#387).

Those three are Python `json`'s NON-STANDARD extension literals, emitted by
default because `json.dumps(..., allow_nan=True)` is the default. They are not
JSON: `JSON.parse` throws on them, and the alp-sdk-vscode extension's ONLY
channel is the envelope on stdout, so a manifest carrying `.inf` used to hand
the consumer a parse throw at exit code 0 with `issues:[]` -- indistinguishable
from tan crashing, and with no coded signal to fall back on. The persisted
`<build-root>/image-bundle/bundle-manifest.json` is worse: it is an artefact
downstream flashing/OTA tooling parses long after the run that wrote it.

The oracle's answers, MEASURED on `tan 0.4.1` and hardcoded below rather than
guessed, come from two different layers and this suite pins both:

  `.inf` / `-.inf` / `.nan` -> `null`
      serde_yaml resolves all three to non-finite `f64`, and `serde_json`
      serialises a non-finite `f64` as `null` (it has no other legal choice --
      RFC 8259 has no infinity). So the conversion belongs at SERIALISE time,
      which is why it lives in `tan/envelope.py` and applies to every command's
      envelope rather than to `hw_info` alone.

  `1e400` -> the STRING `"1e400"`
      serde_yaml's `parse_f64` accepts a parsed float only `if float.is_finite()`,
      so an overflowing exponent never becomes a float at all and falls through
      to a string. This one belongs at PARSE time, in the YAML 1.2 core-schema
      loader (`tan/core/system_manifest.py`) -- projecting it at serialise time
      would emit `null` where the oracle emits a string.

`json.loads` is NOT a strict parser: it accepts the same three literals by
default, so a Python-side round-trip hides the whole break. Every assertion
here goes through :func:`strict_loads`, which is the failure condition the
issue names.
"""

import json
import math
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tan.core.system_manifest import raw_passthrough
from tan.envelope import Envelope, Issue, Project

PACKAGE_ROOT = Path(__file__).resolve().parents[2]

#: A manifest whose `hw_info` carries every shape the issue names. Kept in one
#: place so the unit, end-to-end and oracle cases all measure the SAME document.
MANIFEST = """schema_version: 1
hw_info:
  sku: E1M-TEST
  a: .inf
  b: -.inf
  c: .nan
  soc_flash_mb: 1e400
slices:
- core_id: m55_hp
  os: zephyr
"""

#: What `tan 0.4.1` puts in `data.hw_info` for :data:`MANIFEST`, verbatim.
ORACLE_HW_INFO = {
    "sku": "E1M-TEST",
    "a": None,
    "b": None,
    "c": None,
    "soc_flash_mb": "1e400",
}


def _reject_constant(literal: str):
    """`json.loads`' hook for `Infinity` / `-Infinity` / `NaN`.

    Raising here is what makes `strict_loads` a stand-in for a real RFC 8259
    parser (`JSON.parse`, `serde_json::from_str`); the default hook silently
    hands back a Python float and the defect goes unobserved.
    """
    raise AssertionError(
        f"non-JSON literal {literal!r} on the wire -- no strict parser accepts it"
    )


def strict_loads(text: str):
    return json.loads(text, parse_constant=_reject_constant)


def run_cli(cwd, *argv):
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(
            [str(PACKAGE_ROOT), *([p] if (p := os.environ.get("PYTHONPATH")) else [])]
        ),
    }
    return subprocess.run(
        [sys.executable, "-m", "tan", *argv],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(cwd),
        env=env,
        timeout=180,
    )


def build_root_with_manifest(base: Path) -> Path:
    build_root = base / "br"
    build_root.mkdir(parents=True, exist_ok=True)
    (build_root / "system-manifest.yaml").write_text(
        MANIFEST, encoding="utf-8", newline=""
    )
    return build_root


# --------------------------------------------------------------------------
# Serialise time: the envelope itself.
# --------------------------------------------------------------------------


def envelope_with(data) -> Envelope:
    return Envelope("image", Project(root=None, board_yaml=None), data, [], 0)


def test_envelope_renders_every_non_finite_float_as_null():
    # The regression the issue asks for: a non-finite float fed straight through
    # `Envelope.to_json()`. Before tan-cli#387 this text read `Infinity` and
    # `strict_loads` raised.
    text = envelope_with(
        {"a": float("inf"), "b": float("-inf"), "c": float("nan")}
    ).to_json()

    assert strict_loads(text)["data"] == {"a": None, "b": None, "c": None}


def test_envelope_reaches_non_finite_floats_nested_in_lists_and_maps():
    # `data` is an arbitrary tree on several commands, so a projection that only
    # looked one level down would leave the break reachable.
    text = envelope_with(
        {"outer": [{"inner": [float("nan")]}, 1.5], "t": (float("inf"),)}
    ).to_json()

    assert strict_loads(text)["data"] == {
        "outer": [{"inner": [None]}, 1.5],
        "t": [None],
    }


def test_envelope_leaves_finite_floats_and_every_other_scalar_untouched():
    # The projection must not become a lossy "sanitise the payload" pass: only
    # the three literals no parser accepts change.
    payload = {"f": 1.5, "neg": -0.25, "i": 7, "b": True, "s": ".inf", "n": None}

    assert strict_loads(envelope_with(payload).to_json())["data"] == payload


def test_envelope_keeps_exit_code_zero_rather_than_the_serialize_failed_fallback():
    # `allow_nan=False` alone would raise into the `envelope.serialize-failed` /
    # exit-5 fallback, where the oracle exits 0 -- trading one divergence for
    # another (tan-cli#387, acceptance criterion 3).
    env = envelope_with({"a": float("inf")})
    decoded = strict_loads(env.to_json())

    assert decoded["exitCode"] == 0
    assert decoded["ok"] is True
    assert decoded["issues"] == []


def test_envelope_does_not_mutate_the_data_it_was_handed():
    # House rule: the projection builds new containers. A caller that inspects
    # its own payload after `emit()` must still see its own floats.
    payload = {"a": float("inf")}
    envelope_with(payload).to_json()

    assert math.isinf(payload["a"])


def test_issue_messages_still_serialise_alongside_a_non_finite_payload():
    env = Envelope(
        "image",
        Project(root=None, board_yaml=None),
        {"a": float("nan")},
        [Issue("image.write-failed", "error", "boom")],
        3,
    )

    decoded = strict_loads(env.to_json())
    assert decoded["issues"] == [
        {"code": "image.write-failed", "severity": "error", "message": "boom"}
    ]
    assert decoded["data"] == {"a": None}


# --------------------------------------------------------------------------
# Parse time: the YAML 1.2 core-schema loader.
# --------------------------------------------------------------------------


def test_overflowing_exponent_stays_a_string_exactly_as_serde_yaml_leaves_it():
    # serde_yaml's `parse_f64` returns `None` unless the parsed float
    # `is_finite()`, so `1e400` is never a float on the oracle side -- it is the
    # raw scalar text. Resolving it to `inf` here (PyYAML's default) diverged
    # twice: wrong type, and then an unserialisable value.
    hw_info, _ = raw_passthrough(MANIFEST)

    assert hw_info["soc_flash_mb"] == "1e400"


def test_yaml_inf_and_nan_still_resolve_to_non_finite_floats():
    # The parse-time rule must NOT swallow the three explicit spellings: those
    # really are floats on the oracle side, and it is `serde_json` -- not
    # `serde_yaml` -- that turns them into `null`.
    hw_info, _ = raw_passthrough(MANIFEST)

    assert hw_info["a"] == float("inf")
    assert hw_info["b"] == float("-inf")
    assert math.isnan(hw_info["c"])


def test_ordinary_floats_still_resolve_to_floats():
    hw_info, _ = raw_passthrough(
        "hw_info:\n  a: 1.5\n  b: -0.25\n  c: 1e3\n  d: 1e-400\n"
    )

    assert hw_info["a"] == 1.5
    assert hw_info["b"] == -0.25
    assert hw_info["c"] == 1000.0
    # Underflow is FINITE (0.0), so serde_yaml keeps it a float -- only overflow
    # falls through to a string.
    assert hw_info["d"] == 0.0


# --------------------------------------------------------------------------
# End to end: stdout AND the persisted artefact.
# --------------------------------------------------------------------------


def test_image_envelope_and_persisted_bundle_manifest_are_both_strict_json(tmp_path):
    build_root = build_root_with_manifest(tmp_path)

    result = run_cli(tmp_path, "image", "--format", "json", "--build-root", "br")

    assert result.returncode == 0, result.stdout + result.stderr
    envelope = strict_loads(result.stdout)
    assert envelope["data"]["hw_info"] == ORACLE_HW_INFO
    assert envelope["issues"] == []

    persisted = (build_root / "image-bundle" / "bundle-manifest.json").read_text(
        encoding="utf-8"
    )
    assert strict_loads(persisted)["hw_info"] == ORACLE_HW_INFO


# --------------------------------------------------------------------------
# The oracle, live.
# --------------------------------------------------------------------------


def oracle_binary() -> str | None:
    """The shipped Rust `tan`, resolved by the ONE shared resolver.

    Deliberately not re-derived here: tan-cli#393 is exactly what a hand-copied
    fourth copy of this rule costs (two copies picked a stale
    `target/release/tan` and measured against the wrong binary silently).
    """
    from tests.parity.oracle import rust_binary

    return rust_binary()


def test_image_matches_the_oracle_byte_for_byte_on_non_finite_hw_info(tmp_path):
    oracle = oracle_binary()
    if oracle is None:
        pytest.skip("no Rust oracle built (target/{release,debug}/tan absent)")

    port_root = tmp_path / "port"
    rust_root = tmp_path / "rust"
    port_root.mkdir()
    rust_root.mkdir()
    build_root_with_manifest(port_root)
    build_root_with_manifest(rust_root)

    port = run_cli(port_root, "image", "--format", "json", "--build-root", "br")
    rust = subprocess.run(
        [oracle, "image", "--format", "json", "--build-root", "br"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(rust_root),
        timeout=180,
    )

    assert port.returncode == rust.returncode == 0
    # `project.root` is the only key that legitimately differs (two temp dirs),
    # so the comparison is over `data` -- which is where the divergence lived.
    assert strict_loads(port.stdout)["data"] == strict_loads(rust.stdout)["data"]

    port_manifest = port_root / "br" / "image-bundle" / "bundle-manifest.json"
    rust_manifest = rust_root / "br" / "image-bundle" / "bundle-manifest.json"
    # Bytes, not parsed objects: the persisted artefact is what downstream
    # flashing/OTA tooling opens, so indentation and separators are part of the
    # contract too -- a compare over decoded dicts would pass on a file that
    # still said `Infinity` if both sides said it.
    assert port_manifest.read_bytes() == rust_manifest.read_bytes()
