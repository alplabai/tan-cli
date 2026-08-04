# SPDX-License-Identifier: Apache-2.0
"""`tan new-som` -- CLI surface tests.

`new-som` is not registered in `tan.cli.app` by this change (the shared
`cli.py` registration point is owned by the orchestrator wiring commands in
parallel), so these tests mount the command on a throwaway `typer.Typer()`,
matching how it will actually be invoked once registered:
`app.command("new-som")(new_som)`.

Every test needs REAL `metadata/schemas/som-preset-v1.schema.json` /
`soc-spec-v1.schema.json` / `metadata/boards/*.yaml` to validate against, so
this whole module points `--sdk-root` at an alp-sdk checkout named by
**`ALP_SDK_ROOT`**, and skips (never fails) when that variable is unset or does
not name a checkout carrying `scripts/alp_cli/new_som.py`.

`ALP_SDK_ROOT` and not a literal path: this repo is PUBLIC, and a committed
absolute path is both a leak and a lie -- it makes the module skip itself into
vacuity on every machine except the one that wrote it, so the coverage silently
evaporates rather than failing. `tests/parity/` already uses `ALP_SDK_ROOT` for
exactly this, so the convention is the repo's, not this file's invention.

Skipping IS acceptable here, unlike a data-only guard: these tests need real
schemas and a real `metadata/boards/` set to cross-check board names against, so
there is nothing to pin literally in their place. Where a guard's subject IS
data -- `test_faultdecode.py`'s register bit tables -- it must be pinned inline
instead, precisely so it cannot skip.

A handful of tests additionally diff this command's output directly against
`alp_cli.new_som`'s own `click.testing` output, both dry-run (stdout only)
and real-write (byte-comparing the generated preset YAML / SoC JSON): the
oracle honours `--output-root` for both (`scripts/alp_cli/new_som.py:432,
525-528` derive `preset_path`/`soc_path` from it, same as this port), so a
real-write parity run into a scratch `--output-root` never touches the
oracle's own checkout.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from tan.commands.new_som_cmd import (
    ETHOS_U_VARIANTS,
    INFERENCE_BACKENDS,
    _split_cores_csv,
    _yaml_dquote,
    new_som,
)

app = typer.Typer()
app.command("new-som")(new_som)
runner = CliRunner()

_SDK_ROOT_ENV = os.environ.get("ALP_SDK_ROOT", "").strip()
_SDK_ROOT = Path(_SDK_ROOT_ENV) if _SDK_ROOT_ENV else None
_ORACLE_NEW_SOM = (
    _SDK_ROOT / "scripts" / "alp_cli" / "new_som.py" if _SDK_ROOT else None
)

pytestmark = pytest.mark.skipif(
    _ORACLE_NEW_SOM is None or not _ORACLE_NEW_SOM.is_file(),
    reason=(
        "set ALP_SDK_ROOT to an alp-sdk checkout that still ships "
        "scripts/alp_cli/new_som.py to run the new-som oracle-parity tests"
    ),
)


def _load_oracle_command():
    sdk_scripts = str(_SDK_ROOT / "scripts")
    added = sdk_scripts not in sys.path
    if added:
        sys.path.insert(0, sdk_scripts)
    try:
        spec = importlib.util.spec_from_file_location("_new_som_oracle_cli", _ORACLE_NEW_SOM)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module.new_som_cmd
    finally:
        if added:
            sys.path.remove(sdk_scripts)


def _normalise(text: str, *roots: Path) -> str:
    """Strip the `alp `/`tan ` prefix divergence (RFC #837) and any absolute
    scratch-dir path so oracle vs port output is comparable."""
    text = text.replace("alp new-som:", "new-som:")
    for root in roots:
        text = text.replace(str(root), "XDIR").replace(str(root).replace("\\", "/"), "XDIR")
    return text


def _normalise_content(text: str) -> str:
    """As `_normalise`, but for on-disk generated file content, which reads
    the backtick-quoted `` `alp new-som` `` (no trailing colon) rather than
    the stdout-only `alp new-som:` error prefix."""
    return text.replace("alp new-som", "tan new-som")


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_yaml_dquote_escapes_backslash_and_quote():
    assert _yaml_dquote(r'a "quoted" \ value') == r'a \"quoted\" \\ value'


def test_split_cores_csv_trims_and_drops_empties():
    assert _split_cores_csv(None, None, "core0, core1 ,, core2") == ("core0", "core1", "core2")


def test_split_cores_csv_none_stays_none():
    assert _split_cores_csv(None, None, None) is None


# ---------------------------------------------------------------------------
# Validation failures (exit 1, no filesystem writes)
# ---------------------------------------------------------------------------


def test_bad_sku_pattern_fails(tmp_path):
    result = runner.invoke(
        app,
        [
            "--dry-run",
            "--sdk-root",
            str(_SDK_ROOT),
            "--output-root",
            str(tmp_path),
            "--sku",
            "not-a-sku",
            "--soc-ref",
            "a:b:c",
            "--family",
            "fam",
        ],
    )
    assert result.exit_code == 1
    assert "must look like E1M-<UPPERCASE>" in result.output


def test_ethos_u_without_variant_fails(tmp_path):
    result = runner.invoke(
        app,
        [
            "--dry-run",
            "--sdk-root",
            str(_SDK_ROOT),
            "--output-root",
            str(tmp_path),
            "--sku",
            "E1M-XTST1",
            "--soc-ref",
            "a:b:c",
            "--family",
            "fam",
            "--inference-backend",
            "ethos_u",
        ],
    )
    assert result.exit_code == 1
    assert "requires --ethos-u-variant" in result.output


def test_unknown_inference_backend_is_a_usage_error(tmp_path):
    """Regression: `click_type=click.Choice(...)` on the `typer.Option` crashed
    with an uncaught `StopIteration` under this repo's pinned typer/click pair
    instead of a clean exit-2 usage error -- see `new_som`'s manual check."""
    result = runner.invoke(
        app,
        [
            "--dry-run",
            "--sdk-root",
            str(_SDK_ROOT),
            "--output-root",
            str(tmp_path),
            "--sku",
            "E1M-XTST1",
            "--soc-ref",
            "a:b:c",
            "--family",
            "fam",
            "--inference-backend",
            "bogus",
        ],
    )
    assert result.exit_code == 2
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert "not one of" in result.output


def test_default_board_must_be_known(tmp_path):
    result = runner.invoke(
        app,
        [
            "--dry-run",
            "--sdk-root",
            str(_SDK_ROOT),
            "--output-root",
            str(tmp_path),
            "--sku",
            "E1M-XTST1",
            "--soc-ref",
            "a:b:c",
            "--family",
            "fam",
            "--default-board",
            "NOT-A-REAL-BOARD",
        ],
    )
    assert result.exit_code == 1
    assert "does not match any `name:` in metadata/boards/" in result.output


def test_hw_rev_cross_checked_against_real_family_file(tmp_path):
    """E1M-AEN* resolves to the real `aen` family via a subprocess call into
    the target SDK's own `alp_project_loader._sku_family` (never reproduced
    in tan -- I-26)."""
    result = runner.invoke(
        app,
        [
            "--dry-run",
            "--sdk-root",
            str(_SDK_ROOT),
            "--output-root",
            str(tmp_path),
            "--sku",
            "E1M-AEN999",
            "--soc-ref",
            "alif:ensemble:e8",
            "--family",
            "alif-ensemble",
            "--default-board",
            "E1M-EVK",
            "--default-hw-rev",
            "not-a-real-rev",
        ],
    )
    assert result.exit_code == 1
    assert "does not resolve in" in result.output
    assert "hw-revisions.yaml" in result.output


def test_sdk_root_unresolved_fails_loud(tmp_path):
    """Exit code 2 (VALIDATION_FAILURE), not the flat 1 every other new-som
    failure uses: this is the ONE failure the port adds that the alp_cli
    original never had (it always ran from within a checkout), and it
    mirrors the Rust forwarder's own preflight
    (`sdk_cli.rs::run`) -- confirmed live: `tan.exe new-som --sdk-root <bad>`
    exits 2."""
    result = runner.invoke(
        app,
        [
            "--dry-run",
            "--sdk-root",
            str(tmp_path),  # empty dir -- no scripts/alp_project.py
            "--sku",
            "E1M-XTST1",
            "--soc-ref",
            "a:b:c",
            "--family",
            "fam",
        ],
    )
    assert result.exit_code == 2
    assert "alp-sdk root is unresolved" in result.output


def test_non_interactive_stdin_reports_the_missing_flags(tmp_path, monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    result = runner.invoke(
        app, ["--dry-run", "--sdk-root", str(_SDK_ROOT), "--output-root", str(tmp_path)]
    )
    assert result.exit_code == 1
    assert "--sku" in result.output and "--soc-ref" in result.output and "--family" in result.output


def test_default_board_default_resolves_against_real_sdk_boards(tmp_path):
    """`DEFAULT_BOARD` ("E1M-EVK") is a UI default only, never trusted --
    every accepted `--default-board`, INCLUDING the unedited default, is
    cross-checked against the real `metadata/boards/*.yaml` `name:` values
    (see the module docstring and the I-26 gate allowlist entry in
    `tests/gates/test_no_new_hardware_facts.py`). This omits
    `--default-board` entirely so the unedited literal is what gets
    cross-checked; a stale `DEFAULT_BOARD` must fail this test."""
    result = runner.invoke(
        app,
        [
            "--dry-run",
            "--sdk-root",
            str(_SDK_ROOT),
            "--output-root",
            str(tmp_path),
            "--sku",
            "E1M-XTST8",
            "--soc-ref",
            "test:testfam:testpart8",
            "--family",
            "test-fam",
            # --default-board deliberately omitted.
        ],
    )
    assert result.exit_code == 0, result.output


def test_output_root_pointing_at_a_regular_file_is_a_usage_error(tmp_path):
    """`click.Path(file_okay=False)` equivalent (`_check_output_root`):
    matches the oracle's exit=2 usage error for an `--output-root` that
    cannot possibly work (`scripts/alp_cli/new_som.py:432`), rather than a
    dry-run reporting success followed by a real-write failure."""
    not_a_dir = tmp_path / "im-a-file"
    not_a_dir.write_text("", encoding="utf-8")
    result = runner.invoke(
        app,
        [
            "--dry-run",
            "--sdk-root",
            str(_SDK_ROOT),
            "--output-root",
            str(not_a_dir),
            "--sku",
            "E1M-XTST9",
            "--soc-ref",
            "test:testfam:testpart9",
            "--family",
            "test-fam",
            "--default-board",
            "E1M-EVK",
        ],
    )
    assert result.exit_code == 2
    assert "is a file" in result.output


def test_full_global_flag_set_is_accepted_even_when_meaningless(tmp_path):
    """The oracle's clap `GlobalArgs` are `global = true`, so `new-som`
    accepts `--board-yaml`/`--target`/`--all`/`--verbose`/`--quiet`/
    `--no-color`/`--non-interactive`/`--ci`/`--format` even though it never
    reads any of them -- confirmed live: `tan.exe new-som --board-yaml x
    --target t --all --verbose --quiet --no-color --non-interactive --ci
    --format json --sdk-root <bad>` still reaches the SDK-root-unresolved
    failure, not a parse error. Regression for the Click "No such option"
    usage error (exit 2) this port used to raise for each of these instead
    (tan-cli#254)."""
    result = runner.invoke(
        app,
        [
            "--board-yaml", "x.yaml",
            "--target", "zephyr-conf",
            "--all",
            "--verbose",
            "--quiet",
            "--no-color",
            "--non-interactive",
            "--ci",
            "--format", "json",
            "--dry-run",
            "--sdk-root", str(_SDK_ROOT),
            "--output-root", str(tmp_path),
            "--sku", "E1M-XTST6",
            "--soc-ref", "test:testfam:testpart6",
            "--family", "test-fam",
            "--default-board", "E1M-EVK",
        ],
    )
    assert result.exit_code == 0, result.output


# ---------------------------------------------------------------------------
# Successful scaffold: dry-run and real write, in a scratch --output-root
# ---------------------------------------------------------------------------


def test_dry_run_writes_nothing_and_reports_the_plan(tmp_path):
    result = runner.invoke(
        app,
        [
            "--dry-run",
            "--sdk-root",
            str(_SDK_ROOT),
            "--output-root",
            str(tmp_path),
            "--sku",
            "E1M-XTST1",
            "--soc-ref",
            "test:testfam:testpart",
            "--family",
            "test-fam",
            "--default-board",
            "E1M-EVK",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Preset skeleton validates against som-preset-v1" in result.output
    assert "SoC spec skeleton validates against soc-spec-v1" in result.output
    assert "Would create" in result.output
    assert "Dry run -- validated OK, nothing was written." in result.output
    assert not (tmp_path / "metadata").exists()


def test_write_then_refuses_without_force_then_force_overwrites(tmp_path):
    argv = [
        "--sdk-root",
        str(_SDK_ROOT),
        "--output-root",
        str(tmp_path),
        "--sku",
        "E1M-XTST2",
        "--soc-ref",
        "test:testfam:testpart2",
        "--family",
        "test-fam",
        "--default-board",
        "E1M-EVK",
    ]
    first = runner.invoke(app, argv)
    assert first.exit_code == 0, first.output
    preset = tmp_path / "metadata" / "e1m_modules" / "E1M-XTST2.yaml"
    soc = tmp_path / "metadata" / "socs" / "test" / "testfam" / "testpart2.json"
    assert preset.is_file() and soc.is_file()
    assert "`tan new-som`" in preset.read_text(encoding="utf-8")

    second = runner.invoke(app, argv)
    assert second.exit_code == 1
    assert "already exists (pass --force to overwrite)" in second.output

    third = runner.invoke(app, [*argv, "--force"])
    assert third.exit_code == 0, third.output


def test_written_preset_and_soc_content_are_correct(tmp_path):
    """Real (non-dry-run) content assertions over the generated preset YAML
    and SoC JSON skeleton -- these files are the command's actual product;
    a dry-run-only check never reads a single rendered line."""
    result = runner.invoke(
        app,
        [
            "--sdk-root",
            str(_SDK_ROOT),
            "--output-root",
            str(tmp_path),
            "--sku",
            "E1M-XTST4",
            "--soc-ref",
            "test:testfam:testpart4",
            "--family",
            "test-fam",
            # --inference-backend deliberately omitted: exercises the "tbd"
            # default, so a render that ignores the argument and always
            # emits e.g. "drpai" is caught here directly (not only via the
            # oracle-parity test, which could coincidentally match a
            # hardcoded value passed explicitly).
            "--default-board",
            "E1M-EVK",
        ],
    )
    assert result.exit_code == 0, result.output

    preset_text = (tmp_path / "metadata" / "e1m_modules" / "E1M-XTST4.yaml").read_text(
        encoding="utf-8"
    )
    assert "preferred_backend:    tbd" in preset_text
    assert "silicon_variant: TBD" in preset_text
    assert "{ id: 0, reserved_for: alp_default_rpmsg }" in preset_text

    soc_doc = json.loads(
        (tmp_path / "metadata" / "socs" / "test" / "testfam" / "testpart4.json").read_text(
            encoding="utf-8"
        )
    )
    assert soc_doc["notes"], "SoC skeleton notes[] must not be empty"


def test_cores_option_produces_exactly_the_given_topology_keys(tmp_path):
    """Exercises the `--cores` Option's `callback=_split_cores_csv` wiring
    through the real typer CLI (not just the bare function): if the
    callback ever stopped firing, `cores` would stay a `str` and `for core
    in cores` would emit one topology key PER CHARACTER instead."""
    result = runner.invoke(
        app,
        [
            "--sdk-root",
            str(_SDK_ROOT),
            "--output-root",
            str(tmp_path),
            "--sku",
            "E1M-XTST7",
            "--soc-ref",
            "test:testfam:testpart7",
            "--family",
            "test-fam",
            "--default-board",
            "E1M-EVK",
            "--cores",
            "core_a,core_b",
        ],
    )
    assert result.exit_code == 0, result.output
    preset_text = (tmp_path / "metadata" / "e1m_modules" / "E1M-XTST7.yaml").read_text(
        encoding="utf-8"
    )
    assert "topology:\n  core_a: {}\n  core_b: {}\n" in preset_text


# ---------------------------------------------------------------------------
# Acceptance (tan-cli#254): a new SoC/vendor onboards with NO tan release.
# ---------------------------------------------------------------------------


def test_new_vendor_onboards_through_metadata_alone_with_no_tan_release(tmp_path):
    """The whole point of the metadata-driven porting kit: a vendor/SoC `tan`
    has never heard of scaffolds and validates through `new-som` + the SDK's
    schemas alone -- no `tan` source change, and so no `tan` release, is
    needed to onboard it.

    Proven two ways, not just exercised:

    1. A vendor/family/part triple invented FOR THIS TEST is asserted absent
       from `tan`'s own source tree first -- if onboarding this vendor needed
       special-casing, that string would already have to be there for the
       assertion below to hold, and it is not. (This is the same shape as
       `tests/gates/test_no_new_hardware_facts.py`'s allowlist gate, run here
       against one concrete, never-before-seen vendor rather than the fixed
       patterns that gate already knows to look for.)
    2. The scaffold this genuinely-new vendor produces validates against the
       REAL `som-preset-v1`/`soc-spec-v1` schemas end to end (dry-run AND a
       real write), the same as every other SKU in this file -- proving the
       whole `new-som` -> schema-validate -> `pr-metadata-validate` pipeline
       needs nothing vendor-specific to accept it.
    """
    novel_vendor, novel_family, novel_part = "quixotic", "novaspark", "ns1"
    tan_src = Path(__file__).resolve().parents[2] / "tan"
    hits = [
        p
        for p in tan_src.rglob("*.py")
        if novel_vendor in p.read_text(encoding="utf-8", errors="replace")
    ]
    assert not hits, (
        f"{novel_vendor!r} already appears in {hits} -- this proves nothing about "
        "onboarding a genuinely new vendor; pick a different invented slug"
    )

    soc_ref = f"{novel_vendor}:{novel_family}:{novel_part}"
    common = [
        "--sdk-root", str(_SDK_ROOT),
        "--sku", "E1M-QUIX1",
        "--soc-ref", soc_ref,
        "--family", f"{novel_vendor}-{novel_family}",
        "--default-board", "E1M-EVK",
    ]

    dry = runner.invoke(app, ["--dry-run", "--output-root", str(tmp_path / "dry"), *common])
    assert dry.exit_code == 0, dry.output
    assert "Preset skeleton validates against som-preset-v1" in dry.output
    assert "SoC spec skeleton validates against soc-spec-v1" in dry.output

    written = runner.invoke(app, ["--output-root", str(tmp_path / "written"), *common])
    assert written.exit_code == 0, written.output
    preset_path = tmp_path / "written" / "metadata" / "e1m_modules" / "E1M-QUIX1.yaml"
    soc_path = (
        tmp_path / "written" / "metadata" / "socs" / novel_vendor / novel_family
        / f"{novel_part}.json"
    )
    assert preset_path.is_file()
    assert soc_path.is_file()
    assert f"silicon: {soc_ref}" in preset_path.read_text(encoding="utf-8")
    assert json.loads(soc_path.read_text(encoding="utf-8"))["vendor"] == novel_vendor


def test_interactive_prompts_ask_same_questions_in_order(monkeypatch, tmp_path):
    """The `questionary` -> `click.prompt`/`click.Choice` swap (module
    docstring) is the riskiest divergence in the port; lock the same
    questions, in the same order, with the same defaults, reaching the
    rendered preset. `CliRunner`'s stdin wraps a `BytesIO` and never
    reports `isatty() == True` on its own, so that is patched at the
    `typer.testing` stream class (its own copy of click's, per
    `typer.testing.CliRunner.isolation`) to take the interactive branch."""
    monkeypatch.setattr("typer.testing._NamedTextIOWrapper.isatty", lambda self: True)
    result = runner.invoke(
        app,
        ["--sdk-root", str(_SDK_ROOT), "--output-root", str(tmp_path)],
        input="E1M-XINT1\ntest:tf:tp\ntfam\n\n\nethos_u\nu55\n\n\n\n",
    )
    assert result.exit_code == 0, result.output
    preset_text = (tmp_path / "metadata" / "e1m_modules" / "E1M-XINT1.yaml").read_text(
        encoding="utf-8"
    )
    assert "ethos_u_variant:      u55" in preset_text


# ---------------------------------------------------------------------------
# tan-cli#399: `--format json` is the standard envelope, on BOTH paths
# ---------------------------------------------------------------------------


def _json_argv(tmp_path, *extra):
    return [
        "--dry-run",
        "--format", "json",
        "--sdk-root", str(_SDK_ROOT),
        "--output-root", str(tmp_path),
        "--sku", "E1M-ZZ9999",
        "--soc-ref", "nxp:imx9:imx95",
        "--family", "nxp-imx9",
        *extra,
    ]


def test_format_json_success_emits_the_standard_envelope_with_the_planned_files(tmp_path):
    """`contract/README.md` states alp-sdk-vscode drives `tan <cmd> --format
    json` and hard-depends on `{command,ok,exitCode,project,data,issues}`.
    `new-som` accepted the flag and printed the same 1238 bytes of human text
    either way -- `json.load(stdout)` threw `JSONDecodeError: Expecting value:
    line 1 column 1`, the extension's `isEnvelope` guard failed OPEN, and the
    panel that should list the two planned files rendered `Command completed.`

    The file lists are the assertion, not just the envelope shape: `data`
    being unreachable is the whole reported impact."""
    result = runner.invoke(app, _json_argv(tmp_path))
    assert result.exit_code == 0, result.output

    env = json.loads(result.output)
    assert env["command"] == "new-som"
    assert env["ok"] is True and env["exitCode"] == 0
    assert set(env) >= {"command", "ok", "exitCode", "project", "data", "issues"}
    assert env["data"]["dryRun"] is True
    assert [Path(p).name for p in env["data"]["planned"]] == ["E1M-ZZ9999.yaml", "imx95.json"]
    assert env["data"]["nextSteps"], env["data"]


def test_format_json_failure_emits_a_new_som_code_not_cli_parse_error(tmp_path):
    """The failure path DID produce parseable JSON before the fix -- from
    `cli.py`'s generic usage-error fallback, as `command: "cli"` with
    `cli.parse-error`, a code `contract/issue-codes.json` records as
    `status: "reserved"`, `consumer: "none"`. So a consumer dispatching on the
    command name had nothing to dispatch on for a refusal that really
    happened.

    `new-som.failed` is the ORACLE's own spelling, measured not read:
    `target/debug/tan.exe new-som --format json` with no SDK resolvable
    answers `command: "new-som"`, `data: {"subcommand":"new-som"}`,
    `issues: [{"code":"new-som.failed",...}]`, exit 2. This pins the whole
    shape against it, message excluded (the port's wording adds a `git clone`
    suggestion the oracle never had -- a separate, pre-existing divergence)."""
    empty = tmp_path / "not-an-sdk"
    empty.mkdir()
    result = runner.invoke(
        app,
        ["--dry-run", "--format", "json", "--sdk-root", str(empty),
         "--sku", "E1M-ZZ9999", "--soc-ref", "nxp:imx9:imx95", "--family", "nxp-imx9"],
    )
    assert result.exit_code == 2, result.output

    env = json.loads(result.output)
    assert env["command"] == "new-som", env
    assert env["ok"] is False and env["exitCode"] == 2
    assert env["data"] == {"subcommand": "new-som"}
    assert [i["code"] for i in env["issues"]] == ["new-som.failed"], env
    assert "alp-sdk root is unresolved" in env["issues"][0]["message"]


def test_the_success_and_failure_paths_agree_on_whether_stdout_is_json(tmp_path):
    """The exact disagreement #399 reports: one path parseable, the other not,
    on the same command with the same flag. Asserted as a PAIR so a fix to
    either half alone cannot pass."""
    empty = tmp_path / "not-an-sdk"
    empty.mkdir()
    ok = runner.invoke(app, _json_argv(tmp_path / "out"))
    bad = runner.invoke(
        app,
        ["--dry-run", "--format", "json", "--sdk-root", str(empty),
         "--sku", "E1M-ZZ9999", "--soc-ref", "nxp:imx9:imx95", "--family", "nxp-imx9"],
    )
    for label, result in (("success", ok), ("failure", bad)):
        env = json.loads(result.output)  # a raise here IS the failure
        assert env["command"] == "new-som", (label, env)
        assert env["exitCode"] == result.exit_code, (label, env, result.exit_code)


def test_text_mode_is_untouched_and_carries_no_envelope(tmp_path):
    """The other side of the same fix: `--format json` gaining an envelope must
    not put one on the DEFAULT path. Text mode is byte-compared against the
    alp_cli original elsewhere in this file; this is the cheap direct guard."""
    result = runner.invoke(app, [a for a in _json_argv(tmp_path) if a not in ("--format", "json")])
    assert result.exit_code == 0, result.output
    assert result.output.lstrip().startswith("Preset skeleton validates"), result.output
    assert '"command"' not in result.output, result.output


# ---------------------------------------------------------------------------
# Oracle parity (skipped when the SDK sibling is absent)
# ---------------------------------------------------------------------------


def test_matches_oracle_output_non_dry_run(tmp_path):
    """Non-dry-run parity: both commands write into SEPARATE tmp
    `--output-root` trees; the generated preset YAML and SoC JSON skeleton
    must be byte-identical after the `alp new-som` -> `tan new-som`
    substitution. The oracle's own `--output-root` (`click.Path(file_okay=
    False, path_type=Path)`) makes this safe -- neither run touches the
    oracle's own checkout tree."""
    oracle_root = tmp_path / "oracle"
    port_root = tmp_path / "port"
    oracle_cmd = _load_oracle_command()

    from click.testing import CliRunner as ClickRunner

    args = [
        "--sku",
        "E1M-XTST5",
        "--soc-ref",
        "test:testfam:testpart5",
        "--family",
        "test-fam",
        "--default-board",
        "E1M-EVK",
    ]
    oracle_result = ClickRunner().invoke(oracle_cmd, ["--output-root", str(oracle_root), *args])
    port_result = runner.invoke(
        app, ["--sdk-root", str(_SDK_ROOT), "--output-root", str(port_root), *args]
    )

    assert oracle_result.exit_code == 0, oracle_result.output
    assert port_result.exit_code == 0, port_result.output

    oracle_preset = (oracle_root / "metadata" / "e1m_modules" / "E1M-XTST5.yaml").read_text(
        encoding="utf-8"
    )
    port_preset = (port_root / "metadata" / "e1m_modules" / "E1M-XTST5.yaml").read_text(
        encoding="utf-8"
    )
    assert _normalise_content(oracle_preset) == port_preset

    oracle_soc = (
        oracle_root / "metadata" / "socs" / "test" / "testfam" / "testpart5.json"
    ).read_text(encoding="utf-8")
    port_soc = (
        port_root / "metadata" / "socs" / "test" / "testfam" / "testpart5.json"
    ).read_text(encoding="utf-8")
    assert _normalise_content(oracle_soc) == port_soc


@pytest.mark.parametrize(
    "args",
    [
        [
            "--sku", "E1M-XTST3", "--soc-ref", "test:testfam:testpart3",
            "--family", "test-fam", "--default-board", "E1M-EVK",
        ],
        [
            "--sku", "badsku", "--soc-ref", "a:b:c", "--family", "fam",
        ],
        [
            "--sku", "E1M-XTST3", "--soc-ref", "BADREF", "--family", "fam",
        ],
        [
            "--sku", "E1M-XTST3", "--soc-ref", "a:b:c", "--family", "fam",
            "--default-board", "NOT-A-REAL-BOARD",
        ],
    ],
)
def test_matches_oracle_output(tmp_path, args):
    oracle_root = tmp_path / "oracle"
    port_root = tmp_path / "port"
    oracle_cmd = _load_oracle_command()

    from click.testing import CliRunner as ClickRunner

    oracle_result = ClickRunner().invoke(
        oracle_cmd, ["--dry-run", "--output-root", str(oracle_root), *args]
    )
    port_result = runner.invoke(
        app,
        ["--dry-run", "--sdk-root", str(_SDK_ROOT), "--output-root", str(port_root), *args],
    )

    assert oracle_result.exit_code == port_result.exit_code
    assert _normalise(oracle_result.output, oracle_root) == _normalise(
        port_result.output, port_root
    )


def test_inference_backends_and_ethos_u_variants_match_the_oracle():
    """The choice tuples themselves are the frozen surface -- a silent
    reorder or typo here would desync every `--inference-backend`/
    `--ethos-u-variant` choice list from the oracle's."""
    assert INFERENCE_BACKENDS == ("ethos_u", "drpai", "deepx_dxm1", "tbd")
    assert ETHOS_U_VARIANTS == ("u55", "u65", "u85")
