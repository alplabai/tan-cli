# SPDX-License-Identifier: Apache-2.0
# Marker file only: `_has_loader` (python/tan/commands/build_output.py) treats a
# checkout carrying `scripts/alp_project.py` as a valid --sdk-root. Never
# executed by this case -- `model build`/`model check` refuse or complete on
# board.yaml's `models:` list alone and spawn no Python.
