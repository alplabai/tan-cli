#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Scaffold byte-parity gate (alp-sdk#864): vendored wizard templates vs. a
live `alp-sdk --emit scaffold`.

`tan init`/`tan scaffold` are SDK-free — for a template mapped onto an SDK
scaffold-catalog id (see `python/tan/templates/vendored/MANIFEST.md`), they
read a vendored copy of `alp_project.py --emit scaffold --template <id> --sku
<sku>`'s output baked into the shipped artifact, instead of shelling the SDK
or re-deriving its build-integration conventions locally. That vendored copy
can silently drift from the SDK if a future SDK scaffold change is never
re-vendored -- exactly the RFC #843-style drift ADR-0020 exists to kill for
the build-plan seam. This script is the tan-cli side of the
`repository_dispatch` gate ADR-0020 Amendment 1 mandates (see
`tests/parity/README.md` for the seam-1 build-plan analogue this mirrors):
for every vendored (template, sku) pair, re-run the live SDK emit and assert
byte-identity against the vendored tree.

Defaults to `python/tan/templates/vendored/` -- the tree the shipped Python
`tan` actually reads. `crates/tan-core/src/wizard/vendored/` is frozen at its
own permanent vendor point (`docs/ROADMAP.md`'s Standing Rules: the Rust
`tan` is the retired oracle, never re-vendored again) and is guarded instead
by `tan-core`'s own SDK-free `cargo test` byte-parity check
(`zephyr_app_scaffold_is_byte_exact_for_the_vendored_sku`); pass `--vendored
crates/tan-core/src/wizard/vendored` to point this script at it by hand.

Optionally self-skipping: the vendored tree's own test suite already proves
it is internally consistent without an SDK checkout, so a local dev-loop run
of this script with no reachable alp-sdk checkout is a clean no-op, not a
failure. Reachability is checked in this order: `--sdk`, then
`$ALP_SDK_ROOT`, then an `alp-sdk` checkout next to this tan-cli checkout --
but an explicit `--sdk` that does not resolve is a hard FAIL, not a
fall-through to the other two (tan-cli#172 review, tan-cli#175; see
`_sdk_checkout.sdk_root_or_exit_code`).

**Run it against the ref the tree is vendored from.** This gate diffs against
a LIVE emit, so an `$ALP_SDK_ROOT` pointing at some other checkout measures
that checkout, not this tree: a local alp-sdk on a feature branch predating
alp-sdk#1016 reports six spurious `board.yaml` diffs (the "Customer workflow"
comment header), which look exactly like real drift and are not. CI clones
`.github/workflows/parity.yml`'s `PINNED_SDK_TAG` for precisely this reason.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable, Iterable
from pathlib import Path

from _sdk_checkout import sdk_root_or_exit_code

VENDORED_ROOT = Path(__file__).resolve().parent.parent.parent / (
    "python/tan/templates/vendored"
)

# Vendored files that `--emit scaffold` never emits at all -- NOT part of the
# envelope and NOT in the catalog's `files.user_owned`. `testcase.yaml` is the
# SDK's own twister harness for the catalog's canonical `example:`; it is
# vendored alongside the scaffold envelope but compared against that example
# directory instead of the (absent-from-emit) live output.
#
# `native_sim.conf` (tan-cli#379) is the same class and, for `iot`, the same
# PAIR: `testcase.yaml`'s `extra_args: EXTRA_CONF_FILE=native_sim.conf` is what
# loads it, so vendoring one without the other shipped a scaffold whose own
# twister scenario and whose own documented `west build ... -DEXTRA_CONF_FILE=
# native_sim.conf` named a file that did not exist. Listing it here is not a
# loosening: every vendored `native_sim.conf` now gets byte-diffed against the
# catalog example's copy, exactly as `testcase.yaml` already was. Templates with
# no such file are unaffected -- `augment_with_example_extras` only reaches for
# a name the vendored tree actually carries.
NON_ENVELOPE_EXTRAS = ("testcase.yaml", "native_sim.conf")


#: `github.com/alplabai/alp-sdk/(blob|tree)/<ref>/` -- what the SDK's
#: doc-link renderer turns a cross-directory link into, pinned at the emitting
#: checkout's own VERSION. Both halves of the ref are spelled out below rather
#: than read from `MANIFEST.md`: this is a declaration of the exact bytes that
#: were hand-changed, so a re-vendor MUST come here and restate it.
_SDK_DOC_LINK_REF = re.compile(r"(github\.com/alplabai/alp-sdk/(?:blob|tree))/v0\.15\.0-rc1/")

