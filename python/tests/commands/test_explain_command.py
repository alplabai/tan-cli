# SPDX-License-Identifier: Apache-2.0
"""`tan explain` -- the oracle's own unit tests, ported, plus the failure
contract the Rust does not have.

Every case in the first two sections has a named twin in
`crates/tan-cli/src/commands/explain.rs`'s `mod tests`: the tan-cli#124
"Default libraries" regressions and the tan-cli#115/#165 catalogue ones. The
`explain-overview` conformance golden reaches only the overview path in JSON,
so everything else -- the two selector paths, the text renderer, all three
error codes -- is pinned here.

The third section is port-specific and has no Rust twin by design: Rust
`expect()`s the vendored board.yaml read and PANICS, which under `--format
json` leaves stdout empty and the extension rendering nothing at all. The port
owes a coded envelope on every failure path, so these tests are the guard.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tan.cli import app
from tan.commands.explain_cmd import (
    GENERATION_TARGETS,
    MODULE_TEMPLATES,
    PROJECT_TEMPLATES,
    resolve,
)
from tan.core.scaffold import _library_names, vendored_library_names_for

runner = CliRunner()


def details(template: str | None = None, target: str | None = None) -> list[str]:
    return resolve(template, target).details


# --------------------------------------------------------------------------
# Default libraries / features (tan-cli#124)
# --------------------------------------------------------------------------


def test_edge_ai_starter_reports_its_vendored_library():
    """The registry's `libs` field is blanked for this vendored template, but
    its real vendored board.yaml declares `libraries: [tflite-micro]` --
    `explain` must report that, not "(none)"."""
    lines = details(template="edge-ai-starter")
    assert "Default libraries: tflite-micro" in lines
    assert "Default libraries: (none)" not in lines


def test_edge_ai_starter_json_envelope_reports_its_vendored_library():
    """Same fact through `data.details`, which is what the extension parses."""
    result = runner.invoke(
        app, ["explain", "--template", "edge-ai-starter", "--format", "json"]
    )
    assert result.exit_code == 0
    doc = json.loads(result.stdout)
    assert "Default libraries: tflite-micro" in doc["data"]["details"]


def test_iot_starter_still_reports_mbedtls_and_its_feature_flags():
    """iot-starter was hand-synced correctly ahead of the #124 fix: pins that
    deriving the libraries line from the vendored board.yaml did not regress
    it, and that "Default features" (still registry-sourced, and the ONE
    template whose flags are not blanked) is untouched."""
    lines = details(template="iot-starter")
    assert "Default libraries: mbedtls" in lines
    assert "Default features: wifi=true mqtt=true ble=false tls=true" in lines


@pytest.mark.parametrize("template", ["zephyr-app", "sensor-starter", "board-diagnostics"])
def test_vendored_templates_with_no_libraries_block_report_none(template):
    """"(none)" is the correct, SOURCED answer for these -- their real
    board.yaml has no `libraries:` block at all (an empty list from
    `vendored_library_names_for`, not an unread default)."""
    assert vendored_library_names_for(template) == []
    assert "Default libraries: (none)" in details(template=template)


def test_minimal_app_falls_back_to_the_registry_libs_field():
    """minimal-app has no vendored tree -- `vendored_library_names_for`
    returns None for it, so explain keeps reading the registry's own (empty)
    `libs` field for this one template."""
    assert vendored_library_names_for("minimal-app") is None
    assert "Default libraries: (none)" in details(template="minimal-app")


def test_features_default_to_all_false_lowercase():
    """Rust renders `Option<WizardFeatureFlags>::None` as all-false with
    lowercase bools; `str(True)` would emit `True`."""
    assert "Default features: wifi=false mqtt=false ble=false tls=false" in details(
        template="minimal-app"
    )


@pytest.mark.parametrize(
    "board_yaml,expected",
    [
        # The scoped spelling every vendored tree uses, with a sibling key and
        # an indented comment inside the block.
        ("libraries:\n  # note\n  - name: tflite-micro\n    cores: [m55_hp]\n", ["tflite-micro"]),
        # The bare shorthand `LibraryEntry` also accepts.
        ("libraries:\n  - mbedtls\n  - cmsis-dsp\n", ["mbedtls", "cmsis-dsp"]),
        # A quoted scalar keeps neither quote.
        ('libraries:\n  - name: "mbedtls"\n', ["mbedtls"]),
        # No block at all, and a block the next top-level key ends.
        ("som:\n  sku: X\n", []),
        ("libraries:\n  - mbedtls\nsom:\n  sku: X\n", ["mbedtls"]),
    ],
)
def test_library_name_scan_covers_both_entry_spellings(board_yaml, expected):
    assert _library_names(board_yaml) == expected


# --------------------------------------------------------------------------
# The catalogue (tan-cli#115/#165)
# --------------------------------------------------------------------------


def test_os_topology_is_explainable():
    """#115/#165: os-topology must be discoverable through `explain --target`,
    not just spawnable through `generate --target`."""
    result = resolve(None, "os-topology")
    assert result.kind == "generation-target"
    assert "OS topology" in result.summary
    assert "Output path: build/generated/os-topology.json" in result.details


def test_zephyr_board_is_explainable_and_flagged_as_a_directory():
    """#165: zephyr-board used to answer "Unknown generation target". Its
    directory shape must also be called out, not silently presented as a
    single file path."""
    result = resolve(None, "zephyr-board")
    assert "Zephyr board tree" in result.summary
    assert (
        "Output path: build/boards/alp_e1m_<sku-slug>_<core>/ (writes a directory "
        "of files, not one path)" in result.details
    )


def test_available_generation_targets_lists_all_ten():
    """#165's core regression: only 4 of 10 real generate targets used to be
    discoverable at all."""
    result = runner.invoke(app, ["explain", "--format", "json"])
    targets = json.loads(result.stdout)["data"]["available"]["generationTargets"]
    assert targets == [
        "zephyr-conf",
        "dts-overlay",
        "cmake-args",
        "yocto-conf",
        "native-sim-overlay",
        "carrier-netlist",
        "west-libraries",
        "hw-info-h",
        "os-topology",
        "zephyr-board",
    ]


#: Targets `generate` can emit that `explain` deliberately does NOT catalogue.
#:
#: `ipc-contract-h` is driven by `alp_sdk_ipc_contract_header()` in alp-sdk's
#: `cmake/alp.cmake`, not chosen by a human: `explain`'s catalogue feeds
#: `data.available.generationTargets`, which is the extension's target PICKER,
#: and a build-internal front door has no place in a menu. That list is also a
#: frozen conformance golden (`contract/envelopes/explain-overview`), so adding
#: it would be a UI change dressed up as a plumbing fix.
#:
#: `composed-route-table` is alp-sdk's own "demonstrator" debug view of
#: `carrier-netlist`'s route rows (`scripts/alp_project_emit/bom_netlist.py`'s
#: own docstring), not a shipped artefact any build or picker consumes -- the
#: same "not a human picker item" reasoning as `ipc-contract-h` -- and
#: `GENERATION_TARGETS` mirrors `crates/tan-core/src/loader.rs`'s
#: `GENERATION_TARGET_CATALOG` verbatim, which (frozen) has no entry for it.
#:
#: Named here rather than left to a set-difference so the exclusion is a
#: decision on the record: anything else that appears in `generate`'s table
#: without a catalogue entry still fails the drift check below.
UNCATALOGUED_GENERATE_TARGETS = frozenset({"ipc-contract-h", "composed-route-table"})


def test_catalogue_cannot_drift_from_what_generate_actually_emits():
    """The #165 failure was two structures disagreeing in silence: the emit
    list `generate` runs, and the catalogue `explain` describes. Nothing else
    couples them, so this does -- ids AND output paths, verbatim."""
    from tan.commands.generate_cmd import _OUTPUT_RELATIVE_PATH, ZEPHYR_BOARD

    described = {t.emit: t.output_relative_path for t in GENERATION_TARGETS}
    emitted = set(_OUTPUT_RELATIVE_PATH) | {ZEPHYR_BOARD}
    assert set(described) == emitted - UNCATALOGUED_GENERATE_TARGETS
    for emit, path in _OUTPUT_RELATIVE_PATH.items():
        if emit in UNCATALOGUED_GENERATE_TARGETS:
            continue
        assert described[emit] == path, emit


def test_an_uncatalogued_target_is_still_a_target_generate_can_emit():
    """The other half of the exclusion above: an entry that named a target
    `generate` cannot emit would be a stale exclusion silently masking drift in
    the direction the #165 guard exists to catch."""
    from tan.commands.generate_cmd import _OUTPUT_RELATIVE_PATH, ZEPHYR_BOARD

    emitted = set(_OUTPUT_RELATIVE_PATH) | {ZEPHYR_BOARD}
    assert UNCATALOGUED_GENERATE_TARGETS <= emitted


