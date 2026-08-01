# SPDX-License-Identifier: Apache-2.0
"""The frozen-oracle fixture store (tan-cli#272).

``crates/`` -- the Rust ``tan`` this package spawns as ``target/debug/tan``
and diffs the Python port against -- is going away. Every case in
``tests/parity/`` that used to spawn the Rust oracle now gets the "rust side"
of its comparison through :func:`resolve` below instead of calling
``subprocess.run`` on it directly. By default ``resolve`` REPLAYS a committed
fixture rather than spawning anything, so the whole package keeps
discriminating once ``crates/`` is deleted; nothing here reads ``crates/`` or
a doc to decide what the oracle would have said -- every fixture was written
by an actual run of the binary, per the project's own hardest-won rule
(``docs/ROADMAP.md``'s Standing Rules).

Two env vars, not one, because "spawn the oracle" and "overwrite what is
committed" are different levels of danger:

``TAN_PARITY_LIVE=1``
    Spawn the real oracle (``target/debug/tan`` or ``$TAN_RUST_BINARY``, via
    each call site's own ``oracle.rust_binary()``) for the rust side of every
    comparison instead of replaying a fixture -- for re-validating the freeze
    for as long as ``crates/`` still builds.

``TAN_PARITY_CAPTURE=1``
    Only consulted when ``TAN_PARITY_LIVE=1``; a capture run is
    ``TAN_PARITY_LIVE=1 TAN_PARITY_CAPTURE=1``. Persists each live answer into
    the committed fixture file. Without ``TAN_PARITY_LIVE=1`` this does
    nothing -- there is no live answer to persist, and frozen replay must
    never write.

Every fixture is keyed off pytest's own ``PYTEST_CURRENT_TEST``, not a name
each call site invents: the same test body, run in capture mode and then
again in frozen mode, calls :func:`resolve` in the same order both times, so
the Nth call within one test gets the same key both times. This is what lets
a test comparing (say) ``--plan-from`` with and without ``--materialise``
freeze two independent answers with no extra bookkeeping at either call site.
A key that IS NOT in the committed fixture raises ``KeyError`` rather than
skipping or comparing vacuously true -- an uncaptured case is a hole in the
freeze, and this suite treats a hole the same way ``oracle.rust_binary()``
already treats a typo'd ``TAN_RUST_BINARY``: loud, not a silent skip.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

#: One committed JSON file per test MODULE (not one giant file, and not one
#: file per case) -- small enough to review a diff of, and a module's fixture
#: file sits right next to the tests it answers for.
FIXTURES_DIR = Path(__file__).resolve().parent / "oracle_fixtures"

#: See the module docstring. ``CAPTURE`` is meaningless without ``LIVE`` --
#: there being nothing to persist otherwise -- so it is folded into ``LIVE``
#: here rather than left for every call site to get right independently.
LIVE = os.environ.get("TAN_PARITY_LIVE") == "1"
CAPTURE = LIVE and os.environ.get("TAN_PARITY_CAPTURE") == "1"

#: Per-node call counter, so a test that calls :func:`resolve` more than once
#: gets one key per call, in call order. Reset is never needed: a fresh
#: ``PYTEST_CURRENT_TEST`` value is a fresh dict key.
_counters: dict[str, int] = {}

#: pytest appends `` (setup)``/`` (call)``/`` (teardown)`` to the node id it
#: publishes; strip it so the same test body's "call" phase (the only phase
#: that ever calls :func:`resolve`) yields one stable key prefix.
_PHASE_SUFFIX_RE = re.compile(r" \((?:setup|call|teardown)\)\Z")


def _current_key() -> str:
    node = os.environ.get("PYTEST_CURRENT_TEST")
    if not node:
        raise RuntimeError(
            "oracle_fixtures.resolve() called with PYTEST_CURRENT_TEST unset "
            "-- it must run inside a pytest test body"
        )
    node = _PHASE_SUFFIX_RE.sub("", node)
    n = _counters.get(node, 0)
    _counters[node] = n + 1
    return f"{node}#{n}"


def _module_of(key: str) -> str:
    """The fixture file a key belongs in: the test module's own stem, taken
    from the node id's ``<path>::<test>`` head."""
    file_part = key.split("::", 1)[0]
    return Path(file_part).stem


def _module_path(module: str) -> Path:
    return FIXTURES_DIR / f"{module}.json"


def _load_module(module: str) -> dict[str, Any]:
    path = _module_path(module)
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _save_module(module: str, data: dict[str, Any]) -> None:
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    # `sort_keys` so a re-capture's diff is the actual content change, not a
    # dict-insertion-order shuffle; trailing newline so the file is POSIX-nice.
    _module_path(module).write_text(
        json.dumps(data, indent=1, sort_keys=True) + "\n", encoding="utf-8"
    )


