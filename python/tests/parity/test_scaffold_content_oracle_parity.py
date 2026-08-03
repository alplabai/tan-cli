# SPDX-License-Identifier: Apache-2.0
"""`tan init`'s scaffold CONTENT vs. the frozen Rust oracle (tan-cli#371).

Three checks already sit near this gap, and every one of them checks
something else:

* `contract/envelopes/init-preview-minimal-app/expected.json` pins only
  `{"relativePath": ..., "kind": "new"}` -- the file LIST and ORDER, never a
  byte of content. `crates/tan-cli/tests/contract.rs:246`
  (`contract_case!(init_preview_minimal_app, ...)`) reads that same golden,
  so the Rust side checks the same nothing.
* `tests/parity/scaffold_byte_parity.py` (repo root -- NOT this directory;
  see `oracle.py`'s own docstring on why the two `tests/parity/` trees are
  unrelated) diffs `python/tan/templates/vendored/` against a LIVE alp-sdk
  `--emit scaffold`. That is the SDK-vs-port axis, never the
  port-vs-Rust-oracle axis this file covers -- and it has no vendored tree to
  read for `minimal-app` at all (`_minimal_app_files` is tan's OWN generator,
  not an SDK catalog entry -- see `tan.core.scaffold`'s module docstring).
* `test_oracle_parity.py`'s `CASES` has no `init` entry at all.

So the two implementations could -- and, per tan-cli#309, DID -- diverge in
what `tan init` actually writes, with nothing noticing. This file is the
missing third leg: for every file every template plans (at the one SKU every
other CLI-level case in this suite already uses, `DEFAULT_SOM_SKU`), run a
REAL `tan init` through both binaries and diff the bytes each one put on
disk.

**Granularity is per (template, relative_path), not per template.** A
template with one deliberately-divergent file (`minimal-app`'s CMake shape)
still has seven other files that must keep matching -- collapsing the whole
template into one `xfail` would silence a regression in any of those seven.
`DELIBERATE_DIVERGENCE` below is keyed the same shape as
`tests/conformance/test_contract_envelopes.py`'s dict of the same name, and
for the identical reason: `xfail(strict=True)`, so an accidental
CONVERGENCE (the divergence quietly disappearing -- `crates/` unfrozen, or
the vendor pin note going stale) FAILS the run instead of reporting a silent
XPASS. Every entry's reason string names which side is authoritative, so a
red here reads as either "the port regressed" (an undeclared file started
differing) or "the divergence healed, update the declaration" (a declared
XFAIL turned XPASS) -- never "guess which one is right".

Scoped to `DEFAULT_SOM_SKU` (`E1M-AEN801`) only: the SKU `iot-starter`
supports exclusively, and the one every other CLI-level case in this suite
(`work_dir`, `test_generate_matches_rust_with_a_resolvable_sdk`) already
uses. A per-SKU sweep (`E1M-V2N101` for every family-split template) is a
separate, larger axis this case does not attempt -- `wizard/vendored/
MANIFEST.md`'s own "Per-SKU substitution" section already documents that
axis's shape for the vendored templates.

Live-spawn, unconditionally, whenever a Rust oracle is built -- the same
shape `test_oracle_parity.py`'s v0.6.0 section uses for `_ORACLE_REQUIRED`
(skips on binary ABSENCE only, not gated by `TAN_PARITY_LIVE`): a file diff
has no frozen-fixture replay to fall back to the way an envelope comparison
does (`oracle_fixtures.resolve`), so there is no cheaper mode to default to.
Skips in `ci.yml`'s `python` job (never runs `cargo build`); genuinely runs
in `parity.yml`'s `python-tests` job (`cargo build --locked --bin tan` is
that job's own first step) and in any local `python -m pytest tests/parity`
run against an already-built `target/{release,debug}/tan`.
"""
from pathlib import Path

import pytest

from tan.core.scaffold import DEFAULT_SOM_SKU, TEMPLATE_IDS, plan_template_files

from .oracle import _run, python_command, rust_binary

RUST = rust_binary()
_SKIP_NO_ORACLE = pytest.mark.skipif(
    RUST is None,
    reason="needs a built Rust tan; run `cargo build --bin tan` (or set TAN_RUST_BINARY)",
)

