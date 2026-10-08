# SPDX-License-Identifier: Apache-2.0
"""`deferredPlaceholders` in the build plan, the refusals around it, and the
Mender tenant that no longer references itself (alp-sdk#2696 / #2705 and
alp-sdk#2706 / #2735), ported from alp-sdk's
`tests/scripts/test_build_plan_deferred_placeholders.py` plus the matching
cases of `test_orchestrate_consistency.py` / `test_orchestrate_slices.py`.

A board.yaml value written as `${NAME}` is copied verbatim into a slice config
artefact for the build host or the device to fill.  The plan lists those names
(a top-level list of bare names -- the shape `tan.core.build_plan` reads, see
tan-cli#1306) so the consumer can tell them from a plan path token it must
substitute.  The planner refuses every `${...}` a consumer could not handle,
and the Zephyr fragment refuses a placeholder on a live Kconfig line, because
Zephyr never expands one there.

Real-SDK-gated: loads real SoM presets, the shipped examples and the bound
checkout's `build-plan-v1.schema.json`.
"""
from __future__ import annotations

import json
import textwrap
from pathlib import Path

import jsonschema
import pytest

from tests.planner._bound_sdk_fixture import SDK, _bound_sdk  # noqa: F401

pytestmark = pytest.mark.skipif(
    SDK is None,
    reason="ALP_SDK_ROOT is not set (or does not point at a real alp-sdk "
           "checkout) -- these load real SoM presets, the shipped examples "
           "and the bound build-plan schema. A SKIP about the missing root, "
           "not a pass.",
)

#: Mirrors `tan.planner.buildplan.PLAN_PATH_TOKENS`; spelled out here because
#: a parametrize list is built at collection time, before `_bound_sdk` binds
#: the root that importing `tan.planner` needs.  Pinned equal below.
_PLAN_PATH_TOKENS = ("PROJECT_ROOT", "PYTHON", "SDK_ROOT", "TOOLCHAIN_ROOT")

AEN_OTA = """
som:
  sku: E1M-AEN701

cores:
  a32_cluster:
    os: "off"
  m55_hp:
    os: zephyr
    app: ./m55_hp
  m55_he:
    os: "off"

ota:
{ota}
"""

MENDER = """\
  provider: mender
  artifact_name: alp-aen-test
  server:
    url: "https://hosted.mender.io"
    tenant: "{tenant}"
"""


def _schema() -> dict:
    path = SDK / "metadata" / "schemas" / "build-plan-v1.schema.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _board(tmp: Path, ota: str) -> Path:
    path = tmp / "board.yaml"
    body = textwrap.dedent(AEN_OTA).lstrip("\n").format(ota=ota.rstrip("\n"))
    path.write_text(body, encoding="utf-8")
    return path


def _plan(path: Path) -> dict:
    from tan.planner import emit_build_plan, load_board_yaml

    project = load_board_yaml(path)
    return json.loads(emit_build_plan(project, board_yaml=path, build_root=Path("build")))


def _validate(plan: dict) -> None:
    errors = list(jsonschema.Draft202012Validator(_schema()).iter_errors(plan))
    assert errors == [], "\n".join(str(e) for e in errors)


def _orchestrator_error():
    from tan.planner.models import OrchestratorError

    return OrchestratorError


def test_the_token_list_here_matches_the_planner():
    from tan.planner.buildplan import PLAN_PATH_TOKENS

    assert sorted(PLAN_PATH_TOKENS) == list(_PLAN_PATH_TOKENS)


# --- what the plan lists -------------------------------------------------


def test_board_yaml_placeholder_is_listed(tmp_path: Path) -> None:
    plan = _plan(_board(tmp_path, MENDER.format(tenant="${MENDER_TENANT_TOKEN}")))

    assert plan["deferredPlaceholders"] == ["MENDER_TENANT_TOKEN"]
    _validate(plan)