#: `list(PREPEND EXTRA_CONF_FILE ${_alp_generated})` plus the comment block
#: immediately above it. The comment is matched by SHAPE, not by its wording --
#: rewording the rationale is not drift from the SDK, and pinning the prose
#: here would red the gate for an edit that changes no behaviour.
_IOT_EXTRA_CONF_PREPEND = re.compile(
    r"(?:^#.*\n)*^list\(PREPEND (EXTRA_CONF_FILE \$\{_alp_generated\})\)$", re.M
)


def un_edit_doc_link_ref(text: str) -> str:
    """tan-cli#384: the emit renders `blob/v0.15.0/` (it drops a pre-release
    suffix), a tag alp-sdk has never cut, so all 40 links 404 in a scaffolded
    project. The vendored tree pins `v0.15.0-rc1`, the ref it is actually
    captured from. Undo that to recover the emit's own bytes."""
    return _SDK_DOC_LINK_REF.sub(r"\1/v0.15.0/", text)


def un_edit_iot_extra_conf_order(text: str) -> str:
    """tan-cli#379: the emit APPENDS the generated `alp.conf` to
    `EXTRA_CONF_FILE`, and Zephyr's last-assignment-wins merge then lets it
    override a caller's own `-DEXTRA_CONF_FILE=native_sim.conf` -- the exact
    overlay this template documents. The vendored tree prepends. Undo that
    (comment block included) to recover the emit's own bytes."""
    return _IOT_EXTRA_CONF_PREPEND.sub(r"list(APPEND \1)", text)


#: The hand-edits `python/tan/templates/vendored/MANIFEST.md` declares under
#: "Deliberate edits on top of the emit" -- the only bytes in that tree that
#: are NOT what `--emit scaffold` produced, each because the emit's own output
#: is wrong for a customer and the fix lives in alp-sdk, not here.
#:
#: Keyed `(template, sku, path)`; the value is `(reason, un_edit)` where
#: `un_edit` maps the VENDORED bytes back onto what the emit is expected to
#: say. `diff_trees` then runs against THAT, so a declaration excuses exactly
#: the edit it describes and nothing else -- an unrelated change in the same
#: file still fails, which a path-level allow-list can never do.
#:
#: Strict in the `xfail(strict=True)` sense this repo declares divergences with
#: (`python/tests/parity/test_scaffold_content_oracle_parity.py`'s
#: `DELIBERATE_DIVERGENCE`, same discipline for the port-vs-oracle axis): an
#: `un_edit` that finds nothing to undo is a FAILURE. A healed divergence --
#: the tree re-vendored, or alp-sdk fixing its emit -- has to force the entry
#: out, otherwise the next real drift in that file inherits a dead excuse.
DELIBERATE_EDITS: dict[tuple[str, str, str], tuple[str, Callable[[str], str]]] = {
    (template, sku, "README.md"): (
        "tan-cli#384: doc links pinned at the vendor ref v0.15.0-rc1, not the "
        "emit's own v0.15.0, which alp-sdk has never tagged",
        un_edit_doc_link_ref,
    )
    for template, sku in (
        ("diagnostics", "E1M-AEN801"),
        ("diagnostics", "E1M-V2N101"),
        ("iot", "E1M-AEN801"),
        ("minimal", "E1M-AEN801"),
        ("minimal", "E1M-V2N101"),
        ("sensor", "E1M-AEN801"),
        ("sensor", "E1M-V2N101"),
    )
} | {
    ("iot", "E1M-AEN801", "CMakeLists.txt"): (
        "tan-cli#379: list(PREPEND EXTRA_CONF_FILE ...) so a caller's own "
        "-DEXTRA_CONF_FILE=native_sim.conf wins over the generated alp.conf",
        un_edit_iot_extra_conf_order,
    ),
}


class ScaffoldEmitError(RuntimeError):
    """Raised for a live SDK emit failure (not a byte diff -- diffs are reported)."""


def discover_vendored_matrix(vendored_root: Path) -> list[tuple[str, str]]:
    """Scan `vendored_root` for `<template>/<sku>/` pairs, sorted."""
    pairs = []
    for template_dir in sorted(p for p in vendored_root.iterdir() if p.is_dir()):
        for sku_dir in sorted(p for p in template_dir.iterdir() if p.is_dir()):
            pairs.append((template_dir.name, sku_dir.name))
    return pairs


