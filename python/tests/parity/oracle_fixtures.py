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
import sys
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

#: The OS every fixture in this store was captured on -- ``oracle_fixtures/
#: PROVENANCE.txt``'s own ``platform:`` line, mirrored here as data rather
#: than left for each call site to hardcode separately. A frozen answer is
#: only a valid oracle for a REPLAY on this same OS: a captured envelope can
#: carry this host's native path separators (``buildRoot``, per-slice paths,
#: ...), and comparing that against a DIFFERENT platform's live output diffs
#: the two platforms' genuinely different -- and both correct -- behaviour,
#: not a port defect. Not applied automatically by :func:`resolve` -- most
#: fixtures carry no host-shaped data at all (an issue code, a bool, a
#: version string) and stay portable everywhere; a call site whose compared
#: surface DOES carry native separators opts in itself (see
#: ``test_clean_parity.py`` for the worked example) rather than every fixture
#: silently losing non-Windows coverage.
CAPTURE_PLATFORM = "win32"

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


#: True when THIS replay's separator style is the one the fixture store was
#: captured with, so a byte-for-byte diff is meaningful and
#: :func:`normalise_scrubbed_path_separators` must stay a no-op. Two ways to
#: be true: replaying on :data:`CAPTURE_PLATFORM` itself (Windows CI), or
#: running ``TAN_PARITY_LIVE=1``, where both binaries are spawned on the SAME
#: host and any separator disagreement between them is a REAL divergence
#: rather than a capture artefact.
REPLAY_IS_CAPTURE_PLATFORM = LIVE or sys.platform == CAPTURE_PLATFORM

#: The two placeholder spellings this suite's root-redaction produces:
#: ``<ORACLE-ROOT-N>`` from :func:`scrub` (position-keyed, however many roots a
#: call site passes) and the bare ``<ROOT>`` from ``test_clean_parity._scrub``,
#: which predates :func:`scrub` and redacts exactly one tree root. Both mean
#: the same thing -- "a scratch directory this harness created and then
#: redacted" -- so both open the same host-shaped region. Listed here rather
#: than parameterised per call site so a third spelling cannot appear without
#: landing next to these two.
_ROOT_TOKEN = r"<(?:ORACLE-ROOT-\d+|ROOT)>"

#: The region a placeholder opens: the token, then everything up
#: to the next quote or newline. Deliberately NOT bounded at whitespace, even
#: though a bare ``data.buildRoot`` ends there -- the surface this has to reach
#: is mostly free text that EMBEDS a path, and a path segment can legally
#: contain a space. Both shapes are real, in the same fixture store::
#:
#:     no footprint source at <ORACLE-ROOT-0>\br\with space-zephyr\zephyr\...
#:     would run dd if=<ORACLE-ROOT-0>\.\build\a.wic of=/dev/sdb bs=4M
#:
#: The first needs the space to CONTINUE the run, the second needs it to end
#: it; nothing lexical separates the two. Running greedily to the quote (``dd:
#: failed to open '<ORACLE-ROOT-0>\.\build\a.elf': ...``) resolves it in the
#: only direction that keeps both correct, and over-running costs nothing
#: wherever the tail carries no ``\`` to rewrite -- which is every case
#: measured. The residual: a message that pairs a redacted path with an
#: UNRELATED backslash inside the same quote-delimited region would have that
#: one rewritten too. A string with no token at all is never touched.
_SCRUBBED_PATH_TAIL_RE = re.compile(_ROOT_TOKEN + r"[^'\"\n]*")


def normalise_scrubbed_path_separators(payload: Any, *, force: bool = False) -> Any:
    """``\\`` -> ``/``, but ONLY inside a path anchored at a :func:`scrub`
    placeholder, and ONLY when this replay is not on the capture platform
    (:data:`REPLAY_IS_CAPTURE_PLATFORM`) -- or when *force* overrides that.

    ``force=True`` is for a key captured on a host that is NOT
    :data:`CAPTURE_PLATFORM` (tan-cli#409 added the first ones: the
    command-surface cases, captured on darwin). The automatic rule reads the
    store as single-platform and disables itself on win32, which is right for
    the win32-captured majority and exactly backwards for a POSIX-captured
    key -- that one records ``/`` and a Windows replay's live port answers
    ``\\``, with nothing left to reconcile them. Scoped per call site rather
    than flipped globally, so it reaches only the keys whose provenance says
    it should; ``PROVENANCE.txt`` records which those are.

    A path rooted at ``<ORACLE-ROOT-N>`` is, by construction, a scratch
    directory the harness itself created and then redacted. What separator
    joins its segments is the RECORDING host's, not a behaviour of either
    binary: a fixture frozen on win32 (tan-cli#272) says ``<ORACLE-ROOT-0>\\.
    \\build``, a live port on ubuntu/macOS says ``<ORACLE-ROOT-0>/./build``,
    and both are correct on their own host. Diffing those two byte-for-byte
    reports a platform difference as a port defect.

    Deliberately narrower than the blanket separator-flattening
    ``test_clean_parity.py`` refuses, and narrower than a key allow-list
    (:data:`oracle.PATH_KEYS`) can be: the anchor is the redaction token, not
    a field name, so it reaches ``issues[].message`` and
    ``data.entries[].message`` -- where the real mismatch lives -- without
    granting those free-text fields separator-insensitivity anywhere else in
    their content. A ``\\`` that is not in a redacted-root path still fails
    the diff.

    What this gives up, stated plainly: on a NON-capture platform it can no
    longer catch the two binaries rendering a workspace-rooted path with
    different separators. That is not a loss against the alternative --
    skipping the case, which is what the platform gate this replaces did --
    because a fixture frozen on win32 cannot observe that divergence on POSIX
    either way. On the capture platform, and under ``TAN_PARITY_LIVE=1``,
    the comparison stays byte-exact and the divergence is still caught.

    Accepts a JSON-serializable value or a plain ``str`` (captured stderr),
    like :func:`scrub`; returns the same shape it was given.

    tan-cli#272
    """
    if REPLAY_IS_CAPTURE_PLATFORM and not force:
        return payload
    if isinstance(payload, str):
        return _SCRUBBED_PATH_TAIL_RE.sub(lambda m: m.group(0).replace("\\", "/"), payload)
    if isinstance(payload, list):
        return [normalise_scrubbed_path_separators(item, force=force) for item in payload]
    if isinstance(payload, dict):
        return {k: normalise_scrubbed_path_separators(v, force=force) for k, v in payload.items()}
    return payload
