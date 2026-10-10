# SPDX-License-Identifier: Apache-2.0
"""tan-cli#1351: `--from-example --som` renames per-board files onto the chosen
SKU's board target; `tan build` warns about a board file no slice matches."""

from __future__ import annotations

from pathlib import Path

from tan.core.board_files import unmatched_board_file_messages
from tests.commands.test_init_command import envelope, run_tan

AEN801_HE = "alp_e1m_aen801_m55_he/ae822fa0e5597ls0/rtss_he"
AEN803_HE = "alp_e1m_aen803_m55_he/ae822fa0e5597ls0/rtss_he"
FLAT801 = "alp_e1m_aen801_m55_he_ae822fa0e5597ls0_rtss_he"
FLAT803 = "alp_e1m_aen803_m55_he_ae822fa0e5597ls0_rtss_he"


def _sdk(tmp_path: Path) -> Path:
    sdk = tmp_path / "sdk"
    (sdk / "scripts").mkdir(parents=True)
    (sdk / "scripts" / "alp_project.py").write_text("", encoding="utf-8")
    som = sdk / "metadata" / "e1m_modules"
    som.mkdir(parents=True)
    for sku, board in (("E1M-AEN801", AEN801_HE), ("E1M-AEN803", AEN803_HE)):
        (som / f"{sku}.yaml").write_text(
            f"topology:\n  m55_he:\n    board: {board}\n", encoding="utf-8"
        )
    example = sdk / "examples" / "peripheral-io" / "alp-console"
    (example / "src").mkdir(parents=True)
    (example / "boards").mkdir()
    (example / "board.yaml").write_text(
        "som:\n  sku: E1M-AEN801\ncores:\n  m55_he:\n    os: zephyr\n", encoding="utf-8"
    )
    (example / "src" / "main.c").write_text("int main(void) { return 0; }\n", encoding="utf-8")
    for name in (FLAT801, "native_sim_native_64"):
        (example / "boards" / f"{name}.conf").write_text("CONFIG_X=y\n", encoding="utf-8")
        (example / "boards" / f"{name}.overlay").write_text("/ {};\n", encoding="utf-8")
    return sdk


def _init(tmp_path: Path, sku: str):
    proc = run_tan(
        "init", "--from-example", "peripheral-io/alp-console", "--sdk-root", "./sdk",
        "--name", "out", "--som", sku, "--format", "json", cwd=tmp_path,
    )  # fmt: skip
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return sorted(p.name for p in (tmp_path / "out" / "boards").iterdir())


def test_aen803_gets_aen803_board_files(tmp_path):
    _sdk(tmp_path)
    names = _init(tmp_path, "E1M-AEN803")
    assert names == sorted(
        [f"{FLAT803}.conf", f"{FLAT803}.overlay",
         "native_sim_native_64.conf", "native_sim_native_64.overlay"]
    )  # fmt: skip


def test_aen801_keeps_its_own_board_files(tmp_path):
    _sdk(tmp_path)
    names = _init(tmp_path, "E1M-AEN801")
    assert f"{FLAT801}.conf" in names and f"{FLAT801}.overlay" in names
    assert not any("aen803" in n for n in names)


def test_build_warns_on_a_board_file_no_slice_matches(tmp_path):
    boards = tmp_path / "boards"
    boards.mkdir()
    for name in (FLAT801, FLAT803, "native_sim_native_64", "alp_e1m_aen803_m55_he"):
        (boards / f"{name}.conf").write_text("", encoding="utf-8")
    slices = [("zephyr", ["west", "build", "-b", AEN803_HE, "."], ".")]

    msgs = unmatched_board_file_messages(slices, tmp_path)

    assert len(msgs) == 1, msgs
    assert f"{FLAT801}.conf" in msgs[0] and AEN803_HE in msgs[0]


def test_build_is_quiet_when_every_board_file_matches(tmp_path):
    (tmp_path / "boards").mkdir()
    (tmp_path / "boards" / f"{FLAT803}.overlay").write_text("", encoding="utf-8")
    slices = [("zephyr", ["west", "build", "-b", AEN803_HE, "."], str(tmp_path))]
    assert unmatched_board_file_messages(slices, tmp_path) == []


V2N_CM33 = "alp_e1m_v2n101_r9a09g056_cm33"
V2N_BOARD = "alp_e1m_v2n101/r9a09g056/cm33"


def test_build_skips_files_for_another_real_board(tmp_path):
    from tan.core.board_files import all_topology_boards

    sdk = _sdk(tmp_path)
    som = sdk / "metadata" / "e1m_modules"
    (som / "E1M-V2N101.yaml").write_text(
        f"topology:\n  cm33:\n    board: {V2N_BOARD}\n", encoding="utf-8"
    )
    known = all_topology_boards(sdk / "metadata")
    assert {AEN801_HE, AEN803_HE, V2N_BOARD} <= set(known)

    boards = tmp_path / "boards"
    boards.mkdir()
    for name in (V2N_CM33, FLAT801, "alp_e1m_aen803_m55_hx_ae822fa0e5597ls0_rtss_he"):
        (boards / f"{name}.conf").write_text("", encoding="utf-8")
    slices = [("zephyr", ["west", "build", "-b", AEN803_HE, "."], ".")]

    msgs = unmatched_board_file_messages(slices, tmp_path, known)

    # The V2N file is another SoC's; the AEN801-only file (sibling SKU, same
    # silicon, no own-board twin) and the typo both warn.
    assert len(msgs) == 2, msgs
    assert any(FLAT801 in m for m in msgs) and any("m55_hx" in m for m in msgs)


def _known(tmp_path):
    from tan.core.board_files import all_topology_boards

    sdk = _sdk(tmp_path)
    (sdk / "metadata" / "e1m_modules" / "E1M-V2N101.yaml").write_text(
        f"topology:\n  cm33:\n    board: {V2N_BOARD}\n", encoding="utf-8"
    )
    return all_topology_boards(sdk / "metadata")


def _boards(tmp_path, *names):
    d = tmp_path / "boards"
    d.mkdir()
    for n in names:
        (d / f"{n}.conf").write_text("", encoding="utf-8")


_AEN803_SLICE = [("zephyr", ["west", "build", "-b", AEN803_HE, "."], ".")]


def test_sibling_sku_file_alone_still_warns_with_known_boards(tmp_path):
    known = _known(tmp_path)
    _boards(tmp_path, FLAT801)
    msgs = unmatched_board_file_messages(_AEN803_SLICE, tmp_path, known)
    assert len(msgs) == 1 and f"{FLAT801}.conf" in msgs[0], msgs


def test_twin_file_beside_own_board_file_is_silent(tmp_path):
    known = _known(tmp_path)
    _boards(tmp_path, FLAT801, FLAT803)
    assert unmatched_board_file_messages(_AEN803_SLICE, tmp_path, known) == []


def test_other_soc_shim_file_on_aen_build_is_silent(tmp_path):
    known = _known(tmp_path)
    _boards(tmp_path, V2N_CM33)
    assert unmatched_board_file_messages(_AEN803_SLICE, tmp_path, known) == []
