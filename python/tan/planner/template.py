# SPDX-License-Identifier: Apache-2.0
"""`--emit scaffold`: a NEW project's files, rendered from the SDK's catalog.

RELOCATED (was alp-sdk `scripts/alp_template.py`). Only the `--emit scaffold`
path came across -- `render_to_envelope()` and everything it reaches. What stayed
behind stayed because nothing in `tan` reaches it and a second copy would be a
second source of truth:

* `render()` / `RenderPlan` / `DestinationNotEmptyError` -- the disk-writing
  front door (`alp_template.py render`, and `alp generate`'s `sku=` form). The
  one line of its `plan()` this path actually uses is the FILE ORDER, inlined
  below as `_ordered_files`.
* `validate()` / `ValidateResult` / `_count_passed` -- the twister self-test that
  renders a template into a temp dir and builds it. An SDK CI gate, not a
  customer path.
* `default_sku()` and the `argparse` CLI -- callers that stayed in alp-sdk.

**This is NOT `tan/core/scaffold.py`, and the two must not be merged.** That
module serves `tan init` from a VENDORED capture under `tan/templates/vendored/`
and consults no SDK checkout at all (invariant I-32), deliberately -- see its own
docstring on why re-deriving the SDK's `_scaffold_cmakelists()` rewrite in tan
would be a silent `ALP_SDK_ROOT`-guess bug. This module is the OTHER path: it
runs behind `tan.planner_cli`, which has already bound a real SDK root, so it
reads that checkout's LIVE `metadata/templates/catalog-v1.json` and the canonical
example the record names, exactly as `scripts/alp_project.py` did. Two paths, two
purposes: `tan init` must keep working with no SDK anywhere; `--emit scaffold`
exists to be byte-identical to the SDK's own front door, which no vendored
snapshot can promise for a template x SKU pair nobody captured.

Two relocation changes, no behavioural one:

* **`REPO` / `METADATA_ROOT` are the BOUND SDK checkout** (`paths.py`), not a
  `__file__` walk. The walk landed on the SDK because the file lived there;
  inside `tan` it would land on the `tan` package and every `metadata/**` and
  `examples/**` path would be wrong.
* **`plan()` collapsed to `_ordered_files`.** `render_to_envelope` called
  `plan()` for one value -- `tuple(sorted(record["files"]["user_owned"]))` -- and
  re-validated the params the very next line already validates. The rest of
  `plan()` served `render()`, which did not come across.

Docstrings below still name `render()`, `validate()`, `plan()`/`RenderPlan` and
`alp_template.py render`: those are alp-sdk's own paths, and the sentences are
true of the file this moved from. They are kept verbatim so the move stays
diffable against it.

SPLIT (tan-cli#1142): this module was 2206 lines against `MODULE_CAP = 800`.
`template_pins.py` (SoM/board-metadata reads and rename derivation) and
`template_rewrite.py` (board.yaml/CMakeLists.txt/README.md text rewrites) now
carry roughly half the body each; this module keeps the catalog/parameter
half plus `render_to_envelope`/`emit_scaffold` themselves, since those are
what `cli.py` imports and where the hand-port audit is densest. All three
files are the SAME `HAND_PORT_SOURCES` entry for `scripts/alp_template.py` --
see `template_pins.py`'s own module docstring for why that split is allowed
(`MIRRORED_PREFIX` bars a `PINNED_HASHES` module's shape from diverging from a
moving upstream file it is 3-way-merged against; it says nothing about a
`HAND_PORT_SOURCES` one, which upstream never merges into) and for the load-
bearing detail of how the two new modules import `TemplateError` back from
here without a real import cycle.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tan.core.document_guards import SHAPE_NOUN, DocumentGuards

from .orchestrator import _zephyr_app_dir
from .paths import METADATA_ROOT, REPO

__all__ = [
    "ParameterError",
    "SkuNotSupportedError",
    "TemplateError",
    "TemplateNotFoundError",
    "emit_scaffold",
    "render_to_envelope",
]

CATALOG = REPO / "metadata" / "templates" / "catalog-v1.json"


def _ordered_files(
    record: dict[str, Any], *, doc: Any, field: str,
) -> tuple[str, ...]:
    """The envelope's file ORDER: the record's own `files.user_owned` list,
    sorted. Verbatim from `plan()`, which did not otherwise come across (see the
    module docstring). `files.generated` is never in it -- those artefacts are
    emitted later, at build-configure time, by the planner itself.

    tan-cli#1077: the double subscript was bare. `KeyError: 'files'` on a
    record missing it, `KeyError: 'user_owned'` one level in, and
    `TypeError: '<' not supported between instances of 'int' and 'str'`
    out of `sorted()` on a list whose entries are not the strings the
    schema declares. All three keys are `required` in
    `template-catalog-v1.schema.json` (`items: {"type": "string"}` for the
    list), so naming them is never stricter than that schema."""
    files = _require_key(record, "files", dict, doc=doc, field=field)
    return tuple(sorted(
        _require_field(name, str, doc=doc,
                       field=f"{field}.files.user_owned[{i}]")
        for i, name in enumerate(_require_key(
            files, "user_owned", list, doc=doc, field=f"{field}.files"))))


class TemplateError(Exception):
    """Base for every error alp_template raises -- never a bare
    KeyError/ValueError escapes to a caller."""


class TemplateNotFoundError(TemplateError):
    """No record with this id in the catalog."""


class ParameterError(TemplateError):
    """An unknown parameter name, a type mismatch, or a constraint
    violation (enum / minimum / maximum)."""


class SkuNotSupportedError(TemplateError):
    """`render_to_envelope`'s --sku is not in the record's declared
    `supported.som_skus` -- a hard error, never a best-effort render."""


class PathEscapeError(TemplateError):
    """A catalog-declared `files.user_owned` path resolves outside its
    example/destination root (alp-sdk#1126). The schema itself puts no
    shape constraint on these entries (plain strings, no pattern), so
    this is enforced at the point of use, not by schema validation
    alone -- containment is checked on the RESOLVED path (symlinks
    followed), not by pattern-matching for `..`."""


class AmbiguousCoresError(TemplateError):
    """`find_template_by_cores()`'s `cores` topology matches more than
    one catalog record -- naming the candidates rather than guessing
    which one the caller meant (use `--template` to disambiguate)."""


def load_catalog(catalog_path: Path | None = None) -> dict[str, Any]:
    """Parse `metadata/templates/catalog-v1.json` -- the FOURTH document
    this module reads, and its only JSON one (tan-cli#1077).

    The three YAML documents already go through `_require_mapping_doc`;
    this decode did not. A catalog that is legal JSON but not an object (a
    bare list, a bare scalar) reached every caller's `doc.get("templates",
    ...)` as a raw `AttributeError`, and a catalog that is not legal JSON
    at all escaped as a raw `json.JSONDecodeError` -- and `cli._emit_
    scaffold` catches `TemplateError` and nothing else, so both reached a
    CLI user as a traceback. That is the defect this whole family
    (tan-cli#1025 -> #1034 -> #1037/#1048 -> #1052 -> this) exists to
    close, one document at a time.

    The `noun` is "a JSON object", not the register's shared default "a
    YAML mapping": same helper, same message shape, one word that is true
    of THIS document. The three YAML callers keep their default and their
    byte-identical message.

    The ABSENT-document half is curated here too (tan-cli#1077 review). The
    first cut deferred it as "equally true of all four documents this
    module reads"; measured, it was not -- `_load_som_doc` and
    `_board_route_entries` carried an `is_file()` check and `_docs_ref` an
    `except OSError`, so three of the module's five reads were ALREADY
    handled and only this one and `render_to_envelope`'s example
    `board.yaml` were bare. A false symmetry claim in a docstring is the
    same defect class this round exists to close, so both are guarded and
    the claim is gone. `except OSError`, not a pre-flight `is_file()`: a
    present-but-unreadable path (a directory, a permissions error) is named
    too, not only a missing one.

    tan-cli#1133 finished that and corrected the COUNT: this module makes
    SIX filesystem reads, not five -- the four documents (this catalog, the
    SoM preset, the board metadata, the example `board.yaml`), `_docs_ref`'s
    `sdk_version.yaml`, and `_rendered_bytes`' per-file `read_bytes()`,
    which no round of this family had looked at and which was raw on three
    separate errnos (PR #1160 review, MAJOR 1). All six are curated now and
    no `is_file()` pre-flight is left. Re-derive by grepping
    `read_text|read_bytes|safe_load|json.loads` here rather than inheriting:
    the version of this claim one revision ago was off by one, in the
    direction that hid a live defect.

    tan-cli#1084: the read itself is `DocumentGuards.read_catalog_document`
    now -- the SAME body, moved to `tan/core/document_guards.py` so
    `tan/core/example_catalog.py`'s second implementation of this read
    produces byte-identical messages instead of the raw
    `FileNotFoundError`/`JSONDecodeError`/`AttributeError` it used to.
    """
    return _GUARDS.read_catalog_document(catalog_path or CATALOG)


def find_template(
    doc: dict[str, Any], template_id: str, *, path: Any = CATALOG,
) -> dict[str, Any]:
    """The catalog record whose `id:` is @template_id.

    tan-cli#1077: `rec["id"]` was bare at BOTH sites -- the match scan and
    the not-found message -- so a `templates:` entry with no `id:` (or one
    that is not a mapping at all) raised `KeyError: 'id'` / `TypeError`
    instead of a curated error naming the catalog, the record and the
    type. @path is a MESSAGE LABEL only, never read.

    Its `CATALOG` default is the bound checkout's own catalog -- exactly
    the file `load_catalog()` reads when no caller overrode IT, so the two
    defaults agree and the label cannot lie for the one caller that omits
    it (`tan/planner/cli.py:182`, whose `load_catalog()` is also
    default-pathed). It CAN lie for a caller that passes a custom
    `catalog_path` to `load_catalog` and then omits `path` here; there is
    no such caller today (`render_to_envelope` threads the resolved path
    through, and every test that asserts on the file name passes it
    explicitly), and making it required would mean editing `cli.py` and
    the pre-existing `test_find_template_by_cores.py` fixtures for a label
    (tan-cli#1077 review nit -- recorded rather than silently accepted).

    Every id is resolved up front, as the not-found path always did (and
    as the match scan did for every record up to the match). A record
    malformed AFTER the requested one therefore reds where it used to be
    skipped: a new refusal, of a document the schema never accepted (`id`
    is `required` on every record), asserted as its own test rather than
    folded in.
    """
    records = _catalog_templates(doc, path=path)
    ids = [_require_key(rec, "id", str, doc=path, field=f"templates[{i}]")
           for i, rec in enumerate(records)]
    for rec, rec_id in zip(records, ids):
        if rec_id == template_id:
            return rec
    known = ", ".join(sorted(ids))
    raise TemplateNotFoundError(
        f"no template {template_id!r} in catalog (known: {known})")


def find_template_by_cores(
    doc: dict[str, Any], cores: dict[str, str], *, path: Any = CATALOG,
) -> dict[str, Any]:
    """Select the catalog record whose `cores:` topology (core id ->
    os) is EXACTLY `cores` -- alp-sdk#1652's `--cores` scaffold input.

    RELOCATED verbatim from alp-sdk's `scripts/alp_template.py` (the
    HAND_PORT_HASHES entry for this file). This is a SELECTOR over the
    catalog's existing templates, not a generic skeleton renderer: an
    IDE wizard names the core/OS topology it wants (e.g. `{"m55_hp":
    "zephyr", "m55_he": "zephyr"}`) instead of naming a template id,
    and gets back whichever already-gated example matches -- never an
    arbitrary, never-built combination. See the issue's recorded
    decision: the scaffold's value to a customer is that the generated
    app builds on their SoM, which only holds for a topology this SDK
    already ships and twister-gates.

    No exact match -> TemplateNotFoundError naming the topologies that
    ARE on offer. More than one exact match -> AmbiguousCoresError
    naming the candidate ids (use --template to disambiguate; this can
    happen when two templates share a core/OS shape but differ in
    what they actually do, e.g. an RPMsg demo vs a compute-offload
    demo on the same SoM).

    `tan init`'s customer-facing selector (`--topology`) does NOT call
    this function -- it runs with no SDK checkout bound at module-import
    time (invariant I-32) and cannot import `tan.planner` for that
    reason (see `tan/core/example_catalog.py`'s own docstring on the
    same constraint), so it carries a small standalone re-implementation
    instead. This copy is the one `tan.planner_cli --emit scaffold
    --cores` (the developer/parity entry that mirrors alp-sdk's own argv
    1:1) actually calls, and the one `HAND_PORT_HASHES` audits against
    alp-sdk's original.

    @path is the same message-label-only keyword `find_template` takes,
    with the same `CATALOG` default and the same caveat -- see its
    docstring.

    tan-cli#1077: `_topology`'s `{c["id"]: c["os"] ...}` and the ambiguous
    branch's `rec["id"]` were bare subscripts on catalog-sourced mappings,
    and all three `doc.get("templates", [])` iterations were unguarded.
    `id`/`os` are `required` on every `cores[]` entry and `id` on every
    record, so requiring them is not stricter than the schema. Each
    topology is computed ONCE now (it used to be recomputed per record per
    branch) and the ids of the MATCHES are resolved before the >1 test, so
    the single-match record's `id:` is checked too -- that is what makes
    `cli._emit_scaffold`'s own `record["id"]` (`planner/cli.py:186`) safe,
    the one site of this defect that lives outside this module.
    """
    def _topology(rec: Any, index: int) -> dict[str, str]:
        field = f"templates[{index}]"
        _require_field(rec, dict, doc=path, field=field)
        entries = _require_field(rec.get("cores", []), list,
                                 doc=path, field=f"{field}.cores")
        return {
            _require_key(core, "id", str, doc=path,
                         field=f"{field}.cores[{j}]"):
            _require_key(core, "os", str, doc=path,
                         field=f"{field}.cores[{j}]")
            for j, core in enumerate(entries)}

    indexed = list(enumerate(_catalog_templates(doc, path=path)))
    topologies = {index: _topology(rec, index) for index, rec in indexed}
    matches = [(index, rec) for index, rec in indexed
               if topologies[index] == cores]
    if not matches:
        known = sorted(
            {tuple(sorted(topo.items())) for topo in topologies.values()})
        raise TemplateNotFoundError(
            f"no template with cores topology {cores!r} in catalog "
            f"(known topologies: {known})")
    ids = sorted(
        _require_key(rec, "id", str, doc=path, field=f"templates[{index}]")
        for index, rec in matches)
    if len(matches) > 1:
        raise AmbiguousCoresError(
            f"cores topology {cores!r} matches multiple templates "
            f"{ids} -- use --template to disambiguate")
    return matches[0][1]


def _coerce(spec: dict[str, Any], raw: Any) -> Any:
    """Coerce a CLI-style string override to the parameter's declared
    type. Values already of the right type (e.g. an untouched default,
    or a native value a Python caller passed directly) pass through.

    `spec["type"]` / `spec["name"]` stay BARE subscripts on purpose
    (tan-cli#1077): this function and `_check_constraints` are reached
    only from `_resolve_params` (`:346` and `:347`, verified by the
    tan-cli#1077 review), and only after `_record_parameters` has required
    both keys on every spec. Guarding them a second time here would be a
    second register for the same fact.

    That reasoning covers the SHAPE of `_check_constraints`'s
    `spec.get("constraints")` and its `constraints["enum"]` /
    `["minimum"]` / `["maximum"]` reads -- and only their shape, and only
    since `_require_constraints` joined `_record_parameters`. Before that,
    this docstring's claim was written while a sixth unguarded subscript
    sat one function over (tan-cli#1077 review, MAJOR 1).

    It does NOT cover what `_check_constraints` then DOES with them: a
    schema-VALID `type: string` (or `enum`) parameter carrying
    `constraints.minimum`/`maximum` is legal per `$defs/parameter`, and
    `_require_constraints` guarantees the `int` that would make a bare
    `"a" < 5` raise a `TypeError`. tan-cli#1087 closed that: `_check_
    constraints` now refuses `minimum`/`maximum` on any non-`integer`
    `type` with a curated `ParameterError` before it ever compares, so the
    bare `TypeError` this paragraph used to describe as latent no longer
    happens -- do not read it as still open."""
    if not isinstance(raw, str):
        return raw
    ptype = spec["type"]
    if ptype == "integer":
        try:
            return int(raw)
        except ValueError as exc:
            raise ParameterError(
                f"{spec['name']}: {raw!r} is not an integer") from exc
    if ptype == "boolean":
        if raw.lower() in ("1", "true", "yes"):
            return True
        if raw.lower() in ("0", "false", "no"):
            return False
        raise ParameterError(f"{spec['name']}: {raw!r} is not a boolean")
    return raw  # string / enum stay strings


def _check_constraints(template_id: str, spec: dict[str, Any], value: Any) -> None:
    """Enforce `spec["constraints"]` against the coerced @value.

    tan-cli#1087: `minimum`/`maximum` are only well-typed against `type:
    integer` -- `$defs/parameter.type` is exactly `string`/`integer`/
    `boolean`/`enum`, none of the other three compare against an `int`
    bound, and the schema does not cross-reference `type` against
    `constraints` at all. `boolean` is refused too, on purpose, even though
    `bool < int` never raises -- a bound that cannot crash still is not one
    that means anything on a boolean knob. A schema addition of a fifth
    numeric `type` would need a matching line here.

    DIVERGES FROM alp-sdk on this input class, deliberately: `scripts/
    alp_template.py`'s own `_check_constraints` has no such guard and still
    raises the bare `TypeError` this closes. alp-sdk#1916 tracks closing the
    gap; until then tan refuses where alp-sdk still crashes.

    `constraints.enum` is the SAME inapplicability class one line below --
    an `integer` parameter can never satisfy it, since the schema forces
    `enum` items to strings -- and is DELIBERATELY LEFT OPEN here: it fails
    loudly with a value-blaming message rather than crashing, so it stayed
    out of #1087's scope. alp-sdk#1916 tracks it too.
    """
    constraints = spec.get("constraints") or {}
    if "enum" in constraints and value not in constraints["enum"]:
        raise ParameterError(
            f"{template_id}: {spec['name']}={value!r} not in "
            f"{constraints['enum']}")
    for bound in ("minimum", "maximum"):
        if bound in constraints and spec["type"] != "integer":
            raise ParameterError(
                f"{template_id}: {spec['name']}={value!r} is type "
                f"{spec['type']!r}; constraints.{bound} "
                f"({constraints[bound]!r}) only applies "
                f"to type 'integer'")
    if "minimum" in constraints and value < constraints["minimum"]:
        raise ParameterError(
            f"{template_id}: {spec['name']}={value!r} < minimum "
            f"{constraints['minimum']}")
    if "maximum" in constraints and value > constraints["maximum"]:
        raise ParameterError(
            f"{template_id}: {spec['name']}={value!r} > maximum "
            f"{constraints['maximum']}")


def _resolve_params(
    record: dict[str, Any], params: dict[str, Any] | None,
    *, doc: Any, field: str,
) -> dict[str, Any]:
    """Resolve every declared parameter to its effective value (override
    or default), rejecting any name the record doesn't declare -- this
    can never invent a knob the catalog doesn't have.

    tan-cli#1077: `p["name"]`, `spec["default"]` and `record["id"]` (twice)
    were bare subscripts on a catalog-sourced mapping. The spec shapes are
    resolved once by `_record_parameters`, so `_coerce`/`_check_constraints`
    below keep reading `spec["type"]`/`spec["name"]`/`spec["default"]` bare
    -- see `_coerce`'s docstring."""
    specs = _record_parameters(record, doc=doc, field=field)
    declared = {spec["name"]: spec for spec in specs}
    template_id = _require_key(record, "id", str, doc=doc, field=field)
    params = dict(params or {})
    unknown = sorted(set(params) - set(declared))
    if unknown:
        raise ParameterError(
            f"{template_id}: unknown parameter(s) {unknown}; declared: "
            f"{sorted(declared) or '(none)'}")

    resolved: dict[str, Any] = {}
    for name, spec in declared.items():
        value = _coerce(spec, params.get(name, spec["default"]))
        _check_constraints(template_id, spec, value)
        resolved[name] = value
    return resolved


def _substitutions_for(
    record: dict[str, Any], resolved: dict[str, Any], *, doc: Any, field: str,
) -> dict[str, list[tuple[str, str]]]:
    """dest-relative file -> [(literal_to_replace, new_value_str), ...].

    Reads an opt-in `substitute: {"file": ..., "literal": <optional,
    defaults to str(default)>}` key on a parameter record. No parameter
    the shipped catalog declares today carries this key (the schema
    forbids it -- additionalProperties: false), so this is a no-op for
    every real template; see the module docstring and
    tests/scripts/test_alp_template.py's synthetic-fixture case.

    tan-cli#1077: `sub["file"]` was a bare subscript and `sub` itself was
    never shape-checked, so a `substitute:` block with no `file:` raised
    `KeyError: 'file'` and a non-mapping one raised `AttributeError` from
    `.get("literal", ...)`. Guarded in the ORIGINAL order -- a spec whose
    override equals its default still returns early, untouched, exactly as
    before, so this adds no refusal to a document that used to render.
    """
    per_file: dict[str, list[tuple[str, str]]] = {}
    for index, spec in enumerate(
            _record_parameters(record, doc=doc, field=field)):
        sub = spec.get("substitute")
        if not sub:
            continue
        sub_field = f"{field}.parameters[{index}].substitute"
        value = resolved[spec["name"]]
        if value == spec["default"]:
            continue  # override equals default: nothing to change
        literal = _require_field(sub, dict, doc=doc, field=sub_field).get(
            "literal", str(spec["default"]))
        target = _require_key(sub, "file", str, doc=doc, field=sub_field)
        per_file.setdefault(target, []).append((literal, str(value)))
    return per_file


def _safe_join(root: Path, rel: str, *, what: str) -> Path:
    """Join `rel` onto `root` and require the RESOLVED result stay
    beneath the RESOLVED root (alp-sdk#1126).

    Resolve-then-contain, not pattern-match-for-`..`: `(root / rel)`
    already neutralises nothing on its own -- pathlib lets an absolute
    `rel` replace `root` outright (`Path("a") / "/etc/passwd" ==
    Path("/etc/passwd")`), and a lexical `..` scan misses a `rel` that
    walks back out through a symlink placed inside `root`. Resolving
    both sides and checking containment catches all three forms
    (traversal, absolute paths, symlink escape) with one check.

    tan-cli#1133 (PR #1160 review, found while driving `_rendered_bytes`'s
    symlink-loop shape): `resolve()` itself can fail, and it fails
    DIFFERENTLY per interpreter -- the same family as the tan-cli#1127
    `is_file()` trap, one method over. Against a self-referential symlink,
    measured non-root:

        3.12.3   RuntimeError("Symlink loop from '<path>'")  <- raised
        3.13.15  returns the path unchanged
        3.14.7   returns the path unchanged

    So on 3.12.3 a looped template source file escaped `emit_scaffold` as a
    raw `RuntimeError` -- not even an `OSError`, so no read guard downstream
    could ever have caught it -- while on 3.13/3.14 the same tree fell
    through to the read and was curated there. Both arms are curated now.
    The two messages still DIFFER by interpreter, because the failure
    genuinely happens at different points, and inventing one message for
    both would mean lying about where it broke on one of them; what is
    identical across all three is the CLASS the caller contracts for."""
    try:
        root = root.resolve()
        candidate = (root / rel).resolve()
    except RecursionError:
        # `RecursionError` SUBCLASSES `RuntimeError`, so the clause below
        # would otherwise report a runaway recursion inside `resolve()` as
        # `cannot resolve <what> ...` -- a curated message about the wrong
        # thing, which is its own defect class (PR #1160 review round 2).
        # The measured shape is the plain `RuntimeError("Symlink loop from
        # ...")`; nothing here is a claim about recursion depth.
        raise
    except (OSError, RuntimeError) as exc:
        raise TemplateError(
            f"cannot resolve {what} {rel!r} under {root}: "
            f"{getattr(exc, 'strerror', None) or exc}") from exc
    if not candidate.is_relative_to(root):
        raise PathEscapeError(f"{what} {rel!r} escapes root {root}")
    return candidate


def _rendered_bytes(
    template_id: str,
    record: dict[str, Any],
    files: tuple[str, ...],
    resolved: dict[str, Any],
    base_dir: Path,
    *,
    doc: Any,
    field: str,
) -> list[tuple[str, bytes]]:
    """Read + apply every declared-parameter substitution for `files`
    (a RenderPlan.files list), returning [(relpath, bytes), ...] in the
    same order. Shared by render()'s disk-write loop and
    render_to_envelope()'s in-memory capture -- the same bytes a
    customer gets from `alp_template.py render` are what `--emit
    scaffold` hands back as JSON `contents` (see the module docstring).

    tan-cli#1077: `record["example"]` was bare here and again twice in
    `render_to_envelope`. `example` is `required` in the schema (and
    pattern-constrained to `^examples/<dir>/<dir>$`), so a record without
    it raised `KeyError: 'example'`; a non-string one raised a raw
    `TypeError` from `_safe_join`'s `root / rel`.

    tan-cli#1133 review (PR #1160 MAJOR 1): the read below was the FOURTH
    absent-`try` site and the busiest of the six (4-7 files per scaffold);
    the substitution branch's `.decode` was a FIFTH, LATENT one (no shipped
    catalog can declare a `substitute:`). Per-cell measurement, and the
    schema evidence for that, in `tests/planner/test_emit_scaffold_
    unreadable_metadata.py`."""
    example = _safe_join(
        base_dir, _require_key(record, "example", str, doc=doc, field=field),
        what="template example directory")
    file_subs = _substitutions_for(record, resolved, doc=doc, field=field)
    out: list[tuple[str, bytes]] = []
    for rel in files:
        path = _safe_join(example, rel, what="template source file")
        data = _require_readable_bytes(path, what="template source file")
        subs = file_subs.get(rel)
        if subs:
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise TemplateError(
                    f"{template_id}: {rel} is not valid UTF-8 text, cannot "
                    f"have substitutions applied ({exc})") from exc
            for literal, value in subs:
                if literal not in text:
                    raise ParameterError(
                        f"{template_id}: substitution literal {literal!r} "
                        f"not found in {rel}")
                text = text.replace(literal, value)
            data = text.encode("utf-8")
        out.append((rel, data))
    return out


# ---------------------------------------------------------------------
# --emit scaffold (issue #864): in-memory, SKU-parameterised capture
# ---------------------------------------------------------------------

#: The malformed-document register, bound to THIS module's error class.
#:
#: tan-cli#1084 MOVED the register itself to `tan/core/document_guards.py`
#: (definitions, message shapes, schema-strictness claim and the recorded
#: falsy-value asymmetry all live in that module's docstring). It did not
#: change a single one of them, and it did not change a call site: the names
#: below are the SAME objects this module has used since tan-cli#1073/#1077,
#: so every `_require_field(...)` / `_require_key(...)` / `_catalog_templates
#: (...)` call in this file is byte-identical to what those PRs landed.
#:
#: The move exists because `tan/core/example_catalog.py` is a DELIBERATE
#: second implementation of the catalog read (`tan.planner` binds its SDK root
#: at module-import time, so `tan init`'s SDK-free path cannot import this
#: module at all -- see that file's own docstring) and, after tan-cli#1077,
#: this module refused a malformed catalog with a curated error while that one
#: still crashed raw. One register, two callers, one exception class each:
#: `cli._emit_scaffold` catches `TemplateError` and nothing else, so the class
#: is a constructor argument rather than a fixed type.
#:
#: `_require_constraints` below did NOT move -- it guards `$defs/parameter`'s
#: bounds, which only this module's parameter resolution reads, so it has no
#: second caller to prove it shared. `require_readable_text` (tan-cli#1085)
#: is the file-read half `render_to_envelope`'s example `board.yaml` read
#: used to run by hand -- now the same definition `read_catalog_document`
#: itself calls.
_GUARDS = DocumentGuards(TemplateError)

_SHAPE_NOUN = SHAPE_NOUN
_require_mapping_doc = _GUARDS.require_mapping_doc
_require_field = _GUARDS.require_field
_require_key = _GUARDS.require_key
_require_readable_text = _GUARDS.require_readable_text
_catalog_templates = _GUARDS.catalog_templates
#: tan-cli#1133's four. The first cut of that fix defined the two YAML ones
#: as module-level functions HERE, on the register's stated membership bar
#: ("a shape RUN BY more than one consumer module"): `example_catalog.py`,
#: the register's second consumer, reads JSON only. PR #1160's review asked
#: the question the other way round and it is the better question --
#: `read_catalog_document` is already read + parse + mapping-check for JSON,
#: so keeping the identical composite for YAML in a CONSUMER split one
#: question across two homes for no reason a reader could reconstruct. The
#: import-closure half of the original argument was also simply wrong: seven
#: `tan/core/**` modules defer `import yaml` into a function body for exactly
#: this purpose. Moved, and `template.py` is 71 lines lighter for it -- at
#: the time argued as mattering MORE here than in an ordinary module,
#: because `_module_size_budget_core.MIRRORED_PREFIX`'s comment was read as
#: barring a split of this file. WRONG: that comment describes
#: `PINNED_HASHES` modules (3-way-merged against a moving upstream file,
#: where a shape change would conflict on every future upstream hunk), and
#: this module is `HAND_PORT_SOURCES` instead -- see `template_pins.py`'s
#: module docstring for the correction and the actual split (tan-cli#1142).
_require_readable_bytes = _GUARDS.require_readable_bytes
_parse_yaml_mapping = _GUARDS.require_yaml_mapping_doc
_read_yaml_mapping = _GUARDS.read_yaml_mapping


def _record_parameters(record: Any, *, doc: Any, field: str) -> list[Any]:
    """The record's `parameters:` list, every spec shape-checked once.

    tan-cli#1077: `{p["name"]: p for p in record.get("parameters", [])}`
    (`:229`) and `spec["default"]` (`:239`, `:263`, `:265`) were bare, so
    a spec missing either raised `KeyError`, and a non-list `parameters:`
    raised a raw `TypeError`. `name`/`type`/`description`/`default` are
    all `required` in the schema's `$defs/parameter`; `default` carries no
    declared type there, so only its PRESENCE is required here, and
    `description` is not read by this module at all and is not required
    here either -- guarding a key nobody subscripts would be exactly the
    stricter-than-the-schema this issue rules out.

    `constraints:` is checked here too, via `_require_constraints` --
    tan-cli#1077 review MAJOR 1, whose six measured failures (five raw
    `TypeError`s and one SILENT dropped bound) that helper's own docstring
    records.

    Called by BOTH `_resolve_params` and `_substitutions_for`, which each
    walked the same list bare; the check is idempotent, so the second
    call re-proves what the first did rather than trusting a caller.
    """
    _require_field(record, dict, doc=doc, field=field)
    specs = _require_field(record.get("parameters", []), list,
                           doc=doc, field=f"{field}.parameters")
    for index, spec in enumerate(specs):
        spec_field = f"{field}.parameters[{index}]"
        _require_key(spec, "name", str, doc=doc, field=spec_field)
        _require_key(spec, "type", str, doc=doc, field=spec_field)
        _require_key(spec, "default", doc=doc, field=spec_field)
        _require_constraints(spec, doc=doc, field=spec_field)
    return specs


def _require_constraints(spec: dict[str, Any], *, doc: Any, field: str) -> None:
    """`constraints:` and the three bounds `_check_constraints` reads.

    OPTIONAL in the schema, so an absent block is legal and this returns
    without a word; only a PRESENT one is shape-checked, against
    `$defs/parameter`'s own `constraints` object (`enum` an array,
    `minimum`/`maximum` integers).

    tan-cli#1077 review MAJOR 1: `_check_constraints` read
    `spec.get("constraints") or {}` and then membership-tested and
    subscripted it, INSIDE a function the first sweep table declared
    cleared. Re-driven verbatim on the tree this PR opened with:

        constraints: 3              TypeError: argument of type 'int' is
                                      not iterable                   :307
        constraints: ['enum']       TypeError: list indices must be
                                      integers or slices, not str    :307
        constraints: ['minimum']    same                             :311
        constraints: ['maximum']    same                             :315
        constraints: {enum: 3}      TypeError: argument of type 'int' is
                                      not iterable                   :307
        constraints: {minimum: 'a'} TypeError: '<' not supported between
                                      instances of 'int' and 'str'   :311
        constraints: 'abc'          RENDERS -- every bound DROPPED

    The last row is the serious one, and the same shape as the
    `pins: 'E1M_GPIO_IO4'` character-iteration bug tan-cli#1052 found on
    this file: no exception, wrong behaviour. `"enum" in "abc"` is a
    SUBSTRING test, so every bound evaluated False and an out-of-range
    override was ACCEPTED. The behaviour even depended on the spelling of
    the junk -- `constraints: 'an enum'` DOES contain the substring and
    took the `TypeError` branch instead.
    """
    raw = spec.get("constraints")
    if raw is None:
        return
    cons_field = f"{field}.constraints"
    _require_field(raw, dict, doc=doc, field=cons_field)
    if "enum" in raw:
        _require_field(raw["enum"], list, doc=doc, field=f"{cons_field}.enum")
    for bound in ("minimum", "maximum"):
        if bound in raw:
            _require_field(raw[bound], int, doc=doc,
                           field=f"{cons_field}.{bound}")


#: `template_pins.py` / `template_rewrite.py` (tan-cli#1142 split -- see the
#: module docstring). Deliberately imported here, AFTER the exception classes
#: and the `_GUARDS` block above rather than in this module's top import
#: block: each of those two modules imports `TemplateError` (and
#: `template_pins.py` also imports `_require_field`/`_read_yaml_mapping`) back
#: FROM `.template`, so this is a real import cycle at the module-object
#: level. It resolves because Python does not re-execute an already-importing
#: module -- it looks up the requested names on the (partially built) module
#: object -- and by the time THIS line runs, `TemplateError` and the `_GUARDS`
#: bindings are already set as attributes on `tan.planner.template`. Moving
#: this import above the `_GUARDS` block (or moving that block below this
#: import) breaks the cycle the other way and fails at import time. The names
#: below are also `template.py`'s re-export surface for them: `import
#: tan.planner.template as m; m._load_som_doc` (and ~a dozen siblings) is how
#: `python/tests/` reaches these across the split, so removing one of these
#: bindings without checking its test callers first reproduces exactly the
#: silent-coverage-gap shape tan-cli#279/#778 exist to catch, one module over.
from .template_pins import (
    _alias_for_pin,
    _board_alias_to_entry,
    _board_route_entries,
    _core_board,
    _default_preset_for_sku,
    _derive_core_renames,
    _derive_pin_doc_renames,
    _derive_pin_macro_renames,
    _derive_pin_renames,
    _load_som_doc,
    _pin_pad_and_macro,
    _resolve_pin_target,
    _topology_for_sku,
)
from .template_rewrite import (
    _docs_ref,
    _scaffold_bare_repo_paths,
    _scaffold_cmakelists,
    _scaffold_readme,
    _strip_stale_core_prose,
    _substitute_board_yaml_core,
    _substitute_board_yaml_pin_docs,
    _substitute_board_yaml_pin_macros,
    _substitute_board_yaml_pins,
    _substitute_board_yaml_sku,
    _substitute_cmake_core,
    _substitute_readme_pins,
    _tag_resolves,
)


def _cmake_core_map(
    record: dict[str, Any], example_dir: Path, *, doc: Any, field: str,
) -> dict[str, str]:
    """{CMakeLists.txt relpath (posix, example-root-relative): core_id}
    for every ZEPHYR core the catalog's `cores` field declares (alp-sdk
    #1275 item 1) -- the fix for the single-core assumption that used to
    apply ONE re-derived `--core` rename to every `*CMakeLists.txt` file
    a template happened to own, silently correct only by accident (every
    shipped multi-CMakeLists template today has exactly one supported
    sku, so the rename path was never actually exercised against a
    second file -- see `_derive_core_renames`'s own docstring for the
    same "unreachable but latently wrong" class of bug).

    Reuses `orchestrator._zephyr_app_dir` -- the SAME function `west
    build`'s app-dir argument (and alp-sdk's
    `check_core_cmakelists_mapping.py` gate) resolve `cores.<id>.app`
    through -- rather than re-deriving the "self-contained app dir vs.
    sources-only dir whose CMakeLists.txt lives at the parent" rule a
    second time; a resolver that disagreed would silently re-target the
    wrong file. A non-Zephyr core (`os: yocto`/`off`/`baremetal`) is
    skipped: it either has no `--core` literal to rewrite at all (a
    Yocto CMakeLists.txt never invokes `--emit zephyr-conf`) or, for
    `off`, no `dir` to resolve in the first place.

    tan-cli#1077: `cores` was iterated unguarded and `core["dir"]` /
    `core["id"]` were bare subscripts. `dir` is read only after the
    pre-existing truthiness test, so its guard is a TYPE check, not a new
    requirement (`dir: 3` reached `_safe_join` as a raw TypeError)."""
    out: dict[str, str] = {}
    for index, core in enumerate(_require_field(
            record.get("cores", []), list, doc=doc, field=f"{field}.cores")):
        core_field = f"{field}.cores[{index}]"
        _require_field(core, dict, doc=doc, field=core_field)
        if core.get("os") != "zephyr" or not core.get("dir"):
            continue
        # alp-sdk#1126 containment guard: validate core["dir"] the same way
        # every other catalog-sourced path in this file is validated, BEFORE
        # handing it to `_zephyr_app_dir` (which has no containment check
        # of its own and would otherwise let `../x` walk out of
        # `example_dir` and surface a bare ValueError from `.relative_to`
        # below instead of PathEscapeError).
        core_dir = _safe_join(
            example_dir,
            _require_key(core, "dir", str, doc=doc, field=core_field),
            what="core dir")
        app_dir = _zephyr_app_dir(str(core_dir), example_dir)
        rel = (app_dir / "CMakeLists.txt").relative_to(example_dir).as_posix()
        out[rel] = _require_key(core, "id", str, doc=doc, field=core_field)
    return out


def render_to_envelope(
    template_id: str,
    sku: str,
    params: dict[str, Any] | None = None,
    *,
    catalog_path: Path | None = None,
    base_dir: Path | None = None,
    metadata_root: Path | None = None,
) -> list[tuple[str, str]]:
    """Render `template_id` for `sku` entirely in memory: no dest_dir, no
    disk write. Returns `[(path, contents), ...]` in the same sorted
    order `plan()` computes -- the `{path, contents}[]` shape
    `scripts/alp_project.py --emit scaffold` JSON-encodes (issue #864),
    matching the shape `--emit build-plan`'s `configArtefacts` /
    `sharedArtefacts` already use. `testcase.yaml` is never in this
    envelope (dropped from the catalog's `files.user_owned`: SDK CI
    wiring, not a user's project file).

    `sku` MUST be one of the record's declared `supported.som_skus` --
    SkuNotSupportedError (naming the supported set) otherwise, never a
    silent best-effort render. The rendered `board.yaml`'s `som.sku:`
    and top-level `preset:` are substituted for `sku`'s own default
    board (metadata/e1m_modules/<sku>.yaml `default_board:`). The app
    CORE is re-derived too (`_derive_core_renames`): `board.yaml`'s
    `cores:` key(s) and CMakeLists.txt's `--core` flag are rewritten
    from the canonical example's own SoM core (e.g. `m55_hp`) to
    `sku`'s own Zephyr-buildable core (e.g. `m33_sm` for E1M-V2N101)
    whenever the canonical core isn't already valid for `sku` -- this
    is the fix for issue #864's follow-up: the shallow `som.sku`-only
    swap emitted a board.yaml `--emit zephyr-conf --core m55_hp` can't
    build against for any cross-SoM-family sku. `prj.conf` is a byte-
    identical passthrough when `sku` already matches the example's own
    default (or shares its core ids); `board.yaml`/`src/*.c`/`src/*.h`
    keep their sku/core/pin substitutions scoped to that case but ALWAYS
    get `_scaffold_bare_repo_paths` (alp-sdk#1855: a bare, non-markdown-
    link `docs/*.md`/`examples/<...>` mention in a comment is wrong for
    a copied-out scaffold regardless of which sku was requested, same
    reasoning as the next sentence). CMakeLists.txt and README.md are
    ALSO scaffold-adapted regardless of `sku` (`_scaffold_cmakelists` /
    `_scaffold_readme`) -- their in-tree `ALP_SDK_ROOT` guess and SDK-
    tree-relative links/paths are wrong for a copied-out scaffold no
    matter which sku was requested.
    """
    catalog = catalog_path or CATALOG
    doc = load_catalog(catalog)
    record = find_template(doc, template_id, path=catalog)
    # tan-cli#1077: everything below reads the CATALOG record, the fourth
    # document of the malformed-document family and the only one decoded
    # by `json.loads` + BARE SUBSCRIPT -- so its failure mode is
    # `KeyError`, not the shape failure the three YAML documents produce.
    # `record["supported"]["som_skus"]` (this line) and `record["example"]`
    # (twice below) were bare double/single subscripts; the rest are
    # guarded inside the helpers this function calls, each of which now
    # takes the catalog path and this record's label so its curated
    # message names THE FILE, THE FIELD and THE TYPE like the register at
    # `tan/model/targets.py:312-323`. `find_template` has already required
    # `record["id"] == template_id`, so labelling by id cannot itself lie.
    rec_field = f"templates[{template_id!r}]"
    supported = [
        _require_field(entry, str, doc=catalog,
                       field=f"{rec_field}.supported.som_skus[{index}]")
        for index, entry in enumerate(_require_key(
            _require_key(record, "supported", dict,
                         doc=catalog, field=rec_field),
            "som_skus", list, doc=catalog, field=f"{rec_field}.supported"))]
    if sku not in supported:
        raise SkuNotSupportedError(
            f"{template_id}: sku {sku!r} is not supported "
            f"(supported: {sorted(supported)})")

    files = _ordered_files(record, doc=catalog, field=rec_field)
    resolved = _resolve_params(record, params, doc=catalog, field=rec_field)
    base = base_dir or REPO
    metadata_root = metadata_root or METADATA_ROOT
    preset = _default_preset_for_sku(sku, metadata_root)

    example_rel = _require_key(record, "example", str,
                               doc=catalog, field=rec_field)
    example_dir = _safe_join(base, example_rel, what="template example directory")
    board_yaml_path = example_dir / "board.yaml"
    # The other half of the ABSENT-document pair (tan-cli#1077 review) --
    # see `load_catalog`'s docstring for the five-site measurement. The
    # catalog's `example:` is drift-checked by alp-sdk's own
    # `check_template_catalog.py`, but that gate runs on the SDK, not here,
    # so a hand-edited catalog pointing at a directory with no `board.yaml`
    # reached the user as a raw `FileNotFoundError`.
    #
    # tan-cli#1116 fixed a narrowed `except OSError` here (a non-UTF-8
    # board.yaml escaped raw); tan-cli#1085 folded the fixed body into
    # `DocumentGuards.require_readable_text`, the same definition
    # `read_catalog_document` calls for the catalog's own read. Message
    # text unchanged, one definition instead of two.
    board_yaml_text = _require_readable_text(
        board_yaml_path, what="template example board.yaml")
    # tan-cli#1052: the fifth sibling of the same malformed-YAML family
    # tan-cli#1025 -> #1034 -> #1037/#1048 swept through the SoM preset
    # and the board metadata. THIS document is the catalog template's
    # own `examples/<...>/board.yaml`, and every one of the four reads
    # below was bare -- all four reachable from one command, `--emit
    # scaffold --template peripheral --sku E1M-V2N101`. Measured on the
    # pre-fix tree:
    #
    #     <doc> = "- one\n- two\n"  -> AttributeError: 'list' object has
    #                                    no attribute 'get'   (:1542)
    #     cores: 3                   -> AttributeError: 'int' object has
    #                                    no attribute 'keys'  (:1542)
    #     som: 3                     -> AttributeError: 'int' object has
    #                                    no attribute 'get'   (:1543)
    #     pins: 3                    -> TypeError: 'int' object is not
    #                                    iterable              (:1551)
    #
    # Guarded through the same `_require_mapping_doc`/`_require_field`
    # register the other two documents now use, so the next field added
    # here inherits the rule instead of starting a sixth round. NOT
    # stricter than `metadata/schemas/board.schema.json`: `pins:` is
    # `type: array` with no `minItems`, so `pins: []` (and an absent or
    # `null` `pins:`) still passes, as does `preset:` being absent -- it
    # is optional, and only consulted when `pins:` is non-empty.
    #
    # Each read normalises `None` ONLY -- `[] if raw is None else raw`,
    # never the `raw or []` the other two documents use. `or` collapses
    # every falsy value, so a PRESENT but illegal scalar would be
    # emptied instead of refused; measured on the first cut of this fix
    # (tan-cli#1052 review), `pins: 0` / `pins: false` / `pins: ''` /
    # `cores: 0` all rendered while `pins: 3` and `pins: true` raised.
    # That degrades to empty rather than to garbage, but it is exactly
    # the unpinned residual sibling this PR exists to stop leaving
    # behind, so the falsy scalars are refused too.
    #
    # tan-cli#1133: the PARSE is guarded too. tan-cli#1116 fixed the READ
    # here and left `yaml.safe_load` bare one line down, so a template
    # example `board.yaml` that decoded fine but did not parse still raised
    # a raw `yaml.parser.ParserError` through `emit_scaffold` -- measured on
    # 3.12.3, 3.13.15 and 3.14.7 alike. Same `_parse_yaml_mapping` the two
    # `metadata/**` documents above now use.
    example_doc = _parse_yaml_mapping(
        board_yaml_text, path=board_yaml_path,
        what="template example board.yaml")
    raw_cores = example_doc.get("cores")
    original_core_ids = list(_require_field(
        {} if raw_cores is None else raw_cores, dict,
        doc=board_yaml_path, field="cores").keys())
    raw_som = example_doc.get("som")
    example_sku = _require_field(
        {} if raw_som is None else raw_som, dict,
        doc=board_yaml_path, field="som").get("sku", "")
    core_renames = _derive_core_renames(original_core_ids, sku, metadata_root)
    # `pins:` re-derivation (issue #876): each entry is either a bare
    # pad string or a `{e1m, macro?, doc?}` mapping (same shape
    # alp_orchestrate.loader's pins cross-check accepts); only the
    # example's OWN preset (not an inline board def -- no catalog
    # template ships one today) has a metadata/boards/<preset>.yaml to
    # re-derive against.
    raw_pins = example_doc.get("pins")
    original_pins = list(_require_field(
        [] if raw_pins is None else raw_pins, list,
        doc=board_yaml_path, field="pins"))
    source_preset = example_doc.get("preset")
    if source_preset is not None:
        # Same shape as `default_board:` one document over: a `preset:`
        # that isn't a string reaches `metadata/boards/<preset>.yaml`
        # as a path component and surfaces a curated-but-untrue message
        # (measured pre-fix: `no metadata/boards/['a'].yaml for board
        # ['a']`, which reads like a missing file rather than a
        # malformed field). `None` is legal -- an inline board def, or
        # a template with no `pins:` to re-derive.
        _require_field(source_preset, str,
                       doc=board_yaml_path, field="preset")
    pin_renames = (
        _derive_pin_renames(original_pins, sku, source_preset, metadata_root)
        if original_pins else {}
    )
    pin_macro_renames = (
        _derive_pin_macro_renames(original_pins, sku, source_preset, metadata_root)
        if original_pins else {}
    )
    pin_doc_renames = (
        _derive_pin_doc_renames(original_pins, sku, source_preset, metadata_root)
        if original_pins else {}
    )
    # README board-target rewrite (MAJOR C) still keys off a single
    # "primary" app core -- the first m-prefixed core in
    # original_core_ids, matching board.yaml's own declaration order
    # (the same tie-break `_derive_core_renames`'s MAJOR D picks). One
    # board id in the README prose is all MAJOR C ever rewrote, single-
    # core template or not -- unaffected by item 1's per-CMakeLists fix
    # below, which is a SEPARATE map over every Zephyr core, not this
    # scalar.
    app_core_old = next((c for c in original_core_ids if c.startswith("m")), None)
    app_core_sub = (
        (app_core_old, core_renames[app_core_old])
        if core_renames and app_core_old in core_renames else None
    )
    source_board = _core_board(example_sku, app_core_old, metadata_root)
    target_board = _core_board(
        sku, app_core_sub[1] if app_core_sub else app_core_old, metadata_root)
    docs_ref = _docs_ref(base)
    # CMakeLists.txt per-core map (alp-sdk#1275 item 1): each Zephyr core
    # the catalog's `cores` field declares gets its OWN `--core` rename
    # applied to its OWN CMakeLists.txt -- fixes the single-core
    # assumption above (app_core_sub) blindly re-applying ONE rename to
    # every `*CMakeLists.txt` file a multi-core template owns. See
    # `_cmake_core_map`'s docstring.
    cmake_core_for = _cmake_core_map(
        record, example_dir, doc=catalog, field=rec_field)

    out: list[tuple[str, str]] = []
    for rel, data in _rendered_bytes(
            template_id, record, files, resolved, base,
            doc=catalog, field=rec_field):
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            # render() copies any file as raw bytes; the JSON envelope
            # cannot -- a future binary user_owned asset must fail
            # cleanly here, not escape _run_scaffold_emit's `except
            # TemplateError` as a raw traceback.
            raise TemplateError(
                f"{template_id}: {rel} is not valid UTF-8 text, cannot "
                f"be JSON-encoded for --emit scaffold ({exc})") from exc
        if rel == "board.yaml":
            text = _substitute_board_yaml_sku(text, sku, preset)
            for old, new in (core_renames or {}).items():
                text = _substitute_board_yaml_core(text, old, new)
            if pin_renames:
                text = _substitute_board_yaml_pins(text, pin_renames, original_pins)
            if pin_macro_renames:
                text = _substitute_board_yaml_pin_macros(text, pin_macro_renames)
            if pin_doc_renames:
                text = _substitute_board_yaml_pin_docs(text, pin_doc_renames)
            # A renamed pad can also survive as a stale mention in
            # unrelated prose (issue #876 review MINOR 4) -- e.g.
            # gpio-button-led's `preset:` header comment names the
            # E1M-EVK pad in a continuation line the `preset:`
            # substitution above never touches. Same treatment
            # `_substitute_board_yaml_core` already gives a renamed
            # core id (`_strip_stale_core_prose`).
            for old in (pin_renames or {}):
                text = _strip_stale_core_prose(text, old)
            # alp-sdk#1855: board.yaml comments carry the same kind of
            # bare alp-sdk-tree-only cross-reference README.md does
            # (see `_BARE_REPO_PATH_RE`), but never went through any
            # rewrite -- only README.md did.
            text = _scaffold_bare_repo_paths(text, docs_ref)
        elif rel.endswith("CMakeLists.txt"):
            this_core = cmake_core_for.get(rel)
            if this_core and core_renames and this_core in core_renames:
                text = _substitute_cmake_core(
                    text, this_core, core_renames[this_core])
            text = _scaffold_cmakelists(text)
        elif rel == "README.md":
            text = _scaffold_readme(
                text, example_rel, docs_ref,
                example_sku=example_sku, sku=sku,
                source_board=source_board, target_board=target_board,
                pin_renames=pin_renames)
        elif rel.endswith((".c", ".h")):
            # Same alp-sdk#1855 gap as board.yaml above -- a source
            # comment (e.g. cold-chain-monitor's src/main.c "(see
            # examples/ai/cold-chain-monitor/models/README.md)") is
            # never touched by any existing scaffold-adaptation pass.
            text = _scaffold_bare_repo_paths(text, docs_ref)
        out.append((rel, text))
    return out


def emit_scaffold(
    template_id: str,
    sku: str,
    params: dict[str, Any] | None = None,
    *,
    catalog_path: Path | None = None,
    base_dir: Path | None = None,
    metadata_root: Path | None = None,
) -> str:
    """`--emit scaffold`'s STDOUT, byte for byte.

    RELOCATED from `alp_project._run_scaffold_emit`'s two serialising lines. The
    envelope is `[{path, contents}, ...]` -- `render_to_envelope`'s ordered
    pairs, `json.dumps(..., indent=2)`, one trailing newline -- and alp-sdk's
    `scripts/check_emit_snapshots.py` diffs exactly these bytes against a
    committed golden, so the shape is the contract, not a presentation choice.
    """
    envelope = render_to_envelope(
        template_id, sku, params, catalog_path=catalog_path, base_dir=base_dir,
        metadata_root=metadata_root)
    return json.dumps(
        [{"path": p, "contents": c} for p, c in envelope], indent=2) + "\n"
