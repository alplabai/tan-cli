#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Scaffold byte-parity gate (alp-sdk#864): vendored wizard templates vs. a
live `alp-sdk --emit scaffold`.

`tan init`/`tan scaffold` are SDK-free — for a template mapped onto an SDK
scaffold-catalog id (see `python/tan/templates/vendored/MANIFEST.md`), they
read a vendored copy of `alp_project.py --emit scaffold --template <id> --sku
<sku>`'s output baked into the shipped artifact, instead of shelling the SDK
or re-deriving its build-integration conventions locally. That vendored copy
can silently drift from the SDK if a future SDK scaffold change is never
re-vendored -- exactly the RFC #843-style drift ADR-0020 exists to kill for
the build-plan seam. This script is the tan-cli side of the
`repository_dispatch` gate ADR-0020 Amendment 1 mandates (see
`tests/parity/README.md` for the seam-1 build-plan analogue this mirrors):
for every vendored (template, sku) pair, re-run the live SDK emit and assert
byte-identity against the vendored tree.

Defaults to `python/tan/templates/vendored/` -- the tree the shipped Python
`tan` actually reads, and since tan-cli#269 the ONLY vendored scaffold tree in
the repo. The Rust oracle carried a second copy, frozen at its own permanent
vendor point and guarded by its own SDK-free `cargo test`; both went with
`crates/`. `--vendored <path>` still exists for pointing this script at an
arbitrary tree by hand, but there is no longer a second in-repo tree to point
it at.

Optionally self-skipping: the vendored tree's own test suite already proves
it is internally consistent without an SDK checkout, so a local dev-loop run
of this script with no reachable alp-sdk checkout is a clean no-op, not a
failure. Reachability is checked in this order: `--sdk`, then
`$ALP_SDK_ROOT`, then an `alp-sdk` checkout next to this tan-cli checkout --
but an explicit `--sdk` that does not resolve is a hard FAIL, not a
fall-through to the other two (tan-cli#172 review, tan-cli#175; see
`_sdk_checkout.sdk_root_or_exit_code`).

**Run it against the ref the tree is vendored from.** This gate diffs against
a LIVE emit, so an `$ALP_SDK_ROOT` pointing at some other checkout measures
that checkout, not this tree: a local alp-sdk on a feature branch predating
alp-sdk#1016 reports six spurious `board.yaml` diffs (the "Customer workflow"
comment header), which look exactly like real drift and are not. CI clones
`.github/workflows/parity.yml`'s `PINNED_SDK_TAG` for precisely this reason.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable, Iterable
from pathlib import Path

from _sdk_checkout import sdk_root_or_exit_code

VENDORED_ROOT = Path(__file__).resolve().parent.parent.parent / (
    "python/tan/templates/vendored"
)

# Vendored files that `--emit scaffold` never emits at all -- NOT part of the
# envelope and NOT in the catalog's `files.user_owned`. `testcase.yaml` is the
# SDK's own twister harness for the catalog's canonical `example:`; it is
# vendored alongside the scaffold envelope but compared against that example
# directory instead of the (absent-from-emit) live output.
#
# `native_sim.conf` (tan-cli#379) is the same class and, for `iot`, the same
# PAIR: `testcase.yaml`'s `extra_args: EXTRA_CONF_FILE=native_sim.conf` is what
# loads it, so vendoring one without the other shipped a scaffold whose own
# twister scenario and whose own documented `west build ... -DEXTRA_CONF_FILE=
# native_sim.conf` named a file that did not exist. Listing it here is not a
# loosening: every vendored `native_sim.conf` now gets byte-diffed against the
# catalog example's copy, exactly as `testcase.yaml` already was. Templates with
# no such file are unaffected -- `augment_with_example_extras` only reaches for
# a name the vendored tree actually carries.
#
# `boards/native_sim_native_64.{overlay,conf}` (tan-cli#501) is the same class
# again, for `sensor` and `diagnostics`: Zephyr auto-discovers a board overlay
# by this path convention with no CMakeLists wiring at all, so without it the
# documented `west build -b native_sim/native/64` run has no `alp-i2c0` DT
# alias and no `CONFIG_EMUL`/`CONFIG_I2C_EMUL`, and produces output that does
# not match either README's own "Expected output" block. `edge-ai` ships the
# same missing pair but is UNAFFECTED (measured identical native_sim output
# with and without it) and is deliberately not vendored here.
#
# `peer/testcase.yaml` (tan-cli#864) is the first NESTED entry, and it is a
# literal path rather than a suffix match on purpose. `multicore-mailbox` is
# the first vendored template with a second build slice, so it is the first
# with a per-slice twister harness; vendoring the root `testcase.yaml` and not
# the peer's would ship a scaffold whose main slice has a twister scenario and
# whose peer slice does not, which is an asymmetry a reader would take for a
# mistake. Matching `testcase.yaml` by SUFFIX would cover this and any future
# `<slice>/testcase.yaml` in one line, and was rejected: it silently widens
# what counts as a non-envelope extra for every template in the tree, and this
# tuple's whole job is to be an exact, reviewed list. A per-template mapping
# is the right shape once a THIRD nested extra appears; one entry does not pay
# for it.
NON_ENVELOPE_EXTRAS = (
    "testcase.yaml",
    "native_sim.conf",
    "boards/native_sim_native_64.overlay",
    "boards/native_sim_native_64.conf",
    "peer/testcase.yaml",
)


#: `github.com/alplabai/alp-sdk/(blob|tree)/<ref>/` -- what the SDK's
#: doc-link renderer turns a cross-directory link into, pinned at the emitting
#: checkout's own VERSION. Both halves of the ref are spelled out below rather
#: than read from `MANIFEST.md`: this is a declaration of the exact bytes that
#: were hand-changed, so a re-vendor MUST come here and restate it.
_SDK_DOC_LINK_REF = re.compile(r"(github\.com/alplabai/alp-sdk/(?:blob|tree))/v0\.15\.0-rc1/")

#: `list(PREPEND EXTRA_CONF_FILE ${_alp_generated})` plus the comment block
#: immediately above it. The comment is matched by SHAPE, not by its wording --
#: rewording the rationale is not drift from the SDK, and pinning the prose
#: here would red the gate for an edit that changes no behaviour.
_IOT_EXTRA_CONF_PREPEND = re.compile(
    r"(?:^#.*\n)*^list\(PREPEND (EXTRA_CONF_FILE \$\{_alp_generated\})\)$", re.M
)


def un_edit_doc_link_ref(text: str) -> str:
    """tan-cli#384, historical (healed at tan-cli#543/#545 -- see the
    `DELIBERATE_EDITS` comment above): the emit used to render `blob/v0.15.0/`
    (it dropped a pre-release suffix), a tag alp-sdk had not yet cut, so all
    40 links 404'd in a scaffolded project. The vendored tree of that era
    pinned `v0.15.0-rc1`, the ref it was actually captured from, and this
    function recovered the emit's own bytes by undoing that pin. Not called
    from `DELIBERATE_EDITS` any more -- the pin has since moved on (the
    vendored tree pins v0.16.0 at the time of writing; see MANIFEST.md's
    "Current vendor point"). Kept only as the record of the transform and
    exercised by `self_check()`'s literal `v0.15.0-rc1`/`v0.15.0` fixture,
    which tests the transform's own mechanics, not the current vendor pin."""
    return _SDK_DOC_LINK_REF.sub(r"\1/v0.15.0/", text)


def un_edit_iot_extra_conf_order(text: str) -> str:
    """tan-cli#379: the emit APPENDS the generated `alp.conf` to
    `EXTRA_CONF_FILE`, and Zephyr's last-assignment-wins merge then lets it
    override a caller's own `-DEXTRA_CONF_FILE=native_sim.conf` -- the exact
    overlay this template documents. The vendored tree prepends. Undo that
    (comment block included) to recover the emit's own bytes."""
    return _IOT_EXTRA_CONF_PREPEND.sub(r"list(APPEND \1)", text)


#: tan-cli#1001 review (major), first half: alp-sdk's own `examples/
#: connectivity/mqtt-telemetry/README.md` links `native_sim.conf` via a
#: self-referential `../mqtt-telemetry/native_sim.conf` detour (its own
#: README, one level down, pointing back at its own directory -- verified in
#: the alp-sdk source tree at the current vendor point). That IS
#: `../`-prefixed, so tan's doc-link rewriter (the same one entry 4's
#: `- Ref:`-pinned links go through) treats it as a cross-directory reference
#: and renders it as a `github.com/.../blob/<ref>/...` link -- even though
#: `native_sim.conf` ships as a sibling of `README.md` in THIS scaffolded
#: tree (`tan init --template iot-starter` writes it, tan-cli#379,
#: `NON_ENVELOPE_EXTRAS` above). Kept as its own entry, separate from the
#: build-comment fix below, per this module's own two-independent-
#: substitutions-get-two-entries discipline (see the `model_comment_1`/`_2`
#: split above): healing one without the other must still fail on its own.
_IOT_AEN801_README_NATIVE_SIM_CONF_LINK_EDITED = (
    "turns mbedtls off (see [`native_sim.conf`](native_sim.conf)) so the\n"
)
_IOT_AEN801_README_NATIVE_SIM_CONF_LINK_EMITTED = (
    "turns mbedtls off (see\n"
    "[`native_sim.conf`](https://github.com/alplabai/alp-sdk/blob/v0.16.0/"
    "examples/connectivity/mqtt-telemetry/native_sim.conf)) so the\n"
)


def un_edit_iot_aen801_readme_native_sim_conf_link(text: str) -> str:
    """tan-cli#1001 review: reverse the sibling-link correction above to
    recover the emit's own (dead-outside-an-alp-sdk-checkout) GitHub blob
    link."""
    return text.replace(
        _IOT_AEN801_README_NATIVE_SIM_CONF_LINK_EDITED,
        _IOT_AEN801_README_NATIVE_SIM_CONF_LINK_EMITTED,
    )


#: tan-cli#1001 review (major), second half: the re-vendor also imported a
#: "copy it in first" build comment that is true upstream (alp-sdk's own
#: checkout, where `native_sim.conf` really does live only in the SDK tree)
#: and false here: `tan init --template iot-starter` vendors and writes
#: `native_sim.conf` as a sibling of this very `README.md`
#: (`NON_ENVELOPE_EXTRAS` above) -- there is nothing to copy in. Own entry,
#: same discipline as the link fix above.
_IOT_AEN801_README_NATIVE_SIM_COPY_COMMENT_EDITED = (
    "# Standalone, native_sim (no radio; framing-only, mbedtls off):\n"
    "west build -b native_sim/native/64 . \\\n"
)
_IOT_AEN801_README_NATIVE_SIM_COPY_COMMENT_EMITTED = (
    "# Standalone, native_sim (no radio; framing-only, mbedtls off):\n"
    "# native_sim.conf ships only in the alp-sdk tree, not in this\n"
    "# scaffold -- copy it in first (see the link above) before\n"
    "# running this leg.\n"
    "west build -b native_sim/native/64 . \\\n"
)


def un_edit_iot_aen801_readme_native_sim_copy_comment(text: str) -> str:
    """tan-cli#1001 review: reverse the "copy it in first" comment removal
    above to recover the emit's own (false-in-a-tan-scaffold) instruction."""
    return text.replace(
        _IOT_AEN801_README_NATIVE_SIM_COPY_COMMENT_EDITED,
        _IOT_AEN801_README_NATIVE_SIM_COPY_COMMENT_EMITTED,
    )


#: tan-cli#821(a): `edge-ai`'s `## Model` / `## Tests` README sections and the
#: matching `src/main.c` comments point at `models/README.md` and
#: `tests/unit/cold_chain` -- both real only in the alp-sdk checkout the text
#: was captured from, never emitted into any scaffolded project
#: (`_vendored_files` in `tan/core/scaffold.py` never reaches outside
#: `vendored/edge-ai/<sku>/`). The emit's own bytes are a bare code span and a
#: bare twister argument, so the SDK's markdown-link rewriter never touches
#: them, same as most cross-repo references in this tree, which ARE real
#: `[text](https://github.com/alplabai/alp-sdk/blob/<ref>/...)` links the
#: rewriter already covers. Not the only bare ones, though: `diagnostics`'s
#: `README.md` names a bare `scripts/program_eeprom.py` and `sensor`'s a bare
#: `examples/peripheral-io/i2c-scanner`, the same defect class, tracked
#: separately and NOT fixed here -- tan-cli#912.
_EDGE_AI_README_MODEL_TESTS_EDITED = re.compile(
    r"## Model\n\n"
    r"No model is shipped \(stub \+ deterministic classifier/fallback\)\. The\n"
    r"autoencoder training recipe is alp-sdk's\n"
    r"\[`examples/ai/cold-chain-monitor/models/README\.md`\]"
    r"\(https://github\.com/alplabai/alp-sdk/blob/v0\.16\.0/"
    r"examples/ai/cold-chain-monitor/models/README\.md\)\n"
    r"-- not part of this scaffolded project; the path lives only in an "
    r"alp-sdk\n"
    r"checkout, though the link above works without one\.\n\n"
    r"## Tests\n\n"
    r"The `cold_chain` core's host-unit test suite is alp-sdk's\n"
    r"\[`tests/unit/cold_chain`\]"
    r"\(https://github\.com/alplabai/alp-sdk/tree/v0\.16\.0/tests/unit/cold_chain\)\n"
    r"-- also not part of this scaffolded project\. From an alp-sdk checkout:\n\n"
    r"```\n"
    r"twister -p native_sim/native/64 -T tests/unit/cold_chain\n"
    r"```\n"
)

_EDGE_AI_README_MODEL_TESTS_EMITTED = (
    "## Model\n\n"
    "No model is shipped (stub + deterministic classifier/fallback). See\n"
    "`models/README.md` for the autoencoder training recipe.\n\n"
    "## Tests\n\n"
    "```\n"
    "twister -p native_sim/native/64 -T tests/unit/cold_chain\n"
    "```\n"
)


def un_edit_edge_ai_readme_model_tests_pointers(text: str) -> str:
    """tan-cli#821(a): reverse the README `## Model` / `## Tests` rewrite
    above to recover the emit's own (dead-pointer) bytes."""
    return _EDGE_AI_README_MODEL_TESTS_EDITED.sub(
        _EDGE_AI_README_MODEL_TESTS_EMITTED, text
    )


_EDGE_AI_MAIN_C_MODEL_COMMENT_1_EDITED = re.compile(
    r" \* \(see alp-sdk's examples/ai/cold-chain-monitor/models/README\.md -- not\n"
    r" \* part of this scaffolded project\); with no model the deterministic\n"
    r" \* classifier \+ anomaly fallback run\.\n"
)
_EDGE_AI_MAIN_C_MODEL_COMMENT_1_EMITTED = (
    " * (see models/README.md); with no model the deterministic classifier + anomaly\n"
    " * fallback run.\n"
)

_EDGE_AI_MAIN_C_MODEL_COMMENT_2_EDITED = re.compile(
    r" \* detects and routes to cc_anomaly_fallback\(\)\.  See alp-sdk's\n"
    r" \* examples/ai/cold-chain-monitor/models/README\.md \(not part of this\n"
    r" \* scaffolded project\) for the autoencoder training recipe to replace this\n"
    r" \* stub\. \*/\n"
)
_EDGE_AI_MAIN_C_MODEL_COMMENT_2_EMITTED = (
    " * detects and routes to cc_anomaly_fallback().  See models/README.md for\n"
    " * the autoencoder training recipe to replace this stub. */\n"
)


def un_edit_edge_ai_main_c_model_comment_1(text: str) -> str:
    """tan-cli#821(a): reverse the first `src/main.c` `models/README.md`
    comment rewrite above to recover the emit's own (dead-pointer) bytes.

    Kept as its own `DELIBERATE_EDITS` entry, separate from comment 2 below,
    so healing one comment without the other still fails the declaration --
    a single combined un_edit over both substitutions passed `after == before`
    only if BOTH were already healed, so restoring just this one left the
    still-live comment-2 edit passing silently (measured)."""
    return _EDGE_AI_MAIN_C_MODEL_COMMENT_1_EDITED.sub(
        _EDGE_AI_MAIN_C_MODEL_COMMENT_1_EMITTED, text
    )


def un_edit_edge_ai_main_c_model_comment_2(text: str) -> str:
    """tan-cli#821(a): reverse the second `src/main.c` `models/README.md`
    comment rewrite above to recover the emit's own (dead-pointer) bytes.
    See `un_edit_edge_ai_main_c_model_comment_1` for why this is a separate
    entry rather than folded into one function with it."""
    return _EDGE_AI_MAIN_C_MODEL_COMMENT_2_EDITED.sub(
        _EDGE_AI_MAIN_C_MODEL_COMMENT_2_EMITTED, text
    )


