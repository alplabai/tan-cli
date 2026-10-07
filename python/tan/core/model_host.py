# SPDX-License-Identifier: Apache-2.0
"""What the host-tier `tan model prep` / `run` / `ab` need installed
(tan-cli#1287).

Their numeric dependencies (`numpy`, `onnxruntime`, `onnx`, `sympy`) live in the
optional `model` extra, never in the base install and never in the frozen
release assets (`.[monitor]` only): onnxruntime ships no macOS x86_64 wheel and
adds tens of MB for a verb most users never run. A missing one is a coded
refusal naming `pip install "tan-cli[model]"`, the `monitor.pyserial-missing`
pattern -- found with `importlib.util.find_spec`, so the check itself imports
nothing heavy.
"""

from __future__ import annotations

import importlib.util

#: Modules each verb imports (lazily) from the `model` extra.
REQUIRED_MODULES = {
    "prep": ("numpy", "onnxruntime", "onnx", "sympy"),
    "run": ("numpy", "onnxruntime"),
    "ab": ("numpy", "onnxruntime"),
}

#: The one install hint every refusal quotes.
INSTALL_HINT = 'pip install "tan-cli[model]"'


def missing_extra_modules(verb: str) -> list[str]:
    """The `verb`'s extra modules that are not importable, in declared order."""
    return [m for m in REQUIRED_MODULES[verb] if importlib.util.find_spec(m) is None]


def extra_missing_message(verb: str, missing: list[str]) -> str:
    return (
        f"`tan model {verb}` needs the optional `model` extra ({', '.join(missing)} not "
        f"installed). Install it with `{INSTALL_HINT}`. A frozen `tan` binary does not "
        "bundle it; use a pip-installed tan for this command."
    )
