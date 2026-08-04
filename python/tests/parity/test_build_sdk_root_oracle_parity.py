# SPDX-License-Identifier: Apache-2.0
"""`tan build --sdk-root <nonexistent>` against the shipped Rust oracle.

Closes the divergence `test_run_oracle_parity.py`'s
`test_run_sdk_root_invalid_is_a_known_divergence_from_the_oracle` documents
for `run` (which shares its own copy of the same ladder-resolution code and
is unaffected by this fix, tan-cli#257/#258 scope): `build_cmd.build`
resolved a bogus explicit `--sdk-root` unvalidated, fell through to
`_emit_plan`'s NEXT missing thing (`no board.yaml found`), and reported an
`sdk` key the oracle never emits on this path.

**Frozen replay, not live** (tan-cli#409). This case used to spawn the oracle
directly behind `skipif(RUST is None, ...)`, on the reasoning that "a
regression here cannot hide behind a stale fixture". What that traded for was
worse: gated on binary PRESENCE, it becomes a passing SKIP the day tan-cli#269
deletes `crates/`, and a skip hides a regression far more completely than a
stale fixture does -- the fixture at least still answers. The staleness worry
is handled where it belongs, by `oracle.PINNED_ORACLE_BUILD_INPUT_DIGEST` and
`test_oracle_provenance.py`, which fail when `crates/` moves past the capture.
`TAN_PARITY_LIVE=1` re-validates against a real binary for as long as one
builds.
"""
import pytest

from . import oracle_fixtures
from .oracle import _run, missing_for_live, python_command, rust_binary, rust_run

RUST = rust_binary()

pytestmark = pytest.mark.skipif(
    missing_for_live(RUST),
    reason="TAN_PARITY_LIVE=1 needs a Rust tan; set TAN_RUST_BINARY",
)


def test_build_sdk_root_invalid_matches_the_oracle(tmp_path):
    home = tmp_path / "home"
    work = tmp_path / "root"
    work.mkdir()
    argv = ["build", "--format", "json", "--sdk-root", "./nowhere"]
    r_code, r_out = rust_run(argv, work, home)
    p_code, p_out = _run(python_command(), argv, work, home)
    # The rust side comes back already scrubbed (`rust_run` redacts cwd/home);
    # the python side has to be put through the identical substitution or
    # `project.root` compares a live scratch path against a placeholder.
    p_out = oracle_fixtures.scrub(p_out, work, home)

    assert r_code == p_code == 1
    assert [i["code"] for i in r_out["issues"]] == ["build.plan-unavailable"]
    assert [i["code"] for i in p_out["issues"]] == ["build.plan-unavailable"]
    # Neither side reports an `sdk` key: an unresolvable explicit --sdk-root
    # is treated as no root at all, not a resolved-but-wrong one.
    assert "sdk" not in r_out
    assert "sdk" not in p_out
    # Both refuse for the SDK, not for the (also-missing) board.yaml -- the
    # message text still differs (allowed; only the machine-contract fields
    # are pinned here), but neither may mention board.yaml.
    assert "board.yaml" not in r_out["issues"][0]["message"]
    assert "board.yaml" not in p_out["issues"][0]["message"]
    assert r_out["data"] is None
    assert p_out["data"] is None
    # Everything outside `issues` must AGREE, which is what makes this a
    # parity case rather than three spot checks: `command`, `ok`, `exitCode`
    # and the whole `project` block, compared whole.
    assert {**r_out, "issues": []} == {**p_out, "issues": []}
