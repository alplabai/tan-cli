# SPDX-License-Identifier: Apache-2.0
"""The release docs must agree with the workflow a `v*` tag actually runs.

tan-cli#383 / tan-cli#385. Four documents drifted from one workflow at the same
time, in the same direction -- each restating a fact `release.yml` had changed
underneath it:

* three of them said the tag must equal the workspace `Cargo.toml` version, and
  one said `verify-version` reads neither Python version file. The job has run
  `python/scripts/version_check.py` since the port, whose source of truth is
  `TAN_VERSION` and which deliberately does not read `Cargo.toml` at all. An
  operator following the docs bumps the frozen Rust version and fails the real
  gate, or misses the Python version entirely;
* one said `publish_npm` is hardcoded `if: false` and that no registry publish
  is on the release path. The job is real, opt-in through the repository
  variable `TAN_NPM_PUBLISH`;
* one still described the retired eight-asset / `--onefile` plan as current.

A FIFTH hand-written copy of those facts would drift the same way, so nothing
here is written down twice: every fact this gate compares against is READ OUT OF
`release.yml` (and, for the version gate, out of the script that job runs) at
test time. Change the workflow and the assertions move with it; change only a
doc and it goes red.

Scope is deliberately the four documents that describe the release contract
rather than every markdown file in the repo -- a doc that never claims anything
about the release cannot contradict it. A document that has become HISTORY
rather than contract opts out by carrying `HISTORICAL` in its opening lines,
and that banner is itself asserted below: "archived" has to be a fact a reader
sees, not merely an exclusion this file grants in private.
"""

from __future__ import annotations

import ast
import functools
import re
import tomllib
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "release.yml"
VERSION_CHECK = REPO_ROOT / "python" / "scripts" / "version_check.py"
PYPROJECT = REPO_ROOT / "python" / "pyproject.toml"

#: The documents that make claims about the release. `npm-shim/README.md` makes
#: them too but is owned by that package and already derives its wording from
#: the same workflow; add it here the day it stops.
#:
#: `release.yml`'s own COMMENTS are a fifth source and are scanned too, from
#: `_workflow_comments()` below rather than from here -- they are not a file a
#: reader opens whole, and the install-command check has no business in a
#: workflow. #383 listed them among the four places that had to be reconciled,
#: and they are the one place its first fix missed: every markdown file was
#: corrected while `release.yml`'s `verify-version` comment still said the tag
#: had to match "the crate version" before building "8 targets", neither of
#: which has been true since the port.
RELEASE_DOCS = (
    "README.md",
    "python/README.md",
    "docs/release-contract.md",
    "docs/python-release-feasibility.md",
)

#: A doc whose opening lines carry this word is a record of what was true once,
#: not a statement of what is true now, and is exempt from the comparisons
#: below. Only the opening lines count: the word appears mid-document in prose
#: ("the historical measurements that motivated all of this") where it means
#: nothing about the document as a whole.
HISTORICAL_BANNER = "HISTORICAL"
_BANNER_LINES = 15

#: Phrases that mark a registry command as one the reader must NOT run. The
#: check below is not "is this command mentioned" -- recording an unusable
#: command so nobody reinvents the package name is legitimate, and the root
#: README does exactly that for npm and crates.io. It is "is it mentioned
#: WITHOUT saying it does not work", which is what `pip install alp-tan` was
#: doing as python/README.md's primary install instruction (#385).
_UNUSABLE_MARKERS = (
    "404",
    "does not exist",
    "not published",
    "never published",
    "not on pypi",
    "do not use",
    "do not advertise",
    "does not resolve",
    "will not resolve",
    "cannot work",
    "is wrong",
)
#: How far from the command the marker may sit. Six lines is one short markdown
#: paragraph or a fenced block plus its lead-in.
_MARKER_WINDOW = 6

