# SPDX-License-Identifier: Apache-2.0
"""`python/scripts/planner_resync.py` -- the re-sync PROPOSER's own logic.

The freshness gate next door catches drift; this script proposes the fix. Both
halves are only worth having if the proposer is honest about what it could not
do, so most of what is asserted here is a REFUSAL:

* a hand-port source is flagged and never merged (conflating the two halves is
  how tan-side adaptations get silently discarded);
* a 3-way conflict writes nothing and blocks the pin;
* `STRICT_LOADERS_PINNED_SDK_COMMIT` never moves automatically;
* a pin whose hash table disagrees with it aborts the whole run rather than
  merging from a base that was never the audited text.

The integration tests build a miniature alp-sdk git repo rather than binding a
real checkout: the behaviour under test is the classifier, and a real SDK would
make these tests depend on whatever alp-sdk happens to be doing today (which is
exactly the coupling the freshness gate exists to make explicit, not something
to re-introduce here).
"""

from __future__ import annotations

import importlib.util
import os
import pathlib
import subprocess
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "python" / "scripts" / "planner_resync.py"
GATE = REPO_ROOT / "python" / "tests" / "gates" / "test_planner_relocation_freshness.py"


def _load():
    """Import the script by path -- `python/scripts/` is not a package.

    Registered in `sys.modules` BEFORE `exec_module`: `@dataclass` resolves its
    field annotations through `sys.modules[cls.__module__]`, so a module that
    is not yet registered raises `AttributeError: 'NoneType' object has no
    attribute '__dict__'` at class-creation time rather than anywhere useful.
    """
    spec = importlib.util.spec_from_file_location("planner_resync", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


pr = _load()


# ---------------------------------------------------------------- gate parse


def test_parse_gate_reads_the_real_gate_files_seven_constants():
    """Parsed, never imported -- see `parse_gate`'s own docstring."""
    gate = pr.parse_gate(GATE.read_text(encoding="utf-8"))
    assert len(gate.pinned_sdk_commit) == 40
    assert len(gate.hand_port_pinned_sdk_commit) == 40
    assert len(gate.strict_loaders_pinned_sdk_commit) == 40
    assert len(gate.strict_loaders_hash) == 64
    # The tables are non-empty and every value is a sha256.
    assert gate.pinned_hashes and gate.hand_port_hashes and gate.hand_port_sources
    for table in (gate.pinned_hashes, gate.hand_port_hashes):
        for name, value in table.items():
            assert len(value) == 64, f"{name} is not a sha256"


def test_parse_gate_refuses_a_file_missing_a_pin_rather_than_defaulting():
    text = GATE.read_text(encoding="utf-8").replace(
        "STRICT_LOADERS_PINNED_SDK_COMMIT = ", "SOMETHING_ELSE = ", 1
    )
    with pytest.raises(pr.Refused) as exc:
        pr.parse_gate(text)
    assert "strict_loaders_pinned_sdk_commit" in str(exc.value)


# -------------------------------------------------------------- gate rewrite

NEW = "0" * 39 + "1"


def test_rewrite_pin_keeps_the_line_shape_parity_yml_greps_for():
    """`parity.yml` fails when `^HAND_PORT_PINNED_SDK_COMMIT = "<40hex>"` does
    not match exactly once -- so the rewrite must preserve the anchored form
    AND the trailing comment, not just the value.

    The trailing comment is read out of the file rather than hardcoded
    (tan-cli#756 review). It used to be spelled `# alp-sdk origin/dev`
    inline here, which asserted preservation by memorising the comment that
    happened to be there -- and so quietly asserted a second thing, that the
    pin is always on `dev`. It is not always on `dev`: `88318e75` is
    reachable from `origin/main` only, deliberately (see that pin's own
    comment for why the tree stops one commit short of the `v0.16.0-rc1`
    back-merge). Capturing the comment makes this test say what it means --
    whatever trailing text a pin carries survives a rewrite -- and stops a
    legitimate pin move from failing a gate that has no opinion about which
    branch a pin sits on."""
    import re

    text = GATE.read_text(encoding="utf-8")
    before = re.search(
        r'^HAND_PORT_PINNED_SDK_COMMIT = "[0-9a-f]{40}"(?P<tail>.*)$', text, re.M)
    assert before is not None, "the pin line this test rewrites is not in the gate file"
    out = pr.rewrite_pin(text, "HAND_PORT_PINNED_SDK_COMMIT", NEW)
    grep = re.compile(r'^HAND_PORT_PINNED_SDK_COMMIT = "[0-9a-f]{40}"', re.M)
    assert len(grep.findall(out)) == 1
    assert f'HAND_PORT_PINNED_SDK_COMMIT = "{NEW}"{before.group("tail")}' in out, (
        "the rewrite dropped or altered the pin line's trailing comment"
    )
    # and the OTHER two pins are untouched
    gate_after = pr.parse_gate(out)
    gate_before = pr.parse_gate(text)
    assert gate_after.pinned_sdk_commit == gate_before.pinned_sdk_commit
    assert (
        gate_after.strict_loaders_pinned_sdk_commit
        == gate_before.strict_loaders_pinned_sdk_commit
    )


def test_rewrite_pin_refuses_a_name_it_cannot_find_exactly_once():
    with pytest.raises(pr.Refused):
        pr.rewrite_pin("x = 1\n", "PINNED_SDK_COMMIT", NEW)


def test_rewrite_hash_table_round_trips_and_can_add_and_drop_keys():
    text = GATE.read_text(encoding="utf-8")
    gate = pr.parse_gate(text)
    new = dict(gate.pinned_hashes)
    dropped = new.pop("loader.py")
    new["brand_new.py"] = "f" * 64
    out = pr.rewrite_hash_table(text, "PINNED_HASHES", new)
    after = pr.parse_gate(out)
    assert after.pinned_hashes == new
    assert dropped not in out
    # HAND_PORT_HASHES must be untouched by a PINNED_HASHES rewrite.
    assert after.hand_port_hashes == gate.hand_port_hashes


def test_insert_note_above_pin_puts_the_machine_written_caveat_in_the_file():
    text = GATE.read_text(encoding="utf-8")
    note = pr.audit_note("mirror", "a" * 40, "b" * 40, ["deadbee subject"], ["x: merged"])
    out = pr.insert_note_above_pin(text, "PINNED_SDK_COMMIT", note)
    head, _, _ = out.partition('PINNED_SDK_COMMIT = "')
    assert "MACHINE-WRITTEN" in head.rsplit("\n\n", 1)[-1]
    assert "does not\n#: auto-merge" in note or "auto-merge" in note
    assert "deadbee subject" in note
    # It stays parseable -- a note that broke `ast` would take the gate with it.
    assert pr.parse_gate(out).pinned_sdk_commit == pr.parse_gate(text).pinned_sdk_commit


# ------------------------------------------------------------- 3-way merge


def test_three_way_merge_returns_ours_untouched_when_upstream_did_not_move():
    ours = b"tan's own adapted line\n"
    base = theirs = b"upstream line\n"
    merged, conflicts = pr.three_way_merge(ours, base, theirs)
    assert conflicts == 0
    assert merged == ours


def test_three_way_merge_applies_an_upstream_delta_onto_an_adapted_file():
    base = b"a\nb\nc\n"
    theirs = b"a\nb\nc\nd\n"  # upstream appended
    ours = b"A-adapted-by-tan\nb\nc\n"  # tan adapted a different region
    merged, conflicts = pr.three_way_merge(ours, base, theirs)
    assert conflicts == 0
    assert merged == b"A-adapted-by-tan\nb\nc\nd\n"


def test_three_way_merge_reports_a_conflict_and_never_pretends_it_merged():
    base = b"a\nb\nc\n"
    theirs = b"a\nUPSTREAM\nc\n"
    ours = b"a\nTAN-ADAPTED\nc\n"
    merged, conflicts = pr.three_way_merge(ours, base, theirs)
    assert conflicts == 1
    assert b"<<<<<<<" in merged  # the caller must NOT write this


# --------------------------------------------------- pin-movement policy


def _verdict(mirror, hand_port=(), strict=(), head="b" * 40):
    return pr.Report(
        sdk_head=head,
        mirror_base="a" * 40,
        hand_port_base="a" * 40,
        strict_base="c" * 40,
        mirror=list(mirror),
        hand_port=list(hand_port),
        strict=list(strict),
    )


def _fv(path, status, **kw):
    return pr.FileVerdict(path, kw.pop("target", "t"), status, **kw)


def test_a_mirror_conflict_blocks_the_mirror_pin_and_the_hand_port_pin():
    rep = _verdict([_fv("m/a.py", "merged"), _fv("m/b.py", "conflict")])
    assert rep.verdict == "partial"
    assert rep.mirror_moves is False
    assert rep.hand_port_moves is False


def test_a_changed_hand_port_blocks_only_its_own_pin_not_the_mirrors():
    """tan-cli#296's two-audits-two-pins design, exercised: the mirror audit
    IS complete at the target ref, so its pin advances; the hand-port audit is
    not, so its pin stays and the gate stays red on that half alone."""
    rep = _verdict(
        [_fv("m/a.py", "merged")],
        hand_port=[_fv("scripts/gen_zephyr_board.py", "hand-port-changed")],
    )
    assert rep.verdict == "partial"
    assert rep.mirror_moves is True
    assert rep.hand_port_moves is False


def test_a_changed_strict_loaders_blocks_the_hand_port_pin_too():
    """Advancing HAND_PORT_PINNED_SDK_COMMIT past an unported
    `strict_loaders.py` change would be the same re-freeze in another table."""
    rep = _verdict(
        [_fv("m/a.py", "merged")],
        strict=[_fv("scripts/strict_loaders.py", "hand-port-changed")],
    )
    assert rep.hand_port_moves is False


def test_a_new_upstream_module_blocks_the_pin_and_is_never_invented():
    rep = _verdict([_fv("m/new.py", "new-upstream", target=None)])
    assert rep.verdict == "partial"
    assert rep.mirror_moves is False


def test_nothing_moved_means_no_proposal_at_all():
    rep = _verdict([_fv("m/a.py", "unchanged")])
    assert rep.verdict == "up-to-date"
    assert rep.mirror_moves is False
    assert rep.hand_port_moves is False


def test_strict_loaders_pin_is_declared_unmovable_rather_than_untracked():
    assert pr.PIN_MOVABLE["STRICT_LOADERS_PINNED_SDK_COMMIT"] is False
    assert pr.PIN_MOVABLE["PINNED_SDK_COMMIT"] is True
    assert pr.PIN_MOVABLE["HAND_PORT_PINNED_SDK_COMMIT"] is True


def test_render_markdown_says_hand_ports_were_flagged_not_copied():
    rep = _verdict(
        [_fv("m/a.py", "merged")],
        hand_port=[
            _fv(
                "scripts/alp_template.py",
                "hand-port-changed",
                detail="port it by hand",
            )
        ],
    )
    md = pr.render_markdown(rep, applied=True)
    assert "FLAGGED ONLY, never copied" in md
    assert "NEEDS A HUMAN" in md
    assert "Do not merge without reading the diff" in md
    # The pins table must not claim the hand-port pin moved.
    assert "| `HAND_PORT_PINNED_SDK_COMMIT` |" in md
    row = next(r for r in md.splitlines() if "HAND_PORT_PINNED_SDK_COMMIT" in r)
    assert row.rstrip().endswith("no |")
    strict_row = next(
        r for r in md.splitlines() if "STRICT_LOADERS_PINNED_SDK_COMMIT" in r
    )
    assert "never automatic" in strict_row


# ------------------------------------------------ tan-cli#1109 fault-1/text


def test_up_to_date_reason_names_the_range_and_why():
    """The explicit "log why" line for an `up-to-date` verdict -- printed to
    stderr by `main()` so a silent run (rc=0, no PR) reads as a measurement
    in the run LOG, not only in the job summary markdown."""
    rep = _verdict([_fv("m/a.py", "unchanged")], head="b" * 40)
    reason = pr.up_to_date_reason(rep)
    assert pr.MIRROR_DIR in reason
    assert rep.mirror_base[:8] in reason
    assert rep.sdk_head[:8] in reason
    assert "nothing to propose" in reason


def test_main_prints_the_up_to_date_reason_to_stderr_only_when_up_to_date(
    mini, capsys
):
    sdk, repo = mini
    base = _head(sdk)
    _write_gate(repo, sdk, base)

    assert pr.main(["--sdk-root", str(sdk), "--to", base, "--repo-root", str(repo)]) == 0
    err = capsys.readouterr().err
    assert "up to date" in err
    assert base[:8] in err

    # A run that DOES have something to propose must not print the
    # up-to-date reason -- it would be actively misleading on a `clean` verdict.
    (sdk / "scripts" / "alp_orchestrate" / "mirror.py").write_text(
        MINI_MIRROR + "line-d\n"
    )
    _run(sdk, "commit", "-qam", "feat: append line-d")
    head = _head(sdk)
    assert pr.main(["--sdk-root", str(sdk), "--to", head, "--repo-root", str(repo)]) == 0
    err2 = capsys.readouterr().err
    assert "up to date" not in err2


def test_partial_headline_does_not_claim_the_freshness_gate_goes_red():
    """tan-cli#1109: the old wording ("so the freshness gate stays RED on
    purpose") was measured false during PR #1103's review --
    `planner-resync.yml`'s own "Run the freshness gate" step binds each pin's
    checkout to a worktree pinned at that SAME unmoved commit, so it compares
    the pin to itself and passes by construction. The corrected text must
    say what actually stays red (this workflow's own recurring run), not the
    gate."""
    headline = pr._HEADLINE["partial"]
    assert "so the freshness gate stays RED on purpose" not in headline
    assert "passes" in headline or "trivially passes" in headline
    assert "workflow" in headline.lower()


def test_hand_port_moves_docstring_does_not_claim_the_gate_goes_red():
    doc = pr.Report.hand_port_moves.__doc__ or ""
    normalized = " ".join(doc.split())
    assert "gate stays red" not in normalized, doc


# ------------------------------------------------------- integration (git)


def _run(cwd, *args):
    subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        env={
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.invalid",
            "PATH": os.environ.get("PATH", ""),
            "HOME": str(cwd),
        },
    )


