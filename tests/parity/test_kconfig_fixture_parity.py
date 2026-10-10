# SPDX-License-Identifier: Apache-2.0
"""Self-test for the kconfig fixture byte-parity gate (tan-cli#1261).

`kconfig_fixture_parity.py` used to pass -- printing a NOTICE -- whenever
`tests/fixtures/kconfig-contract/emit-kconfig.golden.json` was absent from the
`--sdk` tree, at ANY ref, including a released alp-sdk that had REMOVED it.
`release-sdk-parity` in `.github/workflows/parity.yml` runs that script against
whatever alp-sdk tag `releases/latest` resolves to, so the one case the gate
exists to catch was the one case it could not fail on.

This module pins all four verdicts (absent upstream FAILS -- that is the
regression -- byte-identical PASSES, differing bytes FAIL, a missing vendored
copy FAILS), that each verdict reaches the process EXIT CODE, and the two
invariants the fix must not disturb: an explicit `--sdk` that does not resolve
is still a hard FAIL rather than a fall-through (tan-cli#172 review,
tan-cli#175), and a run with no alp-sdk checkout reachable at all is still a
clean self-skipping no-op.

The exit-code layer is not redundant with the return-value layer. CI acts on
the exit code and on nothing else, so asserting only `run()`'s bool leaves the
whole #1261 defect reproducible one level up -- see
`test_each_verdict_reaches_the_process_exit_code`.

Run: python3 -m pytest tests/parity/test_kconfig_fixture_parity.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _sdk_checkout  # noqa: E402
import kconfig_fixture_parity as k  # noqa: E402

FIXTURE_BYTES = b'{"schemaVersion": 1, "symbols": []}\n'
DRIFTED_BYTES = FIXTURE_BYTES.replace(b"symbols", b"symbol_list")


@pytest.fixture(autouse=True)
def _no_ambient_sdk_checkout(monkeypatch):
    """Every case here is about the tree it builds itself. `$ALP_SDK_ROOT` is
    the second candidate `resolve_sdk_root` tries, so on a developer host that
    binds it, a regression that stopped honouring `--sdk` could resolve a
    genuine alp-sdk and let a case pass for the wrong reason."""
    monkeypatch.delenv("ALP_SDK_ROOT", raising=False)


def _sdk_tree(tmp_path: Path, *, fixture: bytes | None) -> Path:
    """An alp-sdk-shaped checkout, optionally carrying the golden fixture.

    `scripts/alp_orchestrate/` is what `_sdk_checkout.looks_like_sdk_checkout`
    tests for, so a tree without it is "not an alp-sdk checkout" rather than
    "an alp-sdk checkout missing the fixture" -- two different verdicts.
    """
    sdk_root = tmp_path / "alp-sdk"
    (sdk_root / "scripts" / "alp_orchestrate").mkdir(parents=True)
    if fixture is not None:
        upstream = sdk_root / k.FIXTURE_RELPATH
        upstream.parent.mkdir(parents=True)
        upstream.write_bytes(fixture)
    return sdk_root


def _vendored(tmp_path: Path, monkeypatch, *, contents: bytes | None) -> Path:
    """Point the module's VENDORED_PATH at a temp file (or a missing one)."""
    path = tmp_path / "vendored" / k.FIXTURE_RELPATH.name
    if contents is not None:
        path.parent.mkdir(parents=True)
        path.write_bytes(contents)
    monkeypatch.setattr(k, "VENDORED_PATH", path)
    return path


def test_absent_upstream_fixture_fails(tmp_path, monkeypatch, capsys):
    """THE tan-cli#1261 regression: before the fix this returned True with a
    NOTICE, so a released alp-sdk that dropped the fixture reported PASS."""
    _vendored(tmp_path, monkeypatch, contents=FIXTURE_BYTES)
    sdk_root = _sdk_tree(tmp_path, fixture=None)

    assert k.run(sdk_root) is False

    out = capsys.readouterr().out
    assert "FAIL" in out
    assert "NOTICE" not in out
    assert str(k.FIXTURE_RELPATH) in out
    # The message has to say what happened and what to do about it, not just
    # that something is missing.
    assert "REMOVED or MOVED" in out
    assert "PINNED_SDK_TAG" in out
    assert "re-vendor" in out


def test_byte_identical_fixture_passes(tmp_path, monkeypatch, capsys):
    _vendored(tmp_path, monkeypatch, contents=FIXTURE_BYTES)
    sdk_root = _sdk_tree(tmp_path, fixture=FIXTURE_BYTES)

    assert k.run(sdk_root) is True
    assert "PASS" in capsys.readouterr().out