def test_template_ids_match_the_scaffolder_registry():
    """`explain --template <id>` must accept exactly what `init --template`
    does, in the same order (the overview line reports that order)."""
    from tan.core.scaffold import TEMPLATE_IDS

    assert tuple(t.id for t in PROJECT_TEMPLATES) == TEMPLATE_IDS


def test_module_template_is_explainable():
    result = resolve("inference-stage", None)
    assert result.kind == "module-template"
    assert result.summary == "Inference stage module (inference-stage)"
    assert result.details == [
        "Adds module skeleton for model pre/post processing path.",
        "Function prefix: alp_infer",
        "Use this template with tan scaffold to generate a module source/header "
        "baseline.",
    ]


# --------------------------------------------------------------------------
# Selectors and the failure contract
# --------------------------------------------------------------------------


def test_a_blank_selector_is_absent_not_unknown():
    """Rust trims and drops an empty selector, so `--template "  "` prints the
    overview rather than reporting an unknown template."""
    assert resolve("   ", None).value == "all"
    assert resolve(None, "\t").value == "all"


def test_both_selectors_at_once_is_an_error():
    result = runner.invoke(
        app,
        ["explain", "--template", "minimal-app", "--target", "zephyr-conf", "--format", "json"],
    )
    assert result.exit_code == 1
    doc = json.loads(result.stdout)
    assert doc["ok"] is False
    assert doc["issues"] == [
        {
            "code": "explain.ambiguous-selector",
            "severity": "error",
            "message": "Use either --template or --target for explain, not both.",
        }
    ]
    # The error asymmetry, which is contract: the selector reverts to
    # `overview` with an EMPTY value (two were named), summary/details go
    # empty, and `available` stays populated so the caller can recover.
    assert doc["data"]["selector"] == {"kind": "overview", "value": ""}
    assert doc["data"]["summary"] == ""
    assert doc["data"]["details"] == []
    assert doc["data"]["available"]["projectTemplates"]


