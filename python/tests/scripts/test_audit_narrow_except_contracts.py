# SPDX-License-Identifier: Apache-2.0
"""The lazy-iteration detector must be able to report the thing it was built
to catch -- and must NOT report the two shapes it already got wrong once.

tan-cli#1132 added SHAPE 2 to `scripts/audit_narrow_except_contracts.py`: a
`try` whose body builds a lazy iterator (`glob`, `rglob`, `iterdir`,
`scandir`, `walk`) that nothing in that body forces, so the filesystem work
happens after the handlers and the `except` is dead. Nothing in CI runs that
script, and its own tally on a clean tree is `0` -- which is exactly the
"a check that exists, runs, and cannot report the thing it would catch" shape
this milestone spent a week closing. A detector whose only evidence is a zero
is indistinguishable from a broken one.

THE REGRESSION THIS EXISTS FOR IS NOT HYPOTHETICAL. The `ast.With` /
`optional_vars` alias handling in `escaping_lazy_calls` exists solely because
a first draft of the detector reported `examples_cmd._subdirectories` and
`presets_cmd._entries` as escapes when both force their iterator inside the
`try` one line down -- they just bind it with `with os.scandir(...) as
entries:` rather than an assignment. `test_the_two_real_false_positive_sites_
are_not_reported` drives the detector over those two REAL modules, not a
paraphrase of them, so the fix cannot silently regress and so a future edit
to either module that genuinely breaks the shape is visible here.

Both directions are pinned, deliberately: the positive half (`escaping_lazy_
calls` reports the #1132 shape, `find_candidates` selects it, the floor
fires) and the negative half (every forcing spelling the docstring claims is
modelled really is). A detector that only ever gets tested with input it
reports is half a test.

THE RECORDED LIMITS ARE PINNED TOO, as measurements rather than as approval
-- see `test_walrus_forcing_is_a_known_false_positive` and its neighbours.
The module docstring claims those gaps in both directions; asserting them
here is what stops that claim rotting into a lie, and what turns "we fixed
it" into a red test rather than a silent behaviour change.
"""
from __future__ import annotations

import ast
import importlib.util
import re
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "audit_narrow_except_contracts.py"
_spec = importlib.util.spec_from_file_location("audit_narrow_except_contracts", _SCRIPT)
assert _spec and _spec.loader
audit = importlib.util.module_from_spec(_spec)
sys.modules["audit_narrow_except_contracts"] = audit
_spec.loader.exec_module(audit)

_TAN = Path(__file__).resolve().parents[2] / "tan"


def _first_try(source: str) -> ast.Try:
    """The first `try` statement in @source, dedented-by-construction."""
    tries = [n for n in ast.walk(ast.parse(source)) if isinstance(n, ast.Try)]
    assert tries, "the snippet under test has no `try` at all"
    return tries[0]


def _escapes(source: str) -> list[str]:
    return audit.escaping_lazy_calls(_first_try(source))


# ---------------------------------------------------------------------------
# The positive half -- the detector reports the shape it exists for.
# ---------------------------------------------------------------------------


def test_the_tan_cli_1132_shape_is_reported():
    # Byte-for-byte the shape `discover_configure_inputs` carried on `dev`:
    # the lazy call is the whole `try` body, the iteration is below it.
    escapes = _escapes(
        "try:\n"
        "    matches = app_dir.glob(pattern)\n"
        "except OSError:\n"
        "    continue\n"
        "for path in matches:\n"
        "    pass\n"
    )
    assert escapes == ["app_dir.glob(...) at line 2"]


def test_an_unforced_with_scandir_is_still_reported():
    # The `ast.With` alias handling must not be a blanket suppression of
    # every `with os.scandir(...) as`: one whose iterator escapes the block
    # is still an escape. This is the assertion that separates "modelled the
    # binding" from "stopped looking at `with` statements".
    escapes = _escapes(
        "try:\n"
        "    with os.scandir(root) as entries:\n"
        "        it = entries\n"
        "except OSError:\n"
        "    return []\n"
        "return list(it)\n"
    )
    assert escapes == ["os.scandir(...) at line 2"]


def test_forcing_in_the_else_clause_is_still_an_escape():
    # `else:` runs outside the handlers' cover, which is the entire point.
    assert _escapes(
        "try:\n"
        "    m = d.glob('*.json')\n"
        "except OSError:\n"
        "    m = []\n"
        "else:\n"
        "    m = sorted(m)\n"
    ) == ["d.glob(...) at line 2"]