#: tan-cli#814, re-anchored tan-cli#1001 review (blocker): the emit's own
#: sentence tells the customer to flip `som.sku` to either `E1M-V2M101` or
#: `E1M-V2M102` in `board.yaml` and stop there. On the `E1M-V2N101` sibling
#: that is correct (V2N101/V2M101/V2M102 are the same PCB, same
#: `preset:`/`cores:`/`pins:`, and alp-sdk#1749 made the sentence SKU-neutral
#: there -- see `deepx_v2m102_scope` in the retirement block comment below),
#: but here it is a cross-family swap (`alif-ensemble` -> `renesas-rzv2n-
#: deepx`) that leaves `preset: e1m-evk`, `cores:` and `pins:` all pinned to
#: the Alif module -- measured: `tan validate` refuses with ALP-B007 (board/
#: family mismatch: `board preset 'e1m-evk' hosts SoM families
#: ['alif-ensemble', 'nxp-imx9'], but E1M-V2M101 is family
#: 'renesas-rzv2n-deepx'`). alp-sdk#1749's rewording at the 722320a1 re-vendor
#: made the V2N101 sibling's sentence correct but reintroduced the identical
#: AEN801 defect in new words -- the anchor below matches the NEW wording, not
#: the tan-cli#814-era one it replaces. Matches the literal sentence, not a
#: paraphrase, so an unrelated README edit still fails this gate.
_EDGE_AI_AEN801_README_DEEPX_NOTE = (
    "For the DEEPX DX-M1 path, re-scaffold rather than edit: `tan init --template\n"
    "edge-ai-starter --som E1M-V2M101`. Flipping `som.sku` alone leaves `preset:`,\n"
    "`cores:` and `pins:` pinned to this module (`e1m-evk`, which does not host\n"
    "the `renesas-rzv2n-deepx` family) and `tan validate` refuses it."
)
_EDGE_AI_AEN801_README_DEEPX_NOTE_EMITTED = (
    "The DEEPX DX-M1 NPU is populated on `E1M-V2M101`/`E1M-V2M102` -- not on\n"
    "`E1M-V2N101`/`E1M-V2N102`, the same PCB without it. Pick either via\n"
    "`som.sku` in `board.yaml`."
)


def un_edit_edge_ai_aen801_readme_deepx_note(text: str) -> str:
    """tan-cli#814, re-anchored tan-cli#1001 review: undo the README
    correction above to recover the emit's own (still-wrong on this SKU)
    sentence -- alp-sdk's 722320a1 rewording fixed this sentence for the
    E1M-V2N101 sibling but not for E1M-AEN801, which cannot flip `som.sku`
    to a DEEPX SKU at all without re-scaffolding."""
    return text.replace(
        _EDGE_AI_AEN801_README_DEEPX_NOTE, _EDGE_AI_AEN801_README_DEEPX_NOTE_EMITTED
    )


#: tan-cli#946 (review round on #932/#942): the SAME emit sentence entry 5/6
#: above rewrites for `E1M-AEN801` is ALSO wrong on the `E1M-V2N101` tree
#: itself -- just for a different sibling. `E1M-V2N101`/`E1M-V2N102`/
#: `E1M-V2M101`/`E1M-V2M102` all render this ONE tree; "Flip `som.sku` ...
#: to `E1M-V2M101`" is correct advice for a V2N101/V2N102 customer (neither
#: carries DEEPX) but misleading for a V2M102 customer, who already has it:
#: `metadata/socs/deepx/dx/m1.json`'s `alp_module_skus` lists BOTH
#: `E1M-V2M101` and `E1M-V2M102` (confirmed on `E1M-V2M102.yaml`'s own
#: `on_module.npu: deepx_dxm1`), so telling that customer to flip to
#: `E1M-V2M101` implies their own SKU lacks what it already has. Rewritten
#: SKU-neutral -- true regardless of which of the four SKUs the customer
#: is actually on -- rather than naming one specific target SKU. Filed
#: upstream as alp-sdk#1749; this entry retires the moment that lands and
#: this tree is re-vendored.
_EDGE_AI_V2N101_README_DEEPX_NOTE = (
    "`E1M-V2M101` and `E1M-V2M102` both carry the DEEPX DX-M1 NPU; pick either via\n"
    "`som.sku` in `board.yaml` for the DEEPX DX-M1 path."
)
_EDGE_AI_V2N101_README_DEEPX_NOTE_EMITTED = (
    "Flip `som.sku` in `board.yaml` to `E1M-V2M101` for the DEEPX DX-M1 path."
)


def un_edit_edge_ai_v2n101_readme_deepx_note(text: str) -> str:
    """tan-cli#946: undo the README correction above to recover the emit's
    own (still-wrong, V2M102-misleading) sentence -- alp-sdk#1749 has not
    landed yet, so the live emit still says this."""
    return text.replace(
        _EDGE_AI_V2N101_README_DEEPX_NOTE, _EDGE_AI_V2N101_README_DEEPX_NOTE_EMITTED
    )


#: tan-cli#912: `diagnostics`'s README names a bare
#: `scripts/program_eeprom.py` -- real only in the alp-sdk checkout the text
#: was captured from, never emitted into any scaffolded project
#: (`_vendored_files` in `tan/core/scaffold.py` never reaches outside
#: `vendored/diagnostics/<sku>/`). A bare inline code span, not a markdown
#: link, so the emit's own doc-link rewriter never touches it -- the same
#: shape as tan-cli#821(a)'s `edge-ai` pointers above. Byte-identical between
#: the two SKUs at this bullet (the two SKUs also differ at the `west
#: build`/`west flash` block and, for `diagnostics`, the "Real hardware"
#: heading and `[selftest] SoM identity` line above it -- none of that
#: touches the matched region here), so both entries below share this one
#: `un_edit`.
_DIAGNOSTICS_README_EEPROM_SCRIPT_EDITED = (
    "* **SoM identity `ALP_ERR_NOT_PROVISIONED`.** The on-module EEPROM\n"
    "  reads back blank -- the module was never run through alp-sdk's\n"
    "  [`scripts/program_eeprom.py`](https://github.com/alplabai/alp-sdk/blob/v0.16.0/scripts/program_eeprom.py)\n"
    "  at production test -- not part of this scaffolded project; the path\n"
    "  lives only in an alp-sdk checkout, though the link above works\n"
    "  without one. On a factory-fresh board this is expected; on a\n"
    "  shipped SoM it is a real fault.\n"
)
_DIAGNOSTICS_README_EEPROM_SCRIPT_EMITTED = (
    "* **SoM identity `ALP_ERR_NOT_PROVISIONED`.** The on-module EEPROM\n"
    "  reads back blank -- the module was never run through\n"
    "  `scripts/program_eeprom.py` at production test. On a factory-fresh\n"
    "  board this is expected; on a shipped SoM it is a real fault.\n"
)


def un_edit_diagnostics_readme_eeprom_script(text: str) -> str:
    """tan-cli#912: reverse the README `program_eeprom.py` rewrite above to
    recover the emit's own (dead-pointer) bytes."""
    return text.replace(
        _DIAGNOSTICS_README_EEPROM_SCRIPT_EDITED,
        _DIAGNOSTICS_README_EEPROM_SCRIPT_EMITTED,
    )


#: tan-cli#912: `sensor`'s README names a bare
#: `examples/peripheral-io/i2c-scanner` in one Troubleshooting bullet -- the
#: same referent this file links TWICE elsewhere as a real markdown link
#: (the emit's own doc-link rewriter already covers those two), so this is
#: the only bare instance in this README, not a novel defect shape. (At the
#: time this entry was written the wider `sensor` scaffold still shipped
#: several more bare `i2c-scanner` referents outside this README --
#: `src/main.c`'s four are now covered below (tan-cli#924); `board.yaml` and
#: `testcase.yaml` carry one bare, descriptive mention each and remain out
#: of scope.) Byte-identical between the two SKUs, so both entries below
#: share this one `un_edit`.
_SENSOR_README_I2C_SCANNER_BULLET_EDITED = (
    "  slave).  Run alp-sdk's\n"
    "  [`examples/peripheral-io/i2c-scanner`](https://github.com/alplabai/alp-sdk/tree/v0.16.0/examples/peripheral-io/i2c-scanner)\n"
    "  to confirm what ACKs -- not part of this scaffolded project.\n"
)
_SENSOR_README_I2C_SCANNER_BULLET_EMITTED = (
    "  slave).  Run `examples/peripheral-io/i2c-scanner` to confirm what ACKs.\n"
)


def un_edit_sensor_readme_i2c_scanner_bullet(text: str) -> str:
    """tan-cli#912: reverse the README `i2c-scanner` bullet rewrite above to
    recover the emit's own (dead-pointer) bytes."""
    return text.replace(
        _SENSOR_README_I2C_SCANNER_BULLET_EDITED,
        _SENSOR_README_I2C_SCANNER_BULLET_EMITTED,
    )


_SENSOR_MAIN_C_BRINGUP_INSTRUCTION_EDITED = (
    " * On a brand-new bring-up you may want to run alp-sdk's\n"
    " * examples/peripheral-io/i2c-scanner (not part of this scaffolded\n"
    " * project) first to confirm which address ACKs.\n"
)
_SENSOR_MAIN_C_BRINGUP_INSTRUCTION_EMITTED = (
    " * On a brand-new bring-up you may want to run examples/peripheral-io/i2c-scanner\n"
    " * first to confirm which address ACKs.\n"
)


def un_edit_sensor_main_c_bringup_instruction(text: str) -> str:
    """tan-cli#924: reverse the header-comment `run i2c-scanner first`
    run-this instruction rewrite above to recover the emit's own
    (dead-pointer) bytes."""
    return text.replace(
        _SENSOR_MAIN_C_BRINGUP_INSTRUCTION_EDITED,
        _SENSOR_MAIN_C_BRINGUP_INSTRUCTION_EMITTED,
    )


_SENSOR_MAIN_C_INIT_FAIL_COMMENT_EDITED = (
    "     * If init fails the example exits cleanly -- maybe the chip\n"
    "     * isn't populated, maybe the address is wrong, maybe the\n"
    "     * bus is held low by another device.  alp-sdk's\n"
    "     * examples/peripheral-io/i2c-scanner (not part of this\n"
    "     * scaffolded project) can confirm which devices ACK. */\n"
)
_SENSOR_MAIN_C_INIT_FAIL_COMMENT_EMITTED = (
    "     * If init fails the example exits cleanly -- maybe the chip\n"
    "     * isn't populated, maybe the address is wrong, maybe the\n"
    "     * bus is held low by another device.  i2c-scanner can\n"
    "     * confirm which devices ACK. */\n"
)


def un_edit_sensor_main_c_init_fail_comment(text: str) -> str:
    """tan-cli#924: reverse the `tmp112_init` doc-comment `i2c-scanner can
    confirm` rewrite above to recover the emit's own (dead-pointer) bytes."""
    return text.replace(
        _SENSOR_MAIN_C_INIT_FAIL_COMMENT_EDITED,
        _SENSOR_MAIN_C_INIT_FAIL_COMMENT_EMITTED,
    )


_SENSOR_MAIN_C_FAILURE_MODES_INSTRUCTION_EDITED = (
    "         * Use alp-sdk's examples/peripheral-io/i2c-scanner (not\n"
    "         * part of this scaffolded project) to enumerate what IS\n"
    "         * on this bus before chasing a TMP112 that may not be\n"
    "         * populated. */\n"
)
_SENSOR_MAIN_C_FAILURE_MODES_INSTRUCTION_EMITTED = (
    "         * Use i2c-scanner to enumerate what IS on this bus before\n"
    "         * chasing a TMP112 that may not be populated. */\n"
)


def un_edit_sensor_main_c_failure_modes_instruction(text: str) -> str:
    """tan-cli#924: reverse the "Most-frequent failure modes" `Use
    i2c-scanner` run-this instruction rewrite above to recover the emit's
    own (dead-pointer) bytes."""
    return text.replace(
        _SENSOR_MAIN_C_FAILURE_MODES_INSTRUCTION_EDITED,
        _SENSOR_MAIN_C_FAILURE_MODES_INSTRUCTION_EMITTED,
    )


_SENSOR_MAIN_C_HARDWARE_PARAGRAPH_EDITED = (
    " * SoM per alp-sdk's metadata/chips/tmp112.yaml (not part of this\n"
    " * scaffolded project).  7-bit address depends on the ADD0 strap,\n"
    " * which selects one of 0x48..0x4B; every current SoM family straps\n"
    " * ADD0 = GND, so the address is 0x48 throughout.\n"
)
_SENSOR_MAIN_C_HARDWARE_PARAGRAPH_EMITTED = (
    " * SoM per metadata/chips/tmp112.yaml.  7-bit address depends on\n"
    " * the ADD0 strap, which selects one of 0x48..0x4B; every current\n"
    " * SoM family straps ADD0 = GND, so the address is 0x48 throughout.\n"
)


def un_edit_sensor_main_c_hardware_paragraph(text: str) -> str:
    """PR #975 review round: reverse the header-comment `Hardware:`
    paragraph's bare `metadata/chips/tmp112.yaml` referent rewrite above
    (a sibling of tan-cli#924's `pattern_paragraph`/`bringup_instruction`
    entries three lines below it, in the same rewritten comment block, that
    #924 itself left uncovered) to recover the emit's own (dead-pointer)
    bytes."""
    return text.replace(
        _SENSOR_MAIN_C_HARDWARE_PARAGRAPH_EDITED,
        _SENSOR_MAIN_C_HARDWARE_PARAGRAPH_EMITTED,
    )


#: Same tan-cli#814 defect, the `board.yaml` comment that reinforces the bad
#: README instruction one file over.
_EDGE_AI_AEN801_BOARD_YAML_DEEPX_NOTE = (
    "and classifies the integrity state.  The V2N\n"
    "# DEEPX path is a separate scaffold (--som E1M-V2M101), not a som.sku flip\n"
    "# here -- see this project's README."
)
_EDGE_AI_AEN801_BOARD_YAML_DEEPX_NOTE_EMITTED = (
    "and classifies the integrity state.  Same source\n"
    "# targets the V2N DEEPX path when som.sku is flipped."
)


def un_edit_edge_ai_aen801_board_yaml_deepx_note(text: str) -> str:
    """tan-cli#814: undo the `board.yaml` comment correction to recover the
    emit's own (still-wrong) comment."""
    return text.replace(
        _EDGE_AI_AEN801_BOARD_YAML_DEEPX_NOTE,
        _EDGE_AI_AEN801_BOARD_YAML_DEEPX_NOTE_EMITTED,
    )


#: The hand-edits `python/tan/templates/vendored/MANIFEST.md` declares under
#: "Deliberate edits on top of the emit" -- the only bytes in that tree that
#: are NOT what `--emit scaffold` produced, each because the emit's own output
#: is wrong for a customer and the fix lives in alp-sdk, not here.
#:
#: Keyed `(template, sku, path, edit_id)`; the value is `(reason, un_edit)`
#: where `un_edit` maps the VENDORED bytes back onto what the emit is expected
#: to say. `diff_trees` then runs against THAT, so a declaration excuses
#: exactly the edit it describes and nothing else -- an unrelated change in
#: the same file still fails, which a path-level allow-list can never do.
#: `edit_id` exists so two independent substitutions in the same file get two
#: entries, applied in dict-iteration order: bundling them under one `path`
#: key let `after == before` (the strict check below) pass on the AGGREGATE,
#: so healing only one of two comments in `edge-ai`'s `src/main.c` -- see the
#: two `model_comment_*` entries -- found the OTHER substitution still
#: matching, the combined result still changed, and the stale half's own
#: declaration failure never fired (measured, tan-cli#908 review).
#:
#: Strict in the `xfail(strict=True)` sense this repo declares divergences with
#: (`python/tests/parity/test_scaffold_content_oracle_parity.py`'s
#: `DELIBERATE_DIVERGENCE`, same discipline for the port-vs-oracle axis): an
#: `un_edit` that finds nothing to undo is a FAILURE. A healed divergence --
#: the tree re-vendored, or alp-sdk fixing its emit -- has to force the entry
#: out, otherwise the next real drift in that file inherits a dead excuse.
# tan-cli#384's seven README entries were RETIRED here (the tan-cli#543/#545
# re-vendor). They pinned every scaffold README's doc links at the vendor ref
# `v0.15.0-rc1` because the emit's own `v0.15.0` was a tag "alp-sdk has never
# cut" -- a link a customer could not open. MEASURED: alp-sdk HAS since tagged
# `v0.15.0` (`e2928b9f`, `git tag -l v0.15*`), so the emit's own bytes are
# browsable and the divergence is healed. This module's own doctrine directly
# above says a healed divergence must force its entry OUT rather than linger as
# a dead excuse, and an `un_edit` with nothing to undo is a hard failure -- so
# the entries are gone and the tree carries the emit's own vendor-point links
# (`v0.15.0` at the time this paragraph was written; `v0.16.0` since the
# tan-cli#891 pin bump -- see `MANIFEST.md`'s "Current vendor point").
# `un_edit_doc_link_ref` is kept: it is the only record of the transform, and
# the next pre-release vendor point will need it again.
#: tan-cli#864 (Q5): the vendored multicore-mailbox README gains a leading
#: caveat that the scaffold's own IPC carve-out resolves `blocked` on
#: E1M-AEN801 -- measured, `memory_map.base is TBD for region 'mram_main'`.
#: The emit says nothing about it, and a customer whose first run silently
#: does nothing is the failure this section exists to prevent. Undo it to
#: recover the emit's own bytes.
#: `\n+` on purpose: the caveat is appended after the emit's own trailing
#: newline, so the vendored file carries a blank line the emit does not.
#: Matching a single `\n` leaves that blank line behind and the byte-diff
#: still fails -- measured, `'...ahead.\n\n'` against the emit's
#: `'...ahead.\n'`.
_MAILBOX_BLOCKED_CAVEAT = re.compile(
    r"\n+## Before you run this: the channel is not allocated yet\n.*\Z", re.S
)


def un_edit_mailbox_blocked_caveat(text: str) -> str:
    return _MAILBOX_BLOCKED_CAVEAT.sub("\n", text)