_NUMBER_WORDS = {
    "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "2": 2, "3": 3, "4": 4, "5": 5, "6": 6, "7": 7, "8": 8, "9": 9, "10": 10,
}
#: Asset-count claims in the two ASSERTIVE shapes -- the doc is stating the
#: total, so it has to be the matrix's total exactly. `one` is absent from
#: _NUMBER_WORDS on purpose: "one archive per target triple" is a claim about
#: SHAPE, not about how many ship.
_COUNT_CLAIMS = (
    re.compile(r"\*\*(\w+)\*\*\s+assets", re.I),
    re.compile(r"\b(\w+)\s+assets?\s+(?:ship|are published|per release)", re.I),
)

#: Every OTHER way a number lands next to the thing a release publishes.
#: Anchored on the number rather than on a leading `(\w+)`, because a leftmost
#: match otherwise eats the count: in "Plus two non-binary assets" a leading
#: `\w+` captures "Plus" and the scan resumes past "assets", so the number is
#: never looked at.
#:
#: Compared as an UPPER BOUND, not for equality, and that is the whole design.
#: These loose hits are frequently talking about a SUBSET -- "two non-binary
#: assets" (checksums + the envelope contract), "all four `tan-*` archives" --
#: and a subset may legitimately be any size up to the whole. What it can never
#: be is BIGGER than the whole, which is the shape every instance of this drift
#: has taken, because the plan being quoted is always the retired eight-asset
#: one: #383's own quotation "all eight assets now exist" (no bold, no
#: following verb -- both assertive patterns above miss it, so it could be
#: pasted into a live doc with this gate staying green) and `release.yml`'s
#: "before we build 8 targets" both exceed the four this matrix can produce.
#:
#: The lookarounds keep a version string out of it: `\b5\b` matches inside
#: `v0.5.0`, which put "5 assets" on every line mentioning `v0.5.0` and an
#: asset in the same breath.
_LOOSE_COUNT = re.compile(
    r"(?<![-.\w])(" + "|".join(sorted(_NUMBER_WORDS, key=len, reverse=True)) + r")"
    r"(?![-.\w])[\w`*,./ -]{0,24}?\b(assets?|archives?|binaries|targets?)\b",
    re.I,
)

#: A count in the past tense is describing the pipeline that WAS, which these
#: docs do deliberately and at length: "Eight targets were possible while the
#: binary was a `cargo` build" is the contract doc explaining why four is the
#: number now. Only a claim about what a release publishes today can contradict
#: the matrix, so a sentence carrying one of these is history and is skipped.
_PAST_TENSE = re.compile(r"\b(was|were|used to|no longer|retired)\b", re.I)


# ---------------------------------------------------------------------------
# Facts, read out of the workflow rather than restated
# ---------------------------------------------------------------------------
@functools.cache
def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


@functools.cache
def _published_assets() -> frozenset[str]:
    """Every asset name the `build` matrix produces -- the release's own list."""
    include = _workflow()["jobs"]["build"]["strategy"]["matrix"]["include"]
    return frozenset(entry["asset"] for entry in include)


def _runs(node: object) -> list[str]:
    """Every `run:` string under a workflow node, at any depth."""
    out: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "run" and isinstance(value, str):
                out.append(value)
            else:
                out.extend(_runs(value))
    elif isinstance(node, list):
        for item in node:
            out.extend(_runs(item))
    return out


@functools.cache
def _tag_gate_commands() -> str:
    """What the job that gates the tag on a version actually executes."""
    return "\n".join(_runs(_workflow()["jobs"]["verify-version"]))