#: `(template_id, relative_path) -> reason`, the SAME shape and the SAME
#: `xfail(strict=True)` discipline as `test_contract_envelopes.py`'s dict of
#: the same name. Every reason states which side is authoritative.
DELIBERATE_DIVERGENCE: dict[tuple[str, str], str] = {
    # tan-cli#309: `minimal-app`'s pre-fix shape sent `west build` at a plain
    # `add_executable` with no `find_package(Zephyr ...)` -- a silent host
    # binary for a core declared `os: zephyr` (see `tan.core.scaffold`'s
    # `_minimal_app_files` module comment for the full mechanism). Fixed on
    # the Python side only: `crates/` is frozen (`docs/ROADMAP.md`'s Standing
    # Rules) and still emits the pre-#309 shape verbatim
    # (`crates/tan-core/src/wizard/service/c_project.rs`). PYTHON IS
    # AUTHORITATIVE here; the oracle is the stale, pre-fix answer.
    ("minimal-app", "board.yaml"): (
        "tan-cli#309: `app: .` (Python, fixed) vs `app: ./src` (the frozen "
        "pre-#309 oracle). Python is authoritative; the oracle is stale."
    ),
    ("minimal-app", "CMakeLists.txt"): (
        "tan-cli#309: Python's root CMakeLists.txt carries `find_package("
        "Zephyr REQUIRED HINTS $ENV{ZEPHYR_BASE})`; the frozen oracle's does "
        "not. Python is authoritative; the oracle is stale."
    ),
    ("minimal-app", "src/CMakeLists.txt"): (
        "tan-cli#309: Python contributes to Zephyr's own `app` target via "
        "`target_sources(app ...)`; the frozen oracle still calls a plain "
        "`add_executable(alp_app ...)` Zephyr's build never links in. Python "
        "is authoritative; the oracle is stale."
    ),
    # crates/ is frozen at alp-sdk v0.14.0 (`crates/tan-core/src/wizard/
    # vendored/MANIFEST.md`); the Python port tracks `parity.yml`'s
    # PINNED_SDK_TAG, currently v0.15.0-rc1 (`python/tan/templates/vendored/
    # MANIFEST.md`). The ONLY difference either re-vendor introduced is the
    # doc-version link in each README's "Further reading" section
    # (`blob/v0.14.0/` vs `blob/v0.15.0/`) -- measured directly against a
    # live run of both binaries, not inferred from either MANIFEST: exactly
    # those four lines move, nothing else. PYTHON IS AUTHORITATIVE (it tracks
    # the CURRENT pin); the oracle is a permanently frozen snapshot, per
    # `docs/ROADMAP.md`'s Standing Rules -- expect this to keep recurring
    # every time the vendored tree is re-pinned, not a one-time fix.
    ("zephyr-app", "README.md"): (
        "crates/ frozen at alp-sdk v0.14.0 vs the port's v0.15.0-rc1 pin -- "
        "doc-version link only. Python is authoritative (current pin); the "
        "oracle is a frozen historical snapshot (docs/ROADMAP.md)."
    ),
    ("sensor-starter", "README.md"): (
        "crates/ frozen at alp-sdk v0.14.0 vs the port's v0.15.0-rc1 pin -- "
        "doc-version link only. Python is authoritative (current pin); the "
        "oracle is a frozen historical snapshot (docs/ROADMAP.md)."
    ),
    ("board-diagnostics", "README.md"): (
        "crates/ frozen at alp-sdk v0.14.0 vs the port's v0.15.0-rc1 pin -- "
        "doc-version link only. Python is authoritative (current pin); the "
        "oracle is a frozen historical snapshot (docs/ROADMAP.md)."
    ),
    ("iot-starter", "README.md"): (
        "crates/ frozen at alp-sdk v0.14.0 vs the port's v0.15.0-rc1 pin -- "
        "doc-version link only. Python is authoritative (current pin); the "
        "oracle is a frozen historical snapshot (docs/ROADMAP.md)."
    ),
    # `edge-ai-starter` is deliberately ABSENT: measured byte-identical (its
    # README carries no version-pinned link at all -- both MANIFEST.md files
    # say so; confirmed by a live diff of both binaries' output). An entry
    # appearing here later would mean it grew a real divergence, not that
    # this omission was an oversight.
}

_TREE_CACHE: dict[str, tuple[dict[str, bytes], dict[str, bytes]]] = {}


