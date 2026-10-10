# SPDX-License-Identifier: Apache-2.0
"""tan-cli#1267: the pure half of Flow A's whole-ATOC guard
(`tan.core.atoc_guard` + `tan.core.atoc_guard_messages`). The command-level
behaviour -- argv, the issue codes, the stale-verdict handling -- is
`tests/commands/test_flash_atoc_guard.py`.

Verdict fixtures follow alp-sdk's frozen `alp-sdk.alif-flash-atoc-guard.v1`
contract (alp-sdk `docs/aen-provisioning.md` section 0.6, at tan's pin
`34c11c9de04e264fdcab2bc0d58b328d9d117ca8`), field for field.
"""
import json

import pytest

from tan.core import atoc_guard, atoc_guard_messages, flash_plan
from tan.core.atoc_guard import (
    GuardVerdict,
    RunnerFacts,
    decide_replace_atoc,
    parse_domain_build_dirs,
    parse_flash_runner,
    parse_module_dir,
    parse_verdict,
    runner_has_guard,
)
from tan.core.atoc_guard_messages import passed_note, refusal_message, split_runner_setup_noise

GUARDED = RunnerFacts(runner="alif_flash", source="src.py", origin="o", has_guard=True)


def verdict_json(status, foreign=(), query_status="ok", **overrides):
    body = {
        "schema": "alp-sdk.alif-flash-atoc-guard.v1",
        "status": status,
        # A tuple is a fixture convenience; anything else is written as-is so
        # a malformed `foreign` can be fed in on purpose.
        "foreign": list(foreign) if isinstance(foreign, tuple) else foreign,
        "transcript": "/build/alif_flash/atoc-before.txt",
        "allowed": ["ALP-HE"],
        "query_status": query_status,
    }
    body.update(overrides)
    return json.dumps(body, indent=2) + "\n"


def test_the_flow_d_flag_named_here_is_the_flow_d_flag():
    """The messages spell `--atoc-unqueryable` themselves rather than
    importing it; this pins the two equal so a rename cannot leave one stale."""
    assert atoc_guard_messages.FLOW_D_ACK_FLAG == flash_plan.ATOC_ACK_FLAG
    assert atoc_guard.REPLACE_ATOC_FLAG != flash_plan.ATOC_ACK_FLAG


# ── build files + the runner source ──────────────────────────────────────────


def test_the_default_flash_runner_is_read_from_runners_yaml():
    text = (
        "# Available runners configured by board.cmake.\n"
        "runners:\n- alif_flash\n- jlink\n\n"
        "# Default flash runner if --runner is not given.\n"
        "flash-runner: alif_flash\n\n"
        "debug-runner: jlink\n"
    )
    assert parse_flash_runner(text) == "alif_flash"
    assert parse_flash_runner("runners:\n- jlink\n") is None
    assert parse_flash_runner("flash-runner: 'jlink'\r\n") == "jlink"


def test_the_alp_sdk_module_root_is_read_from_zephyr_modules_txt():
    text = (
        '"hal_alif":"/ws/modules/hal/alif":"/ws/modules/hal/alif/zephyr"\n'
        '"alp-sdk":"C:/ws/alp-sdk":"C:/ws/alp-sdk/zephyr"\n'
    )
    assert parse_module_dir(text) == "C:/ws/alp-sdk"
    assert parse_module_dir('"hal_alif":"/x":"/x/zephyr"\n') is None


def test_sysbuild_domain_dirs_come_from_domains_yaml_in_order():
    text = (
        "default: app\nbuild_dir: /b\n"
        "domains:\n- name: mcuboot\n  build_dir: /b/mcuboot\n- name: app\n  build_dir: /b/app\n"
        "flash_order:\n- mcuboot\n- app\n"
    )
    assert parse_domain_build_dirs(text) == ["/b/mcuboot", "/b/app"]
    assert parse_domain_build_dirs("domains: []\n") is None
    assert parse_domain_build_dirs("domains:\n- name: app\n") is None
    assert parse_domain_build_dirs(": : :") is None


def test_a_runner_has_the_guard_only_with_both_the_flag_and_the_schema():
    flag = "parser.add_argument(\n    '--replace-atoc', dest='replace_atoc')\n"
    schema = "'schema': 'alp-sdk.alif-flash-atoc-guard.v1',\n"
    assert runner_has_guard(flag + schema)
    assert not runner_has_guard(flag)
    assert not runner_has_guard(schema)
    # A mention in prose is not a registered argument.
    assert not runner_has_guard("see --replace-atoc in the docs\n" + schema)


# ── which slices the flag reaches ─────────────────────────────────────────────


