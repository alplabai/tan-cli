# SPDX-License-Identifier: Apache-2.0
"""`tan.core.alp_conf_pregen` (alp-sdk#866): which `tan generate` command
writes the `generated/alp.conf` a copied example reads."""

from __future__ import annotations

from tan.core.alp_conf_pregen import pregeneration_message
from tan.core.scaffold import PlannedFile

_READS = "extra_args: EXTRA_CONF_FILE=generated/alp.conf CONFIG_X=y\n"


def test_one_command_per_reading_directory_with_its_own_core():
    files = [
        PlannedFile("testcase.yaml", _READS),
        PlannedFile("peer/testcase.yaml", _READS),
        PlannedFile("peer/CMakeLists.txt", "project(peer)\n"),
    ]
    cores = {"m55_hp": {"app": "./src"}, "m55_he": {"app": "./peer"}}

    message = pregeneration_message(files, cores, "/sdk", "example 'x'")

    assert message is not None
    assert (
        "Write it with: `tan generate --target zephyr-conf --core m55_hp --sdk-root /sdk "
        "--output generated/alp.conf`; `tan generate --target zephyr-conf --core m55_he "
        "--sdk-root /sdk --output peer/generated/alp.conf`."
    ) in message
    assert message.startswith("example 'x' reads generated/alp.conf (peer/testcase.yaml, testcase.yaml)")


def test_an_ambiguous_directory_gets_a_placeholder_not_a_guess():
    files = [PlannedFile("testcase.yaml", _READS)]
    cores = {"m55_hp": {"app": "./src"}, "m55_he": {"app": "./src"}}

    message = pregeneration_message(files, cores, "/sdk", "example 'x'")

    assert "--core <core-id> --sdk-root /sdk --output generated/alp.conf" in message


def test_the_per_image_spelling_and_other_paths_are_not_readers():
    files = [
        PlannedFile("a.yaml", "extra_args: m55_hp_EXTRA_CONF_FILE=generated/alp.conf\n"),
        PlannedFile("b.yaml", "extra_args: EXTRA_CONF_FILE=generated/alp.conf.bak\n"),
        PlannedFile("c.yaml", "extra_args: EXTRA_CONF_FILE=native_sim.conf\n"),
    ]
    assert pregeneration_message(files, None, "/sdk", "example 'x'") is None
