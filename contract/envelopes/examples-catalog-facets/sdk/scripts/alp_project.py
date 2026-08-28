# SPDX-License-Identifier: Apache-2.0
# Marker file only: `has_loader_script` (tan-cli src/util.rs) treats a checkout
# with `scripts/alp_project.py` as a valid --sdk-root. Never executed by this
# case -- `tan examples` reads metadata/ and examples/ off disk and spawns no
# Python.