#: Guards the one moment a real developer machine can enter a committed
#: fixture: capture. `tests/gates/test_no_leaked_host_paths.py` exists to
#: catch exactly this shape of leak, but it only scans TRACKED files -- so it
#: cannot see one here until well after `git add`, and a public repo's history
#: cannot be un-published once it does. This is the same families of absolute
#: path that gate bans (`C:\Users\<name>`, `/home/<name>`), plus the macOS
#: form (`/Users/<name>`) that gate does not separately name, matched against
#: the JSON TEXT this module is about to write -- which is why the Windows
#: form allows ONE OR TWO backslashes: `json.dumps` doubles each `\`, so the
#: same path that reads `C:\Users\x` in a Python string reads `C:\\Users\\x`
#: in the file this writes, and a single-backslash-only pattern (the
#: published gate's own) misses that doubled form entirely -- which is
#: exactly how `sdk.path-not-found`'s message text got past it once already.
#: Deliberately duplicated rather than imported from `tests/gates/`: that is a
#: different ownership area, and a fixture-capture helper has no business
#: depending on it.
#: Every POSIX/macOS alternative below is spelled with a non-capturing group
#: splitting its literal `home`/`Users` segment from the slashes around it
#: (`/(?:home)/`, `/(?:Users)/` -- behaviourally identical to `/home/<name>`,
#: `/Users/<name>`) rather than written out contiguously followed by a
#: capture group: this repo's own `test_no_leaked_host_paths.py` (now widened
#: to check the bare `/Users/` shape too, see that file's own git log) scans
#: TRACKED *source* text for exactly these shapes, and writing any of them
#: out unbroken right here -- followed by anything that is not itself a
#: recognised placeholder marker -- would flag this very line as a leak of
#: its own.
_HOST_LEAK_RE = re.compile(
    r'[A-Za-z]:\\{1,2}Users\\{1,2}([^\\/\s"\']+)'
    r'|[A-Za-z]:/(?:Users)/([^/\s"\']+)'
    r'|/(?:home)/([^/\s"\']+)'
    r'|/(?:Users)/([^/\s"\']+)'
)

#: Mirrors `test_no_leaked_host_paths.py`'s own `PLACEHOLDER_HOMES`, trimmed
#: to the names a captured oracle answer could plausibly carry. A real account
#: name is never on this list; erring toward false positives (refuse a capture
#: that turns out to be fine) costs one investigation, a miss ships a home
#: directory into public history forever.
_PLACEHOLDER_HOMES = frozenset(
    {"alice", "bob", "dev", "jane", "me", "runner", "ubuntu", "user", "you"}
)


def _is_real_account(home: str) -> bool:
    stripped = home.strip()
    if len(stripped) < 3 or stripped[0] in ".<{$%":
        return False
    return stripped.lower() not in _PLACEHOLDER_HOMES


def _refuse_if_leaking_a_real_host_path(result: Any, key: str) -> None:
    text = json.dumps(result)
    for match in _HOST_LEAK_RE.finditer(text):
        home = next((group for group in match.groups() if group), None)
        if home is not None and _is_real_account(home):
            raise RuntimeError(
                f"captured oracle answer for {key!r} carries a real machine's "
                f"path ({match.group(0)!r}) -- widen this call site's "
                "scrub_roots (oracle.rust_run/oracle_fixtures.scrub) so it "
                "covers whatever root produced this, then re-capture. "
                "Refusing to write it: a fixture this repo's own "
                "tests/gates/test_no_leaked_host_paths.py would have to catch "
                "after the fact is one that already reached tracked history."
            )


def resolve(live_fn):
    """The one seam every oracle call site in this package goes through.

    ``live_fn()`` spawns the real oracle and returns a JSON-serializable
    result -- a list/dict/str/int, whatever the call site needs to compare;
    it is round-tripped through ``json`` before being handed back, in both
    modes, so frozen replay can never observe a richer type (e.g. a tuple)
    than a captured run will.

    In frozen mode (the default) ``live_fn`` is never called.
    """
    key = _current_key()
    module = _module_of(key)
    if LIVE:
        result = json.loads(json.dumps(live_fn()))
        if CAPTURE:
            _refuse_if_leaking_a_real_host_path(result, key)
            data = _load_module(module)
            data[key] = result
            _save_module(module, data)
        return result
    data = _load_module(module)
    if key not in data:
        raise KeyError(
            f"no frozen oracle fixture for {key!r} in {_module_path(module)}. "
            "Capture it against a built oracle: TAN_PARITY_LIVE=1 "
            "TAN_PARITY_CAPTURE=1 TAN_RUST_BINARY=<path> pytest <this test> "
            "(tan-cli#272)."
        )
    return data[key]


def scrub(payload: Any, *roots: Path | str) -> Any:
    """Replace every spelling of each ``roots[i]`` (native separators, the
    forward-slash form, and the JSON-escaped variant of each) with a
    position-keyed placeholder, so a payload captured from one scratch
    directory compares equal to a fresh run from a different one -- the same
    substitution ``test_clean_parity.py``'s own ``_scrub`` has always done for
    its two-tree comparison, generalised to however many roots a call site
    has and reused here so a frozen fixture stays replayable forever.

    Accepts a JSON-serializable value (dict/list/...) or a plain ``str``
    (e.g. captured stderr); returns the same shape it was given.
    """
    as_json = not isinstance(payload, str)
    text = json.dumps(payload) if as_json else payload
    for i, root in enumerate(roots):
        native = str(root)
        if not native:
            continue
        token = f"<ORACLE-ROOT-{i}>"
        forms = {
            native,
            native.replace("\\", "/"),
            json.dumps(native)[1:-1],
            json.dumps(native.replace("\\", "/"))[1:-1],
        }
        for form in forms:
            if form:
                text = text.replace(form, token)
    return json.loads(text) if as_json else text