#: tan-cli#932: `diagnostics/E1M-V2N101` is a Renesas RZ/V2N scaffold whose
#: `src/main.c` was never SKU-substituted at all -- it is byte-identical to
#: `E1M-AEN801`'s, still naming that Alif module in its own "what success
#: looks like" comment -- and whose `README.md` was substituted on the SoM
#: SKU line only, leaving the serial beside it and both `SoC identity:`
#: lines at their AEN801/Alif values. Six entries, not one, per the
#: `edit_id` discipline above: the SoM SKU fix in `src/main.c` gets TWO
#: (the header comment and the sample-output line are independent
#: locations -- `README.md` already had this half right, so neither has a
#: `README.md` counterpart), and the placeholder serial and the SoC
#: identity string each get ONE entry PER FILE (`edit_id` keys on `path`) --
#: 2 + 2 + 2 = 6.
#:
#: The SoC identity string is NOT invented to match the Alif shape: it is
#: `renesas:rzv2n:n44`, `metadata/socs/renesas/rzv2n/n44.json`'s own `ref`
#: field (what `scripts/gen_soc_caps.py` bakes into `ALP_SOC_REF_STR`), the
#: same file `metadata/e1m_modules/E1M-V2N101.yaml`'s `silicon:` key names
#: for this SKU. `n44` not `n48` on purpose: alp-sdk's own SoC json documents
#: the n44/n48 delta as GPU/ISP/crypto fusing only (devicetree-identical for
#: the M33 + SPI/I2C peripherals this SDK targets) and that `n48` is only the
#: Zephyr `zephyr_soc_variant` Kconfig/DT symbol Zephyr board artefacts
#: reference in place of the SoC json's own `ref` -- which is why this
#: README's OWN (correct, untouched) `west build -b
#: alp_e1m_v2n101_m33_sm/r9a09g056n48gbg/cm33` line names `n48gbg` two lines
#: below a `SoC identity:` string that correctly names `n44`; not a
#: contradiction, two different identifiers for the same silicon.
#:
#: The serial is NOT a plausible-looking invented value in AEN801's shape
#: (e.g. `V2N0000123`) -- alp-sdk's own `scripts/program_eeprom.py --serial`
#: help text uses a completely different shape (`2026W19-0001`), which
#: proves `AEN0000123` was never a schema-driven format to begin with, just
#: this one example's flavour text. `<factory-serial>` reuses the README's
#: own angle-bracket placeholder convention already sitting two lines above
#: it (`west flash --host <board-ip>`) -- obviously a placeholder, not
#: readable as a real serial.
_MAIN_C_V2N101_SOM_SKU_HEADER_EDITED = (
    " * What success looks like (real hardware, E1M-V2N101):\n"
)
_MAIN_C_V2N101_SOM_SKU_HEADER_EMITTED = (
    " * What success looks like (real hardware, E1M-AEN801):\n"
)


def un_edit_main_c_v2n101_som_sku_header(text: str) -> str:
    """tan-cli#932: reverse the "what success looks like" heading's SKU fix
    to recover the emit's own (unsubstituted, AEN801-named) bytes."""
    return text.replace(
        _MAIN_C_V2N101_SOM_SKU_HEADER_EDITED, _MAIN_C_V2N101_SOM_SKU_HEADER_EMITTED
    )


_MAIN_C_V2N101_SOM_SKU_OUTPUT_LINE_EDITED = (
    " *   [selftest] SoM identity: E1M-V2N101 rev r1 sn"
)
_MAIN_C_V2N101_SOM_SKU_OUTPUT_LINE_EMITTED = (
    " *   [selftest] SoM identity: E1M-AEN801 rev r1 sn"
)


def un_edit_main_c_v2n101_som_sku_output_line(text: str) -> str:
    """tan-cli#932: reverse the sample-output SoM-identity line's SKU fix to
    recover the emit's own (unsubstituted, AEN801-named) bytes. Own entry,
    separate from the serial fix on the same line -- see the `edit_id`
    discipline above -- so healing one half without the other still reds."""
    return text.replace(
        _MAIN_C_V2N101_SOM_SKU_OUTPUT_LINE_EDITED,
        _MAIN_C_V2N101_SOM_SKU_OUTPUT_LINE_EMITTED,
    )


def un_edit_v2n101_serial_placeholder(text: str) -> str:
    """tan-cli#932: reverse the `<factory-serial>` placeholder back onto the
    emit's own AEN-shaped `AEN0000123` bytes. Shared by `src/main.c` and
    `README.md` -- the placeholder text is identical in both, own entry per
    file since `edit_id` is keyed per `(template, sku, path, edit_id)`.

    `.replace()`, unbounded -- every occurrence of the matched text is
    undone, not just the first. Deliberate and safe for THIS token today:
    `sn <factory-serial>` appears exactly once per file. The discipline this
    keys on is per-TOKEN (one `un_edit_*` per distinct wrong string), not
    per-OCCURRENCE-COUNT -- a second, independent `sn <factory-serial>`
    landing in either file later would be silently folded into this same
    call rather than flagged as a new thing to declare. `un_edit_v2n101_soc_
    identity` below makes the same trade deliberately (its docstring counts
    the two occurrences it undoes); this one has never needed to."""
    return text.replace("sn <factory-serial>", "sn AEN0000123")


def un_edit_v2n101_soc_identity(text: str) -> str:
    """tan-cli#932: reverse the `renesas:rzv2n:n44` SoC-identity fix back
    onto the emit's own (unsubstituted) `alif:ensemble:e8` bytes. A plain
    substring replace, not a regex -- `README.md` carries two occurrences
    (the real-hardware and native_sim blocks) and both are the same wrong
    token with the same correct replacement, so one `.replace()` (which
    substitutes every occurrence by default) undoes both without needing two
    entries; `src/main.c` carries one occurrence and the same call is a
    no-op past it.

    Unbounded like `un_edit_v2n101_serial_placeholder` above, same trade:
    every occurrence of `renesas:rzv2n:n44` is undone, whichever line it is
    on. Correct today because every occurrence in each file IS this one
    wrong token; a future SECOND (different) reason for that string to
    appear in either file would be masked by the same call rather than
    caught as its own gap -- the discipline here is per-token, not
    per-occurrence."""
    return text.replace("renesas:rzv2n:n44", "alif:ensemble:e8")


#: tan-cli#977 (the follow-up PR #975's review round scoped out, see the
#: retired entry 12 above): `sensor`'s `src/main.c` carried a bare
#: `metadata/chips/tmp112.yaml` and a bare `include/alp/chips/tmp112.h` in
#: its `TMP112_ADDR_7BIT` doc-comment when this entry was first written.
#: **Re-derived against `dev`'s tan-cli#996/#1001 re-vendor** (alp-sdk#1269:
#: `sensor` swapped its chip from TMP112 to BMP581, since TMP112 lives on
#: BRD_I2C -- a bus the E1M-AEN family's LPI2C0 cannot master at all -- not
#: on the `BOARD_I2C_SENSORS` bus this example actually opens): the anchors
#: below match the CURRENT vendored bytes, not the TMP112-era ones this
#: entry was filed against. `#1001` retired entry 12 outright rather than
#: re-deriving it -- entries 11/12's own module doctrine two sections up
#: warns against exactly that conflation ("retire only when upstream healed
#: the underlying problem, not when the anchor stopped matching"): the
#: TMP112-specific bytes are genuinely gone, but the BARE-REFERENT DEFECT
#: recurred in the new BMP581 paragraph verbatim, one bare mention for the
#: other. Swept the whole file rather than only the two lines the issue
#: named (`src/main.c:51,53` in the pre-re-vendor tree): the `Hardware:`
#: paragraph and the `#1269` historical note each gained a fresh bare
#: mention neither predecessor entry ever covered, and the `bmp581_init`
#: failure doc-comment's `i2c-scanner` mention -- entry 11's `init_fail_
#: comment`, also retired anchor-gone by `#1001` -- recurred near-verbatim.
#: Five substitutions, one per site, byte-identical between the two SKUs
#: (`sensor`'s `src/main.c` carries no SKU substitution at all, confirmed
#: against the current tree), so each `un_edit` is shared across both.
_SENSOR_MAIN_C_HARDWARE_BOARDS_POINTER_EMITTED = (
    "on both -- see metadata/boards/e1m-evk.yaml and\n"
    " * metadata/boards/e1m-x-evk.yaml `i2c_devices:`.  On a brand-new\n"
)
_SENSOR_MAIN_C_HARDWARE_BOARDS_POINTER_EDITED = (
    "on both -- see alp-sdk's metadata/boards/e1m-evk.yaml and\n"
    " * metadata/boards/e1m-x-evk.yaml (not part of this scaffolded\n"
    " * project) `i2c_devices:`.  On a brand-new\n"
)


def un_edit_sensor_main_c_hardware_boards_pointer(text: str) -> str:
    """tan-cli#977: reverse the `Hardware:` paragraph's bare
    `metadata/boards/e1m-evk.yaml`/`e1m-x-evk.yaml` mentions rewrite above
    to recover the emit's own (dead-pointer) bytes."""
    return text.replace(
        _SENSOR_MAIN_C_HARDWARE_BOARDS_POINTER_EDITED,
        _SENSOR_MAIN_C_HARDWARE_BOARDS_POINTER_EMITTED,
    )


_SENSOR_MAIN_C_ADDR_METADATA_POINTER_EMITTED = (
    "E1M-X EVK (see\n * metadata/boards/e1m-evk.yaml / e1m-x-evk.yaml `i2c_devices:`).\n"
)
_SENSOR_MAIN_C_ADDR_METADATA_POINTER_EDITED = (
    "E1M-X EVK (see alp-sdk's\n"
    " * metadata/boards/e1m-evk.yaml / e1m-x-evk.yaml, not part of\n"
    " * this scaffolded project, `i2c_devices:`).\n"
)


def un_edit_sensor_main_c_addr_metadata_pointer(text: str) -> str:
    """tan-cli#977: reverse the `BMP581_ADDR_7BIT` doc-comment's bare
    `metadata/boards/e1m-evk.yaml`/`e1m-x-evk.yaml` mention rewrite above to
    recover the emit's own (dead-pointer) bytes."""
    return text.replace(
        _SENSOR_MAIN_C_ADDR_METADATA_POINTER_EDITED,
        _SENSOR_MAIN_C_ADDR_METADATA_POINTER_EMITTED,
    )


_SENSOR_MAIN_C_ADDR_HEADER_POINTER_EMITTED = (
    " * BST-BMP581-DS004 s5.6 or include/alp/chips/bmp581.h if your\n"
    " * board straps differently. */\n"
)
_SENSOR_MAIN_C_ADDR_HEADER_POINTER_EDITED = (
    " * BST-BMP581-DS004 s5.6 or alp-sdk's include/alp/chips/bmp581.h\n"
    " * (not part of this scaffolded project) if your board straps\n"
    " * differently. */\n"
)


def un_edit_sensor_main_c_addr_header_pointer(text: str) -> str:
    """tan-cli#977: reverse the `BMP581_ADDR_7BIT` doc-comment's bare
    `include/alp/chips/bmp581.h` mention rewrite above to recover the emit's
    own (dead-pointer) bytes. Own entry from the `metadata/boards/*.yaml`
    fix above -- the two are independent substitutions in the same comment
    block, per the one-substitution-per-entry discipline (tan-cli#908)."""
    return text.replace(
        _SENSOR_MAIN_C_ADDR_HEADER_POINTER_EDITED,
        _SENSOR_MAIN_C_ADDR_HEADER_POINTER_EMITTED,
    )


#: tan-cli#977: `sensor`'s `board.yaml` carries the board.yaml-side sibling
#: of the `src/main.c` sweep above -- a different file, same defect class,
#: **re-derived against `dev`'s tan-cli#996/#1001 re-vendor** the same way.
#: The "Customer workflow" paragraph names `scripts/alp_project.py` bare (it
#: IS invoked, via `ALP_SDK_ROOT` -- see `CMakeLists.txt` -- but the
#: physical file lives only in the alp-sdk checkout `ALP_SDK_ROOT` resolves,
#: never in this scaffolded tree, so the "not part of this scaffolded
#: project" framing still applies; worded "resolved via ALP_SDK_ROOT" rather
#: than the plain "not part of this scaffolded project" the purely
#: descriptive pointers below use, since this one names real build machinery
#: a customer's own `CMakeLists.txt` genuinely runs) -- unchanged by the
#: re-vendor. The chip-classification paragraph's `metadata/chips/
#: bmp581.yaml` mention is the chip-renamed descendant of the entry this was
#: originally filed against (`tmp112.yaml`); its `scripts/check_example_
#: portability.py` mention is unchanged. The `#1269` historical NOTE
#: paragraph is new (added by the same re-vendor as `src/main.c`'s) and
#: carries two more bare mentions of its own -- `metadata/boards/*.yaml
#: i2c_devices:` and `examples/v2n/v2n-temp-sensor`, the board.yaml-side
#: siblings of `src/main.c`'s equivalents above. Five entries, one per
#: substitution. Byte-identical between the two SKUs at every line these
#: edits touch (the per-SKU diff is `som.sku:`, `preset:`, the `pins:` route
#: and the `cores:` id further down -- confirmed none of it overlaps this
#: paragraph), so all five `un_edit`s are shared across both SKUs.
_SENSOR_BOARD_YAML_WORKFLOW_POINTER_EMITTED = (
    "CMakeLists.txt invokes\n"
    "# scripts/alp_project.py at configure time + layers the\n"
)
_SENSOR_BOARD_YAML_WORKFLOW_POINTER_EDITED = (
    "CMakeLists.txt invokes alp-sdk's\n"
    "# scripts/alp_project.py (resolved via ALP_SDK_ROOT, not part of\n"
    "# this scaffolded project) at configure time + layers the\n"
)


def un_edit_sensor_board_yaml_workflow_pointer(text: str) -> str:
    """tan-cli#977: reverse the "Customer workflow" paragraph's bare
    `scripts/alp_project.py` mention rewrite above to recover the emit's own
    (dead-pointer) bytes."""
    return text.replace(
        _SENSOR_BOARD_YAML_WORKFLOW_POINTER_EDITED,
        _SENSOR_BOARD_YAML_WORKFLOW_POINTER_EMITTED,
    )


_SENSOR_BOARD_YAML_METADATA_POINTER_EMITTED = (
    "families per metadata/chips/bmp581.yaml -- so this example is\n"
    "# Ring 2 (chip-bound, multi-family) per\n"
)
_SENSOR_BOARD_YAML_METADATA_POINTER_EDITED = (
    "families per alp-sdk's metadata/chips/bmp581.yaml (not part of\n"
    "# this scaffolded project) -- so this example is Ring 2\n"
    "# (chip-bound, multi-family) per\n"
)


def un_edit_sensor_board_yaml_metadata_pointer(text: str) -> str:
    """tan-cli#977: reverse the chip-classification paragraph's bare
    `metadata/chips/bmp581.yaml` mention rewrite above to recover the emit's
    own (dead-pointer) bytes."""
    return text.replace(
        _SENSOR_BOARD_YAML_METADATA_POINTER_EDITED,
        _SENSOR_BOARD_YAML_METADATA_POINTER_EMITTED,
    )


_SENSOR_BOARD_YAML_PORTABILITY_SCRIPT_POINTER_EMITTED = (
    "# scripts/check_example_portability.py classification.\n"
)
_SENSOR_BOARD_YAML_PORTABILITY_SCRIPT_POINTER_EDITED = (
    "# alp-sdk's scripts/check_example_portability.py classification\n"
    "# (also not part of this scaffolded project).\n"
)


def un_edit_sensor_board_yaml_portability_script_pointer(text: str) -> str:
    """tan-cli#977: reverse the chip-classification paragraph's bare
    `scripts/check_example_portability.py` mention rewrite above to recover
    the emit's own (dead-pointer) bytes. Own entry from the
    `metadata/chips/bmp581.yaml` fix above, per the one-substitution-per-entry
    discipline."""
    return text.replace(
        _SENSOR_BOARD_YAML_PORTABILITY_SCRIPT_POINTER_EDITED,
        _SENSOR_BOARD_YAML_PORTABILITY_SCRIPT_POINTER_EMITTED,
    )