@functools.cache
def _version_authority() -> tuple[str, str]:
    """`(repo-relative file, symbol)` the tag gate treats as the source of truth.

    Taken from `version_check.py`'s own reader -- the function the tag gate
    calls -- so moving the version somewhere else moves this expectation with
    it. Restating "python/tan/version.py" here would make this gate the fifth
    copy of the fact it exists to stop copying.
    """
    tree = ast.parse(VERSION_CHECK.read_text(encoding="utf-8"))
    reader = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "read_tan_version"
    )
    strings = [
        node
        for node in ast.walk(reader)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]
    # The path is assembled as `PYTHON_ROOT / "tan" / "version.py"`; source
    # order is the path order, which `ast.walk` does not preserve.
    parts = [
        node.value
        for node in sorted(strings, key=lambda n: (n.lineno, n.col_offset))
        if re.fullmatch(r"[a-z_]+(?:\.py)?", node.value)
    ]
    symbol = next(
        match.group(0)
        for node in strings
        if (match := re.search(r"[A-Z][A-Z0-9_]{3,}", node.value))
    )
    package_root = VERSION_CHECK.parent.parent.relative_to(REPO_ROOT).as_posix()
    return "/".join([package_root, *parts]), symbol


@functools.cache
def _non_authority_version_files() -> tuple[str, ...]:
    """Version files a doc must not present as what the tag is checked against.

    `Cargo.toml` is the one that matters and the one three docs still named:
    it versions the frozen Rust crates on their own cadence, and the tag gate
    stopped reading it when the assets stopped being cargo builds. Derived from
    the gate's own commands so that re-introducing the grep also re-permits the
    documentation -- the two must never be able to disagree again.
    """
    return tuple(name for name in ("Cargo.toml",) if name not in _tag_gate_commands())


@functools.cache
def _publish_jobs() -> dict[str, dict]:
    return {
        job_id: body
        for job_id, body in _workflow()["jobs"].items()
        if job_id.startswith("publish")
    }


def _literal_false(expression: object) -> bool:
    """A job switched off by construction, as opposed to one whose condition is
    simply not met on this run. `yaml` renders a bare `false` as a bool."""
    if expression is False:
        return True
    return isinstance(expression, str) and re.fullmatch(
        r"\$\{\{\s*false\s*\}\}|false", expression.strip(), re.I
    ) is not None


@functools.cache
def _gating_variables() -> dict[str, frozenset[str]]:
    """publish job -> the repository VARIABLES that arm it (`vars.X`).

    A channel behind a `vars.` gate is opt-in and OFF until someone sets it, so
    a doc that describes the channel has to name the switch; and an install
    command it serves is not usable just because the job exists.
    """
    out: dict[str, frozenset[str]] = {}
    for job_id, body in _publish_jobs().items():
        text = yaml.safe_dump(body)
        out[job_id] = frozenset(re.findall(r"vars\.([A-Z0-9_]+)", text))
    return out


@functools.cache
def _registry_channels() -> dict[str, str]:
    """registry -> `"absent"` | `"opt-in"` | `"default"`.

    `absent` there is no publish step at all; `opt-in` there is one but it is
    behind a repository variable, so nothing is on the registry until someone
    arms it; `default` it runs on every final tag. Only `default` makes an
    install command from that registry a thing a reader can be told to run.
    """
    commands = {"pypi": ("twine upload", "pypi-publish"),
                "crates.io": ("cargo publish",),
                "npm": ("npm publish",)}
    out: dict[str, str] = {}
    # Only the `publish*` jobs are scanned, and that is a structural rule of
    # this workflow rather than a shortcut: release_gate's own comment forbids
    # merging the publish channels into one job, so a channel always IS a
    # separate `publish*` job. Scanning every job instead matches release_gate's
    # prose ("npm published while ...") and reports the channel as unconditional.
    for registry, needles in commands.items():
        state = "absent"
        for job_id, body in _publish_jobs().items():
            for run in _runs(body):
                if any(needle in run for needle in needles):
                    state = "opt-in" if _gating_variables().get(job_id) else "default"
        out[registry] = state
    return out


# ---------------------------------------------------------------------------
# Reading the docs
# ---------------------------------------------------------------------------
def _text(rel: str) -> str:
    return (REPO_ROOT / rel).read_text(encoding="utf-8")


def _is_historical(rel: str) -> bool:
    head = _text(rel).splitlines()[:_BANNER_LINES]
    return any(HISTORICAL_BANNER in line for line in head)


def _live_docs() -> tuple[str, ...]:
    return tuple(rel for rel in RELEASE_DOCS if not _is_historical(rel))


