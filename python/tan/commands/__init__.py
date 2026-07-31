# SPDX-License-Identifier: Apache-2.0
"""Command modules.

Register every command with a STATIC import (``from tan.commands.x import y``).
PyInstaller follows the static import graph only, so an ``importlib``/``pkgutil``
auto-discovery registry would work from source and produce a frozen `tan` that
cannot find its own commands -- see ``scripts/build_binary.sh``.
"""
