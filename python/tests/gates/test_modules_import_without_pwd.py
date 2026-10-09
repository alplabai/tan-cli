# SPDX-License-Identifier: Apache-2.0
"""Every `tan.commands` / `tan.core` module (and `tan.cli`) must import where
`pwd`/`grp` do not exist (Windows). A top-level `import pwd` in one of them breaks
`import tan.cli`, i.e. EVERY command, on windows-latest (tan-cli#1452 review)."""
from __future__ import annotations

import subprocess
import sys
import textwrap

_SCRIPT = textwrap.dedent(
    """
    import importlib, pkgutil, sys
    sys.modules["pwd"] = None
    sys.modules["grp"] = None
    bad = []
    names = ["tan.cli"]
    for pkg in ("tan.commands", "tan.core"):
        mod = importlib.import_module(pkg)
        names += [m.name for m in pkgutil.walk_packages(mod.__path__, pkg + ".")]
    for name in names:
        try:
            importlib.import_module(name)
        except ImportError as err:
            if "pwd" in str(err) or "grp" in str(err):
                bad.append(f"{name}: {err}")
        except Exception:
            pass  # unrelated import-time requirements are not this gate's business
    print("\\n".join(bad))
    sys.exit(1 if bad else 0)
    """
)


def test_no_module_needs_pwd_or_grp_at_import_time(tmp_path):
    done = subprocess.run(
        [sys.executable, "-c", _SCRIPT], cwd=tmp_path, capture_output=True, text=True, timeout=300
    )
    assert done.returncode == 0, done.stdout + done.stderr
