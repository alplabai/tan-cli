# SPDX-License-Identifier: Apache-2.0
"""`tan explain` -- describe a project template, a module template, or a
generation target. tan's teaching surface: the only command whose OUTPUT is the
product, so its wording is contract in the same way an envelope key is.

Mirrors `crates/tan-cli/src/commands/explain.rs` plus the two `tan-core`
registries it reads (`wizard/service/registry.rs`'s template definitions,
`loader.rs`'s `GENERATION_TARGET_CATALOG`). `--template` explains an
init/scaffold template, `--target` a generation output target, neither prints an
overview, and BOTH is an error -- exit 1, like an unknown id.

**Why the catalogue is vendored here and not read from the SDK.** Weighed
against `alp-sdk/docs/superpowers/specs/2026-07-29-tan-port-invariants.md`:

* **I-32** is the governing one, and it is not close. `tan explain --template`
  describes what `tan init` will write, and `tan init` is SDK-FREE by design --
  it reads the vendored `--emit scaffold` tree "without ever shelling the SDK"
  so a fresh customer's first command works with no alp-sdk checkout anywhere.
  A description sourced from a checkout would go silent (or lie) on exactly the
  machine `tan init` is guaranteed to work on. Anti-pattern #22 in that doc names
  shelling the SDK for this content specifically.
* **I-26 / ADR-0017** (`metadata/**` stays in alp-sdk; tan learns no hardware
  fact) is what would otherwise pull the other way, as it does for
  `tan examples`. It does not apply: this prose is tan's own description of
  tan's own commands and tan's own emit outputs -- there is no `metadata/`
  source for "Minimal template keeps generated code intentionally small". The
  part-numbers inside the prose (TMP112, BME280, CC3501E, the two SKUs) enter no
  decision here; they are transcribed English, and every one already ships in
  tan via the vendored scaffold trees and `core/scaffold.py`'s
  `DEFAULT_SOM_SKU`/`TEMPLATE_SUPPORTED_SKUS`. Nothing in this file is keyed
  on a SKU, an address, or a pin.

The ONE derived line is "Default libraries", which reads each template's
vendored `board.yaml` rather than a registry field
(`scaffold.vendored_library_names_for`) -- tan-cli#124: the field is
deliberately blanked for a vendored template and reported "(none)" for
`edge-ai-starter`, whose scaffold declares `tflite-micro`.

**`data.som` (tan-cli#866): what `tan init` will accept/refuse for this
template, structured, not just in prose.** A project template's `--template`
hit carries `data.som.{initAcceptsSkus,initRefusesSkuPrefixes}`, computed by
`_som_support_data` from `scaffold.TEMPLATE_SUPPORTED_SKUS` /
`scaffold.UNSUPPORTED_SOM_FAMILY_PREFIXES` -- the SAME two tables `tan init`
already gates `init.invalid-som` / `init.som-unsupported` on, READ here
rather than retyped.

**These are `tan init` REFUSAL predictions, not a capability statement --
name them accordingly and do not rename them back.** (PR #985 review, major
1.) `TEMPLATE_SUPPORTED_SKUS` is exactly, and only, the set `init_cmd` checks
before writing a tree; it says nothing about which SKUs alp-sdk's own
scaffold catalog actually validates a template against. Measured: for every
template here except `iot-starter`/`multicore-mailbox`, alp-sdk's catalog
`supported.som_skus` is the narrow `["E1M-AEN801", "E1M-V2N101"]`
(`tan/templates/vendored/MANIFEST.md`), while `initRefusesSkuPrefixes` here
publishes only `["E1M-NX9"]` for those same four -- i.e. "every SoM except
NXP", 10 of 11 catalogued SKUs, not 2. That is not a bug in this field: tan
is SDK-free by design (I-32 above) and `_family_bucket`
(`tan.core.scaffold`) deliberately falls an unrecognised SKU prefix onto the
default (Alif) tree rather than refusing it, so `tan init --template
sensor-starter --som E1M-AEN301` really does exit 0 today. A field NAMED
"supported" would be lying about capability for those 4 of 7 templates; a
field that says "what tan init will accept" is exactly true, including for
a SKU that does not exist (`E1M-ZZZ999` also predicts `ok`, correctly, by
this policy). `python/tests/gates/test_no_new_hardware_facts.py` already
tracks the underlying gap as its largest acknowledged debt item, retiring
only once the SDK catalog's own per-template family mapping is readable from
`metadata/**` -- that debt is orthogonal to and not resolved by this field;
do not read `initAcceptsSkus`/`initRefusesSkuPrefixes` as "the SDK says this
combination works".

Before this PR, the only place a template's SoM restriction was written down
was inside `details[]` prose ("... (E1M-AEN801 only)"), which a consumer
would have had to parse and which could silently drift from what `tan init`
actually enforces -- #866 is named for exactly that string. The `iot-starter`
description's own parenthetical, and its explanation's Wi-Fi-transport
sentence, are now GENERATED from the same data (`_only_note`, `_iot_wifi_note`),
so neither can disagree with it by construction; see
`tests/commands/test_explain_command.py`'s
`test_every_sku_mentioned_in_template_prose_matches_its_structured_som_data`
for the drift gate over every OTHER SKU mention in project-template prose
(`multicore-mailbox`'s explanation, the one sentence this change did not also
make generated). Scoped to PROJECT templates only -- module
templates and generation targets carry no SoM concept, so `data.som` is
absent (not `null`) on those two selector kinds, the same absent-vs-null
convention `--code`'s `data.diagnostic`/`data.suggestions` already use.

**Every failure is a coded issue, never a traceback.** Ambiguous selector,
unknown template, unknown target, and an unreadable vendored tree each map to an
`explain.*` code and exit 1/5; the catch-all at the bottom is the backstop. This
command spawns no subprocess and opens at most one file (a vendored board.yaml,
only for `--template`), so there is nothing to time out. `available` is emitted
on the failure path too -- deliberately, so a caller that guessed wrong gets the
valid ids in the same envelope.

NOT PORTED, and honest about it: `--quiet` (Rust suppresses the `- <detail>`
lines with it). The Python port carries no `--quiet` on any command, and one
command-local copy would be a worse surface than a clean refusal -- passing it
is a Click usage error naming the unknown flag.

**`--code` is a THIRD selector, and the one mode that reads a checkout**
(ADR-0020 end-state B). `tan explain --code ALP-B003` resolves an
alp-sdk diagnostic / `ALP_ERR_*` code out of the bound checkout's
`metadata/error-catalog.json` -- the lookup half of alp-sdk's
`scripts/alp_cli/explain.py`, whose retirement this enables. A FLAG on this
verb, not a new command: alp-sdk spells it `alp explain <code>` too, so the
muscle memory transfers and the ambiguity guard already here extends to it.
Not the positional, which already means TEMPLATE -- sniffing "does this
argument look like a code?" is exactly the ambiguous-input class
`explain.positional-template-conflict` refuses.

The other three paths stay BYTE-IDENTICAL, deliberately: `--code` adds no
line to the overview and no key to `data.available` (the `explain-overview`
golden pins both, and `--help` already lists the flag), and
`data.diagnostic`/`data.suggestions` appear on the `--code` path ALONE -- the
extension reads a `data` key it does not find with a `?? []` fallback, so an
absent key is the honest shape for a mode that did not run. I-32 therefore
still holds where it was measured to matter: with no alp-sdk anywhere,
`tan init`'s templates are still explainable. With no checkout resolvable
`--code` REFUSES (`explain.sdk-root-unresolved`) rather than answering from a
vendored copy -- the code list is alp-sdk's fact (I-26/ADR-0017), and a baked
copy would answer confidently out of date.

DIVERGENCE from `alp_cli/explain.py`, measured by running both against the same
alp-sdk `origin/dev` worktree, and each one deliberate
(`faultdecode_cmd.py:14-26` is the precedent for documenting it here):

* **Field VALUES are verbatim; the frame around them is tan's.** `explain.py`
  writes `<CODE>  (<kind>)` with two spaces then `  <label>: <value>` per
  field; this writes `explain: <CODE> (<kind>)` then `- <label>: <value>`,
  because that is the ONE shape every `explain` selector renders in and a
  per-mode renderer would make two commands out of one. Same fields, same
  order, same omissions, same text.
* **stderr, not stdout.** `explain.py` `click.echo`s to stdout; every `tan`
  command's human text goes to stderr because stdout is the envelope channel.
* **`--format json` wraps; `explain.py --json` does not.** The raw entry
  `explain.py` printed as its whole document is `data.diagnostic` here,
  byte-for-byte, so the payload survives the wrapping intact -- the same
  divergence, for the same reason, as `tan faultdecode --format json`.
* **NOT PORTED: `--no-color`.** `explain.py` paints the header cyan and each
  label yellow; `tan explain` has never coloured any selector's output, so
  there is nothing for a `--no-color` to turn off. The flag still PARSES (it
  is one of the ten global flags) and is simply inert here, as on every other
  `tan` command that emits no colour.
* **The `doc:` value stays repo-relative** (`docs/diagnostics/ALP-B003.md`),
  exactly as the catalogue holds it. Joining it onto the resolved checkout
  would be a value alp-sdk never wrote; `sdk.root` in the same envelope is
  what a consumer joins it against.
* **The no-near-miss sentence is tan's own wording, not `explain.py`'s.**
  `explain.py` prints ``unknown code 'ZZZ' -- run `alp explain` against a
  code from metadata/error-catalog.json`` -- its own binary name and
  subcommand, which cannot be repeated verbatim from a `tan`-only install;
  this prints `` explain: unknown code 'ZZZ' -- pass a code from the SDK's
  metadata/error-catalog.json.`` instead (`_unknown_code_line`). Only the
  WITH-SUGGESTIONS miss sentence is verbatim from `explain.py`.
* **The whole JSON MISS document, not just the hit path.** `alp_cli explain
  <bad> --json` prints `{"error": "unknown-code", "code": ..., "suggestions":
  [...]}` as its ENTIRE stdout document for a miss. `tan explain --code <bad>
  --format json` wraps instead, the same as the hit path: `data.diagnostic`
  is `null` (never the bare `"error"` shape), `data.suggestions` carries the
  shortlist, and the reason lives in `issues[0]` (`explain.code-unknown`) --
  not folded into `data` the way `explain.py`'s single-document miss is.
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

import typer

from tan.core import error_catalog
from tan.core.global_flags import accept_global_flags
from tan.core.scaffold import (
    TEMPLATE_SUPPORTED_SKUS,
    UNSUPPORTED_SOM_FAMILY_PREFIXES,
    TemplateDataError,
    is_family_gated,
    vendored_library_names_for,
)
from tan.core.text_layout import wrap_lines
from tan.env import wrap_width
from tan.envelope import Envelope, Issue, Project, SdkInfo, emit
from tan.exit_codes import ExitCode
from tan.output_format import FORMAT_HELP, OutputFormat

#: `data.schemaVersion` for this command's payload.
DATA_SCHEMA_VERSION = "1"

#: An unrecognised `[TEMPLATE]`/`--template` value shaped like a diagnostic
#: code (`ALP-B003`, `ALP_ERR_NO_BACKEND`) rather than a template id. The
#: overview deliberately never lists `--code` (it would break the golden), so
#: `--help` was the ONLY way to discover it -- a caller who typed the
#: alp-sdk spelling minus the flag got "unknown template" with no pointer at
#: the flag that would have worked. Anchored (not a bare `search`) so an id
#: that merely CONTAINS one of these shapes mid-string doesn't false-positive.
_LOOKS_LIKE_A_DIAGNOSTIC_CODE = re.compile(r"^(ALP-B\d+|ALP_ERR_.+)$", re.IGNORECASE)


def _code_hint(value: str) -> str:
    """The extra sentence appended to `explain.template-unknown`'s text line
    when `value` is shaped like a diagnostic code -- empty string otherwise,
    so every other rejected template keeps its existing, golden-pinned
    wording verbatim."""
    if _LOOKS_LIKE_A_DIAGNOSTIC_CODE.match(value):
        return f" Looking for a diagnostic code? Use --code {value} instead."
    return ""


def _only_note(skus: tuple[str, ...] | None) -> str:
    """` (<SKU>[, <SKU>...] only)`, or `""` when `skus` is falsy -- the
    description's trailing SoM qualifier, DERIVED from `TEMPLATE_SUPPORTED_SKUS`
    (`tan.core.scaffold`) rather than a hand-typed SKU string in the registry
    below. tan-cli#866: a hand-typed "(E1M-AEN801 only)" is exactly the second
    source of truth the issue is about -- it can drift from the data the CLI
    actually gates `tan init` on, silently, the way a copy-edit here never
    would if a consumer parsed the sentence instead of `data.som`. Called
    at MODULE LOAD, building `PROJECT_TEMPLATES` below -- must stay defined
    ahead of that tuple."""
    if not skus:
        return ""
    return f" ({', '.join(skus)} only)"


def _iot_wifi_note(skus: tuple[str, ...] | None) -> str:
    """`iot-starter`'s explanation sentence about the CC3501E Wi-Fi transport,
    with its SUPPORTED-SKU mentions (both of them) DERIVED from
    `TEMPLATE_SUPPORTED_SKUS["iot-starter"]` rather than hand-typed a second
    time. PR #985 review, major 3: #866 names this sentence, verbatim, as one
    of the two prose carriers of this restriction the issue is about; before
    this it was hand-typed independently of `_only_note`'s parenthetical, so
    a hand-edit of only its trailing SKU passed the whole suite (mutant E in
    that review). `E1M-V2N101` in the middle clause stays hand-typed: it is
    not a claim of support (the opposite -- the Wi-Fi path that does NOT
    exist there yet) and carries no table this module owns; it is the one
    named entry in `test_explain_command.py`'s `_CONTRAST_MENTIONS`, so a
    future rewrite that changes which SKU is contrasted still has to update
    that gate on purpose rather than by accident.

    `skus` empty/`None` is unreachable in practice (`iot-starter` is always a
    key of `TEMPLATE_SUPPORTED_SKUS`) but degrades to a SKU-free form rather
    than raising, matching `_only_note`'s style."""
    if not skus:
        return (
            "AEN-only + preview: the CC3501E Wi-Fi transport is "
            "silicon-validated on the supported SKU only; the Wi-Fi path on "
            "any other SoM does not exist yet, so --som is fixed for this "
            "template."
        )
    sku = skus[0]
    return (
        f"AEN-only + preview: {sku}'s CC3501E Wi-Fi transport is "
        f"silicon-validated; the E1M-V2N101 Wi-Fi path does not exist yet, "
        f"so --som is fixed to {sku} for this template."
    )


