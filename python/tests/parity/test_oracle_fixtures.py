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


# --- normalise_scrubbed_path_separators -------------------------------------
#
# The gate these cover is a MODULE CONSTANT read at import
# (`REPLAY_IS_CAPTURE_PLATFORM`), so every case below sets it explicitly rather
# than inheriting whatever OS the suite happens to run on -- otherwise the
# no-op branch would only ever be measured on Windows and the rewriting branch
# only on POSIX, which is exactly the single-state testing that let four
# earlier defects ship green.


@pytest.fixture
def replaying_off_capture_platform(monkeypatch):
    """A frozen replay on an OS that is NOT the one the store was captured on."""
    monkeypatch.setattr(oracle_fixtures, "REPLAY_IS_CAPTURE_PLATFORM", False)


@pytest.fixture
def replaying_on_capture_platform(monkeypatch):
    """Windows CI, or any `TAN_PARITY_LIVE=1` run: byte-for-byte is meaningful."""
    monkeypatch.setattr(oracle_fixtures, "REPLAY_IS_CAPTURE_PLATFORM", True)


@pytest.mark.parametrize(
    "captured, expected",
    [
        # The bare path field the whole class of failure was first seen on.
        ("<ORACLE-ROOT-0>\\.\\build", "<ORACLE-ROOT-0>/./build"),
        # Free text that EMBEDS the path -- `issues[].message`, where the
        # `size`/`flash` mismatch actually lived. Everything after the path is
        # prose and must survive intact.
        (
            "no system-manifest.yaml at <ORACLE-ROOT-0>\\br\\system-manifest.yaml; "
            "run `tan build` first.",
            "no system-manifest.yaml at <ORACLE-ROOT-0>/br/system-manifest.yaml; "
            "run `tan build` first.",
        ),
        # A quote CLOSES the region: `dd`'s tail puts prose after it.
        (
            "dd: failed to open '<ORACLE-ROOT-0>\\.\\build\\a.elf': No such file",
            "dd: failed to open '<ORACLE-ROOT-0>/./build/a.elf': No such file",
        ),
        # A space INSIDE a segment (`core_id: 'with space'`) must not end the
        # run -- the bug the first cut of the regex had.
        (
            "no footprint source at <ORACLE-ROOT-0>\\br\\with space-zephyr\\zephyr.elf",
            "no footprint source at <ORACLE-ROOT-0>/br/with space-zephyr/zephyr.elf",
        ),
        # Already forward-slash (a POSIX-captured value, or a mixed one): a
        # no-op, not a double-rewrite.
        ("<ORACLE-ROOT-0>/br/m55/hp-zephyr", "<ORACLE-ROOT-0>/br/m55/hp-zephyr"),
        # NO token: untouched. This is what keeps the helper from being the
        # blanket separator-flattening `test_clean_parity.py` refuses.
        ("a literal C:\\Windows\\path in prose", "a literal C:\\Windows\\path in prose"),
        # Token present, but the backslash is BEFORE it -- outside the region.
        ("C:\\pre <ORACLE-ROOT-0>\\b", "C:\\pre <ORACLE-ROOT-0>/b"),
        # `test_clean_parity._scrub`'s OWN spelling, which predates `scrub`.
        # Its real frozen shape is MIXED -- forward-slash root portion, native
        # separator on the final join -- which is why a `\`-only search would
        # have missed that this module had the same defect.
        ("<ROOT>/proj\\build", "<ROOT>/proj/build"),
    ],
    ids=[
        "bare-path-field",
        "embedded-in-prose",
        "quote-closes-the-region",
        "space-inside-a-segment",
        "already-forward-slash",
        "no-token-untouched",
        "backslash-before-the-token-untouched",
        "clean-parity-bare-root-token",
    ],
)
def test_rewrites_only_the_token_anchored_region(
    captured, expected, replaying_off_capture_platform
) -> None:
    assert oracle_fixtures.normalise_scrubbed_path_separators(captured) == expected


def test_walks_dicts_lists_and_leaves_non_strings_alone(
    replaying_off_capture_platform,
) -> None:
    payload = {
        "buildRoot": "<ORACLE-ROOT-0>\\.\\build",
        "entries": [{"rc": 0, "ok": True, "message": "would run west --build-dir <ORACLE-ROOT-0>\\b"}],
        "unrelated": None,
    }
    assert oracle_fixtures.normalise_scrubbed_path_separators(payload) == {
        "buildRoot": "<ORACLE-ROOT-0>/./build",
        "entries": [{"rc": 0, "ok": True, "message": "would run west --build-dir <ORACLE-ROOT-0>/b"}],
        "unrelated": None,
    }


def test_is_a_no_op_on_the_capture_platform(replaying_on_capture_platform) -> None:
    """Windows CI and `TAN_PARITY_LIVE=1` keep the byte-for-byte diff -- a real
    separator divergence between the two binaries is still caught there, which
    is the whole reason this is gated rather than applied everywhere."""
    captured = "<ORACLE-ROOT-0>\\.\\build"
    assert oracle_fixtures.normalise_scrubbed_path_separators(captured) == captured