@functools.cache
def _workflow_comments() -> str:
    """`release.yml`'s own prose: its `#` comments, as paragraphs.

    A comment is documentation a release operator reads -- and unlike the four
    markdown files it sits inside the thing it describes, which is exactly why
    it drifts unnoticed. It is never HISTORICAL: this file IS the executable
    truth, so a comment in it that contradicts the code below it is wrong by
    construction.

    Paragraphs rather than one blob, ended by a blank comment line, a `====`
    rule or any non-comment line. Without that, `_sentences` runs the header
    banner "releaseAssetForTarget MUST match this exactly" straight into the
    "Tag scheme :" field beneath it -- neither line ends in a full stop -- and
    reads the pair as a claim that the tag must match the asset map.
    """
    paragraphs: list[list[str]] = [[]]
    for raw in WORKFLOW.read_text(encoding="utf-8").splitlines():
        stripped = raw.strip()
        # Only whole-line comments. An inline `key: value # note` is annotating
        # one setting, not describing the release.
        if not stripped.startswith("#"):
            paragraphs.append([])
            continue
        body = stripped.lstrip("#").strip()
        if not body or re.fullmatch(r"[=~*-]{3,}", body):
            paragraphs.append([])
            continue
        paragraphs[-1].append(body)
    return "\n".join(" ".join(p).rstrip(".") + "." for p in paragraphs if p)


def _claim_sources() -> tuple[tuple[str, str], ...]:
    """(label, prose) for everything that describes this release to a human."""
    return (
        *((rel, _text(rel)) for rel in _live_docs()),
        (f"{WORKFLOW.relative_to(REPO_ROOT).as_posix()} (comments)", _workflow_comments()),
    )


def _sentences(text: str) -> list[str]:
    """Markdown wraps mid-sentence, so join first and split on terminators."""
    flat = re.sub(r"\s+", " ", text)
    return re.split(r"(?<=[.!?])\s+", flat)


def _tag_equality_claims(text: str) -> list[str]:
    """Sentences that tell a release operator what the tag has to equal."""
    return [
        sentence
        for sentence in _sentences(text)
        if re.search(r"\btags?\b", sentence, re.I)
        and re.search(r"\b(must|has to|have to)\b", sentence, re.I)
        and re.search(r"\b(equal|equals|match|matches)\b", sentence, re.I)
    ]


def _fenced(text: str) -> frozenset[int]:
    """0-based indices of the lines INSIDE a ``` fence.

    A leading blockquote marker still opens one: the root README records the
    dead npm commands as `> ```sh`.
    """
    inside, out = False, set()
    for index, line in enumerate(text.splitlines()):
        if re.match(r"\s*>?\s*```", line):
            inside = not inside
        elif inside:
            out.add(index)
    return frozenset(out)