@pytest.mark.parametrize(
    "argv,code,message,echoed",
    [
        (
            ["--template", "nope"],
            "explain.template-unknown",
            "Unknown template 'nope'.",
            "nope",
        ),
        (
            ["--target", "zephyr-cnf"],
            "explain.target-unknown",
            "Unknown generation target 'zephyr-cnf'.",
            "zephyr-cnf",
        ),
    ],
)
def test_an_unknown_id_is_exit_1_with_the_id_echoed(argv, code, message, echoed):
    result = runner.invoke(app, ["explain", *argv, "--format", "json"])
    assert result.exit_code == 1
    doc = json.loads(result.stdout)
    assert doc["issues"][0]["code"] == code
    assert doc["issues"][0]["message"] == message
    assert doc["data"]["selector"] == {"kind": "overview", "value": echoed}


def test_json_mode_writes_one_envelope_and_nothing_else():
    """The hard constraint: stdout is the envelope channel. A stray byte on
    either stream silently breaks the extension -- it renders nothing, with no
    error anywhere."""
    result = runner.invoke(app, ["explain", "--template", "iot-starter", "--format", "json"])
    assert result.exit_code == 0
    assert len(result.stdout.strip().splitlines()) == 1
    assert result.stderr == ""
    doc = json.loads(result.stdout)
    assert doc["command"] == "explain"
    assert doc["project"] == {"root": None, "boardYaml": None}
    # explain resolves no checkout, so `sdk` is absent -- never null.
    assert "sdk" not in doc
    assert list(doc["data"]) == [
        "schemaVersion",
        "selector",
        "summary",
        "details",
        "available",
    ]
    assert list(doc["data"]["available"]) == [
        "projectTemplates",
        "moduleTemplates",
        "generationTargets",
    ]


