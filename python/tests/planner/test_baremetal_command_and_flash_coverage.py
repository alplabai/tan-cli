# SPDX-License-Identifier: Apache-2.0
"""tan-cli#1042, orchestrator half: `_slice_flash_recipe`'s baremetal arm,
`_slice_command`'s baremetal arm, and the four baremetal-only helpers that
feed it (`_baremetal_output_dir`, `_baremetal_cache_args`,
`_baremetal_project_include`, `_baremetal_project_include_arg`) had no caller
anywhere under `tests/`.

Same structural blind spot tan-cli#1036 documented for `_slice_post_commands`
and tan-cli#1044 closed for that one function: every one of these is reached
outside a hand-written unit test ONLY from the two `ALP_SDK_ROOT`-gated
parity suites (`tests/parity/test_planner_emit_parity.py`,
`tests/parity/test_planner_oracle_regression.py`), and neither can
structurally reach a baremetal arm -- alp-sdk's pinned tree has zero
`os: baremetal` board.yaml examples across all 99, and every
`build-plan.json` fixture's 213 slices are `zephyr` or `yocto`. No fixture
can parametrize what the corpus does not contain, so hand-written units are
the only route.

Hermetic by construction: no alp-sdk checkout, no `board.yaml`, no
`metadata/**` tree -- see `tests/planner/_baremetal_support.py` for the
synthetic project and for the SDK-root binding rules this module obeys. That
is the point, not a convenience: a test that only ran in the `sdk_parity` job
would reproduce the exact blind spot the missing baremetal fixture created.

Every assertion here is VERBATIM on the emitted strings. A paraphrased
"contains ALP_SOM_SKU somewhere" assertion would pass a mutant that changed
the spelling, the order or the `=`, and the spelling IS the contract -- the
customer's `CMakeLists.txt` tests `ALP_SOM_SKU` by that exact name, and
`cmake -D` rejects a define with no `=value` outright.

The `buildplan.py` / `validate.py` half of tan-cli#1042 lives in
`test_baremetal_plan_emit_coverage.py`.
"""
from __future__ import annotations

import pytest

# `base_dir` and `bound_sdk_root` are pytest fixtures, imported for their
# side effect -- the same idiom `tests/commands/test_build_streaming.py` uses
# for `project`. `bound_sdk_root` is autouse where it is defined, so importing
# it is what arms it here; see `_baremetal_support` for why the binder has
# exactly one definition and why a `conftest.py` would be the wrong home.
from tests.planner._baremetal_support import (  # noqa: F401
    PROJECT_INCLUDE_PREAMBLE,
    baremetal_project,
    base_dir,
    bound_sdk_root,
    slice_of,
)


# ---------------------------------------------------------------------
# orchestrator.py:104-106 -- `_slice_flash_recipe`'s baremetal arm
# ---------------------------------------------------------------------


def test_a_baremetal_slice_flashes_via_the_cmake_recipe(base_dir):
    """`("baremetal_cmake_flash", {})` -- the recipe name is the string
    `tan flash` dispatches on and `Slice.to_manifest_entry` writes into
    `system-manifest.yaml` as `flash_method:`, so it is a consumer-visible
    contract, not an internal label.

    The empty args dict is asserted too, not skipped: the zephyr arm next to
    it builds a populated `flash_args` (`jlink_flash_device`,
    `expect_dpidr`/`jlink_device`, `slot0_load_address`), and `{}` here is
    the positive claim that a baremetal slice carries none of them.
    """
    from tan.planner.orchestrator import _slice_flash_recipe

    assert _slice_flash_recipe(slice_of("baremetal", app="./src")) == (
        "baremetal_cmake_flash", {})


@pytest.mark.parametrize(
    ("os_", "expected"),
    [
        ("yocto", ("yocto_wic_to_sd_or_emmc", {"target": ""})),
        ("zephyr", ("zephyr_west_flash", {})),
        ("off", (None, None)),
        ("nonsense", (None, None)),
    ],
)
def test_a_non_baremetal_slice_gets_a_different_flash_recipe(os_, expected):
    """The control: without it, hard-wiring `("baremetal_cmake_flash", {})`
    as this function's only return would satisfy the assertion above."""
    from tan.planner.orchestrator import _slice_flash_recipe

    assert _slice_flash_recipe(slice_of(os_, app="./src")) == expected


# ---------------------------------------------------------------------
# orchestrator.py:394-459 -- `_slice_command`'s baremetal arm
# ---------------------------------------------------------------------


