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
    added = doc["data"]["added"]
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
