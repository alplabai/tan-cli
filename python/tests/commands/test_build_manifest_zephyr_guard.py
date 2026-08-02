# SPDX-License-Identifier: Apache-2.0
"""tan-cli#309 (upstream tan-cli #97): `zephyr_boilerplate_loaded` -- port of
`crates/tan-cli/src/commands/build/execute/manifest.rs`'s
`zephyr_boilerplate_loaded`/`dir_shows_zephyr`, confirmed against that
module's own compiled test suite (`cargo test -p alp-tan-cli --bin tan
native_execute_`, all 14 passing, including the three guard-specific cases:
`native_execute_refuses_a_zephyr_slice_whose_configure_never_loaded_zephyr`,
`native_execute_accepts_a_zephyr_slice_evidenced_by_the_cmake_cache`,
`native_execute_accepts_a_sysbuild_slice_evidenced_one_level_down`)."""
from tan.commands.build.manifest import zephyr_boilerplate_loaded


def test_false_on_a_never_configured_dir(tmp_path):
    assert not zephyr_boilerplate_loaded(tmp_path)


def test_false_on_a_plain_host_configure_with_no_zephyr_evidence(tmp_path):
    """The tan-cli #97 defect itself: a `CMakeCache.txt` exists (the tool
    DID configure something) but carries no `ZEPHYR_BASE:` line and there is
    no `zephyr/` output -- a plain host project, not firmware."""
    build = tmp_path / "build"
    build.mkdir()
    (build / "CMakeCache.txt").write_text(
        "CMAKE_PROJECT_NAME:STATIC=alp_app\nCMAKE_GENERATOR:INTERNAL=Ninja\n",
        encoding="utf-8",
    )
    assert not zephyr_boilerplate_loaded(tmp_path)


def test_true_when_the_cmake_cache_carries_zephyr_base(tmp_path):
    """The primary signal -- what `find_package(Zephyr)` actually caches. A
    verified real Zephyr slice carries `ZEPHYR_BASE:PATH=...` and has NO
    `zephyr/` directory, so this must be sufficient on its own."""
    build = tmp_path / "build"
    build.mkdir()
    (build / "CMakeCache.txt").write_text(
        "CMAKE_PROJECT_NAME:STATIC=zephyr\nZEPHYR_BASE:PATH=/work/zephyr\n",
        encoding="utf-8",
    )
    assert not (build / "zephyr").exists(), "cache-only evidence, the real-world case"
    assert zephyr_boilerplate_loaded(tmp_path)


def test_true_when_a_zephyr_output_directory_exists_with_no_cache(tmp_path):
    """The OR fallback signal -- kept narrow (never promoted to primary) so
    the guard can only ever fail a build it is SURE about."""
    build = tmp_path / "build"
    (build / "zephyr").mkdir(parents=True)
    assert zephyr_boilerplate_loaded(tmp_path)


def test_true_for_a_sysbuild_slice_evidenced_one_level_down(tmp_path):
    """`--sysbuild` nests the real per-image Zephyr build one directory
    deeper than its own superbuild top level, which carries neither signal
    (`share/sysbuild/CMakeLists.txt` calls `find_package(Sysbuild ...)`, not
    `find_package(Zephyr)`). Without the one-level-down look this fails a
    correct V2N sysbuild build."""
    build = tmp_path / "build"
    nested_image = build / "alp_app"
    (nested_image / "zephyr").mkdir(parents=True)
    assert not (build / "zephyr").exists()
    assert not (build / "CMakeCache.txt").exists()
    assert zephyr_boilerplate_loaded(tmp_path)


def test_false_when_the_one_level_down_look_finds_nothing_either(tmp_path):
    build = tmp_path / "build"
    (build / "some_other_dir").mkdir(parents=True)
    (build / "some_other_dir" / "unrelated.txt").write_text("x", encoding="utf-8")
    assert not zephyr_boilerplate_loaded(tmp_path)


def test_non_utf8_cmake_cache_falls_back_to_the_zephyr_dir_signal(tmp_path):
    """Review finding (blocking): a non-UTF-8 `CMakeCache.txt` (a stray
    `CMAKE_C_COMPILER:FILEPATH=/opt/caf\\xe9/gcc`-style byte is real-world --
    some toolchain paths embed Latin-1 bytes) must not raise. The Rust
    oracle's `std::fs::read_to_string(...).is_ok_and(...)` folds invalid UTF-8
    into the same `io::Error` a missing file gets and falls straight through
    to the `zephyr/` fallback -- it never propagates. `UnicodeDecodeError` is
    a `ValueError`, not an `OSError`; `_dir_shows_zephyr`'s `except` clause
    must catch both or this raises out of `execute_slices`, whose own module
    docstring promises no escaping exception."""
    build = tmp_path / "build"
    build.mkdir()
    (build / "CMakeCache.txt").write_bytes(b"CMAKE_C_COMPILER:FILEPATH=/opt/caf\xe9/gcc\n")
    (build / "zephyr").mkdir()
    assert zephyr_boilerplate_loaded(tmp_path)


def test_non_utf8_cmake_cache_with_no_zephyr_dir_is_false_not_a_raise(tmp_path):
    """Same corrupt cache, but with no `zephyr/` fallback evidence either --
    must resolve to `False` (an unproven Zephyr build), never raise."""
    build = tmp_path / "build"
    build.mkdir()
    (build / "CMakeCache.txt").write_bytes(b"CMAKE_C_COMPILER:FILEPATH=/opt/caf\xe9/gcc\n")
    assert not zephyr_boilerplate_loaded(tmp_path)