def test_a_baremetal_slice_configures_with_cmake_and_the_full_cache_arg_fan_in(base_dir):
    """The whole baremetal `_slice_command` arm, argv-for-argv.

    `-B .` (not `-B <buildDir>`) is load-bearing: the plan pins this
    command's `cwd` to the slice's buildDir and cmake resolves a relative
    `-B` against its own cwd, so the historical `-B build/<core>-baremetal`
    double-nested the tree where nothing reading `artifacts`/`buildDir`
    would find it (tan-cli#550).

    The three `-D` fan-ins that follow are the ones tan-cli#551 found the
    configure had NEVER carried: they used to be rendered only into a
    `cmake-args.txt` nothing read, then dropped outright with that artefact
    (#1278). Order is asserted with them because argv order is what a human
    reads back out of a failing configure log.
    """
    from tan.planner.orchestrator import _slice_command

    project, slice_ = baremetal_project(
        base_dir, board_name="E1M-EVK", toolchain="arm-zephyr-eabi")

    assert _slice_command(project, slice_, base_dir) == [
        "cmake", "-S", "${PROJECT_ROOT}/src", "-B", ".",
        "--no-warn-unused-cli",
        "-DCMAKE_EXPORT_COMPILE_COMMANDS=ON",
        "-DCMAKE_RUNTIME_OUTPUT_DIRECTORY=$<1:"
        "${PROJECT_ROOT}/build/m55_he-baremetal/output>",
        "-DALP_SOM_SKU=E1M-AEN801",
        "-DALP_SOM_FAMILY=aen",
        "-DALP_CORE_ID=m55_he",
        "-DALP_TOOLCHAIN=arm-zephyr-eabi",
        "-DCMAKE_PROJECT_INCLUDE="
        "${PROJECT_ROOT}/build/m55_he-baremetal/alp-baremetal.cmake",
    ]


def test_the_runtime_output_directory_is_wrapped_in_a_no_op_generator_expression(base_dir):
    """`$<1:...>`, asserted on its own so a mutant that drops JUST the
    wrapper reds here rather than only inside the full-argv test above.

    CMake: "Multi-configuration generators (Visual Studio, Xcode, Ninja
    Multi-Config) append a per-configuration subdirectory to the specified
    directory UNLESS A GENERATOR EXPRESSION IS USED." Without the wrapper the
    plan says `<buildDir>/output` while the binary lands in
    `<buildDir>/output/Debug/` -- and the default generator on Windows
    (Visual Studio) is a multi-config one, so this is the common case, not
    the exotic one.
    """
    from tan.planner.orchestrator import _slice_command

    project, slice_ = baremetal_project(base_dir)
    cmd = _slice_command(project, slice_, base_dir)

    assert ("-DCMAKE_RUNTIME_OUTPUT_DIRECTORY=$<1:"
            "${PROJECT_ROOT}/build/m55_he-baremetal/output>") in cmd


def test_the_configure_carries_the_npu_dispatch_cache_entries(base_dir):
    """The SoM-preset `capabilities:` half of the `_baremetal_cache_args`
    fan-in: on a multi-NPU SKU every available NPU is enabled, because apps
    dispatch concurrent independent models via
    `alp_inference_open(.backend=...)` per handle.

    The two names are the option names `src/yocto/CMakeLists.txt` tests --
    a rename on either side silently compiles the dispatcher out.
    """
    from tan.planner.orchestrator import _slice_command

    project, slice_ = baremetal_project(
        base_dir, sku="E1M-V2M101", family="v2m",
        capabilities={"drp_ai": True, "deepx_dxm1": True})
    cmd = _slice_command(project, slice_, base_dir)

    assert cmd[-2:] == ["-DALP_SDK_USE_DRPAI_V2N=ON",
                        "-DALP_SDK_USE_DEEPX_DXM1=ON"]


def test_a_baremetal_slice_with_no_app_has_no_command(base_dir):
    """`None`, not a `cmake -S None`: the caller carries such a slice as
    `no-command` with a warning rather than dropping it."""
    from tan.planner.orchestrator import _slice_command

    project, slice_ = baremetal_project(base_dir, app=None)

    assert _slice_command(project, slice_, base_dir) is None


@pytest.mark.parametrize(
    ("slice_kwargs", "expected"),
    [
        ({"os_": "yocto", "image": "alp-image-edge"}, ["bitbake", "alp-image-edge"]),
        ({"os_": "off", "app": "./src"}, None),
    ],
)
def test_a_non_baremetal_slice_does_not_get_a_cmake_configure(
        base_dir, slice_kwargs, expected):
    """The control: a `cmake -S ... -B .` hard-wired as this function's only
    return would pass every baremetal assertion above."""
    from tan.planner.orchestrator import _slice_command

    project, _ = baremetal_project(base_dir)
    os_ = slice_kwargs.pop("os_")

    assert _slice_command(
        project, slice_of(os_, **slice_kwargs), base_dir) == expected


# ---------------------------------------------------------------------
# orchestrator.py:463-596 -- the four baremetal-only helpers
# ---------------------------------------------------------------------


