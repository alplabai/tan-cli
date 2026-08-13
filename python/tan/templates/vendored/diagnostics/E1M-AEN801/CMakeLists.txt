# SPDX-License-Identifier: Apache-2.0

cmake_minimum_required(VERSION 3.20)

if(NOT DEFINED ALP_SDK_ROOT AND NOT DEFINED ENV{ALP_SDK_ROOT})
    message(FATAL_ERROR
        "ALP_SDK_ROOT is not set -- point it at your alp-sdk checkout, "
        "e.g. `export ALP_SDK_ROOT=/path/to/alp-sdk` or `-DALP_SDK_ROOT=/path/to/alp-sdk`.")
endif()
if(NOT DEFINED ALP_SDK_ROOT)
    set(ALP_SDK_ROOT $ENV{ALP_SDK_ROOT})
endif()

find_package(Python3 REQUIRED COMPONENTS Interpreter)

set(_alp_generated ${CMAKE_BINARY_DIR}/generated/alp.conf)
execute_process(
    COMMAND ${Python3_EXECUTABLE} ${ALP_SDK_ROOT}/scripts/alp_project.py
            --input ${CMAKE_CURRENT_SOURCE_DIR}/board.yaml
            --emit zephyr-conf --core m55_hp
            --output ${_alp_generated}
    RESULT_VARIABLE _alp_rv
    OUTPUT_VARIABLE _alp_stdout
    ERROR_VARIABLE  _alp_stderr
)
if(NOT _alp_rv EQUAL 0)
    message(FATAL_ERROR
        "alp_project.py failed (rv=${_alp_rv}); "
        "check ${CMAKE_CURRENT_SOURCE_DIR}/board.yaml.\n"
        "stderr: ${_alp_stderr}")
endif()

# PREPEND, not APPEND (tan-cli#379, tan-cli#501). Zephyr merges the
# EXTRA_CONF_FILE list in order and the last assignment of a symbol wins, so
# appending the GENERATED alp.conf parks it after the `boards/
# native_sim_native_64.conf` Zephyr auto-discovers for this board (CONFIG_EMUL,
# CONFIG_I2C_EMUL) -- backwards for a generated baseline: this template ships
# that board conf to turn the emulated I2C bus on for native_sim, and could
# not if the generated file were free to override it. Prepending keeps
# alp.conf winning over prj.conf (the whole list is merged after it) while
# letting the board-specific conf win, which is what this README's native_sim
# build already claims happens.
list(PREPEND EXTRA_CONF_FILE ${_alp_generated})

find_package(Zephyr REQUIRED HINTS $ENV{ZEPHYR_BASE})
project(board_selftest LANGUAGES C)

target_sources(app PRIVATE src/main.c)
