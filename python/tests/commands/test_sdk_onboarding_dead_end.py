# SPDX-License-Identifier: Apache-2.0
"""tan-cli#305 -- a clean host (no alp-sdk checkout anywhere, no `~/.alp`, an
empty cwd) must never be told to run `tan sdk install`/`tan sdk switch`: both
refuse outright in this build (`sdk_cmd._run_not_ported`, `sdk.not-ported`),
so recommending either is a dead end -- the FIRST command a new customer
types (`tan doctor`) used to name a subcommand that immediately refuses, with
no other documented way to reach `tan bootstrap` at all.

The invariant this file pins is NOT "these three strings changed" -- it is
"no next-steps text names a subcommand this build refuses", checked with one
shared assertion against every site that used to get this wrong
independently (`doctor_cmd.sdk_check`, `sdk_cmd.no_active_sdk_text`,
`bootstrap_cmd`'s SDK-unresolved refusal), so a FUTURE rewording that still
points at the same broken command keeps failing this, and a fourth site
added later that makes the same mistake fails it too.

`test_the_clean_host_sequence_from_the_issue_is_no_longer_a_dead_end` is the
one that matters most: it reproduces the exact three-command sequence
tan-cli#305 measured against the published asset (`tan doctor` -> `tan sdk
install <ver>` -> `tan bootstrap --dry-run`), end to end, as real
subprocesses in a hermetically clean `HOME`/cwd -- a green `pytest` run over
unit-level checks alone is what let the original defect ship.

Follow-up sweep (still tan-cli#305): the identical hardcoded string was found
independently duplicated in five more sites `ffaa1bf` did not own --
`generate_cmd`, `model_cmd`, `new_som_cmd`, `kconfig_cmd`, and the
library-level `build.token_substitution` -- each naming `tan sdk switch` as
the fix for an SDK its own resolver could not find. The
`test_*_never_recommends_a_refused_subcommand` functions below cover those
five the same way as the original three: against the shared
`assert_no_refused_subcommand_named` invariant, not a pin on the exact
replacement wording.

Third follow-up (tan-cli#381): the defect came back a FOURTH time, from a
direction the two sweeps above could not see -- `trace_cmd`,
`support_bundle_cmd` and `inspect_cmd` were PORTED AFTER #305 landed, and
each hardcoded `tan sdk switch` fresh from the oracle's own (honest, there)
wording instead of reading `NO_SDK_NEXT_STEPS`. A sweep only fixes the sites
that exist when it runs; what stops the next port is this file growing a case
per site, plus `test_readme_*` below, which pins the DOCUMENTATION -- #381's
own headline was that `README.md`'s primary quickstart told a new customer to
run `tan sdk install <version> && tan sdk switch <version>`, two hard
refusals, as the very first thing they type.

Fourth round, and the reason `test_no_shipped_string_literal_names_a_refused_
subcommand` now exists: the #381 sweep diagnosed the mechanism correctly
("a sweep only fixes the sites that exist when it runs") and then did not
build the guard that follows from it, because the guard "would go red
immediately" on a FIFTH live site (`validate_cmd`'s
`validate.sdk-root-unresolved`, written by tan-cli#376 while #381 was cleaning
the other four). That is the guard catching a real unfixed instance -- the
outcome it exists for -- so it is here, and the site it caught is fixed. A
case per site is a record of what already went wrong; the AST sweep is the
only thing in this file that can fail for a site nobody has written yet.

Second follow-up: three more sites in `bootstrap_cmd.py`'s workspace-
relocation and rollback-failure messages named `tan sdk switch --global` as
the way to repoint/undo the global default-SDK pointer (`~/.alp/sdk-
default`) -- same defect, a different flow (informing/recovering, not
onboarding). Those three tests below reuse `test_bootstrap_command.py`'s own
fixtures (`make_sdk`/`run_tan`/`envelope`/`codes`, and a direct `_run` call
for the one failure mode no filesystem trick reaches through a real
subprocess) rather than re-deriving that harness here -- the same
cross-module import `test_build_streaming.py` already uses against
`test_build_command.py`.
"""
import ast
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from tan.commands import bootstrap_cmd, doctor_cmd, generate_cmd, kconfig_cmd, model_cmd, new_som_cmd, sdk_cmd
from tan.commands.bootstrap_cmd import HostPython
from tan.commands.build.token_substitution import TokenSubstitutionError, apply_plan_token_substitution
from tan.commands.inspect_cmd import collect_resolved_values
from tan.core.build_plan import parse_build_plan
from tests.commands import test_inspect_command as inspect_mod
from tests.commands import test_support_bundle_command as support_mod
from tests.commands import test_trace_command as trace_mod
from tests.commands.test_bootstrap_command import PRESENT_TOOL, codes, envelope, make_sdk, run_tan