def test_differing_bytes_fail(tmp_path, monkeypatch, capsys):
    """The drift the gate was always able to catch -- unchanged by #1261."""
    _vendored(tmp_path, monkeypatch, contents=FIXTURE_BYTES)
    sdk_root = _sdk_tree(tmp_path, fixture=DRIFTED_BYTES)

    assert k.run(sdk_root) is False

    out = capsys.readouterr().out
    assert "FAIL" in out
    assert "differs from upstream" in out


def test_missing_vendored_copy_fails(tmp_path, monkeypatch, capsys):
    """Checked BEFORE the upstream file, and still a failure in its own right
    -- a vendored copy nobody can find is not a reason to skip the diff."""
    vendored_path = _vendored(tmp_path, monkeypatch, contents=None)
    sdk_root = _sdk_tree(tmp_path, fixture=FIXTURE_BYTES)

    assert k.run(sdk_root) is False

    out = capsys.readouterr().out
    assert "FAIL" in out
    assert "vendored fixture missing" in out
    assert str(vendored_path) in out


@pytest.mark.parametrize(
    "vendored_bytes, upstream_bytes, expected_rc",
    [
        pytest.param(FIXTURE_BYTES, None, 1, id="absent-upstream"),
        pytest.param(FIXTURE_BYTES, DRIFTED_BYTES, 1, id="byte-mismatch"),
        pytest.param(None, FIXTURE_BYTES, 1, id="vendored-copy-missing"),
        pytest.param(FIXTURE_BYTES, FIXTURE_BYTES, 0, id="byte-identical"),
    ],
)
def test_each_verdict_reaches_the_process_exit_code(
        tmp_path, monkeypatch, vendored_bytes, upstream_bytes, expected_rc):
    """CI acts on the EXIT CODE, never on `run()`'s bool -- `parity.yml` fails
    a step on a non-zero exit and on nothing else.

    Asserting only the return value leaves the whole #1261 defect reproducible
    one layer up: replace `main()`'s `return 0 if run(sdk_root) else 1` with a
    bare `run(sdk_root)` followed by `return 0`, and every return-value case
    above still passes while the gate reports PASS to CI on an absent upstream
    fixture. These four cases are what turn that mutation red.
    """
    _vendored(tmp_path, monkeypatch, contents=vendored_bytes)
    sdk_root = _sdk_tree(tmp_path, fixture=upstream_bytes)

    assert k.main(["--sdk", str(sdk_root)]) == expected_rc


def test_an_explicit_sdk_that_does_not_resolve_is_a_hard_fail(
        tmp_path, monkeypatch, capsys):
    """tan-cli#172 review / tan-cli#175: `--sdk` is a demand, not a hint. It
    must not fall through to $ALP_SDK_ROOT, a sibling checkout, or the SKIP."""
    _vendored(tmp_path, monkeypatch, contents=FIXTURE_BYTES)
    # Neutralise the sibling-checkout candidate too (the env var is already
    # gone): on a host carrying an `alp-sdk/` checkout beside this one, a
    # regression that demoted `--sdk` back to a hint would otherwise resolve
    # THAT and pass for the wrong reason.
    monkeypatch.setattr(_sdk_checkout, "resolve_sdk_root", lambda explicit: None)
    not_an_sdk = tmp_path / "not-an-sdk"
    not_an_sdk.mkdir()

    assert k.main(["--sdk", str(not_an_sdk)]) == 1

    out = capsys.readouterr().out
    assert "FAIL" in out
    assert "--sdk" in out
    assert "SKIP" not in out


def test_no_reachable_sdk_checkout_is_a_clean_no_op(
        tmp_path, monkeypatch, capsys):
    """The one non-failure this gate keeps: a local dev loop with no alp-sdk
    anywhere exits 0. `python/tests/commands/test_kconfig_command.py` already
    proves the vendored copy is internally consistent without a checkout."""
    _vendored(tmp_path, monkeypatch, contents=FIXTURE_BYTES)
    # Same reason as above: the sibling-checkout candidate depends on what sits
    # next to the checkout this suite happens to run from.
    monkeypatch.setattr(_sdk_checkout, "resolve_sdk_root", lambda explicit: None)

    assert k.main([]) == 0

    out = capsys.readouterr().out
    assert "SKIP" in out
    assert "FAIL" not in out
