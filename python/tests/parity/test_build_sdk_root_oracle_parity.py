# SPDX-License-Identifier: Apache-2.0
"""`tan build --sdk-root <nonexistent>` against the shipped Rust oracle.

Closes the divergence `test_run_oracle_parity.py`'s
`test_run_sdk_root_invalid_is_a_known_divergence_from_the_oracle` documents
for `run` (which shares its own copy of the same ladder-resolution code and
is unaffected by this fix, tan-cli#257/#258 scope): `build_cmd.build`
resolved a bogus explicit `--sdk-root` unvalidated, fell through to
`_emit_plan`'s NEXT missing thing (`no board.yaml found`), and reported an
`sdk` key the oracle never emits on this path. Live, not frozen-replay --
spawns both binaries unconditionally whenever an oracle is present, mirroring
`test_run_oracle_parity.py`'s own `_ORACLE_REQUIRED` cases, so a regression
here cannot hide behind a stale fixture."""
import pytest

from .oracle import _run, python_command, rust_binary

RUST = rust_binary()

_ORACLE_REQUIRED = pytest.mark.skipif(
    RUST is None,
    reason="needs a built Rust tan; run `cargo build --bin tan` (or set TAN_RUST_BINARY)",
)


@_ORACLE_REQUIRED
def test_build_sdk_root_invalid_matches_the_oracle(tmp_path):
    home = tmp_path / "home"
    work = tmp_path / "root"
    work.mkdir()
    argv = ["build", "--format", "json", "--sdk-root", "./nowhere"]
    r_code, r_out = _run([RUST], argv, work, home)
    p_code, p_out = _run(python_command(), argv, work, home)

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
