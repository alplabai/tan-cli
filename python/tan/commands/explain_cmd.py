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
  `DEFAULT_SOM_SKU`/`IOT_STARTER_SUPPORTED_SKU`. Nothing in this file is keyed
  on a SKU, an address, or a pin.

The ONE derived line is "Default libraries", which reads each template's
vendored `board.yaml` rather than a registry field
(`scaffold.vendored_library_names_for`) -- tan-cli#124: the field is
deliberately blanked for a vendored template and reported "(none)" for
`edge-ai-starter`, whose scaffold declares `tflite-micro`.

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
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field

import typer

from tan.core.scaffold import TemplateDataError, vendored_library_names_for
from tan.envelope import Envelope, Issue, Project, emit
from tan.exit_codes import ExitCode

#: `data.schemaVersion` for this command's payload.
DATA_SCHEMA_VERSION = "1"


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
            "the SDK loader (E1M-AEN801 only)."
        ),
        explanation=(
            "Real Zephyr app vendored from the SDK's `iot` scaffold: brings up Wi-Fi "
            "via the CC3501E bridge and publishes an mqtts:// (TLS) MQTT telemetry "
            "reading on a cadence, through the portable <alp/iot.h> surface.",
            "AEN-only + preview: E1M-AEN801's CC3501E Wi-Fi transport is "
            "silicon-validated; the E1M-V2N101 Wi-Fi path does not exist yet, so "
            "--som is fixed to E1M-AEN801 for this template.",
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
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.text_line = text_line
        self.exit_code = exit_code
        self.selector_value = selector_value


@dataclass
class _Result:
    """A resolved (non-error) explanation."""

    kind: str
    value: str
    summary: str
    details: list[str] = field(default_factory=list)


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


def _project_template_details(pt: ProjectTemplate) -> list[str]:
    """Description, per-template explanation, default libraries, default
    features. Raises `TemplateDataError` when the vendored board.yaml behind
    the libraries line will not read."""
    names = vendored_library_names_for(pt.id)
    if names is None:
        names = list(pt.libs)  # `minimal-app` only: no vendored tree to read.
    return [
        pt.description,
        *pt.explanation,
        f"Default libraries: {_format_library_names(names)}",
        f"Default features: {_format_feature_flags(pt.features)}",
    ]


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
            f"selectors to list available topics.",
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


def _data(*, kind: str, value: str, summary: str, details: list[str]) -> dict:
    return {
        "schemaVersion": DATA_SCHEMA_VERSION,
        "selector": {"kind": kind, "value": value},
        "summary": summary,
        "details": details,
        "available": _available(),
    }


def _emit(json_mode: bool, data: dict, issues: list[Issue], exit_code: ExitCode) -> None:
    """Write the one envelope (JSON) and exit. `project` is always null/null:
    explain is project-agnostic -- it reads no board.yaml and no checkout."""
    if json_mode:
        emit(
            Envelope(
                "explain",
                Project(root=None, board_yaml=None),
                data,
                issues,
                exit_code,
            )
        )
    raise typer.Exit(int(exit_code))


def explain(
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
    output_format: str = typer.Option(
        "text", "--format", metavar="FORMAT", help="Output format: text or json."
    ),
) -> None:
    """Explain a project/module template or a generation target."""
    if output_format not in ("text", "json"):
        raise typer.BadParameter(
            f"'{output_format}' (choose from 'text', 'json')", param_hint="--format"
        )
    json_mode = output_format == "json"

    try:
        result = resolve(template, target)
    except ExplainError as err:
        _fail(json_mode, err)
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
                selector_value=(template or target or "").strip(),
            ),
        )
        return

    if not json_mode:
        # stderr, in both formats: stdout is the envelope channel and nothing
        # else. Matches Rust's `emit()`, which `eprintln!`s every text line.
        print(f"explain: {result.summary}", file=sys.stderr)
        for detail in result.details:
            print(f"- {detail}", file=sys.stderr)
    _emit(
        json_mode,
        _data(
            kind=result.kind,
            value=result.value,
            summary=result.summary,
            details=result.details,
        ),
        [],
        ExitCode.SUCCESS,
    )


def _fail(json_mode: bool, err: ExplainError) -> None:
    """The error envelope. Note the asymmetry with the success path, which is
    contract: `selector.kind` reverts to `overview` and summary/details go
    EMPTY even though the caller named a selector, because nothing was
    explained. `data.available` stays populated."""
    if not json_mode:
        print(err.text_line, file=sys.stderr)
    _emit(
        json_mode,
        _data(kind="overview", value=err.selector_value, summary="", details=[]),
        [Issue(err.code, "error", err.message)],
        err.exit_code,
    )