def test_plan_without_placeholders_carries_an_empty_list(tmp_path: Path) -> None:
    """Always present, so a consumer can tell "none" from an older plan."""
    plan = _plan(_board(tmp_path, MENDER.format(tenant="a-literal-token")))

    assert plan["deferredPlaceholders"] == []
    _validate(plan)


@pytest.mark.parametrize("example", [
    "examples/connectivity/iot-fleet-ota/board.yaml",
    "examples/connectivity/production-deployment/board.yaml",
])
def test_shipped_examples_list_the_mender_tenant(example: str) -> None:
    """The two examples tan-cli#1302 refused."""
    plan = _plan(SDK / example)

    assert plan["deferredPlaceholders"] == ["MENDER_TENANT_TOKEN"]
    _validate(plan)


def test_the_consumer_reads_the_emitted_field(tmp_path: Path) -> None:
    """The in-process planner's field is what `tan.core.build_plan` parses:
    a top-level list of bare names, no parse problem recorded."""
    from tan.core.build_plan import _deferred_placeholders as parse

    plan = _plan(_board(tmp_path, MENDER.format(tenant="${MENDER_TENANT_TOKEN}")))

    assert parse(plan) == (("MENDER_TENANT_TOKEN",), None)


# --- what the emitter refuses --------------------------------------------


@pytest.mark.parametrize("token", _PLAN_PATH_TOKENS)
def test_placeholder_named_like_a_plan_token_is_refused(tmp_path: Path, token: str) -> None:
    """A consumer would replace it with a checkout path."""
    path = _board(tmp_path, MENDER.format(tenant=f"${{{token}}}"))

    with pytest.raises(_orchestrator_error()) as exc:
        _plan(path)
    assert str(exc.value).startswith(f"build plan: `${{{token}}}` in core 'm55_hp' config artefact")
    assert "is a plan path token: a consumer would replace it with a checkout path" in str(exc.value)
    assert str(exc.value).endswith("(issue #2696)")


def test_lower_case_placeholder_is_refused(tmp_path: Path) -> None:
    path = _board(tmp_path, MENDER.format(tenant="${tenant_token}"))

    with pytest.raises(_orchestrator_error()) as exc:
        _plan(path)
    assert "`${tenant_token}`" in str(exc.value)
    assert "is not a valid placeholder name -- use upper-case letters" in str(exc.value)


def test_hawkbit_placeholder_on_a_live_kconfig_line_is_refused(tmp_path: Path) -> None:
    """`CONFIG_HAWKBIT_SERVER` is live; Zephyr would ship the literal text."""
    path = _board(tmp_path, """\
  provider: hawkbit
  server:
    url: "${HAWKBIT_HOST}"
""")

    with pytest.raises(_orchestrator_error()) as exc:
        _plan(path)
    msg = str(exc.value)
    assert msg.startswith("core 'm55_hp': the Zephyr config would carry the "
                          "placeholder `${HAWKBIT_HOST}` literally")
    assert 'CONFIG_HAWKBIT_SERVER="${HAWKBIT_HOST}"' in msg
    assert "Zephyr does not expand environment variables in a Kconfig fragment" in msg


def test_hawkbit_literal_host_still_emits(tmp_path: Path) -> None:
    """The refusal is about the placeholder, not about hawkbit."""
    plan = _plan(_board(tmp_path, """\
  provider: hawkbit
  server:
    url: "https://hawkbit.example.com"
"""))

    conf = plan["slices"][0]["configArtefacts"][0]["contents"]
    assert 'CONFIG_HAWKBIT_SERVER="hawkbit.example.com"' in conf
    assert plan["deferredPlaceholders"] == []


