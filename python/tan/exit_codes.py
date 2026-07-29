# SPDX-License-Identifier: Apache-2.0
"""Stable process exit codes, fixed by the CLI output contract."""
from enum import IntEnum


class ExitCode(IntEnum):
    SUCCESS = 0
    RUNTIME_FAILURE = 1
    VALIDATION_FAILURE = 2
    WRITE_FAILURE = 3
    DOCTOR_FAILURE = 4
    INTERNAL_FAILURE = 5
