# SPDX-License-Identifier: Apache-2.0
"""tan-cli#1217: `validate.py::_validate_consistency`'s `profile:` resolve
carried the identical unguarded `(REPO / prof).resolve()` +
`prof_path.is_file()` alp-sdk#1961 fixed in the sibling
`scripts/alp_orchestrate/validate.py` -- ported here, not re-derived, from
alp-sdk's own final shape (`scripts/alp_orchestrate/validate.py:177-206`,
alp-sdk#1961): the
except tuple is `(OSError, RuntimeError)`, not the POSIX-only
`(RuntimeError, PermissionError)` an EARLIER commit on that same branch
shipped and its own next commit corrected -- `PermissionError` is already an
`OSError` subclass, and a real WSL-created symlink loop driven through
Windows CPython showed `.resolve()` return without raising at all and
`.is_file()` raise a plain `OSError` (`WinError 1920`) instead of either
POSIX shape.

Unlike the `kconfig.py::_emit_extra_library_profile` sibling this file's
neighbour `tests/gates/test_never_raises_contract_holds.py` already seeds
(quiet-return: a `#`-comment line on every failure), `_validate_consistency`
is a RAISE-contract function by design -- its own docstring says "raises
`OrchestratorError` for hard violations" for a couple dozen unrelated rule
violations having nothing to do with file IO (curated-library collisions,
`ota.provider` compatibility, `boot.signing.algorithm` support, ...). That
is NOT the same shape as the gate file's opt-in seed list, whose every
existing entry's contract is "for THIS function's own read, only a quiet
value or exactly one curated exception type ever escapes" -- seeding the
whole `validate._validate_consistency` name there would overstate what is
actually pinned (only the `profile:` resolve call, not the function's many
other raise sites, which this fix does not touch and this file does not
audit). So this fix's coverage lives here instead, as a dedicated module,
the same shape `test_load_som_doc_malformed_preset.py` /
`test_emit_scaffold_unreadable_metadata.py` already use for a planner-file
fix that is not folded into that gate.

Constructs a minimal `BoardProject`/`Slice` directly (the
`test_kconfig_nonstring_core_type.py` / `test_topology_nonstring_core_type.py`
shape) rather than a full `load_board_yaml()` round trip -- `_validate_
consistency` takes the resolved model, and `models.py` is a bound-root-free
leaf so no metadata tree needs to exist on disk for the surrounding project.
`profile:` is passed as an ABSOLUTE `tmp_path`-rooted string: `(REPO / prof)`
lets an absolute right-hand side replace `REPO` outright (the same
`Path("a") / "/etc/passwd" == Path("/etc/passwd")` fact
`kconfig.py::_emit_extra_library_profile`'s own seed test documents for the
identical join), so these cases never touch the bound SDK checkout.

Windows note (this host): real `os.symlink` needs Developer Mode/admin
(`OSError: [WinError 1314]`), so `test_symlink_loop` cannot construct its own
fixture here and errors at setup -- expected on this host, exercised for real
by CI's ubuntu-only `python`/`seam1` legs. `test_windows_shape_clean_error`
below is the one case proven length-for-length against the confirmed REAL
Windows failure (`.resolve()` silently succeeds, `.is_file()` raises a plain
`OSError`) via a deterministic monkeypatch, and is the case this host can
actually run and watch go red against the unfixed source.
"""
from __future__ import annotations

import contextlib
import os
from pathlib import Path

import pytest

# `_bound_sdk` is a pytest fixture, imported for its side effect -- the same
# idiom `_baremetal_support`'s consumers use for `bound_sdk_root`.
from tests.planner._bound_sdk_fixture import SDK, _bound_sdk  # noqa: F401

pytestmark = pytest.mark.skipif(
    SDK is None,
    reason="ALP_SDK_ROOT is not set (or does not point at a real alp-sdk "
           "checkout) -- importing tan.planner.validate requires SOME bound "
           "root (tan/planner_root.py). A SKIP about the missing root, not a "
           "pass.",
)

_skip_as_root = pytest.mark.skipif(
    os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="POSIX-only, non-root: chmod 0o000 has no effect for root and "
           "Windows ACLs don't honour POSIX mode bits.",
)

_NAME = "seed1217"


def _validate_module():
    """Imported inside the call so the module is not imported before
    `bind_sdk_root` has run (collection order) -- `tan.planner.validate`
    triggers `tan.planner.__init__`, which reads `.paths` at import time."""
    import tan.planner.validate as m
    return m


def _project(tmp_path: Path, profile_path):
    from tan.planner.models import BoardProject, Slice

    return BoardProject(
        sku="E1M-TEST",
        hw_rev=None,
        board_name=None,
        board_hw_rev=None,
        cores={"a55": Slice(
            core_id="a55", os="yocto",
            extra_libraries=[{"name": _NAME, "profile": str(profile_path)}],
        )},
        ipc=[],
        soc_spec={},
        som_preset={},
        board_preset=None,
        # An empty, isolated tree: `_curated_library_names` returns
        # `frozenset()` for a missing `libraries/` dir, so `_NAME` never
        # collides with a curated entry.
        metadata_root=tmp_path / "metadata",
    )