#: RE-ANCHORED tan-cli#1026 (finishing the f1b1c9df re-sync): anchor moved,
#: problem intact -- alp-sdk#1866 (`f1b1c9df`) reworded the technical claim
#: in the middle of this NOTE (the old, WRONG "On the E1M-AEN family
#: BRD_I2C is additionally the slave-only Alif LPI2C0 (ADR 0017)" text,
#: corrected by that same commit's own PR title: BRD_I2C is master-capable
#: I2C0 on P7_0/P7_1, not the slave-only LPI2C0), which shifted this
#: entry's old `"...opens. On the\n"` trailing anchor out of existence --
#: but did not touch the bare `metadata/boards/e1m-evk.yaml`/
#: `e1m-x-evk.yaml` mentions this entry qualifies, which upstream still
#: emits bare. Span narrowed to end right before the reworded sentence
#: (`"...on each -- "`, shared boundary with the sibling
#: `historical_note_v2n_pointer` entry below) so it no longer overlaps
#: upstream's changed text; the middle sentence itself passes through
#: unedited (it needs no qualifier -- it names no path).
#: RE-ANCHORED tan-cli#1151 (the `ff27f179` re-vendor). The old span ran to
#: `# boards, at the same address (0x47) on each -- ` and handed the rest of
#: the paragraph to the sibling `historical_note_v2n_pointer` entry, which
#: alp-sdk#1855 superseded and this same change retired. Left as it was, the
#: replacement's `--\n` tail landed in front of the emit's own ` see\n#
#: https://...` continuation and orphaned a bare `see` line with no `# `
#: prefix -- which made the vendored `board.yaml` INVALID YAML (measured:
#: `yaml.safe_load` -> `ScannerError: could not find expected ':'` at line
#: 52, caught by `tests/commands/test_validate_command.py::
#: test_offline_accepts_every_board_yaml_the_templates_ship`). Narrowed to
#: the two `metadata/boards/*.yaml` mentions this entry actually exists to
#: qualify, ending mid-sentence at `BRD_I2C` so the tail is untouched
#: passthrough. alp-sdk#1855 does NOT rewrite `metadata/` referents (its own
#: `_BARE_REPO_PATH_RE` comment says so), which is why this entry survives
#: the re-vendor at all while its paragraph-mate did not.
_SENSOR_BOARD_YAML_HISTORICAL_NOTE_BOARDS_POINTER_EMITTED = (
    "BRD_I2C, not on BOARD_I2C_SENSORS -- see metadata/boards/e1m-evk.yaml\n"
    "# i2c_devices: and metadata/boards/e1m-x-evk.yaml i2c_devices:, neither\n"
    "# of which lists a TMP112 on the sensor bus this example opens. BRD_I2C"
)
_SENSOR_BOARD_YAML_HISTORICAL_NOTE_BOARDS_POINTER_EDITED = (
    "BRD_I2C, not on BOARD_I2C_SENSORS -- see alp-sdk's\n"
    "# metadata/boards/e1m-evk.yaml i2c_devices: and\n"
    "# metadata/boards/e1m-x-evk.yaml i2c_devices: (not part of this\n"
    "# scaffolded project), neither of which lists a TMP112 on the sensor\n"
    "# bus this example opens. BRD_I2C"
)


def un_edit_sensor_board_yaml_historical_note_boards_pointer(text: str) -> str:
    """tan-cli#977, re-anchored tan-cli#1026: reverse the `#1269` historical
    NOTE's bare `metadata/boards/e1m-evk.yaml`/`e1m-x-evk.yaml` mentions
    rewrite above to recover the emit's own (dead-pointer) bytes -- the
    board.yaml-side sibling of `src/main.c`'s `hardware_boards_pointer`
    entry above. `EDITED`'s trailing `"each --\n"` need not match
    `EMITTED`'s trailing `"each -- "` (no newline) byte-for-byte: this is a
    plain independent `str.replace`, not a line-anchored patch, and the
    adjacent `historical_note_v2n_pointer` entry's own `EMITTED` supplies
    the `"see\n..."` that follows in the real emit either way."""
    return text.replace(
        _SENSOR_BOARD_YAML_HISTORICAL_NOTE_BOARDS_POINTER_EDITED,
        _SENSOR_BOARD_YAML_HISTORICAL_NOTE_BOARDS_POINTER_EMITTED,
    )


#: tan-cli#977: `sensor`'s `prj.conf` carries the same
#: `scripts/alp_project.py` bare referent as `board.yaml`'s "Customer
#: workflow" paragraph above, a different file. Byte-identical between the
#: two SKUs (confirmed: `sensor`'s `prj.conf` carries no SKU substitution at
#: all), so one `un_edit` shared across both.
_SENSOR_PRJ_CONF_WORKFLOW_POINTER_EMITTED = (
    "# from board.yaml by scripts/alp_project.py and layered in via\n"
    "# EXTRA_CONF_FILE (see CMakeLists.txt).\n"
)
_SENSOR_PRJ_CONF_WORKFLOW_POINTER_EDITED = (
    "# from board.yaml by alp-sdk's scripts/alp_project.py (resolved\n"
    "# via ALP_SDK_ROOT, not part of this scaffolded project) and\n"
    "# layered in via EXTRA_CONF_FILE (see CMakeLists.txt).\n"
)


def un_edit_sensor_prj_conf_workflow_pointer(text: str) -> str:
    """tan-cli#977: reverse `prj.conf`'s bare `scripts/alp_project.py`
    mention rewrite above to recover the emit's own (dead-pointer) bytes."""
    return text.replace(
        _SENSOR_PRJ_CONF_WORKFLOW_POINTER_EDITED,
        _SENSOR_PRJ_CONF_WORKFLOW_POINTER_EMITTED,
    )


#: tan-cli#977: `minimal`'s `README.md` names two bare example directories
#: ("`gpio-button-led`" / "`i2c-scanner`") in its opening troubleshooting
#: paragraph -- neither emitted into any scaffolded project. Entry 7's shape
#: (a markdown file, so turned into real links rather than named in prose):
#: both are real alp-sdk directories
#: (`examples/peripheral-io/gpio-button-led`,
#: `examples/peripheral-io/i2c-scanner`), pinned at `v0.16.0` per the current
#: vendor point's `- Ref:`, same as every other cross-repo link this tree
#: carries. One substitution (both referents sit in the same sentence, turned
#: into links together), byte-identical between the two SKUs (the per-SKU
#: `west build`/`west flash` block is further down, untouched here), so one
#: `un_edit` shared across both.
_MINIMAL_README_EXAMPLE_POINTERS_EMITTED = (
    "Pin down which one BEFORE moving on to `gpio-button-led` or\n"
    "`i2c-scanner`.\n"
)
_MINIMAL_README_EXAMPLE_POINTERS_EDITED = (
    "Pin down which one BEFORE moving on to alp-sdk's\n"
    "[`examples/peripheral-io/gpio-button-led`]"
    "(https://github.com/alplabai/alp-sdk/tree/v0.16.0/"
    "examples/peripheral-io/gpio-button-led)\n"
    "or\n"
    "[`examples/peripheral-io/i2c-scanner`]"
    "(https://github.com/alplabai/alp-sdk/tree/v0.16.0/"
    "examples/peripheral-io/i2c-scanner)\n"
    "-- neither is part of this scaffolded project.\n"
)


def un_edit_minimal_readme_example_pointers(text: str) -> str:
    """tan-cli#977: reverse the opening troubleshooting paragraph's bare
    `gpio-button-led`/`i2c-scanner` mentions rewrite above to recover the
    emit's own (dead-pointer) bytes."""
    return text.replace(
        _MINIMAL_README_EXAMPLE_POINTERS_EDITED,
        _MINIMAL_README_EXAMPLE_POINTERS_EMITTED,
    )



#: tan-cli#1009 (PR #1009 review finding 1): the byte-identical
#: `scripts/alp_project.py` "Customer workflow" paragraph bare referent
#: `sensor`'s `board.yaml` was fixed for above survived, unqualified, in
#: `minimal` (a template this very PR already edits -- its `README.md`) and
#: `diagnostics` (named in tan-cli#977's own title). Same substitution, same
#: trailing three lines, in all four (template, sku) board.yaml files -- the
#: only difference between `minimal` and `diagnostics` is the preceding
#: `Customer workflow:` sentence naming a different `--from-example` target,
#: which this substitution does not touch. One `un_edit`, four entries.
_MINIMAL_DIAGNOSTICS_BOARD_YAML_WORKFLOW_POINTER_EMITTED = (
    "and `tan build`.  CMakeLists.txt invokes\n"
    "# scripts/alp_project.py at configure time + layers the\n"
    "# generated alp.conf over prj.conf via EXTRA_CONF_FILE.\n"
)
_MINIMAL_DIAGNOSTICS_BOARD_YAML_WORKFLOW_POINTER_EDITED = (
    "and `tan build`.  CMakeLists.txt invokes alp-sdk's\n"
    "# scripts/alp_project.py (resolved via ALP_SDK_ROOT, not part of\n"
    "# this scaffolded project) at configure time + layers the\n"
    "# generated alp.conf over prj.conf via EXTRA_CONF_FILE.\n"
)


def un_edit_minimal_diagnostics_board_yaml_workflow_pointer(text: str) -> str:
    """tan-cli#1009: reverse the "Customer workflow" paragraph's bare
    `scripts/alp_project.py` mention rewrite above (shared, byte-identical
    text, across `minimal` and `diagnostics`, both SKUs) to recover the
    emit's own (dead-pointer) bytes."""
    return text.replace(
        _MINIMAL_DIAGNOSTICS_BOARD_YAML_WORKFLOW_POINTER_EDITED,
        _MINIMAL_DIAGNOSTICS_BOARD_YAML_WORKFLOW_POINTER_EMITTED,
    )


#: tan-cli#1009: `minimal`'s `prj.conf` carries the same bare
#: `scripts/alp_project.py` mention as its own `board.yaml` above, a
#: different file, plus a trailing "Add app-specific tuning knobs" sentence
#: `diagnostics`'s shorter `prj.conf` (below) does not carry -- own constant.
#: Byte-identical between the two SKUs (confirmed: `minimal`'s `prj.conf`
#: carries no SKU substitution at all).
_MINIMAL_PRJ_CONF_WORKFLOW_POINTER_EMITTED = (
    "# Intentionally empty.  Every CONFIG_* this app needs is derived\n"
    "# from board.yaml by scripts/alp_project.py and layered in via\n"
    "# EXTRA_CONF_FILE (see CMakeLists.txt).  Add app-specific tuning\n"
    "# knobs (e.g. CONFIG_HEAP_MEM_POOL_SIZE=N) here only when they\n"
    "# are NOT feature-selection -- everything declarative belongs in\n"
    "# board.yaml.\n"
)
_MINIMAL_PRJ_CONF_WORKFLOW_POINTER_EDITED = (
    "# Intentionally empty.  Every CONFIG_* this app needs is derived\n"
    "# from board.yaml by alp-sdk's scripts/alp_project.py (resolved\n"
    "# via ALP_SDK_ROOT, not part of this scaffolded project) and\n"
    "# layered in via EXTRA_CONF_FILE (see CMakeLists.txt).  Add\n"
    "# app-specific tuning knobs (e.g. CONFIG_HEAP_MEM_POOL_SIZE=N)\n"
    "# here only when they are NOT feature-selection -- everything\n"
    "# declarative belongs in board.yaml.\n"
)


def un_edit_minimal_prj_conf_workflow_pointer(text: str) -> str:
    """tan-cli#1009: reverse `minimal`'s `prj.conf` bare
    `scripts/alp_project.py` mention rewrite above to recover the emit's own
    (dead-pointer) bytes."""
    return text.replace(
        _MINIMAL_PRJ_CONF_WORKFLOW_POINTER_EDITED,
        _MINIMAL_PRJ_CONF_WORKFLOW_POINTER_EMITTED,
    )


#: tan-cli#1009: `diagnostics`'s `prj.conf` carries the same bare
#: `scripts/alp_project.py` mention, four lines total (no trailing tuning-
#: knobs sentence, unlike `minimal`'s) -- own constant. Byte-identical
#: between the two SKUs.
_DIAGNOSTICS_PRJ_CONF_WORKFLOW_POINTER_EMITTED = (
    "# Intentionally empty.  Every CONFIG_* this app needs is derived\n"
    "# from board.yaml by scripts/alp_project.py and layered in via\n"
    "# EXTRA_CONF_FILE (see CMakeLists.txt).\n"
)
_DIAGNOSTICS_PRJ_CONF_WORKFLOW_POINTER_EDITED = (
    "# Intentionally empty.  Every CONFIG_* this app needs is derived\n"
    "# from board.yaml by alp-sdk's scripts/alp_project.py (resolved\n"
    "# via ALP_SDK_ROOT, not part of this scaffolded project) and\n"
    "# layered in via EXTRA_CONF_FILE (see CMakeLists.txt).\n"
)


def un_edit_diagnostics_prj_conf_workflow_pointer(text: str) -> str:
    """tan-cli#1009: reverse `diagnostics`'s `prj.conf` bare
    `scripts/alp_project.py` mention rewrite above to recover the emit's own
    (dead-pointer) bytes."""
    return text.replace(
        _DIAGNOSTICS_PRJ_CONF_WORKFLOW_POINTER_EDITED,
        _DIAGNOSTICS_PRJ_CONF_WORKFLOW_POINTER_EMITTED,
    )


#: tan-cli#1009: `iot`'s `prj.conf` names bare `scripts/alp_project.py` in
#: its own opening paragraph -- own constant, different wording than
#: `sensor`/`minimal`/`diagnostics`'s prj.conf (finding 1 named this exact
#: site as one of the two "different wording" instances).
_IOT_PRJ_CONF_WORKFLOW_POINTER_EMITTED = (
    "# Empty-by-design Kconfig.  The effective build config is the\n"
    "# overlay alp.conf emitted from board.yaml by scripts/alp_project.py\n"
    "# at configure time (see CMakeLists.txt) -- it turns the\n"
    "# `iot: {wifi, tls}` toggles + `libraries: [mbedtls]` into the\n"
    "# CONFIG_* the app needs (Wi-Fi, the MQTT client, mbedtls).\n"
)
_IOT_PRJ_CONF_WORKFLOW_POINTER_EDITED = (
    "# Empty-by-design Kconfig.  The effective build config is the\n"
    "# overlay alp.conf emitted from board.yaml by alp-sdk's\n"
    "# scripts/alp_project.py (resolved via ALP_SDK_ROOT, not part of\n"
    "# this scaffolded project) at configure time (see CMakeLists.txt)\n"
    "# -- it turns the `iot: {wifi, tls}` toggles + `libraries:\n"
    "# [mbedtls]` into the CONFIG_* the app needs (Wi-Fi, the MQTT\n"
    "# client, mbedtls).\n"
)


def un_edit_iot_prj_conf_workflow_pointer(text: str) -> str:
    """tan-cli#1009: reverse `iot`'s `prj.conf` opening paragraph's bare
    `scripts/alp_project.py` mention rewrite above to recover the emit's own
    (dead-pointer) bytes."""
    return text.replace(
        _IOT_PRJ_CONF_WORKFLOW_POINTER_EDITED,
        _IOT_PRJ_CONF_WORKFLOW_POINTER_EMITTED,
    )


#: tan-cli#1009: `iot`'s `prj.conf` also names a bare
#: `examples/connectivity/iot-fleet-ota/prj.conf` in its mbedtls migration
#: note -- own entry, a different paragraph than the workflow pointer above.
_IOT_PRJ_CONF_FLEET_OTA_POINTER_EMITTED = (
    "# runs.  Same workaround as\n"
    "# examples/connectivity/iot-fleet-ota/prj.conf.\n"
)
_IOT_PRJ_CONF_FLEET_OTA_POINTER_EDITED = (
    "# runs.  Same workaround as alp-sdk's\n"
    "# examples/connectivity/iot-fleet-ota/prj.conf (not part of this\n"
    "# scaffolded project).\n"
)


def un_edit_iot_prj_conf_fleet_ota_pointer(text: str) -> str:
    """tan-cli#1009: reverse `iot`'s `prj.conf` migration-note bare
    `examples/connectivity/iot-fleet-ota/prj.conf` mention rewrite above to
    recover the emit's own (dead-pointer) bytes."""
    return text.replace(
        _IOT_PRJ_CONF_FLEET_OTA_POINTER_EDITED,
        _IOT_PRJ_CONF_FLEET_OTA_POINTER_EMITTED,
    )


#: tan-cli#1009: `multicore-mailbox`'s `board.yaml` names alp-sdk's
#: `metadata/e1m_modules/E1M-AEN801.yaml` bare (the reviewer's own
#: `multicore-mailbox/E1M-AEN801/board.yaml:30` finding) -- one SKU only
#: (`multicore-mailbox` ships E1M-AEN801 only).
_MULTICORE_MAILBOX_BOARD_YAML_E1M_MODULES_POINTER_EMITTED = (
    "# #1069 gave E1M-AEN801 a real `memory_map:` (metadata/e1m_modules/\n"
    "# E1M-AEN801.yaml), but this carve-out still resolves `status: blocked`\n"
)
_MULTICORE_MAILBOX_BOARD_YAML_E1M_MODULES_POINTER_EDITED = (
    "# #1069 gave E1M-AEN801 a real `memory_map:` (alp-sdk's\n"
    "# metadata/e1m_modules/E1M-AEN801.yaml, not part of this scaffolded\n"
    "# project), but this carve-out still resolves `status: blocked`\n"
)


def un_edit_multicore_mailbox_board_yaml_e1m_modules_pointer(text: str) -> str:
    """tan-cli#1009: reverse `multicore-mailbox`'s `board.yaml` bare
    `metadata/e1m_modules/E1M-AEN801.yaml` mention rewrite above to recover
    the emit's own (dead-pointer) bytes."""
    return text.replace(
        _MULTICORE_MAILBOX_BOARD_YAML_E1M_MODULES_POINTER_EDITED,
        _MULTICORE_MAILBOX_BOARD_YAML_E1M_MODULES_POINTER_EMITTED,
    )


#: tan-cli#1009: `multicore-mailbox`'s `boards/native_sim_native_64.overlay`
#: (a `NON_ENVELOPE_EXTRAS` file, diffed against the catalog example's own
#: copy) names alp-sdk's `scripts/alp_orchestrate.py` bare (the reviewer's
#: own `multicore-mailbox/E1M-AEN801/boards/native_sim_native_64.overlay:14`
#: finding).
_MULTICORE_MAILBOX_OVERLAY_ORCHESTRATOR_POINTER_EMITTED = (
    " * On real AEN silicon the alp-shmem0 alias must point at a\n"
    " * reserved-memory carve-out shared between the M55-HP and M55-HE\n"
    " * images.  The orchestrator (scripts/alp_orchestrate.py) emits the\n"
    " * per-core overlay with the matching alias when the board.yaml v2\n"
)
_MULTICORE_MAILBOX_OVERLAY_ORCHESTRATOR_POINTER_EDITED = (
    " * On real AEN silicon the alp-shmem0 alias must point at a\n"
    " * reserved-memory carve-out shared between the M55-HP and M55-HE\n"
    " * images.  The orchestrator (alp-sdk's scripts/alp_orchestrate.py,\n"
    " * not part of this scaffolded project) emits the\n"
    " * per-core overlay with the matching alias when the board.yaml v2\n"
)