def test_hawkbit_bare_host_is_case_preserved(tmp_path: Path) -> None:
    """A value with no scheme is already a bare host: emitted verbatim, case
    preserved, with port and TLS left at Zephyr's defaults."""
    plan = _plan(_board(tmp_path, """\
  provider: hawkbit
  server:
    url: "OTA.Example.lan"
"""))

    conf = plan["slices"][0]["configArtefacts"][0]["contents"]
    assert 'CONFIG_HAWKBIT_SERVER="OTA.Example.lan"' in conf
    for line in conf.splitlines():
        assert not line.startswith("CONFIG_HAWKBIT_PORT")
        assert not line.startswith("CONFIG_HAWKBIT_USE_TLS")


def test_commented_placeholder_line_is_allowed():
    """Only a LIVE line is refused; a `#` line is inert."""
    from tan.planner.kconfig import _refuse_live_kconfig_placeholders

    _refuse_live_kconfig_placeholders('# CONFIG_X="${A}"\nCONFIG_Y=y\n', "m55_hp")
    with pytest.raises(_orchestrator_error(), match=r"placeholder `\$\{A\}` literally"):
        _refuse_live_kconfig_placeholders('CONFIG_X="${A}"\n', "m55_hp")


# --- the guard itself, on synthetic plans --------------------------------


def _slice(**over) -> dict:
    base = {
        "coreId": "m55_hp",
        "configArtefacts": [],
        "command": {"tool": "west", "args": ["build", "${PROJECT_ROOT}/app"]},
        "env": {"ALP_SDK_ROOT": "${SDK_ROOT}"},
        "envAppendPath": {"PYTHONPATH": ["${SDK_ROOT}/scripts"]},
        "appDir": "${PROJECT_ROOT}/app",
        "postCommands": [],
    }
    base.update(over)
    return base


def _yaml(tmp: Path, text: str = 'tenant: "${FROM_BOARD}"\n') -> Path:
    path = tmp / "board.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def _guard(*args):
    from tan.planner.buildplan import _deferred_placeholders

    return _deferred_placeholders(*args)


def test_guard_lists_a_board_yaml_name(tmp_path: Path) -> None:
    artefact = {"path": "build/a/local.conf", "contents": 'X ?= "${FROM_BOARD}"\n'}

    assert _guard([_slice(configArtefacts=[artefact])], [], _yaml(tmp_path)) == ["FROM_BOARD"]


def test_guard_refuses_a_placeholder_in_a_non_conf_artefact(tmp_path: Path) -> None:
    """CMake would expand `${NAME}` in `alp-baremetal.cmake` itself."""
    artefact = {"path": "build/a/alp-baremetal.cmake", "contents": 'set(X "${FROM_BOARD}")\n'}

    with pytest.raises(_orchestrator_error()) as exc:
        _guard([_slice(configArtefacts=[artefact])], [], _yaml(tmp_path))
    assert str(exc.value) == (
        "build plan: `${FROM_BOARD}` in core 'm55_hp' config artefact "
        "`build/a/alp-baremetal.cmake` is in a non-`.conf` artefact, which its "
        "reader (e.g. CMake) would expand itself -- a placeholder may only "
        "reach a Kconfig fragment or local.conf (issue #2696)")


def test_guard_refuses_a_name_the_planner_invented(tmp_path: Path) -> None:
    """Not in board.yaml: an unresolved token, not a deferred placeholder."""
    artefact = {"path": "build/a/local.conf", "contents": 'X = "${NOT_IN_BOARD}"\n'}

    with pytest.raises(_orchestrator_error()) as exc:
        _guard([_slice(configArtefacts=[artefact])], [], _yaml(tmp_path))
    assert "`${NOT_IN_BOARD}`" in str(exc.value)
    assert ("does not come from board.yaml, so the planner produced it -- an "
            "unresolved token") in str(exc.value)