#: The five names SHAPE 2 selects on, spelled here independently of the
#: script so the two can be compared rather than assumed equal.
_EXPECTED_LAZY_NAMES = {"glob", "rglob", "iterdir", "scandir", "walk"}


@pytest.mark.parametrize("call", ["d.glob('*')", "d.rglob('*')", "d.iterdir()",
                                  "os.scandir(d)", "os.walk(d)"])
def test_every_expected_lazy_name_is_actually_detected(call):
    """Each name is DRIVEN, not merely listed -- a name in the constant that
    the walk cannot reach (an `ast.Name` callee spelling it never inspects,
    say) would pass a set comparison and fail here."""
    assert _escapes(f"try:\n    m = {call}\nexcept OSError:\n    m = []\n")


def test_the_lazy_name_set_matches_this_file_and_the_scripts_own_docstring():
    """Pins `LAZY_ITER_CALLS` in BOTH directions, and pins the docstring too.

    Review nit: the parametrized test above only catches a name being
    REMOVED. Adding an inert `"zzz_never_used"` to `LAZY_ITER_CALLS` reds
    nothing there, and nothing read the prose at all -- so the constant was
    pinned against shrinking, and the "SHAPE 2 ... a call named `glob`,
    `rglob`, `iterdir`, `scandir` or `walk`" sentence in the script's module
    docstring was pinned not at all. That sentence is the whole contract a
    reader acts on; an over-broad constant silently widens the walk under a
    docstring that still promises five names.

    Both halves are asserted against `_EXPECTED_LAZY_NAMES` above rather
    than against each other, so agreeing on a wrong value is not enough --
    growing the set means editing the constant, the prose AND this list,
    which is the point.
    """
    assert audit.LAZY_ITER_CALLS == _EXPECTED_LAZY_NAMES
    claim = "a call named `glob`, `rglob`, `iterdir`,\n`scandir` or `walk`"
    assert claim in audit.__doc__, (
        "the script's SHAPE 2 docstring no longer spells the five names this "
        "test expects; if the set really changed, change the prose, this "
        "assertion and _EXPECTED_LAZY_NAMES together"
    )
    quoted = set(re.findall(r"`(\w+)`", claim))
    assert quoted == _EXPECTED_LAZY_NAMES


# ---------------------------------------------------------------------------
# The negative half -- every forcing spelling the docstring claims to model.
# ---------------------------------------------------------------------------


def test_the_two_real_false_positive_sites_are_not_reported():
    """The regression this file exists for, driven over the REAL modules.

    `examples_cmd._subdirectories` binds with `with os.scandir(root) as
    entries:` and forces with a comprehension; `presets_cmd._entries` binds
    `as it` and forces with `list(it)`. A first draft of the detector
    reported both. Parsing the shipped source rather than a paraphrase means
    this also reds if either module is later rewritten into a real escape --
    which is the correct outcome, not a false alarm.
    """
    reported = []
    for module, qualname in (("commands/examples_cmd.py", "_subdirectories"),
                             ("commands/presets_cmd.py", "_entries")):
        tree = ast.parse((_TAN / module).read_text(encoding="utf-8"))
        fns = [n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == qualname]
        assert len(fns) == 1, f"{module}::{qualname} moved or was renamed"
        for node in ast.walk(fns[0]):
            if isinstance(node, ast.Try):
                reported.extend(audit.escaping_lazy_calls(node))
    assert reported == []


@pytest.mark.parametrize("body", [
    # forced directly on the call
    "    return sorted(d.glob('*.json'))",
    "    return list(d.glob('*'))",
    # forced by a `for` loop over the call
    "    for p in d.glob('*'):\n        pass",
    # forced through an ASSIGNMENT-bound name
    "    m = d.glob('*')\n    for p in m:\n        pass",
    "    m = d.glob('*')\n    return sorted(m)",
    # forced through a `with ... as`-bound name -- both real spellings
    "    with os.scandir(d) as entries:\n        return [e for e in entries]",
    "    with os.scandir(d) as it:\n        return list(it)",
    # a comprehension driving the call itself
    "    return {p.name for p in d.rglob('*.conf')}",
])
def test_forced_inside_the_try_is_not_reported(body):
    assert _escapes(f"try:\n{body}\nexcept OSError:\n    return []\n") == []