def test_text_mode_renders_to_stderr_only():
    """Rust `eprintln!`s every text line (`main.rs`'s `emit`), and stdout
    carries the envelope or nothing. `- ` prefixes each detail."""
    result = runner.invoke(app, ["explain", "--target", "hw-info-h"])
    assert result.exit_code == 0
    assert result.stdout == ""
    assert result.stderr.splitlines()[0] == "explain: Hardware info header (hw-info-h)"
    assert "- Preview language: c" in result.stderr


def test_text_mode_error_line_carries_its_own_prefix():
    result = runner.invoke(app, ["explain", "--template", "nope"])
    assert result.exit_code == 1
    assert result.stdout == ""
    assert result.stderr.strip() == (
        "explain: unknown template 'nope'. Run tan explain without selectors to "
        "list available topics."
    )


def test_bad_format_is_a_usage_error_not_a_traceback():
    result = runner.invoke(app, ["explain", "--format", "yaml"])
    assert result.exit_code == 2
    assert "Traceback" not in result.output


def test_an_unreadable_vendored_tree_is_a_coded_envelope_not_a_traceback(monkeypatch):
    """The port's recurring bug class. Rust `expect()`s this read and panics
    (exit 101, empty stdout); here it must be `explain.template-unreadable`
    with exit 5 -- a broken tan installation, not a project problem, matching
    `init.template-unreadable`."""
    import tan.commands.explain_cmd as mod

    monkeypatch.setattr(
        mod, "vendored_library_names_for", _raise_template_data_error
    )
    result = runner.invoke(
        app, ["explain", "--template", "edge-ai-starter", "--format", "json"]
    )
    assert result.exit_code == 5
    assert "Traceback" not in result.output
    doc = json.loads(result.stdout)
    assert doc["issues"][0]["code"] == "explain.template-unreadable"
    assert doc["data"]["selector"] == {"kind": "overview", "value": "edge-ai-starter"}
    assert doc["data"]["available"]["projectTemplates"]


def _raise_template_data_error(_template_id):
    from tan.core.scaffold import TemplateDataError

    raise TemplateDataError("vendored board.yaml could not be read at '<x>'")


def test_an_unexpected_exception_is_still_an_envelope(monkeypatch):
    """The backstop for the path nobody enumerated."""
    import tan.commands.explain_cmd as mod

    def boom(_template_id):
        raise ValueError("boom")

    monkeypatch.setattr(mod, "vendored_library_names_for", boom)
    result = runner.invoke(
        app, ["explain", "--template", "minimal-app", "--format", "json"]
    )
    assert result.exit_code == 5
    doc = json.loads(result.stdout)
    assert doc["issues"][0]["code"] == "explain.internal-failure"
    assert "ValueError: boom" in doc["issues"][0]["message"]


def test_positional_template_id_works_like_the_option():
    """`tan explain minimal-app` used to fail with a Click "unexpected extra
    argument(s)" usage error -- the positional this port now accepts as
    shorthand for `--template` (a port-only convenience; the oracle has no
    positional here, see `explain()`'s docstring)."""
    result = runner.invoke(app, ["explain", "minimal-app", "--format", "json"])
    assert result.exit_code == 0
    doc = json.loads(result.stdout)
    assert doc["data"]["selector"] == {"kind": "project-template", "value": "minimal-app"}


