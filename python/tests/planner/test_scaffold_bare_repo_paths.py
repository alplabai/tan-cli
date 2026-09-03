# SPDX-License-Identifier: Apache-2.0
r"""tan-cli#1118, the hand-port half of the alp-sdk `0914da38` -> `ff27f179`
planner re-sync: alp-sdk#1855 (`5c33ef04`, "fix(scaffold): emit a buildable
multicore-rpmsg tree, and rewrite bare alp-sdk-tree paths in scaffolded
comments", alp-sdk#1906), ported by hand into `tan/planner/template.py`.

`scripts/alp_template.py` is a HAND-PORT, not a mirror -- tan's counterpart
is restructured (guarded document reads, `_require_key`/`_require_field`
threading, `example_rel`), so `planner_resync.py` never proposes a merge for
it and the freshness gate stays red until a person ports the delta. This
file is the proof that the delta was PORTED rather than the pin merely
moved: every case below reds against the unported `template.py` on its own
assertion, not on an import error (measured -- see the three groups).

THE DELTA, in three behavioural parts:

1. `_scaffold_bare_repo_paths` / `_BARE_REPO_PATH_RE` -- new. A bare,
   non-markdown-link `docs/*.md` or `examples/<category>/<name>[/<subpath>]`
   mention in PROSE is rewritten to the same absolute GitHub URL
   `_scaffold_readme`'s `_fix_link` already gives a `](../docs/x.md)` link.
   Narrow on purpose: `scripts/`/`metadata/` mentions are left alone.

2. `_scaffold_readme`'s bare-own-path substitution now captures a trailing
   `/<subpath>`. Pre-port, `(?<!\S)<example_path>(?!\S)` could not match
   `examples/multicore/mproc-mailbox/peer` at all (the `(?!\S)` boundary
   fails on the `/`), so a multi-slice README's per-core `west build`
   argument survived verbatim, naming a path that exists only inside the
   alp-sdk tree. It now becomes `./peer`.

3. `render_to_envelope` wiring -- `board.yaml` gets `_scaffold_bare_repo_paths`
   at the end of its arm, and a NEW `.c`/`.h` arm gets it too. Both run
   ALWAYS, not only on a cross-sku swap: a bare alp-sdk-tree cross-reference
   in a comment is wrong for a copied-out scaffold whichever sku was asked
   for. That "always" is the load-bearing half and is asserted separately
   (`sku == example_sku`, the byte-identical-passthrough case that used to
   be documented as such for `board.yaml`/`src/main.c` and no longer is).

NOT THE ONLY PROOF, and deliberately not the strongest one.
`tests/parity/test_planner_emit_parity.py::test_the_scaffold_mode_agrees_on_
stdout_through_argv` byte-compares tan's whole `--emit scaffold` stdout
against the bound SDK's own, on the real catalog: with `ALP_SDK_ROOT` at a
real `ff27f179` checkout it is 5/5 PASS with this port and 3/5 FAIL without
it (`peripheral`, `sensor`, `edge-ai`). That settles byte-equivalence. THIS
file exists for what byte-parity cannot give: it runs against a synthetic
catalog rather than whichever checkout happens to be bound, it names each of
the three behavioural parts separately so a future regression says WHICH one
broke, and it covers three things the five parity cases never reach -- the
`.h` arm (no vendored template ships one), `_scaffold_readme`'s subpath
capture in isolation, and the pass's measured non-idempotence together with
the exactly-once-per-arm invariant that makes it safe.

Importing `tan.planner.template` needs SOME bound alp-sdk root (its package
`__init__` reads `metadata/registries/*` at import time) -- same requirement
as `test_find_template_by_cores.py`, the previous hand-port of this same
upstream file, whose gating idiom this reuses verbatim. Every case here
passes a synthetic `catalog_path`/`base_dir`/`metadata_root` and never reads
the bound checkout's own content, so what the checkout IS is inert; that it
EXISTS is not.
"""
from __future__ import annotations

import json

import pytest

# `_bound_sdk` is a pytest fixture, imported for its side effect -- the
# same idiom `_baremetal_support`'s consumers use for `bound_sdk_root`.
from tests.planner._bound_sdk_fixture import SDK, _bound_sdk  # noqa: F401