MINI_MIRROR = "line-a\nline-b\nline-c\n"
MINI_HANDPORT = "def hand_ported():\n    return 1\n"
MINI_STRICT = "def strict():\n    return 2\n"


@pytest.fixture()
def mini(tmp_path):
    """A miniature alp-sdk repo + a miniature tan checkout, wired to each other.

    Two upstream commits: the base (which the gate pins) and a head that moves
    the mirror module, the hand-port source, or neither, depending on what the
    test asks for.
    """
    sdk = tmp_path / "alp-sdk"
    (sdk / "scripts" / "alp_orchestrate").mkdir(parents=True)
    (sdk / "scripts" / "alp_project.py").write_text("# root marker\n")
    (sdk / "scripts" / "alp_orchestrate" / "mirror.py").write_text(MINI_MIRROR)
    (sdk / "scripts" / "gen_zephyr_board.py").write_text(MINI_HANDPORT)
    (sdk / "scripts" / "strict_loaders.py").write_text(MINI_STRICT)
    _run(sdk.parent, "init", "-q", "-b", "main", str(sdk))
    _run(sdk, "add", "-A")
    _run(sdk, "commit", "-qm", "base")

    repo = tmp_path / "tan"
    (repo / "python" / "tan" / "planner").mkdir(parents=True)
    (repo / "python" / "tests" / "gates").mkdir(parents=True)
    # tan's copy carries a local adaptation, as every real one does.
    (repo / "python" / "tan" / "planner" / "mirror.py").write_text(
        "line-a\nline-b (adapted by tan)\nline-c\n"
    )
    return sdk, repo


