#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Scaffold byte-parity gate (alp-sdk#864): vendored wizard templates vs. a
live `alp-sdk --emit scaffold`.

`tan init`/`tan scaffold` are SDK-free — for a template mapped onto an SDK
scaffold-catalog id (see `crates/tan-core/src/wizard/vendored/MANIFEST.md`),
they read a vendored copy of `alp_project.py --emit scaffold --template <id>
--sku <sku>`'s output baked into the binary, instead of shelling the SDK or
re-deriving its build-integration conventions in Rust. That vendored copy can
silently drift from the SDK if a future SDK scaffold change is never
re-vendored -- exactly the RFC #843-style drift ADR-0020 exists to kill for
the build-plan seam. This script is the tan-cli side of the
`repository_dispatch` gate ADR-0020 Amendment 1 mandates (see
`tests/parity/README.md` for the seam-1 build-plan analogue this mirrors):
for every vendored (template, sku) pair, re-run the live SDK emit and assert
byte-identity against the vendored tree.

Unlike seam1_field_diff.py (which hard-requires `--sdk`), this gate is
optionally self-skipping: `tan-core`'s own `cargo test` byte-parity test
(`zephyr_app_scaffold_is_byte_exact_for_the_vendored_sku`) already proves the
vendored tree is internally consistent without an SDK checkout, so a local
`cargo test`/dev-loop run of this script with no reachable alp-sdk checkout
is a clean no-op, not a failure. Reachability is checked in this order:
`--sdk`, then `$ALP_SDK_ROOT`, then an `alp-sdk` checkout next to this
tan-cli checkout.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path

VENDORED_ROOT = Path(__file__).resolve().parent.parent.parent / (
    "crates/tan-core/src/wizard/vendored"
)

# Vendored files `--emit scaffold`'s envelope never covers (its
# `files.user_owned` list, per `metadata/templates/catalog-v1.json`) -- the
# SDK's own twister harness for the catalog's canonical `example:`, vendored
# alongside the scaffold envelope but compared against that example directory
# instead of the live emit.
NON_ENVELOPE_EXTRAS = ("testcase.yaml",)


class ScaffoldEmitError(RuntimeError):
    """Raised for a live SDK emit failure (not a byte diff -- diffs are reported)."""


def _looks_like_sdk_checkout(path: Path) -> bool:
    return (path / "scripts" / "alp_project.py").is_file()


def resolve_sdk_root(explicit: Path | None) -> Path | None:
    """Find a reachable alp-sdk checkout: `--sdk`, then `$ALP_SDK_ROOT`, then a
    `../alp-sdk` sibling of this tan-cli checkout. `None` if none resolves --
    the caller treats that as a clean skip, not a failure."""
    candidates = []
    if explicit is not None:
        candidates.append(explicit)
    env_root = os.environ.get("ALP_SDK_ROOT")
    if env_root:
        candidates.append(Path(env_root))
    candidates.append(Path(__file__).resolve().parent.parent.parent.parent / "alp-sdk")

    for candidate in candidates:
        candidate = candidate.resolve()
        if _looks_like_sdk_checkout(candidate):
            return candidate
    return None


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

        diffs = diff_trees(vendored, live)
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
                         help="Vendored tree root (default: tan-core's "
                              "wizard/vendored/ next to this script).")
    args = parser.parse_args(argv)

    sdk_root = resolve_sdk_root(args.sdk)
    if sdk_root is None:
        print("SKIP: no alp-sdk checkout reachable (--sdk / $ALP_SDK_ROOT / "
              "a sibling alp-sdk checkout); scaffold byte-parity not checked "
              "this run.")
        return 0

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