def test_the_flag_is_appended_only_for_a_guarded_alif_flash_slice():
    decision = decide_replace_atoc("zephyr_west_flash", "m55_he", GUARDED, True)
    assert decision.append and decision.guarded and decision.warning is None
    assert decision.source == "src.py"

    quiet = decide_replace_atoc("zephyr_west_flash", "m55_he", GUARDED, False)
    assert not quiet.append and quiet.guarded and quiet.warning is None


@pytest.mark.parametrize(
    ("method", "facts", "needle"),
    [
        ("alif_mram_jlink", RunnerFacts(), "--atoc-unqueryable"),
        ("yocto_wic", RunnerFacts(), "only to the alif_flash west runner"),
        ("zephyr_west_flash", RunnerFacts(unknown_reason="WHY"),
         "tan could not learn which west runner it uses: WHY."),
        ("zephyr_west_flash", RunnerFacts(runner="jlink"), "its west runner is jlink, not alif_flash"),
    ],
)
def test_the_flag_on_a_slice_it_does_not_apply_to_warns_and_is_not_passed(method, facts, needle):
    decision = decide_replace_atoc(method, "c1", facts, True)
    assert not decision.append and not decision.guarded
    assert decision.warning_kind == atoc_guard.NOT_APPLICABLE
    assert f"{method}[c1]: --replace-atoc was not passed to this entry" in decision.warning
    assert needle in decision.warning, decision.warning
    # Without the flag there is nothing to say about any of them.
    assert decide_replace_atoc(method, "c1", facts, False).warning is None


def test_an_alif_flash_slice_whose_runner_lacks_the_guard_says_which_file_was_checked():
    facts = RunnerFacts(runner="alif_flash", source="/mod/runner.py", origin="ORIGIN", has_guard=False)
    for flag in (False, True):
        decision = decide_replace_atoc("zephyr_west_flash", "m55_he", facts, flag)
        assert not decision.append and not decision.guarded
        assert decision.warning_kind == atoc_guard.UNGUARDED
        assert "the runner tan checked -- /mod/runner.py (ORIGIN) -- has no pre-burn" in decision.warning
        assert "REPLACES the whole ATOC" in decision.warning
        assert ("--replace-atoc was NOT passed" in decision.warning) is flag


# ── the verdict ───────────────────────────────────────────────────────────────


def test_a_v1_verdict_parses_field_for_field():
    verdict = parse_verdict(verdict_json("refused-foreign", ("A32_APP", "BOOTLOAD")))
    assert verdict == GuardVerdict(
        "refused-foreign", ("A32_APP", "BOOTLOAD"), "/build/alif_flash/atoc-before.txt", "ok",
        ("ALP-HE",),
    )
    assert verdict.refused


@pytest.mark.parametrize(
    ("status", "query", "foreign"),
    [
        ("clear", "ok", ()),
        ("empty", "empty", ()),
        ("refused-foreign", "ok", ("A32_APP",)),
        ("refused-unverified", "unverified", ()),
        ("replaced", "unverified", ()),
        ("replaced", "ok", ("A32_APP",)),
    ],
)
def test_every_combination_the_runner_produces_parses(status, query, foreign):
    assert isinstance(parse_verdict(verdict_json(status, foreign, query)), GuardVerdict)


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("{not json", "not valid JSON"),
        ("[]", "not a JSON object"),
        (verdict_json("clear", schema="alp-sdk.alif-flash-atoc-guard.v2"), "its schema is"),
        (verdict_json("refused-sideways"), "its status 'refused-sideways'"),
        (verdict_json("clear", query_status="maybe"), "its query_status 'maybe'"),
        (verdict_json("refused-foreign", foreign="A32_APP"), "its foreign field"),
        (verdict_json("refused-foreign", foreign=[1]), "its foreign field"),
        (verdict_json("clear", allowed="ALP-HE"), "its allowed field"),
        (verdict_json("clear", allowed=None), "its allowed field"),
        (verdict_json("clear", transcript=None), "transcript field"),
        # Combinations alp-sdk's decide_atoc_guard cannot produce.
        (verdict_json("refused-foreign", ()), "is a combination"),
        (verdict_json("replaced", (), "ok"), "is a combination"),
        (verdict_json("refused-unverified", ("X",), "unverified"), "is a combination"),
        (verdict_json("refused-unverified", (), "ok"), "is a combination"),
        (verdict_json("empty", (), "ok"), "is a combination"),
        (verdict_json("clear", ("X",), "ok"), "is a combination"),
    ],
)
def test_an_unreadable_verdict_is_a_reason_never_a_guess(text, reason):
    result = parse_verdict(text)
    assert isinstance(result, str), result
    assert reason in result, result


def test_the_refused_foreign_message_names_every_entry_and_the_override():
    verdict = GuardVerdict("refused-foreign", ("A32_APP", "BOOTLOAD"), "/t.txt", "ok")
    message = refusal_message("m55_he", verdict, {})
    assert message.startswith(
        "zephyr_west_flash[m55_he]: the alif_flash runner's pre-burn ATOC guard refused "
        "this write before anything was burned (status: refused-foreign; transcript: /t.txt)."
    ), message
    assert "the board also carries: A32_APP, BOOTLOAD." in message
    assert "re-run with --replace-atoc" in message
    assert "--atoc-unqueryable does not answer this guard" in message


