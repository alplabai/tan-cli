# SPDX-License-Identifier: Apache-2.0
"""Module-scaffold template registry, name normalization, and file-content
generators for `tan scaffold` -- adds one driver/service/stage/check module
INTO an existing project. Distinct from `tan.core.scaffold`, which is the
whole-*project* `tan init` engine (a different template id space: six
project templates there vs. the four module templates here, and neither
registry's ids overlap the other's).

Port of `crates/tan-core/src/wizard/{models,service/registry,service/plan,
service/module_scaffold}.rs`'s module-scaffold slice only -- the project-
wizard half of those same files is `tan.core.scaffold`'s job.

Diffing planned files against disk, writing them, and rendering the ASCII
tree preview are deliberately NOT re-implemented here: `tan.core.scaffold`'s
`PlannedFile` / `collect_file_changes` / `write_files` / `scaffold_tree_preview`
already do exactly that -- module-scaffold's planned files are the SAME
`PlannedFile` shape a whole-project plan uses, and `write_files` already
carries the tan-cli#325 containment fix (`tan.core.fs_confine.resolve_confined`).
A second copy of any of the four here would be exactly the drift
`tan.core.fs_confine`'s own module docstring warns a THIRD hand-rolled
resolver into being.
"""

from __future__ import annotations

from dataclasses import dataclass

from tan.core.scaffold import PlannedFile

#: Module template ids, in registry order (`ModuleTemplateId::as_str`,
#: `wizard/models.rs`). Wire contract: `data.templateId` echoes one of these
#: verbatim.
MODULE_TEMPLATE_IDS = (
    "sensor-driver",
    "connectivity-service",
    "inference-stage",
    "diagnostics-check",
)

#: The template a non-interactive `tan scaffold` with no `--template` gets --
#: `resolve_template`'s non-interactive arm in `crates/tan-cli/src/commands/
#: scaffold.rs` hardcodes `ModuleTemplateId::SensorDriver`, the registry's
#: first entry. Unlike `tan.core.scaffold.DEFAULT_TEMPLATE_ID` (`tan init`'s
#: own default), this one has no reason to diverge from "first in the list":
#: there is no `CMakeLists.txt`-shaped trap here, every module template emits
#: the same three-file shape.
DEFAULT_MODULE_TEMPLATE_ID = "sensor-driver"


@dataclass(frozen=True)
class ModuleTemplateDefinition:
    """One module-template registry entry. `explanation` lines may contain a
    literal `{nm}` placeholder, substituted with the normalized module name
    when a module's `README.md` is rendered (see `_readme`, below)."""

    id: str
    label: str
    description: str
    function_prefix: str
    explanation: tuple[str, ...]


#: `MODULE_TEMPLATE_DEFINITIONS` (`wizard/service/registry.rs`), ported
#: verbatim -- same four ids, same order, same prose.
_REGISTRY: tuple[ModuleTemplateDefinition, ...] = (
    ModuleTemplateDefinition(
        id="sensor-driver",
        label="Sensor driver module",
        description="Adds a source/header pair for sensor acquisition logic.",
        function_prefix="alp_sensor",
        explanation=(
            "Use {nm}_run to place sensor polling and conversion logic.",
            "Keep hardware-specific register access isolated from upper-level app flow.",
        ),
    ),
    ModuleTemplateDefinition(
        id="connectivity-service",
        label="Connectivity service module",
        description="Adds module skeleton for network/session orchestration.",
        function_prefix="alp_conn",
        explanation=(
            "Use {nm}_init for stack/session initialization.",
            "Keep retry/backoff and transport health checks localized in this module.",
        ),
    ),
    ModuleTemplateDefinition(
        id="inference-stage",
        label="Inference stage module",
        description="Adds module skeleton for model pre/post processing path.",
        function_prefix="alp_infer",
        explanation=(
            "Use {nm}_run to host pre-process, infer, and post-process calls.",
            "Keep model IO shaping and feature extraction close to this module boundary.",
        ),
    ),
    ModuleTemplateDefinition(
        id="diagnostics-check",
        label="Diagnostics check module",
        description="Adds bring-up and runtime health-check module scaffold.",
        function_prefix="alp_diag",
        explanation=(
            "Use {nm}_run for periodic health checks and error probes.",
            "Keep board bring-up assertions and diagnostics output in this module.",
        ),
    ),
)

_BY_ID = {d.id: d for d in _REGISTRY}


def list_module_templates() -> list[ModuleTemplateDefinition]:
    """All registered module-scaffold templates, in registry order."""
    return list(_REGISTRY)