def _uncaveated(rel: str, pattern: re.Pattern[str]) -> list[str]:
    """Lines matching `pattern` that read as something the reader should run.

    Two rules, because a fenced line and a prose line are not the same act.

    In PROSE, naming a command that does not work is legitimate -- the root
    README does it so nobody reinvents the package name -- and a caveat within
    `_MARKER_WINDOW` lines is what makes it a record rather than an offer.

    Inside a FENCE the command IS the instruction, so the refusal has to be in
    the copied text itself. This is #385's actual shape and the reason a window
    cannot see it: `python3 -m pip install alp-tan` sat in python/README.md's
    install fence, and the prose introducing that very fence is where the "not
    on PyPI" wording lives -- so a window check calls the block caveated while
    the first command a new user copies still 404s. Nothing about a paragraph
    six lines up survives the copy. The npm commands the root README preserves
    pass because they carry their own `# 404 today`.
    """
    text = _text(rel)
    lines = text.splitlines()
    fenced = _fenced(text)
    hits = []
    for index, line in enumerate(lines):
        if not pattern.search(line):
            continue
        if index in fenced:
            window = line.lower()
        else:
            window = " ".join(
                lines[max(0, index - _MARKER_WINDOW): index + _MARKER_WINDOW + 1]
            ).lower()
        if not any(marker in window for marker in _UNUSABLE_MARKERS):
            hits.append(f"{rel}:{index + 1}: {line.strip()}")
    return hits


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------
#: Every version source `version_check.py` compares, as the reader function
#: that fetches it. Asserted as a SET rather than a count so a drop names the
#: source it lost, and asserted at all because nothing else in the repo does:
#: when `npm-shim/package.json` was retired (tan-cli#1054), an audit found the
#: contract had no census of its own sources. `_version_authority()` reads
#: only `read_tan_version`, so removing any OTHER reader -- plus its call site
#: and its summary line -- left every gate green and the whole pytest suite
#: passing while `check()` silently compared one fewer file. That is the
#: over-deletion this exists to make loud.
#:
#: `read_changelog_headings` is reached through `changelog_problems()` rather
#: than called from `check()` directly, which is why the two assertions below
#: differ: DEFINED covers the reader surviving at all, CALLED covers `check()`
#: still routing through it.
_DECLARED_VERSION_READERS = frozenset(
    {"read_tan_version", "read_pyproject_version", "read_changelog_headings"}
)
_READERS_CALLED_BY_CHECK = frozenset({"read_tan_version", "read_pyproject_version"})


def _version_readers() -> tuple[frozenset[str], frozenset[str]]:
    """`(defined, called-by-check)` reader names, from `version_check.py`'s AST.

    Derived, not restated -- the same reason `_version_authority()` is.
    """
    tree = ast.parse(VERSION_CHECK.read_text(encoding="utf-8"))
    defined = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name.startswith("read_")
    }
    check = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "check"
    )
    called = {
        node.func.id
        for node in ast.walk(check)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id.startswith("read_")
    }
    return frozenset(defined), frozenset(called)


def test_version_check_still_reads_every_source_it_is_supposed_to():
    """A version source cannot be dropped silently.

    Failing here means `version_check.py` gained or lost a reader. If that was
    deliberate, update the two frozensets above IN THE SAME COMMIT and say why
    -- the point is that the change is stated, not that it is forbidden.
    """
    defined, called = _version_readers()
    assert defined == _DECLARED_VERSION_READERS, (
        f"version_check.py's reader set changed: "
        f"lost {sorted(_DECLARED_VERSION_READERS - defined)}, "
        f"gained {sorted(defined - _DECLARED_VERSION_READERS)}"
    )
    assert called == _READERS_CALLED_BY_CHECK, (
        f"check() no longer routes through the same readers: "
        f"lost {sorted(_READERS_CALLED_BY_CHECK - called)}, "
        f"gained {sorted(called - _READERS_CALLED_BY_CHECK)}"
    )


def test_the_documented_version_authority_is_the_one_the_tag_gate_reads():
    authority_file, symbol = _version_authority()
    wrong = []
    for label, text in _claim_sources():
        for sentence in _tag_equality_claims(text):
            if symbol in sentence:
                continue
            named = [f for f in _non_authority_version_files() if f in sentence]
            wrong.append(f"{label}: {sentence.strip()}" + (f"  [names {named[0]}]" if named else ""))
    assert not wrong, (
        f"A release doc tells the operator the tag must equal something other than\n"
        f"`{symbol}` in {authority_file}, which is what the tag gate actually\n"
        f"reads:\n\n  " + "\n  ".join(wrong) + "\n\n"
        f"`verify-version` runs: {_tag_gate_commands().strip()}\n"
        f"Following the doc instead bumps a version no gate reads, and fails the\n"
        f"real one after the tag is already pushed."
    )


