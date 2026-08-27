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
"""
pytest_plugins = ["pytester"]