# ---------------------------------------------------------------------------
# The recorded limits. These assert what the module docstring CLAIMS about
# its own gaps -- so the claim cannot rot, and so closing a gap reds here
# rather than changing behaviour silently.
# ---------------------------------------------------------------------------


def test_a_helper_returned_iterator_is_a_known_false_negative():
    assert _escapes(
        "try:\n"
        "    m = _lazy(d)\n"
        "except OSError:\n"
        "    m = []\n"
        "for p in m:\n"
        "    pass\n"
    ) == []


def test_walrus_forcing_is_a_known_false_positive():
    # `_forced_within` reconciles names only against `ast.Assign`/`ast.With`
    # bindings, so this IS forced inside the `try` and is reported anyway.
    assert _escapes(
        "try:\n"
        "    if (m := d.glob('*')):\n"
        "        list(m)\n"
        "except OSError:\n"
        "    pass\n"
    ) == ["d.glob(...) at line 2"]


def test_method_call_forcing_is_a_known_false_positive():
    # `str.join` forces the iterator but is not in `FORCING_CALLS`.
    assert _escapes(
        "try:\n"
        "    s = ','.join(d.glob('*'))\n"
        "except OSError:\n"
        "    s = ''\n"
    ) == ["d.glob(...) at line 2"]


# ---------------------------------------------------------------------------
# `find_candidates` -- selection, not just the predicate.
# ---------------------------------------------------------------------------


def _tan_tree(tmp_path: Path, **modules: str) -> Path:
    """A synthetic `<pkg>/tan/` for `find_candidates` to walk."""
    root = tmp_path / "pkg" / "tan"
    root.mkdir(parents=True)
    for name, source in modules.items():
        (root / f"{name}.py").write_text(source, encoding="utf-8")
    return root


def test_a_lazy_escape_is_selected_and_labelled(tmp_path, monkeypatch):
    monkeypatch.setattr(audit, "TAN_ROOT", _tan_tree(tmp_path, mod=(
        "def f(d):\n"
        "    try:\n"
        "        m = d.glob('*')\n"
        "    except OSError:\n"
        "        return []\n"
        "    return list(m)\n"
    )))
    [found] = audit.find_candidates(include_planner=False)
    assert found.qualname == "f"
    assert found.shapes == frozenset({"lazy-escape"})
    assert found.lazy_detail == ("d.glob(...) at line 3",)


def test_handler_breadth_does_not_excuse_a_lazy_escape(tmp_path, monkeypatch):
    # An `except Exception` around a lazy call it never covers is exactly as
    # dead as an `except OSError`, so SHAPE 2 does not filter on `BROAD`
    # (SHAPE 1 does, and still must -- see the next test).
    monkeypatch.setattr(audit, "TAN_ROOT", _tan_tree(tmp_path, mod=(
        "def f(d):\n"
        "    try:\n"
        "        m = d.glob('*')\n"
        "    except Exception:\n"
        "        return []\n"
        "    return list(m)\n"
    )))
    [found] = audit.find_candidates(include_planner=False)
    assert found.shapes == frozenset({"lazy-escape"})


def test_shape_1_selection_is_unchanged_by_shape_2(tmp_path, monkeypatch):
    # Three modules, three verdicts: a narrow `except` around risky I/O is
    # still SHAPE 1; a broad one is still excluded; a handler-less
    # `try`/`finally` around risky I/O is still selected exactly as the
    # original walk selected it (empty handler names are not BROAD).
    monkeypatch.setattr(audit, "TAN_ROOT", _tan_tree(
        tmp_path,
        narrow="def f(p):\n    try:\n        return p.read_text()\n"
               "    except OSError:\n        return None\n",
        broad="def g(p):\n    try:\n        return p.read_text()\n"
              "    except Exception:\n        return None\n",
        no_handler="def h(p):\n    try:\n        return p.read_text()\n"
                   "    finally:\n        pass\n",
    ))
    picked = {c.qualname: c.shapes for c in audit.find_candidates(include_planner=False)}
    assert picked == {"f": frozenset({"narrow-except"}),
                      "h": frozenset({"narrow-except"})}