PACKAGE_ROOT = Path(__file__).resolve().parents[2]

_cli_runner = CliRunner()

_generate_app = typer.Typer(add_completion=False)
_generate_app.command("generate")(generate_cmd.generate)

_model_app = typer.Typer(add_completion=False)
_model_app.command("model")(model_cmd.model)

_new_som_app = typer.Typer()
_new_som_app.command("new-som")(new_som_cmd.new_som)

_kconfig_app = typer.Typer()
_kconfig_app.command("kconfig")(kconfig_cmd.kconfig)


def assert_no_refused_subcommand_named(text: str) -> None:
    """The shared invariant. `install`/`switch` both refuse
    (`sdk_cmd._run_not_ported`, `sdk.not-ported`) -- checked as the literal
    phrase a customer would actually type (`tan sdk install`/`tan sdk
    switch`), not a bare `"sdk install"` substring, because `west sdk
    install` (the REAL, working Zephyr SDK toolchain installer `doctor`'s
    `zephyrSdk` check legitimately recommends) would otherwise false-positive
    this check.
    """
    for verb in sdk_cmd.NOT_PORTED_SDK_SUBCOMMANDS:
        phrase = f"tan sdk {verb}"
        assert phrase not in text, f"names a refused subcommand ({phrase!r}):\n{text}"


# ── unit level: every site that used to hardcode the recommendation ─────────


def test_doctor_sdk_check_never_recommends_a_refused_subcommand():
    for project_scope in (None, "examples/uart-echo"):
        check = doctor_cmd.sdk_check(None, project_scope=project_scope)
        assert check.status == "fail"
        assert_no_refused_subcommand_named(check.detail)
        assert_no_refused_subcommand_named(check.fix or "")


def test_sdk_current_no_sdk_text_never_recommends_a_refused_subcommand():
    for cached in ([], ["0.13.0"]):
        text = "\n".join(sdk_cmd.no_active_sdk_text(cached))
        assert_no_refused_subcommand_named(text)


def test_python_floor_skew_fix_never_recommends_a_refused_subcommand():
    """The `pythonFloor` warning's `fix` text used to send an unresolved host
    to `tan sdk switch` -- the one other next-steps string tan-cli#305's own
    repro surfaced beside the three the issue named directly."""
    check = doctor_cmd.python_floor_skew_check(
        manifest_floor=doctor_cmd.FALLBACK_PYTHON_FLOOR,
        effective_floor=doctor_cmd.ZEPHYR_PYTHON_FLOOR,
        effective_source="zephyr python.cmake",
        manifest_is_real=False,
    )
    assert check is not None
    assert_no_refused_subcommand_named(check.fix or "")


def test_generate_sdk_root_unresolved_never_recommends_a_refused_subcommand(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "board.yaml").write_text("som:\n  sku: E1M-TEST\n", encoding="utf-8")
    result = _cli_runner.invoke(
        _generate_app,
        [
            "--project", str(project),
            "--sdk-root", str(tmp_path / "nope"),  # no scripts/alp_project.py
            "--format", "json",
        ],
    )
    assert result.exit_code == 2
    env = json.loads(result.stdout)
    assert env["issues"][0]["code"] == "generate.sdk-root-unresolved"
    assert_no_refused_subcommand_named(env["issues"][0]["message"])