def test_the_published_asset_names_come_from_the_build_matrix():
    """The contract doc's Targets-published table IS the extension's map, so it
    has to be the matrix, name for name -- not a snapshot of one."""
    text = _text("docs/release-contract.md")
    section = text.split("## Targets published", 1)[1].split("### Not published", 1)[0]
    documented = set(re.findall(r"tan-(?:x86_64|aarch64)-[A-Za-z0-9_.-]+", section))
    assert documented == set(_published_assets()), (
        "docs/release-contract.md's `## Targets published` table and "
        "release.yml's build matrix disagree.\n"
        f"  only in the doc      : {sorted(documented - _published_assets())}\n"
        f"  only in the workflow : {sorted(_published_assets() - documented)}"
    )


def test_a_stated_asset_count_matches_the_number_the_matrix_builds():
    expected = len(_published_assets())
    wrong = []
    for label, text in _claim_sources():
        for sentence in _sentences(text):
            if _PAST_TENSE.search(sentence):
                continue  # a count about the retired pipeline, not this one
            for pattern in _COUNT_CLAIMS:
                for word in pattern.findall(sentence):
                    count = _NUMBER_WORDS.get(word.lower())
                    if count is not None and count != expected:
                        wrong.append(f"{label}: states {word} assets -- {sentence.strip()[:120]}")
            for word, noun in _LOOSE_COUNT.findall(sentence):
                if _NUMBER_WORDS[word.lower()] > expected:
                    wrong.append(
                        f"{label}: {word} {noun.lower()}, more than the "
                        f"{expected} published -- {sentence.strip()[:120]}"
                    )
    assert not wrong, (
        f"release.yml's build matrix produces {expected} assets "
        f"({sorted(_published_assets())}), but:\n  " + "\n  ".join(wrong)
    )


def test_no_doc_claims_a_publish_job_the_workflow_does_not_have():
    """Two directions, both of which shipped: a doc calling a live job
    hardcoded-off, and a doc claiming the release path reaches no registry at
    all. Either one tells a release engineer not to look at a job that can
    publish."""
    off = {job for job, body in _publish_jobs().items() if _literal_false(body.get("if"))}
    problems = []
    for rel in _live_docs():
        text = _text(rel)
        if not off and re.search(r"if:\s*(\$\{\{\s*)?false", text):
            problems.append(
                f"{rel} attributes a hardcoded `if: false` to a publish job, but no "
                f"job in release.yml has one (publish jobs: {sorted(_publish_jobs())})"
            )
        if _publish_jobs() and re.search(r"\b(no|neither) registry publish", text, re.I):
            problems.append(
                f"{rel} says no registry publish is on the release path, but "
                f"release.yml defines {sorted(_publish_jobs())}"
            )
        for job, variables in _gating_variables().items():
            if job in text and not (variables & set(re.findall(r"[A-Z][A-Z0-9_]{3,}", text))):
                problems.append(
                    f"{rel} describes `{job}` without naming the repository "
                    f"variable that arms it ({sorted(variables)}) -- a reader "
                    f"cannot tell whether the channel is live"
                )
    assert not problems, "\n  ".join(["release docs vs release.yml:", *problems])


def test_an_install_command_for_an_unpublished_registry_is_never_offered():
    """tan-cli#385. `python3 -m pip install alp-tan` was python/README.md's
    PRIMARY instruction while `alp-tan` had never been published: no PyPI job
    exists in release.yml and `https://pypi.org/pypi/alp-tan/json` answers 404,
    so the first command a new user ran could only fail. The distribution name
    is read from pyproject, and whether each registry is on the release path is
    read from the workflow, so renaming the distribution or adding a real
    publish job both land here automatically."""
    pypi_name = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]["name"]
    patterns = {
        "pypi": re.compile(rf"""pip\s+install\s+["']?{re.escape(pypi_name)}\b"""),
        "crates.io": re.compile(r"cargo\s+install\s+alp-tan-cli\b"),
        "npm": re.compile(r"np[mx]\s+(?:i|install\s+-g|@alplabai)"),
    }
    problems = []
    for registry, state in _registry_channels().items():
        if state == "default":
            continue
        for rel in _live_docs():
            for hit in _uncaveated(rel, patterns[registry]):
                problems.append(f"[{registry}: {state}] {hit}")
    assert not problems, (
        "An install command is presented as usable for a registry this release "
        "does not publish to.\n"
        f"Channel states derived from release.yml: {_registry_channels()}\n"
        "Say plainly that it does not resolve (any of "
        f"{_UNUSABLE_MARKERS}) within "
        f"{_MARKER_WINDOW} lines of the command, or drop the command:\n  "
        + "\n  ".join(problems)
    )


