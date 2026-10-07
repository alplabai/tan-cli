# SPDX-License-Identifier: Apache-2.0
"""tan-cli#1312 test seam for SUBPROCESS `tan flash` runs: no J-Link is
visible, whatever the host has plugged in. Loaded only because
`run_flash` (tests/commands/test_flash_command.py) puts this directory on the
child's PYTHONPATH -- tan itself has no environment switch that hides probes."""
from tan.commands import flash_cmd

flash_cmd.enumerate_jlinks = lambda: []