@dataclass(frozen=True)
class ProjectTemplate:
    """A `tan init` template as `tan explain` describes it.

    `features` is a `(wifi, mqtt, ble, tls)` tuple defaulting to all-false,
    collapsing Rust's `Option<WizardFeatureFlags>`: `format_feature_flags`
    renders `None` as all-false, so the distinction is unobservable in output
    and modelling it would only add a branch.
    """

    id: str
    label: str
    description: str
    explanation: tuple[str, ...]
    features: tuple[bool, bool, bool, bool] = (False, False, False, False)
    #: The registry's own `libs`, read ONLY for `minimal-app` -- every other
    #: template's libraries come from its vendored board.yaml (tan-cli#124).
    libs: tuple[str, ...] = ()


@dataclass(frozen=True)
class ModuleTemplate:
    """A `tan scaffold` module template. No `explanation`: Rust's
    `module_template_details` reads description + function_prefix + a fixed
    hint line and nothing else, so the registry's per-module explanation
    strings are unreachable from this command."""

    id: str
    label: str
    description: str
    function_prefix: str


@dataclass(frozen=True)
class GenerationTarget:
    """One `tan generate --target <emit>` output, verbatim from
    `GENERATION_TARGET_CATALOG` (`crates/tan-core/src/loader.rs`)."""

    emit: str
    display_name: str
    output_relative_path: str
    preview_label: str
    preview_language_id: str
    #: True when `output_relative_path` is a documentary template for a
    #: per-SKU/core DIRECTORY, not a literal path (only `zephyr-board`).
    is_directory: bool = False


