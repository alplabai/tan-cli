# SPDX-License-Identifier: Apache-2.0
"""`tan.core.module_template` -- the module-scaffold registry, name
normalization, and file-content generators `tan scaffold` (#260) plans
against. Every assertion below was cross-checked against the frozen Rust
oracle (`target/debug/tan.exe scaffold --format json ...`), not derived from
reading `crates/tan-core/src/wizard/service/module_scaffold.rs` alone.
"""

import pytest

from tan.core.module_template import (
    DEFAULT_MODULE_TEMPLATE_ID,
    MODULE_TEMPLATE_IDS,
    create_module_scaffold_plan,
    list_module_templates,
    normalize_module_name,
    plan_module_files,
)


def test_registry_order_and_ids_match_the_oracle():
    # `ModuleTemplateId::as_str` order, `wizard/models.rs`.
    assert MODULE_TEMPLATE_IDS == (
        "sensor-driver",
        "connectivity-service",
        "inference-stage",
        "diagnostics-check",
    )
    assert [d.id for d in list_module_templates()] == list(MODULE_TEMPLATE_IDS)


def test_default_template_is_the_first_registry_entry():
    # `resolve_template`'s non-interactive arm hardcodes `SensorDriver`
    # (`crates/tan-cli/src/commands/scaffold.rs`) -- the registry's first id,
    # unlike `tan init`'s own default (which is NOT its first template).
    assert DEFAULT_MODULE_TEMPLATE_ID == MODULE_TEMPLATE_IDS[0] == "sensor-driver"


# ---------------------------------------------------------------------------
# normalize_module_name
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("my-conn", "my_conn"),
        ("My Sensor!", "my_sensor"),
        ("  a1_b2  ", "a1_b2"),
        ("___", None),  # every char is a separator -> empty -> ValueError
        # Measured against the oracle: the accented `é` and the double dash
        # beside it collapse into ONE separator run, not two underscores.
        ("Héllo--World123", "h_llo_world123"),
    ],
)
def test_normalize_module_name_matches_the_oracle(raw, expected):
    if expected is None:
        with pytest.raises(ValueError, match="empty after normalization"):
            normalize_module_name(raw)
    else:
        assert normalize_module_name(raw) == expected


def test_normalize_module_name_never_leaves_a_leading_or_trailing_separator():
    assert normalize_module_name("--leading and trailing--") == "leading_and_trailing"


# ---------------------------------------------------------------------------
# File-content generators
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("template_id", MODULE_TEMPLATE_IDS)
def test_every_template_plans_the_same_three_paths_in_order(template_id):
    plan = create_module_scaffold_plan(template_id, "my_mod")
    paths = [f.relative_path for f in plan.files]
    # Exact order the oracle's `data.fileChanges[]` lists them in.
    assert paths == [
        "include/modules/my_mod.h",
        "src/modules/my_mod/my_mod.c",
        "src/modules/my_mod/README.md",
    ]
    assert plan.template_id == template_id
    assert plan.normalized_name == "my_mod"