def test_the_superseded_feasibility_report_says_so_in_its_opening_lines():
    """It opens on "all eight assets" and the retired `python-binaries.yml` /
    `--onefile` plan, marks a blocker OPEN that a later section closes, and
    keeps a "what I still need from you" list of things already done. As a
    record of a decision it is worth keeping; as something a release operator
    might read as current it is actively harmful, and nothing in its own text
    said which it was."""
    rel = "docs/python-release-feasibility.md"
    head = "\n".join(_text(rel).splitlines()[:_BANNER_LINES])
    assert HISTORICAL_BANNER in head, (
        f"{rel} describes the retired eight-asset release plan and must open "
        f"with a {HISTORICAL_BANNER} banner (within {_BANNER_LINES} lines) "
        f"pointing at docs/release-contract.md, which is the live contract. "
        f"Without it the report reads as current -- and this gate has to check "
        f"it against a workflow it deliberately no longer describes."
    )
    assert "release-contract" in _text(rel), (
        f"{rel}'s banner must link the live contract, or a reader who correctly "
        f"stops trusting this file has nowhere to go."
    )


def test_the_facts_are_actually_being_read():
    """Anti-tautology. Every assertion above is "no doc contradicts X"; if X
    silently came back empty -- a renamed job, a restructured matrix, a doc set
    that all opted out -- the whole file would pass having compared nothing."""
    assets = _published_assets()
    assert len(assets) >= 4 and all(a.startswith("tan-") for a in assets), assets
    assert "version_check.py" in _tag_gate_commands(), _tag_gate_commands()
    assert _version_authority()[1] == "TAN_VERSION", _version_authority()
    assert _non_authority_version_files() == ("Cargo.toml",), (
        f"expected Cargo.toml to be the version file the tag gate does NOT read, "
        f"got {_non_authority_version_files()}. If `verify-version` genuinely "
        f"reads Cargo.toml again, the authority check above is comparing against "
        f"nothing and the docs need rewriting to match -- decide deliberately, "
        f"rather than letting this gate quietly stop covering anything."
    )
    assert _publish_jobs(), "no publish job found in release.yml"
    assert any(_gating_variables().values()), _gating_variables()
    live = _live_docs()
    assert len(live) >= 3, live
    sources = _claim_sources()
    assert any(_tag_equality_claims(text) for _, text in sources), (
        "no document states what the tag must equal any more -- the version "
        "authority is now undocumented, which is the drift this gate exists for"
    )
    assert any(
        pattern.findall(text) for _, text in sources for pattern in _COUNT_CLAIMS
    ), "no document states how many assets a release publishes"

    # The workflow's comments are a claim source only while they parse as prose.
    # A `#`-stripping bug that returned "" would silently drop `release.yml`
    # from every check above -- and it is the one source whose drift #383's
    # first fix left in the tree.
    comments = _workflow_comments()
    assert _version_authority()[1] in comments and "Tag scheme" in comments, comments[:400]
    # `#` markers really are stripped (a paragraph may still open on `#349`).
    assert comments.startswith("SPDX-License-Identifier"), comments[:200]
    # The loose count tier has to be looking at something. If it matched
    # nothing, "eight assets" could be pasted anywhere and this gate would
    # still be green -- which is exactly the state #383 was filed against.
    assert any(_LOOSE_COUNT.findall(text) for _, text in sources), sources

    # A fenced install command is judged on its own line, so `_fenced` must
    # actually find the fences: with it empty, every command falls back to the
    # window and #385 becomes invisible again.
    assert _fenced(_text("python/README.md")), "no fenced block found in python/README.md"