#: The project-wizard templates, in registry order -- `TEMPLATE_DEFINITIONS`
#: (`crates/tan-core/src/wizard/service/registry.rs`). Order is wire contract:
#: `data.available.projectTemplates` and the overview's "Project templates:"
#: line both report it, and `contract/envelopes/explain-overview` pins both.
PROJECT_TEMPLATES: tuple[ProjectTemplate, ...] = (
    ProjectTemplate(
        id="minimal-app",
        label="Minimal app",
        description="Smallest baseline project with a simple main loop.",
        explanation=(
            "Minimal template keeps generated code intentionally small and neutral.",
            "Use this baseline when you want full control over feature bring-up order.",
        ),
    ),
    ProjectTemplate(
        id="zephyr-app",
        label="Zephyr app",
        description=(
            "West-buildable Zephyr application wired to board.yaml via the SDK loader."
        ),
        explanation=(
            "Real Zephyr app vendored from the SDK's `minimal` scaffold: "
            "find_package(Zephyr) + board.yaml -> alp.conf via EXTRA_CONF_FILE.",
            "Build with `west build -b <board>` after `export ALP_SDK_ROOT=<your "
            "alp-sdk checkout>`.",
        ),
    ),
    ProjectTemplate(
        id="sensor-starter",
        label="Sensor starter",
        description=(
            "West-buildable TMP112 i2c-master app wired to board.yaml via the SDK loader."
        ),
        explanation=(
            "Real Zephyr app vendored from the SDK's `sensor` scaffold: reads the "
            "TMP112 temperature sensor via <alp/chips/tmp112.h> on BRD_I2C.",
            "Build with `west build -b <board>` after `export ALP_SDK_ROOT=<your "
            "alp-sdk checkout>`.",
        ),
    ),
    ProjectTemplate(
        id="iot-starter",
        label="IoT starter",
        description=(
            "West-buildable Wi-Fi + MQTT/TLS telemetry app wired to board.yaml via "
            "the SDK loader"
            + _only_note(TEMPLATE_SUPPORTED_SKUS.get("iot-starter"))
            + "."
        ),
        explanation=(
            "Real Zephyr app vendored from the SDK's `iot` scaffold: brings up Wi-Fi "
            "via the CC3501E bridge and publishes an mqtts:// (TLS) MQTT telemetry "
            "reading on a cadence, through the portable <alp/iot.h> surface.",
            _iot_wifi_note(TEMPLATE_SUPPORTED_SKUS.get("iot-starter")),
            "Build with `west build -b <board>` after `export ALP_SDK_ROOT=<your "
            "alp-sdk checkout>`.",
        ),
        # The ONE template whose flags are not blanked in the Rust registry:
        # "Default features" still reads them, and the vendored board.yaml
        # cannot supply `mqtt` (there is no `iot.mqtt` toggle -- MQTT is
        # inherent to this app). `mqtt=false` here would contradict the
        # explanation line above it.
        features=(True, True, False, True),
    ),
    ProjectTemplate(
        id="edge-ai-starter",
        label="Edge AI starter",
        description=(
            "West-buildable BME280 cold-chain-monitor app wired to board.yaml via "
            "the SDK loader."
        ),
        explanation=(
            "Real Zephyr app vendored from the SDK's `edge-ai` scaffold: a BME280 "
            "cold-chain integrity monitor (MKT / dewpoint / excursion time) plus a "
            "TFLite-Micro anomaly score.",
            'Heterogeneous board.yaml: the app core (m33_sm/m55_hp) runs inference '
            'alongside a companion Cortex-A cluster (a55_cluster/a32_cluster, os: '
            '"off").',
            "Build with `west build -b <board>` after `export ALP_SDK_ROOT=<your "
            "alp-sdk checkout>`.",
        ),
    ),
    ProjectTemplate(
        id="board-diagnostics",
        label="Board diagnostics",
        description=(
            "West-buildable board self-test app wired to board.yaml via the SDK loader."
        ),
        explanation=(
            "Real Zephyr app vendored from the SDK's `diagnostics` scaffold: reads "
            "the SoM/SoC identity, the RUN operating-point profile, and scans the "
            "on-module I2C management bus for a pass/fail bring-up report.",
            "Portable <alp/*> APIs only -- no chip driver -- so the same source runs "
            "on every E1M family; a check a backend can't service reports SKIP, not "
            "FAIL.",
            "Build with `west build -b <board>` after `export ALP_SDK_ROOT=<your "
            "alp-sdk checkout>`.",
        ),
    ),
    ProjectTemplate(
        id="multicore-mailbox",
        label="Multicore -- mailbox (AEN M55-HP <-> M55-HE)",
        description=(
            "Dual-Zephyr-core starter for the Alif Ensemble E8: two Cortex-M55 "
            "cores, both real project cores."
        ),
        explanation=(
            "Vendored from the SDK's `multicore-mailbox` scaffold. Both cores get "
            "their own app -- `m55_hp: app: ./src` and `m55_he: app: ./peer` -- "
            "which is the topology no --template/--cores combination could "
            "scaffold before (tan-cli#864).",
            "HP stages a payload in cache-coherent shared SRAM and signals HE "
            "through the hardware mailbox over the portable <alp/mproc.h> "
            "raw-shmem + mailbox + hwsem surface; HE echoes it back. Deliberately "
            "NOT RPMsg -- no framing and no negotiated channel, just a raw pointer "
            "view over a fixed carve-out plus a doorbell.",
            "E1M-AEN801 only: the SDK catalog gates this template's "
            "`supported.som_skus` to that one SKU and refuses to emit it for any "
            "other, so tan refuses the same set rather than rendering it against "
            "another tree.",
        ),
    ),
)