def test_a_try_finally_carries_no_lazy_escape(tmp_path, monkeypatch):
    # SHAPE 2 needs an `except` to call dead. A `try`/`finally` has none, so
    # it must not be reported as one however the iterator is used.
    monkeypatch.setattr(audit, "TAN_ROOT", _tan_tree(tmp_path, mod=(
        "def f(d):\n"
        "    try:\n"
        "        m = d.glob('*')\n"
        "    finally:\n"
        "        pass\n"
        "    return list(m)\n"
    )))
    assert audit.find_candidates(include_planner=False) == []


# ---------------------------------------------------------------------------
# The floor -- the guard against a vacuous run.
# ---------------------------------------------------------------------------


def test_the_forty_candidate_floor_refuses_a_vacuous_run(tmp_path, monkeypatch, capsys):
    """A walk that finds almost nothing is a broken walk, not a clean tree.

    The tally line is what a reader acts on, so printing `1 candidate` and
    exiting 0 would be the #1105 shape inside the tool built to prevent it.
    Asserting the MESSAGE and the code: a refusal that fired for some other
    reason would also exit 1.
    """
    monkeypatch.setattr(audit, "TAN_ROOT", _tan_tree(tmp_path, mod=(
        "def f(p):\n    try:\n        return p.read_text()\n"
        "    except OSError:\n        return None\n"
    )))
    monkeypatch.setattr(sys, "argv", ["audit_narrow_except_contracts.py"])
    assert audit.main() == 1
    captured = capsys.readouterr()
    assert "::error:: only 1 candidate(s) found -- expected at least 40" in captured.err
    assert "tally:" not in captured.out


def test_the_real_tree_clears_the_floor_and_reports_no_lazy_escape():
    """The end-to-end claim the PR body cites, asserted rather than pasted.

    Two things at once, and both matter: the walk still finds far more than
    the floor (so the floor is not the thing keeping this green), and no
    SHAPE 2 finding survives on this tree -- which is only meaningful
    because the tests above prove the detector reports one when there is one.
    """
    candidates = audit.find_candidates(include_planner=False)
    assert len(candidates) >= 40
    assert [c for c in candidates if "lazy-escape" in c.shapes] == []


# ---------------------------------------------------------------------------
# SHAPE 3, "there is no `try` at all" (tan-cli#1133). Same standard as SHAPE
# 2 above: the positive half, the negative half, and every limit the module
# docstring claims, asserted rather than asserted-about.
# ---------------------------------------------------------------------------


#: The pre-fix `tan/planner/template.py::_load_som_doc`, transcribed. This is
#: the shape the first two detectors are structurally blind to and the reason
#: shape 3 exists, so it is pinned as the real thing rather than a paraphrase.
_PRE_FIX_LOAD_SOM_DOC = (
    "def _load_som_doc(sku, metadata_root):\n"
    "    som_path = metadata_root / 'e1m_modules' / f'{sku}.yaml'\n"
    "    if not som_path.is_file():\n"
    "        raise TemplateError(f'no metadata/e1m_modules/{sku}.yaml')\n"
    "    return _require_mapping_doc(\n"
    "        yaml.safe_load(som_path.read_text(encoding='utf-8')) or {},\n"
    "        path=som_path, what='SoM preset')\n"
)


def test_the_tan_cli_1133_shape_is_reported(tmp_path, monkeypatch):
    monkeypatch.setattr(audit, "TAN_ROOT",
                        _tan_tree(tmp_path, mod=_PRE_FIX_LOAD_SOM_DOC))
    [found] = audit.find_candidates(include_planner=False)
    assert found.qualname == "_load_som_doc"
    assert found.shapes == frozenset({"absent-try"})
    # BOTH bare calls, not just the outer one: the read and the parse each
    # raise a different class (`UnicodeDecodeError`/`OSError` and
    # `yaml.YAMLError`), and reporting only one would understate the site.
    assert found.absent_detail == (
        "yaml.safe_load(...) at line 6 [raises TemplateError]",
        "som_path.read_text(...) at line 6 [raises TemplateError]",
    )


def test_a_quiet_return_docstring_is_contract_evidence_too(tmp_path, monkeypatch):
    # The other half of `declared_contract`: no curated raise anywhere, but
    # the docstring promises an outcome a raw OSError would breach.
    monkeypatch.setattr(audit, "TAN_ROOT", _tan_tree(tmp_path, mod=(
        "def f(path):\n"
        "    '''Never raises: None when the file cannot be read.'''\n"
        "    return path.read_text()\n"
    )))
    [found] = audit.find_candidates(include_planner=False)
    assert found.shapes == frozenset({"absent-try"})
    assert found.absent_detail == (
        "path.read_text(...) at line 3 [docstring says 'never raises']",)