def test_positional_and_option_together_is_a_conflict_not_a_silent_pick():
    """scaffold-cx review, Finding 3: giving both the positional template id and
    `--template` used to silently keep `--template` and drop the positional --
    the accepted-but-ignored-input class this port keeps re-introducing (and
    strictly more permissive than the oracle, whose `ExplainArgs` has no
    positional at all). Both values must be named in a coded error instead."""
    result = runner.invoke(
        app, ["explain", "minimal-app", "--template", "sensor-starter", "--format", "json"]
    )
    assert result.exit_code == 1
    doc = json.loads(result.stdout)
    assert doc["ok"] is False
    issue = doc["issues"][0]
    assert issue["code"] == "explain.positional-template-conflict"
    assert "minimal-app" in issue["message"]
    assert "sensor-starter" in issue["message"]


def test_positional_alone_is_shorthand_for_template():
    result = runner.invoke(app, ["explain", "sensor-starter", "--format", "json"])
    assert result.exit_code == 0
    doc = json.loads(result.stdout)
    assert doc["data"]["selector"]["value"] == "sensor-starter"


def test_every_emitted_issue_code_is_namespaced_to_explain():
    """Mirrors Rust's `every_emitted_issue_code_is_registered`: the extension
    keys behaviour off these strings, so a typo'd namespace is silent."""
    codes = {
        "explain.ambiguous-selector",
        "explain.positional-template-conflict",
        "explain.template-unknown",
        "explain.target-unknown",
        "explain.template-unreadable",
        "explain.internal-failure",
    }
    import tan.commands.explain_cmd as mod

    source = Path(mod.__file__).read_text(encoding="utf-8")
    for code in codes:
        assert f'"{code}"' in source, code


def test_no_os_or_backend_flag_exists():
    """I-01/I-02: the OS follows each core's Cortex class and is never
    user-selectable. Neither flag may ever appear on any command."""
    for flag in ("--os", "--backend"):
        result = runner.invoke(app, ["explain", flag, "zephyr"])
        assert result.exit_code == 2, flag


def test_no_module_template_id_collides_with_a_project_template_id():
    """`resolve` searches project templates first; the two sets are disjoint
    today and this keeps the shadowing hypothetical."""
    assert not {t.id for t in PROJECT_TEMPLATES} & {t.id for t in MODULE_TEMPLATES}


# --------------------------------------------------------------------------
# The shared wrap seam (`tan.core.text_layout.wrap_lines` via
# `tan.env.wrap_width`), adopted here after `doctor` (PR #480). `iot-starter`
# is the fixture throughout: its "Real Zephyr app vendored..." explanation
# line is 207 columns unwrapped -- measured, the widest of any template.
#
# Every detail line wraps uniformly now -- an earlier version of this seam
# kept catalogue/field lines (`Generation targets: ...`, `Default libraries:
# ...`) exempt via a `_RECORD_DETAIL_PREFIXES` classification table, on the
# theory that a piped reader might grep them whole. That case cannot occur:
# `wrap_width()` returns a width only when stderr IS a tty, and any pipe
# that could grep these lines makes stderr not a tty, which already returns
# `None` and disables wrapping wholesale -- see
# `test_text_mode_does_not_wrap_off_a_terminal` below.
# --------------------------------------------------------------------------


def test_text_mode_does_not_wrap_off_a_terminal():
    """The one that matters: `CliRunner`'s stderr is never a tty (a
    `BytesIO`-backed stream), so this is the DEFAULT case every other test in
    this file already runs under -- asserted explicitly here against the
    widest known line so a future change to the wrap seam cannot silently
    start wrapping piped output and break `tan explain | grep`."""
    result = runner.invoke(app, ["explain", "--template", "iot-starter"])
    assert result.exit_code == 0
    long_line = (
        "- Real Zephyr app vendored from the SDK's `iot` scaffold: brings up "
        "Wi-Fi via the CC3501E bridge and publishes an mqtts:// (TLS) MQTT "
        "telemetry reading on a cadence, through the portable <alp/iot.h> surface."
    )
    assert long_line in result.stderr.splitlines()