def test_model_sdk_root_unresolved_never_recommends_a_refused_subcommand(tmp_path):
    (tmp_path / "board.yaml").write_text("som:\n  sku: E1M-TEST\n", encoding="utf-8")
    result = _cli_runner.invoke(
        _model_app,
        [
            "build",
            "--project", str(tmp_path),
            "--sdk-root", str(tmp_path / "nope"),
            "--format", "json",
        ],
    )
    assert result.exit_code == 2
    env = json.loads(result.stdout)
    assert env["issues"][0]["code"] == "model.sdk-root-unresolved"
    assert_no_refused_subcommand_named(env["issues"][0]["message"])


def test_new_som_sdk_root_unresolved_never_recommends_a_refused_subcommand(tmp_path):
    result = _cli_runner.invoke(
        _new_som_app,
        [
            "--dry-run",
            "--sdk-root", str(tmp_path),  # empty dir -- no scripts/alp_project.py
            "--sku", "E1M-XTST1",
            "--soc-ref", "a:b:c",
            "--family", "fam",
        ],
    )
    assert result.exit_code == 2
    assert "alp-sdk root is unresolved" in result.output
    assert_no_refused_subcommand_named(result.output)


def test_kconfig_no_sdk_root_never_recommends_a_refused_subcommand(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    result = _cli_runner.invoke(
        _kconfig_app,
        [
            "--project", str(proj),
            "--sdk-root", str(tmp_path / "nope"),
            "--format", "json",
        ],
    )
    assert result.exit_code == 2
    env = json.loads(result.stdout)
    assert env["issues"][0]["code"] == "kconfig.no-sdk-root"
    assert_no_refused_subcommand_named(env["issues"][0]["message"])


def test_token_substitution_sdk_root_unresolved_never_recommends_a_refused_subcommand():
    """The library-level site: `build.token_substitution.apply_plan_token_
    substitution` raises `TokenSubstitutionError` directly, with no CLI or
    envelope wrapping it here -- unlike the other four sites, this is a pure
    function call, not a typer command."""
    plan_json = """{
      "schemaVersion": 1, "generatedBy": "g", "planPathMode": "tokened",
      "boardYaml": "${PROJECT_ROOT}/board.yaml", "sku": "S", "buildRoot": "build",
      "slices": [], "sharedArtefacts": [], "warnings": []
    }"""
    plan = parse_build_plan(plan_json)
    with pytest.raises(TokenSubstitutionError) as excinfo:
        apply_plan_token_substitution(
            plan,
            board_yaml_path="/work/proj/board.yaml",
            exec_base="/work/proj",
            sdk_root=None,
            python="python3",
            toolchain_root=None,
        )
    assert excinfo.value.code == "build.sdk-root-unresolved"
    assert_no_refused_subcommand_named(excinfo.value.message)


# ── tan-cli#381: the three debug-family ports written after the #305 sweep ──


def test_trace_sdk_root_unresolved_never_recommends_a_refused_subcommand(tmp_path, monkeypatch):
    """`trace_cmd.py`'s `trace.sdk-root-unresolved` message. Reuses
    `test_trace_command.py`'s own local Typer app rather than `tan.cli.app`:
    `trace`/`inspect`/`support-bundle` are not registered there yet (see
    `test_inspect_command.py`'s module docstring), so a real subprocess would
    reach the deferred stub instead of the ported command."""
    monkeypatch.chdir(tmp_path)
    trace_mod.write(tmp_path / "board.yaml", "x")
    result = trace_mod.runner.invoke(trace_mod.app, ["trace", "--format", "json"])
    assert result.exit_code == 2
    issue = json.loads(result.stdout)["issues"][0]
    assert issue["code"] == "trace.sdk-root-unresolved"
    assert_no_refused_subcommand_named(issue["message"])
    assert sdk_cmd.NO_SDK_NEXT_STEPS in issue["message"]


def test_support_bundle_sdk_root_fix_never_recommends_a_refused_subcommand(
    tmp_path, monkeypatch
):
    """The `sdkRoot` check's `fix` inside the WRITTEN BUNDLE, not the stdout
    envelope -- `_debug_doctor_report` puts the remediation there, so an
    assertion against stdout alone would have passed while the bundle a stuck
    user actually reads kept naming the refused command."""
    monkeypatch.chdir(tmp_path)
    support_mod.write(tmp_path / "board.yaml", "x")
    monkeypatch.setattr(doctor_cmd, "_collect", lambda *a, **k: support_mod._clean_checks())
    result = support_mod.runner.invoke(
        support_mod.app, ["support-bundle", "--format", "json"]
    )
    assert result.exit_code == 4  # unresolved SDK is a hard doctor failure
    bundle_path = json.loads(result.stdout)["data"]["outputPath"]
    bundle = json.loads(Path(bundle_path).read_text(encoding="utf-8"))
    sdk_check = next(c for c in bundle["doctor"]["checks"] if c["name"] == "sdkRoot")
    assert sdk_check["status"] == "fail"
    assert_no_refused_subcommand_named(sdk_check["fix"])
    assert sdk_cmd.NO_SDK_NEXT_STEPS in sdk_check["fix"]


def test_inspect_unresolved_sdk_row_never_recommends_a_refused_subcommand():
    """`inspect`'s `sdkRoot` resolved-value row. A pure function call --
    `collect_resolved_values` builds the row from the context alone."""
    row = next(
        v
        for v in collect_resolved_values(inspect_mod._context())
        if v["key"] == "sdkRoot"
    )
    assert row["source"] == "unresolved"
    assert_no_refused_subcommand_named(row["detail"])
    assert sdk_cmd.NO_SDK_NEXT_STEPS in row["detail"]


# ── tan-cli#381: the recurrence guard, not another sweep ────────────────────
#
# Four rounds of this defect, each fixed by enumerating the sites that existed
# when someone looked: #305 found three, its own follow-up found five more,
# #381 found three ported after #305 landed, and #376 wrote a fifth WHILE #381
# was sweeping. Every per-site test above is a record of one round. None of
# them can fail for a site nobody has written yet, which is the only failure
# mode this defect has ever had.
#
# So: walk the source. One assertion over every string literal `python/tan`
# ships, in place of one test per site added after the fact.

SOURCE_ROOT = PACKAGE_ROOT / "tan"

#: `Module`/`FunctionDef`/`AsyncFunctionDef`/`ClassDef` -- the four nodes whose
#: first statement can be a docstring.
_DOCSTRING_OWNERS = (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


def _docstring_ids(tree: ast.AST) -> set[int]:
    """`id()` of every docstring Constant, so the sweep can skip them.

    Docstrings and `#` comments are EXEMPT, deliberately and for the same
    reason the README's prose is: half this codebase's docstrings exist to
    explain that `install`/`switch` refuse and why (`sdk_cmd`'s module
    docstring, `doctor_cmd.sdk_check`'s, `build_cmd`'s normalisation note).
    Forbidding the phrase there would delete the explanation and keep the
    defect. Comments never reach the AST at all, so they need no exemption.
    What is left after the exemption is every string that can reach a USER --
    an issue message, a `Check.fix`, a `detail`, a help string -- which is
    exactly the surface all four rounds of this defect landed on.
    """
    out: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, _DOCSTRING_OWNERS):
            continue
        body = getattr(node, "body", None)
        if not body or not isinstance(body[0], ast.Expr):
            continue
        first = body[0].value
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            out.add(id(first))
    return out


def test_no_shipped_string_literal_names_a_refused_subcommand():
    """The guard the #381 sweep identified and then skipped, because it "would
    go red immediately on the concurrent agent's in-flight `validate_cmd.py`
    line". That line WAS the fifth live site -- a guard going red on a real
    unfixed instance is the guard working, and skipping it is how the instance
    shipped.

    AST, not `grep`, for a reason this exact site demonstrates: the offending
    text was `"... pin one with "` `"\\`tan sdk switch <version|path>\\`, ..."`,
    split across two implicitly-concatenated source lines, so no line-oriented
    search for the phrase could match it. The parser folds implicit
    concatenation into ONE `Constant`, and reaches f-string literal parts
    (`JoinedStr`) too, which is where `NO_SDK_NEXT_STEPS`' siblings live.
    """
    offenders = []
    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        skip = _docstring_ids(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            if id(node) in skip:
                continue
            for verb in sdk_cmd.NOT_PORTED_SDK_SUBCOMMANDS:
                if f"tan sdk {verb}" in node.value:
                    offenders.append(
                        f"{path.relative_to(PACKAGE_ROOT)}:{node.lineno} -> {node.value!r}"
                    )
    assert not offenders, (
        "these shipped string literals name a subcommand this build refuses; "
        "route the remediation through `sdk_cmd.NO_SDK_NEXT_STEPS` instead:\n"
        + "\n".join(offenders)
    )


# ── tan-cli#381: the README is a surface too ────────────────────────────────

README = PACKAGE_ROOT.parent / "README.md"


def _fenced_blocks(markdown: str) -> list[str]:
    """EVERY fenced block body, whatever the info string says. What a customer
    COPIES, as opposed to prose about a command -- the README must stay free
    to say in words that `install`/`switch` refuse (it does, and should, in
    its own `> [!WARNING]`), while never handing anyone a line to paste that
    exits 1.

    tan-cli#381 follow-up: this used to whitelist ```sh / ```bash / ```console
    / ```shell, which skipped this README's two ```powershell blocks and its
    four unlabeled ones -- and the powershell blocks are the WINDOWS install
    sections, i.e. exactly where per-platform SDK advice would reappear.
    Proved by injecting `tan sdk switch 0.13.0` into a ```powershell block:
    the extractor returned it in no block and the guard stayed green. There is
    no language whose fenced content is safe to hand over unread, so the
    language filter is gone rather than extended -- an extended one is a list
    that needs the next language added to it by someone who remembers this."""
    blocks, current = [], None
    for raw in markdown.splitlines():
        # Strip the blockquote marker BEFORE anything else, opening and closing
        # fence alike: this README's npm section is a `> [!WARNING]` quote with
        # a fenced block inside it, and matching `>```` only on the way in left
        # the block permanently open -- every later line, prose included,
        # counted as shell.
        line = raw.lstrip()
        if line.startswith(">"):
            line = line[1:].lstrip()
        if line.startswith("```"):
            if current is not None:
                blocks.append("\n".join(current))
                current = None
            else:
                current = []
            continue
        if current is not None:
            current.append(line)
    return blocks


def test_the_fence_extractor_does_not_skip_powershell_or_unlabeled_blocks():
    """The drift-stopper's own drift-stopper. `_fenced_blocks` is the thing
    standing between the README and a pasteable dead end, so a silent hole in
    it disables the guard above without failing anything -- which is what a
    ```sh-only whitelist was. Synthetic input, not the real README: this must
    keep failing if the filter comes back even once the README happens to be
    clean."""
    markdown = "\n".join(
        [
            "```powershell",
            "tan sdk switch 0.13.0",
            "```",
            "",
            "```",
            "tan sdk install 0.13.0",
            "```",
            "",
            "```sh",
            "tan build",
            "```",
        ]
    )
    blocks = _fenced_blocks(markdown)
    assert blocks == ["tan sdk switch 0.13.0", "tan sdk install 0.13.0", "tan build"]
    for block in blocks[:2]:
        with pytest.raises(AssertionError):
            assert_no_refused_subcommand_named(block)


def test_readme_shell_blocks_never_tell_a_customer_to_run_a_refused_subcommand():
    """tan-cli#381's headline defect: the quickstart's own opening comment was
    "clone one, or `tan sdk install <version> && tan sdk switch <version>`" --
    two commands that exit 1 with `sdk.not-ported`, presented as THE primary
    way to get an SDK. Every ported refusal message had been cleaned by #305;
    the document a new customer actually starts from had not.

    Deliberately scans comment lines too. `# ... tan sdk install ...` inside a
    fenced block is read as an instruction, and that is exactly the form the
    defect took.
    """
    blocks = _fenced_blocks(README.read_text(encoding="utf-8"))
    assert blocks, "no fenced blocks found -- the extractor is broken"
    for block in blocks:
        assert_no_refused_subcommand_named(block)


def test_readme_quickstart_opens_on_a_mechanism_that_resolves_an_sdk():
    """The other half: removing the refused commands must not leave the
    quickstart with NO answer to "where does the alp-sdk checkout come from".
    Pins the two things that work -- a plain `git clone`, and `--sdk-root`."""
    text = README.read_text(encoding="utf-8")
    quickstart = text.split("## Quickstart", 1)[1].split("\n## ", 1)[0]
    assert "git clone https://github.com/alplabai/alp-sdk" in quickstart
    assert "--sdk-root" in quickstart


def test_the_readme_quickstart_opening_sequence_runs_on_a_clean_host(tmp_path):
    """The documentation smoke test #381 asks for: the quickstart's first two
    `tan` commands, taken FROM the README (not a copy that can drift out of
    sync with it), executed as real subprocesses on a host with no `~/.alp`,
    no pointer file and no SDK anywhere but the freshly-cloned checkout.

    Two deliberate divergences from the literal text, both additive:

    * `--no-west --no-pip` -- the documented `tan bootstrap --sdk-root
      ./alp-sdk` runs `west init`/`west update` and a pip install, i.e. the
      network. This test is about SDK RESOLUTION reaching bootstrap at all,
      which is the half #381 broke; the west/pip phases keep their coverage in
      `test_bootstrap_command.py`.
    * `--format json` on `init`, to read the outcome rather than scrape text.

    `make_sdk` stands in for `git clone` -- same marker file (`scripts/
    alp_project.py`, I-31) and same `metadata/bootstrap.json` the resolver and
    the prerequisite gate read, without a network.
    """
    quickstart = (
        README.read_text(encoding="utf-8").split("## Quickstart", 1)[1].split("\n## ", 1)[0]
    )
    commands = [
        line.split("#")[0].strip()
        for line in quickstart.splitlines()
        if line.startswith("tan ")
    ]
    # If the README reorders its opening, this test must be updated with it
    # rather than silently drifting onto a sequence nobody documents.
    assert commands[:2] == ["tan bootstrap --sdk-root ./alp-sdk", "tan init --name my-app"]

    sdk = make_sdk(tmp_path, tools=[PRESENT_TOOL])  # ends up at <tmp>/ws/alp-sdk
    workspace = sdk.parent
    bootstrap = run_tan(
        "bootstrap", "--sdk-root", "./alp-sdk", "--no-west", "--no-pip",
        "--format", "json", cwd=workspace,
    )
    assert bootstrap.returncode == 0, bootstrap.stdout + bootstrap.stderr
    assert envelope(bootstrap)["ok"] is True

    init = run_tan("init", "--name", "my-app", "--format", "json", cwd=workspace)
    assert init.returncode == 0, init.stdout + init.stderr
    assert (workspace / "my-app" / "board.yaml").is_file()
    # The quickstart's `cd my-app  # sibling ../alp-sdk resolves automatically`
    # claim, checked rather than assumed: the pointer `init` wrote is what
    # makes the refused `tan sdk switch` unnecessary in the first place.
    assert (workspace / "my-app" / ".alp" / "sdk-path").is_file()


# ── bootstrap: workspace-relocation + rollback-failure messages ─────────────


def test_workspace_relocated_note_never_recommends_a_refused_subcommand(tmp_path):
    """bootstrap_cmd.py:2083's own success note -- "here is how to change
    your default SDK later" -- used to name `tan sdk switch --global`. Not a
    recovery message (nothing is broken here), but the same dead end: the
    command it named refuses in this build."""
    sdk = make_sdk(tmp_path, tools=[PRESENT_TOOL])
    # Other content beside the checkout is what makes the auto-relocation
    # guard fire at all (`test_the_workspace_parent_guard_relocates_into_
    # alp_workspace_automatically`'s own setup, mirrored here): a topdir
    # holding nothing but the checkout needs no relocating.
    (sdk.parent / "unrelated.txt").write_text("x", encoding="utf-8")
    proc = run_tan(
        "bootstrap", "--no-west", "--no-pip", "--format", "json",
        "--sdk-root", str(sdk), cwd=sdk.parent,
    )
    env = envelope(proc)
    assert proc.returncode == 0
    message = next(
        i["message"] for i in env["issues"] if i["code"] == "bootstrap.workspace-relocated"
    )
    assert_no_refused_subcommand_named(message)
    # The real mechanism is named instead: the pointer file itself.
    assert "sdk-default" in message


def test_relocation_rollback_pointer_restore_failure_never_recommends_a_refused_subcommand(
    tmp_path,
):
    """bootstrap_cmd.py:2172 -- the checkout moved back after a later step
    failed, but the pointer restore that follows it did not, so the default
    SDK may still name the vacated path. Forces that exact failure with a
    pre-existing DIRECTORY at `~/.alp/sdk-default`: `_undo_relocation`'s
    `unlink`/`write_bytes` then raises `OSError` instead of degrading
    silently -- cross-platform, unlike a chmod-based permission-denied
    repro (the same reasoning `test_a_successful_move_back_with_a_failed_
    pointer_restore_is_not_reported_as_still_relocated` gives for avoiding
    chmod, applied through a real subprocess instead of a monkeypatch)."""
    sdk = make_sdk(tmp_path, tools=[PRESENT_TOOL])
    workspace = tmp_path / "elsewhere"
    workspace.mkdir()
    # Blocks `python -m venv` from creating the venv directory: the same
    # real, deterministic, network-free failure
    # `test_a_relocation_is_rolled_back_when_a_later_step_fails` uses.
    (workspace / ".venv").write_text("not a directory", encoding="utf-8")
    home = tmp_path / "fake-home"  # `run_tan` derives this from `cwd.parent`
    (home / ".alp" / "sdk-default").mkdir(parents=True)

    proc = run_tan(
        "bootstrap", "--format", "json",
        "--sdk-root", str(sdk), "--workspace", str(workspace), cwd=sdk.parent,
    )
    env = envelope(proc)
    assert proc.returncode != 0
    issue_codes = codes(env)
    assert "bootstrap.workspace-relocated" in issue_codes
    assert "bootstrap.workspace-relocation-rolled-back" in issue_codes
    message = next(
        i["message"] for i in env["issues"]
        if i["code"] == "bootstrap.workspace-relocation-rolled-back"
    )
    # Proves this hit the moved-back-but-pointer-stuck branch (2172), not the
    # clean-restore one beside it (unchanged, untouched by this sweep).
    assert "could not be restored" in message
    assert_no_refused_subcommand_named(message)
    assert "sdk-default" in message


def test_relocation_rollback_move_back_refused_never_recommends_a_refused_subcommand(
    tmp_path, monkeypatch
):
    """bootstrap_cmd.py:2192 -- the worst of the three: the move-back itself
    refused (the vacated original path was recreated in the meantime,
    `relocate_checkout`'s own already-exists guard), so the checkout is
    STILL at the relocated path and the default SDK still points there.
    Simulates the recreation with a `relocate_checkout` wrapper that
    recreates the vacated path only on the SECOND call (the rollback's own
    move-back attempt) -- the same race `test_a_blocked_rollback_reports_
    the_checkout_as_still_relocated` reproduces against `_undo_relocation`
    directly; this drives it through the real `_run` message-building code
    instead, which a monkeypatch-free real subprocess cannot reach (nothing
    in a single synchronous run recreates the vacated path on its own)."""
    sdk = make_sdk(tmp_path, tools=[PRESENT_TOOL])
    workspace = tmp_path / "elsewhere"
    workspace.mkdir()
    (workspace / ".venv").write_text("not a directory", encoding="utf-8")

    real_relocate = bootstrap_cmd.relocate_checkout
    calls = {"n": 0}

    def flaky_relocate(repo_root, target_parent, dry_run=False):
        calls["n"] += 1
        if calls["n"] == 2:
            (target_parent / repo_root.name).mkdir(parents=True)
        return real_relocate(repo_root, target_parent, dry_run=dry_run)

    monkeypatch.setattr(bootstrap_cmd, "relocate_checkout", flaky_relocate)
    monkeypatch.setattr(
        bootstrap_cmd, "probe_host_python", lambda _floor: HostPython((sys.executable,), (3, 12))
    )
    monkeypatch.setenv("HOME", str(tmp_path / "fake-home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "fake-home"))

    outcome, _project, _sdk_info = bootstrap_cmd._run(
        project=str(sdk.parent),
        board_yaml=None,
        sdk_root_flag=str(sdk),
        no_pip=False,
        no_west=True,
        print_env=False,
        allow_partial=False,
        workspace=str(workspace),
        dry_run=False,
        json_mode=True,
    )
    assert calls["n"] == 2  # both the relocation AND the rollback's move-back ran
    issue_codes = [i.code for i in outcome.issues]
    assert "bootstrap.workspace-relocation-rolled-back" in issue_codes
    message = next(
        i.message for i in outcome.issues
        if i.code == "bootstrap.workspace-relocation-rolled-back"
    )
    # Proves this hit the still-relocated branch (2192), not the
    # moved-back-but-pointer-stuck one above.
    assert "could NOT move it back" in message
    assert_no_refused_subcommand_named(message)
    assert "sdk-default" in message


# ── end to end: the real dead end, reproduced against a live subprocess ─────


def _run_tan(*argv, cwd, home):
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(
            [str(PACKAGE_ROOT), *([p] if (p := os.environ.get("PYTHONPATH")) else [])]
        ),
        "HOME": str(home),
        "USERPROFILE": str(home),
    }
    return subprocess.run(
        [sys.executable, "-m", "tan", *argv],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(cwd),
        env=env,
        timeout=120,
    )


def test_the_clean_host_sequence_from_the_issue_is_no_longer_a_dead_end(tmp_path):
    """The exact repro tan-cli#305 measured on the published `v0.5.0-rc2`
    asset: no alp-sdk checkout anywhere, `~/.alp` absent, an empty cwd.

    `sdk install` keeps refusing (exit 1, `sdk.not-ported`) -- that refusal
    is correct and this test does not ask it to change (see `sdk_cmd`'s
    module docstring). What must change is that NEITHER `tan doctor` nor
    `tan bootstrap` sends the customer to it, or to `sdk switch`, leaving
    `--sdk-root <path>` (and how to get a checkout at all) as the one path
    forward that is actually named.
    """
    home = tmp_path / "home"
    cwd = tmp_path / "cwd"
    home.mkdir()
    cwd.mkdir()

    doctor = _run_tan("doctor", "--format", "text", cwd=cwd, home=home)
    assert "Traceback" not in doctor.stderr
    assert doctor.returncode != 0, "an unresolved SDK must not read as a healthy host"
    assert_no_refused_subcommand_named(doctor.stderr)

    install = _run_tan("sdk", "install", "v0.13.0", cwd=cwd, home=home)
    assert install.returncode == 1
    assert "not available in this build of tan" in install.stderr

    bootstrap = _run_tan("bootstrap", "--dry-run", cwd=cwd, home=home)
    assert "Traceback" not in bootstrap.stderr
    assert bootstrap.returncode != 0
    assert_no_refused_subcommand_named(bootstrap.stderr)
    # The honest, working way forward must actually be named.
    assert "--sdk-root" in bootstrap.stderr
    assert "git clone" in bootstrap.stderr


def test_sdk_help_no_longer_advertises_install_and_switch_as_plain_options():
    """`tan sdk --help` used to list `install`/`switch` beside `list`/
    `current` with nothing marking that two of the four refuse."""
    result = subprocess.run(
        [sys.executable, "-m", "tan", "sdk", "--help"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={
            **os.environ,
            "PYTHONPATH": os.pathsep.join(
                [str(PACKAGE_ROOT), *([p] if (p := os.environ.get("PYTHONPATH")) else [])]
            ),
        },
        timeout=60,
    )
    assert result.returncode == 0
    help_text = result.stdout
    assert "install" in help_text and "switch" in help_text
    assert "refuse" in help_text.lower()
