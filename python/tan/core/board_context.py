# SPDX-License-Identifier: Apache-2.0
"""Where a project's `board.yaml` is, and the one fact `tan scaffold` reports
out of it.

Two halves of the same question, and both had a copy-per-command problem:

* [`resolve_board_path`] is `validate_cmd._resolve_board_path`, MOVED here
  rather than re-typed. `tan scaffold` needed exactly what `tan validate`
  already does (tan-cli#1031), and this repo already carries SIX project/board
  resolvers. A SEVENTH, written for `scaffold`, is precisely the drift
  `tan.core.shapes`' own docstring is about; and it would be the same drift
  alp-sdk-vscode#601/#633 had just finished deleting a second copy of THIS
  generator to escape (the README's `## Wiring` section went missing on the
  extension's side because a second copy existed at all). So the function
  moved to a binding-free home both commands import.

  **The criterion, stated so the next reader can CHECK the membership instead
  of re-deriving it** -- this count has now been revised three times in a
  docstring whose whole thesis is drift, which is its own argument for naming
  the rule rather than the number. A member is a NAMED, REUSABLE function
  under `python/tan/**` that turns the `--project`/`--board-yaml` pair into
  the `board.yaml` path a command works from. Per-command inline joins do not
  count; neither does anything under `tan/planner/**`, which resolves a board
  DOCUMENT out of metadata rather than a path out of two flags. The six, with
  the commands each serves:

    tan/core/board_context.py:resolve_board_path      validate, scaffold
    presets_cmd.py:resolve_project_paths              presets, pinmux, kconfig,
                                                      diff, clean, bootstrap
    build_output.py:resolve_project_context           size, image, model, west,
                                                      debug-config
    inspect_cmd.py:resolve_debug_project_context      inspect, trace,
                                                      support-bundle
    flash_cmd.py:_resolve_project                     flash
    generate_cmd.py:_resolve_board_path               generate

  FIVE of the six feed `tan.envelope.Project.resolved`, the reporting seam.
  `generate_cmd._resolve_board_path` is the one that does not, deliberately:
  `generate` reports the AS-GIVEN strings, built inline, because existence-
  checking them against the real cwd is wrong the moment `--project` differs
  from it (its own call site says so, citing tan-cli#236), and this resolver
  answers only the path it READS. It is on the list anyway because it answers
  the same question. A criterion of "uses `Project.resolved`" would have got
  this exactly backwards on both ends: it would have EXCLUDED `generate_cmd`,
  the one resolver that answers the question without the seam, while
  ADMITTING a dozen things that are not resolvers at all -- measured at this
  commit, there are 20 `Project.resolved(` call sites across 16 modules
  (excluding the definition at `tan/envelope.py:222`), mostly command
  callbacks doing an inline join. Hence the criterion above keys on what a
  function DOES, not on which helper it happens to call.

  Two earlier revisions of this paragraph were wrong in two different
  directions -- one listed `generate_cmd`, the loosest match, while omitting
  `inspect_cmd`, which does use the seam; the next inverted the reason above.
  Three wrong revisions of one claim in a docstring whose subject is drift is
  itself the argument for tan-cli#1091: prose does not hold a boundary, a
  gate does.

  The count did NOT go down: six before this change and six after, because
  the sixth is validate's, relocated. Each is pinned to a different oracle's
  reported shape, so they are distinct answers rather than duplicates, and
  unifying them is not this change's job. What this change bought is that
  `scaffold` did not make it seven.

  **"One definition" here is prose, not a gate, and stays prose until
  tan-cli#1091.** `tests/gates/test_shared_helpers_have_one_definition.py`
  hard-asserts that every helper it owns lives in `tan/core/shapes.py`, and
  it reads its ownership list out of that one file, so `resolve_board_path`
  cannot join it without generalising the gate from "shapes owns these" to a
  `{name -> home module}` map. tan-cli#1091 is the issue for exactly that,
  seeded with this function; its sibling
  `tests/gates/test_shared_test_helpers_have_one_definition.py` (tan-cli#1083)
  already IS a `{name -> home module}` allow-list and is the shape to copy.
  NOT tan-cli#1081, which an earlier revision of this paragraph pointed at:
  #1081's durable half already landed as #1083 and it is held open solely for
  an outstanding `bound_sdk*` binder audit that touches none of `TAN_ROOT`,
  `_OWNED_BY_SHAPES`, or the `rel == "tan/core/shapes.py"` assertion -- so
  closing it would have left this invariant unguarded behind a dead pointer.
  Doing the gate work here would put a refactor inside a bugfix. Until #1091
  lands, a seventh private `_resolve_board_path` would land all-green, and
  this paragraph is the only thing in its way.

* [`read_board_context`] is the content of the generated module's
  `// Board context: ...` line. Its wire spelling -- `<sku> / <os>`, with
  [`UNSET`] for a half the document does not declare -- is the retired
  alp-sdk-vscode generator's, kept verbatim so a module scaffolded from the
  extension and one scaffolded by `tan` carry the same bytes.

**What a reported path does and does not promise.** `tan.envelope.Project.
resolved` is the seam that turns a resolved path into `project.boardYaml`, and
it gates on `os.path.exists`, which is true for a DIRECTORY -- so a directory
named `board.yaml` reports a non-null path despite that method's own "only when
a file is really there". Pre-existing, and shared with `tan validate` and `tan
flash` (all three report through the same seam), so it is recorded here rather
than fixed asymmetrically in one command. It costs this module nothing:
[`read_board_context`] answers `unavailable` for that input either way, because
reading a directory raises and lands on the file-failure arm
(`test_a_directory_where_the_file_should_be_is_unavailable`).

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
#: it can never be the project's OS. Not a nicety: of the 100 example
#: `board.yaml` files in alp-sdk v0.16.0 (tag `v0.16.0`, `eb96112ba` -- an
#: earlier count of 99 here was measured off a stale `dev` working tree, not
#: the tag it cited), 53 declare `os:` at all and 51 of those declare ONLY
#: this -- reading the first declared value blindly would report `off` as the
#: board context for most real projects. Zero of the 100 carry a top-level
#: `os:`.
#:
#: The two that name a real runtime are the whole population this module can
#: answer without an SDK, so they are named rather than counted:
#: `examples/connectivity/modbus-server/board.yaml` (one core, `os: zephyr`)
#: and `examples/power-timing/power-managed-sensor/board.yaml` (`os: "off"`
#: on the parked HP core AND `os: zephyr` on the HE core -- the mixed shape
#: this constant exists for, and the one `test_a_parked_core_is_not_a_runtime`
#: pins). Every other example board renders the [`UNSET`] half.
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

    PyYAML is a DECLARED BASE dependency (`python/pyproject.toml`'s
    `dependencies = [... "pyyaml>=6" ...]`, which
    `tests/gates/test_declared_dependencies.py` enforces) -- an earlier version
    of this note claimed "tan ships no YAML dependency of its own", which is
    simply false. The `except ImportError` is still right, for the reason
    `tan.core.system_manifest._import_yaml` gives for its own copy: a frozen
    `tan` built from a stale venv. It answers "nothing resolved" rather than a
    coded envelope only because this is a comment line, not a manifest read.
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
