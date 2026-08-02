# SPDX-License-Identifier: Apache-2.0
"""Diff the Python ``tan clean`` against the shipped Rust ``tan`` on identical
trees. Rust is authoritative: a divergence is a port bug until proven otherwise.

**This module is the only oracle ``clean`` has.** ``contract/README.md`` freezes
no ``clean`` fixture, so there is no golden to check the port against -- the
evidence has to be produced here, by running both binaries.

``oracle.compare`` cannot be reused as-is: it runs both implementations in the
SAME cwd, and ``clean`` DELETES, so whichever side ran first would leave the
other nothing to do and every destructive case would "match" vacuously. Each
case therefore builds two byte-identical trees, runs one implementation in each,
and compares three things:

* the exit code;
* the whole stdout envelope, with the tree root textually scrubbed out (the two
  trees necessarily live at different paths) -- so ``data.buildRoot``, every
  ``targets[].path``, ``project.*`` and ``sdk.*`` are all compared. Separator
  style included ON the capture platform and under ``TAN_PARITY_LIVE=1``; on a
  frozen replay elsewhere the separators INSIDE the redacted root are
  normalised first (``oracle_fixtures.normalise_scrubbed_path_separators``),
  because a fixture frozen on ``CAPTURE_PLATFORM`` carries that host's joins
  (``"<ROOT>/proj\\build"``) and diffing those against a live POSIX run reports
  a platform difference as a port defect. Everything else in the envelope is
  still byte-for-byte on every platform;
* the SURVIVING FILE SET. An envelope that reads correctly is not evidence that
  nothing else was deleted, and on this command that is the only claim that
  matters.

Every tree carries a ``CANARY.txt`` and a ``precious/`` directory OUTSIDE the
build root, and both are asserted intact on BOTH sides of every case -- including
the cases whose whole point is that the command refuses. No case names
``precious/`` as a removal target, so that assertion is never legitimately
violated; the out-of-tree removal case uses a separate ``oot/`` instead.

**Nothing in this module deletes anything outside a pytest ``tmp_path``.** An
earlier draft carried a ``teardown_module`` that derived a cleanup path from
``PYTEST_CURRENT_TEST`` and recursively removed it; run once, it deleted this
very directory. pytest already prunes its own ``tmp_path`` roots (and its
``rm_rf`` clears the read-only bit the ``read-only-artefact`` case plants), so
there is nothing here to clean up by hand -- and a test module for a destructive
command is the last place to hand-roll a recursive delete.

Two message divergences are declared in [`_normalise_issues`] rather than
globally muted; see that function for why neither is reproducible in Python.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from . import oracle_fixtures
from .oracle import PACKAGE_ROOT, missing_for_live, python_command, rust_binary

RUST = rust_binary()
WINDOWS = os.name == "nt"

pytestmark = pytest.mark.skipif(
    missing_for_live(RUST),
    reason="TAN_PARITY_LIVE=1 needs a Rust tan; set TAN_RUST_BINARY or run `cargo build`",
)


def _manifest(build_dir: str | None, core_id: str = "m55_hp") -> str:
    body = f"schema_version: 1\nslices:\n- core_id: {core_id}\n  os: zephyr\n  status: ok\n"
    if build_dir is not None:
        body += f'  build_dir: "{build_dir}"\n'
    return body


def _link(link: Path, target: Path) -> None:
    """A Windows JUNCTION, or a POSIX directory symlink.

    A junction rather than a symlink on Windows deliberately: creating a symlink
    needs Developer Mode or elevation while a junction does not, and a junction
    is the shape ``os.path.islink`` silently misses (see ``clean_cmd.is_link``).
    """
    if WINDOWS:
        made = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True
        )
        if made.returncode != 0:  # pragma: no cover -- host policy
            pytest.skip("cannot create a junction on this host")
    else:
        link.symlink_to(target, target_is_directory=True)


def _build_tree(
    root: Path,
    *,
    manifest: str | None = None,
    manifest_bytes: bytes | None = None,
    manifest_is_dir: bool = False,
    marker: bool = True,
    build_is_file: bool = False,
    junction: str | None = None,
    out_of_tree: bool = False,
    read_only: bool = False,
) -> None:
    """One scratch project, built identically on both sides of a case."""
    proj = root / "proj"
    (proj / "scripts").mkdir(parents=True)
    if marker:
        # The SDK-root marker `resolve_sdk_root` looks for (I-31).
        (proj / "scripts" / "alp_project.py").write_text("")
    (proj / "src").mkdir()
    (proj / "src" / "main.c").write_text("int main(void){return 0;}")
    (proj / "board.yaml").write_text("schema_version: 1\n")
    (proj / ".alp-build-state.json").write_text("{}")
    (root / "precious").mkdir()
    (root / "precious" / "work.txt").write_text("A CUSTOMER'S WORK")
    (root / "CANARY.txt").write_text("CANARY")
    (root / "home").mkdir()
    if junction == "build":
        _link(proj / "build", root / "precious")
        return
    if build_is_file:
        (proj / "build").write_text("i am a file")
        return
    (proj / "build" / "m55_hp-zephyr" / "zephyr").mkdir(parents=True)
    (proj / "build" / "m55_hp-zephyr" / "zephyr" / "zephyr.elf").write_text("ELF")
    (proj / "build" / "marker").write_text("x")
    if junction == "inside":
        _link(proj / "build" / "escape", root / "precious")
    if out_of_tree:
        (root / "oot").mkdir()
        (root / "oot" / "tmp.txt").write_text("OOT")
    if read_only:
        locked = proj / "build" / "locked.txt"
        locked.write_text("x")
        os.chmod(locked, 0o444)
    if manifest_is_dir:
        (proj / "build" / "system-manifest.yaml").mkdir()
    elif manifest_bytes is not None:
        (proj / "build" / "system-manifest.yaml").write_bytes(manifest_bytes)
    elif manifest is not None:
        (proj / "build" / "system-manifest.yaml").write_text(manifest)


def _survivors(root: Path) -> list[str]:
    return sorted(str(p.relative_to(root)).replace("\\", "/") for p in root.rglob("*"))


def _scrub(text: str, root: Path) -> str:
    """Replace every spelling of the tree root with a placeholder.

    Both the native and forward-slash forms, and the JSON-escaped variant of
    each: the envelope carries ``project.root`` posix and ``data.buildRoot`` in
    the platform's own separators -- sometimes MIXED inside one string -- and the
    two sides' roots differ only by the case directory name.
    """
    native = str(root)
    for form in (
        native,
        native.replace("\\", "/"),
        json.dumps(native)[1:-1],
        json.dumps(native.replace("\\", "/"))[1:-1],
    ):
        text = text.replace(form, "<ROOT>")
    return text


def _normalise_issues(doc: dict) -> dict:
    """The two declared, non-reproducible message tails. Everything BEFORE each
    is still compared byte for byte.

    * ``clean.manifest-unreadable``: serde_yaml appends ``at line N column M``
      and ``yaml.safe_load`` discards node marks, so recovering them would mean a
      custom YAML composer for a warning string.
    * ``clean.remove-failed``: Windows' ``FormatMessageW`` ends its sentences with
      a period, which Rust keeps and Python's ``strerror`` strips. One character,
      in a system message neither implementation authored.
    """
    for issue in doc.get("issues") or []:
        if issue.get("code") == "clean.manifest-unreadable":
            issue["message"] = issue["message"].split(" at line ")[0]
        elif issue.get("code") == "clean.remove-failed":
            issue["message"] = issue["message"].replace(". (os error ", " (os error ")
    return doc


def _run(command: list[str], argv: list[str], root: Path):
    env = {
        **os.environ,
        # A developer's real `~/.alp/sdk-default` must not decide what either
        # side resolves -- the same reason the conformance harness redirects both.
        "HOME": str(root / "home"),
        "USERPROFILE": str(root / "home"),
        "SOURCE_DATE_EPOCH": "0",
        "PYTHONPATH": os.pathsep.join(
            [
                str(PACKAGE_ROOT),
                *([os.environ["PYTHONPATH"]] if "PYTHONPATH" in os.environ else []),
            ]
        ),
    }
    proc = subprocess.run(
        [*command, *argv],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(root / "proj"),
        env=env,
        timeout=120,
    )
    try:
        payload = json.loads(proc.stdout)
    except ValueError:
        # Text mode, or a usage error: "stdout carries the envelope and NOTHING
        # else" is itself the assertion, so compare it verbatim.
        return proc.returncode, {"__raw__": _scrub(proc.stdout.strip(), root)}
    if not isinstance(payload, dict):  # a bare JSON scalar/array on stdout
        return proc.returncode, {"__raw__": _scrub(proc.stdout.strip(), root)}
    return proc.returncode, _normalise_issues(json.loads(_scrub(json.dumps(payload), root)))


def _run_rust(argv: list[str], root: Path) -> tuple[int, dict]:
    """The rust side of `_run`, frozen by default (tan-cli#272) -- see
    `oracle_fixtures.resolve`. `_run` already scrubs its own `root` out of the
    payload before returning, so the captured/replayed answer is already
    portable across scratch directories with no extra work here."""

    def _live():
        return list(_run([RUST], argv, root))

    return oracle_fixtures.resolve(_live)


def _run_rust_with_survivors(argv: list[str], root: Path) -> tuple[int, dict, list[str]]:
    """`_run_rust` plus `_survivors(root)` in the SAME frozen unit: which
    files rust's own run left behind is part of what tan-cli#272 asks this
    suite to capture (`clean` deletes -- the survivor set is not a pure
    function of `tree`, it is the oracle's own observed behaviour)."""

    def _live():
        code, payload = _run([RUST], argv, root)
        return [code, payload, _survivors(root)]

    code, payload, survivors = oracle_fixtures.resolve(_live)
    return code, payload, survivors


#: ``(id, argv, tree kwargs)``. ``@@ROOT@@`` in an argument is replaced with that
#: side's own tree root, so a case can name an absolute path outside the project.
CASES = [
    ("dry-run", ["clean", "--dry-run", "--format", "json"], {}),
    ("dry-run-text", ["clean", "--dry-run"], {}),
    ("real", ["clean", "--format", "json"], {}),
    ("real-text", ["clean"], {}),
    ("nothing-to-remove", ["clean", "--build-root", "absent", "--format", "json"], {}),
    # The `rm -rf $UNSET_VAR` shapes: each resolves to the project root or above.
    ("build-root-empty", ["clean", "--build-root", "", "--format", "json"], {}),
    ("build-root-dot", ["clean", "--build-root", ".", "--format", "json"], {}),
    ("build-root-dotdot", ["clean", "--build-root", "..", "--format", "json"], {}),
    ("build-root-dotdot-text", ["clean", "--build-root", ".."], {}),
    # Out of tree is SUPPORTED, not catastrophic -- the pin on why `clean` cannot
    # screen every target with `confine_to_build_root`. Targets `../oot`, never
    # `../precious`: `precious/` is the canary both sides must leave alone, and a
    # case that legitimately removed it could not assert that.
    (
        "build-root-outside",
        ["clean", "--build-root", "../oot", "--format", "json"],
        {"out_of_tree": True},
    ),
    (
        "build-root-nested",
        ["clean", "--build-root", "build/m55_hp-zephyr", "--format", "json"],
        {},
    ),
    ("build-is-file", ["clean", "--format", "json"], {"build_is_file": True}),
    ("build-is-junction", ["clean", "--format", "json"], {"junction": "build"}),
    ("junction-inside-build", ["clean", "--format", "json"], {"junction": "inside"}),
    ("read-only-artefact", ["clean", "--format", "json"], {"read_only": True}),
    ("no-sdk", ["clean", "--format", "json"], {"marker": False}),
    ("sdk-root-bogus", ["clean", "--sdk-root", "@@ROOT@@/nope", "--format", "json"], {}),
    (
        "sdk-root-flag",
        ["clean", "--sdk-root", "@@ROOT@@/proj", "--dry-run", "--format", "json"],
        {},
    ),
    ("positional-absolute", ["clean", "@@ROOT@@/oot", "--format", "json"], {"out_of_tree": True}),
    ("positional-dotdot", ["clean", "..", "--format", "json"], {}),
    # Manifest sweep: refusal, out-of-tree pickup, and every read/parse arm.
    ("manifest-empty-build-dir", ["clean", "--format", "json"], {"manifest": _manifest("")}),
    ("manifest-root-build-dir", ["clean", "--format", "json"], {"manifest": _manifest("/")}),
    ("manifest-dotdot-build-dir", ["clean", "--format", "json"], {"manifest": _manifest("../..")}),
    (
        "manifest-out-of-tree",
        ["clean", "--format", "json"],
        {"manifest": _manifest("../oot"), "out_of_tree": True},
    ),
    (
        "manifest-out-of-tree-dry",
        ["clean", "--dry-run", "--format", "json"],
        {"manifest": _manifest("../oot"), "out_of_tree": True},
    ),
    (
        "manifest-subsumed",
        ["clean", "--format", "json"],
        {"manifest": _manifest("build/m55_hp-zephyr")},
    ),
    ("manifest-no-build-dir", ["clean", "--format", "json"], {"manifest": _manifest(None)}),
    (
        "manifest-v2",
        ["clean", "--format", "json"],
        {"manifest": "schema_version: 2\nslices: []\n"},
    ),
    ("manifest-sequence", ["clean", "--format", "json"], {"manifest": "- a\n"}),
    ("manifest-empty-file", ["clean", "--format", "json"], {"manifest": ""}),
    ("manifest-null-doc", ["clean", "--format", "json"], {"manifest": "null\n"}),
    ("manifest-comment-only", ["clean", "--format", "json"], {"manifest": "# nothing\n"}),
    (
        "manifest-version-string",
        ["clean", "--format", "json"],
        {"manifest": 'schema_version: "1"\nslices: []\n'},
    ),
    (
        "manifest-slices-scalar",
        ["clean", "--format", "json"],
        {"manifest": "schema_version: 1\nslices: 7\n"},
    ),
    (
        "manifest-slice-missing-core-id",
        ["clean", "--format", "json"],
        {"manifest": "schema_version: 1\nslices:\n- os: zephyr\n"},
    ),
    (
        "manifest-scalar-core-id",
        ["clean", "--format", "json"],
        {"manifest": "schema_version: 1\nslices:\n- core_id: 7\n  os: zephyr\n"},
    ),
    ("manifest-is-a-directory", ["clean", "--format", "json"], {"manifest_is_dir": True}),
    (
        "manifest-non-utf8",
        ["clean", "--format", "json"],
        {"manifest_bytes": b"schema_version: 1\nslices: []\n# \xff\xfe\x00\n"},
    ),
    ("manifest-quiet", ["clean", "--quiet", "--format", "json"], {"manifest": "- a\n"}),
    ("manifest-quiet-text", ["clean", "--quiet"], {"manifest": "- a\n"}),
]

#: Windows-only path shapes. `C:foo` is drive-relative, `\\?\` is the device
#: namespace, `\x` is rooted-but-prefixless -- none is `is_absolute()`, and each
#: makes a naive join discard part or all of the base it was joined onto.
WINDOWS_CASES = [
    ("build-root-drive-relative", ["clean", "--build-root", "C:foo", "--format", "json"], {}),
    ("build-root-unc", ["clean", "--build-root", r"\\server\share\x", "--format", "json"], {}),
    ("build-root-device-ns", ["clean", "--build-root", r"\\?\C:\nope", "--format", "json"], {}),
    ("build-root-rooted", ["clean", "--build-root", r"\rooted-nope", "--format", "json"], {}),
    # The same four shapes arriving through the MANIFEST instead of the flag, so
    # the containment test is asked about them too. `build_dir: "C:rel"` caught a
    # live port defect: `Path(build_root) / "C:rel"` re-reads a drive-relative
    # value as relative to the base and answered "already covered", silently
    # dropping a target the oracle reports (see `_subsumed_by_build_root`).
    (
        "manifest-drive-relative-build-dir",
        ["clean", "--format", "json"],
        {"manifest": _manifest("C:rel")},
    ),
    (
        "manifest-unc-build-dir",
        ["clean", "--format", "json"],
        {"manifest": _manifest(r"\\\\srv\\sh\\x")},
    ),
    (
        "manifest-device-ns-build-dir",
        ["clean", "--format", "json"],
        {"manifest": _manifest(r"\\\\?\\C:\\x")},
    ),
    (
        "manifest-rooted-build-dir",
        ["clean", "--format", "json"],
        {"manifest": _manifest(r"\\rooted-nope")},
    ),
    # A hostile flag AND a hostile manifest at once: the build root's own
    # `Path.resolve()` can raise here, which is the arm the OSError guard covers.
    (
        "crossed-device-ns-flag-and-out-of-tree-slice",
        ["clean", "--build-root", r"\\?\C:\nope", "--format", "json"],
        {"manifest": _manifest("../oot"), "out_of_tree": True},
    ),
]


#: Cases whose manifest plants a ROOT-RELATIVE path, which the two platforms
#: resolve to genuinely different absolute paths: Windows anchors `/` onto the
#: current drive (`C:/`), POSIX to the filesystem root (`/`). The refusal
#: itself is identical on both -- `clean.unsafe-target`, same exit code, same
#: surviving file set -- only the resolved path quoted back in the message and
#: in `targets[].path` differs, and each is right about its own OS.
#:
#: NOT covered by :func:`oracle_fixtures.normalise_scrubbed_path_separators`,
#: and deliberately so: that helper rewrites separators inside a REDACTED
#: scratch root, where the text is a harness artefact. `C:/` is neither
#: redacted nor an artefact -- it is what the platform resolved. Arrived at by
#: measurement: it is the ONLY case of the 41 that runs on POSIX once the
#: separator noise is gone.
_HOST_ANCHORED_ROOT_CASES = frozenset({"manifest-root-build-dir"})


@pytest.mark.parametrize(
    "argv,tree",
    [pytest.param(a, t, id=i) for i, a, t in CASES]
    + [
        pytest.param(
            a,
            t,
            id=i,
            marks=pytest.mark.skipif(not WINDOWS, reason="Windows-only path shape"),
        )
        for i, a, t in WINDOWS_CASES
    ],
)
def test_python_clean_matches_rust(argv, tree, tmp_path, request):
    if (
        request.node.callspec.id in _HOST_ANCHORED_ROOT_CASES
        and not oracle_fixtures.REPLAY_IS_CAPTURE_PLATFORM
    ):
        pytest.skip(
            f"{request.node.callspec.id} plants `build_dir: /`, which Windows "
            f"resolves onto the current DRIVE (`C:/`) and POSIX resolves to the "
            f"filesystem root (`/`); the frozen answer is "
            f"{oracle_fixtures.CAPTURE_PLATFORM}'s and both are correct -- a real "
            "OS difference in what a root-relative path MEANS, not a separator "
            "artefact and not a port defect. Both sides still refuse it"
        )
    sides = {}
    for name in ("rust", "python"):
        root = tmp_path / name
        root.mkdir()
        _build_tree(root, **tree)
        resolved = [a.replace("@@ROOT@@", str(root)) for a in argv]
        if name == "rust":
            sides[name] = _run_rust_with_survivors(resolved, root)
        else:
            code, payload = _run(python_command(), resolved, root)
            sides[name] = (code, payload, _survivors(root))

    # Independent of parity, and asserted FIRST: neither implementation may ever
    # reach outside the build root. A case where BOTH destroyed the canary would
    # otherwise "match" and pass.
    for name in ("rust", "python"):
        assert "CANARY.txt" in sides[name][2], f"{name} destroyed the canary"
        assert "precious/work.txt" in sides[name][2], f"{name} destroyed out-of-tree work"
        assert "proj/src/main.c" in sides[name][2], f"{name} destroyed the sources"

    diffs = []
    if sides["rust"][0] != sides["python"][0]:
        diffs.append(f"exit code: rust={sides['rust'][0]} python={sides['python'][0]}")
    # `_scrub` redacts the tree ROOT but leaves the separators that JOIN the
    # segments after it, and the frozen rust side carries the CAPTURE host's:
    # `"buildRoot": "<ROOT>/proj\\build"` -- forward-slash root portion, native
    # `\` on the final join -- where a live POSIX run says `<ROOT>/proj/build`.
    # Both are correct on their own OS. Applied to BOTH sides, and only off the
    # capture platform, so Windows CI and `TAN_PARITY_LIVE=1` keep the
    # byte-for-byte diff this module's docstring describes; see that function
    # for the exact trade. Replaces a blanket skip of every JSON-mode case,
    # which stopped measuring the other 40-odd fields to guard this one.
    rust_doc = oracle_fixtures.normalise_scrubbed_path_separators(sides["rust"][1])
    py_doc = oracle_fixtures.normalise_scrubbed_path_separators(sides["python"][1])
    for key in sorted(set(rust_doc) | set(py_doc)):
        if rust_doc.get(key) != py_doc.get(key):
            diffs.append(
                f"{key}: rust={json.dumps(rust_doc.get(key))} "
                f"python={json.dumps(py_doc.get(key))}"
            )
    if sides["rust"][2] != sides["python"][2]:
        diffs.append(
            f"surviving files:\n  rust  ={sides['rust'][2]}\n  python={sides['python'][2]}"
        )
    assert not diffs, "\n".join(diffs)


def test_the_comparator_goes_red_on_a_planted_divergence(tmp_path):
    """The comparator above is the only gate ``clean`` has, so it has to be shown
    capable of failing. Runs the RUST binary as BOTH sides but with different
    argv and requires a diff -- the same self-check ``test_oracle_parity`` makes
    through its ``python=`` override seam. Without this, a comparator bug that
    made everything compare equal would read as 45 green cases."""
    sides = {}
    for name, argv in (
        ("a", ["clean", "--dry-run", "--format", "json"]),
        ("b", ["clean", "--format", "json"]),
    ):
        root = tmp_path / name
        root.mkdir()
        _build_tree(root)
        sides[name] = _run_rust_with_survivors(argv, root)
    assert sides["a"][1] != sides["b"][1], "the comparator cannot tell two runs apart"
    # Compared from the FROZEN survivor lists, not a fresh disk scan: in
    # frozen replay mode nothing actually ran `clean` against either tree
    # (there is no live rust to spawn), so the trees on disk are identical by
    # construction and a disk re-scan would read this self-check vacuously
    # true for the wrong reason.
    assert sides["a"][2] != sides["b"][2], (
        "the file-set comparison cannot tell a dry run from a real one"
    )
