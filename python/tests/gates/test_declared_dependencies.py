# SPDX-License-Identifier: Apache-2.0
"""Every third-party module `tan/` imports must be declared in pyproject.

Written because a real `pip install tan-cli` failed on first run with
`ModuleNotFoundError: No module named 'click'` while every local test suite
passed. `tan/cli.py` does `from click.testing import CliRunner` (that is how
`--help` under `--format json` is captured into one envelope instead of Click
printing to stdout and calling `ctx.exit(0)`), and click had never been declared
-- it arrived transitively because typer depended on it. **typer 0.27.0 dropped
that dependency**, so the transitive supply vanished and the import became a
first-run crash for anyone whose environment did not happen to carry click.

Nothing caught it, and nothing could have: the test suite runs in an environment
where click is installed for other reasons, so importing it works. The only
reliable signal is a STATIC one -- compare what the source imports against what
the distribution promises -- which is what this gate does.

Two failure directions matter and both are covered:
  * an import with no matching declaration (the click case) -- a broken install;
  * an import this gate cannot classify at all -- which must FAIL rather than be
    waved through, since an unknown name is exactly how the next one hides.

Deliberately conservative: an import guarded by `TYPE_CHECKING` is treated as
needing a declaration too. Over-declaring costs a line in pyproject; under-
declaring costs a broken `pip install`.
"""

from __future__ import annotations

import ast
import pathlib
import sys
import tomllib

_HERE = pathlib.Path(__file__).resolve()
PACKAGE = _HERE.parents[2] / "tan"
PYPROJECT = _HERE.parents[2] / "pyproject.toml"

#: Import name -> distribution name. They differ often enough (`yaml` ships in
#: `pyyaml`) that guessing is wrong. An import missing from this map fails the
#: gate on purpose: adding an entry is the moment to ask whether the
#: distribution also belongs in `dependencies`.
IMPORT_TO_DIST = {
    "typer": "typer",
    "rich": "rich",
    "yaml": "pyyaml",
    "jsonschema": "jsonschema",
    "click": "click",
    "serial": "pyserial",
    "truststore": "truststore",
    "certifi": "certifi",
    "cbor2": "cbor2",
    "tflite": "tflite",
}

#: Vendored customer scaffolds are template DATA, not part of tan's own runtime
#: import graph -- their imports are the generated project's problem.
EXCLUDED_PARTS = ("templates", "vendored")


def _norm(spec: str) -> str:
    """Distribution name out of a requirement string: 'typer>=0.12' -> 'typer'."""
    name = spec.split(";")[0].strip()
    for sep in ("[", "=", ">", "<", "!", "~", " "):
        name = name.split(sep)[0]
    return name.strip().lower().replace("_", "-")


def _declared_distributions() -> tuple[set[str], set[str]]:
    """(required, optional) distribution names from pyproject.

    The split matters: a REQUIRED distribution may be imported anywhere, but an
    OPTIONAL one is absent from a default install, so importing it at module
    scope makes the entire CLI unimportable rather than degrading one command.
    """
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    required = {_norm(s) for s in data["project"]["dependencies"]}
    optional: set[str] = set()
    for specs in (data["project"].get("optional-dependencies") or {}).values():
        optional |= {_norm(s) for s in specs}
    return required, optional


def _source_files() -> list[pathlib.Path]:
    return [
        p for p in sorted(PACKAGE.rglob("*.py"))
        if not any(part in EXCLUDED_PARTS for part in p.relative_to(PACKAGE).parts)
    ]


def _imports(path: pathlib.Path) -> tuple[set[str], set[str]]:
    """(module_scope, lazy) top-level module names of one file's absolute imports.

    Module scope means a direct child of the module body -- executed the instant
    anything imports this file. Anything nested (inside a function, a method, a
    `try:`) only runs when that code path does, which is what makes an optional
    dependency survivable.
    """
    def names(node: ast.AST) -> set[str]:
        if isinstance(node, ast.Import):
            return {a.name.split(".")[0] for a in node.names}
        # node.level > 0 is a relative import -- first-party by definition.
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            return {node.module.split(".")[0]}
        return set()

    tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"), str(path))
    module_scope: set[str] = set()
    for node in tree.body:
        module_scope |= names(node)
    every: set[str] = set()
    for node in ast.walk(tree):
        every |= names(node)
    return module_scope, every - module_scope


def _third_party() -> dict[str, dict]:
    """Third-party import name -> {files, module_scope}."""
    out: dict[str, dict] = {}
    for path in _source_files():
        rel = str(path.relative_to(PACKAGE.parent))
        top, lazy = _imports(path)
        for mod in top | lazy:
            if mod == "tan" or mod in sys.stdlib_module_names:
                continue
            entry = out.setdefault(mod, {"files": [], "module_scope": False})
            entry["files"].append(rel)
            if mod in top:
                entry["module_scope"] = True
    return out


def test_every_third_party_import_is_a_declared_dependency():
    required, optional = _declared_distributions()
    unmapped: list[str] = []
    undeclared: list[str] = []
    eager_optional: list[str] = []
    for mod, info in sorted(_third_party().items()):
        where = ", ".join(sorted(info["files"])[:3])
        dist = IMPORT_TO_DIST.get(mod)
        if dist is None:
            unmapped.append(f"{mod} (imported by {where})")
            continue
        norm = dist.lower().replace("_", "-")
        if norm in required:
            continue
        if norm not in optional:
            undeclared.append(f"{mod} -> distribution `{dist}` (imported by {where})")
        elif info["module_scope"]:
            eager_optional.append(f"{mod} -> `{dist}` at MODULE SCOPE in {where}")
    assert not unmapped, (
        "Third-party import this gate cannot classify. Add it to IMPORT_TO_DIST\n"
        "with its real distribution name, and then decide whether that\n"
        "distribution belongs in `dependencies` or in an extra:\n  "
        + "\n  ".join(unmapped)
    )
    assert not undeclared, (
        "`tan/` imports a distribution pyproject declares nowhere, so a clean\n"
        "`pip install tan-cli` will crash on first run even though every test\n"
        "here passes (the module is present locally for unrelated reasons).\n"
        "Declare it in `dependencies`, or as an extra if it is optional, or\n"
        "stop importing it:\n  " + "\n  ".join(undeclared)
    )
    assert not eager_optional, (
        "An EXTRAS-only distribution is imported at module scope. It is absent\n"
        "from a default install, so this does not degrade one command -- it makes\n"
        "the whole CLI unimportable, and even `tan --version` dies. Move the\n"
        "import inside the function that needs it and refuse with an actionable\n"
        "message when it is missing:\n  " + "\n  ".join(eager_optional)
    )


def test_the_scan_actually_reads_the_package():
    """Guard against a vacuous pass.

    If the walker silently finds nothing -- a moved package root, a changed
    layout, an rglob that matches no files -- the gate above passes while
    checking nothing. Pin the two imports that are structurally certain to
    exist: `tan/cli.py` is built on typer, and it imports click directly for
    the `--help` capture that is the whole reason this gate exists.
    """
    files = _source_files()
    assert len(files) > 20, f"only {len(files)} source files found under {PACKAGE}"
    seen = _third_party()
    for expected in ("typer", "click"):
        assert expected in seen, (
            f"`{expected}` not found among tan's third-party imports; the scan "
            f"is not reading what it thinks it is (found: {sorted(seen)})"
        )
