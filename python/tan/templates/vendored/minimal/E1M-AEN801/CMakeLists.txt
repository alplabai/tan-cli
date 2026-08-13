# SPDX-License-Identifier: Apache-2.0

cmake_minimum_required(VERSION 3.20)

# Resolve the alp-sdk root.  This project lives OUTSIDE the SDK
# tree, so there is nothing to guess: ALP_SDK_ROOT must name your
# alp-sdk checkout, set in the environment or passed as
# `-DALP_SDK_ROOT=/path/to/alp-sdk`.
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

# Layer generated CONFIG_* over prj.conf via EXTRA_CONF_FILE.
list(APPEND EXTRA_CONF_FILE ${_alp_generated})

find_package(Zephyr REQUIRED HINTS $ENV{ZEPHYR_BASE})
project(hello_world LANGUAGES C)

target_sources(app PRIVATE src/main.c)