def emit_live_scaffold(sdk_root: Path, template: str, sku: str) -> dict[str, str]:
    """Run the live SDK scaffold emit; return {relative_path: contents}."""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(sdk_root / "scripts")
    proc = subprocess.run(
        [sys.executable, "scripts/alp_project.py", "--emit", "scaffold",
         "--template", template, "--sku", sku],
        cwd=sdk_root, env=env, capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise ScaffoldEmitError(
            f"emit failed for template={template!r} sku={sku!r} "
            f"(exit {proc.returncode}): {proc.stderr.strip()}")
    try:
        envelope = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise ScaffoldEmitError(
            f"emit for template={template!r} sku={sku!r} did not produce "
            f"valid JSON: {e}") from e
    return {f["path"]: f["contents"] for f in envelope}


def resolve_example_dir(sdk_root: Path, template: str) -> Path | None:
    """The scaffold catalog's `example:` directory for `template` id --
    where `NON_ENVELOPE_EXTRAS` are compared against instead of the scaffold
    envelope. `None` if the catalog or the template entry can't be read (the
    caller then leaves any such extras undiffed rather than erroring)."""
    catalog_path = sdk_root / "metadata" / "templates" / "catalog-v1.json"
    try:
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    for entry in catalog.get("templates", []):
        if entry.get("id") == template:
            example = entry.get("example")
            return (sdk_root / example) if example else None
    return None


def augment_with_example_extras(
    live: dict[str, str], sdk_root: Path, template: str, vendored_paths: Iterable[str],
) -> None:
    """For any `NON_ENVELOPE_EXTRAS` path present in the vendored tree, read
    its live content from the catalog's example directory and add it to
    `live` in place -- so `diff_trees` compares it same as every other file,
    against its real source instead of flagging it as a spurious
    vendored-only diff."""
    example_dir = resolve_example_dir(sdk_root, template)
    if example_dir is None:
        return
    for name in NON_ENVELOPE_EXTRAS:
        if name in vendored_paths and name not in live:
            extra_path = example_dir / name
            if extra_path.is_file():
                live[name] = extra_path.read_text(encoding="utf-8")


def read_vendored_tree(tree_root: Path) -> dict[str, str]:
    """Read every file under `tree_root` into {relative_path: contents},
    forward-slash normalized, matching the emit envelope's path style."""
    files = {}
    for path in sorted(p for p in tree_root.rglob("*") if p.is_file()):
        rel = path.relative_to(tree_root).as_posix()
        files[rel] = path.read_text(encoding="utf-8")
    return files


def diff_trees(vendored: dict[str, str], live: dict[str, str]) -> list[str]:
    """Return a list of human-readable diff lines; empty iff byte-identical."""
    diffs = []
    for path in sorted(set(vendored) | set(live)):
        if path not in live:
            diffs.append(f"{path}: vendored only (missing from live emit)")
        elif path not in vendored:
            diffs.append(f"{path}: live only (missing from vendored tree)")
        elif vendored[path] != live[path]:
            diffs.append(f"{path}: content differs")
    return diffs


def undo_declared_edits(
    template: str, sku: str, vendored: dict[str, str]
) -> tuple[dict[str, str], list[str]]:
    """Reverse this `(template, sku)`'s `DELIBERATE_EDITS`, so the result is
    what the live emit should produce byte-for-byte.

    Returns that tree plus the declaration failures: an entry whose file is not
    in the tree at all, and -- the strict half -- an entry that found nothing to
    undo. Both are hard failures for the caller, not notes."""
    as_emitted = dict(vendored)
    failures = []
    for (declared_template, declared_sku, path), (reason, un_edit) in DELIBERATE_EDITS.items():
        if (declared_template, declared_sku) != (template, sku):
            continue
        before = as_emitted.get(path)
        if before is None:
            failures.append(
                f"{path}: DELIBERATE_EDITS declares an edit here, but the vendored "
                f"tree has no such file ({reason})"
            )
            continue
        after = un_edit(before)
        if after == before:
            failures.append(
                f"{path}: DELIBERATE_EDITS declares an edit that is no longer there "
                f"-- re-vendored, or reworded past its own matcher. Drop or update "
                f"the entry (and MANIFEST.md's 'Deliberate edits' section); leaving "
                f"it excuses the next real drift in this file ({reason})"
            )
            continue
        as_emitted[path] = after
    return as_emitted, failures


def self_check() -> None:
    """Prove the declaration mechanism on every invocation, SDK bound or not.

    A run whose declarations all still apply never exercises the strict half,
    and a run with no reachable SDK exercises none of it -- which is how the
    gate could rot into a path-level allow-list without anything going red.
    Asserts here rather than a `tests/parity/test_*.py` file because repo-root
    parity tests only run where `.github/workflows/parity.yml` names them one
    by one, and this script is already named there."""
    link = "https://github.com/alplabai/alp-sdk/blob/{}/docs/x.md"
    assert un_edit_doc_link_ref(link.format("v0.15.0-rc1")) == link.format("v0.15.0")
    assert (
        un_edit_iot_extra_conf_order(
            "# why\n# more why\nlist(PREPEND EXTRA_CONF_FILE ${_alp_generated})\n"
        )
        == "list(APPEND EXTRA_CONF_FILE ${_alp_generated})\n"
    )

    template, sku, path = "iot", "E1M-AEN801", "CMakeLists.txt"
    emitted = "list(APPEND EXTRA_CONF_FILE ${_alp_generated})\n"
    assert (template, sku, path) in DELIBERATE_EDITS

    # A declared edit that is no longer there FAILS instead of passing quietly
    # -- the `xfail(strict=True)` half. Feed the already-emitted bytes: a
    # path-level allow-list would call this clean and keep the dead entry.
    _, failures = undo_declared_edits(template, sku, {path: emitted})
    assert [f for f in failures if f.startswith(f"{path}: DELIBERATE_EDITS declares an edit that is no longer")], failures

    # ...and a declaration excuses ONLY the edit it describes: an unrelated
    # change in the same file survives the un-edit and reaches `diff_trees`.
    drifted = "stray line\n# why\nlist(PREPEND EXTRA_CONF_FILE ${_alp_generated})\n"
    as_emitted, failures = undo_declared_edits(template, sku, {path: drifted})
    assert not [f for f in failures if f.startswith(f"{path}:")], failures
    assert diff_trees({path: as_emitted[path]}, {path: emitted}) == [f"{path}: content differs"]


def run(sdk_root: Path, vendored_root: Path, pairs: list[tuple[str, str]]) -> bool:
    all_ok = True
    for template, sku in pairs:
        tree_root = vendored_root / template / sku
        vendored = read_vendored_tree(tree_root)
        try:
            live = emit_live_scaffold(sdk_root, template, sku)
        except ScaffoldEmitError as e:
            print(f"FAIL {template}/{sku}: {e}")
            all_ok = False
            continue
        augment_with_example_extras(live, sdk_root, template, vendored)
        as_emitted, declaration_failures = undo_declared_edits(template, sku, vendored)

        diffs = declaration_failures + diff_trees(as_emitted, live)
        if diffs:
            print(f"FAIL {template}/{sku}: {len(diffs)} diff(s)")
            for d in diffs:
                print(f"    {d}")
            all_ok = False
        else:
            print(f"PASS {template}/{sku} ({len(vendored)} files)")
    return all_ok


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sdk", type=Path, default=None,
                         help="Path to the alp-sdk checkout to emit live "
                              "scaffolds from. Falls back to $ALP_SDK_ROOT, "
                              "then an alp-sdk checkout next to this "
                              "tan-cli checkout.")
    parser.add_argument("--vendored", type=Path, default=VENDORED_ROOT,
                         help="Vendored tree root (default: "
                              "python/tan/templates/vendored/, the tree the "
                              "shipped Python tan reads).")
    args = parser.parse_args(argv)

    self_check()

    sdk_root, exit_code = sdk_root_or_exit_code(
        args.sdk,
        self_skip_message=(
            "SKIP: no alp-sdk checkout reachable (--sdk / $ALP_SDK_ROOT / "
            "a sibling alp-sdk checkout); scaffold byte-parity not checked "
            "this run."
        ),
    )
    if exit_code is not None:
        return exit_code

    vendored_root = args.vendored.resolve()
    pairs = discover_vendored_matrix(vendored_root)
    if not pairs:
        print(f"error: no vendored (template, sku) trees found under "
              f"{vendored_root}", file=sys.stderr)
        return 2

    ok = run(sdk_root, vendored_root, pairs)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
