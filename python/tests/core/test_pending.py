# SPDX-License-Identifier: Apache-2.0
"""`tan.core.pending`: the one `TBD` pending-placeholder definition (#276)
shared by `tan.core.flash_plan` and `tan.core.size` (and `pinmux`, once
ported) without either pulling the other's domain machinery in."""
from tan.core.pending import PENDING_PLACEHOLDER, is_pending_placeholder


def test_pending_placeholder_is_the_literal_sdk_sentinel():
    assert PENDING_PLACEHOLDER == "TBD"


def test_trimmed_but_not_folded_and_not_substring():
    assert is_pending_placeholder("TBD")
    assert is_pending_placeholder("  TBD  ")
    assert is_pending_placeholder("\tTBD\n")
    # Not case-folded: `tbd` is not the sentinel alp-sdk emits.
    assert not is_pending_placeholder("tbd")
    assert not is_pending_placeholder("Tbd")
    # Not a substring test: a real part number/path can contain `TBD`.
    assert not is_pending_placeholder("TBD-1234-XYZ")
    assert not is_pending_placeholder("/opt/TBDtool/x")
    assert not is_pending_placeholder("")
    assert not is_pending_placeholder(None)
    assert not is_pending_placeholder(123)
