# SPDX-License-Identifier: Apache-2.0
"""tan-cli#1350: toggling board.yaml `diagnostics.link: itcm` -> `auto` in an
already-configured build dir must not keep the ITCM overlay.

`EXTRA_DTC_OVERLAY_FILE` is read by Zephyr with `zephyr_get(... CACHE ...)`, so
the `-D` passed while the knob was on persists in `CMakeCache.txt` after the
plan stops passing it. `stale_itcm_overlay_reset` unsets it and deletes the
leftover artefacts.
"""

from __future__ import annotations

from pathlib import Path

from tan.commands.build.link_stale import stale_itcm_overlay_reset

_ITCM_ARGS = [
    "build", "-b", "x", "app", "--",
    "-DEXTRA_DTC_OVERLAY_FILE=/p/build/m55_he-zephyr/alp-link-itcm.overlay",
]
_AUTO_ARGS = ["build", "-b", "x", "app", "--", "-DEXTRA_CONF_FILE=/p/alp.conf"]


def _slice_dir(tmp_path: Path, cache: str | None) -> Path:
    (tmp_path / "alp-link-itcm.conf").write_text("CONFIG_FLASH_LOAD_OFFSET=0x0\n")
    (tmp_path / "alp-link-itcm.overlay").write_text("/ {};\n")
    (tmp_path / "alp.conf").write_text("CONFIG_X=y\n")
    if cache is not None:
        (tmp_path / "build").mkdir()
        (tmp_path / "build" / "CMakeCache.txt").write_text(cache)
    return tmp_path


_STALE_CACHE = (
    "CMAKE_BUILD_TYPE:STRING=\n"
    "EXTRA_DTC_OVERLAY_FILE:UNINITIALIZED=/p/build/m55_he-zephyr/alp-link-itcm.overlay\n"
)


def test_itcm_to_auto_unsets_the_cached_overlay_and_deletes_the_artefacts(tmp_path):
    cwd = _slice_dir(tmp_path, _STALE_CACHE)
    args, issues = stale_itcm_overlay_reset(cwd, _AUTO_ARGS)
    assert args == ["-UEXTRA_DTC_OVERLAY_FILE"]
    assert [i.code for i in issues] == ["build.configure-cache-reset"]
    assert not (cwd / "alp-link-itcm.conf").exists()
    assert not (cwd / "alp-link-itcm.overlay").exists()
    assert (cwd / "alp.conf").exists()  # only the itcm artefacts go


def test_an_itcm_slice_keeps_everything(tmp_path):
    cwd = _slice_dir(tmp_path, _STALE_CACHE)
    assert stale_itcm_overlay_reset(cwd, _ITCM_ARGS) == ([], [])
    assert (cwd / "alp-link-itcm.overlay").exists()


def test_a_cache_that_never_named_the_overlay_is_left_alone(tmp_path):
    cwd = _slice_dir(tmp_path, "EXTRA_DTC_OVERLAY_FILE:UNINITIALIZED=/mine/x.overlay\n")
    args, issues = stale_itcm_overlay_reset(cwd, _AUTO_ARGS)
    assert (args, issues) == ([], [])
    # the stale files are still deleted: nothing reads them with the knob off
    assert not (cwd / "alp-link-itcm.overlay").exists()


def test_a_first_build_with_no_cache_is_a_no_op(tmp_path):
    cwd = _slice_dir(tmp_path, None)
    assert stale_itcm_overlay_reset(cwd, _AUTO_ARGS) == ([], [])


def test_refusal_helpers_recognise_only_the_link_itcm_codes():
    from tan.core.link_refusal import refusal_code, split_coded_message

    class _E(Exception):
        def __init__(self, code):
            super().__init__("m")
            self.code = code

    assert refusal_code(_E("build.link-itcm-unsupported")) == "build.link-itcm-unsupported"
    assert refusal_code(_E("build.plan-unavailable")) is None
    assert refusal_code(ValueError("x")) is None
    assert split_coded_message("build.link-itcm-console-conflict: why") == (
        "build.link-itcm-console-conflict", "why")
    assert split_coded_message("Generation failed: x") == (None, "Generation failed: x")


def test_the_unset_goes_right_after_the_separator_before_every_define():
    """tan-cli#1386: a user's/plan's later `-DEXTRA_DTC_OVERLAY_FILE` must
    survive, so the `-U` is placed before every `-D`, never appended last."""
    from tan.commands.build.link_stale import insert_after_separator

    args = ["build", "-b", "x", "app", "--", "-DA=1", "-DEXTRA_DTC_OVERLAY_FILE=/u.overlay"]
    out = insert_after_separator(args, ["-UEXTRA_DTC_OVERLAY_FILE"])
    assert out[:6] == ["build", "-b", "x", "app", "--", "-UEXTRA_DTC_OVERLAY_FILE"]
    assert out.index("-UEXTRA_DTC_OVERLAY_FILE") < out.index("-DEXTRA_DTC_OVERLAY_FILE=/u.overlay")
    assert insert_after_separator(args, []) == args
    assert insert_after_separator(["a"], ["-U"]) == ["a", "-U"]  # no `--`: appended