pytestmark = pytest.mark.skipif(
    SDK is None,
    reason="ALP_SDK_ROOT is not set (or does not point at a real alp-sdk "
           "checkout) -- importing tan.planner.template requires SOME bound "
           "root (tan/planner_root.py). A SKIP about the missing root, not a "
           "pass.",
)

#: `_docs_ref` degrades to `main` for a synthetic `base_dir` with no
#: `metadata/sdk_version.yaml` (that function's own documented contract), so
#: every expected URL below is pinned at this ref rather than recomputed.
_REF = "main"
_BLOB = f"https://github.com/alplabai/alp-sdk/blob/{_REF}"
_TREE = f"https://github.com/alplabai/alp-sdk/tree/{_REF}"


def _tmpl():
    """Imported inside the call so the module is not imported before
    `bind_sdk_root` has run (collection order)."""
    import tan.planner.template as m
    return m


# ---------------------------------------------------------------------
# Part 1 -- `_scaffold_bare_repo_paths` itself.
# ---------------------------------------------------------------------

@pytest.mark.parametrize(
    ("prose", "expected"),
    [
        # A directory (no dot in the last segment) is a `tree` link; a
        # file is a `blob` link -- the same split `_fix_link` makes.
        ("see examples/v2n/v2n-temp-sensor for the wiring",
         f"see {_TREE}/examples/v2n/v2n-temp-sensor for the wiring"),
        ("(see examples/ai/cold-chain-monitor/models/README.md)",
         f"(see {_BLOB}/examples/ai/cold-chain-monitor/models/README.md)"),
        ("read docs/e1m-pinout.md first",
         f"read {_BLOB}/docs/e1m-pinout.md first"),
        ("nested: docs/adr/0020-tan-executor.md",
         f"nested: {_BLOB}/docs/adr/0020-tan-executor.md"),
    ],
)
def test_a_bare_tree_path_in_prose_becomes_an_absolute_github_url(
        prose, expected):
    """The whole point of the new pass: these mentions carry no
    `[...](...)` around them, so `_RELATIVE_LINK_RE` never saw them and
    they survived a scaffold verbatim -- naming a path that resolves only
    inside an alp-sdk checkout."""
    assert _tmpl()._scaffold_bare_repo_paths(prose, _REF) == expected


@pytest.mark.parametrize(
    "prose",
    [
        # Deliberately out of scope upstream: every `scripts/`/`metadata/`
        # mention in the catalog today is either already
        # `${ALP_SDK_ROOT}`-qualified or descriptive prose.
        "run scripts/alp_project.py --emit build-plan",
        "metadata/e1m_modules/E1M-V2N101.yaml declares it",
        # A single-segment `examples/` mention is not a template path.
        "the examples/ tree",
        # No repo path at all.
        "nothing to rewrite here",
    ],
)
def test_an_out_of_scope_mention_is_left_alone(prose):
    """Narrow on purpose -- the pass must be a no-op for everything the
    upstream comment lists as deliberately excluded, or it would start
    rewriting prose that is already correct for a copied-out scaffold."""
    assert _tmpl()._scaffold_bare_repo_paths(prose, _REF) == prose


def test_the_pass_is_not_idempotent_which_is_why_it_runs_once_per_file():
    """MEASURED against the upstream `ff27f179` implementation, not
    assumed: the emitted URL CONTAINS `docs/e1m-pinout.md`, so a second
    pass over the pass's own output rewrites it again and produces
    `.../blob/main/https://github.com/alplabai/alp-sdk/blob/main/docs/
    e1m-pinout.md`. A first cut of this file asserted idempotence and
    red -- pinned here as the property it actually has, because it is
    what makes "exactly one application per file" load-bearing rather
    than incidental (`test_a_board_yaml_is_rewritten_exactly_once`
    below, and `README.md` deliberately having no `_scaffold_bare_repo_
    paths` arm at all -- `_fix_link` already ran there).

    The port must MATCH upstream, not improve on it: adding an
    already-a-URL guard here would be a tan-side divergence in a file
    whose whole audit is a byte-comparison against alp-sdk."""
    m = _tmpl()
    once = m._scaffold_bare_repo_paths("see docs/e1m-pinout.md", _REF)
    twice = m._scaffold_bare_repo_paths(once, _REF)
    assert twice != once
    assert twice == f"see {_BLOB}/{_BLOB}/docs/e1m-pinout.md"