def _read_tree(root: Path) -> dict[str, bytes]:
    """Every regular file under `root`, raw bytes -- exactly what each binary
    put on disk, with no text-mode newline translation to launder a real
    divergence."""
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def _scaffold_trees(template_id: str, tmp_path_factory) -> tuple[dict[str, bytes], dict[str, bytes]]:
    """`tan init --template <template_id>` (`DEFAULT_SOM_SKU`) through both
    binaries, each into its own fresh scratch dir, as {relative_path: bytes}.

    Memoised by hand in `_TREE_CACHE`, not `@pytest.fixture(scope="module")`:
    a module-scoped fixture cannot key its cache on a runtime argument like
    `template_id`, and the several-dozen relative-path cases below need to
    share one process pair PER TEMPLATE rather than pay for a fresh spawn per
    file.
    """
    cached = _TREE_CACHE.get(template_id)
    if cached is not None:
        return cached
    root = tmp_path_factory.mktemp(f"scaffold-oracle-{template_id.replace('-', '_')}")
    argv = ["init", "--template", template_id, "--format", "json"]
    trees: list[dict[str, bytes]] = []
    for side, command in (("rust", [RUST]), ("python", python_command())):
        # Nested `<side>/root` -- not `root / side` bare -- so `discover_
        # workspace_sdk`'s parent-of-cwd probe (`oracle.py`'s `work_dir`
        # fixture carries the identical shape, same reason) can never see a
        # sibling directory this harness itself created.
        work = root / side / "root"
        home = root / side / "home"
        work.mkdir(parents=True)
        home.mkdir(parents=True)
        code, out = _run(command, argv, work, home)
        assert code == 0, f"{side} tan init --template {template_id} failed: {out}"
        trees.append(_read_tree(work))
    result = (trees[0], trees[1])
    _TREE_CACHE[template_id] = result
    return result


def _cases():
    """Every (template_id, relative_path) pair the PYTHON planner emits for
    `DEFAULT_SOM_SKU`, computed at collection time -- pure, no subprocess
    (`plan_template_files` only reads tan's own vendored/hand-generated
    data). Measured to be the IDENTICAL file set the oracle emits for all six
    templates at this SKU (`diff -rq` against a live run of both binaries);
    `test_scaffold_file_list_matches_the_oracle` below is what keeps
    re-measuring that on every run, so a FUTURE file-list divergence is
    still caught even though this list itself is Python-derived.
    """
    for template_id in TEMPLATE_IDS:
        relpaths = sorted(f.relative_path for f in plan_template_files(template_id, DEFAULT_SOM_SKU))
        for relative_path in relpaths:
            reason = DELIBERATE_DIVERGENCE.get((template_id, relative_path))
            marks = [pytest.mark.xfail(reason=reason, strict=True)] if reason else []
            yield pytest.param(template_id, relative_path, id=f"{template_id}::{relative_path}", marks=marks)


@_SKIP_NO_ORACLE
@pytest.mark.parametrize("template_id,relative_path", list(_cases()))
def test_scaffold_file_content_matches_the_oracle(template_id, relative_path, tmp_path_factory):
    rust_files, python_files = _scaffold_trees(template_id, tmp_path_factory)
    assert relative_path in rust_files, (
        f"the oracle never wrote {relative_path!r} for --template {template_id}"
    )
    assert relative_path in python_files, (
        f"the port never wrote {relative_path!r} for --template {template_id}"
    )
    assert rust_files[relative_path] == python_files[relative_path]


@_SKIP_NO_ORACLE
@pytest.mark.parametrize("template_id", TEMPLATE_IDS)
def test_scaffold_file_list_matches_the_oracle(template_id, tmp_path_factory):
    """The SET of files, independent of content. `test_scaffold_file_content_
    matches_the_oracle` above is parametrized off the PYTHON side's own
    planned list, so a file the port stopped writing (or the oracle stopped
    writing, or either side started writing an EXTRA one) would never
    surface as a per-relpath case -- this is what catches that. No
    `DELIBERATE_DIVERGENCE` entry exists for the file SET on any template
    today (measured); if one is ever needed, add it the same way as a
    content entry, keyed on the missing/extra path instead of a real one.
    """
    rust_files, python_files = _scaffold_trees(template_id, tmp_path_factory)
    assert sorted(rust_files) == sorted(python_files)
