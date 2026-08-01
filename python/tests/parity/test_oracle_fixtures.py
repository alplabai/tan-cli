# SPDX-License-Identifier: Apache-2.0
"""Unit tests for `oracle_fixtures._refuse_if_leaking_a_real_host_path` -- the
capture-time guard added after a real leak reached a committed fixture (three
`scrub_roots=()` cases in `test_oracle_parity.py` whose stored envelopes still
carried the capture host's own paths; see that module's `git log` for the
fix). Exercises the guard directly rather than through a live capture: a live
capture needs a built oracle binary, and this logic has nothing to do with
one -- it is a pure string check over whatever `live_fn()` returned.

Every leaked-path fixture below builds its account-name fragment through an
f-string interpolation (`{_NAME}`) rather than writing it out contiguously:
`tests/gates/test_no_leaked_host_paths.py` scans this file's own TRACKED
source text for exactly this shape once it is committed, and a literal
`C:/Users/<name>/...` written out whole right here would flag this file the
same way the real leak this module fixes once did. The `{`/`}` braces are
not incidental -- `test_no_leaked_host_paths.py`'s own `_is_real_account`
treats a leading `{` as a placeholder/format-expansion marker, same as
`<`/`$`/`%`, so the interpolated form reads as a template to that gate even
though the runtime string it produces is a full, real-shaped path.
"""
import pytest

from . import oracle_fixtures

#: Realistic-SHAPED but fictional -- long enough and un-listed in either
#: gate's placeholder set to exercise the "this is a real account" branch,
#: without being any actual developer's name.
_NAME = "exampleuser"


@pytest.mark.parametrize(
    "leaked",
    [
        # The forward-slash form `project.root`/`data.boardYamlPath` actually
        # carried (tan renders reported paths this way on every platform).
        {"data": {"boardYamlPath": f"C:/Users/{_NAME}/AppData/Local/Temp/x/board.yaml"}},
        # The JSON-double-escaped backslash form the oracle's own message text
        # carried (`sdk.path-not-found`) -- the shape that slipped past
        # `tests/gates/test_no_leaked_host_paths.py`'s single-backslash-only
        # pattern the first time, because THIS module scans the serialized
        # JSON text (doubled backslashes), not the raw Python string.
        {"issues": [{"message": f"SDK path not found: C:\\Users\\{_NAME}\\AppData\\x"}]},
        # POSIX form.
        {"data": {"root": f"/home/{_NAME}/work/proj"}},
        # macOS form -- the shape a real leak in `test_plan_from_shows_the_
        # plan_and_writes_nothing`'s fixtures actually took (`env.ALP_SDK_ROOT`
        # embedding a real developer's `/Users/<name>/...` checkout path).
        {"data": {"env": {"ALP_SDK_ROOT": f"/Users/{_NAME}/VSCodeProjects/alp-sdk"}}},
    ],
    ids=["windows-forward-slash", "windows-json-escaped-backslash", "posix", "macos"],
)
def test_refuses_a_real_account_path(leaked) -> None:
    with pytest.raises(RuntimeError, match="real machine"):
        oracle_fixtures._refuse_if_leaking_a_real_host_path(leaked, "some#0")


@pytest.mark.parametrize(
    "clean",
    [
        # A scrubbed payload: the placeholder token, not a real path.
        {"data": {"root": "<ORACLE-ROOT-0>/build"}},
        # Legitimate placeholder homes -- must not false-positive, or a
        # genuinely clean capture becomes uncapturable.
        {"data": {"root": "C:/Users/dev/proj"}},
        {"data": {"root": "/home/runner/work/proj"}},
        {"data": {"root": "/home/.alp/sdk-cache"}},
        {"data": {}},
    ],
    ids=["scrubbed-token", "windows-placeholder", "posix-placeholder", "dotfile", "no-paths"],
)
def test_does_not_flag_a_clean_or_placeholder_path(clean) -> None:
    oracle_fixtures._refuse_if_leaking_a_real_host_path(clean, "some#0")  # must not raise