@pytest.mark.parametrize("field,value", [
    ("command", {"tool": "west", "args": ["-DX=${FROM_BOARD}"]}),
    ("env", {"X": "${FROM_BOARD}"}),
    ("envAppendPath", {"PATH": ["${FROM_BOARD}/bin"]}),
    ("appDir", "${FROM_BOARD}/app"),
    ("postCommands", [{"tool": "x", "args": ["${FROM_BOARD}"], "cwd": "b"}]),
])
def test_guard_refuses_a_placeholder_outside_config_artefacts(
        tmp_path: Path, field: str, value: object) -> None:
    with pytest.raises(_orchestrator_error()) as exc:
        _guard([_slice(**{field: value})], [], _yaml(tmp_path))
    assert str(exc.value) == (
        "build plan: `${FROM_BOARD}` in core 'm55_hp' command/env/appDir is not "
        "a plan path token -- a placeholder may only appear in a config "
        "artefact (issue #2696)")


def test_guard_refuses_a_placeholder_in_a_shared_artefact(tmp_path: Path) -> None:
    shared = [{"path": "build/generated/x.h", "contents": "#define X \"${FROM_BOARD}\"\n"}]

    with pytest.raises(_orchestrator_error()) as exc:
        _guard([_slice()], shared, _yaml(tmp_path))
    assert str(exc.value) == (
        "build plan: `${FROM_BOARD}` in shared artefact `build/generated/x.h` "
        "cannot be resolved -- a placeholder may only appear in a slice config "
        "artefact (issue #2696)")


def test_guard_leaves_plan_tokens_in_command_and_env_alone(tmp_path: Path) -> None:
    assert _guard([_slice()], [], _yaml(tmp_path)) == []


# --- the schema ----------------------------------------------------------


@pytest.mark.parametrize("bad", [["lower_case"], ["DUP", "DUP"], ["1LEADING_DIGIT"], "NOT_A_LIST"])
def test_schema_rejects_malformed_lists(tmp_path: Path, bad: object) -> None:
    plan = _plan(_board(tmp_path, MENDER.format(tenant="${MENDER_TENANT_TOKEN}")))
    plan["deferredPlaceholders"] = bad

    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(plan, _schema())


def test_schema_accepts_a_plan_without_the_field(tmp_path: Path) -> None:
    """Additive under schemaVersion 1: a pre-#2696 plan stays valid."""
    plan = _plan(_board(tmp_path, MENDER.format(tenant="x")))
    del plan["deferredPlaceholders"]

    _validate(plan)


# --- Yocto local.conf: no self-referencing MENDER_TENANT_TOKEN (#2706) ---


def _mender_local_conf(tmp_path: Path, tenant: str) -> str:
    from tan.planner import _slice_local_conf, load_board_yaml

    path = tmp_path / "board.yaml"
    path.write_text(textwrap.dedent(f"""\
        som:
          sku: E1M-V2N101

        cores:
          a55_cluster:
            os: yocto
            app: ./linux
            image: alp-image-edge

        ota:
          provider: mender
          server:
            url: "https://hosted.mender.io"
            tenant: "{tenant}"
        """), encoding="utf-8")
    project = load_board_yaml(path)
    return _slice_local_conf(project, project.cores["a55_cluster"])


def test_local_conf_mender_tenant_placeholder_not_emitted(tmp_path: Path) -> None:
    """A ${NAME} tenant is not expanded by BitBake; emit no assignment."""
    conf = _mender_local_conf(tmp_path, "${MENDER_TENANT_TOKEN}")

    assert "MENDER_TENANT_TOKEN ?=" not in conf
    assert "${MENDER_TENANT_TOKEN}" not in conf
    assert ("# MENDER_TENANT_TOKEN: set it in conf/local.conf "
            "(meta-alp-sdk/README.md, Mender step 3); the board.yaml "
            "placeholder is not expanded by BitBake.") in conf.splitlines()
    assert 'MENDER_SERVER_URL ?= "https://hosted.mender.io"' in conf


def test_local_conf_mender_tenant_literal_emitted(tmp_path: Path) -> None:
    conf = _mender_local_conf(tmp_path, "abc123")

    assert 'MENDER_TENANT_TOKEN ?= "abc123"' in conf