def test_the_output_dir_anchors_on_the_project_never_the_cwd(base_dir, tmp_path,
                                                             monkeypatch):
    """Issue #596: a relative `build_dir` resolves against the board.yaml's
    own directory, so the emitted plan is byte-identical wherever it was
    emitted from. The `chdir` is what makes that claim falsifiable --
    without it a `Path(build_dir).resolve()` that silently used `Path.cwd()`
    would agree with `base_dir` whenever the two happened to coincide.
    """
    from tan.planner.orchestrator import _baremetal_output_dir

    monkeypatch.chdir(tmp_path / "..")
    _, slice_ = baremetal_project(base_dir)

    assert _baremetal_output_dir(slice_, base_dir) == (
        base_dir / "build" / "m55_he-baremetal" / "output")


def test_an_absolute_build_dir_passes_through_the_output_dir_untouched(base_dir):
    """The other half: an already-absolute `build_dir` is NOT re-anchored
    under `base_dir`."""
    from tan.planner.orchestrator import _baremetal_output_dir

    elsewhere = (base_dir / "elsewhere" / "m55_he-baremetal").resolve()
    _, slice_ = baremetal_project(base_dir, build_dir=str(elsewhere))

    assert _baremetal_output_dir(slice_, base_dir) == elsewhere / "output"


def test_the_cache_args_keep_only_the_value_bearing_defines(base_dir):
    """`_baremetal_cache_args` splits the slice's `-D` lines on the one thing
    that distinguishes them: `cmake -D` REQUIRES `VAR[:type]=value` and exits
    1 with `Parse error in command line argument: ALP_BOARD_E1M_EVK / Should
    be: VAR:type=value` on anything else.

    This SKU declares `silicon_capabilities.unpopulated`, so
    `_slice_cmake_args` emits a bare `-DALP_SOM_E1M_AEN301` guard alongside
    the `=`-bearing entries -- the exact shape that must NOT reach the
    command line. It reaches the compiler by the other route
    (`-DCMAKE_PROJECT_INCLUDE`), asserted below.
    """
    from tan.planner.orchestrator import _baremetal_cache_args

    project, slice_ = baremetal_project(
        base_dir, sku="E1M-AEN301", board_name="E1M-EVK",
        toolchain="arm-zephyr-eabi", unpopulated=["ethos_u55_count"])

    assert _baremetal_cache_args(project, slice_) == [
        "-DALP_SOM_SKU=E1M-AEN301",
        "-DALP_SOM_FAMILY=aen",
        "-DALP_CORE_ID=m55_he",
        "-DALP_TOOLCHAIN=arm-zephyr-eabi",
    ]


def test_the_project_include_carries_the_bare_guards_as_compile_definitions(base_dir):
    """The complement of the split above, byte-for-byte.

    `add_compile_definitions`, deliberately NOT `-DCMAKE_C_FLAGS=-DALP_...`:
    setting that variable from the command line seeds the cache entry
    itself, so a firmware toolchain file's `CMAKE_C_FLAGS_INIT`
    (`-mcpu=cortex-m55 -mfloat-abi=hard`, ...) would never be applied --
    silently building the slice for the wrong core.
    """
    from tan.planner.orchestrator import _baremetal_project_include

    project, slice_ = baremetal_project(
        base_dir, sku="E1M-AEN301", board_name="E1M-EVK",
        unpopulated=["ethos_u55_count"])

    assert _baremetal_project_include(project, slice_) == (
        PROJECT_INCLUDE_PREAMBLE
        + "add_compile_definitions(ALP_SOM_E1M_AEN301 ALP_BOARD_E1M_EVK)\n")


def test_the_project_include_is_none_when_the_slice_has_no_guards(base_dir):
    """Absence-emits-nothing, and the half that rots silently: an inline
    board with no `preset:`/`name:` and an unrestricted SKU has no
    compile-time guard to carry, so there is no file to write."""
    from tan.planner.orchestrator import _baremetal_project_include

    project, slice_ = baremetal_project(base_dir)

    assert _baremetal_project_include(project, slice_) is None


def test_the_project_include_arg_is_absolute_and_tokened(base_dir):
    """CMake resolves a relative `CMAKE_PROJECT_INCLUDE` against the SOURCE
    dir of the `project()` that pulls it in -- the app's tree, not the
    slice's build dir -- so this one has to be absolute, and tokened
    (#865) so a plan emitted on one checkout materialises faithfully on
    another."""
    from tan.planner.orchestrator import _baremetal_project_include_arg

    project, slice_ = baremetal_project(base_dir, board_name="E1M-EVK")

    assert _baremetal_project_include_arg(project, slice_, base_dir) == [
        "-DCMAKE_PROJECT_INCLUDE="
        "${PROJECT_ROOT}/build/m55_he-baremetal/alp-baremetal.cmake"]


def test_the_project_include_arg_is_empty_when_there_are_no_guards(base_dir):
    """No dangling `-DCMAKE_PROJECT_INCLUDE=` pointing at a file
    `_slice_config_artefact` would not have written -- the two answer the
    same question from opposite ends and must agree."""
    from tan.planner.orchestrator import _baremetal_project_include_arg

    project, slice_ = baremetal_project(base_dir)

    assert _baremetal_project_include_arg(project, slice_, base_dir) == []