def un_edit_multicore_mailbox_overlay_orchestrator_pointer(text: str) -> str:
    """tan-cli#1009: reverse `multicore-mailbox`'s native_sim overlay's bare
    `scripts/alp_orchestrate.py` mention rewrite above to recover the
    emit's own (dead-pointer) bytes."""
    return text.replace(
        _MULTICORE_MAILBOX_OVERLAY_ORCHESTRATOR_POINTER_EDITED,
        _MULTICORE_MAILBOX_OVERLAY_ORCHESTRATOR_POINTER_EMITTED,
    )


#: tan-cli#1009: `multicore-mailbox`'s `prj.conf` names bare
#: `scripts/alp_project.py` (the reviewer's own
#: `multicore-mailbox/E1M-AEN801/prj.conf:5` finding, the second of finding
#: 1's two "different wording" instances) -- one SKU only.
_MULTICORE_MAILBOX_PRJ_CONF_WORKFLOW_POINTER_EMITTED = (
    "# Otherwise empty by design.  The build's effective Kconfig is\n"
    "# build/generated/alp.conf -- emitted from this app's board.yaml\n"
    "# by scripts/alp_project.py and layered over this file via\n"
    "# EXTRA_CONF_FILE (see CMakeLists.txt). board.yaml's `ipc:` block (kind:\n"
)
_MULTICORE_MAILBOX_PRJ_CONF_WORKFLOW_POINTER_EDITED = (
    "# Otherwise empty by design.  The build's effective Kconfig is\n"
    "# build/generated/alp.conf -- emitted from this app's board.yaml\n"
    "# by alp-sdk's scripts/alp_project.py (resolved via ALP_SDK_ROOT,\n"
    "# not part of this scaffolded project) and layered over this file\n"
    "# via EXTRA_CONF_FILE (see CMakeLists.txt). board.yaml's `ipc:` block (kind:\n"
)


def un_edit_multicore_mailbox_prj_conf_workflow_pointer(text: str) -> str:
    """tan-cli#1009: reverse `multicore-mailbox`'s `prj.conf` bare
    `scripts/alp_project.py` mention rewrite above to recover the emit's own
    (dead-pointer) bytes."""
    return text.replace(
        _MULTICORE_MAILBOX_PRJ_CONF_WORKFLOW_POINTER_EDITED,
        _MULTICORE_MAILBOX_PRJ_CONF_WORKFLOW_POINTER_EMITTED,
    )


#: tan-cli#1009 (PR #1009 round-three review, the eleventh sibling site):
#: `multicore-mailbox`'s `board.yaml` names alp-sdk's own
#: `src/backends/mproc/zephyr_drv.c` bare, three lines above the
#: `metadata/e1m_modules/E1M-AEN801.yaml` mention this same PR already
#: qualified in the same comment block. Worse than the other entries in
#: this file: this scaffold has its OWN `src/` (its `m55_hp` app), so an
#: unqualified `src/...` mention reads as a customer path.
_MULTICORE_MAILBOX_BOARD_YAML_ZEPHYR_DRV_POINTER_EMITTED = (
    "# hardware reasoning). `name: alp_shmem0` matches the DT-alias name\n"
    "# `src/backends/mproc/zephyr_drv.c` and both main.c's hardcode --\n"
    "# it is NOT a free-form label like an rpmsg channel name.\n"
)
_MULTICORE_MAILBOX_BOARD_YAML_ZEPHYR_DRV_POINTER_EDITED = (
    "# hardware reasoning). `name: alp_shmem0` matches the DT-alias name\n"
    "# alp-sdk's `src/backends/mproc/zephyr_drv.c` (a different `src/`\n"
    "# than this project's own -- not part of this scaffolded project)\n"
    "# and both main.c's hardcode -- it is NOT a free-form label like an\n"
    "# rpmsg channel name.\n"
)


def un_edit_multicore_mailbox_board_yaml_zephyr_drv_pointer(text: str) -> str:
    """tan-cli#1009: reverse `multicore-mailbox`'s `board.yaml` bare
    `src/backends/mproc/zephyr_drv.c` mention rewrite above to recover the
    emit's own (dead-pointer) bytes."""
    return text.replace(
        _MULTICORE_MAILBOX_BOARD_YAML_ZEPHYR_DRV_POINTER_EDITED,
        _MULTICORE_MAILBOX_BOARD_YAML_ZEPHYR_DRV_POINTER_EMITTED,
    )


#: tan-cli#1009 (PR #1009 round-three review): `multicore-mailbox`'s
#: `README.md` labels its own local `./peer/main.c` (the href, already
#: correct) with alp-sdk's upstream example path as the markdown LINK TEXT
#: -- byte-for-byte the same mislabel shape as `src/main.c`'s
#: `peer_main_pointer` entry above, one file over, left in place by the
#: round-two sweep.
_MULTICORE_MAILBOX_README_PEER_MAIN_LINK_POINTER_EMITTED = (
    "The peer-side firmware lives at\n"
    "[`examples/multicore/mproc-mailbox/peer/main.c`](peer/main.c) -- HE-side\n"
    "image that waits on the same mbox, reads the staged shmem\n"
    "payload, and writes back an echo via reverse send.\n"
)
_MULTICORE_MAILBOX_README_PEER_MAIN_LINK_POINTER_EDITED = (
    "The peer-side firmware lives at\n"
    "[`./peer/main.c`](peer/main.c) -- HE-side\n"
    "image that waits on the same mbox, reads the staged shmem\n"
    "payload, and writes back an echo via reverse send.  (alp-sdk's own\n"
    "upstream copy of this example lives at\n"
    "`examples/multicore/mproc-mailbox/peer/main.c`, not part of this\n"
    "scaffolded project.)\n"
)


def un_edit_multicore_mailbox_readme_peer_main_link_pointer(text: str) -> str:
    """tan-cli#1009: reverse `multicore-mailbox`'s `README.md` "peer-side
    firmware" link-label mislabel rewrite above to recover the emit's own
    (mislabeled) bytes."""
    return text.replace(
        _MULTICORE_MAILBOX_README_PEER_MAIN_LINK_POINTER_EDITED,
        _MULTICORE_MAILBOX_README_PEER_MAIN_LINK_POINTER_EMITTED,
    )


