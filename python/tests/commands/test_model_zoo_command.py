# SPDX-License-Identifier: Apache-2.0
"""`tan model zoo` / `tan model add` (tan-cli#1286), end to end through the
typer command against a fixture SDK carrying `metadata/model_zoo/`."""
from __future__ import annotations

import json
from pathlib import Path

import typer
from typer.testing import CliRunner

from tan.commands.model_cmd import model

app = typer.Typer(add_completion=False)
app.command("model")(model)
runner = CliRunner()

BOARD = (
    "# top comment\nsom:\n  sku: E1M-AEN801  # soc\n\n# models\nmodels:\n"
    "  - name: old  # keep\n    source: old.tflite\n\nrest: 1  # tail\n"
)


def write(path: Path, text: str | bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(text, bytes):
        path.write_bytes(text)
    else:
        path.write_text(text, encoding="utf-8", newline="")


def make_sdk(root: Path, *, zoo: bool = True) -> Path:
    write(root / "scripts" / "alp_project.py", "")
    if zoo:
        d = root / "metadata" / "model_zoo"
        write(d / "starters" / "t.tflite", b"TFL3starter")
        write(
            d / "t.yaml",
            "schema_version: 1\nkind: fixture\nid: t\ntask: smoke\ndescription: tiny\n"
            "license: MIT\nsource:\n  bundled: starters/t.tflite\nvalidated_soms: []\n",
        )
        write(
            d / "v.yaml",
            "schema_version: 1\nkind: model\nid: v\ntask: person-detection\ndescription: vv\n"
            "license: Apache-2.0\nsource:\n  bundled: starters/t.tflite\n"
            "validated_soms: [E1M-V2N101]\ncompile:\n  deepx_dxm1:\n    config: c.json\n    calibration: cal\n",
        )
    return root


def invoke(*args):
    result = runner.invoke(app, ["--format", "json", *args], catch_exceptions=False)
    return result.exit_code, json.loads(result.stdout)


def setup(tmp_path: Path, **kw):
    sdk = make_sdk(tmp_path / "sdk", **kw)
    proj = tmp_path / "proj"
    write(proj / "board.yaml", BOARD)
    return sdk, proj


def test_zoo_lists_and_filters_by_sku(tmp_path):
    sdk, proj = setup(tmp_path)
    code, doc = invoke("zoo", "--sdk-root", str(sdk), "--project", str(proj))
    assert code == 0 and doc["ok"]
    assert [e["id"] for e in doc["data"]["entries"]] == ["t", "v"]
    code, doc = invoke("zoo", "--sku", "E1M-V2N101", "--sdk-root", str(sdk), "--project", str(proj))
    assert [e["id"] for e in doc["data"]["entries"]] == ["v"]
    assert doc["data"]["sku"] == "E1M-V2N101"
    code, doc = invoke("zoo", "--sku", "E1M-AEN801", "--sdk-root", str(sdk), "--project", str(proj))
    assert code == 0 and doc["data"]["entries"] == []


def test_zoo_without_zoo_metadata_names_the_minimum_commit(tmp_path):
    sdk, proj = setup(tmp_path, zoo=False)
    code, doc = invoke("zoo", "--sdk-root", str(sdk), "--project", str(proj))
    assert code == 2
    issue = doc["issues"][0]
    assert issue["code"] == "model.zoo-unavailable" and "7018515f9" in issue["message"]
    assert doc["data"]["entries"] == []


def test_zoo_without_sdk_refuses(tmp_path):
    proj = tmp_path / "proj"
    write(proj / "board.yaml", BOARD)
    code, doc = invoke("zoo", "--sdk-root", str(tmp_path / "none"), "--project", str(proj))
    assert code == 2 and doc["issues"][0]["code"] == "model.sdk-root-unresolved"


def test_add_copies_model_and_appends_preserving_comments(tmp_path):
    sdk, proj = setup(tmp_path)
    code, doc = invoke("add", "v", "--sdk-root", str(sdk), "--project", str(proj))
    assert code == 0, doc
    assert (proj / "models" / "v.tflite").read_bytes() == b"TFL3starter"
    assert doc["data"]["added"] == "v"
    added = doc["data"]["addedEntry"]
    assert added["source"] == "models/v.tflite" and added["bytes"] == 11
    after = (proj / "board.yaml").read_text(encoding="utf-8")
    cut = BOARD.index("\nrest: 1")
    insert = (
        "  - name: v\n    source: models/v.tflite\n    compile:\n      deepx_dxm1:\n"
        "        config: c.json\n        calibration: cal\n"
    )
    assert after == BOARD[: cut - 0].rstrip("\n") + "\n" + insert + "\n" + BOARD[cut + 1 :]
    # SKU E1M-AEN801 is not in v's validated_soms -> warning, still exit 0
    assert [i["code"] for i in doc["issues"]] == ["model.add-sku-not-validated"]


def test_add_twice_is_refused_and_changes_nothing(tmp_path):
    sdk, proj = setup(tmp_path)
    assert invoke("add", "t", "--sdk-root", str(sdk), "--project", str(proj))[0] == 0
    snapshot = (proj / "board.yaml").read_bytes()
    code, doc = invoke("add", "t", "--sdk-root", str(sdk), "--project", str(proj))
    assert code == 2 and doc["issues"][-1]["code"] == "model.add-name-exists"
    assert (proj / "board.yaml").read_bytes() == snapshot


def test_add_refuses_existing_destination_unknown_id_and_missing_id(tmp_path):
    sdk, proj = setup(tmp_path)
    write(proj / "models" / "t.tflite", b"mine")
    code, doc = invoke("add", "t", "--sdk-root", str(sdk), "--project", str(proj))
    assert code == 2 and doc["issues"][-1]["code"] == "model.add-destination-exists"
    assert (proj / "models" / "t.tflite").read_bytes() == b"mine"
    code, doc = invoke("add", "nope", "--sdk-root", str(sdk), "--project", str(proj))
    assert doc["issues"][-1]["code"] == "model.zoo-entry-not-found" and "t, v" in doc["issues"][-1]["message"]
    code, doc = invoke("add", "--sdk-root", str(sdk), "--project", str(proj))
    assert code == 2 and doc["issues"][-1]["code"] == "model.add-id-missing"
    assert (proj / "board.yaml").read_text(encoding="utf-8") == BOARD


def test_add_refused_edit_leaves_no_model_file(tmp_path):
    sdk, proj = setup(tmp_path)
    write(proj / "board.yaml", "som:\n  sku: E1M-AEN801\nmodels: [{name: a, source: b}]\n")
    code, doc = invoke("add", "t", "--sdk-root", str(sdk), "--project", str(proj))
    assert code == 2 and doc["issues"][-1]["code"] == "model.board-yaml-edit-refused"
    assert not (proj / "models").exists()


def test_add_with_a_missing_starter_writes_nothing(tmp_path):
    sdk, proj = setup(tmp_path)
    write(
        sdk / "metadata" / "model_zoo" / "u.yaml",
        "schema_version: 1\nkind: model\nid: u\ntask: smoke\ndescription: x\nlicense: MIT\n"
        "source:\n  bundled: starters/missing.tflite\nvalidated_soms: []\n",
    )
    code, doc = invoke("add", "u", "--sdk-root", str(sdk), "--project", str(proj))
    assert code == 1 and doc["issues"][-1]["code"] == "model.zoo-fetch-failed"
    assert not (proj / "models").exists()
    assert (proj / "board.yaml").read_text(encoding="utf-8") == BOARD


def test_zoo_rows_carry_runs_here_from_the_board_sku(tmp_path):
    sdk, proj = setup(tmp_path)
    write(proj / "board.yaml", "som:\n  sku: E1M-V2N101\n")
    code, doc = invoke("zoo", "--sdk-root", str(sdk), "--project", str(proj))
    assert doc["data"]["boardSku"] == "E1M-V2N101"
    assert {e["id"]: e["runsHere"] for e in doc["data"]["entries"]} == {"t": False, "v": True}
    (proj / "board.yaml").unlink()
    code, doc = invoke("zoo", "--sdk-root", str(sdk), "--project", str(proj))
    assert code == 0 and {e["runsHere"] for e in doc["data"]["entries"]} == {None}
    write(proj / "board.yaml", "not: [valid")
    code, doc = invoke("zoo", "--sdk-root", str(sdk), "--project", str(proj))
    assert code == 0 and doc["data"]["boardSku"] is None


def test_stray_arguments_are_refused(tmp_path):
    sdk, proj = setup(tmp_path)
    code, doc = invoke("zoo", "t", "--sdk-root", str(sdk), "--project", str(proj))
    assert code == 2 and doc["issues"][-1]["code"] == "model.unexpected-argument"
    code, doc = invoke("add", "t", "--sku", "E1M-V2N101", "--sdk-root", str(sdk), "--project", str(proj))
    assert code == 2 and doc["issues"][-1]["code"] == "model.unexpected-argument"
    assert not (proj / "models").exists()


def test_dangling_symlink_destination_is_refused_not_written_through(tmp_path):
    sdk, proj = setup(tmp_path)
    outside = tmp_path / "outside.tflite"
    (proj / "models").mkdir()
    try:
        (proj / "models" / "t.tflite").symlink_to(outside)
    except OSError:
        return
    code, doc = invoke("add", "t", "--sdk-root", str(sdk), "--project", str(proj))
    assert code == 2 and doc["issues"][-1]["code"] == "model.add-destination-exists"
    assert not outside.exists()
    assert (proj / "board.yaml").read_text(encoding="utf-8") == BOARD


def test_symlinked_models_dir_is_refused(tmp_path):
    sdk, proj = setup(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    try:
        (proj / "models").symlink_to(elsewhere, target_is_directory=True)
    except OSError:
        return
    code, doc = invoke("add", "t", "--sdk-root", str(sdk), "--project", str(proj))
    assert code == 2 and doc["issues"][-1]["code"] == "model.add-destination-unsafe"
    assert list(elsewhere.iterdir()) == []


def test_refused_edit_happens_before_any_fetch(tmp_path, monkeypatch):
    sdk, proj = setup(tmp_path)
    write(proj / "board.yaml", "som:\n  sku: E1M-AEN801\nmodels: [{name: a, source: b}]\n")

    def boom(*a, **k):
        raise AssertionError("fetched before the edit was checked")

    monkeypatch.setattr("tan.commands.model_zoo_cmd.fetch_source", boom)
    code, doc = invoke("add", "t", "--sdk-root", str(sdk), "--project", str(proj))
    assert doc["issues"][-1]["code"] == "model.board-yaml-edit-refused"


def test_integrity_failure_has_its_own_code_and_leaves_nothing(tmp_path):
    from tan.commands.model_zoo_cmd import run_add
    from tan.commands.build_output import resolve_project_context

    sdk, proj = setup(tmp_path)
    write(
        sdk / "metadata" / "model_zoo" / "w.yaml",
        "schema_version: 1\nkind: model\nid: w\ntask: person-detection\ndescription: x\n"
        "license: MIT\nsource:\n  url: https://example.com/w.tflite\n  sha256: " + "a" * 64 + "\n"
        "validated_soms: []\n",
    )
    ctx = resolve_project_context(str(proj), None, str(sdk))
    _, _, data, issues, exit_code = run_add(
        context=ctx, zoo_dir=sdk / "metadata" / "model_zoo", model_id="w", sku=None,
        existing_names=set(), reader=lambda url: iter([b"tampered"]),
    )
    assert issues[-1].code == "model.zoo-integrity-failed" and int(exit_code) == 1
    assert not (proj / "models").exists()  # the models/ dir this run made is removed
    assert (proj / "board.yaml").read_text(encoding="utf-8") == BOARD
    assert data["added"] is None


def test_write_failure_has_its_own_code_and_removes_the_dir(tmp_path, monkeypatch):
    sdk, proj = setup(tmp_path)

    def boom(tmp, dest):
        raise OSError("disk full")

    monkeypatch.setattr("tan.commands.model_zoo_cmd._publish", boom)
    code, doc = invoke("add", "t", "--sdk-root", str(sdk), "--project", str(proj))
    assert code == 1 and doc["issues"][-1]["code"] == "model.add-write-failed"
    assert not (proj / "models").exists()


def test_board_write_failure_rolls_back_the_model_file(tmp_path, monkeypatch):
    sdk, proj = setup(tmp_path)

    def boom(path, data):
        raise OSError("read-only")

    monkeypatch.setattr("tan.commands.model_zoo_cmd.atomic_write_bytes", boom)
    code, doc = invoke("add", "t", "--sdk-root", str(sdk), "--project", str(proj))
    assert code == 1 and doc["issues"][-1]["code"] == "model.board-yaml-edit-failed"
    assert not (proj / "models").exists()
    assert (proj / "board.yaml").read_text(encoding="utf-8") == BOARD


def test_successful_add_leaves_no_temp_files(tmp_path):
    sdk, proj = setup(tmp_path)
    assert invoke("add", "t", "--sdk-root", str(sdk), "--project", str(proj))[0] == 0
    assert [p.name for p in (proj / "models").iterdir()] == ["t.tflite"]


def test_published_model_has_the_umask_mode_not_0600(tmp_path):
    import os
    import stat

    sdk, proj = setup(tmp_path)
    assert invoke("add", "t", "--sdk-root", str(sdk), "--project", str(proj))[0] == 0
    mask = os.umask(0)
    os.umask(mask)
    mode = stat.S_IMODE((proj / "models" / "t.tflite").stat().st_mode)
    assert mode == 0o666 & ~mask


def test_link_not_implemented_falls_back_to_exclusive_create(tmp_path, monkeypatch):
    sdk, proj = setup(tmp_path)

    def nope(*a, **k):
        raise NotImplementedError

    monkeypatch.setattr("tan.commands.model_zoo_cmd.os.link", nope)
    code, doc = invoke("add", "t", "--sdk-root", str(sdk), "--project", str(proj))
    assert code == 0, doc
    assert [p.name for p in (proj / "models").iterdir()] == ["t.tflite"]


def test_rerun_adopts_an_orphaned_identical_model_file(tmp_path):
    sdk, proj = setup(tmp_path)
    (proj / "models").mkdir()
    (proj / "models" / "t.tflite").write_bytes(b"TFL3starter")  # interrupted run's leftover
    code, doc = invoke("add", "t", "--sdk-root", str(sdk), "--project", str(proj))
    assert code == 0, doc
    assert "name: t" in (proj / "board.yaml").read_text(encoding="utf-8")


def test_a_different_existing_file_is_still_refused(tmp_path):
    sdk, proj = setup(tmp_path)
    (proj / "models").mkdir()
    (proj / "models" / "t.tflite").write_bytes(b"someone else's")
    code, doc = invoke("add", "t", "--sdk-root", str(sdk), "--project", str(proj))
    assert code == 2 and doc["issues"][-1]["code"] == "model.add-destination-exists"


def test_board_write_failure_on_the_adoption_path_names_the_board(tmp_path, monkeypatch):
    sdk, proj = setup(tmp_path)
    (proj / "models").mkdir()
    (proj / "models" / "t.tflite").write_bytes(b"TFL3starter")

    def boom(path, data):
        raise OSError("read-only")

    monkeypatch.setattr("tan.commands.model_zoo_cmd.atomic_write_bytes", boom)
    code, doc = invoke("add", "t", "--sdk-root", str(sdk), "--project", str(proj))
    issue = doc["issues"][-1]
    assert code == 1 and issue["code"] == "model.board-yaml-edit-failed" and "board.yaml" in issue["message"]
    assert (proj / "models" / "t.tflite").read_bytes() == b"TFL3starter"  # the user's file is kept