def test_text_mode_wraps_prose_on_a_real_terminal(monkeypatch):
    """Forces `sys.stderr.isatty()` True through the REAL CLI dispatch (same
    technique as `test_doctor_command.py`'s `--no-color` test) and pins
    `shutil.get_terminal_size` so the wrap width is deterministic across
    hosts/CI rather than whatever `sys.__stdout__` happens to report."""
    import shutil

    from typer.testing import _NamedTextIOWrapper

    monkeypatch.setattr(_NamedTextIOWrapper, "isatty", lambda self: True)
    monkeypatch.setattr(shutil, "get_terminal_size", lambda **_: os.terminal_size((100, 24)))

    result = runner.invoke(app, ["explain", "--template", "iot-starter"])
    assert result.exit_code == 0
    lines = result.stderr.splitlines()
    # The 207-column line above is gone as ONE physical line...
    assert not any(len(line) > 100 for line in lines)
    # ...but its own text still appears, reassembled.
    assert "brings up Wi-Fi via the CC3501E bridge" in " ".join(lines)
    # The catalogue/field lines stay exactly one line each, never wrapped,
    # even though "Default libraries: mbedtls" style checks are moot here --
    # the record-shaped assertion that matters for THIS template is the
    # feature-flags line, still whole:
    assert "- Default features: wifi=true mqtt=true ble=false tls=true" in lines


def test_text_mode_wraps_the_generation_targets_catalogue_line_too(monkeypatch):
    """The overview's own "Generation targets: ..." line is 161 columns with
    all ten target ids listed -- measured, the widest line `tan explain`
    (bare) prints. Contract change from this task: this is no longer exempt
    from wrapping (see the section comment above) -- measured at width 100,
    it now splits into two physical lines (91 + 71 columns), and every
    target id survives intact on one or the other; none is broken mid-token
    the way a terminal's own destructive soft-wrap could split one."""
    import shutil

    from typer.testing import _NamedTextIOWrapper

    monkeypatch.setattr(_NamedTextIOWrapper, "isatty", lambda self: True)
    monkeypatch.setattr(shutil, "get_terminal_size", lambda **_: os.terminal_size((100, 24)))

    result = runner.invoke(app, ["explain"])
    assert result.exit_code == 0
    lines = result.stderr.splitlines()
    assert not any(len(line) > 100 for line in lines)

    # The "- Generation targets: ..." block: its own first line, plus every
    # continuation line after it up to the next "- "-prefixed detail (or
    # end of output).
    start = next(i for i, line in enumerate(lines) if line.startswith("- Generation targets:"))
    block = [lines[start]]
    for line in lines[start + 1 :]:
        if line.startswith("- "):
            break
        block.append(line)
    assert len(block) > 1, "the 161-column line must actually have split -- it did not"
    joined = " ".join(block)
    # Every target id from `GENERATION_TARGETS` (`_overview`'s source) is
    # present, whole -- none split mid-token across the line break.
    for target in GENERATION_TARGETS:
        assert target.emit in joined, (target.emit, block)


def test_text_mode_wraps_on_a_terminal_even_with_no_color(monkeypatch):
    """`--no-color` must NOT disable wrapping (`tan.env.wrap_width` takes no
    `no_color` argument at all, unlike `use_color`) -- a user who wants plain
    text still wants readable line breaks on a real terminal."""
    import shutil

    from typer.testing import _NamedTextIOWrapper

    monkeypatch.setattr(_NamedTextIOWrapper, "isatty", lambda self: True)
    monkeypatch.setattr(shutil, "get_terminal_size", lambda **_: os.terminal_size((100, 24)))

    result = runner.invoke(app, ["explain", "--template", "iot-starter", "--no-color"])
    assert result.exit_code == 0
    lines = result.stderr.splitlines()
    assert not any(len(line) > 100 for line in lines)
    assert "brings up Wi-Fi via the CC3501E bridge" in " ".join(lines)
