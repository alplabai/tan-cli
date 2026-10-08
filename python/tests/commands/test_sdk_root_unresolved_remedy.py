# SPDX-License-Identifier: Apache-2.0
"""tan-cli#1423: a project outside every alp-sdk checkout gets `build.sdk-root-
unresolved` / `flash.sdk-root-not-found` naming where tan looked and the
`--sdk-root` flag that fixes it, not a bare "Cannot locate alp-sdk root."."""

from pathlib import Path

from tan.core.sdk_discovery import sdk_search_summary
from tests.commands.test_build_command import envelope_of, run_tan

BOARD = "alp_e1m_aen803_m55_he/ae822fa0e5597ls0/rtss_he"


def _scratch(tmp_path: Path) -> tuple[Path, dict]:
    project = tmp_path / "scratch" / "proj"
    project.mkdir(parents=True)
    (project / "main.c").write_text("int main(void) { return 0; }\n", encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir()
    return project, {"HOME": str(home), "USERPROFILE": str(home)}


def _message(doc: dict, code: str) -> str:
    found = [i["message"] for i in doc["issues"] if i["code"] == code]
    assert len(found) == 1, doc["issues"]
    return found[0]


def test_the_summary_names_every_tier_the_ladder_tries(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    root = tmp_path / "proj"
    text = sdk_search_summary(root)
    for part in (
        "`--sdk-root`",
        f"`{(root / '.alp' / 'sdk-path').as_posix()}`",
        f"`{(tmp_path / 'home' / '.alp' / 'sdk-default').as_posix()}`",
        f"`{(root / 'alp-sdk').as_posix()}`",
        f"`{(tmp_path / 'alp-sdk').as_posix()}`",
        f"`{(tmp_path / 'alp-sdk-upstream').as_posix()}`",
        "in a directory above",
    ):
        assert part in text, part
    assert "ALP_SDK_ROOT" not in text  # not a tier; never offered as a remedy


def test_flash_names_where_it_looked_and_the_flag(tmp_path):
    project, env = _scratch(tmp_path)
    doc = envelope_of(run_tan("--format", "json", "flash", "--ram", "--build-root", str(project),
                              cwd=project, env_overrides=env))
    message = _message(doc, "flash.sdk-root-not-found")
    assert message.startswith("Cannot locate alp-sdk root. Neither `--sdk-root`")
    assert f"`{(project / '.alp' / 'sdk-path').as_posix()}`" in message
    assert "Pass `--sdk-root <path to an alp-sdk checkout>`" in message


def test_build_names_where_it_looked(tmp_path):
    project, env = _scratch(tmp_path)
    doc = envelope_of(run_tan("--format", "json", "build", "--project", str(project),
                              "--board", BOARD, cwd=project, env_overrides=env))
    message = _message(doc, "build.sdk-root-unresolved")
    assert "pass `--sdk-root <PATH>`" in message
    assert f"`{(project / '.alp' / 'sdk-path').as_posix()}`" in message
    assert "in a directory above" in message