#: The module-scaffold templates, in registry order --
#: `MODULE_TEMPLATE_DEFINITIONS` (same Rust file).
MODULE_TEMPLATES: tuple[ModuleTemplate, ...] = (
    ModuleTemplate(
        id="sensor-driver",
        label="Sensor driver module",
        description="Adds a source/header pair for sensor acquisition logic.",
        function_prefix="alp_sensor",
    ),
    ModuleTemplate(
        id="connectivity-service",
        label="Connectivity service module",
        description="Adds module skeleton for network/session orchestration.",
        function_prefix="alp_conn",
    ),
    ModuleTemplate(
        id="inference-stage",
        label="Inference stage module",
        description="Adds module skeleton for model pre/post processing path.",
        function_prefix="alp_infer",
    ),
    ModuleTemplate(
        id="diagnostics-check",
        label="Diagnostics check module",
        description="Adds bring-up and runtime health-check module scaffold.",
        function_prefix="alp_diag",
    ),
)

#: Every generation target, in fixed catalog order. All TEN of them: tan-cli#165
#: was the catalogue carrying only four, so `tan explain --target zephyr-board`
#: answered "Unknown generation target" for a target `tan generate` accepts.
#: Kept in step with `generate_cmd._OUTPUT_RELATIVE_PATH` by
#: `tests/commands/test_explain_command.py`, which is the guard against those
#: two drifting apart again -- the exact failure #165 was.
GENERATION_TARGETS: tuple[GenerationTarget, ...] = (
    GenerationTarget(
        emit="zephyr-conf",
        display_name="Zephyr config",
        output_relative_path="build/generated/alp.conf",
        preview_label="Zephyr config preview",
        preview_language_id="properties",
    ),
    GenerationTarget(
        emit="dts-overlay",
        display_name="Devicetree overlay",
        output_relative_path="build/generated/alp.overlay",
        preview_label="Devicetree overlay preview",
        preview_language_id="dts",
    ),
    GenerationTarget(
        emit="cmake-args",
        display_name="CMake args",
        output_relative_path="build/generated/alp-cmake-args.txt",
        preview_label="CMake args preview",
        preview_language_id="plaintext",
    ),
    GenerationTarget(
        emit="yocto-conf",
        display_name="Yocto config",
        output_relative_path="build/generated/alp-yocto.conf",
        preview_label="Yocto config preview",
        preview_language_id="properties",
    ),
    GenerationTarget(
        emit="native-sim-overlay",
        display_name="native_sim overlay",
        # The one target outside build/generated/: Zephyr auto-discovers
        # boards/<board>.overlay in the app's own SOURCE tree. Must stay
        # verbatim equal to generate_cmd's path table.
        output_relative_path="boards/native_sim_native_64.overlay",
        preview_label="native_sim overlay preview",
        preview_language_id="dts",
    ),
    GenerationTarget(
        emit="carrier-netlist",
        display_name="Carrier netlist",
        output_relative_path="build/generated/carrier-netlist.json",
        preview_label="Carrier netlist preview",
        preview_language_id="json",
    ),
    GenerationTarget(
        emit="west-libraries",
        display_name="West libraries",
        output_relative_path="build/generated/alp-west-libs.yml",
        preview_label="West libraries preview",
        preview_language_id="yaml",
    ),
    GenerationTarget(
        emit="hw-info-h",
        display_name="Hardware info header",
        output_relative_path="build/generated/alp_hw_info_build.h",
        preview_label="Hardware info header preview",
        preview_language_id="c",
    ),
    GenerationTarget(
        emit="os-topology",
        display_name="OS topology",
        output_relative_path="build/generated/os-topology.json",
        preview_label="OS topology preview",
        preview_language_id="json",
    ),
    GenerationTarget(
        emit="zephyr-board",
        display_name="Zephyr board tree",
        # Documentary only (`is_directory`): the real name is resolved per-run
        # by `generate_cmd.zephyr_board_dir_name`. Never joined onto a
        # workspace root from here -- this command writes nothing at all.
        output_relative_path="build/boards/alp_e1m_<sku-slug>_<core>/",
        preview_label="Zephyr board tree preview",
        preview_language_id="plaintext",
        is_directory=True,
    ),
)


