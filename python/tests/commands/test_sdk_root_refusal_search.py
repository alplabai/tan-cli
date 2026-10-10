# SPDX-License-Identifier: Apache-2.0
"""tan-cli#1463: every SDK-root refusal names where tan looked (the shared
`with_sdk_search`), and `size` / `run` refuse a REJECTED explicit `--sdk-root`
with a coded issue instead of continuing silently."""

import pytest

from tan.core.sdk_discovery import sdk_search_summary, with_sdk_search
from tests.commands.test_build_command import envelope_of, run_tan

BOARD_YAML = "board:\n  name: demo\n"

# name -> (argv after `--format json`, issue code the refusal carries)
COMMANDS = {
    "model-build": (["model", "build"], "model.sdk-root-unresolved"),
    "model-check": (["model", "check"], "model.sdk-root-unresolved"),
    "model-doctor": (["model", "doctor"], "model.doctor-sdk-unresolved"),
    "generate": (
        ["generate", "--target", "zephyr-conf", "--core", "m55"],
        "generate.sdk-root-unresolved",
    ),
    "trace": (["trace"], "trace.sdk-root-unresolved"),
    "validate": (["validate"], "validate.sdk-root-unresolved"),
    "presets": (["presets"], "presets.sdk-root-unresolved"),
    "pinmux": (["pinmux", "--family", "aen"], "pinmux.sdk-root-unresolved"),
    "examples": (["examples"], "examples.sdk-root-unresolved"),
    "explain": (
        ["explain", "--code", "build.sdk-root-unresolved"],
        "explain.sdk-root-unresolved",
    ),
    "init-from-example": (
        ["init", "--from-example", "x", "--name", "p2"],
        "init.sdk-root-unresolved",
    ),
    "kconfig": (["kconfig"], "kconfig.no-sdk-root"),
}


def _scratch(tmp_path):
    project = tmp_path / "scratch" / "proj"
    project.mkdir(parents=True)
    (project / "board.yaml").write_text(BOARD_YAML, encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir()
    return project, {"HOME": str(home), "USERPROFILE": str(home)}


def _message(doc, code):
    found = [i["message"] for i in doc["issues"] if i["code"] == code]
    assert len(found) == 1, doc["issues"]
    return found[0]


def test_with_sdk_search_appends_the_ladder_summary(tmp_path):
    assert with_sdk_search("boom.", tmp_path) == f"boom. {sdk_search_summary(tmp_path)}"


def test_with_sdk_search_leaves_a_rejected_flag_alone(tmp_path):
    # `--sdk-root` is terminal (I-31): the ladder was never searched.
    assert with_sdk_search("boom.", tmp_path, "/typo") == "boom."


@pytest.mark.parametrize("name", sorted(COMMANDS))
def test_each_refusal_names_where_it_looked(name, tmp_path):
    argv, code = COMMANDS[name]
    project, env = _scratch(tmp_path)
    proc = run_tan("--format", "json", *argv, cwd=project, env_overrides=env)
    message = _message(envelope_of(proc), code)
    assert f"`{(project / '.alp' / 'sdk-path').as_posix()}`" in message
    assert "in a directory above" in message


def test_a_rejected_flag_is_named_not_searched(tmp_path):
    project, env = _scratch(tmp_path)
    bad = tmp_path / "not-an-sdk"
    bad.mkdir()
    proc = run_tan(
        "--format", "json", "model", "build", "--sdk-root", str(bad),
        cwd=project, env_overrides=env,
    )
    message = _message(envelope_of(proc), "model.sdk-root-unresolved")
    assert str(bad) in message
    assert "in a directory above" not in message


def test_size_refuses_a_rejected_explicit_sdk_root(tmp_path):
    project, env = _scratch(tmp_path)
    bad = tmp_path / "not-an-sdk"
    bad.mkdir()
    proc = run_tan(
        "--format", "json", "size", "--build-root", str(project), "--sdk-root", str(bad),
        cwd=project, env_overrides=env,
    )
    doc = envelope_of(proc)
    assert proc.returncode == 1
    assert str(bad) in _message(doc, "size.sdk-root-unresolved")


def test_size_without_the_flag_keeps_going(tmp_path):
    # No `--sdk-root` + no checkout: nothing was asked for, so the manifest
    # gate (not an SDK-root refusal) is what answers.
    project, env = _scratch(tmp_path)
    proc = run_tan(
        "--format", "json", "size", "--build-root", str(project),
        cwd=project, env_overrides=env,
    )
    codes = [i["code"] for i in envelope_of(proc)["issues"]]
    assert "size.sdk-root-unresolved" not in codes
    assert "size.manifest-unavailable" in codes


def test_run_refuses_a_rejected_explicit_sdk_root(tmp_path):
    project, env = _scratch(tmp_path)
    bad = tmp_path / "not-an-sdk"
    bad.mkdir()
    proc = run_tan(
        "--format", "json", "run", "--sdk-root", str(bad),
        cwd=project, env_overrides=env,
    )
    doc = envelope_of(proc)
    assert proc.returncode == 2
    assert doc["ok"] is False
    assert str(bad) in _message(doc, "run.sdk-root-unresolved")


def test_image_helper_clause_names_where_it_looked(tmp_path):
    from tan.commands.image_cmd import _unresolved_sdk_clause

    clause = _unresolved_sdk_clause(None, str(tmp_path))
    assert clause.startswith("sdk root not resolved (no --sdk-root and no discoverable checkout)")
    assert "in a directory above" in clause
    # A rejected flag is terminal: named, not searched.
    assert "in a directory above" not in _unresolved_sdk_clause("/typo", str(tmp_path))