def normalize_module_name(name: str) -> str:
    """Lowercase `name` and collapse every run of non-`[a-z0-9]` characters
    (after lowering) into a single `_` separator, with none leading or
    trailing. Raises `ValueError` -- its message is `scaffold.invalid-name`'s
    wire text verbatim -- when nothing survives.

    `wizard::service::plan::normalize_module_name`, ported: Rust lowercases
    with the full Unicode mapping, then keeps only `is_ascii_lowercase() ||
    is_ascii_digit()` chars, treating everything else (accented letters
    included, even after they lower) as a separator run. Python's `.lower()`
    is equally Unicode-aware, so the same two-step -- lower, then filter to
    ASCII alnum -- reproduces it byte-for-byte (measured against the oracle:
    `"Héllo--World123"` -> `"h_llo_world123"`, the accented `é` collapsing
    into the same run as the double dash beside it).
    """
    lowered = name.strip().lower()
    out: list[str] = []
    in_sep = False
    for ch in lowered:
        if ("a" <= ch <= "z") or ("0" <= ch <= "9"):
            if in_sep and out:
                out.append("_")
            out.append(ch)
            in_sep = False
        else:
            in_sep = True
    result = "".join(out)
    if not result:
        raise ValueError("Module name is empty after normalization.")
    return result


def _header(prefix: str, nm: str) -> str:
    """`gen_module_header`, ported. `nm` is always already-normalized ASCII
    lowercase/digits/underscore, so a plain `.upper()` reproduces Rust's
    `nm.to_uppercase()` exactly -- no non-ASCII input ever reaches here."""
    upper = nm.upper()
    return (
        "// SPDX-License-Identifier: Apache-2.0\n"
        "\n"
        f"#ifndef ALP_MODULES_{upper}_H\n"
        f"#define ALP_MODULES_{upper}_H\n"
        "\n"
        f"int {prefix}_{nm}_init(void);\n"
        f"int {prefix}_{nm}_run(void);\n"
        "\n"
        f"#endif /* ALP_MODULES_{upper}_H */\n"
    )


def _source(prefix: str, nm: str) -> str:
    """`gen_module_c`, ported."""
    return (
        "// SPDX-License-Identifier: Apache-2.0\n"
        "\n"
        f'#include "modules/{nm}.h"\n'
        "\n"
        "// Board context: unavailable\n"
        "\n"
        f"int {prefix}_{nm}_init(void) {{\n"
        "  // TODO: initialize module dependencies.\n"
        "  return 0;\n"
        "}\n"
        "\n"
        f"int {prefix}_{nm}_run(void) {{\n"
        "  // TODO: implement module main behavior.\n"
        "  return 0;\n"
        "}\n"
    )


def _readme(definition: ModuleTemplateDefinition, nm: str) -> str:
    """`gen_module_readme`, ported."""
    lines = "".join(f"- {line.replace('{nm}', nm)}\n" for line in definition.explanation)
    return (
        "# Alp Module Scaffold\n"
        "\n"
        f"Template: {definition.id}\n"
        f"Module: {nm}\n"
        "\n"
        "## Notes\n"
        "\n"
        f"{lines}"
        "\n"
        "Generated by Alp: Scaffold module.\n"
    )


def plan_module_files(definition: ModuleTemplateDefinition, nm: str) -> list[PlannedFile]:
    """The three files a module template lays down: a header, its source, and
    a README. `gen_module_files`, ported -- same relative paths, same order
    (the order the oracle's own `data.fileChanges[]` lists them in, and what
    `contract`-style byte-for-byte JSON comparison depends on)."""
    return [
        PlannedFile(f"include/modules/{nm}.h", _header(definition.function_prefix, nm)),
        PlannedFile(f"src/modules/{nm}/{nm}.c", _source(definition.function_prefix, nm)),
        PlannedFile(f"src/modules/{nm}/README.md", _readme(definition, nm)),
    ]


@dataclass(frozen=True)
class ModuleScaffoldPlan:
    """`ModuleScaffoldPlan`, ported: the resolved template plus the module's
    normalized name and its planned files."""

    template_id: str
    normalized_name: str
    files: list[PlannedFile]


def create_module_scaffold_plan(template_id: str, module_name: str) -> ModuleScaffoldPlan:
    """Normalize `module_name`, then plan its three-file set against
    `template_id`. Raises `ValueError` when the name normalizes to empty
    (`normalize_module_name`); a `template_id` outside `MODULE_TEMPLATE_IDS`
    is a caller bug, not a user error -- `scaffold_cmd` validates it against
    the registry BEFORE calling this, so `KeyError` here would mean that
    validation was skipped. `create_module_scaffold_plan`, ported.
    """
    normalized = normalize_module_name(module_name)
    definition = _BY_ID[template_id]
    files = plan_module_files(definition, normalized)
    return ModuleScaffoldPlan(template_id=template_id, normalized_name=normalized, files=files)