def _write_gate(repo, sdk, base_ref):
    import hashlib

    def h(rel):
        return hashlib.sha256((sdk / rel).read_bytes()).hexdigest()

    text = f'''"""mini gate"""
PINNED_SDK_COMMIT = "{base_ref}"  # alp-sdk origin/dev
PINNED_HASHES: dict[str, str] = {{
    "mirror.py": "{h('scripts/alp_orchestrate/mirror.py')}",
}}
HAND_PORT_PINNED_SDK_COMMIT = "{base_ref}"  # alp-sdk origin/dev
HAND_PORT_HASHES: dict[str, str] = {{
    "scripts/gen_zephyr_board.py": "{h('scripts/gen_zephyr_board.py')}",
}}
HAND_PORT_SOURCES: dict[str, str] = {{
    "zephyr_board.py": "scripts/gen_zephyr_board.py",
}}
STRICT_LOADERS_PINNED_SDK_COMMIT = "{base_ref}"  # alp-sdk origin/dev
STRICT_LOADERS_HASH = "{h('scripts/strict_loaders.py')}"
'''
    (repo / pr.GATE_REL).write_text(text)
    return pr.parse_gate(text)


def _head(sdk):
    return subprocess.run(
        ["git", "-C", str(sdk), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


def test_integration_clean_mirror_delta_merges_and_moves_both_pins(mini):
    sdk, repo = mini
    base = _head(sdk)
    gate = _write_gate(repo, sdk, base)
    # upstream appends -- a region tan did not adapt
    (sdk / "scripts" / "alp_orchestrate" / "mirror.py").write_text(
        MINI_MIRROR + "line-d\n"
    )
    _run(sdk, "commit", "-qam", "feat: append line-d")
    head = _head(sdk)

    rep = pr.classify(sdk, repo, gate, head)
    assert rep.verdict == "clean"
    assert [v.status for v in rep.mirror] == ["merged"]
    assert rep.mirror_moves and rep.hand_port_moves

    touched = pr.apply(repo, gate, rep)
    assert "python/tan/planner/mirror.py" in touched
    assert (repo / "python" / "tan" / "planner" / "mirror.py").read_text() == (
        "line-a\nline-b (adapted by tan)\nline-c\nline-d\n"
    )
    after = pr.parse_gate((repo / pr.GATE_REL).read_text())
    assert after.pinned_sdk_commit == head
    assert after.hand_port_pinned_sdk_commit == head
    # ...and the third pin did NOT move, deliberately.
    assert after.strict_loaders_pinned_sdk_commit == base


def test_integration_a_conflicting_mirror_delta_writes_nothing(mini):
    sdk, repo = mini
    base = _head(sdk)
    gate = _write_gate(repo, sdk, base)
    (sdk / "scripts" / "alp_orchestrate" / "mirror.py").write_text(
        "line-a\nline-b (changed upstream)\nline-c\n"
    )
    _run(sdk, "commit", "-qam", "feat: touch the line tan adapted")
    head = _head(sdk)

    rep = pr.classify(sdk, repo, gate, head)
    assert [v.status for v in rep.mirror] == ["conflict"]
    assert rep.verdict == "partial"
    before = (repo / "python" / "tan" / "planner" / "mirror.py").read_text()
    pr.apply(repo, gate, rep)
    after = (repo / "python" / "tan" / "planner" / "mirror.py").read_text()
    assert after == before, "a conflicted merge must never reach disk"
    assert "<<<<<<<" not in after
    assert pr.parse_gate((repo / pr.GATE_REL).read_text()).pinned_sdk_commit == base


def test_integration_a_hand_port_change_is_flagged_with_its_diff_never_copied(mini):
    sdk, repo = mini
    base = _head(sdk)
    gate = _write_gate(repo, sdk, base)
    (sdk / "scripts" / "gen_zephyr_board.py").write_text(
        "def hand_ported():\n    return 99\n"
    )
    _run(sdk, "commit", "-qam", "fix: change the hand-port source")
    head = _head(sdk)

    rep = pr.classify(sdk, repo, gate, head)
    (hp,) = rep.hand_port
    assert hp.status == "hand-port-changed"
    assert "python/tan/planner/zephyr_board.py" in (hp.target or "")
    assert "return 99" in hp.diff, "the upstream diff must be attached"
    assert rep.verdict == "partial"
    assert rep.hand_port_moves is False
    pr.apply(repo, gate, rep)
    after = pr.parse_gate((repo / pr.GATE_REL).read_text())
    assert after.hand_port_pinned_sdk_commit == base, "the pin must stay put"
    assert after.hand_port_hashes == gate.hand_port_hashes


def test_integration_a_strict_loaders_change_is_flagged_and_the_pin_stays(mini):
    sdk, repo = mini
    base = _head(sdk)
    gate = _write_gate(repo, sdk, base)
    (sdk / "scripts" / "strict_loaders.py").write_text("def strict():\n    return 3\n")
    _run(sdk, "commit", "-qam", "fix: change strict_loaders")
    head = _head(sdk)

    rep = pr.classify(sdk, repo, gate, head)
    (st,) = rep.strict
    assert st.status == "hand-port-changed"
    assert "never advances" in st.detail
    pr.apply(repo, gate, rep)
    assert (
        pr.parse_gate((repo / pr.GATE_REL).read_text()).strict_loaders_pinned_sdk_commit
        == base
    )


def test_integration_a_brand_new_upstream_module_blocks_rather_than_relocating(mini):
    sdk, repo = mini
    base = _head(sdk)
    gate = _write_gate(repo, sdk, base)
    (sdk / "scripts" / "alp_orchestrate" / "arrived.py").write_text("x = 1\n")
    _run(sdk, "add", "-A")
    _run(sdk, "commit", "-qm", "feat: a new upstream module")
    head = _head(sdk)

    rep = pr.classify(sdk, repo, gate, head)
    statuses = {v.path: v.status for v in rep.mirror}
    assert statuses["scripts/alp_orchestrate/arrived.py"] == "new-upstream"
    assert rep.verdict == "partial"
    assert not (repo / "python" / "tan" / "planner" / "arrived.py").exists()


def test_integration_a_pin_that_disagrees_with_its_hash_table_refuses(mini):
    """The exit-2 case: merging from a base that was never the audited text
    would produce a plausible diff built on a false premise."""
    sdk, repo = mini
    base = _head(sdk)
    gate = _write_gate(repo, sdk, base)
    lying = pr.Gate(
        pinned_sdk_commit=gate.pinned_sdk_commit,
        pinned_hashes={"mirror.py": "0" * 64},
        hand_port_pinned_sdk_commit=gate.hand_port_pinned_sdk_commit,
        hand_port_hashes=gate.hand_port_hashes,
        hand_port_sources=gate.hand_port_sources,
        strict_loaders_pinned_sdk_commit=gate.strict_loaders_pinned_sdk_commit,
        strict_loaders_hash=gate.strict_loaders_hash,
    )
    with pytest.raises(pr.Refused) as exc:
        pr.classify(sdk, repo, lying, base)
    assert "disagree about what was audited" in str(exc.value)


def test_integration_cli_exit_codes_are_0_clean_1_partial_2_refused(mini, tmp_path):
    sdk, repo = mini
    base = _head(sdk)
    _write_gate(repo, sdk, base)
    argv = ["--sdk-root", str(sdk), "--to", base, "--repo-root", str(repo)]

    md = tmp_path / "r.md"
    assert pr.main(argv + ["--markdown", str(md)]) == 0  # up-to-date
    assert "UP TO DATE" in md.read_text()

    (sdk / "scripts" / "gen_zephyr_board.py").write_text("def hand_ported():\n    return 5\n")
    _run(sdk, "commit", "-qam", "fix: hand-port moved")
    assert pr.main(
        ["--sdk-root", str(sdk), "--to", _head(sdk), "--repo-root", str(repo),
         "--markdown", str(md)]
    ) == 1
    assert "NEEDS A HUMAN" in md.read_text()

    assert pr.main(
        ["--sdk-root", str(tmp_path), "--to", "HEAD", "--repo-root", str(repo),
         "--markdown", str(md)]
    ) == 2
    assert "REFUSED" in md.read_text()
