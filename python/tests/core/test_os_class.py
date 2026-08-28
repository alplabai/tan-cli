# SPDX-License-Identifier: Apache-2.0
"""`tan.core.os_class` -- the shared Cortex-A/Cortex-M -> OS convention
(tan-cli#870), free of `tan.planner`'s bind-first import requirement.

No test file for this module existed before tan-cli#957: both callers
(`presets_cmd.core_type_lookup`/`allowed_os_lookup` and
`topology._default_os_from_core_type`/`_allowed_os_for_core`) had their own
guard against a non-string `core_type` reaching here, so this module's own
`isinstance` backstop (added alongside those two call-site guards) was
otherwise reachable only through a caller this suite doesn't know about yet
-- exactly the "for every caller, present or future" case its own docstring
names.
"""
from __future__ import annotations

import pytest

from tan.core.os_class import (
    CLASS_RUNTIMES,
    allowed_os_for_core,
    cross_class_os,
    default_os_from_core_type,
)


class TestDefaultOsFromCoreType:
    def test_cortex_a_is_yocto(self):
        assert default_os_from_core_type("cortex-a55") == "yocto"

    def test_cortex_m_is_zephyr(self):
        assert default_os_from_core_type("cortex-m33") == "zephyr"

    def test_anything_else_is_off(self):
        assert default_os_from_core_type("cortex-r5") == "off"

    def test_empty_string_is_off(self):
        assert default_os_from_core_type("") == "off"

    @pytest.mark.parametrize(
        "value",
        [7, ["cortex-a55"], {"a": 1}, True, None, 0, []],
        ids=["int", "list", "dict", "bool", "null", "zero", "emptylist"],
    )
    def test_a_nonstring_core_type_degrades_to_off_never_raises(self, value):
        """tan-cli#957: before the `isinstance` guard, `(core_type or
        "").lower()` raised `AttributeError` for every TRUTHY non-string
        (`7`, a list, a dict, `True`) -- `7 or ""` is `7`, and `int` has no
        `.lower()`. Falsy non-strings (`None`, `0`, `[]`) happened to survive
        the `or ""` already (they're all falsy, so `core_type or ""` -> `""`)
        but that was luck, not a guard -- this module's own docstring calls
        out that the `isinstance` check is the backstop for every caller,
        not just the two that currently guard at their own read site.

        Mutation-proven: reverting `t = core_type.lower() if
        isinstance(core_type, str) else ""` to `t = (core_type or
        "").lower()` turns the TRUTHY cases in this parametrize RED with a
        raised `AttributeError` (pytest reports it as a test error, not a
        failure, but it is exactly the regression this test exists to
        catch); the FALSY cases stay accidentally GREEN either way, which is
        why the truthy half is the load-bearing part of this test. Restoring
        the guard turns all seven GREEN -- verified by hand while writing
        this test.
        """
        assert default_os_from_core_type(value) == "off"


class TestCrossClassOs:
    def test_cortex_a_excludes_zephyr(self):
        assert cross_class_os("cortex-a55") == {"zephyr"}

    def test_cortex_m_excludes_yocto(self):
        assert cross_class_os("cortex-m33") == {"yocto"}

    def test_unresolved_excludes_both(self):
        # `default_os_from_core_type("")` falls through to `"off"`, which is
        # not in CLASS_RUNTIMES, so nothing is subtracted from the full set
        # -- this is exactly why `allowed_os_for_core` special-cases `""`
        # itself rather than trusting this function's answer for it (see
        # that function's docstring, tan-cli#914).
        assert cross_class_os("") == set(CLASS_RUNTIMES)

    def test_a_nonstring_core_type_does_not_raise(self):
        assert cross_class_os(7) == set(CLASS_RUNTIMES)


class TestAllowedOsForCore:
    CHOICES = ("zephyr", "yocto", "baremetal", "off")

    def test_cortex_a_offers_yocto_baremetal_off(self):
        assert allowed_os_for_core("cortex-a55", self.CHOICES) == [
            "yocto", "baremetal", "off"
        ]

    def test_cortex_m_offers_zephyr_baremetal_off(self):
        assert allowed_os_for_core("cortex-m33", self.CHOICES) == [
            "zephyr", "baremetal", "off"
        ]

    def test_unresolved_empty_string_degrades_to_empty_not_a_guess(self):
        """The `""` UNRESOLVED sentinel must not offer `["baremetal", "off"]`
        -- a plausible-looking but WRONG guess for a core whose real type
        could be Cortex-M (tan-cli#914 / alp-sdk-vscode#538)."""
        assert allowed_os_for_core("", self.CHOICES) == []

    @pytest.mark.parametrize(
        "value",
        [7, ["cortex-a55"], {"a": 1}, True, 0, [], None],
        ids=["int", "list", "dict", "bool", "zero", "emptylist", "null"],
    )
    def test_a_nonstring_core_type_never_raises(self, value):
        """Every non-string is falsy-or-truthy in Python's own sense, not
        just JSON's -- `allowed_os_for_core` must not raise for ANY of them,
        whether or not `core_type_lookup`'s own guard already normalised it
        to `""` before this call. `0`/`[]`/`None` are falsy in Python, so
        they take the `if not core_type: return []` branch directly; `7`/a
        list/a dict/`True` are truthy and reach `cross_class_os`, which
        must not raise either (see `TestCrossClassOs.
        test_a_nonstring_core_type_does_not_raise` above)."""
        assert allowed_os_for_core(value, self.CHOICES) == []
