# SPDX-License-Identifier: Apache-2.0
"""tan-cli#1302: a `${NAME}` the user's board.yaml writes on purpose (alp-sdk's
`connectivity/iot-fleet-ota` writes `ota.server.tenant: "${MENDER_TENANT_TOKEN}"`)
reaches a config artefact verbatim. The unresolved-token guard must keep
refusing a genuinely unresolved SDK-side token (tan-cli#89, #547) while letting
this one through -- only inside `configArtefacts[*].contents`, only for a name
that is not a plan path token, and only when the plan's `deferredPlaceholders`
(alp-sdk#2696) names it. A plan with NO such key admits nothing, whatever the
project's board.yaml says: tan's own planner always emits the key (alp-sdk
`d7d17c7ae`, tan-cli#1309), so tan-cli#1306's interim board.yaml whole-value
rule is gone.

Where the placeholder sits decides its severity: a live Kconfig line (Kconfig
does not expand `${NAME}`) is a `warning`, a commented Kconfig line or a Yocto
`local.conf` is `info`, a `.cmake` file is refused.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tan.commands.build.token_substitution import (
    TokenSubstitutionError,
    apply_plan_token_substitution,
    deferred_placeholder_issues,
)
from tan.core.build_plan import parse_build_plan

PACKAGE_ROOT = Path(__file__).resolve().parents[2]
NAME = "MENDER_TENANT_TOKEN"
TOKEN = "${" + NAME + "}"
CONF = f'MENDER_TENANT_TOKEN ?= "{TOKEN}"\n'
BOARD_WITH = f'ota:\n  server:\n    tenant: "{TOKEN}"\n'
BOARD_WITHOUT = "som:\n  sku: E1M-TEST\n"
UNRESOLVED_PREFIX = "plan is `planPathMode: tokened` but field `"


def _slice(
    *, contents=CONF, env=None, app_dir=None, args=None, env_append=None,
    core="a32", art_path="build/a32/local.conf",
):
    return {
        "coreId": core,
        "backend": "yocto",
        "buildDir": f"build/{core}",
        "appDir": app_dir,
        "configArtefacts": [{"path": art_path, "contents": contents}],
        "toolchain": {"targetTriple": None, "compiler": None, "sysroot": None, "id": "x"},
        "artifacts": {
            "elf": None, "map": None, "bin": None,
            "sizeReport": None, "symbols": None, "compileCommands": None,
        },
        "debug": {"console": "rtt", "probe": None},
        "command": {"tool": "bitbake", "args": args or ["image"], "cwd": "build/a32"},
        "env": env or {},
        "envAppendPath": env_append or {},
    }


def _plan(*, extra=None, **slice_kw):
    """A tokened plan listing `NAME` in `deferredPlaceholders`, the shape tan's
    own planner emits for alp-sdk's Mender examples. `extra` overrides any
    top-level key; `_plan_without_key` drops it."""
    doc = {
        "schemaVersion": 1,
        "generatedBy": "t",
        "planPathMode": "tokened",
        "boardYaml": "${PROJECT_ROOT}/board.yaml",
        "sku": "E1M-TEST",
        "buildRoot": "build",
        "slices": [_slice(**slice_kw)],
        "sharedArtefacts": [],
        "warnings": [],
        "deferredPlaceholders": [NAME],
    }
    doc.update(extra or {})
    return doc


def _plan_without_key(**slice_kw):
    """An older or hand-written plan: no `deferredPlaceholders` at all."""
    doc = _plan(**slice_kw)
    del doc["deferredPlaceholders"]
    return doc


def _apply(tmp_path, doc, board_text=BOARD_WITH, toolchain_root=None):
    """Run the substitution layer against a real board.yaml on disk. Returns
    (plan, deferred)."""
    proj = tmp_path / "proj"
    proj.mkdir(exist_ok=True)
    board = proj / "board.yaml"
    if board_text is not None:
        board.write_text(board_text, encoding="utf-8")
    plan = parse_build_plan(json.dumps(doc))
    deferred = []
    out, _ = apply_plan_token_substitution(
        plan,
        board_yaml_path=str(board),
        exec_base=str(proj).replace("\\", "/"),
        sdk_root=str(tmp_path / "sdk"),
        python="python3",
        toolchain_root=toolchain_root,
        deferred_out=deferred,
    )
    return out, deferred


def _refused(tmp_path, doc, board_text=BOARD_WITH):
    with pytest.raises(TokenSubstitutionError) as e:
        _apply(tmp_path, doc, board_text)
    assert e.value.code == "build.plan-token-unresolved"
    return e.value.message


# --- the iot-fleet-ota shape -------------------------------------------------


def test_a_listed_placeholder_stays_in_a_config_artefact(tmp_path):
    out, deferred = _apply(tmp_path, _plan())
    assert out.slices[0].config_artefacts[0]["contents"] == CONF  # byte-identical
    assert [(d.name, d.field) for d in deferred] == [
        (NAME, "slices[0].configArtefacts[0].contents")
    ]


def test_issue_is_reported_once_per_name(tmp_path):
    doc = _plan(contents=CONF + CONF)
    doc["slices"].append(_slice())
    out, deferred = _apply(tmp_path, doc)
    assert len(deferred_placeholder_issues(out, deferred)) == 1


def test_a_plan_without_the_key_refuses_even_when_board_yaml_writes_the_value(tmp_path):
    """tan-cli#1309 retired tan-cli#1306's interim rule (b): a board.yaml whose
    string VALUE is exactly `${NAME}` no longer admits it. Only the plan's own
    list does, and a plan without the key lists nothing."""
    msg = _refused(tmp_path, _plan_without_key(), BOARD_WITH)
    assert msg == (
        "plan is `planPathMode: tokened` but field "
        "`slices[0].configArtefacts[0].contents` still names the literal token "
        "`${MENDER_TENANT_TOKEN}` after substitution -- an SDK-side token this CLI does "
        "not resolve (only ${SDK_ROOT}, ${PROJECT_ROOT}, ${PYTHON}, ${TOOLCHAIN_ROOT} "
        "are known). Upgrade tan, or check the plan for a bug."
        " If `${MENDER_TENANT_TOKEN}` is a placeholder meant for the build host or the "
        "device, it is accepted only when the plan's `deferredPlaceholders` lists "
        "`MENDER_TENANT_TOKEN` (alplabai/alp-sdk#2696); this plan carries no "
        "`deferredPlaceholders` at all -- re-emit it with a current planner."
    )


# --- where the exemption must NOT reach --------------------------------------


@pytest.mark.parametrize(
    "kw, field",
    [
        ({"args": [f"--tenant={TOKEN}"]}, "slices[0].command.args[0]"),
        ({"env": {"TENANT": TOKEN}}, "slices[0].env.TENANT"),
        ({"env_append": {"PATH": [TOKEN]}}, "slices[0].envAppendPath.PATH[0]"),
        ({"app_dir": f"app/{TOKEN}"}, "slices[0].appDir"),
    ],
)
def test_same_placeholder_outside_config_contents_still_refuses(tmp_path, kw, field):
    # The plan lists it: still refused outside configArtefacts[*].contents.
    doc = _plan(contents="CLEAN=1\n", **kw)
    msg = _refused(tmp_path, doc, BOARD_WITH)
    assert f"field `{field}` still names the literal token `{TOKEN}`" in msg


def test_config_artefact_path_still_refuses(tmp_path):
    doc = _plan()
    doc["slices"][0]["configArtefacts"][0]["path"] = f"build/{TOKEN}/local.conf"
    msg = _refused(tmp_path, doc)
    assert "configArtefacts[0].path" in msg


def test_shared_artefact_contents_still_refuse(tmp_path):
    doc = _plan(contents="CLEAN=1\n")
    doc["sharedArtefacts"] = [{"path": "build/shared.h", "contents": CONF}]
    assert "sharedArtefacts[0].contents" in _refused(tmp_path, doc)


@pytest.mark.parametrize("name", ["lower", "1BAD", "Mixed_Case", "WITH-DASH"])
def test_malformed_names_are_never_exempt_even_when_listed(tmp_path, name):
    token = "${" + name + "}"
    doc = _plan(extra={"deferredPlaceholders": [name]}, contents=f"X={token}\n")
    assert f"`{token}`" in _refused(tmp_path, doc)


@pytest.mark.parametrize("name", ["SDK_ROOTX", "SDK_ROOT_", "TOOLCHAIN_ROOT2"])
def test_a_name_that_only_resembles_a_plan_token_is_an_ordinary_placeholder(tmp_path, name):
    """Not a plan token, so a plan that lists it admits it like any other name
    -- and one that does not list it refuses it."""
    token = "${" + name + "}"
    doc = _plan(extra={"deferredPlaceholders": [name]}, contents=f"X={token}\n")
    out, deferred = _apply(tmp_path, doc)
    assert [d.name for d in deferred] == [name]
    assert out.slices[0].config_artefacts[0]["contents"] == f"X={token}\n"
    assert f"`{token}`" in _refused(tmp_path, _plan(contents=f"X={token}\n"))


@pytest.mark.parametrize("name", ["SDK_ROOT", "PROJECT_ROOT", "PYTHON", "TOOLCHAIN_ROOT"])
def test_real_plan_tokens_are_substituted_not_deferred(tmp_path, name):
    """Listing a plan token as deferred must not stop it being substituted (or,
    for an unresolved TOOLCHAIN_ROOT, demoted)."""
    doc = _plan(extra={"deferredPlaceholders": [name]}, contents="X=${%s}\n" % name)
    out, deferred = _apply(tmp_path, doc, "x: ${%s}\n" % name)
    assert deferred == []
    if name != "TOOLCHAIN_ROOT":
        assert "${" not in out.slices[0].config_artefacts[0]["contents"]


# --- the plan's own `deferredPlaceholders` -----------------------------------


def test_plan_listing_exempts_without_any_board_yaml_mention(tmp_path):
    out, deferred = _apply(
        tmp_path, _plan(extra={"deferredPlaceholders": [NAME]}), BOARD_WITHOUT
    )
    assert out.slices[0].config_artefacts[0]["contents"] == CONF
    (issue,) = deferred_placeholder_issues(out, deferred)
    assert issue.message == (
        "placeholder `${MENDER_TENANT_TOKEN}` in `slices[0].configArtefacts[0].contents` "
        "is left as written for the build host or the device to supply; tan does not "
        "substitute it"
    )


def test_empty_plan_list_admits_nothing(tmp_path):
    assert _refused(tmp_path, _plan(extra={"deferredPlaceholders": []}), BOARD_WITH)


def test_name_missing_from_the_plan_list_is_not_exempt(tmp_path):
    doc = _plan(extra={"deferredPlaceholders": ["OTHER_TOKEN"]})
    assert f"`{TOKEN}`" in _refused(tmp_path, doc, BOARD_WITH)


def test_absent_key_parses_as_none():
    assert parse_build_plan(json.dumps(_plan_without_key())).deferred_placeholders is None


# --- end to end through `tan build --materialise` ----------------------------


def _run(argv, cwd):
    env = {**os.environ, "PATH": "", "PYTHONPATH": str(PACKAGE_ROOT)}
    return subprocess.run(
        [sys.executable, "-m", "tan", *argv],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=cwd, env=env,
    )


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    sdk = tmp_path / "sdk"
    (sdk / "scripts").mkdir(parents=True)
    (sdk / "scripts" / "alp_project.py").write_text("", encoding="utf-8")
    return root, sdk


def _materialise(project, doc, board_text):
    root, sdk = project
    (root / "board.yaml").write_text(board_text, encoding="utf-8")
    plan_file = root / "plan.json"
    plan_file.write_text(json.dumps(doc), encoding="utf-8")
    proc = _run(
        ["build", "--plan-from", str(plan_file), "--materialise",
         "--sdk-root", str(sdk), "--format", "json"],
        root,
    )
    assert proc.stdout.strip(), proc.stderr
    return proc, json.loads(proc.stdout), root


def test_materialise_writes_the_placeholder_verbatim_and_reports_it(project):
    proc, env, root = _materialise(project, _plan(), BOARD_WITH)
    assert proc.returncode == 0, env
    assert proc.stderr == ""
    assert (root / "build/a32/local.conf").read_bytes() == CONF.encode()
    assert [(i["code"], i["severity"]) for i in env["issues"]] == [
        ("build.deferred-placeholder", "info")
    ]
    assert "`${MENDER_TENANT_TOKEN}` in `slices[0].configArtefacts[0].contents`" in (
        env["issues"][0]["message"]
    )


def test_materialise_refuses_and_writes_nothing_for_a_plan_without_the_key(project):
    """End to end: the board.yaml writes the placeholder as a whole value --
    what tan-cli#1306's rule (b) admitted -- and the plan carries no
    `deferredPlaceholders`, so `tan build` refuses and writes nothing."""
    proc, env, root = _materialise(project, _plan_without_key(), BOARD_WITH)
    assert proc.returncode != 0
    assert [i["code"] for i in env["issues"]] == ["build.plan-token-unresolved"]
    assert not (root / "build/a32/local.conf").exists()


# --- a CMake variable is not a placeholder ----------------------------------


def test_nanopb_cmake_variable_the_plan_does_not_list_is_refused(tmp_path):
    """alp-sdk examples/connectivity/nanopb-encode-decode/board.yaml mentions
    `${ZEPHYR_NANOPB_MODULE_DIR}` (a CMake host-path variable) in a comment;
    a plan leftover of that name is not in `deferredPlaceholders`, so it must
    not pass as a placeholder."""
    var = "${ZEPHYR_NANOPB_MODULE_DIR}"
    board = f"# generator lives under {var}/generator\nsom:\n  sku: E1M-TEST\n"
    assert f"`{var}`" in _refused(tmp_path, _plan(contents=f"P={var}\n"), board)


# --- where the placeholder sits decides the severity -------------------------


def _typed(tmp_path, art_path, contents):
    out, deferred = _apply(tmp_path, _plan(contents=contents, art_path=art_path))
    return deferred_placeholder_issues(out, deferred)


def test_live_kconfig_line_is_a_warning_saying_kconfig_does_not_expand_it(tmp_path):
    (issue,) = _typed(tmp_path, "build/m55/alp.conf", f'CONFIG_HAWKBIT_SERVER="{TOKEN}"\n')
    assert (issue.code, issue.severity) == ("build.deferred-placeholder", "warning")
    assert issue.message == (
        "placeholder `${MENDER_TENANT_TOKEN}` in `slices[0].configArtefacts[0].contents` sits "
        "on a live line of a Kconfig fragment; Kconfig does not expand "
        "`${MENDER_TENANT_TOKEN}`, so the firmware will carry the literal text unless "
        "something substitutes it before the build"
    )


def test_commented_kconfig_line_is_info(tmp_path):
    (issue,) = _typed(tmp_path, "build/m55/alp.conf", f'  # CONFIG_X="{TOKEN}"\n')
    assert issue.severity == "info"


def test_any_live_occurrence_makes_the_name_a_warning(tmp_path):
    contents = f'# CONFIG_X="{TOKEN}"\nCONFIG_Y="{TOKEN}"\n'
    (issue,) = _typed(tmp_path, "build/m55/alp.conf", contents)
    assert issue.severity == "warning"


def test_yocto_local_conf_is_info_even_on_a_live_line(tmp_path):
    (issue,) = _typed(tmp_path, "build/a32/local.conf", CONF)
    assert issue.severity == "info"


@pytest.mark.parametrize("art", ["build/m33/alp-baremetal.cmake", "build/m33/notes.txt"])
def test_cmake_and_unknown_artefacts_are_never_exempt(tmp_path, art):
    msg = _refused(tmp_path, _plan(art_path=art))
    assert f"`{TOKEN}`" in msg
    assert "placeholder meant for the build host" not in msg  # nothing would cure it


# --- the refusal itself carries the explanation ------------------------------


def test_refusal_with_a_plan_list_that_omits_the_name_says_so(tmp_path):
    msg = _refused(tmp_path, _plan(extra={"deferredPlaceholders": ["OTHER"]}))
    assert msg.endswith(
        " If `${MENDER_TENANT_TOKEN}` is a placeholder meant for the build host or the "
        "device, the plan's `deferredPlaceholders` has to list `MENDER_TENANT_TOKEN` "
        "(alplabai/alp-sdk#2696); it does not."
    )


def test_refusal_outside_config_contents_carries_no_hint(tmp_path):
    msg = _refused(tmp_path, _plan(env={"T": TOKEN}, contents="CLEAN=1\n"))
    assert msg.endswith("Upgrade tan, or check the plan for a bug.")


# --- malformed / odd `deferredPlaceholders` ----------------------------------


def test_null_is_absent_not_malformed():
    plan = parse_build_plan(json.dumps(_plan(extra={"deferredPlaceholders": None})))
    assert (plan.deferred_placeholders, plan.deferred_placeholders_problem) == (None, None)


def test_duplicates_are_folded():
    plan = parse_build_plan(json.dumps(_plan(extra={"deferredPlaceholders": ["A", "B", "A"]})))
    assert plan.deferred_placeholders == ("A", "B")


def test_a_listed_plan_token_is_a_planner_bug_warning_and_never_exempt(tmp_path):
    doc = _plan(extra={"deferredPlaceholders": ["SDK_ROOT", NAME]})
    out, deferred = _apply(tmp_path, doc, BOARD_WITHOUT)
    assert [d.name for d in deferred] == [NAME]
    warn, info = deferred_placeholder_issues(out, deferred)
    assert (warn.code, warn.severity) == ("build.plan-warning", "warning")
    assert warn.message == (
        "[deferred-placeholders-plan-token] `deferredPlaceholders` lists the plan path "
        "token(s) `SDK_ROOT` -- a planner bug: tan substitutes those and they are never "
        "deferred; ignoring the entries"
    )
    assert info.code == "build.deferred-placeholder"


def test_malformed_warning_text_depends_on_the_plan_being_tokened():
    doc = _plan(extra={"deferredPlaceholders": "X"})
    tokened = parse_build_plan(json.dumps(doc))
    (w,) = deferred_placeholder_issues(tokened, [])
    assert w.message == (
        "[deferred-placeholders-malformed] `deferredPlaceholders` must be a list of "
        "placeholder names -- ignoring the field, so no `${NAME}` placeholder is exempt "
        "from the unresolved-token refusal"
    )
    del doc["planPathMode"]
    (w,) = deferred_placeholder_issues(parse_build_plan(json.dumps(doc)), [])
    assert w.message == (
        "[deferred-placeholders-malformed] `deferredPlaceholders` must be a list of "
        "placeholder names -- ignoring the field"
    )


@pytest.mark.parametrize(
    "bad, why",
    [
        ("MENDER_TENANT_TOKEN", "`deferredPlaceholders` must be a list of placeholder names"),
        ([5], "`deferredPlaceholders[0]` must be a string matching `^[A-Z][A-Z0-9_]*$`"),
        (["ok_lower"], "`deferredPlaceholders[0]` must be a string matching `^[A-Z][A-Z0-9_]*$`"),
    ],
)
def test_malformed_list_is_explained_in_the_enveloped_refusal(project, bad, why):
    """A malformed key lists nothing, so the build is refused -- and the
    envelope must say why, not merely blame an unknown SDK-side token."""
    proc, env, root = _materialise(
        project, _plan(extra={"deferredPlaceholders": bad}), BOARD_WITH
    )
    assert proc.returncode != 0
    assert [i["code"] for i in env["issues"]] == ["build.plan-token-unresolved"]
    msg = env["issues"][0]["message"]
    assert msg.startswith(UNRESOLVED_PREFIX)
    assert msg.endswith(f" Note: {why} -- the field was ignored, so no `${{NAME}}` placeholder was exempt.")
    assert not (root / "build/a32/local.conf").exists()


# --- a demoted slice's admissions are dropped with its artefacts -------------


def test_demoted_slice_admissions_are_not_reported(tmp_path):
    demoted = _slice(core="m55", args=["build", "${TOOLCHAIN_ROOT}/bin/x"])
    kept = _slice(core="a32")
    doc = _plan()
    doc["slices"] = [demoted, kept]
    out, deferred = _apply(tmp_path, doc, BOARD_WITHOUT, toolchain_root=None)
    assert out.slices[0].config_artefacts == []  # demoted: artefacts stripped
    assert [(d.name, d.field) for d in deferred] == [
        (NAME, "slices[1].configArtefacts[0].contents")
    ]
