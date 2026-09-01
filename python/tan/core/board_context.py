# SPDX-License-Identifier: Apache-2.0
"""Where a project's `board.yaml` is, and the one fact `tan scaffold` reports
out of it.

Two halves of the same question, and both had a copy-per-command problem:

* [`resolve_board_path`] is `validate_cmd._resolve_board_path`, MOVED here
  rather than re-typed. `tan scaffold` needed exactly what `tan validate`
  already does (tan-cli#1031), and this repo already carries three
  project/board resolvers -- `presets_cmd.resolve_project_paths`,
  `build_output.resolve_project_context`, and this one. A FOURTH, written for
  `scaffold`, is precisely the drift `tan.core.shapes`' own docstring is
  about; and it would be the same drift alp-sdk-vscode#601/#633 had just
  finished deleting a second copy of THIS generator to escape (the README's
  `## Wiring` section went missing on the extension's side because a second
  copy existed at all). So the function moved to a binding-free home both
  commands import.

* [`read_board_context`] is the content of the generated module's
  `// Board context: ...` line. Its wire spelling -- `<sku> / <os>`, with
  [`UNSET`] for a half the document does not declare -- is the retired
  alp-sdk-vscode generator's, kept verbatim so a module scaffolded from the
  extension and one scaffolded by `tan` carry the same bytes.

**`board.yaml` ONLY, no SDK -- deliberately.** `tan scaffold` resolves no
alp-sdk checkout (its own module docstring says so, and one of tan-cli#1031's
four reported invocations passes no `--sdk-root` at all), and this module does
not change that. The consequence is stated rather than hidden: a v2
`board.yaml` that omits `os:` genuinely does not carry one -- alp-sdk's
`board.schema.json` says of `cores.<id>.os` that "The OS runtime is DERIVED
from the core's silicon class and is not selectable... Omit `os:` to take that
runtime", so the value then lives in the SoM preset, behind a checkout. That
half renders [`UNSET`]; it is not guessed from the core id. Resolving it would
mean walking `board.yaml -> som.sku -> metadata/e1m_modules/<sku>.yaml ->
silicon -> metadata/socs/**.json -> cores[].type ->
tan.core.os_class.default_os_from_core_type`, which would make a COMMENT's
bytes depend on which checkout happened to be discovered on the machine that
ran the scaffold. That is a bigger contract than a comment is worth; if it is
wanted it is a separate, argued change.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

#: What a resolved `board.yaml` renders for a field it does not declare. The
#: retired alp-sdk-vscode generator's own `?? "<unset>"` spelling
#: (`packages/alp-core/src/wizard/service.ts`, deleted in alp-sdk-vscode#601).
UNSET = "<unset>"

#: A `cores.<id>.os` value that PARKS a core rather than naming a runtime, so
#: it can never be the project's OS. Not a nicety: of the 99 example
#: `board.yaml` files in alp-sdk v0.16.0, 53 declare `os:` at all and 51 of
#: those declare exactly this -- reading the first declared value blindly
#: would report `off` as the board context for most real projects.
OS_OFF = "off"

#: `cores.<id>.os` values that are declared but carry no runtime name. `""`
#: is not schema-valid (the enum is `zephyr|yocto|baremetal|off`); it is
#: rejected here anyway rather than being echoed into a C comment, since this
#: input is a hand-edited customer file and nothing upstream of here has
#: validated it.
_NOT_A_RUNTIME = ("", OS_OFF)


def resolve_board_path(project: str | None, board_yaml: str | None) -> tuple[str, str]:
    """Return `(project_root, board_yaml_path)`, both as the CLI reports them.

    Mirrors `resolve_offline_board_path`: the root defaults to the literal `"."`
    and the board path stays RELATIVE, which the conformance fixtures pin
    (`project.root == "."`, `boardYamlPath == "./board.yaml"`).

    **`board_yaml` -- the `--board-yaml` flag -- WINS over the project-relative
    default whenever it is given**: absolute, verbatim; relative, joined onto
    the root. That is what its help text has always claimed ("Explicit
    board.yaml path (overrides project resolution)") and what tan-cli#1031
    found `tan scaffold` was not honouring, because `scaffold` never called a
    resolver at all.

    Existence is NOT checked here, and must not be: `validate`'s refusal
    message names the path it could not open, so nulling it at the resolver
    would strip the path out of the very message that names it.
    `tan.envelope.Project.resolved` is the seam that checks -- its own
    docstring documents that split.
    """
    root = project if project else "."
    if board_yaml and os.path.isabs(board_yaml):
        return root, board_yaml
    leaf = board_yaml or "board.yaml"
    # Joined as STRINGS, not via pathlib: `Path(".") / "board.yaml"` normalises
    # to `board.yaml`, but Rust's `Path::new(".").join("board.yaml")` keeps the
    # `./`, and the conformance fixtures pin `"./board.yaml"`.
    sep = "" if root.endswith(("/", "\\")) else "/"
    return root, f"{root}{sep}{leaf}"


def _board_mapping(board_yaml_path: str | None) -> dict[str, Any] | None:
    """`board.yaml` parsed to a mapping, or `None` for EVERY way that can
    fail -- no path at all, a missing file, a directory, an unreadable one, a
    non-UTF-8 byte, malformed YAML, an empty document, a document that parses
    to something other than a mapping, or PyYAML not installed.

    **Never raises**, and that is the contract, not an implementation detail:
    `tan scaffold`'s job is to write a module, and it must still write one
    when the `board.yaml` beside it is broken. `unavailable` is what the
    generated line is FOR.

    The broad `except` is deliberate rather than lazy. `UnicodeDecodeError` is
    a `ValueError`, NOT an `OSError`, so an `except OSError` alone could never
    catch it -- one undecodable byte in a customer's own board.yaml escaped
    `tan model` as a traceback for exactly that reason (tan-cli#396). Nothing
    upstream of here has validated this file.

    tan ships no YAML dependency of its own, so PyYAML is used when importable
    and its absence degrades to "nothing resolved" -- the same shape
    `debug_config_cmd._load_yaml` takes for the build's own output files.
    """
    if not board_yaml_path:
        return None
    try:
        import yaml  # noqa: PLC0415  (optional at runtime, by design)
    except ImportError:
        return None
    # `Path(...)` is built OUTSIDE the `try` on purpose. The backstop below is
    # for a BROKEN FILE; a `board_yaml_path` that is not path-like at all is a
    # caller bug, and swallowing its `TypeError` here would report "no board
    # context" for a defect in `tan`'s own code. It is also what makes the
    # `if not board_yaml_path` guard above load-bearing rather than decorative:
    # measured, with the try widened to cover this line, deleting that guard
    # left `test_no_path_at_all_is_unavailable` GREEN, because `Path(None)`'s
    # `TypeError` landed on the file-failure arm and answered `None` by
    # accident (tan-cli#1031's own mutation run, mutant G3).
    path = Path(board_yaml_path)
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 -- the customer's hand-edited file, not ours
        return None
    return document if isinstance(document, dict) else None


def _declared_os(board: dict[str, Any]) -> str | None:
    """The OS this `board.yaml` DECLARES, or `None` when it declares none.

    Two shapes, because `board.yaml` has had two. A v1 document carries a
    top-level `os:` -- the field the retired alp-sdk-vscode generator read
    (`boardModel.os`) and the one that produced the
    `// Board context: E1M-AEN801 / zephyr` line tan-cli#1031 asks for. A v2
    document has no top-level `os` at all (alp-sdk's `board.schema.json`
    declares no such property, and the extension's own `normalizeBoardModel`
    deleted it on the way out: "v2: top-level os: has no meaning"); v2's home
    for the field is `cores.<id>.os`, which the second arm reads.

    Cores that DISAGREE collapse to `None` rather than picking one. A
    heterogeneous project (`a55_cluster: yocto` + `m33_sm: zephyr`) has no
    single OS, and a module scaffold is never told which core it is for
    (`plan_module_files` receives the template definition and the name,
    nothing else) -- so there is nothing to choose with.

    `None` is also the honest answer for the common v2 project that declares
    no `os:` anywhere; see the module docstring for why that is not resolved
    from the SDK here.
    """
    top = board.get("os")
    if isinstance(top, str) and top not in _NOT_A_RUNTIME:
        return top
    cores = board.get("cores")
    if not isinstance(cores, dict):
        return None
    declared = {
        entry["os"]
        for entry in cores.values()
        if isinstance(entry, dict)
        and isinstance(entry.get("os"), str)
        and entry["os"] not in _NOT_A_RUNTIME
    }
    return declared.pop() if len(declared) == 1 else None


def read_board_context(board_yaml_path: str | None) -> str | None:
    """`"<som.sku> / <os>"` for the generated `// Board context:` line, or
    `None` when there is no board context to report -- the caller then keeps
    the `unavailable` placeholder that line has always carried, now meaning
    "genuinely none" rather than "never looked" (tan-cli#1031).

    **`som.sku` is the gate, not a field that may go [`UNSET`].** It is the one
    value alp-sdk's `board.schema.json` requires at the top level (`required:
    [som, cores]`, and `som.required: [sku]`), so a document without a usable
    one is not a board -- reporting `<unset> / <unset>` for it would dress a
    broken file up as a resolved board, and `project.boardYaml` would still
    name the file for anyone who wants to look. Every other half may be
    `<unset>`; see [`_declared_os`].
    """
    board = _board_mapping(board_yaml_path)
    if board is None:
        return None
    som = board.get("som")
    sku = som.get("sku") if isinstance(som, dict) else None
    if not isinstance(sku, str) or not sku:
        return None
    return f"{sku} / {_declared_os(board) or UNSET}"
