# SPDX-License-Identifier: Apache-2.0
"""`tan explain --code` -- the alp-sdk diagnostic/error-code lookup.

Every asserted string here is VERBATIM from a real alp-sdk checkout's
`metadata/error-catalog.json` (`ALP-B003`, `ALP_ERR_NO_BACKEND`), captured by
running `python -m alp_cli explain <code>` in an alp-sdk `origin/dev` worktree
and copying its output -- not paraphrased, and not re-derived from the docs.
The catalogue is rebuilt hermetically per test rather than read from a bound
checkout, so the suite pins what tan RENDERS and stays green with no alp-sdk
anywhere.

The last group is the regression half: `--code` must not have moved the three
paths that already existed. `test_the_overview_envelope_still_matches_the_
frozen_golden` is the strongest of them -- it diffs the whole envelope against
`contract/envelopes/explain-overview/expected.json`, the same fixture the
conformance suite drives.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tan.cli import app

runner = CliRunner()

#: `contract/envelopes/`, three levels above `python/tests/commands/`.
_CONTRACT = Path(__file__).resolve().parents[3] / "contract" / "envelopes"

#: Two entries copied verbatim out of alp-sdk `origin/dev`'s generated
#: `metadata/error-catalog.json` -- one of each `kind`. `ALP_ERR_NO_BACKEND`
#: is the api-error shape and carries NO `cause`/`fix` (the generator omits a
#: field with no source rather than fabricating one), which is what makes it
#: the case that pins field SKIPPING.
_ALP_B003 = {
    "cause": "- A value with the right idea but the wrong spelling or casing.",
    "code": "ALP-B003",
    "doc": "docs/diagnostics/ALP-B003.md",
    "fix": "Use one of the listed enum values (verbatim, case included).",
    "kind": "runtime-diagnostic",
    "summary": "value violates an enum or pattern constraint",
}
_ALP_ERR_NO_BACKEND = {
    "code": "ALP_ERR_NO_BACKEND",
    "doc": "include/alp/peripheral.h",
    "kind": "api-error",
    "summary": (
        ".alpmodel has no blob for any backend available on this SoM "
        "(and no CPU fallback)."
    ),
}
_ALP_ERR_NOT_READY = {
    "code": "ALP_ERR_NOT_READY",
    "doc": "include/alp/peripheral.h",
    "kind": "api-error",
    "summary": "Device or subsystem is not ready.",
}


def _sdk(tmp_path: Path, codes: dict | None = None, *, catalog: str | None = None) -> Path:
    """A checkout the ladder accepts: the `scripts/alp_project.py` marker plus
    a `metadata/error-catalog.json`. `catalog` writes raw text instead (a
    malformed document); passing neither writes no catalogue at all."""
    root = tmp_path / "alp-sdk"
    (root / "scripts").mkdir(parents=True)
    (root / "scripts" / "alp_project.py").write_text("", encoding="utf-8")
    if codes is not None or catalog is not None:
        (root / "metadata").mkdir()
        text = catalog if catalog is not None else json.dumps({"codes": codes})
        (root / "metadata" / "error-catalog.json").write_text(text, encoding="utf-8")
    return root


def _run(tmp_path: Path, monkeypatch, *argv: str):
    """Invoke from an EMPTY cwd. The ladder's discovery tier walks the working
    directory's ancestors, so a test that means "no SDK is bound" has to run
    somewhere with none above it -- otherwise it silently measures whatever
    checkout happens to sit above the developer's tan-cli. (`HOME` and
    `ALP_SDK_ROOT` are already scrubbed by `tests/conftest.py`.)"""
    empty = tmp_path / "cwd"
    empty.mkdir(exist_ok=True)
    monkeypatch.chdir(empty)
    return runner.invoke(app, ["explain", *argv])


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def test_a_diagnostic_code_resolves_to_its_catalogue_fields(tmp_path, monkeypatch):
    """The rendered block, field for field. The values are the catalogue's own
    text -- `explain.py` prints exactly these, indented two spaces where tan
    bullets them (see the module docstring on `test_explain_code_command`'s
    divergence note in `explain_cmd`)."""
    sdk = _sdk(tmp_path, {"ALP-B003": _ALP_B003})
    result = _run(tmp_path, monkeypatch, "--code", "ALP-B003", "--sdk-root", str(sdk))

    assert result.exit_code == 0, result.output
    assert result.stderr.splitlines() == [
        "explain: ALP-B003 (runtime-diagnostic)",
        "- summary: value violates an enum or pattern constraint",
        "- cause: - A value with the right idea but the wrong spelling or casing.",
        "- fix: Use one of the listed enum values (verbatim, case included).",
        "- doc: docs/diagnostics/ALP-B003.md",
    ]


def test_an_api_error_omits_the_fields_the_catalogue_has_no_source_for(tmp_path, monkeypatch):
    """`ALP_ERR_*` entries carry no `cause`/`fix`. Those lines are ABSENT, not
    rendered empty -- the generator's "never fabricate a field" rule, held on
    the reading side."""
    sdk = _sdk(tmp_path, {"ALP_ERR_NO_BACKEND": _ALP_ERR_NO_BACKEND})
    result = _run(
        tmp_path, monkeypatch, "--code", "ALP_ERR_NO_BACKEND", "--sdk-root", str(sdk)
    )

    assert result.exit_code == 0, result.output
    assert result.stderr.splitlines() == [
        "explain: ALP_ERR_NO_BACKEND (api-error)",
        "- summary: .alpmodel has no blob for any backend available on this SoM "
        "(and no CPU fallback).",
        "- doc: include/alp/peripheral.h",
    ]


@pytest.mark.parametrize("typed", ["alp-b003", "ALP-b003", "  ALP-B003  "])
def test_lookup_is_case_insensitive_and_trims(tmp_path, monkeypatch, typed):
    """`explain.py` normalises with `.strip().upper()`; so does this. The
    ANSWER always reports the catalogue's own spelling, never the caller's."""
    sdk = _sdk(tmp_path, {"ALP-B003": _ALP_B003})
    result = _run(
        tmp_path, monkeypatch, "--code", typed, "--sdk-root", str(sdk), "--format", "json"
    )

    assert result.exit_code == 0, result.output
    doc = json.loads(result.stdout)
    assert doc["data"]["selector"] == {"kind": "diagnostic-code", "value": "ALP-B003"}


def test_the_envelope_carries_the_catalogue_entry_verbatim(tmp_path, monkeypatch):
    """`data.diagnostic` is byte-for-byte what `alp explain <code> --json`
    printed as its whole document -- the shape a consumer of the retired
    command can move onto without re-reading anything."""
    sdk = _sdk(tmp_path, {"ALP-B003": _ALP_B003})
    result = _run(
        tmp_path, monkeypatch, "--code", "ALP-B003", "--sdk-root", str(sdk), "--format", "json"
    )

    assert result.exit_code == 0, result.output
    doc = json.loads(result.stdout)
    assert doc["command"] == "explain"
    assert doc["ok"] is True
    assert doc["exitCode"] == 0
    assert doc["issues"] == []
    assert {k: v for k, v in doc["data"].items() if k in ("diagnostic", "suggestions")} == {
        "diagnostic": _ALP_B003,
        "suggestions": [],
    }
    assert doc["data"]["summary"] == "ALP-B003 (runtime-diagnostic)"


def test_the_success_envelope_names_the_checkout_it_read(tmp_path, monkeypatch):
    """`sdk` is the answer to "which catalogue said that". `--sdk-root` is the
    terminal tier, so it must report `sdkRootFlag` and not a discovered root."""
    sdk = _sdk(tmp_path, {"ALP-B003": _ALP_B003})
    result = _run(
        tmp_path, monkeypatch, "--code", "ALP-B003", "--sdk-root", str(sdk), "--format", "json"
    )

    doc = json.loads(result.stdout)
    # `.get`, not `[...]`: the failure this pins includes the key going ABSENT,
    # and a KeyError there would be an incidental error rather than this
    # assertion reporting what changed.
    assert doc.get("sdk") == {
        "root": str(sdk).replace("\\", "/"),
        "sourceTier": "sdkRootFlag",
    }
    # `project` stays null/null on this path too: explain reads no board.yaml,
    # and a diagnostic code is a property of the SDK, not of a project.
    assert doc["project"] == {"root": None, "boardYaml": None}


def _write_pin(workspace: Path, target: Path) -> None:
    """`.alp/sdk-path` in the `{"sdkPath": ...}` shape `sdk_discovery._pointer_target`
    reads -- mirrors `test_sdk_discovery_ladders._write_pin`."""
    (workspace / ".alp").mkdir(parents=True, exist_ok=True)
    (workspace / ".alp" / "sdk-path").write_text(
        json.dumps({"sdkPath": str(target).replace("\\", "/")}), encoding="utf-8"
    )


def test_project_flag_picks_which_checkout_the_ladder_answers(tmp_path, monkeypatch):
    """`--project` is the ONE input the SDK ladder exists to resolve against on
    the `--code` path -- unlike `--template`/`--target`, which read no checkout
    at all. MEASURED pre-fix: `bind_sdk` resolved from the bare CWD and
    `--project` was accepted-and-ignored, so running from a cwd pinned to
    SDK-A with `--project <projB>` (pinned to SDK-B) still answered SDK-A --
    the exact accepted-but-ignored input class `explain`'s own `--sdk-root`
    rationale says this command refuses."""
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    sdk_a = _sdk(tmp_path / "sdk-a", {"ALP-B003": _ALP_B003})
    entry_b = dict(_ALP_B003, summary="FROM SDK-B")
    sdk_b = _sdk(tmp_path / "sdk-b", {"ALP-B003": entry_b})
    proj_b = tmp_path / "proj-b"
    proj_b.mkdir()
    _write_pin(cwd, sdk_a)
    _write_pin(proj_b, sdk_b)

    monkeypatch.chdir(cwd)
    result = runner.invoke(
        app, ["explain", "--code", "ALP-B003", "--project", str(proj_b), "--format", "json"]
    )

    assert result.exit_code == 0, result.output
    doc = json.loads(result.stdout)
    assert doc["data"]["diagnostic"]["summary"] == "FROM SDK-B"
    assert doc.get("sdk", {}).get("root") == str(sdk_b).replace("\\", "/")


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


def test_no_sdk_bound_refuses_and_names_what_it_could_not_find(tmp_path, monkeypatch):
    """The honest-degradation requirement. With no checkout resolvable this is
    a REFUSAL that names the missing thing and how to supply it -- not an
    empty answer, and not a traceback."""
    result = _run(tmp_path, monkeypatch, "--code", "ALP-B003", "--format", "json")

    assert result.exit_code == 1, result.output
    doc = json.loads(result.stdout)
    assert doc["ok"] is False
    assert doc["issues"] == [
        {
            "code": "explain.sdk-root-unresolved",
            "severity": "error",
            "message": (
                "alp-sdk root is unresolved, so no diagnostic catalogue could be "
                "read -- get an alp-sdk checkout (`git clone "
                "https://github.com/alplabai/alp-sdk`), then point tan at it with "
                "`--sdk-root <path>`."
            ),
        }
    ]
    # Nothing was resolved, so nothing is claimed: the `sdk` key is ABSENT
    # rather than reporting a root the run never read.
    assert "sdk" not in doc


def test_no_sdk_bound_refuses_on_the_text_path_too(tmp_path, monkeypatch):
    result = _run(tmp_path, monkeypatch, "--code", "ALP-B003")

    assert result.exit_code == 1
    assert result.stderr.startswith(
        "explain: alp-sdk root is unresolved, so no diagnostic catalogue could be read"
    )


def test_an_sdk_root_flag_missing_the_marker_is_unresolved_not_unreadable(tmp_path, monkeypatch):
    """`--sdk-root` is the ladder's TERMINAL tier (I-31: a typo'd flag must not
    fall through to a lower one) but was taken on faith -- no `SDK_MARKER`
    check. MEASURED pre-fix: pointing it at a directory that resolves but is
    not an alp-sdk checkout reached `resolve_code` and misreported
    `explain.catalog-unreadable`, telling the caller to run the catalogue
    generator inside a directory that was never a checkout. Fixed, it reports
    the SAME code `presets --sdk-root <bad>` reports for this input class."""
    not_an_sdk = tmp_path / "not-an-sdk"
    not_an_sdk.mkdir()
    result = _run(
        tmp_path, monkeypatch, "--code", "ALP-B003", "--sdk-root", str(not_an_sdk),
        "--format", "json",
    )

    assert result.exit_code == 1, result.output
    doc = json.loads(result.stdout)
    assert doc["issues"][0]["code"] == "explain.sdk-root-unresolved"
    assert "sdk" not in doc


def test_a_nonexistent_sdk_root_flag_is_unresolved_too(tmp_path, monkeypatch):
    """The other half of the same measurement: a `--sdk-root` naming a path
    that does not exist at all -- not just one lacking the marker."""
    result = _run(
        tmp_path, monkeypatch, "--code", "ALP-B003", "--sdk-root",
        str(tmp_path / "does-not-exist"), "--format", "json",
    )

    assert result.exit_code == 1, result.output
    doc = json.loads(result.stdout)
    assert doc["issues"][0]["code"] == "explain.sdk-root-unresolved"
    assert "sdk" not in doc


def test_a_checkout_with_no_catalogue_names_the_path_it_looked_at(tmp_path, monkeypatch):
    """A DIFFERENT refusal from "no SDK bound", deliberately: the checkout
    resolved fine, so the reader needs to know which file is missing and how
    to regenerate it -- and the envelope still reports the `sdk` that answered."""
    sdk = _sdk(tmp_path)
    result = _run(
        tmp_path, monkeypatch, "--code", "ALP-B003", "--sdk-root", str(sdk), "--format", "json"
    )

    assert result.exit_code == 1, result.output
    doc = json.loads(result.stdout)
    assert doc["issues"][0]["code"] == "explain.catalog-unreadable"
    assert doc["issues"][0]["message"] == (
        "error catalog not found -- run `python3 scripts/gen_error_catalog.py` in "
        f"the alp-sdk checkout ({sdk / 'metadata' / 'error-catalog.json'})."
    )
    assert doc.get("sdk") == {
        "root": str(sdk).replace("\\", "/"),
        "sourceTier": "sdkRootFlag",
    }


def test_a_malformed_catalogue_is_a_coded_refusal_not_a_traceback(tmp_path, monkeypatch):
    sdk = _sdk(tmp_path, catalog="{not json")
    result = _run(
        tmp_path, monkeypatch, "--code", "ALP-B003", "--sdk-root", str(sdk), "--format", "json"
    )

    assert result.exit_code == 1, result.output
    doc = json.loads(result.stdout)
    assert doc["issues"][0]["code"] == "explain.catalog-unreadable"
    assert "not valid JSON" in doc["issues"][0]["message"]


@pytest.mark.parametrize(
    "catalog",
    ['["ALP-B003"]', '{"codes": ["ALP-B003"]}', '{"codes": {"ALP-B003": "not an object"}}'],
)
def test_a_wrong_shaped_catalogue_refuses_at_exit_1_not_exit_5(tmp_path, monkeypatch, catalog):
    """What the three `isinstance` guards in `load_codes` actually DECIDE.

    All three documents parse as JSON, so none is caught by the malformed-JSON
    arm. MEASURED with each guard removed in turn: the top-level array escapes
    as `AttributeError: 'list' object has no attribute 'get'`, the
    list-valued `codes` as `TypeError: list indices must be integers` (its
    elements survive `lookup`'s dict comprehension, so `"ALP-B003"` MATCHES and
    then indexes a list by string), and the non-object ENTRY as `AttributeError:
    'str' object has no attribute 'get'` (it matches `lookup` fine -- the key is
    a normal string -- and only `summary_line`'s `entry.get(...)` trips). All
    three land in `explain`'s catch-all as `explain.internal-failure` at exit 5
    -- "tan hit a bug" for a checkout problem the reader could fix.

    Pinned here rather than only in `tests/core/test_error_catalog.py`: a
    `pytest.raises(CatalogUnreadable)` there goes red on the raw exception
    escaping, which is an incidental error rather than an assertion about what
    the guard decides. Exit 1 vs exit 5 is that decision, and it is observable
    only at this level.
    """
    sdk = _sdk(tmp_path, catalog=catalog)
    result = _run(
        tmp_path, monkeypatch, "--code", "ALP-B003", "--sdk-root", str(sdk), "--format", "json"
    )

    assert result.exit_code == 1, result.output
    doc = json.loads(result.stdout)
    assert doc["issues"][0]["code"] == "explain.catalog-unreadable"
    assert "is not a JSON object" in doc["issues"][0]["message"]


def test_an_unknown_code_refuses_with_the_difflib_shortlist(tmp_path, monkeypatch):
    """`explain.py`'s own miss sentence and its own shortlist -- same
    `difflib` cutoff, same order, same mapping back to the catalogue's
    spelling."""
    sdk = _sdk(
        tmp_path,
        {"ALP_ERR_NO_BACKEND": _ALP_ERR_NO_BACKEND, "ALP_ERR_NOT_READY": _ALP_ERR_NOT_READY},
    )
    result = _run(
        tmp_path, monkeypatch, "--code", "ALP_ERR_NO_BACKENDD", "--sdk-root", str(sdk)
    )

    assert result.exit_code == 1
    assert result.stderr.strip() == (
        "explain: unknown code 'ALP_ERR_NO_BACKENDD'; did you mean: "
        "ALP_ERR_NO_BACKEND, ALP_ERR_NOT_READY?"
    )


def test_an_unknown_code_carries_its_shortlist_in_data(tmp_path, monkeypatch):
    sdk = _sdk(
        tmp_path,
        {"ALP_ERR_NO_BACKEND": _ALP_ERR_NO_BACKEND, "ALP_ERR_NOT_READY": _ALP_ERR_NOT_READY},
    )
    result = _run(
        tmp_path,
        monkeypatch,
        "--code",
        "ALP_ERR_NO_BACKENDD",
        "--sdk-root",
        str(sdk),
        "--format",
        "json",
    )

    doc = json.loads(result.stdout)
    assert doc["issues"] == [
        {
            "code": "explain.code-unknown",
            "severity": "error",
            "message": "Unknown diagnostic code 'ALP_ERR_NO_BACKENDD'.",
        }
    ]
    # A dict slice, not two `[...]` reads: the failure being pinned includes
    # both keys vanishing, and a KeyError would report that as an incidental
    # error instead of as this assertion.
    assert {k: v for k, v in doc["data"].items() if k in ("diagnostic", "suggestions")} == {
        "diagnostic": None,
        "suggestions": ["ALP_ERR_NO_BACKEND", "ALP_ERR_NOT_READY"],
    }
    # The failure asymmetry `explain` already had: the selector reverts to
    # `overview` with the caller's own input echoed as the value.
    assert doc["data"]["selector"] == {"kind": "overview", "value": "ALP_ERR_NO_BACKENDD"}


def test_a_code_with_no_near_miss_points_at_the_catalogue(tmp_path, monkeypatch):
    sdk = _sdk(tmp_path, {"ALP-B003": _ALP_B003})
    result = _run(tmp_path, monkeypatch, "--code", "ZZZ", "--sdk-root", str(sdk))

    assert result.exit_code == 1
    assert result.stderr.strip() == (
        "explain: unknown code 'ZZZ' -- pass a code from the SDK's "
        "metadata/error-catalog.json."
    )


@pytest.mark.parametrize(
    "other", [["--template", "minimal-app"], ["--target", "zephyr-conf"]]
)
def test_code_cannot_be_combined_with_another_selector(tmp_path, monkeypatch, other):
    result = _run(tmp_path, monkeypatch, "--code", "ALP-B003", *other, "--format", "json")

    assert result.exit_code == 1, result.output
    doc = json.loads(result.stdout)
    assert doc["issues"] == [
        {
            "code": "explain.ambiguous-selector",
            "severity": "error",
            "message": (
                "Use --code on its own; it cannot be combined with --template or "
                "--target."
            ),
        }
    ]


def test_code_cannot_be_combined_with_the_positional_template_id(tmp_path, monkeypatch):
    """`tan explain minimal-app --code ALP-B003`: the positional set
    `template`, not `--template`. The refusal must name the positional, not a
    flag the caller never typed -- the same distinction
    `explain.positional-template-conflict` already draws for the
    positional-vs-flag conflict."""
    result = _run(tmp_path, monkeypatch, "minimal-app", "--code", "ALP-B003", "--format", "json")

    assert result.exit_code == 1, result.output
    doc = json.loads(result.stdout)
    assert doc["issues"] == [
        {
            "code": "explain.ambiguous-selector",
            "severity": "error",
            "message": (
                "Use --code on its own; it cannot be combined with the positional "
                "template id or --target."
            ),
        }
    ]


def test_a_blank_code_is_absent_not_unknown(tmp_path, monkeypatch):
    """Same trim-and-drop rule the other two selectors already have: `--code
    "   "` prints the overview, and -- the part that matters -- resolves NO
    checkout, so it cannot refuse for want of one."""
    result = _run(tmp_path, monkeypatch, "--code", "   ", "--format", "json")

    assert result.exit_code == 0, result.output
    doc = json.loads(result.stdout)
    assert doc["data"]["selector"] == {"kind": "overview", "value": "all"}
    assert "sdk" not in doc


# ---------------------------------------------------------------------------
# The three paths that already existed, proven unmoved
# ---------------------------------------------------------------------------


def test_the_overview_envelope_still_matches_the_frozen_golden(tmp_path, monkeypatch):
    """The strongest regression pin available: the WHOLE envelope, diffed
    against the committed conformance fixture. `--code` adds no line to the
    overview's `details` and no key to `data.available`, so this stays exact."""
    result = _run(tmp_path, monkeypatch, "--format", "json")

    assert result.exit_code == 0, result.output
    expected = json.loads(
        (_CONTRACT / "explain-overview" / "expected.json").read_text(encoding="utf-8")
    )
    assert json.loads(result.stdout) == expected


@pytest.mark.parametrize(
    "argv,kind,value",
    [
        (["--template", "minimal-app"], "project-template", "minimal-app"),
        (["--template", "sensor-driver"], "module-template", "sensor-driver"),
        (["--target", "hw-info-h"], "generation-target", "hw-info-h"),
    ],
)
def test_the_template_and_target_paths_carry_no_code_mode_keys(
    tmp_path, monkeypatch, argv, kind, value
):
    """`data.diagnostic`/`data.suggestions` are `--code`'s alone. An
    unconditional key would be a wire change for three selectors that gained
    nothing -- and the extension reads an absent key with a `?? []` fallback,
    so absence is the honest shape for a mode that did not run. The `sdk` key
    is absent for the same reason: these paths resolve no checkout.

    `som` (tan-cli#866) is the one exception, by design: it IS unconditional
    on a project-template hit (`minimal-app` here), because that selector
    kind is the one with a SoM concept at all -- absent, same as `diagnostic`/
    `suggestions`, on the other two kinds this case also covers."""
    result = _run(tmp_path, monkeypatch, *argv, "--format", "json")

    assert result.exit_code == 0, result.output
    doc = json.loads(result.stdout)
    assert doc["data"]["selector"] == {"kind": kind, "value": value}
    expected_keys = {
        "schemaVersion",
        "selector",
        "summary",
        "details",
        "available",
    }
    if kind == "project-template":
        expected_keys.add("som")
    assert set(doc["data"]) == expected_keys
    assert "sdk" not in doc


def test_all_three_selectors_at_once_reports_the_three_way_message(tmp_path, monkeypatch):
    """The `--code` check runs AHEAD of `resolve()`'s template-vs-target pair
    check, so three selectors report the message that names all three -- the
    pair's "use either --template or --target" would name only two of them.

    This assertion was written the other way round first, claiming the pair
    message won, and failed: the pair check lives INSIDE `resolve()`, which the
    `--code` branch never calls. The comment justifying the ordering in
    `explain_cmd` was corrected to match what the code actually decides."""
    result = _run(
        tmp_path,
        monkeypatch,
        "--template",
        "minimal-app",
        "--target",
        "zephyr-conf",
        "--code",
        "ALP-B003",
        "--format",
        "json",
    )

    assert result.exit_code == 1
    doc = json.loads(result.stdout)
    assert doc["issues"][0]["message"] == (
        "Use --code on its own; it cannot be combined with --template or --target."
    )


def test_the_pair_message_still_wins_when_no_code_is_given(tmp_path, monkeypatch):
    """The other half of the same ordering: with no `--code`, `resolve()`'s own
    check is reached and reports its original, golden-pinned wording."""
    result = _run(
        tmp_path,
        monkeypatch,
        "--template",
        "minimal-app",
        "--target",
        "zephyr-conf",
        "--format",
        "json",
    )

    assert result.exit_code == 1
    doc = json.loads(result.stdout)
    assert doc["issues"][0]["message"] == (
        "Use either --template or --target for explain, not both."
    )