def test_connectivity_service_content_matches_the_oracle_byte_for_byte():
    """Pinned against `target/debug/tan.exe --format json scaffold --name
    my-conn --template connectivity-service` (measured, not read from
    source): the exact bytes a customer's module lands with."""
    plan = create_module_scaffold_plan("connectivity-service", "my-conn")
    by_path = {f.relative_path: f.content for f in plan.files}

    assert by_path["include/modules/my_conn.h"] == (
        "// SPDX-License-Identifier: Apache-2.0\n"
        "\n"
        "#ifndef ALP_MODULES_MY_CONN_H\n"
        "#define ALP_MODULES_MY_CONN_H\n"
        "\n"
        "int alp_conn_my_conn_init(void);\n"
        "int alp_conn_my_conn_run(void);\n"
        "\n"
        "#endif /* ALP_MODULES_MY_CONN_H */\n"
    )
    assert by_path["src/modules/my_conn/my_conn.c"] == (
        "// SPDX-License-Identifier: Apache-2.0\n"
        "\n"
        '#include "modules/my_conn.h"\n'
        "\n"
        "// Board context: unavailable\n"
        "\n"
        "int alp_conn_my_conn_init(void) {\n"
        "  // TODO: initialize module dependencies.\n"
        "  return 0;\n"
        "}\n"
        "\n"
        "int alp_conn_my_conn_run(void) {\n"
        "  // TODO: implement module main behavior.\n"
        "  return 0;\n"
        "}\n"
    )
    # The README is the ONE file here that is deliberately not the oracle's
    # bytes: tan-cli#494 defect 5 adds `## Wiring`, because the oracle's
    # README documents a module no shipped template compiles. See `_wiring`'s
    # docstring for the measurement and for why nothing frozen pins these
    # bytes -- this assertion is the only pin, which is why it is exact and
    # why it carries both build shapes rather than a paraphrase.
    assert by_path["src/modules/my_conn/README.md"] == (
        "# Alp Module Scaffold\n"
        "\n"
        "Template: connectivity-service\n"
        "Module: my_conn\n"
        "\n"
        "## Notes\n"
        "\n"
        "- Use my_conn_init for stack/session initialization.\n"
        "- Keep retry/backoff and transport health checks localized in this module.\n"
        "\n"
        "## Wiring\n"
        "\n"
        "`tan scaffold` writes this module but does NOT edit your build files.\n"
        "\n"
        "Most templates (`zephyr-app`, `iot-starter`, `sensor-starter`,\n"
        "`edge-ai-starter`, `board-diagnostics`) build Zephyr's `app` target -- add\n"
        "both lines to the top-level `CMakeLists.txt`, after its `target_sources`:\n"
        "\n"
        "```cmake\n"
        "target_sources(app PRIVATE src/modules/my_conn/my_conn.c)\n"
        "target_include_directories(app PRIVATE include)\n"
        "```\n"
        "\n"
        "`minimal-app` builds `alp_app` and already sets its include directory, so\n"
        "it needs the source only -- add this to `ALP_APP_SOURCES` in\n"
        "`src/CMakeLists.txt` (paths there are relative to `src/`):\n"
        "\n"
        "```cmake\n"
        "  modules/my_conn/my_conn.c\n"
        "```\n"
        "\n"
        "Without the source entry the module is never compiled; without an include\n"
        'directory `#include "modules/my_conn.h"` does not resolve.\n'
        "\n"
        "Generated by Alp: Scaffold module.\n"
    )


def test_the_wiring_section_names_the_paths_the_plan_actually_writes():
    """The wiring text is only worth anything if its paths match the files the
    same plan puts on disk -- a stale instruction is the silence it replaced,
    with extra steps. Derives both from `plan_module_files` rather than
    restating them, so renaming a planned path fails HERE rather than shipping
    a README pointing at a file that does not exist."""
    definition = next(d for d in list_module_templates() if d.id == "sensor-driver")
    files = plan_module_files(definition, "tmp112")
    by_path = {f.relative_path: f.content for f in files}
    readme = by_path["src/modules/tmp112/README.md"]

    assert "src/modules/tmp112/tmp112.c" in by_path
    assert "include/modules/tmp112.h" in by_path
    # The `app`-target line names the planned source verbatim.
    assert "target_sources(app PRIVATE src/modules/tmp112/tmp112.c)" in readme
    # `minimal-app`'s entry is that same source relative to `src/`.
    assert "  modules/tmp112/tmp112.c\n" in readme
    # The include the module's own `.c` uses is the one the section explains.
    assert '#include "modules/tmp112.h"' in by_path["src/modules/tmp112/tmp112.c"]
    assert '`#include "modules/tmp112.h"` does not resolve' in readme


def test_readme_substitutes_nm_into_every_explanation_line():
    definition = next(d for d in list_module_templates() if d.id == "sensor-driver")
    files = plan_module_files(definition, "tmp112")
    readme = next(f for f in files if f.relative_path.endswith("README.md"))
    assert "tmp112_run" in readme.content
    assert "{nm}" not in readme.content


def test_create_module_scaffold_plan_raises_on_an_unnormalizable_name():
    with pytest.raises(ValueError, match="empty after normalization"):
        create_module_scaffold_plan("sensor-driver", "!!!")