DELIBERATE_EDITS: dict[
    tuple[str, str, str, str], tuple[str, Callable[[str], str]]
] = {
    ("multicore-mailbox", "E1M-AEN801", "README.md", "blocked_caveat"): (
        "tan-cli#864: the scaffold's own alp_shmem0 carve-out resolves "
        "`status: blocked` on E1M-AEN801 (memory_map.base is TBD for "
        "'mram_main'), so the roundtrip it teaches compiles and does nothing "
        "-- the emit says so nowhere",
        un_edit_mailbox_blocked_caveat,
    ),
    ("iot", "E1M-AEN801", "CMakeLists.txt", "extra_conf_order"): (
        "tan-cli#379: list(PREPEND EXTRA_CONF_FILE ...) so a caller's own "
        "-DEXTRA_CONF_FILE=native_sim.conf wins over the generated alp.conf",
        un_edit_iot_extra_conf_order,
    ),
    ("iot", "E1M-AEN801", "README.md", "native_sim_conf_link"): (
        "tan-cli#1001 review (major): alp-sdk's own README links "
        "native_sim.conf via a self-referential ../mqtt-telemetry/ detour, "
        "which the doc-link rewriter renders as a GitHub blob link -- wrong "
        "here, native_sim.conf ships as a sibling of this README in every "
        "scaffold this template produces",
        un_edit_iot_aen801_readme_native_sim_conf_link,
    ),
    ("iot", "E1M-AEN801", "README.md", "native_sim_conf_copy_comment"): (
        "tan-cli#1001 review (major): the emit tells the customer "
        "native_sim.conf ships only in the alp-sdk tree and must be copied "
        "in -- false here, tan init --template iot-starter vendors and "
        "writes native_sim.conf itself (tan-cli#379, NON_ENVELOPE_EXTRAS)",
        un_edit_iot_aen801_readme_native_sim_copy_comment,
    ),
    # tan-cli#996/#1001 (the 722320a1 re-vendor): the dict went 31 -> 11
    # entries here, 20 retired, 0 added (tan-cli#1001 review correction --
    # an earlier revision of this comment said "eight entries retired" and
    # "21 total", both wrong; MANIFEST.md's "Twenty" was and is correct).
    # Named below: model_tests_pointers x2, model_comment_1/2 x4,
    # deepx_v2m102_scope, eeprom_script_pointer x2, i2c_scanner_bullet x2,
    # bringup_instruction x2, init_fail_comment x2, failure_modes_instruction
    # x2, hardware_paragraph x2 -- 20 total. (`deepx_v2m_note`'s README.md
    # half is NOT in this list any more -- tan-cli#1001 review found upstream
    # reintroduced the identical defect in new words on `E1M-AEN801` rather
    # than healing it, so that entry is RE-ANCHORED just below, not retired;
    # see `un_edit_edge_ai_aen801_readme_deepx_note`'s own comment above.)
    # Each of the 20 above stopped matching this re-vendor's fresh `--emit
    # scaffold` output verbatim because alp-sdk 722320a1 reworded the
    # surrounding prose, but NOT all for the same reason -- two distinct
    # classes, not an either/or, per entry (tan-cli#1001 review):
    #
    # - healed (the customer-facing gap the edit closed is now closed by
    #   alp-sdk's own prose, verified against the 722320a1 diff): the
    #   `sensor` template's own underlying example swapped chip, TMP112 ->
    #   BMP581, `examples/peripheral-io/i2c-master`'s alp-sdk#1269 fix
    #   (`bringup_instruction` x2, `failure_modes_instruction` x2,
    #   `i2c_scanner_bullet` x2); `edge-ai`'s cold-chain-monitor README/
    #   main.c gained real doc links and a units test pointer on its own
    #   (`model_tests_pointers` x2, `model_comment_1`/`model_comment_2` x4);
    #   `diagnostics`'s eeprom Troubleshooting bullet gained a real markdown
    #   link on its own (`eeprom_script_pointer` x2); `deepx_v2m102_scope`'s
    #   V2N101/V2M102 sentence was made SKU-neutral by alp-sdk#1749.
    # - anchor-gone, not healed (the underlying gap alp-sdk's prose does not
    #   close -- the specific matched string is simply gone, and
    #   reconstructing an equivalent correction against the new wording is
    #   out of scope for a pin-bump alone): `hardware_paragraph` x2
    #   (`sensor/*/src/main.c:14-17` -- the bare `metadata/chips/tmp112.yaml`
    #   referent the edit corrected is now a bare `metadata/boards/
    #   e1m-evk.yaml` + `e1m-x-evk.yaml`, same defect class, new path) and
    #   `init_fail_comment` x2 (`:110` -- upstream went bare-name ->
    #   bare-path, dropping the "alp-sdk's"/"not part of this scaffolded
    #   project" qualifiers the edit added).
    #
    # `un_edit_edge_ai_readme_model_tests_pointers`,
    # `un_edit_edge_ai_main_c_model_comment_1`,
    # `un_edit_edge_ai_main_c_model_comment_2`,
    # `un_edit_edge_ai_v2n101_readme_deepx_note`,
    # `un_edit_diagnostics_readme_eeprom_script`,
    # `un_edit_sensor_readme_i2c_scanner_bullet`,
    # `un_edit_sensor_main_c_bringup_instruction`,
    # `un_edit_sensor_main_c_init_fail_comment`,
    # `un_edit_sensor_main_c_failure_modes_instruction` and
    # `un_edit_sensor_main_c_hardware_paragraph` are kept as functions (their
    # constants document the transform for the next re-vendor that finds the
    # same class of gap) but are no longer referenced from this dict.
    # `pattern_paragraph` below is NOT in this list: its anchor (`sensor`'s
    # generic "Contrasts with... i2c-scanner" paragraph, which names no
    # chip) is untouched by the TMP112 -> BMP581 swap and still matches
    # verbatim, so it is reapplied forward, unchanged.
    ("edge-ai", "E1M-AEN801", "README.md", "deepx_v2m_note"): (
        "tan-cli#814, re-anchored tan-cli#1001 review (blocker): alp-sdk "
        "722320a1 did not heal this -- it reintroduced the identical defect "
        "in new words ('Pick either via som.sku in board.yaml', still wrong "
        "for E1M-AEN801's e1m-evk preset, which does not host the "
        "renesas-rzv2n-deepx family). Re-anchored on the new sentence "
        "rather than retired.",
        un_edit_edge_ai_aen801_readme_deepx_note,
    ),
    ("edge-ai", "E1M-AEN801", "board.yaml", "deepx_v2m_note"): (
        "tan-cli#814: same defect as the README entry above, the comment "
        "one file over that reinforces it. Both halves of this pair are "
        "live again after the tan-cli#1001 review re-anchor -- board.yaml "
        "is untouched by the 722320a1 re-vendor and this entry was never "
        "affected.",
        un_edit_edge_ai_aen801_board_yaml_deepx_note,
    ),
    ("diagnostics", "E1M-V2N101", "src/main.c", "som_sku_header"): (
        "tan-cli#932: src/main.c was never SKU-substituted at all -- the "
        "'what success looks like' heading still names E1M-AEN801",
        un_edit_main_c_v2n101_som_sku_header,
    ),
    ("diagnostics", "E1M-V2N101", "src/main.c", "som_sku_output_line"): (
        "tan-cli#932: same unsubstituted-SKU defect, the sample-output SoM "
        "identity line -- own entry from the header comment above and the "
        "serial fix on the same line, per the edit_id discipline",
        un_edit_main_c_v2n101_som_sku_output_line,
    ),
    ("diagnostics", "E1M-V2N101", "src/main.c", "serial_placeholder"): (
        "tan-cli#932: AEN0000123 is an AEN-shaped placeholder serial, not a "
        "value alp-sdk's own program_eeprom.py --serial format matches; "
        "swapped for the README's own <factory-serial> placeholder shape",
        un_edit_v2n101_serial_placeholder,
    ),
    ("diagnostics", "E1M-V2N101", "src/main.c", "soc_identity"): (
        "tan-cli#932: SoC identity: alif:ensemble:e8 is the Alif SoC ref on "
        "a Renesas RZ/V2N scaffold -- renesas:rzv2n:n44 is "
        "metadata/socs/renesas/rzv2n/n44.json's own 'ref' field for this "
        "SKU's silicon, not a hand-written string",
        un_edit_v2n101_soc_identity,
    ),
    ("diagnostics", "E1M-V2N101", "README.md", "serial_placeholder"): (
        "tan-cli#932: same AEN-shaped-placeholder defect as the src/main.c "
        "entry above; README.md's SoM SKU on this line was already correct, "
        "only the serial beside it was not",
        un_edit_v2n101_serial_placeholder,
    ),
    ("diagnostics", "E1M-V2N101", "README.md", "soc_identity"): (
        "tan-cli#932: same wrong-vendor SoC identity as the src/main.c entry "
        "above, both occurrences (real-hardware and native_sim blocks) -- "
        "one un_edit, since both are the same wrong token with the same fix",
        un_edit_v2n101_soc_identity,
    ),
    # tan-cli#501 review finding 1: a matching PREPEND was added to the four
    # `sensor`/`diagnostics` CMakeLists.txt files under the same
    # "board-specific conf must win" theory, but it does not apply here --
    # `boards/native_sim_native_64.conf` joins Zephyr's `CONF_FILE`, not
    # `EXTRA_CONF_FILE` (`configuration_files.cmake`'s board-dir auto-discovery),
    # and `merge_config_files` orders `CONF_FILE_AS_LIST` strictly before
    # `EXTRA_CONF_FILE_AS_LIST` regardless of PREPEND/APPEND within the latter
    # (`kconfig.cmake`). MEASURED with a real configure of a scaffolded project
    # against Zephyr v4.4.1 (the revision alp-sdk/west.yml pins): PREPEND and
    # APPEND produced the identical merge order and identical `.config`
    # (CONFIG_EMUL=y, CONFIG_I2C_EMUL=y either way). Reverted to plain APPEND,
    # which is what `--emit scaffold` produces unedited -- these four files
    # carry no deliberate edit at all now.
    ("sensor", "E1M-AEN801", "src/main.c", "hardware_boards_pointer"): (
        "tan-cli#977: the Hardware: paragraph's bare "
        "metadata/boards/e1m-evk.yaml/e1m-x-evk.yaml mentions, not emitted "
        "into any scaffolded project -- named the real alp-sdk paths in "
        "prose. Re-derived against dev's tan-cli#996/#1001 re-vendor "
        "(alp-sdk#1269: sensor's chip swapped TMP112 -> BMP581, and this "
        "paragraph's own bare referents changed with it)",
        un_edit_sensor_main_c_hardware_boards_pointer,
    ),
    ("sensor", "E1M-V2N101", "src/main.c", "hardware_boards_pointer"): (
        "tan-cli#977: the Hardware: paragraph's bare "
        "metadata/boards/e1m-evk.yaml/e1m-x-evk.yaml mentions, same "
        "substitution as sensor/E1M-AEN801/src/main.c's "
        "hardware_boards_pointer entry above (own reason string per PR "
        "#1009 review finding 2)",
        un_edit_sensor_main_c_hardware_boards_pointer,
    ),
    ("sensor", "E1M-AEN801", "src/main.c", "addr_metadata_pointer"): (
        "tan-cli#977: the BMP581_ADDR_7BIT doc-comment's bare "
        "metadata/boards/e1m-evk.yaml/e1m-x-evk.yaml mention, a different "
        "comment block than the hardware_boards_pointer fix above -- named "
        "the real alp-sdk paths in prose. Re-derived against dev's "
        "tan-cli#996/#1001 re-vendor (this doc-comment is the BMP581-era "
        "descendant of the retired entry 12's TMP112_ADDR_7BIT anchor)",
        un_edit_sensor_main_c_addr_metadata_pointer,
    ),
    ("sensor", "E1M-V2N101", "src/main.c", "addr_metadata_pointer"): (
        "tan-cli#977: the BMP581_ADDR_7BIT doc-comment's bare "
        "metadata/boards/e1m-evk.yaml/e1m-x-evk.yaml mention, same "
        "substitution as sensor/E1M-AEN801/src/main.c's "
        "addr_metadata_pointer entry above (own reason string per PR "
        "#1009 review finding 2)",
        un_edit_sensor_main_c_addr_metadata_pointer,
    ),
    ("sensor", "E1M-AEN801", "src/main.c", "addr_header_pointer"): (
        "tan-cli#977: the same doc-comment's bare "
        "include/alp/chips/bmp581.h mention, own entry from the "
        "metadata/boards/*.yaml fix above",
        un_edit_sensor_main_c_addr_header_pointer,
    ),
    ("sensor", "E1M-V2N101", "src/main.c", "addr_header_pointer"): (
        "tan-cli#977: the same doc-comment's bare include/alp/chips/"
        "bmp581.h mention, same substitution as sensor/E1M-AEN801/"
        "src/main.c's addr_header_pointer entry above (own reason string "
        "per PR #1009 review finding 2)",
        un_edit_sensor_main_c_addr_header_pointer,
    ),
    ("sensor", "E1M-AEN801", "board.yaml", "workflow_pointer"): (
        "tan-cli#977: the Customer workflow paragraph's bare "
        "scripts/alp_project.py mention -- real build machinery (invoked "
        "via ALP_SDK_ROOT) but the file itself lives only in the alp-sdk "
        "checkout that resolves, never in this scaffolded tree",
        un_edit_sensor_board_yaml_workflow_pointer,
    ),
    ("sensor", "E1M-V2N101", "board.yaml", "workflow_pointer"): (
        "tan-cli#977: the Customer workflow paragraph's bare "
        "scripts/alp_project.py mention, same substitution as "
        "sensor/E1M-AEN801/board.yaml's workflow_pointer entry above (own "
        "reason string per PR #1009 review finding 2)",
        un_edit_sensor_board_yaml_workflow_pointer,
    ),
    ("sensor", "E1M-AEN801", "board.yaml", "metadata_pointer"): (
        "tan-cli#977: the chip-classification paragraph's bare "
        "metadata/chips/bmp581.yaml mention, not emitted into any "
        "scaffolded project. Re-derived against dev's tan-cli#996/#1001 "
        "re-vendor (alp-sdk#1269 chip rename, tmp112.yaml -> bmp581.yaml)",
        un_edit_sensor_board_yaml_metadata_pointer,
    ),
    ("sensor", "E1M-V2N101", "board.yaml", "metadata_pointer"): (
        "tan-cli#977: the chip-classification paragraph's bare "
        "metadata/chips/bmp581.yaml mention, same substitution as "
        "sensor/E1M-AEN801/board.yaml's metadata_pointer entry above (own "
        "reason string per PR #1009 review finding 2)",
        un_edit_sensor_board_yaml_metadata_pointer,
    ),
    ("sensor", "E1M-AEN801", "board.yaml", "portability_script_pointer"): (
        "tan-cli#977: the same paragraph's bare "
        "scripts/check_example_portability.py mention, own entry from the "
        "metadata/chips/bmp581.yaml fix above",
        un_edit_sensor_board_yaml_portability_script_pointer,
    ),
    ("sensor", "E1M-V2N101", "board.yaml", "portability_script_pointer"): (
        "tan-cli#977: the same paragraph's bare "
        "scripts/check_example_portability.py mention, same substitution "
        "as sensor/E1M-AEN801/board.yaml's portability_script_pointer "
        "entry above (own reason string per PR #1009 review finding 2)",
        un_edit_sensor_board_yaml_portability_script_pointer,
    ),
    ("sensor", "E1M-AEN801", "board.yaml", "historical_note_boards_pointer"): (
        "tan-cli#977, re-anchored tan-cli#1026 (alp-sdk#1866 corrected the "
        "surrounding BRD_I2C/LPI2C0 sentence, moving this entry's old "
        "anchor -- the underlying problem, bare metadata/boards/"
        "e1m-evk.yaml/e1m-x-evk.yaml i2c_devices: mentions, was untouched "
        "by that fix): the #1269 historical NOTE's bare "
        "metadata/boards/e1m-evk.yaml/e1m-x-evk.yaml i2c_devices: mentions "
        "-- the board.yaml-side sibling of src/main.c's "
        "hardware_boards_pointer entry",
        un_edit_sensor_board_yaml_historical_note_boards_pointer,
    ),
    ("sensor", "E1M-V2N101", "board.yaml", "historical_note_boards_pointer"): (
        "tan-cli#977, re-anchored tan-cli#1026: the #1269 historical NOTE's "
        "bare metadata/boards/e1m-evk.yaml/e1m-x-evk.yaml i2c_devices: "
        "mentions, same substitution as sensor/E1M-AEN801/board.yaml's "
        "historical_note_boards_pointer entry above (own reason string per "
        "PR #1009 review finding 2)",
        un_edit_sensor_board_yaml_historical_note_boards_pointer,
    ),
    ("sensor", "E1M-AEN801", "prj.conf", "workflow_pointer"): (
        "tan-cli#977: the same bare scripts/alp_project.py mention as "
        "board.yaml's Customer workflow paragraph, a different file",
        un_edit_sensor_prj_conf_workflow_pointer,
    ),
    ("sensor", "E1M-V2N101", "prj.conf", "workflow_pointer"): (
        "tan-cli#977: the bare scripts/alp_project.py mention in prj.conf, "
        "same substitution as sensor/E1M-AEN801/prj.conf's "
        "workflow_pointer entry above (own reason string per PR #1009 "
        "review finding 2)",
        un_edit_sensor_prj_conf_workflow_pointer,
    ),
    ("minimal", "E1M-AEN801", "README.md", "example_pointers"): (
        "tan-cli#977: the opening troubleshooting paragraph's bare "
        "`gpio-button-led`/`i2c-scanner` mentions, neither emitted into "
        "any scaffolded project -- turned into real links, entry 7's shape",
        un_edit_minimal_readme_example_pointers,
    ),
    ("minimal", "E1M-V2N101", "README.md", "example_pointers"): (
        "tan-cli#977: the opening troubleshooting paragraph's bare "
        "`gpio-button-led`/`i2c-scanner` mentions, same substitution as "
        "minimal/E1M-AEN801/README.md's example_pointers entry above (own "
        "reason string per PR #1009 review finding 2)",
        un_edit_minimal_readme_example_pointers,
    ),
    # tan-cli#1009 (PR #1009 review finding 1 -- the ten blocking sibling
    # sites -- plus the reviewer-vetted "beyond finding 1" sites the same
    # review comment named): the sibling sweep #977 stopped one template
    # short of. `minimal`/`diagnostics` board.yaml (4 entries, one shared
    # un_edit -- byte-identical substitution across all four), `minimal`
    # prj.conf (2), `diagnostics` prj.conf (2), `iot` board.yaml (1), `iot`
    # prj.conf (2, two independent paragraphs), `iot` src/main.c (2, two
    # independent doc-comments), `edge-ai` src/main.c (4, two independent
    # doc-comments shared across both SKUs), `multicore-mailbox` board.yaml
    # (1), `multicore-mailbox`'s native_sim overlay (1), `multicore-mailbox`
    # prj.conf (1), `multicore-mailbox` src/main.c (1). Twenty-one entries.
    ("minimal", "E1M-AEN801", "board.yaml", "workflow_pointer"): (
        "tan-cli#1009: the Customer workflow paragraph's bare "
        "scripts/alp_project.py mention, not emitted into any scaffolded "
        "project -- byte-identical substitution to sensor's own "
        "workflow_pointer entry (tan-cli#977), left un-swept in `minimal` "
        "even though this PR already edited minimal's README.md two files "
        "over (PR #1009 review finding 1)",
        un_edit_minimal_diagnostics_board_yaml_workflow_pointer,
    ),
    ("minimal", "E1M-V2N101", "board.yaml", "workflow_pointer"): (
        "tan-cli#1009: same substitution as minimal/E1M-AEN801/board.yaml's "
        "workflow_pointer entry above (own reason string per the "
        "tan-cli#977 review's finding 2 discipline)",
        un_edit_minimal_diagnostics_board_yaml_workflow_pointer,
    ),
    ("diagnostics", "E1M-AEN801", "board.yaml", "workflow_pointer"): (
        "tan-cli#1009: the Customer workflow paragraph's bare "
        "scripts/alp_project.py mention, not emitted into any scaffolded "
        "project -- byte-identical substitution to sensor's own "
        "workflow_pointer entry (tan-cli#977), left un-swept in "
        "`diagnostics` even though tan-cli#977's own title names this "
        "template (PR #1009 review finding 1)",
        un_edit_minimal_diagnostics_board_yaml_workflow_pointer,
    ),
    ("diagnostics", "E1M-V2N101", "board.yaml", "workflow_pointer"): (
        "tan-cli#1009: same substitution as "
        "diagnostics/E1M-AEN801/board.yaml's workflow_pointer entry above "
        "(own reason string per the tan-cli#977 review's finding 2 "
        "discipline)",
        un_edit_minimal_diagnostics_board_yaml_workflow_pointer,
    ),
    ("minimal", "E1M-AEN801", "prj.conf", "workflow_pointer"): (
        "tan-cli#1009: minimal's prj.conf bare scripts/alp_project.py "
        "mention, the prj.conf-side sibling of board.yaml's "
        "workflow_pointer entry above, a different file (PR #1009 review "
        "finding 1)",
        un_edit_minimal_prj_conf_workflow_pointer,
    ),
    ("minimal", "E1M-V2N101", "prj.conf", "workflow_pointer"): (
        "tan-cli#1009: same substitution as minimal/E1M-AEN801/prj.conf's "
        "workflow_pointer entry above (own reason string per the "
        "tan-cli#977 review's finding 2 discipline)",
        un_edit_minimal_prj_conf_workflow_pointer,
    ),
    ("diagnostics", "E1M-AEN801", "prj.conf", "workflow_pointer"): (
        "tan-cli#1009: diagnostics's prj.conf bare scripts/alp_project.py "
        "mention, the prj.conf-side sibling of board.yaml's "
        "workflow_pointer entry above, a different file (PR #1009 review "
        "finding 1)",
        un_edit_diagnostics_prj_conf_workflow_pointer,
    ),
    ("diagnostics", "E1M-V2N101", "prj.conf", "workflow_pointer"): (
        "tan-cli#1009: same substitution as "
        "diagnostics/E1M-AEN801/prj.conf's workflow_pointer entry above "
        "(own reason string per the tan-cli#977 review's finding 2 "
        "discipline)",
        un_edit_diagnostics_prj_conf_workflow_pointer,
    ),
    ("iot", "E1M-AEN801", "prj.conf", "workflow_pointer"): (
        "tan-cli#1009: iot's own bare scripts/alp_project.py mention, one "
        "of finding 1's two named 'different wording' sites "
        "(iot/E1M-AEN801/prj.conf:4 in the review's line numbering)",
        un_edit_iot_prj_conf_workflow_pointer,
    ),
    ("iot", "E1M-AEN801", "prj.conf", "fleet_ota_pointer"): (
        "tan-cli#1009: iot's prj.conf migration-note bare "
        "examples/connectivity/iot-fleet-ota/prj.conf mention -- the "
        "reviewer's own iot/E1M-AEN801/prj.conf:18 finding, own entry from "
        "the workflow_pointer fix above, a different paragraph",
        un_edit_iot_prj_conf_fleet_ota_pointer,
    ),
    ("multicore-mailbox", "E1M-AEN801", "board.yaml", "e1m_modules_pointer"): (
        "tan-cli#1009: multicore-mailbox's board.yaml bare "
        "metadata/e1m_modules/E1M-AEN801.yaml mention -- the reviewer's own "
        "multicore-mailbox/E1M-AEN801/board.yaml:30 finding, one SKU only "
        "(multicore-mailbox ships E1M-AEN801 only)",
        un_edit_multicore_mailbox_board_yaml_e1m_modules_pointer,
    ),
    (
        "multicore-mailbox", "E1M-AEN801",
        "boards/native_sim_native_64.overlay", "orchestrator_pointer",
    ): (
        "tan-cli#1009: multicore-mailbox's native_sim overlay's bare "
        "scripts/alp_orchestrate.py mention -- the reviewer's own "
        "multicore-mailbox/E1M-AEN801/boards/native_sim_native_64.overlay:14 "
        "finding; a NON_ENVELOPE_EXTRAS file, diffed against the catalog "
        "example's own copy, same declaration mechanism",
        un_edit_multicore_mailbox_overlay_orchestrator_pointer,
    ),
    ("multicore-mailbox", "E1M-AEN801", "prj.conf", "workflow_pointer"): (
        "tan-cli#1009: multicore-mailbox's own bare scripts/alp_project.py "
        "mention, the second of finding 1's two named 'different wording' "
        "sites (multicore-mailbox/E1M-AEN801/prj.conf:5 in the review's "
        "line numbering)",
        un_edit_multicore_mailbox_prj_conf_workflow_pointer,
    ),
    # tan-cli#1009 round-three review: the eleventh sibling site the
    # round-two sweep missed, plus two scope-outs from round two that did
    # not hold on closer inspection. Three entries.
    ("multicore-mailbox", "E1M-AEN801", "board.yaml", "zephyr_drv_pointer"): (
        "tan-cli#1009: multicore-mailbox's board.yaml bare "
        "src/backends/mproc/zephyr_drv.c mention, three lines above the "
        "e1m_modules_pointer entry above in the same comment block -- "
        "worse than that one, this scaffold has its own src/, so the bare "
        "mention reads as a customer path (PR #1009 round-three review)",
        un_edit_multicore_mailbox_board_yaml_zephyr_drv_pointer,
    ),
    ("multicore-mailbox", "E1M-AEN801", "README.md", "peer_main_link_pointer"): (
        "tan-cli#1009: multicore-mailbox's README.md 'peer-side firmware' "
        "link labelled its own local ./peer/main.c with alp-sdk's upstream "
        "example path -- byte-for-byte the same mislabel shape as "
        "src/main.c's peer_main_pointer entry above, one file over, missed "
        "by the round-two sweep (PR #1009 round-three review)",
        un_edit_multicore_mailbox_readme_peer_main_link_pointer,
    ),
}


class ScaffoldEmitError(RuntimeError):
    """Raised for a live SDK emit failure (not a byte diff -- diffs are reported)."""


def discover_vendored_matrix(vendored_root: Path) -> list[tuple[str, str]]:
    """Scan `vendored_root` for `<template>/<sku>/` pairs, sorted."""
    pairs = []
    for template_dir in sorted(p for p in vendored_root.iterdir() if p.is_dir()):
        for sku_dir in sorted(p for p in template_dir.iterdir() if p.is_dir()):
            pairs.append((template_dir.name, sku_dir.name))
    return pairs