# ---------------------------------------------------------------------
# Part 2 -- `_scaffold_readme` keeps a trailing `/<subpath>`.
# ---------------------------------------------------------------------

_EXAMPLE_PATH = "examples/multicore/mproc-mailbox"


def test_a_bare_subpath_of_the_examples_own_dir_becomes_a_relative_one():
    """alp-sdk#1855's multi-slice case, measured: pre-port the regex was
    `(?<!\\S)<example_path>(?!\\S)`, whose `(?!\\S)` boundary fails on the
    `/` of `/peer`, so NOTHING matched and the HE-side peer core's
    `west build` argument shipped as `examples/multicore/mproc-mailbox/peer`
    -- a path that exists only inside the alp-sdk tree."""
    out = _tmpl()._scaffold_readme(
        f"west build -b <board> {_EXAMPLE_PATH}/peer\n", _EXAMPLE_PATH, _REF)
    assert out == "west build -b <board> ./peer\n"


def test_the_examples_own_path_with_no_subpath_still_becomes_a_dot():
    """The behaviour the capture group must not regress: `(/\\S+)?` is
    OPTIONAL, so the plain mention still collapses to `.` exactly as
    before -- `m.group(1)` is `None` there, not the empty string."""
    out = _tmpl()._scaffold_readme(
        f"west build -b <board> {_EXAMPLE_PATH}\n", _EXAMPLE_PATH, _REF)
    assert out == "west build -b <board> .\n"


# ---------------------------------------------------------------------
# Part 3 -- the `render_to_envelope` wiring.
# ---------------------------------------------------------------------

_TEMPLATE = "fake1118"
_SKU = "E1M-FAKE1118"
_OTHER_SKU = "E1M-FAKE1118B"
_EXAMPLE = "examples/peripheral-io/fake1118"

_BOARD_YAML = """\
# Cross-reference: see examples/v2n/v2n-temp-sensor for the sensor wiring,
# and docs/e1m-pinout.md for the pad map.
som:
  sku: E1M-FAKE1118

preset: fake-board

cores:
  m33_sm:
    app: ./src
"""

_MAIN_C = """\
/* Model conversion is documented in
 * examples/ai/cold-chain-monitor/models/README.md; the pad map is in
 * docs/e1m-pinout.md. */
int main(void) { return 0; }
"""

_HEADER_H = "/* see docs/e1m-pinout.md */\n"


def _tree(tmp_path):
    """A synthetic (catalog, base_dir, metadata_root) triple carrying one
    `board.yaml`, one `src/main.c` and one `src/app.h`, each with a bare
    alp-sdk-tree cross-reference in a COMMENT. Same shape
    `test_render_to_envelope_malformed_example_board.py::_tree` builds; no
    `metadata/sdk_version.yaml`, so `_docs_ref` gives `_REF`."""
    base = tmp_path / "sdk"
    example = base / _EXAMPLE
    (example / "src").mkdir(parents=True)
    (example / "board.yaml").write_text(_BOARD_YAML, encoding="utf-8")
    (example / "src" / "main.c").write_text(_MAIN_C, encoding="utf-8")
    (example / "src" / "app.h").write_text(_HEADER_H, encoding="utf-8")

    catalog = tmp_path / "catalog-v1.json"
    catalog.write_text(json.dumps({"templates": [{
        "id": _TEMPLATE,
        "example": _EXAMPLE,
        "supported": {"som_skus": [_SKU, _OTHER_SKU]},
        "files": {"user_owned": ["board.yaml", "src/main.c", "src/app.h"]},
        "cores": [],
    }]}), encoding="utf-8")

    metadata = tmp_path / "metadata"
    (metadata / "e1m_modules").mkdir(parents=True)
    for sku in (_SKU, _OTHER_SKU):
        (metadata / "e1m_modules" / f"{sku}.yaml").write_text(
            "default_board: FAKE-BOARD\n"
            "topology:\n"
            "  m33_sm:\n"
            "    board: fake/soc/m33\n",
            encoding="utf-8")
    return catalog, base, metadata


