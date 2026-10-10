# SPDX-License-Identifier: Apache-2.0
"""tan-cli#1446: the `--raw` interlock must not break `import tan.cli` where POSIX `pwd`/`grp`
are missing (Windows). No POSIX-only imports here: this module runs on every CI platform."""
from __future__ import annotations

import subprocess
import sys
import textwrap


def test_pwd_and_grp_are_imported_lazily_and_the_tool_still_imports_without_them():
    """CI runs Windows/macOS: a top-level `import pwd` would break `import tan.cli` there."""
    import subprocess
    import sys
    import textwrap

    code = textwrap.dedent("""
        import sys
        sys.modules['pwd'] = None
        sys.modules['grp'] = None
        import tan.cli
        from tan.commands import flash_raw
        print(flash_raw._platform_refusal())
        print(flash_raw._reservation_refusal('p', '/x', '3-4.2')[0])
    """)
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)
    assert out.returncode == 0, out.stderr
    first, second = out.stdout.strip().splitlines()[:2]
    assert first.startswith("unsupported on this platform") and "unsupported on this platform" in second
