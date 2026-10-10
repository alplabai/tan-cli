# SPDX-License-Identifier: Apache-2.0
"""Repo-wide pytest plugin activation.

`pytest_plugins` must live at the ROOTDIR-level conftest.py (this file, next
to `pyproject.toml`) -- pytest 9 hard-errors on it anywhere else ("Defining
'pytest_plugins' in a non-top-level conftest is no longer supported"),
declaring the whole tree's session-wide effect from a nested file it did not
expect to affect the whole tree.

`pytester` activates pytest's own builtin testing-of-pytest fixture, unused
by default -- `tests/gates/test_home_preflight.py` uses it to run a REAL,
subprocess pytest session against the actual `pytest_configure` hook in
`tests/conftest.py` (the tan-cli#903 HOME pre-flight), rather than
re-implementing what a `pytest.UsageError` does to a session.

`tests.parity._xdist_breadth_floor` is registered HERE, not from a
`tests/parity/conftest.py`, for the same "ROOTDIR-level" reason -- see that
module's own docstring (tan-cli#1256 BLOCKER 1): the xdist CONTROLLER only
loads a subdirectory's `conftest.py` when the invocation path actually
points into that subdirectory, so `pytest -n N tests` (or no path at all)
never loaded a `tests/parity/conftest.py`'s controller-side hooks even
though every WORKER still did, and the breadth floor went unenforced with
no error at all. THIS file is loaded unconditionally for every invocation
regardless of path, so registering the plugin from here closes that gap for
every path shape, not just `tests/parity` itself.
"""
pytest_plugins = ["pytester", "tests.parity._xdist_breadth_floor"]
