# SPDX-License-Identifier: Apache-2.0
"""`tan new-som` -- CLI surface tests.

`new-som` is not registered in `tan.cli.app` by this change (the shared
`cli.py` registration point is owned by the orchestrator wiring commands in
parallel), so these tests mount the command on a throwaway `typer.Typer()`,
matching how it will actually be invoked once registered:
`app.command("new-som")(new_som)`.

Most tests here need REAL `metadata/schemas/som-preset-v1.schema.json` /
`soc-spec-v1.schema.json` / `metadata/boards/*.yaml` to validate against, so
they point `--sdk-root` at an alp-sdk checkout named by **`ALP_SDK_ROOT`** and
carry `@needs_oracle_sdk`, which skips (never fails) when that variable is
unset or does not name a checkout carrying `scripts/alp_cli/new_som.py`.

The `--format json` ENVELOPE cases do not, and must not: that shape is the
contract alp-sdk-vscode parses (tan-cli#399), and it has to be asserted on
every machine and in CI, not only where a checkout happens to sit. They scaffold
a throwaway marker checkout under `tmp_path` (`scripts/alp_project.py` plus
two permissive schemas and one board file) -- enough for the command to reach
its output, which is all a shape assertion needs.

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

import tan.commands.new_som_cmd as new_som_cmd_module
from tan.commands.new_som_cmd import (
    DEFAULT_BOARD,
    ETHOS_U_VARIANTS,
    INFERENCE_BACKENDS,
    _split_cores_csv,
    _yaml_dquote,
    _yaml_scalar,
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

#: Applied PER TEST, not as a module-level `pytestmark` (tan-cli#399). It was
#: module-level until the envelope work needed a case that must never skip:
#: `--format json`'s wire shape is a CONTRACT with alp-sdk-vscode, and a
#: contract test that evaporates on every machine without an alp-sdk checkout
#: -- including CI's `python` job -- is exactly the "silence reading as
#: coverage" failure `contract/README.md` names. The envelope cases below
#: build their own throwaway checkout under `tmp_path` instead: they assert
#: the SHAPE tan puts on stdout, which needs no real schema to be true.
#: Everything that diffs against the oracle, or that needs real
#: `metadata/schemas`/`metadata/boards` content, still carries this.
needs_oracle_sdk = pytest.mark.skipif(
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


def test_yaml_scalar_round_trips_plain_and_quoted_values():
    """tan-cli#496 defect 3: `_yaml_scalar` is PyYAML's own emitter choosing
    the quoting style, not a second hand-rolled implementation like
    `_yaml_dquote` above -- every value here must read back byte-identical
    through `yaml.safe_load`, whatever style the emitter picked."""
    import yaml

    for value in ("r1", "E1M-EVK rev 2", 'a "quoted" \\ value', "", "yes", "1.0", "r1: x"):
        scalar = _yaml_scalar(value)
        assert "\n" not in scalar
        assert yaml.safe_load(f"key: {scalar}")["key"] == value


def test_yaml_scalar_refuses_a_newline():
    with pytest.raises(ValueError, match="control characters"):
        _yaml_scalar("r1\nsku: E1M-AEN801")


def test_split_cores_csv_trims_and_drops_empties():
    assert _split_cores_csv(None, None, "core0, core1 ,, core2") == ("core0", "core1", "core2")


def test_split_cores_csv_none_stays_none():
    assert _split_cores_csv(None, None, None) is None


# ---------------------------------------------------------------------------
# Validation failures (exit 1, no filesystem writes)
# ---------------------------------------------------------------------------


@needs_oracle_sdk
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


@needs_oracle_sdk
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


@needs_oracle_sdk
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


@needs_oracle_sdk
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


@needs_oracle_sdk
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


@needs_oracle_sdk
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


@needs_oracle_sdk
def test_non_interactive_stdin_reports_the_missing_flags(tmp_path, monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    result = runner.invoke(
        app, ["--dry-run", "--sdk-root", str(_SDK_ROOT), "--output-root", str(tmp_path)]
    )
    assert result.exit_code == 1
    assert "--sku" in result.output and "--soc-ref" in result.output and "--family" in result.output


def test_sys_stdin_none_refuses_cleanly_instead_of_an_attributeerror(tmp_path, monkeypatch):
    """tan-cli#496: the missing-flags gate dereferenced `sys.stdin.isatty()`
    bare. `sys.stdin` is `None` under a console-less launcher (a frozen
    build run with no console, or any host that closes standard streams
    rather than redirecting them to `os.devnull`) -- `None.isatty()` is an
    `AttributeError`: an unhandled traceback instead of this command's own
    coded refusal, in exactly the "no real terminal" case this check exists
    to name cleanly.

    Called directly, not through `CliRunner`: `typer.testing`'s own
    `isolation()` unconditionally installs a real (non-`None`) stdin
    wrapper the instant a command is invoked through it, so it can never
    reproduce this condition. Every optional flag below is passed at
    Click's own "omitted" value (`typer.Option`'s first positional arg,
    i.e. exactly what Click passes when a flag is absent from argv), so
    this is equivalent to `tan new-som --sdk-root ... --output-root ...
    --dry-run` with nothing else on the command line.
    """
    sdk = _marker_sdk(tmp_path / "sdk")
    monkeypatch.setattr(new_som_cmd_module.sys, "stdin", None)

    with pytest.raises(typer.Exit) as exc_info:
        new_som_cmd_module.new_som(
            sku=None,
            soc_ref=None,
            family=None,
            vendor=None,
            display_name=None,
            inference_backend=None,
            ethos_u_variant=None,
            cores=None,
            default_board=None,
            default_hw_rev=None,
            output_root=str(tmp_path / "out"),
            dry_run=True,
            force=False,
            project=None,
            sdk_root=str(sdk),
            board_yaml=None,
            target=None,
            all_targets=False,
            output_format=None,
            verbose=False,
            quiet=False,
            no_color=False,
            non_interactive=False,
            ci=False,
        )
    assert exc_info.value.exit_code == 1


@needs_oracle_sdk
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


@needs_oracle_sdk
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


@needs_oracle_sdk
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


@needs_oracle_sdk
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


@needs_oracle_sdk
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


@needs_oracle_sdk
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


@needs_oracle_sdk
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


@needs_oracle_sdk
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


@needs_oracle_sdk
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


@needs_oracle_sdk
def test_interactive_prompts_go_to_stderr_not_stdout(monkeypatch, tmp_path):
    """tan-cli#496 defect 6: `click.prompt`'s OWN default is `err=False` --
    the question text goes to stdout. `typer.testing.CliRunner` captures
    stdout and stderr SEPARATELY (`Result.stdout`/`Result.stderr`) even
    though `Result.output` mixes them for a human reading test output, so
    this is provable without a second runner: click's own `prompt()`
    writes the question via `echo(text, nl=False, err=err)` -- see
    `click.termui.prompt` -- so `err=True` on every `_interactive` call
    (the fix) means the question text lands in `.stderr`, never `.stdout`.
    Before the fix, `Module name: `-shaped text corrupted whatever a
    redirected/piped stdout was carrying, matching the oracle's
    `inquire::Text`, which renders to stderr."""
    monkeypatch.setattr("typer.testing._NamedTextIOWrapper.isatty", lambda self: True)
    result = runner.invoke(
        app,
        ["--sdk-root", str(_SDK_ROOT), "--output-root", str(tmp_path)],
        input="E1M-XINT2\ntest:tf:tp\ntfam\n\n\nethos_u\nu55\n\n\n\n",
    )
    assert result.exit_code == 0, result.output
    # The question text, not the echoed answer (`visible_prompt_func`'s own
    # readline-workaround space still lands on stdout regardless of `err` --
    # see `click.termui.prompt`'s `prompt_func` -- so this checks the
    # QUESTION, the part `err` actually routes).
    assert "New SoM SKU" not in result.stdout
    assert "New SoM SKU" in result.stderr
    assert "Silicon triple-colon ref" in result.stderr
    assert "Default hw rev" in result.stderr


# ---------------------------------------------------------------------------
# tan-cli#399: `--format json` is the standard envelope, on BOTH paths
# ---------------------------------------------------------------------------


def _json_argv(tmp_path, *extra):
    """A THROWAWAY marker checkout (`_marker_sdk`), never `_SDK_ROOT`: these
    envelope-shape cases run unconditionally (no `@needs_oracle_sdk`, see the
    module docstring), so pointing at `_SDK_ROOT` -- `None` on a machine with
    no `ALP_SDK_ROOT` -- resolved `--sdk-root None`, an unresolvable path, and
    every caller of this helper hit the SDK-root-unresolved refusal instead of
    the success envelope it meant to exercise."""
    sdk = _marker_sdk(tmp_path / "sdk")
    return [
        "--dry-run",
        "--format", "json",
        "--sdk-root", str(sdk),
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
    `target/debug/tan.exe new-som --format json --sdk-root <bad>` answers
    `command: "new-som"`, `data: {"subcommand":"new-som"}`,
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


@needs_oracle_sdk
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
@needs_oracle_sdk
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


# ---------------------------------------------------------------------------
# `--format json` emits the standard envelope (tan-cli#399)
# ---------------------------------------------------------------------------
#
# Hermetic on purpose -- see the module docstring. These assert the SHAPE tan
# writes to stdout, which is the half of the contract `contract/README.md`
# says the extension hard-depends on and which no committed golden covers for
# this verb (there is no `new-som` conformance fixture: it needs a checkout to
# scaffold into).

#: `--format json`'s required top-level keys, verbatim from
#: `contract/README.md:4-8` and from the extension's own `isEnvelope` guard
#: (`alp-sdk-vscode src/alpCli/service.ts:705-716`, which tests `command`,
#: `ok`, `exitCode` and `Array.isArray(issues)`).
_ENVELOPE_KEYS = {"command", "ok", "exitCode", "project", "data", "issues"}


def _marker_sdk(root: Path) -> Path:
    """A throwaway alp-sdk checkout: the `scripts/alp_project.py` marker
    (`sdk_cmd.SDK_MARKER`, invariant I-31), permissive schemas, and one board
    whose `name:` is the command's own `DEFAULT_BOARD`.

    The schemas are deliberately permissive rather than copies of the real
    ones: pinning real schema CONTENT here would duplicate a hardware fact
    alp-sdk owns (I-26) and would rot on the SDK's next schema change, while
    proving nothing about the envelope. `@needs_oracle_sdk` covers skeleton
    validity against the real schemas.
    """
    (root / "scripts").mkdir(parents=True, exist_ok=True)
    (root / "scripts" / "alp_project.py").write_text("", encoding="utf-8")
    schemas = root / "metadata" / "schemas"
    schemas.mkdir(parents=True, exist_ok=True)
    (schemas / "som-preset-v1.schema.json").write_text(
        json.dumps({"type": "object", "properties": {"sku": {"pattern": "^E1M-[A-Z0-9-]+$"}}}),
        encoding="utf-8",
    )
    (schemas / "soc-spec-v1.schema.json").write_text(
        json.dumps({"type": "object"}), encoding="utf-8"
    )
    boards = root / "metadata" / "boards"
    boards.mkdir(parents=True, exist_ok=True)
    (boards / "stock.yaml").write_text(f"name: {DEFAULT_BOARD}\n", encoding="utf-8")
    return root


def _marker_sdk_no_boards(root: Path) -> Path:
    """Like `_marker_sdk`, but WITHOUT `metadata/boards/` -- `_known_board_names`
    then returns `None` (its own "not resolvable" branch), so an arbitrary
    `--default-board` reaches `_render_preset` unchecked against a known-name
    set, the same as a checkout mid-port that has not declared any boards
    yet. Needed to reach `_yaml_scalar`/the render self-check with a
    long/hostile `--default-board` at all: `_marker_sdk`'s one real board
    name would otherwise refuse any other value long before render
    (tan-cli#496 round-2 blockers 1+2)."""
    (root / "scripts").mkdir(parents=True, exist_ok=True)
    (root / "scripts" / "alp_project.py").write_text("", encoding="utf-8")
    schemas = root / "metadata" / "schemas"
    schemas.mkdir(parents=True, exist_ok=True)
    (schemas / "som-preset-v1.schema.json").write_text(
        json.dumps({"type": "object", "properties": {"sku": {"pattern": "^E1M-[A-Z0-9-]+$"}}}),
        encoding="utf-8",
    )
    (schemas / "soc-spec-v1.schema.json").write_text(
        json.dumps({"type": "object"}), encoding="utf-8"
    )
    return root


def _dry_run_argv(sdk: Path, out: Path, *extra: str) -> list[str]:
    return [
        "--sku", "E1M-XTST1",
        "--soc-ref", "nxp:imx9:imx95",
        "--family", "nxp-imx9",
        "--dry-run",
        "--sdk-root", str(sdk),
        "--output-root", str(out),
        *extra,
    ]


def test_format_json_dry_run_emits_a_new_som_envelope(tmp_path):
    """The success path used to write 1238 bytes of prose that
    `json.load` refused at column 1, so the extension's `parseEnvelope`
    returned `null`, `classifyOutcome` saw rc 0 and the panel that should list
    the two planned files rendered the literal string `Command completed.`
    """
    sdk = _marker_sdk(tmp_path / "sdk")
    out = tmp_path / "out"
    result = runner.invoke(app, _dry_run_argv(sdk, out, "--format", "json"))
    assert result.exit_code == 0, result.output

    payload = json.loads(result.stdout)
    assert _ENVELOPE_KEYS <= payload.keys()
    assert payload["command"] == "new-som"
    assert payload["ok"] is True
    assert payload["exitCode"] == 0
    assert payload["issues"] == []
    data = payload["data"]
    assert data["dryRun"] is True
    assert data["written"] == []
    assert data["planned"] == [
        str(out / "metadata" / "e1m_modules" / "E1M-XTST1.yaml"),
        str(out / "metadata" / "socs" / "nxp" / "imx9" / "imx95.json"),
    ]
    # The checklist is the command's real product for a human; it must survive
    # into `data` rather than being the thing only the text mode ever saw.
    assert any("never invent values" in step for step in data["nextSteps"])


def test_format_json_write_reports_the_files_it_actually_created(tmp_path):
    """`planned` and `written` are distinct keys because a dry run plans
    without writing; a real run must fill both, and the paths must be the ones
    on disk."""
    sdk = _marker_sdk(tmp_path / "sdk")
    out = tmp_path / "out"
    argv = [a for a in _dry_run_argv(sdk, out, "--format", "json") if a != "--dry-run"]
    result = runner.invoke(app, argv)
    assert result.exit_code == 0, result.output

    data = json.loads(result.stdout)["data"]
    assert data["dryRun"] is False
    assert data["written"] == data["planned"]
    assert [Path(p).is_file() for p in data["written"]] == [True, True]


def test_write_failure_rollback_never_escapes_and_reports_cleanup_json(tmp_path):
    """tan-cli#496: a SECOND `OSError` during the write-failure rollback --
    here a `NotADirectoryError` because a path component below
    `--output-root` (`metadata/socs`) is a regular file, so the SoC-spec
    write fails and its own cleanup attempt fails the identical way -- used
    to escape the handler entirely: `new_som` has no `except Exception`
    backstop, so the run crashed with a raw traceback instead of ever
    reaching `fail()` -- exit 1 and ZERO bytes on stdout under `--format
    json` (the `json.loads(result.stdout)` below IS that assertion: it
    raises on the pre-fix bug). The already-written preset was also left on
    disk non-deterministically (the old cleanup loop iterated a `set`,
    order depending on `PYTHONHASHSEED`); this asserts it is gone
    deterministically instead.

    Also the finding against THIS FILE's own earlier fix: `imx95.json`
    (`soc_path`) is never physically created in this scenario -- `mkdir()`
    on its parent fails structurally before any bytes land -- so it must
    NOT be named as a cleanup failure ("could not remove: ...imx95.json").
    The old cleanup loop attempted `unlink()` on it anyway, got a SECOND
    `NotADirectoryError` (only `FileNotFoundError` is `missing_ok`), and
    told the caller "this may be a half-written scaffold" for a path that
    was never written at all. `_rollback_write_failure` gates every
    candidate on `Path.exists()` (which swallows exactly this class of
    `OSError` and reports `False`), so the message here is the clean
    "rolled back any partial output" -- there is nothing left to report.
    """
    sdk = _marker_sdk(tmp_path / "sdk")
    out = tmp_path / "out"
    (out / "metadata").mkdir(parents=True)
    (out / "metadata" / "socs").write_text("not a directory", encoding="utf-8")
    argv = [a for a in _dry_run_argv(sdk, out, "--format", "json") if a != "--dry-run"]

    result = runner.invoke(app, argv)

    # `typer.Exit`/`SystemExit` from `fail()` is the EXPECTED clean exit; any
    # OTHER exception class reaching here is exactly the pre-fix escape.
    assert result.exception is None or isinstance(result.exception, SystemExit), (
        result.exception,
        result.output,
    )
    assert result.exit_code == 1, result.output
    payload = json.loads(result.stdout)
    assert payload["command"] == "new-som"
    assert payload["ok"] is False
    assert payload["exitCode"] == 1
    assert [i["code"] for i in payload["issues"]] == ["new-som.failed"]
    message = payload["issues"][0]["message"]
    assert "could not write the skeletons" in message
    assert "rolled back any partial output" in message
    assert "imx95.json" not in message, "never created -- must not be reported as uncleanable"
    assert "cleanup also failed" not in message
    preset_path = out / "metadata" / "e1m_modules" / "E1M-XTST1.yaml"
    assert not preset_path.exists(), "rollback must not leave a half-written preset behind"


def test_write_failure_rollback_never_escapes_and_reports_in_text_mode(tmp_path):
    """The DEFAULT text mode side of tan-cli#496: before the fix, the same
    double-fault crashed with a raw Python traceback and no `new-som:`
    prefixed line at all -- reported nowhere a script or a human could parse
    it, in EITHER output mode, not only under `--format json`."""
    sdk = _marker_sdk(tmp_path / "sdk")
    out = tmp_path / "out"
    (out / "metadata").mkdir(parents=True)
    (out / "metadata" / "socs").write_text("not a directory", encoding="utf-8")
    argv = [a for a in _dry_run_argv(sdk, out) if a != "--dry-run"]

    result = runner.invoke(app, argv)

    assert result.exception is None or isinstance(result.exception, SystemExit), (
        result.exception,
        result.output,
    )
    assert result.exit_code == 1, result.output
    assert "new-som: could not write the skeletons" in result.output
    assert "imx95.json" not in result.output, "never created -- must not be reported"
    preset_path = out / "metadata" / "e1m_modules" / "E1M-XTST1.yaml"
    assert not preset_path.exists(), "rollback must not leave a half-written preset behind"


def test_force_rollback_restores_a_pre_existing_preset_instead_of_deleting_it(tmp_path):
    """tan-cli#496 defect 2: the write-failure rollback used to unlink
    `preset_path` UNCONDITIONALLY, so a `--force` re-scaffold whose SoC-spec
    write then fails destroyed a pre-existing, hand-filled preset and left
    NOTHING in its place -- turning "clobbered" into "deleted" with no copy
    taken and none restored. This pre-creates a preset holding content that
    is obviously not the generated skeleton, forces the identical
    `NotADirectoryError` double-fault the other rollback tests use (a
    regular file at `<output-root>/metadata/socs`), and asserts the
    ORIGINAL bytes are back on disk afterward -- not deleted, not left as
    the clobbering skeleton."""
    sdk = _marker_sdk(tmp_path / "sdk")
    out = tmp_path / "out"
    preset_path = out / "metadata" / "e1m_modules" / "E1M-XTST1.yaml"
    preset_path.parent.mkdir(parents=True)
    original = "REAL HAND-EDITED PRESET WITH AUTHORITATIVE HW FACTS\n"
    preset_path.write_text(original, encoding="utf-8")
    (out / "metadata" / "socs").write_text("not a directory", encoding="utf-8")
    argv = [
        a
        for a in _dry_run_argv(sdk, out, "--format", "json", "--force")
        if a != "--dry-run"
    ]

    result = runner.invoke(app, argv)

    assert result.exception is None or isinstance(result.exception, SystemExit), (
        result.exception,
        result.output,
    )
    assert result.exit_code == 1, result.output
    payload = json.loads(result.stdout)
    assert [i["code"] for i in payload["issues"]] == ["new-som.failed"]
    assert preset_path.is_file(), "the pre-existing preset must not be deleted"
    assert preset_path.read_text(encoding="utf-8") == original, (
        "a failed --force re-scaffold must restore the ORIGINAL preset, not "
        "leave the clobbering skeleton or an empty/missing file"
    )


def test_cores_duplicate_id_is_refused_not_a_silent_duplicate_key(tmp_path):
    """tan-cli#496 defect 4: a repeated `--cores` id used to reach both
    skeletons unnoticed -- a duplicate `<id>: {}` mapping key in the
    preset's `topology:` block (PyYAML keeps the last; alp-sdk's own
    `validate_metadata.py` strict loader rejects it outright) and two
    identical `cores[]` rows in the SoC JSON -- reported as a clean success
    by both schema self-checks. `tan init`'s sibling parser
    (`tan.core.scaffold.parse_cores`) already refuses this; new-som must
    too, and refuse BEFORE anything is written."""
    sdk = _marker_sdk(tmp_path / "sdk")
    out = tmp_path / "out"
    argv = [
        a
        for a in _dry_run_argv(sdk, out, "--format", "json", "--cores", "m33_a,m33_a")
        if a != "--dry-run"
    ]

    result = runner.invoke(app, argv)

    assert result.exit_code == 1, result.output
    payload = json.loads(result.stdout)
    assert [i["code"] for i in payload["issues"]] == ["new-som.failed"]
    assert "duplicate" in payload["issues"][0]["message"].lower()
    assert "m33_a" in payload["issues"][0]["message"]
    assert not (out / "metadata").exists(), "a refused --cores must write nothing"


def test_default_hw_rev_multiline_injection_is_refused(tmp_path):
    """tan-cli#496 defect 3 (fact injection): `--default-hw-rev` used to be
    spliced RAW into the hand-built preset YAML. A multi-line value's
    embedded newline turned into a SIBLING YAML mapping key the instant it
    was written -- `--default-hw-rev $'r1\\nsku: E1M-AEN801'` planted a
    DIFFERENT real production SKU inside a file named for this one, and the
    self-check never caught it because it only re-parses the
    ALREADY-INJECTED document (`default_hw_rev` itself still read back
    pattern-clean, `'r1'`). Refused up front now: the FULL raw value must
    match `^[a-z0-9_-]+$`, which no multi-line string can."""
    sdk = _marker_sdk(tmp_path / "sdk")
    out = tmp_path / "out"
    hostile = "r1\ncapabilities: {secure_element: true, ethos_u: true}"
    argv = [
        a
        for a in _dry_run_argv(sdk, out, "--format", "json", "--default-hw-rev", hostile)
        if a != "--dry-run"
    ]

    result = runner.invoke(app, argv)

    assert result.exit_code == 1, result.output
    payload = json.loads(result.stdout)
    assert [i["code"] for i in payload["issues"]] == ["new-som.failed"]
    assert not (out / "metadata").exists(), "an injected hw rev must write nothing at all"


def test_default_hw_rev_colon_value_is_refused_not_an_uncaught_scanner_error(tmp_path):
    """The second half of defect 3: `--default-hw-rev 'r1: x'` used to reach
    `yaml.safe_load(preset_text)` UNGUARDED and raise a raw
    `yaml.scanner.ScannerError` -- exit 1, a Python traceback on stderr, and
    ZERO bytes on stdout under `--format json` (the `json.loads` below is
    itself the regression assertion: it raises on the pre-fix crash)."""
    sdk = _marker_sdk(tmp_path / "sdk")
    out = tmp_path / "out"
    argv = [
        a
        for a in _dry_run_argv(sdk, out, "--format", "json", "--default-hw-rev", "r1: x")
        if a != "--dry-run"
    ]

    result = runner.invoke(app, argv)

    assert result.exception is None or isinstance(result.exception, SystemExit), (
        result.exception,
        result.output,
    )
    assert result.exit_code == 1, result.output
    payload = json.loads(result.stdout)
    assert [i["code"] for i in payload["issues"]] == ["new-som.failed"]


def test_default_hw_rev_out_of_pattern_is_failed_not_internal_error(tmp_path):
    """tan-cli#496 defect 5: an out-of-pattern `--default-hw-rev` (e.g. the
    literal Altium board-rev spelling `R1`, uppercase) used to reach the
    POST-RENDER schema self-check unchecked by this command itself -- and
    for a brand-new family (where the `_family_hw_revisions` cross-check is
    skipped: no `scripts/alp_project_loader.py` in this throwaway checkout,
    matching a real brand-new-family SDK), that self-check reported it as
    `new-som.internal-failure` ("a tan bug, never bad user input") instead
    of the `new-som.failed` every other bad flag gets. Must be
    `new-som.failed` now, with `data` carrying `{"subcommand": "new-som"}`
    (matching `_fail`'s shape), not `internal-failure` with `data: null`."""
    sdk = _marker_sdk(tmp_path / "sdk")
    out = tmp_path / "out"
    argv = [
        a
        for a in _dry_run_argv(sdk, out, "--format", "json", "--default-hw-rev", "R1")
        if a != "--dry-run"
    ]

    result = runner.invoke(app, argv)

    assert result.exit_code == 1, result.output
    payload = json.loads(result.stdout)
    assert [i["code"] for i in payload["issues"]] == ["new-som.failed"]
    assert payload["data"] == {"subcommand": "new-som"}
    assert "R1" in payload["issues"][0]["message"]


# ---------------------------------------------------------------------------
# tan-cli#496 round-2: the review's own fix reintroduced the same class of
# defect it was closing
# ---------------------------------------------------------------------------


def test_default_board_long_value_is_not_truncated(tmp_path):
    """Round-2 blocker 1: `_yaml_scalar` used to call
    `yaml.safe_dump(value)` at PyYAML's DEFAULT 80-column `best_width` and
    keep only `.splitlines()[0]` -- fine under 80 columns, but PyYAML folds
    anything longer at a space, so a `--default-board` past that width was
    SILENTLY TRUNCATED into the generated preset and reported as a clean
    success. A boards-less checkout (`_marker_sdk_no_boards`) is needed to
    even reach render with a value this long -- `_marker_sdk`'s one real
    board name would otherwise refuse it long before `_yaml_scalar` runs."""
    sdk = _marker_sdk_no_boards(tmp_path / "sdk")
    out = tmp_path / "out"
    long_board = " ".join(["word"] * 30)
    assert len(long_board) > 80, "the value must actually cross PyYAML's fold width"
    argv = [
        a
        for a in _dry_run_argv(sdk, out, "--format", "json", "--default-board", long_board)
        if a != "--dry-run"
    ]

    result = runner.invoke(app, argv)

    assert result.exception is None or isinstance(result.exception, SystemExit), (
        result.exception,
        result.output,
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    preset_path = Path(payload["data"]["written"][0])
    import yaml

    doc = yaml.safe_load(preset_path.read_text(encoding="utf-8"))
    assert doc["default_board"] == long_board, "the full value must survive, not just line 1"


def test_default_board_very_long_value_does_not_crash_the_self_check(tmp_path):
    """Round-2 blocker 2: at greater length the fold from blocker 1 can land
    INSIDE an emitted quoted scalar, so keeping only line 1 leaves an
    UNTERMINATED quote -- `preset_text` is then not valid YAML at all, and
    the unguarded `yaml.safe_load(preset_text)` self-check raised a raw
    `yaml.scanner.ScannerError`: exit 1, zero bytes on stdout under
    `--format json` (the `json.loads(result.stdout)` below IS that
    assertion -- it raises on the pre-fix crash). Measured repro value from
    the review."""
    sdk = _marker_sdk_no_boards(tmp_path / "sdk")
    out = tmp_path / "out"
    long_board = "word " * 30
    argv = [
        a
        for a in _dry_run_argv(sdk, out, "--format", "json", "--default-board", long_board)
        if a != "--dry-run"
    ]

    result = runner.invoke(app, argv)

    assert result.exception is None or isinstance(result.exception, SystemExit), (
        result.exception,
        result.output,
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["ok"] is True


def test_rollback_undoes_a_write_that_fails_partway_through(tmp_path, monkeypatch):
    """Round-2 blocker 3: `_rollback_write_failure` used to gate the ENTIRE
    preset branch on `preset_path in written`, and `written.append
    (preset_path)` only ran AFTER `Path.write_text` returned successfully.
    A write that fails PARTWAY (ENOSPC/EDQUOT/EFBIG/EIO -- the file is
    already created and partially written by the time the exception is
    raised) never reaches that append, so the gate skipped rollback
    entirely and left the partial file behind. Simulated here by monkey-
    patching `Path.write_text` to write a partial marker to disk and then
    raise, reproducing "file exists, partially written, exception raised
    before bookkeeping" without needing a real full disk."""
    sdk = _marker_sdk(tmp_path / "sdk")
    out = tmp_path / "out"
    preset_path = out / "metadata" / "e1m_modules" / "E1M-XTST1.yaml"

    real_write_text = Path.write_text

    def _fail_partway(self, data, *args, **kwargs):
        if self == preset_path:
            real_write_text(self, "PARTIAL-GARBAGE", *args, **kwargs)
            raise OSError("simulated ENOSPC partway through the write")
        return real_write_text(self, data, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", _fail_partway)
    argv = [a for a in _dry_run_argv(sdk, out, "--format", "json") if a != "--dry-run"]

    result = runner.invoke(app, argv)

    assert result.exception is None or isinstance(result.exception, SystemExit), (
        result.exception,
        result.output,
    )
    assert result.exit_code == 1, result.output
    payload = json.loads(result.stdout)
    assert [i["code"] for i in payload["issues"]] == ["new-som.failed"]
    assert not preset_path.exists(), (
        "a write that fails partway must be rolled back, not left as a "
        "partially-written file on disk"
    )


def test_interactive_gate_also_checks_stderr_not_only_stdin(tmp_path, monkeypatch):
    """Round-2 major 1: the missing-flags gate checked only
    `sys.stdin.isatty()`, never `sys.stderr` -- but every `_interactive`
    prompt rides stderr (defect 6: `err=True`), so a real stdin terminal
    with a REDIRECTED stderr had no way to show the human the question and
    would block on it silently.

    `CliRunner` gives `sys.stdin`/`sys.stderr` separate `_NamedTextIOWrapper`
    INSTANCES of the same class (`name="<stdin>"` / `name="<stderr>"`), so a
    class-level `isatty` patch keyed on that name can make them disagree --
    stdin a real terminal, stderr not -- the exact split a stdin-only gate
    cannot see. Without the fix, `_interactive` would then run against
    `CliRunner`'s empty stdin and raise `EOFError` instead of this command's
    own coded refusal."""
    monkeypatch.setattr(
        "typer.testing._NamedTextIOWrapper.isatty", lambda self: self.name == "<stdin>"
    )
    sdk = _marker_sdk(tmp_path / "sdk")
    result = runner.invoke(
        app, ["--dry-run", "--sdk-root", str(sdk), "--output-root", str(tmp_path / "out")]
    )
    assert result.exception is None or isinstance(result.exception, SystemExit), (
        result.exception,
        result.output,
    )
    assert result.exit_code == 1, result.output
    assert "--sku" in result.output and "--soc-ref" in result.output and "--family" in result.output


def test_display_name_del_byte_is_refused_not_an_uncaught_reader_error(tmp_path):
    """Round-2 major 2: `--display-name`'s control-character refusal only
    checked `ord(ch) < 0x20`, so a raw DEL byte (0x7F) -- also a control
    character, just outside the C0 range -- passed it, reached the rendered
    YAML double-quoted scalar unescaped (`_yaml_dquote` only escapes `\\`
    and `"`), and crashed the previously-unguarded
    `yaml.safe_load(preset_text)` self-check with a raw
    `yaml.reader.ReaderError`: exit 1, zero bytes on stdout under
    `--format json` (the `json.loads(result.stdout)` below IS that
    assertion -- it raises on the pre-fix crash)."""
    sdk = _marker_sdk(tmp_path / "sdk")
    out = tmp_path / "out"
    hostile = "X\x7fY"
    argv = [
        a
        for a in _dry_run_argv(sdk, out, "--format", "json", "--display-name", hostile)
        if a != "--dry-run"
    ]

    result = runner.invoke(app, argv)

    assert result.exception is None or isinstance(result.exception, SystemExit), (
        result.exception,
        result.output,
    )
    assert result.exit_code == 1, result.output
    payload = json.loads(result.stdout)
    assert [i["code"] for i in payload["issues"]] == ["new-som.failed"]
    assert "control characters" in payload["issues"][0]["message"]
    assert not (out / "metadata").exists(), "a refused display name must write nothing"


def test_format_json_failure_path_carries_a_new_som_code(tmp_path):
    """tan-cli#399: the failure path used to be answered by `cli.main`'s
    generic fallback -- `command: "cli"`, `cli.parse-error` -- so a consumer
    branching on `command` got a different answer depending on whether the
    scaffold had worked. There was no `new-som` entry in
    `contract/issue-codes.json` at all.

    `data` is `{"subcommand": "new-som"}`, not `null` -- measured live against
    `target/debug/tan.exe new-som --format json --sdk-root <bad>`, which
    answers the identical shape for this identical refusal (see
    `test_format_json_failure_emits_a_new_som_code_not_cli_parse_error`'s own
    oracle measurement)."""
    result = runner.invoke(
        app,
        _dry_run_argv(tmp_path / "nope", tmp_path / "out", "--format", "json"),
    )
    assert result.exit_code == 2
    payload = json.loads(result.stdout)
    assert payload["command"] == "new-som"
    assert payload["ok"] is False
    assert payload["exitCode"] == 2
    assert payload["data"] == {"subcommand": "new-som"}
    assert [i["code"] for i in payload["issues"]] == ["new-som.failed"]
    assert "alp-sdk root is unresolved" in payload["issues"][0]["message"]


def test_success_and_failure_paths_agree_that_stdout_is_json(tmp_path):
    """The pinned invariant, stated as one test rather than inferred from the
    two above: ONE argv shape, `--format json`, must not switch stdout between
    JSON and prose depending on whether the run succeeded."""
    sdk = _marker_sdk(tmp_path / "sdk")
    ok = runner.invoke(app, _dry_run_argv(sdk, tmp_path / "out", "--format", "json"))
    bad = runner.invoke(
        app, _dry_run_argv(tmp_path / "nope", tmp_path / "out2", "--format", "json")
    )
    assert (ok.exit_code, bad.exit_code) == (0, 2)
    for result in (ok, bad):
        document = json.loads(result.stdout)
        assert isinstance(document, dict)
        assert _ENVELOPE_KEYS <= document.keys()
        assert document["command"] == "new-som"


def test_text_mode_output_is_untouched_by_the_envelope_work(tmp_path):
    """The default path is the alp_cli original's, verbatim -- the envelope is
    reachable only through `--format json`. Without this, "make it emit an
    envelope" quietly becomes "make it emit an envelope everywhere" and the
    oracle-parity byte diffs above would be the only thing noticing."""
    sdk = _marker_sdk(tmp_path / "sdk")
    out = tmp_path / "out"
    result = runner.invoke(app, _dry_run_argv(sdk, out))
    assert result.exit_code == 0, result.output
    assert result.stdout.startswith("Preset skeleton validates against som-preset-v1")
    assert "Dry run -- validated OK, nothing was written." in result.stdout
    with pytest.raises(json.JSONDecodeError):
        json.loads(result.stdout)


def test_format_json_refuses_to_prompt_even_on_a_terminal(tmp_path, monkeypatch):
    """A `click.prompt` writes to stdout, which under `--format json` carries
    the one envelope -- prompting there would corrupt the document the caller
    asked for. Refuse instead, naming the flags, the same way the non-tty path
    already did."""
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    sdk = _marker_sdk(tmp_path / "sdk")
    result = runner.invoke(
        app, ["--dry-run", "--sdk-root", str(sdk), "--format", "json", "--sku", "E1M-XTST1"]
    )
    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert [i["code"] for i in payload["issues"]] == ["new-som.failed"]
    assert "--soc-ref, --family" in payload["issues"][0]["message"]


def test_bad_format_value_is_a_usage_error(tmp_path):
    """`--format` is validated at the boundary, matching every sibling
    (`faultdecode`, `size`, `sdk`): an unknown value is a Click usage error
    (exit 2), never a silent fall-through to text."""
    sdk = _marker_sdk(tmp_path / "sdk")
    result = runner.invoke(app, _dry_run_argv(sdk, tmp_path / "out", "--format", "bogus"))
    assert result.exit_code == 2
