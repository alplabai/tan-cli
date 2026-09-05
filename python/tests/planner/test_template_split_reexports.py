# SPDX-License-Identifier: Apache-2.0
"""tan-cli#1142 review: the `template.py` -> `template_pins.py` +
`template_rewrite.py` split shipped with a live `NameError` and zero
coverage that would have caught it.

THE BLOCKER, reproduced directly. `template_rewrite.py`'s
`_substitute_board_yaml_pins` calls `_pin_pad_and_macro`, which the split
moved to `template_pins.py`; `template_rewrite.py` imported neither it nor
`pathlib.Path`/`typing.Any` (both used only in annotations, masked at import
time by `from __future__ import annotations`). `render_to_envelope`'s
pin-rename path (`template.py:1042-1043`,
`_substitute_board_yaml_pins(text, pin_renames, original_pins)`) reaches it
directly, so any `--emit scaffold` sku swap that renames a pin raised a raw
`NameError` instead of returning the rewritten `board.yaml`. Measured on the
same call, both trees: dev (pre-split) returns
`'pins:\\n  - e1m: E1M_GPIO_IO7\\n'`; the unfixed split raised `NameError:
name '_pin_pad_and_macro' is not defined`.

WHY NOTHING CAUGHT IT. Every existing template test
(`tests/planner/test_load_som_doc_malformed_preset.py` and its siblings)
`pytest.mark.skipif(SDK is None, ...)`s on a REAL bound alp-sdk checkout,
which the default `gates` job (`python -m pytest tests -q`, no
`ALP_SDK_ROOT`) never provides. Proof (the reviewer's own measurement):
deleting `_load_som_doc` from `template.py`'s re-export list -- the exact
binding the split's own commit message calls load-bearing -- left
`tests/planner+gates+core` byte-identical, 66 failed either way.

This module binds a SYNTHETIC (not real) SDK root via the shared
`bind_planner_sdk_root` (`tests/planner/_baremetal_support.py`, reused
rather than redefined -- see
`tests/gates/test_shared_test_helpers_have_one_definition.py`, which fails
the PR if a second definition of it appears anywhere under
`python/tests/**`), the same binder the `os: baremetal` coverage modules use
to run unconditionally in the default `gates` job instead of being skipped
the way every real-SDK-gated planner test is. That is the whole point: a
test that only ran under `ALP_SDK_ROOT` would reproduce the exact same blind
spot that let the NameError and the dropped re-export both ship green.
"""
from __future__ import annotations

import typing

# `bound_sdk_root` is an autouse pytest fixture, imported for its side effect
# -- the same idiom the `os: baremetal` coverage modules use. It binds the
# real checkout first when one is available, falling back to reusing
# whatever is already bound, or a throwaway `tmp_path` when nothing is bound
# at all -- see `bind_planner_sdk_root`'s own docstring.
from tests.planner._baremetal_support import bound_sdk_root  # noqa: F401

#: `template.py`'s own `from .template_pins import (...)` block
#: (template.py:735-749), verbatim.
_PIN_NAMES = (
    "_alias_for_pin",
    "_board_alias_to_entry",
    "_board_route_entries",
    "_core_board",
    "_default_preset_for_sku",
    "_derive_core_renames",
    "_derive_pin_doc_renames",
    "_derive_pin_macro_renames",
    "_derive_pin_renames",
    "_load_som_doc",
    "_pin_pad_and_macro",
    "_resolve_pin_target",
    "_topology_for_sku",
)

#: `template.py`'s own `from .template_rewrite import (...)` block
#: (template.py:750-763), verbatim.
_REWRITE_NAMES = (
    "_docs_ref",
    "_scaffold_bare_repo_paths",
    "_scaffold_cmakelists",
    "_scaffold_readme",
    "_strip_stale_core_prose",
    "_substitute_board_yaml_core",
    "_substitute_board_yaml_pin_docs",
    "_substitute_board_yaml_pin_macros",
    "_substitute_board_yaml_pins",
    "_substitute_board_yaml_sku",
    "_substitute_cmake_core",
    "_substitute_readme_pins",
    "_tag_resolves",
)


def test_template_reexports_every_split_binding_by_identity():
    """Each re-exported name on `tan.planner.template` must be the SAME
    function object `template_pins.py`/`template_rewrite.py` define -- not
    merely a name that happens to resolve to something. Dropping one of
    these lines (the reviewer's reproduction: removing `_load_som_doc`)
    must fail HERE, unconditionally -- not only in a real-SDK-gated module
    that skips in the environment this gate actually runs in."""
    import tan.planner.template as template
    import tan.planner.template_pins as template_pins
    import tan.planner.template_rewrite as template_rewrite

    for name in _PIN_NAMES:
        assert hasattr(template, name), (
            f"tan.planner.template no longer re-exports {name} from "
            f"template_pins.py")
        assert getattr(template, name) is getattr(template_pins, name)

    for name in _REWRITE_NAMES:
        assert hasattr(template, name), (
            f"tan.planner.template no longer re-exports {name} from "
            f"template_rewrite.py")
        assert getattr(template, name) is getattr(template_rewrite, name)


def test_substitute_board_yaml_pins_does_not_raise_nameerror():
    """The blocker, reproduced at its own call site
    (`template_rewrite.py:157`), through `template_rewrite.py` directly --
    not through `template.py`'s re-export, so a future re-export drop can't
    mask this one going back to raising."""
    import tan.planner.template_rewrite as template_rewrite

    original_pins = [{"e1m": "E1M_GPIO_IO7"}]
    out = template_rewrite._substitute_board_yaml_pins(
        "pins:\n  - e1m: E1M_GPIO_IO7\n",
        {"E1M_GPIO_IO7": "E1M_X_GPIO_IO28"},
        original_pins,
    )
    assert out == "pins:\n  - e1m: E1M_X_GPIO_IO28\n"


def test_template_reexport_of_substitute_board_yaml_pins_agrees():
    """Same call through `template.py`'s own re-exported name -- the exact
    spelling `render_to_envelope` (template.py:1043) calls."""
    import tan.planner.template as template

    original_pins = [{"e1m": "E1M_GPIO_IO7"}]
    out = template._substitute_board_yaml_pins(
        "pins:\n  - e1m: E1M_GPIO_IO7\n",
        {"E1M_GPIO_IO7": "E1M_X_GPIO_IO28"},
        original_pins,
    )
    assert out == "pins:\n  - e1m: E1M_X_GPIO_IO28\n"


def test_template_rewrite_type_hints_resolve():
    """`pathlib.Path`/`typing.Any` were on the pre-split `template.py` and
    did not come across to `template_rewrite.py` -- masked by `from
    __future__ import annotations` until a hint is actually resolved.
    Measured pre-fix: `typing.get_type_hints(_substitute_board_yaml_pins)`
    raised `NameError: name 'Any' is not defined`;
    `_tag_resolves`/`_docs_ref` raised `NameError: name 'Path' is not
    defined`."""
    import tan.planner.template_rewrite as template_rewrite

    for name in ("_substitute_board_yaml_pins", "_tag_resolves", "_docs_ref"):
        typing.get_type_hints(getattr(template_rewrite, name))