class ExplainError(Exception):
    """A failure with its issue code, message, exit code and human line already
    decided. `selector_value` is the id the caller asked about, echoed back in
    `data.selector.value` (empty for an ambiguous selector, which named two)."""

    def __init__(
        self,
        code: str,
        message: str,
        text_line: str,
        *,
        exit_code: ExitCode = ExitCode.RUNTIME_FAILURE,
        selector_value: str = "",
        extra_data: dict | None = None,
        extra_issues: list[Issue] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.text_line = text_line
        self.exit_code = exit_code
        self.selector_value = selector_value
        #: Keys folded into the failure envelope's `data` -- only the `--code`
        #: miss uses it, to carry `suggestions` next to the refusal instead of
        #: burying the shortlist in an issue message a consumer must re-parse.
        self.extra_data = extra_data or {}
        #: tan-cli#950: PREPENDED ahead of this error's own `Issue` by
        #: `_fail`, the same `[*resolution_issues, Issue(...)]` shape
        #: `clean_cmd._run` / `bootstrap_cmd._refusal` / `new_som_cmd.new_som`
        #: use. Only `bind_sdk`'s `explain.sdk-root-unresolved` raise
        #: populates this today (`sdk.project-pin-unresolved` /
        #: `sdk.global-default-foreign-project`, tan-cli#263 review /
        #: tan-cli#464): it is the one `ExplainError` site that can compute a
        #: broken-pin/foreign-default fact and then discard the checkout it
        #: came from, so it is also the one site the shared `Envelope(...,
        #: sdk=...)` advisory machinery (`_with_sdk_resolution_advisories`)
        #: cannot reach -- that machinery fires only when an `SdkInfo` was
        #: actually built, and `bind_sdk` raises before building one.
        self.extra_issues = extra_issues or []


@dataclass
class _Result:
    """A resolved (non-error) explanation."""

    kind: str
    value: str
    summary: str
    details: list[str] = field(default_factory=list)
    #: `--code` only: the catalogue entry verbatim plus an (empty on a hit)
    #: suggestion list, folded into `data`. Empty for every other selector, so
    #: their envelopes keep the exact key set the golden pins.
    extra_data: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Detail lines
# ---------------------------------------------------------------------------


def _format_library_names(names: list[str]) -> str:
    """A comma-joined library list, or `(none)` when empty. `(none)` is the
    SOURCED answer for `zephyr-app`/`sensor-starter`/`board-diagnostics` --
    their vendored board.yaml has no `libraries:` block -- not a default."""
    return ", ".join(names) if names else "(none)"


def _format_feature_flags(features: tuple[bool, bool, bool, bool]) -> str:
    """`wifi=.. mqtt=.. ble=.. tls=..` with Rust's lowercase bool spelling.
    `str(True)` would emit `True`, which no consumer of this line expects."""
    wifi, mqtt, ble, tls = (str(f).lower() for f in features)
    return f"wifi={wifi} mqtt={mqtt} ble={ble} tls={tls}"


def _som_support_data(template_id: str) -> dict[str, object]:
    """`data.som` for one project template -- what `tan init --template
    <template_id>` will ACCEPT or REFUSE for `--som`, READ (never retyped)
    from `TEMPLATE_SUPPORTED_SKUS`/`UNSUPPORTED_SOM_FAMILY_PREFIXES`
    (`tan.core.scaffold`) so this cannot drift from the refusal it describes
    (tan-cli#866).

    PR #985 review, major 1: named `initAcceptsSkus`/`initRefusesSkuPrefixes`
    ON PURPOSE, not `supportedSkus`/`unsupportedSkuPrefixes` -- this is a
    REFUSAL policy, not a capability statement, and the two provably differ:
    `tan.core.scaffold._family_bucket` falls an unrecognised SKU prefix onto
    the default (Alif) tree rather than refusing it, so for every template
    here except `iot-starter`/`multicore-mailbox`, `initAcceptsSkus` is wider
    than alp-sdk's own scaffold-catalog `supported.som_skus` (see the module
    docstring above for the measured gap). A name that said "supported"
    would claim capability this field cannot back for 4 of 7 templates.

    `initAcceptsSkus` mirrors the `init.invalid-som` allowlist -- `None` when
    this template carries none. `initRefusesSkuPrefixes` mirrors the
    `init.som-unsupported` family exclusion -- always `[]` when
    `initAcceptsSkus` is set, because an exact-SKU allowlist is strictly
    narrower than (and already implies) that exclusion; repeating it would be
    a second way to say the same thing, the exact class of duplication this
    field exists to retire. A template with neither restriction
    (`minimal-app`, tan's one vendor-neutral, non-family-gated template --
    `scaffold.is_family_gated` is `False` for it alone) reports
    `initAcceptsSkus: null, initRefusesSkuPrefixes: []`: `tan init` accepts
    every SoM for it, unconditionally.
    """
    supported = TEMPLATE_SUPPORTED_SKUS.get(template_id)
    if supported is not None:
        return {"initAcceptsSkus": list(supported), "initRefusesSkuPrefixes": []}
    if is_family_gated(template_id):
        return {
            "initAcceptsSkus": None,
            "initRefusesSkuPrefixes": list(UNSUPPORTED_SOM_FAMILY_PREFIXES),
        }
    return {"initAcceptsSkus": None, "initRefusesSkuPrefixes": []}


def _family_excluded_note(template_id: str) -> str | None:
    """One `details[]` line for a FAMILY-gated (but not exact-SKU-gated)
    project template, DERIVED from `UNSUPPORTED_SOM_FAMILY_PREFIXES` -- `None`
    when there is nothing to add.

    PR #985 review, minor 5: before this, text mode said nothing at all about
    the family exclusion for `zephyr-app`/`sensor-starter`/`edge-ai-starter`/
    `board-diagnostics` even after `data.som.initRefusesSkuPrefixes` started
    carrying it in JSON -- a human running `tan explain --template zephyr-app`
    with no `--format json` only discovered the restriction as
    `init.som-unsupported`, at `tan init` time. `None` for `minimal-app` (not
    family-gated at all) and for the two exact-SKU-gated templates
    (`iot-starter`/`multicore-mailbox`): `_only_note` already reports their
    narrower restriction from the description line, so a second sentence here
    would repeat it rather than add information."""
    if template_id in TEMPLATE_SUPPORTED_SKUS or not is_family_gated(template_id):
        return None
    if not UNSUPPORTED_SOM_FAMILY_PREFIXES:
        return None
    prefixes = ", ".join(UNSUPPORTED_SOM_FAMILY_PREFIXES)
    return f"Refuses --som for these SoM families: {prefixes}."


def _project_template_details(pt: ProjectTemplate) -> list[str]:
    """Description, per-template explanation, the family-exclusion note (when
    one applies), default libraries, default features. Raises
    `TemplateDataError` when the vendored board.yaml behind the libraries
    line will not read."""
    names = vendored_library_names_for(pt.id)
    if names is None:
        names = list(pt.libs)  # `minimal-app` only: no vendored tree to read.
    details = [pt.description, *pt.explanation]
    family_note = _family_excluded_note(pt.id)
    if family_note is not None:
        details.append(family_note)
    details.append(f"Default libraries: {_format_library_names(names)}")
    details.append(f"Default features: {_format_feature_flags(pt.features)}")
    return details


def _module_template_details(mt: ModuleTemplate) -> list[str]:
    return [
        mt.description,
        f"Function prefix: {mt.function_prefix}",
        "Use this template with tan scaffold to generate a module source/header "
        "baseline.",
    ]


def _generation_target_details(target: GenerationTarget) -> list[str]:
    """tan-cli#165: `zephyr-board`'s path is a documentary template, so the
    "(writes a directory of files, not one path)" suffix says so instead of
    implying a real single file the way the other nine targets' paths are."""
    note = " (writes a directory of files, not one path)" if target.is_directory else ""
    return [
        f"Display name: {target.display_name}",
        f"Output path: {target.output_relative_path}{note}",
        f"Preview label: {target.preview_label}",
        f"Preview language: {target.preview_language_id}",
    ]


def _print_detail_lines(details: list[str], width: int | None) -> None:
    """`- <detail>` per entry, to stderr -- hard-wrapped past `width` when it
    is not `None` (a real terminal; see `tan.env.wrap_width`), unwrapped
    verbatim off one (piped/redirected, or non-interactive) -- the property
    `tan explain | grep` and every existing golden depend on. Every detail
    wraps the same way; see `tan.core.text_layout.wrap_lines` for why there
    is no longer a "record-shaped" exemption here.
    """
    for line in wrap_lines([f"- {detail}" for detail in details], width):
        print(line, file=sys.stderr)


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def _clean(raw: str | None) -> str | None:
    """A selector as Rust reads it: trimmed, and blank counts as ABSENT --
    `--template "   "` prints the overview, it is not an unknown template."""
    if raw is None:
        return None
    trimmed = raw.strip()
    return trimmed or None


def _overview() -> _Result:
    return _Result(
        kind="overview",
        value="all",
        summary="tan explain topics",
        details=[
            "Use --template to explain a project template (init) or module template "
            "(scaffold).",
            "Use --target to explain a generation output target.",
            "Project templates: " + ", ".join(t.id for t in PROJECT_TEMPLATES),
            "Module templates: " + ", ".join(t.id for t in MODULE_TEMPLATES),
            "Generation targets: " + ", ".join(t.emit for t in GENERATION_TARGETS),
        ],
    )


def resolve(template: str | None, target: str | None) -> _Result:
    """The explanation for this selector pair. Raises `ExplainError` for both
    selectors at once or an unknown id, and `TemplateDataError` when a vendored
    board.yaml will not read.

    A project template shadows a module template of the same id, matching the
    Rust's search order -- the two id sets are disjoint today, and this keeps
    the tie-break explicit rather than accidental if they ever collide.
    """
    template = _clean(template)
    target = _clean(target)

    if template is not None and target is not None:
        raise ExplainError(
            "explain.ambiguous-selector",
            "Use either --template or --target for explain, not both.",
            "explain: use either --template or --target, but not both in the same "
            "command.",
        )

    if template is not None:
        for pt in PROJECT_TEMPLATES:
            if pt.id == template:
                return _Result(
                    kind="project-template",
                    value=pt.id,
                    summary=f"{pt.label} ({pt.id})",
                    details=_project_template_details(pt),
                    # tan-cli#866: structured, so a consumer filters a picker
                    # without parsing `details[]` prose. Project templates
                    # only -- module templates and generation targets carry
                    # no SoM concept.
                    extra_data={"som": _som_support_data(pt.id)},
                )
        for mt in MODULE_TEMPLATES:
            if mt.id == template:
                return _Result(
                    kind="module-template",
                    value=mt.id,
                    summary=f"{mt.label} ({mt.id})",
                    details=_module_template_details(mt),
                )
        raise ExplainError(
            "explain.template-unknown",
            f"Unknown template '{template}'.",
            f"explain: unknown template '{template}'. Run tan explain without "
            f"selectors to list available topics.{_code_hint(template)}",
            selector_value=template,
        )

    if target is not None:
        for gt in GENERATION_TARGETS:
            if gt.emit == target:
                return _Result(
                    kind="generation-target",
                    value=gt.emit,
                    summary=f"{gt.display_name} ({gt.emit})",
                    details=_generation_target_details(gt),
                )
        raise ExplainError(
            "explain.target-unknown",
            f"Unknown generation target '{target}'.",
            f"explain: unknown generation target '{target}'. Run tan explain "
            f"without selectors to list available targets.",
            selector_value=target,
        )

    return _overview()


def bind_sdk(sdk_root_arg: str | None, project: str | None, code: str) -> tuple[Path, SdkInfo]:
    """The checkout `--code` reads, via the NARROW ladder (`--sdk-root` >
    `.alp/sdk-path` > `~/.alp/sdk-default` > discovery), walked from the
    `--project`-resolved workspace root -- `os.path.join(cwd, project)`, the
    same join `build_cmd.build`/`run_cmd.run`/`inspect_cmd.inspect` use, so an
    absolute `--project` replaces the cwd outright rather than nesting under it.

    Narrow because `explain` is not one of the three commands the oracle routes
    wide (`init`/`generate`/`examples`), so following the
    thirteen-command majority makes `tan explain --code` answer out of the same
    checkout `tan validate` used in the same directory -- the only reason to
    look a code up while a build is failing. `--project` IS read here (unlike
    every other selector on this command, see `explain`'s docstring): it is the
    one input this whole ladder exists to resolve against, and the twelve other
    `resolve_sdk_root_ladder` call sites all pass a `--project`-derived root,
    not the bare CWD -- accepting the flag and then resolving from the CWD
    anyway is the accepted-but-ignored input class this command refuses
    everywhere else (`explain`'s `--sdk-root` rationale).

    Raises `ExplainError` when no tier resolves anything, INCLUDING when
    `--sdk-root` (the terminal tier) points at a directory that resolves but
    carries no `SDK_MARKER` -- `resolve_sdk_root_ladder` itself does not check
    that for its terminal tier (I-31: a typo'd flag must not fall through to a
    lower tier), so an unmarked `--sdk-root` would otherwise reach
    `resolve_code` and misreport as `explain.catalog-unreadable` ("run the
    generator" in a checkout that was never one) instead of naming the real
    problem. Either way this is a refusal naming what it could not find, never
    an empty answer. Both imports are LOCAL so the three SDK-free paths keep
    paying nothing for a mode they never enter.

    tan-cli#950: the unresolved-SDK raise carries `resolution.broken_project_pin`
    / `.foreign_global_default_for` on `ExplainError.extra_issues` (the
    `clean_cmd._run` / `bootstrap_cmd._refusal` / `new_som_cmd.new_som` shape --
    the eighth instance of the tan-cli#900 class). This is the ONE branch that
    needs it computed explicitly: the SUCCESS return below hands its `SdkInfo`
    to `_emit`'s `Envelope(..., sdk=...)`, whose own
    `_with_sdk_resolution_advisories` already discloses the same pair
    generically from `SdkInfo.broken_project_pin` -- but `bind_sdk` raising
    here means no `SdkInfo` is ever built, so that shared machinery never
    runs and the fact would otherwise be discarded with the rest of
    `resolution`.
    """
    from tan.commands.build_cmd import resolve_sdk_root_ladder
    from tan.commands.sdk_cmd import (
        NO_SDK_NEXT_STEPS,
        global_default_foreign_project_issue,
        project_pin_issue,
    )
    from tan.core.shapes import SDK_MARKER

    cwd = Path.cwd()
    workspace_root = cwd if project is None else Path(os.path.join(str(cwd), project))
    resolution = resolve_sdk_root_ladder(sdk_root_arg, workspace_root)
    if resolution.path is None or not resolution.path.joinpath(*SDK_MARKER).exists():
        pin_issue = project_pin_issue(resolution.broken_project_pin, resolution.tier)
        foreign_issue = global_default_foreign_project_issue(
            resolution.foreign_global_default_for
        )
        raise ExplainError(
            "explain.sdk-root-unresolved",
            f"alp-sdk root is unresolved, so no diagnostic catalogue could be "
            f"read -- {NO_SDK_NEXT_STEPS}.",
            f"explain: alp-sdk root is unresolved, so no diagnostic catalogue "
            f"could be read -- {NO_SDK_NEXT_STEPS}.",
            selector_value=code.strip(),
            extra_issues=[i for i in (pin_issue, foreign_issue) if i is not None],
        )
    return resolution.path, SdkInfo.from_resolution(str(resolution.path), resolution)


def _unknown_code_line(code: str, suggestions: list[str]) -> str:
    """The difflib shortlist when there is one, otherwise a pointer at the
    catalogue that holds every real code.

    Only the WITH-SUGGESTIONS branch is `explain.py`'s own miss sentence,
    verbatim. The no-near-miss branch is tan's OWN wording, not a port: measured
    against alp-sdk `origin/dev`, `explain.py` there prints ``unknown code
    'ZZZ' -- run `alp explain` against a code from metadata/error-
    catalog.json`` (its own binary name, its own subcommand spelling), which
    this port cannot repeat verbatim -- there is no `alp explain` in a
    `tan`-only install. See the module DIVERGENCE list above.
    """
    if suggestions:
        return f"explain: unknown code '{code}'; did you mean: " + ", ".join(suggestions) + "?"
    return (
        f"explain: unknown code '{code}' -- pass a code from the SDK's "
        f"metadata/error-catalog.json."
    )


def resolve_code(code: str, sdk_root: Path) -> _Result:
    """The explanation for one `ALP_ERR_*` / `ALP-Bxxx` code out of `sdk_root`.

    Raises `ExplainError` for an unreadable catalogue and for an unknown code.
    The miss carries its shortlist in `data.suggestions` as well as in the text
    line, so a consumer gets the recovery options structured instead of having
    to re-parse a sentence.
    """
    try:
        codes = error_catalog.load_codes(sdk_root)
    except error_catalog.CatalogUnreadable as err:
        raise ExplainError(
            "explain.catalog-unreadable",
            f"{err.detail} ({err.path}).",
            f"explain: {err.detail} ({err.path}).",
            selector_value=code.strip(),
        ) from None
    key, suggestions = error_catalog.lookup(codes, code)
    if key is None:
        raise ExplainError(
            "explain.code-unknown",
            f"Unknown diagnostic code '{code}'.",
            _unknown_code_line(code, suggestions),
            selector_value=code.strip(),
            extra_data={"diagnostic": None, "suggestions": suggestions},
        )
    entry = codes[key]
    return _Result(
        kind="diagnostic-code",
        value=key,
        summary=error_catalog.summary_line(entry),
        details=error_catalog.detail_lines(entry),
        extra_data={"diagnostic": entry, "suggestions": []},
    )


# ---------------------------------------------------------------------------
# Envelope assembly
# ---------------------------------------------------------------------------


def _available() -> dict:
    """The full catalogue of explainable ids. Emitted on EVERY path, success and
    failure alike, so a caller that guessed an id wrong learns the valid ones
    from the same envelope."""
    return {
        "projectTemplates": [t.id for t in PROJECT_TEMPLATES],
        "moduleTemplates": [t.id for t in MODULE_TEMPLATES],
        "generationTargets": [t.emit for t in GENERATION_TARGETS],
    }


def _data(
    *, kind: str, value: str, summary: str, details: list[str], extra: dict | None = None
) -> dict:
    """`extra` is MERGED LAST and is empty on every path but `--code`: the
    `explain-overview` golden pins this key set exactly, so a key that appears
    unconditionally is a wire change for three selectors that gained nothing."""
    return {
        "schemaVersion": DATA_SCHEMA_VERSION,
        "selector": {"kind": kind, "value": value},
        "summary": summary,
        "details": details,
        "available": _available(),
        **(extra or {}),
    }


def _print_sdk_resolution_warnings(sdk: SdkInfo | None) -> None:
    """tan-cli#959: `explain: warning: <message>` lines to stderr for every
    fact `sdk_resolution_issues` finds in `sdk`'s resolution -- the text-mode
    disclosure of the same `sdk.project-pin-unresolved` /
    `sdk.global-default-foreign-project` pair `Envelope`'s
    `_with_sdk_resolution_advisories` already appends to `issues[]` for free
    under `--format json` (it fires there because `_emit`'s json branch is
    the only branch that ever constructs an `Envelope`). This is the text
    branch's own copy of that disclosure, called both from the success path
    (`explain`, ahead of the summary line) and from `_fail` (ahead of
    `err.text_line`) -- the two SDK-bound refusal sites, `resolve_code`'s
    `explain.catalog-unreadable` and `explain.code-unknown`, each bind an
    `SdkInfo` via `bind_sdk` before raising, so `sdk` is populated there the
    same way it is on success.

    `sdk` is `None` on every path that never resolved a checkout --
    `--template`/`--target` (`resolve`'s three raises), the two selector-
    clash refusals (`explain.positional-template-conflict` fires before
    `--code` is even cleaned; `explain.ambiguous-selector` fires only once
    `--code` IS set, but both `_fail` before `bind_sdk` is ever called, so
    neither attempts checkout resolution), and `bind_sdk`'s own
    `explain.sdk-root-unresolved` raise (it raises BEFORE building an
    `SdkInfo`, which is why tan-cli#950 had to carry that one pair through
    `ExplainError.extra_issues` instead -- the two mechanisms are disjoint by
    construction, never double-printing the same fact).
    """
    if sdk is None:
        return
    from tan.commands.sdk_cmd import sdk_resolution_issues

    for issue in sdk_resolution_issues(
        sdk.broken_project_pin, sdk.source_tier, sdk.foreign_global_default_for
    ):
        for line in wrap_lines([f"explain: warning: {issue.message}"], wrap_width()):
            print(line, file=sys.stderr)


def _emit(
    json_mode: bool,
    data: dict,
    issues: list[Issue],
    exit_code: ExitCode,
    sdk: SdkInfo | None = None,
) -> None:
    """Write the one envelope (JSON) and exit. `project` is always null/null --
    explain reads no board.yaml on ANY path, `--code` included: a diagnostic
    code is a property of the SDK, not of a project. `sdk` is populated only
    when `--code` resolved a checkout, which is also the only path that can
    carry the shared `sdk.*` resolution advisories `Envelope` appends."""
    if json_mode:
        emit(
            Envelope(
                "explain",
                Project(root=None, board_yaml=None),
                data,
                issues,
                exit_code,
                sdk=sdk,
            )
        )
    raise typer.Exit(int(exit_code))


def explain(
    template_arg: str = typer.Argument(
        None,
        metavar="[TEMPLATE]",
        help="Project or module template id -- shorthand for --template.",
    ),
    template: str = typer.Option(
        None,
        "--template",
        metavar="ID",
        help="Project template (init) or module template (scaffold) to explain.",
    ),
    target: str = typer.Option(
        None,
        "--target",
        metavar="EMIT",
        help="Generation output target to explain (e.g. zephyr-conf, zephyr-board).",
    ),
    code: str = typer.Option(
        None,
        "--code",
        metavar="CODE",
        help="alp-sdk diagnostic or error code to explain (e.g. ALP-B003, "
        "ALP_ERR_NO_BACKEND). Reads the bound SDK checkout.",
    ),
    project: str = typer.Option(  # read by --code ONLY; see below
        None, "--project", metavar="PATH", help="Project root (defaults to '.')."
    ),
    sdk_root: str = typer.Option(  # read by --code ONLY; see below
        None, "--sdk-root", metavar="PATH", help="alp-sdk checkout root."
    ),
    output_format: OutputFormat = typer.Option(OutputFormat.TEXT, "--format", help=FORMAT_HELP),
) -> None:
    """Explain a project/module template or a generation target.

    `--project` is declared on EVERY path but consumed by `--code` ALONE
    (`bind_sdk`): `--template`/`--target` are project-agnostic (they read no
    board.yaml, and the oracle's `project` stays `null`/`null` regardless of
    either flag's value on those two paths), but clap makes both `global =
    true` in Rust, so `tan --sdk-root X explain` / `tan --project X explain`
    must not be parse errors -- verified against the oracle. The reported
    envelope `project` key STILL stays `null`/`null` even on `--code`
    (`_emit`'s own docstring): `--project` there only picks WHICH checkout the
    SDK ladder resolves, the same role it plays for `tan validate`/`tan
    build`, it is not itself echoed as a project root. Accepting the flag and
    then resolving from the bare CWD regardless of its value would be the
    accepted-but-ignored input class this command refuses everywhere else --
    which is exactly what an earlier version of `bind_sdk` did (tan-cli#627
    review). `alp-sdk-vscode/src/ideHub/newProjectFlowPanel.ts:188` invokes
    exactly the `--sdk-root` shape. `--sdk-root` IS consumed, but only by
    `--code` (`bind_sdk`): it is the terminal tier of the SDK ladder (I-31),
    and the alternative -- accepting the flag that names the checkout the
    catalogue lives in and then discovering a different one -- is the
    accepted-but-ignored input class this command refuses elsewhere.

    `[TEMPLATE]` (the positional) is a PORT-ONLY convenience, not oracle
    behavior: the real `crates/tan-cli` `ExplainArgs` has no positional field,
    only `#[arg(long)] template`, so `tan explain minimal-app` errors there too
    ("Got unexpected extra argument(s)") even though every other `tan`
    subcommand that names a single id (`inspect`, `generate --target`, ...)
    reads naturally with one. Accepted here ONLY when `--template` is not
    already given; giving BOTH is a coded error (`explain.positional-template-
    conflict`), not a silent `--template`-wins -- the accepted-but-ignored
    input class this port keeps re-introducing. Folded into `template` before
    `resolve()` so the rest of this function is unaware of it.
    """
    json_mode = output_format == "json"

    if template_arg is not None and template is not None:
        _fail(
            json_mode,
            ExplainError(
                "explain.positional-template-conflict",
                f"Use either the positional template id ('{template_arg}') or "
                f"--template ('{template}'), not both.",
                f"explain: use either the positional template id ('{template_arg}') "
                f"or --template ('{template}'), not both in the same command.",
            ),
        )
        return

    # Captured BEFORE the fold below overwrites `template`: whether it was the
    # positional that supplied it (`--template` itself was absent), needed by
    # the `--code` clash message just below so it names what the caller
    # actually typed, the same way `explain.positional-template-conflict`
    # above already does.
    template_via_positional = template is None and template_arg is not None

    if template is None:
        template = template_arg

    code = _clean(code)
    if code is not None and (_clean(template) is not None or _clean(target) is not None):
        # FIRST, ahead of `resolve()`'s own template-vs-target check, so
        # `--template X --target Y --code Z` reports THIS message: it is the
        # accurate one for three selectors, where the pair's ("use either
        # --template or --target") names only two of them. The pair keeps its
        # own golden-pinned message on every input that reaches it, which is
        # every input with no `--code`. Same issue code for both -- one name
        # for "you named more than one thing to explain".
        #
        # `template_selector_name` names the POSITIONAL when that is what set
        # `template` (`tan explain minimal-app --code ALP-B003`): the fixed
        # wording named `--template` unconditionally there, even though the
        # caller never typed that flag -- the same accepted-but-ignored-input
        # confusion `explain.positional-template-conflict` exists to avoid.
        # `--target` stays fixed either way; it has no positional form.
        template_selector_name = (
            "the positional template id" if template_via_positional else "--template"
        )
        _fail(
            json_mode,
            ExplainError(
                "explain.ambiguous-selector",
                f"Use --code on its own; it cannot be combined with "
                f"{template_selector_name} or --target.",
                f"explain: use --code on its own, not combined with "
                f"{template_selector_name} or --target.",
            ),
        )
        return

    sdk: SdkInfo | None = None
    try:
        if code is not None:
            # Two statements, not one: `bind_sdk` raising leaves `sdk` None
            # (the tuple never binds), which is exactly what the failure
            # envelope should report -- nothing was resolved. A `resolve_code`
            # failure below DOES carry the checkout it read, so the reader can
            # see WHICH catalogue answered "unknown".
            sdk_root_path, sdk = bind_sdk(sdk_root, project, code)
            result = resolve_code(code, sdk_root_path)
        else:
            result = resolve(template, target)
    except ExplainError as err:
        _fail(json_mode, err, sdk)
        return
    except TemplateDataError as err:
        # A broken tan installation (the vendored tree did not ship), not a
        # project problem -- so INTERNAL_FAILURE, matching `init`'s
        # `init.template-unreadable`. The Rust `expect()`s the same read and
        # panics, which under `--format json` leaves stdout EMPTY and the
        # extension rendering nothing; a coded envelope is the port's contract.
        _fail(
            json_mode,
            ExplainError(
                "explain.template-unreadable",
                str(err),
                f"explain: {err}",
                exit_code=ExitCode.INTERNAL_FAILURE,
                selector_value=(template or "").strip(),
            ),
        )
        return
    except Exception as err:  # noqa: BLE001 -- the backstop; see the module docstring
        # `typer.Exit` cannot reach here: it is raised only from `_emit`, which
        # runs outside this try.
        _fail(
            json_mode,
            ExplainError(
                "explain.internal-failure",
                f"explain failed unexpectedly: {err.__class__.__name__}: {err}",
                f"explain: failed unexpectedly: {err.__class__.__name__}: {err}",
                exit_code=ExitCode.INTERNAL_FAILURE,
                selector_value=(template or target or code or "").strip(),
            ),
            sdk,
        )
        return

    if not json_mode:
        # stderr, in both formats: stdout is the envelope channel and nothing
        # else. Matches Rust's `emit()`, which `eprintln!`s every text line.
        # tan-cli#959: the SDK-resolution advisories (`sdk.project-pin-
        # unresolved` / `sdk.global-default-foreign-project`) print FIRST,
        # matching `_fail`'s own order and `bootstrap_cmd._refusal`'s
        # `warning_lines + outcome.text` -- a caveat about which checkout
        # answered belongs ahead of the answer itself, not after it.
        # The summary header is a short, bounded "<label> (<id>)" -- never
        # observed over any real width, so it is never run through
        # `wrap_block`; `_print_detail_lines` is where the actual wrap
        # decision lives.
        _print_sdk_resolution_warnings(sdk)
        print(f"explain: {result.summary}", file=sys.stderr)
        _print_detail_lines(result.details, wrap_width())
    _emit(
        json_mode,
        _data(
            kind=result.kind,
            value=result.value,
            summary=result.summary,
            details=result.details,
            extra=result.extra_data,
        ),
        [],
        ExitCode.SUCCESS,
        sdk,
    )


def _fail(json_mode: bool, err: ExplainError, sdk: SdkInfo | None = None) -> None:
    """The error envelope. Note the asymmetry with the success path, which is
    contract: `selector.kind` reverts to `overview` and summary/details go
    EMPTY even though the caller named a selector, because nothing was
    explained. `data.available` stays populated."""
    if not json_mode:
        # tan-cli#950: `err.extra_issues` (today only `bind_sdk`'s broken-pin
        # / foreign-global-default pair) printed AHEAD of the refusal line --
        # `bootstrap_cmd._refusal`'s `warning_lines + outcome.text` order --
        # since this text branch is the only path those facts reach; the JSON
        # branch below carries them in `issues[]` instead. Without this,
        # `--format json` would disclose the pin (tan-cli#677's asymmetry).
        for issue in err.extra_issues:
            for line in wrap_lines([f"explain: warning: {issue.message}"], wrap_width()):
                print(line, file=sys.stderr)
        # tan-cli#959: the other half of the same disclosure -- `sdk` is
        # populated (not `err.extra_issues`) on the two SDK-BOUND refusals,
        # `explain.catalog-unreadable` / `explain.code-unknown`, since both
        # raise from `resolve_code` AFTER `bind_sdk` already built an
        # `SdkInfo`. Disjoint from the loop above by construction (see
        # `_print_sdk_resolution_warnings`'s docstring), so this never
        # double-prints.
        _print_sdk_resolution_warnings(sdk)
        # A refusal sentence, not a record -- `--template`/`--target` echo
        # the CALLER's own (unbounded-length) input back into it (e.g.
        # "unknown template '<whatever was typed>'"), so this is the one
        # `explain` text line genuinely able to run long on a hostile or
        # just-long argument, not only in the two catalogue commands this
        # task is scoped to.
        for line in wrap_lines([err.text_line], wrap_width()):
            print(line, file=sys.stderr)
    _emit(
        json_mode,
        _data(
            kind="overview",
            value=err.selector_value,
            summary="",
            details=[],
            extra=err.extra_data,
        ),
        [*err.extra_issues, Issue(err.code, "error", err.message)],
        err.exit_code,
        sdk,
    )


# tan-cli#261: adds the seven oracle `GlobalArgs` flags this command was
# still missing (`--all`/`--board-yaml`/`--ci`/`--no-color`/
# `--non-interactive`/`--quiet`/`--verbose`) on top of `--target`, already
# declared and read above; see `tan.core.global_flags`. `--project`/
# `--sdk-root` are ALSO declared already (read by `--code` alone -- see
# `explain`'s own docstring); the decorator leaves both untouched the same
# way it leaves `--target` untouched.
explain = accept_global_flags(explain)
