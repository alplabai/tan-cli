# SPDX-License-Identifier: Apache-2.0
"""tan's OWN copy of the SDK scaffold trees `tan init` lays down.

``vendored/`` is ``alp-sdk --emit scaffold`` output captured byte-for-byte (LF,
no retouching) -- a verbatim copy of ``crates/tan-core/src/wizard/vendored/``,
which the Rust binary bakes in with ``include_str!``. It is checked in for the
same reason it is checked in there: **I-32 -- `tan init` is SDK-free.** A
customer's first command must work with no alp-sdk checkout anywhere on the
machine, so the scaffold content cannot be fetched, and shelling the SDK's
scaffold emit would hand `tan init` a dependency it deliberately does not have.

Two consequences worth stating where the bytes live:

* **Re-vendor by re-running the emit, never by hand-editing these files.**
  ``vendored/MANIFEST.md`` carries the exact source commit and the template x
  SKU matrix. ``tests/core/test_scaffold.py`` byte-diffs this tree against the
  Rust one, so a re-vendor that updates only one language fails there.
* **LF is load-bearing.** These bytes are written to the customer's files
  verbatim, and the byte-diff above compares them to an LF capture. The repo
  root's ``.gitattributes`` pins ``text eol=lf`` for this path; without it a
  Windows checkout with ``autocrlf=true`` silently rewrites every one.

The tree ships in the frozen binary via ``--add-data`` (``scripts/
build_binary.sh``). It is data, not code, so PyInstaller's static import graph
does NOT pull it in on its own -- when it is missing, `tan init` reports
``init.template-unreadable`` rather than raising (``tan.core.scaffold``).
"""
from pathlib import Path

#: Root of the vendored scaffold trees: ``vendored/<sdk-template-id>/<SKU>/``.
#: Derived from ``__file__`` so it resolves both from source and from a frozen
#: onefile bundle, where PyInstaller sets ``__file__`` under ``sys._MEIPASS``
#: and ``--add-data`` has placed the tree at the same package-relative path.
VENDORED_ROOT = Path(__file__).resolve().parent / "vendored"
