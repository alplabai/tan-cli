# SPDX-License-Identifier: Apache-2.0
"""Parity-suite conftest. The oracle pin no longer lives here.

``pinned_oracle`` -- the session-scoped autouse check that whatever
``target/{release,debug}/tan`` :func:`oracle.rust_binary` resolved really is
:data:`oracle.PINNED_ORACLE_VERSION`, and not a stale profile standing in for
it -- moved UP to ``python/tests/conftest.py`` in tan-cli#393. Nothing about
its reasoning changed; only its reach did.

It was written here because the four files that bind ``RUST = rust_binary()``
at import and gate on ``missing_for_live`` (an ABSENCE check only, no version
assertion) all live in this directory: ``test_flash_oracle_parity.py``,
``test_image_size_oracle.py``, ``test_clean_parity.py`` and
``test_run_oracle_parity.py``. Under ``TAN_PARITY_LIVE=1`` against a
resolved-but-wrong binary, their failures read as a wall of PORT bugs with
nothing naming the oracle -- measured: ``TAN_RUST_BINARY=<the stale 0.1.1
build>`` over just ``test_image_size_oracle.py`` + ``test_clean_parity.py``
produced "108 failed, 8 passed, 9 skipped, 2 xfailed", eight of those passes
measured against the wrong oracle entirely.

What that reasoning missed is that ``tests/commands/`` has oracle consumers
too -- ``test_pinmux_command.py`` and ``test_diff_command.py`` -- and a
conftest in this directory cannot reach them. Both had hand-copied a
release-first resolver that :func:`oracle.rust_binary` had already been
rewritten to remove, so on a tree with both profiles built and ``release``
older, one went red against ``tan 0.3.1`` with a ``project.boardYaml`` diff
that read as a port regression while the other stayed GREEN measuring a
divergence against that same stale binary. Neither could be netted from here.

At the tree root the fixture is inherited by every module in the suite, so a
future oracle consumer anywhere under ``tests/`` is covered by default rather
than by remembering to put it in the right directory.
"""