def emit_live_scaffold(sdk_root: Path, template: str, sku: str) -> dict[str, str]:
    """Run the live SDK scaffold emit; return {relative_path: contents}."""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(sdk_root / "scripts")
    proc = subprocess.run(
        [sys.executable, "scripts/alp_project.py", "--emit", "scaffold",
         "--template", template, "--sku", sku],
        cwd=sdk_root, env=env, capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise ScaffoldEmitError(
            f"emit failed for template={template!r} sku={sku!r} "
            f"(exit {proc.returncode}): {proc.stderr.strip()}")
    try:
        envelope = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise ScaffoldEmitError(
            f"emit for template={template!r} sku={sku!r} did not produce "
            f"valid JSON: {e}") from e
    return {f["path"]: f["contents"] for f in envelope}


def resolve_example_dir(sdk_root: Path, template: str) -> Path | None:
    """The scaffold catalog's `example:` directory for `template` id --
    where `NON_ENVELOPE_EXTRAS` are compared against instead of the scaffold
    envelope. `None` if the catalog or the template entry can't be read (the
    caller then leaves any such extras undiffed rather than erroring)."""
    catalog_path = sdk_root / "metadata" / "templates" / "catalog-v1.json"
    try:
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    for entry in catalog.get("templates", []):
        if entry.get("id") == template:
            example = entry.get("example")
            return (sdk_root / example) if example else None
    return None


def augment_with_example_extras(
    live: dict[str, str], sdk_root: Path, template: str, vendored_paths: Iterable[str],
) -> None:
    """For any `NON_ENVELOPE_EXTRAS` path present in the vendored tree, read
    its live content from the catalog's example directory and add it to
    `live` in place -- so `diff_trees` compares it same as every other file,
    against its real source instead of flagging it as a spurious
    vendored-only diff."""
    example_dir = resolve_example_dir(sdk_root, template)
    if example_dir is None:
        return
    for name in NON_ENVELOPE_EXTRAS:
        if name in vendored_paths and name not in live:
            extra_path = example_dir / name
            if extra_path.is_file():
                live[name] = extra_path.read_text(encoding="utf-8")


#: `(template, sku, path)` for a `NON_ENVELOPE_EXTRAS` file the live example
#: ships that a vendored tree deliberately does NOT carry -- declared, not
#: silently invisible, so `missing_extras` below has an auditable list rather
#: than reproducing the exact blind spot it exists to close (tan-cli#501
#: review finding 4: `augment_with_example_extras` only ever reaches for a
#: name the vendored tree ALREADY has, so a vendored tree missing an extra
#: entirely was never compared against anything and this gate stayed 9/9 PASS
#: with the fix fully reverted -- measured).
#:
#: `edge-ai`'s `boards/native_sim_native_64.{conf,overlay}` gap is the one
#: entry here today: `examples/ai/cold-chain-monitor` ships the pair, but
#: `cold_chain.c` hits the same synthetic-data `LOG_WRN` branch whether the
#: I2C alias resolves or not, so the native_sim output is unaffected (see
#: MANIFEST.md and this module's own docstring). Not independently
#: re-measured here (would need a real Zephyr build); declared so a future
#: drift in that reasoning has one place to update, not a silent pass.
DELIBERATELY_MISSING_EXTRAS: frozenset[tuple[str, str, str]] = frozenset({
    ("edge-ai", "E1M-AEN801", "boards/native_sim_native_64.conf"),
    ("edge-ai", "E1M-AEN801", "boards/native_sim_native_64.overlay"),
    ("edge-ai", "E1M-V2N101", "boards/native_sim_native_64.conf"),
    ("edge-ai", "E1M-V2N101", "boards/native_sim_native_64.overlay"),
})


def missing_extras(
    sdk_root: Path, template: str, sku: str, vendored_paths: Iterable[str],
) -> list[str]:
    """`NON_ENVELOPE_EXTRAS` the live catalog example ships that the vendored
    tree does not -- the completeness half `augment_with_example_extras`
    cannot provide, since it only ever reaches for a name the vendored tree
    already carries. Anything not declared in `DELIBERATELY_MISSING_EXTRAS`
    is a real gap and a hard failure, matching `undo_declared_edits`'s own
    strict discipline: a declaration excuses exactly the gap it names, and an
    undeclared one cannot pass silently."""
    example_dir = resolve_example_dir(sdk_root, template)
    if example_dir is None:
        return []
    gaps = []
    vendored_paths = set(vendored_paths)
    for name in NON_ENVELOPE_EXTRAS:
        if name in vendored_paths:
            continue
        if (template, sku, name) in DELIBERATELY_MISSING_EXTRAS:
            continue
        if (example_dir / name).is_file():
            gaps.append(
                f"{name}: live example ships this but the vendored tree does not, "
                f"and it is not declared in DELIBERATELY_MISSING_EXTRAS"
            )
    return gaps


def read_vendored_tree(tree_root: Path) -> dict[str, str]:
    """Read every file under `tree_root` into {relative_path: contents},
    forward-slash normalized, matching the emit envelope's path style."""
    files = {}
    for path in sorted(p for p in tree_root.rglob("*") if p.is_file()):
        rel = path.relative_to(tree_root).as_posix()
        files[rel] = path.read_text(encoding="utf-8")
    return files


def diff_trees(vendored: dict[str, str], live: dict[str, str]) -> list[str]:
    """Return a list of human-readable diff lines; empty iff byte-identical."""
    diffs = []
    for path in sorted(set(vendored) | set(live)):
        if path not in live:
            diffs.append(f"{path}: vendored only (missing from live emit)")
        elif path not in vendored:
            diffs.append(f"{path}: live only (missing from vendored tree)")
        elif vendored[path] != live[path]:
            diffs.append(f"{path}: content differs")
    return diffs


def undo_declared_edits(
    template: str, sku: str, vendored: dict[str, str]
) -> tuple[dict[str, str], list[str]]:
    """Reverse this `(template, sku)`'s `DELIBERATE_EDITS`, so the result is
    what the live emit should produce byte-for-byte.

    Returns that tree plus the declaration failures: an entry whose file is not
    in the tree at all, and -- the strict half -- an entry that found nothing to
    undo. Both are hard failures for the caller, not notes."""
    as_emitted = dict(vendored)
    failures = []
    for (declared_template, declared_sku, path, _edit_id), (reason, un_edit) in (
        DELIBERATE_EDITS.items()
    ):
        if (declared_template, declared_sku) != (template, sku):
            continue
        before = as_emitted.get(path)
        if before is None:
            failures.append(
                f"{path}: DELIBERATE_EDITS declares an edit here, but the vendored "
                f"tree has no such file ({reason})"
            )
            continue
        after = un_edit(before)
        if after == before:
            failures.append(
                f"{path}: DELIBERATE_EDITS declares an edit that is no longer there "
                f"-- re-vendored, or reworded past its own matcher. Drop or update "
                f"the entry (and MANIFEST.md's 'Deliberate edits' section); leaving "
                f"it excuses the next real drift in this file ({reason})"
            )
            continue
        as_emitted[path] = after
    return as_emitted, failures


def self_check() -> None:
    """Prove the declaration mechanism on every invocation, SDK bound or not.

    A run whose declarations all still apply never exercises the strict half,
    and a run with no reachable SDK exercises none of it -- which is how the
    gate could rot into a path-level allow-list without anything going red.
    Asserts here rather than a `tests/parity/test_*.py` file because repo-root
    parity tests only run where `.github/workflows/parity.yml` names them one
    by one, and this script is already named there."""
    link = "https://github.com/alplabai/alp-sdk/blob/{}/docs/x.md"
    assert un_edit_doc_link_ref(link.format("v0.15.0-rc1")) == link.format("v0.15.0")
    assert (
        un_edit_iot_extra_conf_order(
            "# why\n# more why\nlist(PREPEND EXTRA_CONF_FILE ${_alp_generated})\n"
        )
        == "list(APPEND EXTRA_CONF_FILE ${_alp_generated})\n"
    )

    template, sku, path = "iot", "E1M-AEN801", "CMakeLists.txt"
    edit_id = "extra_conf_order"
    emitted = "list(APPEND EXTRA_CONF_FILE ${_alp_generated})\n"
    assert (template, sku, path, edit_id) in DELIBERATE_EDITS

    # A declared edit that is no longer there FAILS instead of passing quietly
    # -- the `xfail(strict=True)` half. Feed the already-emitted bytes: a
    # path-level allow-list would call this clean and keep the dead entry.
    _, failures = undo_declared_edits(template, sku, {path: emitted})
    assert [f for f in failures if f.startswith(f"{path}: DELIBERATE_EDITS declares an edit that is no longer")], failures

    # ...and a declaration excuses ONLY the edit it describes: an unrelated
    # change in the same file survives the un-edit and reaches `diff_trees`.
    drifted = "stray line\n# why\nlist(PREPEND EXTRA_CONF_FILE ${_alp_generated})\n"
    as_emitted, failures = undo_declared_edits(template, sku, {path: drifted})
    assert not [f for f in failures if f.startswith(f"{path}:")], failures
    assert diff_trees({path: as_emitted[path]}, {path: emitted}) == [f"{path}: content differs"]

    # Two independent substitutions in one file get two ENTRIES (tan-cli#908
    # review, entry 3): healing only one must fail on its own, not hide behind
    # the other one still matching. Before the split, a single combined
    # un_edit's `after == before` check ran on the AGGREGATE of both
    # substitutions, so healing comment 1 alone while comment 2 stayed edited
    # produced `after != before` (comment 2 still changed something) and no
    # failure fired at all -- measured against the pre-split function.
    #
    # The original fixture for this discipline was `edge-ai`'s
    # `model_comment_1`/`model_comment_2` pair, retired at tan-cli#996/#1001
    # (see the DELIBERATE_EDITS block comment) -- both functions still exist
    # (`un_edit_edge_ai_main_c_model_comment_1`/`_2`) but neither is
    # registered any more, so they can no longer exercise this discipline
    # through `undo_declared_edits`. The discipline itself stays covered by
    # `diagnostics`/E1M-V2N101's four-entries-one-path `src/main.c` set
    # below (reason-string-filtered, so a partial heal there would be caught
    # the same way).

    # tan-cli#814's board.yaml/README.md pair (both live again after the
    # tan-cli#1001 review re-anchor -- see the DELIBERATE_EDITS block
    # comment): each un_edit must round-trip the corrected text back onto
    # the emit's own (still-wrong-for-E1M-AEN801) sentence, and be
    # registered under the exact (template, sku, path, edit_id) key.
    assert ("edge-ai", "E1M-AEN801", "board.yaml", "deepx_v2m_note") in DELIBERATE_EDITS
    assert (
        un_edit_edge_ai_aen801_board_yaml_deepx_note(_EDGE_AI_AEN801_BOARD_YAML_DEEPX_NOTE)
        == _EDGE_AI_AEN801_BOARD_YAML_DEEPX_NOTE_EMITTED
    )
    assert ("edge-ai", "E1M-AEN801", "README.md", "deepx_v2m_note") in DELIBERATE_EDITS
    assert (
        un_edit_edge_ai_aen801_readme_deepx_note(_EDGE_AI_AEN801_README_DEEPX_NOTE)
        == _EDGE_AI_AEN801_README_DEEPX_NOTE_EMITTED
    )

    # tan-cli#924's LAST entry (`pattern_paragraph`) retired at tan-cli#1151
    # (the ff27f179 re-vendor) -- its four siblings
    # (`hardware_paragraph`/`bringup_instruction`/`init_fail_comment`/
    # `failure_modes_instruction`) had already gone at tan-cli#996/#1001, so
    # tan-cli#924 now has no live entry and no round-trip block here. Same
    # reason as the other sixteen this re-vendor retired: alp-sdk#1855 makes
    # the emit itself rewrite the bare `examples/peripheral-io/i2c-scanner`
    # referent this edit existed to qualify, so the vendored bytes are now
    # the emit's own. See the DELIBERATE_EDITS block comment for the list.
    #
    # THE DISCIPLINE THIS BLOCK CARRIED IS NOT LOST, and that is checked
    # rather than asserted: `pattern_paragraph` was also the fixture for the
    # STRICT half -- feeding an un_edit its own already-emitted bytes must
    # make `undo_declared_edits` report a hard failure, not pass quietly.
    # That half is still exercised below by `diagnostics`/E1M-V2N101's
    # six-entry set, which is reason-string-filtered (so a partial heal is
    # still caught) and is the same place the discipline moved to when
    # `edge-ai`'s `model_comment_1`/`_2` pair was retired at
    # tan-cli#996/#1001 -- see the note at the top of this function. Deleting
    # the diagnostics set without providing another strict-half fixture
    # would leave `undo_declared_edits`' failure path uncovered.

    # tan-cli#932's six entries (diagnostics/E1M-V2N101 only): each un_edit
    # must round-trip the corrected src/main.c or README.md bytes back onto
    # the emit's own (still-wrong, AEN801/Alif-named) bytes, and be
    # registered under the exact (template, sku, path, edit_id) key.
    for edit_id in (
        "som_sku_header", "som_sku_output_line", "serial_placeholder", "soc_identity",
    ):
        assert ("diagnostics", "E1M-V2N101", "src/main.c", edit_id) in DELIBERATE_EDITS
    for edit_id in ("serial_placeholder", "soc_identity"):
        assert ("diagnostics", "E1M-V2N101", "README.md", edit_id) in DELIBERATE_EDITS

    assert (
        un_edit_main_c_v2n101_som_sku_header(_MAIN_C_V2N101_SOM_SKU_HEADER_EDITED)
        == _MAIN_C_V2N101_SOM_SKU_HEADER_EMITTED
    )
    assert (
        un_edit_main_c_v2n101_som_sku_output_line(
            _MAIN_C_V2N101_SOM_SKU_OUTPUT_LINE_EDITED
        )
        == _MAIN_C_V2N101_SOM_SKU_OUTPUT_LINE_EMITTED
    )
    assert (
        un_edit_v2n101_serial_placeholder("sn <factory-serial> -> PASS")
        == "sn AEN0000123 -> PASS"
    )
    assert (
        un_edit_v2n101_soc_identity(
            "SoC identity: renesas:rzv2n:n44 (secure-fw OK) -> PASS\n"
            "SoC identity: renesas:rzv2n:n44 (ping ALP_ERR_NOSUPPORT, "
            "read ALP_ERR_NOSUPPORT) -> SKIP"
        )
        == "SoC identity: alif:ensemble:e8 (secure-fw OK) -> PASS\n"
        "SoC identity: alif:ensemble:e8 (ping ALP_ERR_NOSUPPORT, "
        "read ALP_ERR_NOSUPPORT) -> SKIP"
    )

    # ...and each is the strict half too: feeding the un_edit its OWN RAW
    # emitted (AEN801/Alif-named, never-corrected) bytes finds nothing to
    # undo for tan-cli#932's entries -- each `_EMITTED` constant/literal
    # below is the pre-fix form, not the vendored one. Both `src/main.c`
    # (four registered entries) and `README.md` (three, counting
    # `eeprom_script_pointer`) carry more than one entry per path, so a
    # generic-prefix match alone is satisfied by any of the OTHER entries
    # failing regardless of whether the entry under test actually failed --
    # filter on the entry's OWN reason string, which `undo_declared_edits`
    # embeds verbatim as `({reason})`, to prove THIS entry produced a
    # failure, not merely that some failure occurred.
    for path, edit_id, emitted in (
        ("src/main.c", "som_sku_header", _MAIN_C_V2N101_SOM_SKU_HEADER_EMITTED),
        ("src/main.c", "som_sku_output_line",
         _MAIN_C_V2N101_SOM_SKU_OUTPUT_LINE_EMITTED + " AEN0000123 -> PASS\n"),
        ("src/main.c", "serial_placeholder", "sn AEN0000123 -> PASS\n"),
        ("src/main.c", "soc_identity", "SoC identity: alif:ensemble:e8\n"),
        ("README.md", "serial_placeholder", "sn AEN0000123 -> PASS\n"),
        ("README.md", "soc_identity", "SoC identity: alif:ensemble:e8\n"),
    ):
        _, mut_failures = undo_declared_edits(
            "diagnostics", "E1M-V2N101", {path: emitted}
        )
        reason, _ = DELIBERATE_EDITS[("diagnostics", "E1M-V2N101", path, edit_id)]
        assert [
            f for f in mut_failures
            if f.startswith(f"{path}: DELIBERATE_EDITS declares an edit that is no longer")
            and f.endswith(f"({reason})")
        ], ("diagnostics", "E1M-V2N101", path, edit_id, mut_failures)

    # tan-cli#1009 (PR #1009 review finding 3: zero self_check coverage for
    # any of #977's new entries -- and by extension, none for these 21
    # follow-on entries either, unless added here in the same change): each
    # `un_edit` must round-trip the corrected text back onto the emit's own
    # (still-bare) bytes, and each entry's OWN reason string, not a
    # sibling's, must be the one `undo_declared_edits` names when that
    # entry's substitution is fed back in still-unfixed. Round trips first
    # (the loose half -- `un_edit(EDITED) == EMITTED`), one per distinct
    # substitution/constant pair (shared un_edits tested once, not once per
    # registered key):
    for un_edit, edited, emitted in (
        (
            un_edit_minimal_diagnostics_board_yaml_workflow_pointer,
            _MINIMAL_DIAGNOSTICS_BOARD_YAML_WORKFLOW_POINTER_EDITED,
            _MINIMAL_DIAGNOSTICS_BOARD_YAML_WORKFLOW_POINTER_EMITTED,
        ),
        (
            un_edit_minimal_prj_conf_workflow_pointer,
            _MINIMAL_PRJ_CONF_WORKFLOW_POINTER_EDITED,
            _MINIMAL_PRJ_CONF_WORKFLOW_POINTER_EMITTED,
        ),
        (
            un_edit_diagnostics_prj_conf_workflow_pointer,
            _DIAGNOSTICS_PRJ_CONF_WORKFLOW_POINTER_EDITED,
            _DIAGNOSTICS_PRJ_CONF_WORKFLOW_POINTER_EMITTED,
        ),
        (
            un_edit_iot_prj_conf_workflow_pointer,
            _IOT_PRJ_CONF_WORKFLOW_POINTER_EDITED,
            _IOT_PRJ_CONF_WORKFLOW_POINTER_EMITTED,
        ),
        (
            un_edit_iot_prj_conf_fleet_ota_pointer,
            _IOT_PRJ_CONF_FLEET_OTA_POINTER_EDITED,
            _IOT_PRJ_CONF_FLEET_OTA_POINTER_EMITTED,
        ),
        (
            un_edit_multicore_mailbox_board_yaml_e1m_modules_pointer,
            _MULTICORE_MAILBOX_BOARD_YAML_E1M_MODULES_POINTER_EDITED,
            _MULTICORE_MAILBOX_BOARD_YAML_E1M_MODULES_POINTER_EMITTED,
        ),
        (
            un_edit_multicore_mailbox_overlay_orchestrator_pointer,
            _MULTICORE_MAILBOX_OVERLAY_ORCHESTRATOR_POINTER_EDITED,
            _MULTICORE_MAILBOX_OVERLAY_ORCHESTRATOR_POINTER_EMITTED,
        ),
        (
            un_edit_multicore_mailbox_prj_conf_workflow_pointer,
            _MULTICORE_MAILBOX_PRJ_CONF_WORKFLOW_POINTER_EDITED,
            _MULTICORE_MAILBOX_PRJ_CONF_WORKFLOW_POINTER_EMITTED,
        ),
    ):
        assert un_edit(edited) == emitted, un_edit

    # ...and the strict half, reason-filtered per entry (tan-cli#977 review
    # finding 2's own discipline, applied from the start this time): feeding
    # an entry's OWN emitted (still-bare) bytes must produce a failure that
    # ends with THAT entry's own `(reason)`, not a sibling's -- proven by
    # reverting each entry ALONE and checking the filtered failure fires.
    for template, sku, path, edit_id, emitted in (
        ("minimal", "E1M-AEN801", "board.yaml", "workflow_pointer",
         _MINIMAL_DIAGNOSTICS_BOARD_YAML_WORKFLOW_POINTER_EMITTED),
        ("minimal", "E1M-V2N101", "board.yaml", "workflow_pointer",
         _MINIMAL_DIAGNOSTICS_BOARD_YAML_WORKFLOW_POINTER_EMITTED),
        ("diagnostics", "E1M-AEN801", "board.yaml", "workflow_pointer",
         _MINIMAL_DIAGNOSTICS_BOARD_YAML_WORKFLOW_POINTER_EMITTED),
        ("diagnostics", "E1M-V2N101", "board.yaml", "workflow_pointer",
         _MINIMAL_DIAGNOSTICS_BOARD_YAML_WORKFLOW_POINTER_EMITTED),
        ("minimal", "E1M-AEN801", "prj.conf", "workflow_pointer",
         _MINIMAL_PRJ_CONF_WORKFLOW_POINTER_EMITTED),
        ("minimal", "E1M-V2N101", "prj.conf", "workflow_pointer",
         _MINIMAL_PRJ_CONF_WORKFLOW_POINTER_EMITTED),
        ("diagnostics", "E1M-AEN801", "prj.conf", "workflow_pointer",
         _DIAGNOSTICS_PRJ_CONF_WORKFLOW_POINTER_EMITTED),
        ("diagnostics", "E1M-V2N101", "prj.conf", "workflow_pointer",
         _DIAGNOSTICS_PRJ_CONF_WORKFLOW_POINTER_EMITTED),
        ("iot", "E1M-AEN801", "prj.conf", "workflow_pointer",
         _IOT_PRJ_CONF_WORKFLOW_POINTER_EMITTED),
        ("iot", "E1M-AEN801", "prj.conf", "fleet_ota_pointer",
         _IOT_PRJ_CONF_FLEET_OTA_POINTER_EMITTED),
        ("multicore-mailbox", "E1M-AEN801", "board.yaml",
         "e1m_modules_pointer",
         _MULTICORE_MAILBOX_BOARD_YAML_E1M_MODULES_POINTER_EMITTED),
        ("multicore-mailbox", "E1M-AEN801",
         "boards/native_sim_native_64.overlay", "orchestrator_pointer",
         _MULTICORE_MAILBOX_OVERLAY_ORCHESTRATOR_POINTER_EMITTED),
        ("multicore-mailbox", "E1M-AEN801", "prj.conf", "workflow_pointer",
         _MULTICORE_MAILBOX_PRJ_CONF_WORKFLOW_POINTER_EMITTED),
    ):
        _, mut_failures = undo_declared_edits(template, sku, {path: emitted})
        reason, _ = DELIBERATE_EDITS[(template, sku, path, edit_id)]
        assert [
            f for f in mut_failures
            if f.startswith(f"{path}: DELIBERATE_EDITS declares an edit that is no longer")
            and f.endswith(f"({reason})")
        ], (template, sku, path, edit_id, mut_failures)


    # tan-cli#1009 round-three review (major finding: self_check covered
    # #1009's own entries but none of #977's, and three pre-#977 entries had
    # NEVER had coverage at all -- blocked_caveat, native_sim_conf_link,
    # native_sim_conf_copy_comment). This block closes the gap: every
    # remaining DELIBERATE_EDITS entry not already exercised above (#977's
    # 24, the three long-uncovered pre-#977 entries, and round-three's own
    # three new entries) gets the same round-trip + reason-filtered
    # strict-half proof. DELIBERATE_EDITS now has 100% self_check coverage.
    #
    # `blocked_caveat` (multicore-mailbox's README.md) is the one entry in
    # this block whose `un_edit` is regex-`.sub`-based rather than a plain
    # `EDITED`/`EMITTED` constant pair (`_MAILBOX_BLOCKED_CAVEAT.sub("\n",
    # text)`), so its round-trip and strict-half use hand-built literal
    # strings shaped to match the regex instead of named constants.
    _blocked_caveat_edited = (
        "# mproc-mailbox\n\n"
        "## Before you run this: the channel is not allocated yet\n"
        "Some caveat body.\n"
    )
    _blocked_caveat_emitted = "# mproc-mailbox\n"
    assert (
        un_edit_mailbox_blocked_caveat(_blocked_caveat_edited)
        == _blocked_caveat_emitted
    )

    for un_edit, edited, emitted in (
        (
            un_edit_sensor_main_c_hardware_boards_pointer,
            _SENSOR_MAIN_C_HARDWARE_BOARDS_POINTER_EDITED,
            _SENSOR_MAIN_C_HARDWARE_BOARDS_POINTER_EMITTED,
        ),
        (
            un_edit_sensor_main_c_addr_metadata_pointer,
            _SENSOR_MAIN_C_ADDR_METADATA_POINTER_EDITED,
            _SENSOR_MAIN_C_ADDR_METADATA_POINTER_EMITTED,
        ),
        (
            un_edit_sensor_main_c_addr_header_pointer,
            _SENSOR_MAIN_C_ADDR_HEADER_POINTER_EDITED,
            _SENSOR_MAIN_C_ADDR_HEADER_POINTER_EMITTED,
        ),
        (
            un_edit_sensor_board_yaml_workflow_pointer,
            _SENSOR_BOARD_YAML_WORKFLOW_POINTER_EDITED,
            _SENSOR_BOARD_YAML_WORKFLOW_POINTER_EMITTED,
        ),
        (
            un_edit_sensor_board_yaml_metadata_pointer,
            _SENSOR_BOARD_YAML_METADATA_POINTER_EDITED,
            _SENSOR_BOARD_YAML_METADATA_POINTER_EMITTED,
        ),
        (
            un_edit_sensor_board_yaml_portability_script_pointer,
            _SENSOR_BOARD_YAML_PORTABILITY_SCRIPT_POINTER_EDITED,
            _SENSOR_BOARD_YAML_PORTABILITY_SCRIPT_POINTER_EMITTED,
        ),
        (
            un_edit_sensor_board_yaml_historical_note_boards_pointer,
            _SENSOR_BOARD_YAML_HISTORICAL_NOTE_BOARDS_POINTER_EDITED,
            _SENSOR_BOARD_YAML_HISTORICAL_NOTE_BOARDS_POINTER_EMITTED,
        ),
        (
            un_edit_sensor_prj_conf_workflow_pointer,
            _SENSOR_PRJ_CONF_WORKFLOW_POINTER_EDITED,
            _SENSOR_PRJ_CONF_WORKFLOW_POINTER_EMITTED,
        ),
        (
            un_edit_minimal_readme_example_pointers,
            _MINIMAL_README_EXAMPLE_POINTERS_EDITED,
            _MINIMAL_README_EXAMPLE_POINTERS_EMITTED,
        ),
        (
            un_edit_iot_aen801_readme_native_sim_conf_link,
            _IOT_AEN801_README_NATIVE_SIM_CONF_LINK_EDITED,
            _IOT_AEN801_README_NATIVE_SIM_CONF_LINK_EMITTED,
        ),
        (
            un_edit_iot_aen801_readme_native_sim_copy_comment,
            _IOT_AEN801_README_NATIVE_SIM_COPY_COMMENT_EDITED,
            _IOT_AEN801_README_NATIVE_SIM_COPY_COMMENT_EMITTED,
        ),
        (
            un_edit_multicore_mailbox_board_yaml_zephyr_drv_pointer,
            _MULTICORE_MAILBOX_BOARD_YAML_ZEPHYR_DRV_POINTER_EDITED,
            _MULTICORE_MAILBOX_BOARD_YAML_ZEPHYR_DRV_POINTER_EMITTED,
        ),
        (
            un_edit_multicore_mailbox_readme_peer_main_link_pointer,
            _MULTICORE_MAILBOX_README_PEER_MAIN_LINK_POINTER_EDITED,
            _MULTICORE_MAILBOX_README_PEER_MAIN_LINK_POINTER_EMITTED,
        ),
    ):
        assert un_edit(edited) == emitted, un_edit

    for template, sku, path, edit_id, emitted in (
        ("multicore-mailbox", "E1M-AEN801", "README.md", "blocked_caveat",
         _blocked_caveat_emitted),
        ("sensor", "E1M-AEN801", "src/main.c", "hardware_boards_pointer",
         _SENSOR_MAIN_C_HARDWARE_BOARDS_POINTER_EMITTED),
        ("sensor", "E1M-V2N101", "src/main.c", "hardware_boards_pointer",
         _SENSOR_MAIN_C_HARDWARE_BOARDS_POINTER_EMITTED),
        ("sensor", "E1M-AEN801", "src/main.c", "addr_metadata_pointer",
         _SENSOR_MAIN_C_ADDR_METADATA_POINTER_EMITTED),
        ("sensor", "E1M-V2N101", "src/main.c", "addr_metadata_pointer",
         _SENSOR_MAIN_C_ADDR_METADATA_POINTER_EMITTED),
        ("sensor", "E1M-AEN801", "src/main.c", "addr_header_pointer",
         _SENSOR_MAIN_C_ADDR_HEADER_POINTER_EMITTED),
        ("sensor", "E1M-V2N101", "src/main.c", "addr_header_pointer",
         _SENSOR_MAIN_C_ADDR_HEADER_POINTER_EMITTED),
        ("sensor", "E1M-AEN801", "board.yaml", "workflow_pointer",
         _SENSOR_BOARD_YAML_WORKFLOW_POINTER_EMITTED),
        ("sensor", "E1M-V2N101", "board.yaml", "workflow_pointer",
         _SENSOR_BOARD_YAML_WORKFLOW_POINTER_EMITTED),
        ("sensor", "E1M-AEN801", "board.yaml", "metadata_pointer",
         _SENSOR_BOARD_YAML_METADATA_POINTER_EMITTED),
        ("sensor", "E1M-V2N101", "board.yaml", "metadata_pointer",
         _SENSOR_BOARD_YAML_METADATA_POINTER_EMITTED),
        ("sensor", "E1M-AEN801", "board.yaml", "portability_script_pointer",
         _SENSOR_BOARD_YAML_PORTABILITY_SCRIPT_POINTER_EMITTED),
        ("sensor", "E1M-V2N101", "board.yaml", "portability_script_pointer",
         _SENSOR_BOARD_YAML_PORTABILITY_SCRIPT_POINTER_EMITTED),
        ("sensor", "E1M-AEN801", "board.yaml", "historical_note_boards_pointer",
         _SENSOR_BOARD_YAML_HISTORICAL_NOTE_BOARDS_POINTER_EMITTED),
        ("sensor", "E1M-V2N101", "board.yaml", "historical_note_boards_pointer",
         _SENSOR_BOARD_YAML_HISTORICAL_NOTE_BOARDS_POINTER_EMITTED),
        ("sensor", "E1M-AEN801", "prj.conf", "workflow_pointer",
         _SENSOR_PRJ_CONF_WORKFLOW_POINTER_EMITTED),
        ("sensor", "E1M-V2N101", "prj.conf", "workflow_pointer",
         _SENSOR_PRJ_CONF_WORKFLOW_POINTER_EMITTED),
        ("minimal", "E1M-AEN801", "README.md", "example_pointers",
         _MINIMAL_README_EXAMPLE_POINTERS_EMITTED),
        ("minimal", "E1M-V2N101", "README.md", "example_pointers",
         _MINIMAL_README_EXAMPLE_POINTERS_EMITTED),
        ("iot", "E1M-AEN801", "README.md", "native_sim_conf_link",
         _IOT_AEN801_README_NATIVE_SIM_CONF_LINK_EMITTED),
        ("iot", "E1M-AEN801", "README.md", "native_sim_conf_copy_comment",
         _IOT_AEN801_README_NATIVE_SIM_COPY_COMMENT_EMITTED),
        ("multicore-mailbox", "E1M-AEN801", "board.yaml", "zephyr_drv_pointer",
         _MULTICORE_MAILBOX_BOARD_YAML_ZEPHYR_DRV_POINTER_EMITTED),
        ("multicore-mailbox", "E1M-AEN801", "README.md", "peer_main_link_pointer",
         _MULTICORE_MAILBOX_README_PEER_MAIN_LINK_POINTER_EMITTED),
    ):
        _, mut_failures = undo_declared_edits(template, sku, {path: emitted})
        reason, _ = DELIBERATE_EDITS[(template, sku, path, edit_id)]
        assert [
            f for f in mut_failures
            if f.startswith(f"{path}: DELIBERATE_EDITS declares an edit that is no longer")
            and f.endswith(f"({reason})")
        ], (template, sku, path, edit_id, mut_failures)

    # Five entries had SOME self_check coverage before this round (an
    # `in DELIBERATE_EDITS` presence check plus a round-trip), but not the
    # reason-filtered strict half above. Only `pattern_paragraph` was
    # actually vacuity-exposed by that gap -- it shares `sensor/*/src/
    # main.c` with five #977 entries (`hardware_boards_pointer` and
    # friends), so its pre-existing un-filtered "any failure" assert could
    # in principle pass on one of THEIR failures rather than its own (the
    # exact class this round's reason-string-filter discipline exists to
    # catch). `deepx_v2m_note`'s pair and `extra_conf_order` are each the
    # only entry on their own path, so were never vacuity-exposed -- re-
    # proven here anyway, for uniformity, so all 62 entries carry the same
    # reason-filtered proof rather than two different proof shapes.
    for template, sku, path, edit_id, emitted in (
        ("edge-ai", "E1M-AEN801", "board.yaml", "deepx_v2m_note",
         _EDGE_AI_AEN801_BOARD_YAML_DEEPX_NOTE_EMITTED),
        ("edge-ai", "E1M-AEN801", "README.md", "deepx_v2m_note",
         _EDGE_AI_AEN801_README_DEEPX_NOTE_EMITTED),
        ("iot", "E1M-AEN801", "CMakeLists.txt", "extra_conf_order",
         "list(APPEND EXTRA_CONF_FILE ${_alp_generated})\n"),
    ):
        _, mut_failures = undo_declared_edits(template, sku, {path: emitted})
        reason, _ = DELIBERATE_EDITS[(template, sku, path, edit_id)]
        assert [
            f for f in mut_failures
            if f.startswith(f"{path}: DELIBERATE_EDITS declares an edit that is no longer")
            and f.endswith(f"({reason})")
        ], (template, sku, path, edit_id, mut_failures)


    # `missing_extras` needs a real SDK checkout (a live example directory) to
    # say anything -- `resolve_example_dir` returning `None` (no SDK bound) is
    # already covered by every call site tolerating an empty list. What is
    # provable without one: a declared gap is always a `(template, sku, path)`
    # this module's own `NON_ENVELOPE_EXTRAS`, so a typo in the declaration
    # can never silently stop excusing anything.
    for declared_template, declared_sku, declared_path in DELIBERATELY_MISSING_EXTRAS:
        assert declared_path in NON_ENVELOPE_EXTRAS, (
            declared_template, declared_sku, declared_path
        )


def run(sdk_root: Path, vendored_root: Path, pairs: list[tuple[str, str]]) -> bool:
    all_ok = True
    for template, sku in pairs:
        tree_root = vendored_root / template / sku
        vendored = read_vendored_tree(tree_root)
        try:
            live = emit_live_scaffold(sdk_root, template, sku)
        except ScaffoldEmitError as e:
            print(f"FAIL {template}/{sku}: {e}")
            all_ok = False
            continue
        augment_with_example_extras(live, sdk_root, template, vendored)
        as_emitted, declaration_failures = undo_declared_edits(template, sku, vendored)

        diffs = (
            declaration_failures
            + diff_trees(as_emitted, live)
            + missing_extras(sdk_root, template, sku, vendored)
        )
        if diffs:
            print(f"FAIL {template}/{sku}: {len(diffs)} diff(s)")
            for d in diffs:
                print(f"    {d}")
            all_ok = False
        else:
            print(f"PASS {template}/{sku} ({len(vendored)} files)")
    return all_ok


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sdk", type=Path, default=None,
                         help="Path to the alp-sdk checkout to emit live "
                              "scaffolds from. Falls back to $ALP_SDK_ROOT, "
                              "then an alp-sdk checkout next to this "
                              "tan-cli checkout.")
    parser.add_argument("--vendored", type=Path, default=VENDORED_ROOT,
                         help="Vendored tree root (default: "
                              "python/tan/templates/vendored/, the tree the "
                              "shipped Python tan reads).")
    args = parser.parse_args(argv)

    self_check()

    sdk_root, exit_code = sdk_root_or_exit_code(
        args.sdk,
        self_skip_message=(
            "SKIP: no alp-sdk checkout reachable (--sdk / $ALP_SDK_ROOT / "
            "a sibling alp-sdk checkout); scaffold byte-parity not checked "
            "this run."
        ),
    )
    if exit_code is not None:
        return exit_code

    vendored_root = args.vendored.resolve()
    pairs = discover_vendored_matrix(vendored_root)
    if not pairs:
        print(f"error: no vendored (template, sku) trees found under "
              f"{vendored_root}", file=sys.stderr)
        return 2

    ok = run(sdk_root, vendored_root, pairs)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