def test_a_factory_mcuboot_entry_steers_away_from_the_override():
    """Mirrors the runner's own branch: a factory `MCUBOOT-` entry is the
    module's bootloader, so the remedy is SDK Option B, not the flag."""
    verdict = GuardVerdict("refused-foreign", ("MCUBOOT-",), "/t.txt", "ok")
    message = refusal_message("m55_he", verdict, {})
    assert "factory-provisioned 'MCUBOOT-' MCUboot bootloader" in message
    assert "section 0.5, Option B" in message
    assert "re-run with --replace-atoc" not in message
    assert "--replace-atoc still overrides this refusal, but it deletes 'MCUBOOT-'" in message


def test_an_entry_this_run_wrote_is_named_as_such_and_never_answered_by_the_flag():
    verdict = GuardVerdict("refused-foreign", ("ALP-HE",), "/t.txt", "ok", ("ALP-HP",))
    message = refusal_message("m55_hp", verdict, {"ALP-HE": "m55_he"})
    assert (
        "The resident entry it would delist is ALP-HE (written by m55_he), written "
        "earlier in this same run. This manifest cannot be Flow A-flashed one core after "
        "another: every alif_flash write REPLACES the whole ATOC with only its own entry, "
        "so each core delists the one flashed before it. --replace-atoc is NOT the fix "
        "here -- it would delist that entry again."
    ) in message
    assert "scripts/bench/aen/flash-run-dualcore.sh <hp-build-dir> <he-build-dir>" in message
    assert "re-run with --replace-atoc" not in message


def test_the_refused_unverified_message_does_not_push_the_override_first():
    verdict = GuardVerdict("refused-unverified", (), "/t.txt", "unverified")
    message = refusal_message("m55_he", verdict, {})
    assert "(status: refused-unverified; transcript: /t.txt)" in message
    assert "could not verify what is resident in the ATOC over the SE-UART" in message
    assert "confirm by hand what is resident" in message
    assert "re-run with --replace-atoc only once you know it is safe to lose" in message
    assert "--replace-atoc is not the answer there" in message
    assert "the board also carries" not in message


def test_the_replaced_note_says_what_was_overridden_and_never_claims_a_failed_deletion():
    unverified = GuardVerdict("replaced", (), "/t", "unverified")
    foreign = GuardVerdict("replaced", ("A32_APP",), "/t", "ok")
    assert "overrode an UNVERIFIED read" in passed_note(unverified)
    assert passed_note(foreign).endswith("now delisted: A32_APP")
    assert passed_note(foreign, failed=True) == (
        "ATOC guard: --replace-atoc overrode A32_APP; the write then failed, so whether "
        "A32_APP is still listed is unknown"
    )
    assert passed_note(GuardVerdict("clear", (), "/t", "ok")) == "ATOC guard: clear"


def test_the_ambiguous_flag_message_names_every_entry_and_the_narrowing_option():
    message = atoc_guard_messages.ambiguous_replace_message(["m55_he", "m55_hp"])
    assert message.startswith(
        "flash: --replace-atoc would reach 2 alif_flash writes in this run (m55_he, m55_hp)."
    ), message
    assert "Nothing was flashed." in message
    assert "Narrow the run to one entry with --core CORE_ID (or --helper NAME)" in message
    assert "flash-run-dualcore.sh" in message


# ── tan-cli#1426: runner setup noise ─────────────────────────────────────────


def test_runner_setup_noise_is_matched_by_wording_not_by_logger():
    """Only the two host-setup notices move out. Any OTHER `runners.*`
    warning stays in the refusal: the runner's own guard could log one, and
    calling it unrelated could send an operator to --replace-atoc."""
    noise = [
        'The module for runner "rtsflash" could not be imported (No module named \'usb\')',
        'WARNING: The module for runner "jlink" could not be imported (No module named \'pylink\')',
        "WARNING: runners.alif_flash: the 'fdt' Python package (needed by app-gen-toc) "
        "was not found; if app-gen-toc fails, run: pip install fdt",
    ]
    kept_in = [
        "WARNING: runners.alif_flash: resident ATOC table format not recognised",
        "WARNING: runners.alif_flash: the 'fdt' Python package version is too old",
        "FATAL ERROR: refusing to burn",
        "  The module for runner \"x\" was loaded",
    ]
    kept, moved = split_runner_setup_noise([kept_in[0], noise[0], kept_in[1], noise[1],
                                            kept_in[2], noise[2], kept_in[3]])
    assert moved == noise
    assert kept == kept_in