def _render(tmp_path, sku=_SKU):
    catalog, base, metadata = _tree(tmp_path)
    return dict(_tmpl().render_to_envelope(
        _TEMPLATE, sku,
        catalog_path=catalog, base_dir=base, metadata_root=metadata))


def test_a_board_yaml_comments_bare_tree_paths_are_rewritten(tmp_path):
    """The `board.yaml` arm's new tail. Pre-port this arm ended at
    `_strip_stale_core_prose` and no pass in the module ever looked at a
    board.yaml COMMENT for an alp-sdk-tree path -- only README.md did."""
    out = _render(tmp_path)["board.yaml"]
    assert f"{_TREE}/examples/v2n/v2n-temp-sensor" in out
    assert f"{_BLOB}/docs/e1m-pinout.md" in out
    assert "see examples/v2n/v2n-temp-sensor" not in out


@pytest.mark.parametrize("rel", ["src/main.c", "src/app.h"])
def test_a_c_or_h_sources_bare_tree_paths_are_rewritten(tmp_path, rel):
    """The NEW `.c`/`.h` arm. Pre-port there was no arm for these at all
    -- a source file fell straight through to `out.append((rel, text))`
    unmodified, so `examples/ai/cold-chain-monitor/models/README.md` in a
    `src/main.c` comment shipped verbatim in the scaffold."""
    out = _render(tmp_path)[rel]
    assert f"{_BLOB}/docs/e1m-pinout.md" in out
    assert "in\n * docs/e1m-pinout.md" not in out


def test_the_c_source_keeps_its_deeper_example_subpath(tmp_path):
    """The `[/<subpath>]` half of `_BARE_REPO_PATH_RE`, on the arm that
    exercises it for real: the reference is four segments deep and must
    survive whole, as a `blob` link (its last segment has a dot), not be
    truncated to the two-segment example directory."""
    out = _render(tmp_path)["src/main.c"]
    assert (f"{_BLOB}/examples/ai/cold-chain-monitor/models/README.md"
            in out)


def test_the_rewrite_runs_even_when_the_sku_is_the_examples_own(tmp_path):
    """The load-bearing "ALWAYS" in `render_to_envelope`'s re-worded
    docstring. `board.yaml`/`src/main.c` used to be documented as a
    byte-identical passthrough when `sku` already matches the example's
    own default -- and were one. A bare alp-sdk-tree cross-reference is
    wrong for a copied-out scaffold no matter which sku was requested, so
    the new pass is OUTSIDE that conditional. Asserted with
    `sku == example_sku` (`E1M-FAKE1118`, the passthrough case) rather
    than folded into the cases above, which happen to use the same sku:
    this one pins the CLAIM, so a future refactor that re-scopes the pass
    under the cross-sku conditional reds here."""
    out = _render(tmp_path, sku=_SKU)
    assert "som:\n  sku: E1M-FAKE1118\n" in out["board.yaml"]
    assert f"{_TREE}/examples/v2n/v2n-temp-sensor" in out["board.yaml"]
    assert f"{_BLOB}/docs/e1m-pinout.md" in out["src/main.c"]


def test_a_board_yaml_is_rewritten_exactly_once(tmp_path):
    """`_scaffold_bare_repo_paths` is not idempotent (see the Part-1 case
    of that name), so "the arm calls it once" is a real invariant, not a
    tautology: a second call -- a duplicated line in the `board.yaml` arm,
    or a future arm that overlaps this one -- would emit a doubled
    `.../blob/main/https://github.com/...` URL into a customer's scaffold.
    Nothing else in the module would notice."""
    out = _render(tmp_path)["board.yaml"]
    assert out.count("https://github.com/alplabai/alp-sdk/") == 2
    assert f"{_BLOB}/https://" not in out
    assert f"{_TREE}/https://" not in out