def _raises(tmp_path: Path, profile_path) -> str:
    m = _validate_module()
    with pytest.raises(m.OrchestratorError) as excinfo:
        m._validate_consistency(_project(tmp_path, profile_path))
    return str(excinfo.value)


@contextlib.contextmanager
def _permission_denied(dir_path: Path):
    """chmod 0o000 on @dir_path (which must already contain whatever file
    the caller is about to try reading) for the duration of the block."""
    dir_path.mkdir(parents=True, exist_ok=True)
    original_mode = dir_path.stat().st_mode
    dir_path.chmod(0o000)
    try:
        yield
    finally:
        dir_path.chmod(original_mode)


def test_a_present_profile_file_does_not_raise(tmp_path):
    """Control: the guard must not fire on a legitimate profile."""
    path = tmp_path / "hw-backends.yaml"
    path.write_text("accelerators: []\n", encoding="utf-8")
    m = _validate_module()
    # Must not raise.
    m._validate_consistency(_project(tmp_path, path))


def test_an_absent_profile_keeps_its_existing_message(tmp_path):
    """Unchanged pre- and post-fix: a genuinely absent path resolves fine
    (`Path.resolve()` doesn't require the target to exist) and only
    `is_file()` answers False -- the ORIGINAL, still-correct half of this
    check, preserved so the fix doesn't fold this shape into the new
    `could not be resolved` branch by mistake."""
    path = tmp_path / "does-not-exist.yaml"
    msg = _raises(tmp_path, path)
    assert _NAME in msg
    assert "does not resolve" in msg
    assert "could not be resolved" not in msg


def test_symlink_loop_clean_error(tmp_path):
    """The reachable half of alp-sdk#1961's "same defect class" note: a
    `profile:` that hits a symlink loop (ELOOP) must fail with a clean
    `OrchestratorError`, not an unhandled `RuntimeError` out of
    `_validate_consistency` -- which runs on every `load_board_yaml` call,
    including `--emit build-plan`, strictly before `kconfig.py`'s own
    never-raises fix (tan-cli#1122) is ever reached.

    Mutation-proven: reverting `validate.py`'s `try`/`except` back to the
    bare `prof_path = (REPO / prof).resolve()` / `if not prof_path.is_file()`
    this fix replaced turns this red with an unhandled `RuntimeError`
    (verified on this host by reverting, see the PR description; not
    re-asserted here since Windows cannot construct the shape at all --
    see the module docstring)."""
    path = tmp_path / "loopy-hw-backends.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.symlink_to(path)
    msg = _raises(tmp_path, path)
    assert _NAME in msg
    assert "could not be resolved" in msg


@_skip_as_root
def test_permission_denied_clean_error(tmp_path):
    """The EACCES half: `Path.is_file()` re-raises `PermissionError` (an
    `OSError` subclass) instead of swallowing it like
    `ENOENT`/`ENOTDIR`/`EBADF`/`ELOOP` -- the "same defect class" note in
    alp-sdk#1961."""
    sub = tmp_path / "sub"
    path = sub / "denied-hw-backends.yaml"
    sub.mkdir()
    path.write_text("accelerators: []\n", encoding="utf-8")
    with _permission_denied(sub):
        msg = _raises(tmp_path, path)
    assert _NAME in msg
    # Deliberately NOT asserting "could not be resolved" here. Whether the
    # EACCES ancestor surfaces as a raise depends on the interpreter:
    # tests/gates/test_never_raises_contract_holds.py records the measured
    # table -- 3.12.3 and 3.13.15 raise PermissionError from `is_file()`,
    # 3.14.7 swallows every OSError and returns False. On 3.14 this takes
    # the `not prof_is_file` branch and says "does not resolve to a file"
    # instead. Both are a clean OrchestratorError naming the profile, which
    # is the contract; pinning the wording would red the first bound run at
    # the 3.x ceiling.


def test_windows_shape_clean_error(tmp_path, monkeypatch):
    """The REAL Windows shape (issue #1217's own repro, confirmed on
    Windows CPython 3.11.3 against a real WSL-created symlink loop on an
    NTFS drive): `.resolve()` returns *without* raising at all, and it is
    `.is_file()` that raises a plain `OSError` (`WinError 1920`, "The file
    cannot be accessed by the system") -- neither the POSIX `RuntimeError`
    (`test_symlink_loop_clean_error` above) nor a `PermissionError`
    (`test_permission_denied_clean_error` above). Deterministic via
    monkeypatching `pathlib.Path.is_file` for this one profile path only,
    so this is the one case this Windows host can actually run and watch
    go red against the unfixed source: reverting `validate.py`'s
    `try`/`except` turns this red with the real `OSError` escaping
    unhandled.
    """
    prof_name = "winloop-hw-backends.yaml"
    path = tmp_path / prof_name
    real_is_file = Path.is_file

    def _raise_windows_shape_for_profile(self):
        if self.name == prof_name:
            raise OSError(22, "The file cannot be accessed by the system")
        return real_is_file(self)

    monkeypatch.setattr(Path, "is_file", _raise_windows_shape_for_profile)

    msg = _raises(tmp_path, path)
    assert _NAME in msg
    assert "could not be resolved" in msg