def test_a_bare_read_with_no_declared_contract_is_not_reported(tmp_path, monkeypatch):
    # The selectivity that keeps shape 3 from reporting every read in the
    # tree. This function promises nothing, so a raw OSError out of it
    # breaches nothing it declared.
    monkeypatch.setattr(audit, "TAN_ROOT", _tan_tree(tmp_path, mod=(
        "def f(path):\n"
        "    return path.read_text()\n"
    )))
    assert audit.find_candidates(include_planner=False) == []


def test_a_builtin_raise_is_not_contract_evidence(tmp_path, monkeypatch):
    # `raise ValueError(...)` is not a curated contract -- it promises
    # nothing a raw OSError would breach -- so it must not pull every
    # argument-validating function in the tree into the report.
    monkeypatch.setattr(audit, "TAN_ROOT", _tan_tree(tmp_path, mod=(
        "def f(path):\n"
        "    if path is None:\n"
        "        raise ValueError('path')\n"
        "    return path.read_text()\n"
    )))
    assert audit.find_candidates(include_planner=False) == []


def test_a_guarded_read_is_not_an_absent_try_however_narrow(tmp_path, monkeypatch):
    # Judging the handler's BREADTH is SHAPE 1's job. A read inside a
    # handled `try` is shape 1's to report or excuse, never shape 3's --
    # otherwise every shape 1 finding would be double-counted as a shape 3
    # one and the third tally line would mean nothing.
    monkeypatch.setattr(audit, "TAN_ROOT", _tan_tree(tmp_path, mod=(
        "def f(path):\n"
        "    try:\n"
        "        return path.read_text()\n"
        "    except OSError:\n"
        "        raise TemplateError('nope')\n"
    )))
    [found] = audit.find_candidates(include_planner=False)
    assert found.shapes == frozenset({"narrow-except"})
    assert found.absent_detail == ()


def test_a_try_finally_is_not_cover_for_shape_3(tmp_path, monkeypatch):
    # A `try`/`finally` re-raises exactly what shape 3 hunts, so it must not
    # count as cover -- the mirror of `test_a_try_finally_carries_no_lazy_
    # escape` above, where the same construct correctly suppresses SHAPE 2.
    monkeypatch.setattr(audit, "TAN_ROOT", _tan_tree(tmp_path, mod=(
        "def f(path):\n"
        "    '''Never raises.'''\n"
        "    try:\n"
        "        return path.read_text()\n"
        "    finally:\n"
        "        pass\n"
    )))
    [found] = audit.find_candidates(include_planner=False)
    assert "absent-try" in found.shapes


def test_the_caller_side_contract_is_a_known_false_negative(tmp_path, monkeypatch):
    """The recorded limit, pinned as a measurement rather than approved.

    The issue's own definition has two halves -- "a function whose declared
    contract, OR whose caller's only handler, cannot absorb what the read
    raises". This detector implements the first half only: it reads the
    contract off the function, and builds no call graph. `g` below is a real
    instance of the second half (its only caller curates a narrow class
    around it), and shape 3 does not report it. Asserting that is what stops
    the docstring's claim rotting into a lie if the walk ever changes.
    """
    monkeypatch.setattr(audit, "TAN_ROOT", _tan_tree(tmp_path, mod=(
        "def g(path):\n"
        "    return path.read_text()\n"
        "\n"
        "def caller(path):\n"
        "    try:\n"
        "        return g(path)\n"
        "    except TemplateError:\n"
        "        return None\n"
    )))
    assert [c.qualname for c in audit.find_candidates(include_planner=False)] == []


def test_the_third_tally_line_is_printed_on_every_run(tmp_path, monkeypatch, capsys):
    """Three tally lines, never summed. A reader who sees only two would
    have no way to notice shape 3 stopped reporting."""
    monkeypatch.setattr(audit, "TAN_ROOT", _TAN)
    monkeypatch.setattr(sys, "argv", ["audit_narrow_except_contracts.py"])
    assert audit.main() == 0
    out = capsys.readouterr().out
    assert re.search(r"^tally: \d+ OK", out, re.M)
    assert re.search(r"^lazy-escape \(static, shape 2\): \d+ of \d+", out, re.M)
    assert re.search(r"^absent-try \(static, shape 3\): \d+ of \d+", out, re.M)
