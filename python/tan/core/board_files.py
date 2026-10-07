# SPDX-License-Identifier: Apache-2.0
"""Per-board `boards/<board>.{conf,overlay}` files (tan-cli#1351).

Zephyr applies `boards/<board>_<soc>_<cluster>.conf` / `.overlay` only when
`<board>` is the board `west build -b` names. An example scaffolded onto a
different SKU (`E1M-AEN801` -> `E1M-AEN803`) keeps files named for the OLD
board, which Zephyr silently ignores. Two halves, both pure:

* `rename_board_files` -- `tan init --from-example --som`: rename each
  example per-board file onto the target SKU's board target, matched by core
  id, from `metadata/e1m_modules/<sku>.yaml` `topology.<core>.board`.
* `unmatched_board_files` -- `tan build`: name every `boards/alp_e1m_*`
  file that matches no slice's board.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path

from tan.core.scaffold import PlannedFile, SomBlockUnsupportedError, vendored_som

#: Only alp board files are judged: an example legitimately ships
#: `native_sim_native_64.conf` etc. for boards that are never a slice here.
ALP_BOARD_PREFIX = "alp_e1m_"
_BOARD_FILE = re.compile(r"^(?P<stem>.+)\.(?:conf|overlay)$")


def flat_board(board: str) -> str:
    """`alp_e1m_aen803_m55_he/ae822fa0e5597ls0/rtss_he` -> the file-name form."""
    return board.replace("/", "_")


def topology_boards(metadata_root: Path, sku: str) -> dict[str, str]:
    """`{core_id: qualified board}` from the SoM preset; `{}` when the preset
    is missing or malformed (the caller then leaves every file alone)."""
    import yaml  # noqa: PLC0415 -- keeps `import tan.cli` lean (tan-cli#810)

    try:
        doc = yaml.safe_load(
            (metadata_root / "e1m_modules" / f"{sku}.yaml").read_text(encoding="utf-8")
        )
    except (OSError, yaml.YAMLError, UnicodeDecodeError):
        return {}
    topology = doc.get("topology") if isinstance(doc, dict) else None
    if not isinstance(topology, dict):
        return {}
    return {
        str(core): spec["board"]
        for core, spec in topology.items()
        if isinstance(spec, dict) and isinstance(spec.get("board"), str)
    }


def board_file_renames(
    source_boards: Mapping[str, str], target_boards: Mapping[str, str]
) -> dict[str, str]:
    """`{old flat board: new flat board}` for every core both SKUs declare."""
    return {
        flat_board(old): flat_board(target_boards[core])
        for core, old in source_boards.items()
        if core in target_boards and old != target_boards[core]
    }


def rename_board_files(
    paths: Iterable[str], renames: Mapping[str, str]
) -> dict[str, str]:
    """`{old relative path: new relative path}` for `boards/<flat>.<ext>` files
    whose flat board name is a rename key."""
    out: dict[str, str] = {}
    for rel in paths:
        parent, _, name = rel.rpartition("/")
        if parent != "boards":
            continue
        m = _BOARD_FILE.match(name)
        if m and m["stem"] in renames:
            out[rel] = f"boards/{renames[m['stem']]}{name[len(m['stem']):]}"
    return out


def command_board(args: Sequence[str]) -> str | None:
    """The `-b`/`--board` value of a `west build` argv, if it carries one."""
    for i, token in enumerate(args):
        if token in ("-b", "--board") and i + 1 < len(args):
            return args[i + 1]
    return None


def _stem_matches(stem: str, flat: str) -> bool:
    """Zephyr's own prefix rule: the whole board, or any `_`-delimited
    shortening (`<board>`, `<board>_<soc>`) or revision-style extension."""
    return stem == flat or flat.startswith(stem + "_") or stem.startswith(flat + "_")


def unmatched_board_files(
    project_dirs: Sequence[Path], slice_boards: Iterable[str]
) -> list[tuple[Path, str]]:
    """`(file, reason)` for each `boards/alp_e1m_*.{conf,overlay}` under
    `project_dirs` that matches none of `slice_boards`. Empty when no slice
    names a board (nothing to compare against)."""
    flats = [flat_board(b) for b in slice_boards]
    if not flats:
        return []
    found: list[tuple[Path, str]] = []
    seen: set[Path] = set()
    for directory in project_dirs:
        try:
            entries = sorted((directory / "boards").iterdir())
        except OSError:
            continue
        for entry in entries:
            m = _BOARD_FILE.match(entry.name)
            if not m or not m["stem"].startswith(ALP_BOARD_PREFIX) or entry in seen:
                continue
            seen.add(entry)
            if not any(_stem_matches(m["stem"], f) for f in flats):
                found.append((entry, m["stem"]))
    return found


def retarget_example_board_files(
    files: list[PlannedFile], som: str, metadata_root: Path
) -> list[PlannedFile]:
    """Rename an example's per-board files onto `som`'s board targets. The
    example's own SKU is read from its `board.yaml`; a missing SKU or preset
    leaves `files` untouched."""
    board_yaml = next((f for f in files if f.relative_path == "board.yaml"), None)
    if board_yaml is None:
        return files
    try:
        source_sku, _ = vendored_som(board_yaml.content)
    except SomBlockUnsupportedError:
        # Refused, with its own code, by the board.yaml retarget that follows.
        return files
    if not source_sku or source_sku == som:
        return files
    renames = board_file_renames(
        topology_boards(metadata_root, source_sku), topology_boards(metadata_root, som)
    )
    moved = rename_board_files((f.relative_path for f in files), renames)
    if not moved:
        return files
    return sorted(
        (PlannedFile(moved.get(f.relative_path, f.relative_path), f.content) for f in files),
        key=lambda f: f.relative_path,
    )


def unmatched_board_file_messages(
    slices: Iterable[tuple[str | None, Sequence[str], str | None]], build_root: Path
) -> list[str]:
    """One advisory per unmatched per-board file across `slices`, each given
    as `(backend, command argv, app_dir)`. Both `app_dir` and its parent are
    searched: `boards/` sits beside `CMakeLists.txt`, which for the `app:
    ./src` convention is the parent."""
    boards: list[str] = []
    dirs: list[Path] = []
    for backend, args, app_dir in slices:
        board = command_board(args) if backend == "zephyr" else None
        if board is None or app_dir is None:
            continue
        boards.append(board)
        app = Path(app_dir)
        app = app if app.is_absolute() else build_root / app
        dirs.extend(d for d in (app, app.parent) if d not in dirs)
    names = ", ".join(sorted(set(boards)))
    return [
        f"{path} matches no slice's board ({names}) -- Zephyr will never apply it. "
        f"Rename it to `{flat_board(boards[0])}{path.suffix}` (or the board of the "
        f"slice it belongs to)."
        for path, _ in unmatched_board_files(dirs, boards)
    ]
