# SPDX-License-Identifier: Apache-2.0
"""``tan init`` -- a fresh customer's first command, and the failures that must
never reach them as a traceback.

Driven as a real subprocess, like ``test_build_command.py`` and
``test_doctor_command.py``: the three things worth asserting here are ONE JSON
document on stdout, the exit code, and that no input can replace the envelope
with a Python traceback -- an in-process call exercises none of them. The two
committed goldens (``contract/envelopes/init-preview-minimal-app`` and
``init-invalid-template``) pin the exact envelope shape; these cases cover the
paths no golden reaches.

``--preview`` writing nothing is asserted by WALKING the destination afterwards,
not by trusting the code path. It is the one promise a customer evaluating tan
against a real project relies on, and the Rust learned the adjacent lesson the
hard way: the overwrite guard used to run BEFORE the preview branch, so a
preview of a project with local edits failed with ``init.would-overwrite``
instead of answering.
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
import typer
import yaml

from tan.commands.init_cmd import overwrite_refusal_message
from tan.core.scaffold import TEMPLATE_IDS

#: ``python/`` -- pinned onto the child's PYTHONPATH so ``python -m tan``
#: resolves from a scratch cwd without a ``pip install``.
PACKAGE_ROOT = Path(__file__).resolve().parents[2]


def run_tan(*argv, cwd, env_extra=None):
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(
            [str(PACKAGE_ROOT), *([p] if (p := os.environ.get("PYTHONPATH")) else [])]
        ),
        **(env_extra or {}),
    }
    return subprocess.run(
        [sys.executable, "-m", "tan", *argv],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(cwd),
        env=env,
    )


def envelope(proc):
    """The one JSON document on stdout. Fails loudly on zero or two -- both are
    the same break for a consumer that parses stdout whole."""
    assert proc.stdout.strip(), f"no envelope on stdout; stderr:\n{proc.stderr}"
    assert "Traceback" not in proc.stderr, f"an exception escaped the contract:\n{proc.stderr}"
    return json.loads(proc.stdout)


def tree(root: Path):
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())


# ---------------------------------------------------------------------------
# --preview
# ---------------------------------------------------------------------------


def test_preview_writes_nothing_at_all(tmp_path):
    proc = run_tan("init", "--template", "minimal-app", "--preview", "--format", "json", cwd=tmp_path)
    env = envelope(proc)

    assert proc.returncode == 0
    assert env["data"]["preview"] is True
    assert env["data"]["written"] == []
    assert [c["kind"] for c in env["data"]["fileChanges"]] == ["new"] * 8
    # The actual promise: not one file, not one directory, not even `.alp/`.
    assert list(tmp_path.iterdir()) == [], "--preview must not touch disk"


def test_preview_of_a_project_with_local_edits_still_answers(tmp_path):
    """Regression guard ported with the code: the overwrite guard must stay
    BEHIND the preview branch. A read-only question has nothing to guard."""
    assert run_tan("init", "--template", "minimal-app", "--format", "json", cwd=tmp_path).returncode == 0
    board = tmp_path / "board.yaml"
    board.write_text(board.read_text(encoding="utf-8") + "# local edit\n", encoding="utf-8")

    proc = run_tan("init", "--template", "minimal-app", "--preview", "--format", "json", cwd=tmp_path)
    env = envelope(proc)

    assert proc.returncode == 0, "a preview must never fail on disk state"
    kinds = {c["relativePath"]: c["kind"] for c in env["data"]["fileChanges"]}
    assert kinds["board.yaml"] == "update"
    assert "# local edit" in board.read_text(encoding="utf-8"), "preview overwrote a local edit"


@pytest.mark.parametrize("template_id", TEMPLATE_IDS)
def test_every_registered_template_plans_files_with_no_sdk_anywhere(template_id, tmp_path):
    """I-32: `tan init` is SDK-free. This runs in an empty temp directory with no
    alp-sdk checkout reachable, and every template must still produce a plan.

    Also the guard that tan's vendored template data actually SHIPS: five of the
    six templates read `tan/templates/vendored/`, so a build (or a frozen binary
    built without the `--add-data`) that drops it fails here rather than in a
    customer's first command.
    """
    proc = run_tan(
        "init", "--template", template_id, "--preview", "--format", "json", cwd=tmp_path
    )
    env = envelope(proc)

    assert proc.returncode == 0, env["issues"]
    assert env["data"]["templateId"] == template_id
    assert len(env["data"]["fileChanges"]) >= 6
    paths = [c["relativePath"] for c in env["data"]["fileChanges"]]
    assert "board.yaml" in paths
    # Platform-independent order: sorted by the relative POSIX path, so
    # `CMakeLists.txt` precedes `board.yaml` on Windows exactly as on Linux
    # (`PurePath` ordering is case-folded on Windows and would not).
    assert paths == sorted(paths) or template_id == "minimal-app"


# ---------------------------------------------------------------------------
# The write path
# ---------------------------------------------------------------------------


def test_scaffolded_board_yaml_carries_no_top_level_os_key(tmp_path):
    """I-01/I-02: the OS is derived from each core's Cortex class, never
    selected. There is no `--os` flag to reject, so the assertion is on the
    artefact: a top-level `os:` is the shape a selectable OS would leave."""
    assert run_tan("init", "--template", "minimal-app", "--format", "json", cwd=tmp_path).returncode == 0
    lines = (tmp_path / "board.yaml").read_text(encoding="utf-8").splitlines()

    assert not any(line.startswith("os:") for line in lines), lines
    assert any(line.startswith("cores:") for line in lines)


def test_written_files_keep_lf_endings_on_every_platform(tmp_path):
    """The vendored trees are an LF capture and are written verbatim. Universal
    newline translation would CRLF every one on Windows, breaking byte-parity
    with the Rust binary's output for the same command."""
    assert run_tan("init", "--template", "zephyr-app", "--format", "json", cwd=tmp_path).returncode == 0

    crlf = [
        p.relative_to(tmp_path).as_posix()
        for p in tmp_path.rglob("*")
        if p.is_file() and b"\r\n" in p.read_bytes()
    ]
    assert crlf == [], f"newline translation crept in: {crlf}"


def test_rerunning_an_unchanged_project_reports_unchanged_not_written(tmp_path):
    assert run_tan("init", "--template", "minimal-app", "--format", "json", cwd=tmp_path).returncode == 0
    proc = run_tan("init", "--template", "minimal-app", "--format", "json", cwd=tmp_path)
    env = envelope(proc)

    assert proc.returncode == 0
    assert env["data"]["written"] == []
    assert len(env["data"]["unchanged"]) == 8


def test_name_creates_a_subdirectory_under_the_destination(tmp_path):
    proc = run_tan(
        "init", "--template", "minimal-app", "--name", "my-app", "--format", "json", cwd=tmp_path
    )
    env = envelope(proc)

    assert proc.returncode == 0
    # `project.root`/`destination` stay the DESTINATION, not the nested project
    # dir -- that is where the caller pointed tan.
    assert env["project"]["root"] == "."
    assert (tmp_path / "my-app" / "board.yaml").is_file()


def test_text_mode_writes_nothing_to_stdout(tmp_path):
    """stdout is the envelope channel and carries nothing else, in either
    format. The human recap goes to stderr, which carries no contract."""
    proc = run_tan("init", "--template", "minimal-app", "--preview", cwd=tmp_path)

    assert proc.returncode == 0
    assert proc.stdout == ""
    assert "preview for template 'minimal-app'" in proc.stderr


def test_text_mode_created_line_reports_the_nested_path_not_just_the_destination(tmp_path):
    """tan-cli#4: with `--name`, the "created" line used to echo the bare
    `--destination` (here `.`) even though files landed under `my-app/` --
    telling an operator tan had created a directory it had not."""
    proc = run_tan(
        "init", "--template", "minimal-app", "--name", "my-app", cwd=tmp_path
    )

    assert proc.returncode == 0
    created_line = next(line for line in proc.stderr.splitlines() if "created" in line)
    assert "my-app" in created_line, created_line
    assert created_line != "init: created '.' from template 'minimal-app'", created_line


def test_text_mode_created_line_keeps_the_dot_destination_like_the_oracle(tmp_path):
    """scaffold-cx review, Finding 5: the port used to render the "created"
    line from `pathlib.Path(".") / name`, and pathlib silently drops a leading
    `.` component -- printing `'my-app'` where the oracle's `PathBuf::join`
    (`from_example.rs:70-74`, `.display()`) prints `'./my-app'`
    (`'.\\my-app'` on Windows). Assert the exact, separator-aware string."""
    proc = run_tan("init", "--template", "minimal-app", "--name", "my-app", cwd=tmp_path)

    assert proc.returncode == 0
    created_line = next(line for line in proc.stderr.splitlines() if "created" in line)
    assert f"created '.{os.sep}my-app'" in created_line, created_line


# ---------------------------------------------------------------------------
# Every failure is a coded issue (the port's recurring bug class)
# ---------------------------------------------------------------------------


def issue(env):
    assert len(env["issues"]) == 1, env["issues"]
    return env["issues"][0]


def test_unknown_template_is_a_validation_failure(tmp_path):
    proc = run_tan("init", "--template", "nope", "--format", "json", cwd=tmp_path)
    env = envelope(proc)

    assert proc.returncode == 2
    assert issue(env)["code"] == "init.invalid-template"
    # The error asymmetry the golden pins: nothing was created, so there is no
    # project to point a consumer at and no resolved template to report.
    assert env["project"]["root"] is None
    assert env["data"]["templateId"] == ""
    assert env["data"]["destination"] == ""
    assert list(tmp_path.iterdir()) == []


def test_help_distinguishes_template_ids_from_the_sdk_example_catalog(tmp_path):
    """tan-cli#1: `--template` and the SDK's `metadata/templates/catalog-v1.json`
    are two different id vocabularies (`zephyr-app` vs. `minimal`, and the
    catalog's `peripheral`/`multicore-rpmsg`/`gateway` have no `--template`
    counterpart at all) -- deliberately, not a bug to unify by renaming one to
    match the other. The help text must say so and point at --from-example."""
    proc = run_tan("init", "--help", cwd=tmp_path)
    # Rich wraps the boxed help text at terminal width, splitting words across
    # lines and box-bounded rows -- normalise both away before substring checks.
    # Both bar characters, not just the ASCII one: Rich draws the box with
    # U+2502 `│`, so at a narrow terminal width "curated starter set" wraps to
    # `curated │ │ starter set` and an ASCII-only strip leaves the assertion
    # failing on terminal width alone -- green on a wide dev console, red in CI.
    text = " ".join(proc.stdout.replace("|", " ").replace("│", " ").split())

    assert proc.returncode == 0
    assert "--from-example" in text
    assert "curated starter set" in text


def test_conflicting_files_refuse_without_force_and_write_nothing(tmp_path):
    assert run_tan("init", "--template", "minimal-app", "--format", "json", cwd=tmp_path).returncode == 0
    board = tmp_path / "board.yaml"
    board.write_text("# hand-written, do not clobber\n", encoding="utf-8")
    before = tree(tmp_path)

    proc = run_tan("init", "--template", "minimal-app", "--format", "json", cwd=tmp_path)
    env = envelope(proc)

    assert proc.returncode == 3
    assert issue(env)["code"] == "init.would-overwrite"
    assert env["data"]["written"] == []
    assert board.read_text(encoding="utf-8") == "# hand-written, do not clobber\n"
    assert tree(tmp_path) == before

    forced = run_tan("init", "--template", "minimal-app", "--force", "--format", "json", cwd=tmp_path)
    assert forced.returncode == 0
    assert envelope(forced)["data"]["written"] == ["board.yaml"]


def test_destination_that_is_a_file_is_a_write_failure_not_a_crash(tmp_path):
    (tmp_path / "afile").write_text("x\n", encoding="utf-8")

    proc = run_tan(
        "init", "--template", "minimal-app", "--destination", "afile", "--format", "json", cwd=tmp_path
    )
    env = envelope(proc)

    assert proc.returncode == 3
    assert issue(env)["code"] == "init.write-failed"
    assert "not a directory" in issue(env)["message"]
    assert (tmp_path / "afile").read_text(encoding="utf-8") == "x\n"


def test_a_blocked_write_reports_the_files_that_did_land(tmp_path):
    """A directory sitting where a file must go blocks the write deterministically
    on every platform (the Rust suite's own trick). `written: []` for a project
    that is really half on disk leaves a consumer nothing to clean up.

    `--force` is required to get here at all, and that is correct: an existing
    path whose content cannot be read compares unequal to the planned content, so
    the diff calls it an `update` and the overwrite guard refuses first.
    """
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.c").mkdir()

    proc = run_tan("init", "--template", "minimal-app", "--force", "--format", "json", cwd=tmp_path)
    env = envelope(proc)

    assert proc.returncode == 3
    assert issue(env)["code"] == "init.write-failed"
    assert "board.yaml" in env["data"]["written"], env["data"]
    assert "src/main.c" not in env["data"]["written"]


def test_traversal_in_name_is_refused(tmp_path):
    """`--name` is joined onto the destination, so an unchecked `..` put the
    project root -- and with `--force` an overwrite target -- outside it."""
    proc = run_tan(
        "init", "--template", "minimal-app", "--name", "../escape", "--format", "json", cwd=tmp_path
    )
    env = envelope(proc)

    assert proc.returncode == 2
    assert issue(env)["code"] == "init.invalid-name"
    assert not (tmp_path.parent / "escape").exists()


def test_iot_starter_rejects_an_unsupported_som_before_planning(tmp_path):
    proc = run_tan(
        "init", "--template", "iot-starter", "--som", "E1M-V2N101", "--format", "json", cwd=tmp_path
    )
    env = envelope(proc)

    assert proc.returncode == 2
    assert issue(env)["code"] == "init.invalid-som"
    assert "E1M-AEN801" in issue(env)["message"]
    assert list(tmp_path.iterdir()) == []


def test_multicore_mailbox_rejects_a_som_the_sdk_itself_refuses_to_emit_for(tmp_path):
    """tan-cli#864. The SDK gates this template's `supported.som_skus` to
    `['E1M-AEN801']` and refuses everything else outright:

        $ alp_project.py --emit scaffold --template multicore-mailbox --sku E1M-AEN301
        alp_project: multicore-mailbox: sku 'E1M-AEN301' is not supported
                     (supported: ['E1M-AEN801'])          rc=1

    Before the per-template table, tan rendered the AEN801 tree anyway --
    `exitCode 0`, a written project claiming `sku: E1M-AEN301` -- because
    `_family_bucket` maps every unrecognised AEN prefix onto the default
    family. That is tan generating a project the SDK would not, silently.

    E1M-AEN301 does carry both `m55_hp` and `m55_he` (measured in its
    `topology:`), so the scaffold might even build; the defect is the silent
    divergence from the SDK's own support matrix, not a missing core."""
    proc = run_tan(
        "init", "--template", "multicore-mailbox", "--som", "E1M-AEN301",
        "--format", "json", cwd=tmp_path,
    )
    env = envelope(proc)

    assert proc.returncode == 2, env
    assert issue(env)["code"] == "init.invalid-som"
    assert "E1M-AEN801" in issue(env)["message"]
    assert list(tmp_path.iterdir()) == [], "a refused --som must write nothing"


def test_multicore_mailbox_refuses_another_family_without_blaming_the_install(tmp_path):
    """The failure mode this replaces told the customer the wrong thing.
    `--som E1M-V2N101` fell through to `_family_bucket`'s V2N tree, which
    this template does not vendor, and surfaced as:

        exitCode 5  init.template-unreadable
        "tan's vendored template tree for 'multicore-mailbox' is empty at ..."

    i.e. "your tan installation is broken" for a user whose `--som` was
    simply wrong. Asserting the CODE, not just a non-zero exit: an exit 5
    here would still be a refusal, and still be the wrong story."""
    proc = run_tan(
        "init", "--template", "multicore-mailbox", "--som", "E1M-V2N101",
        "--format", "json", cwd=tmp_path,
    )
    env = envelope(proc)

    assert issue(env)["code"] == "init.invalid-som", (
        "a wrong --som must not be reported as an unreadable vendored tree"
    )
    assert proc.returncode == 2, env
    assert list(tmp_path.iterdir()) == []


def test_a_template_with_no_sku_restriction_still_takes_a_non_default_sku(tmp_path):
    """Anti-over-reach: the table must gate only the templates whose catalog
    entry restricts them. `zephyr-app` vendors both family trees and is
    unaffected -- measured `exitCode 0` on E1M-AEN301 before and after."""
    proc = run_tan(
        "init", "--template", "zephyr-app", "--som", "E1M-AEN301",
        "--name", "unrestricted", "--format", "json", cwd=tmp_path,
    )
    env = envelope(proc)

    assert proc.returncode == 0, env
    assert (tmp_path / "unrestricted" / "board.yaml").is_file()


# ---------------------------------------------------------------------------
# --cores: heterogeneous scaffolding
# ---------------------------------------------------------------------------


def test_cores_splices_a_companion_and_a_default_rpmsg_channel(tmp_path):
    proc = run_tan(
        "init", "--template", "zephyr-app", "--cores", "a55_cluster:yocto",
        "--format", "json", cwd=tmp_path,
    )
    env = envelope(proc)

    assert proc.returncode == 0, env["issues"]
    board = (tmp_path / "board.yaml").read_text(encoding="utf-8")
    assert "  a55_cluster:\n    os: yocto\n    image: alp-image-edge\n" in board
    assert "endpoints: [m55_hp, a55_cluster]" in board


def test_cores_infers_the_os_from_the_core_id_when_omitted(tmp_path):
    proc = run_tan(
        "init", "--template", "zephyr-app", "--cores", "a55_cluster",
        "--format", "json", cwd=tmp_path,
    )
    env = envelope(proc)

    assert proc.returncode == 0, env["issues"]
    board = (tmp_path / "board.yaml").read_text(encoding="utf-8")
    assert "  a55_cluster:\n    os: yocto\n" in board


def test_cores_rejects_a_malformed_entry_without_writing_anything(tmp_path):
    proc = run_tan(
        "init", "--template", "zephyr-app", "--cores", "1bad", "--format", "json", cwd=tmp_path
    )
    env = envelope(proc)

    assert proc.returncode == 2
    assert issue(env)["code"] == "init.invalid-cores"
    assert list(tmp_path.iterdir()) == []


def test_cores_rejects_the_app_core_with_a_conflicting_os(tmp_path):
    proc = run_tan(
        "init", "--template", "zephyr-app", "--cores", "m55_hp:yocto",
        "--format", "json", cwd=tmp_path,
    )
    env = envelope(proc)

    assert proc.returncode == 2
    assert issue(env)["code"] == "init.invalid-cores"
    assert "app core" in issue(env)["message"]
    assert list(tmp_path.iterdir()) == []


def test_cores_rejects_a_core_id_already_declared_by_the_scaffold(tmp_path):
    """`edge-ai-starter` pre-declares its own companion (`a32_cluster`,
    `os: "off"`); colliding with it would emit a duplicate `cores:` mapping
    key."""
    proc = run_tan(
        "init", "--template", "edge-ai-starter", "--cores", "a32_cluster:zephyr",
        "--format", "json", cwd=tmp_path,
    )
    env = envelope(proc)

    assert proc.returncode == 2
    assert issue(env)["code"] == "init.invalid-cores"
    assert "a32_cluster" in issue(env)["message"]
    assert "os: off" in issue(env)["message"]  # de-quoted from the vendored `os: "off"`
    assert list(tmp_path.iterdir()) == []


def test_cores_rejects_a_zephyr_companion_that_is_not_the_app_core(tmp_path):
    """tan-cli#643: `--cores m55_he:zephyr` on the default `zephyr-app`
    template (whose app core is `m55_hp`) used to be silently accepted as a
    COMPANION addition -- an app-less `m55_he` spliced in beside the app the
    caller never asked to keep, plus an unrequested default RPMsg carve-out
    -- at `ok:true`/`issues:[]`. A single-core request became a two-core
    project on the wrong core with no signal at all. It must now refuse:
    `splice_companion_cores` cannot give a companion an `app:`, so a `zephyr`
    companion is always inert, and inert-but-accepted is exactly the shape
    that misread the caller's intent here."""
    proc = run_tan(
        "init", "--template", "zephyr-app", "--cores", "m55_he:zephyr",
        "--format", "json", cwd=tmp_path,
    )
    env = envelope(proc)

    assert proc.returncode == 2, env
    assert issue(env)["code"] == "init.invalid-cores"
    assert "m55_he" in issue(env)["message"]
    assert "m55_hp" in issue(env)["message"]
    assert list(tmp_path.iterdir()) == []


def test_cores_rejects_a_baremetal_companion_that_is_not_the_app_core(tmp_path):
    """tan-cli#643 follow-up: `--cores m55_he:baremetal` reaches the same
    dead end as the `zephyr` case above by a different door.
    `splice_companion_cores` cannot give a companion an `app:` either way, so
    an app-less `os: baremetal` slice would plan to `ok:true`/`issues:[]`
    here and only fail later, at `tan build`, against
    `_enforce_loader_rules` ("os: baremetal requires `app:`") -- against a
    board.yaml the customer never edited by hand. Refuse it at `init` time
    like the `zephyr` case, not two commands downstream."""
    proc = run_tan(
        "init", "--template", "zephyr-app", "--cores", "m55_he:baremetal",
        "--format", "json", cwd=tmp_path,
    )
    env = envelope(proc)

    assert proc.returncode == 2, env
    assert issue(env)["code"] == "init.invalid-cores"
    assert "m55_he" in issue(env)["message"]
    assert "m55_hp" in issue(env)["message"]
    assert "requests baremetal" in issue(env)["message"]
    assert list(tmp_path.iterdir()) == []


def test_cores_rejects_a_yocto_companion_on_a_cortex_m_id(tmp_path):
    """tan-cli#645 round-3: `--cores m55_he:yocto` reaches the identical
    #643 dead end as the `zephyr`/`baremetal` cases above by a FOURTH door.
    `m55_he` is a Cortex-M id (per every SoM topology's `m`-prefix
    convention); the planner's `_enforce_os_matches_core_class` refuses a
    Cortex-M core running Yocto exactly as hard as it refuses a Cortex-A
    core running Zephyr. Measured before this refusal existed: `tan init`
    planned this to `ok:true`/`issues:[]`, and `tan validate` on the result
    then raised `validate.schema-violation` ("its runtime is determined by
    the core class ... got os: 'yocto'"). Refuse at `init` time instead."""
    proc = run_tan(
        "init", "--template", "zephyr-app", "--cores", "m55_he:yocto",
        "--format", "json", cwd=tmp_path,
    )
    env = envelope(proc)

    assert proc.returncode == 2, env
    assert issue(env)["code"] == "init.invalid-cores"
    assert "m55_he" in issue(env)["message"]
    assert "m55_hp" in issue(env)["message"]
    assert "requests yocto" in issue(env)["message"]
    assert list(tmp_path.iterdir()) == []


def test_cores_still_accepts_a_yocto_companion_on_a_cortex_a_id(tmp_path):
    """The new Cortex-M/yocto refusal must not catch the Cortex-A case that
    was always the genuinely-honored one: `a55_cluster` (an `a`-prefixed id)
    requesting `yocto` still splices in cleanly."""
    proc = run_tan(
        "init", "--template", "zephyr-app", "--cores", "a55_cluster:yocto",
        "--format", "json", cwd=tmp_path,
    )
    env = envelope(proc)

    assert proc.returncode == 0, env["issues"]
    board = (tmp_path / "board.yaml").read_text(encoding="utf-8")
    assert "  a55_cluster:\n    os: yocto\n    image: alp-image-edge\n" in board


def test_cores_off_companion_round_trips_as_a_yaml_string(tmp_path):
    """tan-cli#645 round-3: an unquoted `os: off` is a YAML 1.1 boolean
    keyword (`yaml.safe_load("os: off")` -> `{"os": False}`), so a companion
    spliced in at `:off` used to write a bool where the schema requires the
    string `"off"` -- `ok:true` at `init`, then a
    `validate.schema-violation` ("False is not of type 'string'") on the
    very next command. `splice_companion_cores` now quotes it like every
    vendored scaffold already does."""
    proc = run_tan(
        "init", "--template", "zephyr-app", "--cores", "m55_he:off",
        "--format", "json", cwd=tmp_path,
    )
    env = envelope(proc)

    assert proc.returncode == 0, env["issues"]
    board_text = (tmp_path / "board.yaml").read_text(encoding="utf-8")
    assert '  m55_he:\n    os: "off"\n' in board_text
    parsed = yaml.safe_load(board_text)
    assert parsed["cores"]["m55_he"]["os"] == "off"


def test_cores_still_accepts_the_app_core_itself_at_zephyr(tmp_path):
    """The companion-zephyr refusal must not catch the app core naming
    itself: `--cores m55_hp:zephyr` on `zephyr-app` (app core `m55_hp`) is
    the documented no-op/reinforcement case, not a conflict."""
    proc = run_tan(
        "init", "--template", "zephyr-app", "--cores", "m55_hp:zephyr",
        "--format", "json", cwd=tmp_path,
    )
    env = envelope(proc)

    assert proc.returncode == 0, env["issues"]
    board = (tmp_path / "board.yaml").read_text(encoding="utf-8")
    assert "m55_he" not in board


def test_cores_is_ignored_on_the_from_example_path(tmp_path):
    """`--cores` is ignored for `--from-example`: the example ships its own
    board.yaml.

    The docstring used to say `--som` was ignored too. Measured for
    tan-cli#890 and it is not: `--som` retargets the copied board.yaml for
    every value, including a different silicon family. Only `--cores` is
    dropped here, which is all this test ever asserted."""
    sdk = tmp_path / "sdk"
    (sdk / "scripts").mkdir(parents=True)
    (sdk / "scripts" / "alp_project.py").write_text("", encoding="utf-8")
    example = sdk / "examples" / "peripheral-io" / "hello-world"
    example.mkdir(parents=True)
    (example / "board.yaml").write_text(
        "som:\n  sku: E1M-AEN801\ncores:\n  m55_hp:\n    app: ./src\n", encoding="utf-8"
    )

    proc = run_tan(
        "init", "--from-example", "peripheral-io/hello-world", "--sdk-root", "./sdk",
        "--cores", "a55_cluster:yocto", "--format", "json", cwd=tmp_path,
    )
    env = envelope(proc)

    assert proc.returncode == 0, env["issues"]
    board = (tmp_path / "board.yaml").read_text(encoding="utf-8")
    assert "a55_cluster" not in board


# ---------------------------------------------------------------------------
# --board-yaml
# ---------------------------------------------------------------------------


def test_board_yaml_renders_the_callers_file_verbatim(tmp_path):
    custom = tmp_path / "custom.yaml"
    custom.write_text("som:\n  sku: E1M-V2N101\ncores:\n  m33_sm:\n    app: ./src\n", encoding="utf-8")

    proc = run_tan(
        "init", "--template", "zephyr-app", "--board-yaml", "custom.yaml",
        "--format", "json", cwd=tmp_path,
    )
    env = envelope(proc)

    assert proc.returncode == 0, env["issues"]
    assert (tmp_path / "board.yaml").read_text(encoding="utf-8") == custom.read_text(encoding="utf-8")


def test_board_yaml_unreadable_path_is_a_coded_issue_not_a_traceback(tmp_path):
    proc = run_tan(
        "init", "--template", "zephyr-app", "--board-yaml", "does-not-exist.yaml",
        "--format", "json", cwd=tmp_path,
    )
    env = envelope(proc)

    assert proc.returncode == 2
    assert issue(env)["code"] == "init.board-yaml-unreadable"
    assert list(tmp_path.iterdir()) == []


# ---------------------------------------------------------------------------
# --all / --target / --verbose / --quiet / --no-color -- accepted, genuinely
# ignored, matching the oracle (not a "silently tolerated" gap).
# ---------------------------------------------------------------------------


def test_global_flags_that_the_oracle_never_reads_for_init_are_accepted_and_are_true_no_ops(
    tmp_path,
):
    """Measured against the real `tan`: none of these five are read by
    `crates/tan-cli/src/commands/init/` (unlike `doctor`/`clean`, `init` calls
    neither `style::render_report` nor `Theme::from_args`). Pinned here as a
    byte-identical envelope, not merely "still exits 0" -- an envelope that
    silently differs would mean one of them DID change something, which is
    the exact "accepted but takes effect anyway" surprise this test guards
    against in the other direction."""
    baseline_dir, flagged_dir = tmp_path / "baseline", tmp_path / "flagged"
    baseline_dir.mkdir()
    flagged_dir.mkdir()

    baseline = run_tan(
        "init", "--template", "minimal-app", "--preview", "--format", "json", cwd=baseline_dir
    )
    flagged = run_tan(
        "init", "--template", "minimal-app", "--preview", "--format", "json",
        "--all", "--target", "zephyr-conf", "--verbose", "--quiet", "--no-color",
        cwd=flagged_dir,
    )

    assert envelope(baseline) == envelope(flagged)
    assert baseline.returncode == flagged.returncode == 0


def test_unreadable_template_data_is_a_coded_internal_failure(tmp_path, monkeypatch):
    """tan's own vendored tree gone missing -- the shape a frozen binary built
    without the template `--add-data` has. A broken tan installation, so exit 5,
    but still an envelope: a traceback here renders as nothing in the extension.

    Driven in-process because the point is a missing data directory, which no
    CLI flag can produce.
    """
    from tan.commands import init_cmd
    from tan.core import scaffold

    monkeypatch.setattr(scaffold, "VENDORED_ROOT", tmp_path / "does-not-exist")
    # `typer.Exit`, not `SystemExit`: Typer's own exit signal descends from its
    # VENDORED click (`typer._click.exceptions`), and the runner turns it into a
    # process exit later.
    with pytest.raises(typer.Exit) as exit_info:
        init_cmd.init(
            template="zephyr-app",
            from_example=None,
            topology=None,
            name=None,
            destination=str(tmp_path),
            som=None,
            preview=True,
            force=False,
            project=None,
            sdk_root=None,
            output_format="json",
        )

    assert exit_info.value.exit_code == 5


def test_non_utf8_vendored_template_byte_is_a_coded_envelope_not_a_traceback(
    tmp_path, monkeypatch, capsys
):
    """tan-cli#415: `scaffold._read_verbatim`'s two callers each caught
    `OSError` only, so one corrupt byte in tan's OWN vendored tree escaped as
    a raw `UnicodeDecodeError` (a `ValueError`, not an `OSError`) -- caught
    only by `init()`'s generic backstop before this fix (`init.internal-
    failure`), not the SAME `init.template-unreadable` a missing tree already
    gets. A real (copied) vendored tree with exactly one corrupted byte, not a
    synthetic replacement, so this exercises the actual read path
    `test_unreadable_template_data_is_a_coded_internal_failure` above cannot
    (a missing directory never reaches `_read_verbatim` at all).
    """
    import shutil

    from tan.commands import init_cmd
    from tan.core import scaffold

    corrupt_root = tmp_path / "vendored-corrupt"
    shutil.copytree(scaffold.VENDORED_ROOT / "minimal", corrupt_root / "minimal")
    board_yaml = corrupt_root / "minimal" / "E1M-AEN801" / "board.yaml"
    board_yaml.write_bytes(board_yaml.read_bytes() + b"\n# \xff\n")
    monkeypatch.setattr(scaffold, "VENDORED_ROOT", corrupt_root)

    with pytest.raises(typer.Exit) as exit_info:
        init_cmd.init(
            template="zephyr-app",
            from_example=None,
            topology=None,
            name=None,
            destination=str(tmp_path / "out"),
            som=None,
            preview=True,
            force=False,
            project=None,
            sdk_root=None,
            output_format="json",
        )
    assert exit_info.value.exit_code == 5

    stdout = capsys.readouterr().out
    assert stdout.count("\n") == 1, stdout  # one JSON document, not a traceback
    doc = json.loads(stdout)
    assert doc["ok"] is False
    assert doc["exitCode"] == 5
    assert doc["issues"][0]["code"] == "init.template-unreadable"


def test_from_example_without_an_sdk_checkout_is_a_coded_issue(tmp_path):
    """The ONE init path that genuinely needs an alp-sdk checkout: it copies a
    directory out of one. Templates never do (I-32)."""
    proc = run_tan(
        "init", "--from-example", "peripheral-io/hello-world", "--format", "json", cwd=tmp_path
    )
    env = envelope(proc)

    assert proc.returncode == 2
    assert issue(env)["code"] == "init.sdk-root-unresolved"


def test_from_example_copies_the_tree_and_retargets_the_som(tmp_path):
    sdk = tmp_path / "sdk"
    (sdk / "scripts").mkdir(parents=True)
    (sdk / "scripts" / "alp_project.py").write_text("", encoding="utf-8")
    example = sdk / "examples" / "peripheral-io" / "hello-world"
    (example / "src").mkdir(parents=True)
    (example / "board.yaml").write_text(
        "som:\n  sku: E1M-AEN801           # aligned comment\ncores:\n  m55_hp:\n    os: zephyr\n",
        encoding="utf-8",
    )
    (example / "src" / "main.c").write_text("int main(void) { return 0; }\n", encoding="utf-8")

    proc = run_tan(
        "init",
        "--from-example",
        "peripheral-io/hello-world",
        "--sdk-root",
        "./sdk",
        "--name",
        "copy",
        "--som",
        "E1M-V2N101",
        "--format",
        "json",
        cwd=tmp_path,
    )
    env = envelope(proc)

    assert proc.returncode == 0
    assert env["data"]["templateId"] == "example:peripheral-io/hello-world"
    assert env["data"]["written"] == ["board.yaml", "src/main.c"]
    board = (tmp_path / "copy" / "board.yaml").read_text(encoding="utf-8")
    # The value token moves; a stale trailing comment (it named the ORIGINAL
    # SoM's vendor) is dropped rather than carried onto the new SKU.
    assert "  sku: E1M-V2N101\n" in board
    assert "aligned comment" not in board
    # `--sdk-root ./sdk` is recorded as an ABSOLUTE path, anchored against the
    # cwd `init` actually ran from (tan-cli#263) -- not the literal `./sdk` the
    # caller typed. A relative pointer read back from a LATER invocation, whose
    # cwd is the project directory itself rather than this one, silently misses
    # (that is the maintainer's exact repro: `--sdk-root .\alp-sdk` from the
    # parent, then `tan sdk current` from inside the new project).
    expected_sdk = (tmp_path / "sdk").as_posix()
    assert env["data"]["sdkPinned"] == expected_sdk
    pointer = json.loads((tmp_path / "copy" / ".alp" / "sdk-path").read_text(encoding="utf-8"))
    assert pointer["sdkPath"] == expected_sdk


def test_a_relative_sdk_root_pin_survives_being_read_back_from_inside_the_project(tmp_path):
    """tan-cli#263, the maintainer's exact repro: `tan init --sdk-root
    .\\alp-sdk --destination .\\blink` run from a parent directory, then a
    LATER command run from inside the new project itself. Before this fix the
    pin stored `.\\alp-sdk` verbatim; read back from `blink/`'s own cwd that
    resolved to `blink/alp-sdk` (nothing there), so `tan sdk current` silently
    fell through to `discovery` -- `ok: true`, `issues: []`, and whichever SDK
    discovery happened to find from that directory, which need not be the one
    `--sdk-root` named.
    """
    sdk = tmp_path / "alp-sdk"
    (sdk / "scripts").mkdir(parents=True)
    (sdk / "scripts" / "alp_project.py").write_text("", encoding="utf-8")
    (sdk / "metadata").mkdir()
    # A developer's real `~/.alp/sdk-default` must not decide this -- it
    # outranks `discovery` but not `projectPin`, so leaving it unisolated
    # would only mask a regression, not cause a false pass; isolated anyway to
    # match `test_sdk_command.py`'s `isolated_home` convention.
    home = tmp_path / "home"
    home.mkdir()
    isolated_env = {"HOME": str(home), "USERPROFILE": str(home)}

    init_proc = run_tan(
        "init",
        "--template",
        "minimal-app",
        "--sdk-root",
        "./alp-sdk",
        "--destination",
        "./blink",
        "--format",
        "json",
        cwd=tmp_path,
        env_extra=isolated_env,
    )
    assert envelope(init_proc)["ok"] is True

    current_proc = run_tan(
        "sdk", "current", "--format", "json", cwd=tmp_path / "blink", env_extra=isolated_env
    )
    env = envelope(current_proc)
    assert env["data"]["sourceTier"] == "projectPin"
    assert env["data"]["sdkPath"] == sdk.as_posix()
    assert env["issues"] == []


@pytest.mark.parametrize(
    "epoch", ["1700000000000", "99999999999", "-99999999999", "253402300799"]
)
def test_an_out_of_range_source_date_epoch_still_pins_the_sdk(epoch, tmp_path):
    """`.alp/sdk-path` carries an `updatedAt`, rendered AFTER the customer's
    project files already landed -- so a timestamp helper that throws breaks
    `tan init` at the last step of a run that otherwise succeeded.

    Milliseconds is the realistic trigger (1700000000000 -> year 55838), and CI
    and reproducible-build environments are what set this variable. The POINTER
    is asserted, not just the exit code, because the two failure modes differ by
    platform and only one is loud: `time.gmtime` raises OSError (Errno 22) on
    Windows, which `_pin_sdk`'s `except OSError` SWALLOWS into a silent
    `sdkPinned: null`, while OverflowError/ValueError elsewhere escapes as a raw
    traceback with EMPTY stdout.

    Mirrors `test_an_out_of_range_source_date_epoch_still_emits_one_envelope` in
    `test_debug_config_command.py`. The helper is now shared
    (`tan.core.timestamp`); this is what keeps `tan init`'s caller honest.
    """
    sdk = tmp_path / "sdk"
    (sdk / "scripts").mkdir(parents=True)
    (sdk / "scripts" / "alp_project.py").write_text("", encoding="utf-8")

    proc = run_tan(
        "init",
        "--template",
        "minimal-app",
        "--sdk-root",
        "./sdk",
        "--name",
        "app",
        "--format",
        "json",
        cwd=tmp_path,
        env_extra={"SOURCE_DATE_EPOCH": epoch},
    )
    env = envelope(proc)

    assert proc.returncode == 0, env["issues"]
    expected_sdk = (tmp_path / "sdk").as_posix()
    assert env["data"]["sdkPinned"] == expected_sdk
    pointer = json.loads((tmp_path / "app" / ".alp" / "sdk-path").read_text(encoding="utf-8"))
    assert pointer["sdkPath"] == expected_sdk
    # Shape, not value: an out-of-range epoch falls back to the wall clock.
    time.strptime(pointer["updatedAt"], "%Y-%m-%dT%H:%M:%SZ")


def test_from_example_traversal_is_refused(tmp_path):
    sdk = tmp_path / "sdk"
    (sdk / "scripts").mkdir(parents=True)
    (sdk / "scripts" / "alp_project.py").write_text("", encoding="utf-8")
    (sdk / "examples").mkdir()
    (tmp_path / "secret.txt").write_text("x", encoding="utf-8")

    for src in ("../../secret.txt", ".", "./peripheral-io"):
        proc = run_tan(
            "init", "--from-example", src, "--sdk-root", "./sdk", "--format", "json", cwd=tmp_path
        )
        env = envelope(proc)
        assert proc.returncode == 2, src
        assert issue(env)["code"] == "init.invalid-example", src


def _sdk_with_catalog(tmp_path, *, example="multicore/mproc-mailbox",
                      som_skus=("E1M-AEN801",), with_catalog=True):
    """A fake SDK carrying one example and (optionally) a catalog record for
    it, in the real `catalog-v1.json` shape: `{"templates": [{...}]}` with
    `example` spelled the way the catalog spells it -- `examples/<src>`."""
    import json
    sdk = tmp_path / "sdk"
    (sdk / "scripts").mkdir(parents=True)
    (sdk / "scripts" / "alp_project.py").write_text("", encoding="utf-8")
    ex = sdk / "examples" / example
    ex.mkdir(parents=True)
    (ex / "board.yaml").write_text(
        "som:\n  sku: E1M-AEN801\ncores:\n  m55_hp:\n    app: ./src\n",
        encoding="utf-8",
    )
    if with_catalog:
        cat = sdk / "metadata" / "templates"
        cat.mkdir(parents=True)
        (cat / "catalog-v1.json").write_text(
            json.dumps({"schemaVersion": 1, "templates": [{
                "id": "multicore-mailbox",
                "example": f"examples/{example}",
                "supported": {"som_skus": list(som_skus)},
            }]}),
            encoding="utf-8",
        )
    return sdk


def test_from_example_warns_when_som_is_outside_the_catalog_support_set(tmp_path):
    """tan-cli#890. `--from-example` never consulted the catalog's
    `supported.som_skus`, so it scaffolded SoMs the SDK refuses outright:

        $ alp_project.py --emit scaffold --template multicore-mailbox --sku E1M-AEN301
        alp_project: multicore-mailbox: sku 'E1M-AEN301' is not supported
                     (supported: ['E1M-AEN801'])          rc=1
        $ tan init --from-example multicore/mproc-mailbox --som E1M-AEN301
        exitCode 0 | ok True | issues 0

    A WARNING rather than a refusal, matching this path's own precedent
    (`test_from_example_with_no_board_yaml_warns_and_still_scaffolds`): a hard
    refusal here "made `tan init --from-example` unusable for nearly the whole
    AEN family, which is worse than the original defect". The issue says the
    same in its own words -- the wider set may genuinely work; what is wrong is
    that nothing checked."""
    _sdk_with_catalog(tmp_path)

    proc = run_tan(
        "init", "--from-example", "multicore/mproc-mailbox", "--sdk-root", "./sdk",
        "--som", "E1M-AEN301", "--format", "json", cwd=tmp_path,
    )
    env = envelope(proc)

    assert proc.returncode == 0, env
    codes = [i["code"] for i in env["issues"]]
    assert "init.example-som-unsupported" in codes, env["issues"]
    warn = next(i for i in env["issues"] if i["code"] == "init.example-som-unsupported")
    assert warn["severity"] == "warning"
    assert "E1M-AEN801" in warn["message"], "the supported set must be named"
    assert "E1M-AEN301" in warn["message"], "the rejected --som must be named"
    # Files are still written -- that is the whole point of warning over refusing.
    assert (tmp_path / "board.yaml").is_file()


def test_from_example_is_silent_when_the_som_is_in_the_support_set(tmp_path):
    """Anti-false-alarm: the supported SKU must not warn."""
    _sdk_with_catalog(tmp_path)

    proc = run_tan(
        "init", "--from-example", "multicore/mproc-mailbox", "--sdk-root", "./sdk",
        "--som", "E1M-AEN801", "--format", "json", cwd=tmp_path,
    )
    env = envelope(proc)

    assert proc.returncode == 0, env
    assert "init.example-som-unsupported" not in [i["code"] for i in env["issues"]]


def test_from_example_does_not_warn_for_an_example_with_no_catalog_record(tmp_path):
    """Anti-over-reach, and this is the common case, not the edge: the SDK
    ships far more examples than the catalog declares (9 records against
    66 under `examples/aen/` alone). An example the catalog says nothing about
    has no declared support set, so there is nothing to check -- inventing a
    restriction there would be the same defect pointed the other way."""
    _sdk_with_catalog(tmp_path, example="peripheral-io/hello-world",
                      with_catalog=False)

    proc = run_tan(
        "init", "--from-example", "peripheral-io/hello-world", "--sdk-root", "./sdk",
        "--som", "E1M-V2N101", "--format", "json", cwd=tmp_path,
    )
    env = envelope(proc)

    assert proc.returncode == 0, env
    assert "init.example-som-unsupported" not in [i["code"] for i in env["issues"]]


def test_from_example_survives_an_sdk_with_no_catalog_at_all(tmp_path):
    """An older SDK checkout has no `metadata/templates/catalog-v1.json`.
    `--from-example` worked there before this gate and must keep working:
    a missing catalog is not a reason to refuse, and must not crash."""
    _sdk_with_catalog(tmp_path, with_catalog=False)

    proc = run_tan(
        "init", "--from-example", "multicore/mproc-mailbox", "--sdk-root", "./sdk",
        "--som", "E1M-AEN301", "--format", "json", cwd=tmp_path,
    )
    env = envelope(proc)

    assert proc.returncode == 0, env
    assert "init.example-som-unsupported" not in [i["code"] for i in env["issues"]]
    assert (tmp_path / "board.yaml").is_file()


def test_from_example_without_som_never_warns(tmp_path):
    """No `--som` means no retarget: the example keeps its own SKU, which is
    by definition one the example was written for."""
    _sdk_with_catalog(tmp_path)

    proc = run_tan(
        "init", "--from-example", "multicore/mproc-mailbox", "--sdk-root", "./sdk",
        "--format", "json", cwd=tmp_path,
    )
    env = envelope(proc)

    assert proc.returncode == 0, env
    assert "init.example-som-unsupported" not in [i["code"] for i in env["issues"]]


def test_from_example_with_no_board_yaml_warns_and_still_scaffolds(tmp_path):
    """tan-cli#3 / scaffold-cx review: an example with no board.yaml anywhere in
    its tree (a real SDK shape -- 57 of 66 examples/aen/* are raw west/twister
    examples, no board.yaml) used to scaffold fine and only fail two commands
    later at `tan build` ("no board.yaml found"). Refusing the copy outright
    made `tan init --from-example` unusable for nearly the whole AEN family,
    which is worse than the original defect -- so this WARNS at scaffold time
    (a `severity: "warning"` issue) and still writes every file, exit 0."""
    sdk = tmp_path / "sdk"
    (sdk / "scripts").mkdir(parents=True)
    (sdk / "scripts" / "alp_project.py").write_text("", encoding="utf-8")
    example = sdk / "examples" / "aen" / "no-board-yaml-example"
    example.mkdir(parents=True)
    (example / "CMakeLists.txt").write_text("# no board.yaml here\n", encoding="utf-8")
    (example / "prj.conf").write_text("CONFIG_ASSERT=y\n", encoding="utf-8")

    proc = run_tan(
        "init", "--from-example", "aen/no-board-yaml-example", "--sdk-root", "./sdk",
        "--format", "json", cwd=tmp_path,
    )
    env = envelope(proc)

    assert proc.returncode == 0
    assert issue(env)["code"] == "init.example-missing-board-yaml"
    assert issue(env)["severity"] == "warning"
    assert sorted(env["data"]["written"]) == ["CMakeLists.txt", "prj.conf"]
    assert (tmp_path / "CMakeLists.txt").exists()
    assert (tmp_path / "prj.conf").exists()


def test_from_example_with_no_board_yaml_and_board_yaml_flag_adds_it(tmp_path):
    """`--board-yaml` is the guard's real escape hatch on the `--from-example`
    path (`allow_add=True`): an example with no board.yaml to override gets the
    caller's file ADDED as its board.yaml, rather than the dead-end
    `init.board-yaml-unsupported` this used to raise even though the warning
    told the operator to pass exactly this flag."""
    sdk = tmp_path / "sdk"
    (sdk / "scripts").mkdir(parents=True)
    (sdk / "scripts" / "alp_project.py").write_text("", encoding="utf-8")
    example = sdk / "examples" / "aen" / "no-board-yaml-example"
    example.mkdir(parents=True)
    (example / "CMakeLists.txt").write_text("# no board.yaml here\n", encoding="utf-8")
    custom = tmp_path / "custom.yaml"
    custom.write_text("som:\n  sku: E1M-AEN801\ncores:\n  m55_hp:\n    app: ./src\n", encoding="utf-8")

    proc = run_tan(
        "init", "--from-example", "aen/no-board-yaml-example", "--sdk-root", "./sdk",
        "--board-yaml", "custom.yaml", "--format", "json", cwd=tmp_path,
    )
    env = envelope(proc)

    assert proc.returncode == 0
    assert env["issues"] == []
    assert sorted(env["data"]["written"]) == ["CMakeLists.txt", "board.yaml"]
    assert (tmp_path / "board.yaml").read_text(encoding="utf-8") == custom.read_text(
        encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# tan-cli#325: `_pin_sdk`'s `.alp/sdk-path` write shares scaffold.py's
# confinement guard -- a symlinked `.alp` (or a symlinked parent of it) must
# not carry the pin write outside the project while `init` still reports
# success. The write's target is the fixed, shallow `<project_root>/.alp/
# sdk-path`, so "a symlinked parent of `.alp`" has exactly one instance that
# is not simply the project root itself (covered separately below, and which
# must keep WORKING): `.alp` being a directory link IS the parent of the
# `sdk-path` leaf. The scaffold.py suite's third shape (an existing LEAF
# itself a symlink) needs a real file symlink, which this box lacks the
# privilege to create -- same constraint noted there.
# ---------------------------------------------------------------------------

WINDOWS = os.name == "nt"


def _make_dir_link(link: Path, target: Path) -> bool:
    """A directory link `link` -> `target`: a Windows JUNCTION (no elevated
    privilege needed, unlike a real symlink) or a POSIX symlink. `False` when
    the host refuses to make one at all (policy-dependent) -- the same
    tradeoff `test_scaffold.py`/`test_generate_command.py` already make for
    the same reason. This box lacks the privilege for a real Windows symlink,
    so the repro below uses a junction, not a symlink -- distinct mechanisms
    (a junction is a filesystem-level directory alias with no reparse-tag
    ACL/privilege check; a symlink can also target a file and needs
    SeCreateSymbolicLinkPrivilege), but both make `.resolve()` walk through a
    directory alias, which is the only property this guard depends on."""
    target.mkdir(parents=True, exist_ok=True)
    if WINDOWS:
        made = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
        )
        return made.returncode == 0
    link.symlink_to(target, target_is_directory=True)
    return True


def _sdk_checkout(root: Path) -> Path:
    (root / "scripts").mkdir(parents=True)
    (root / "scripts" / "alp_project.py").write_text("", encoding="utf-8")
    return root


def test_pin_sdk_refuses_a_write_through_a_symlinked_alp_dir(tmp_path):
    """The tan-cli#325 repro applied to `_pin_sdk`: `<project>/.alp` is a
    pre-existing directory link to somewhere outside the project. Before the
    fix this followed the link, wrote `<outside>/sdk-path`, and still
    reported `ok: true` with `sdkPinned` set. Now it must refuse -- nothing
    from `tan init` may land outside the project, and the command must report
    an error, not a silent `sdkPinned: null`."""
    project = tmp_path / "project"
    outside = tmp_path / "outside"
    project.mkdir()
    sdk = _sdk_checkout(tmp_path / "sdk")
    if not _make_dir_link(project / ".alp", outside):
        pytest.skip("cannot create a directory link on this host")

    proc = run_tan(
        "init", "--template", "minimal-app", "--destination", str(project),
        "--sdk-root", str(sdk), "--format", "json", cwd=tmp_path,
    )
    env = envelope(proc)

    assert proc.returncode == 3  # ExitCode.WRITE_FAILURE
    assert env["ok"] is False
    assert issue(env)["code"] == "init.write-failed"
    # The filesystem outcome, not just the message: nothing landed outside
    # the project, through the link or otherwise.
    assert not any(outside.rglob("*"))
    assert not (project / ".alp" / "sdk-path").exists()


def test_pin_sdk_still_works_when_the_project_root_itself_is_a_symlink(tmp_path):
    """The fix must not be over-tightened: a project reached THROUGH a
    symlinked root is still a legitimate write, because `.alp/sdk-path`'s
    resolved target still lands inside the resolved root."""
    real_project = tmp_path / "real_project"
    real_project.mkdir()
    link = tmp_path / "project_link"
    sdk = _sdk_checkout(tmp_path / "sdk")
    if not _make_dir_link(link, real_project):
        pytest.skip("cannot create a directory link on this host")

    proc = run_tan(
        "init", "--template", "minimal-app", "--destination", str(link),
        "--sdk-root", str(sdk), "--format", "json", cwd=tmp_path,
    )
    env = envelope(proc)

    assert proc.returncode == 0, env["issues"]
    assert env["data"]["sdkPinned"] == sdk.as_posix()
    pointer = json.loads((real_project / ".alp" / "sdk-path").read_text(encoding="utf-8"))
    assert pointer["sdkPath"] == sdk.as_posix()


def test_would_overwrite_names_the_files_and_offers_preview(tmp_path):
    """"One or more files" is the one fact the user already knows. The command
    holds the FileChange list; naming the paths and offering --preview is what
    turns a dead end into a next step."""
    from tan.core.scaffold import FileChange

    changes = [
        FileChange(relative_path="board.yaml", kind="update"),
        FileChange(relative_path="src/main.c", kind="new"),
    ]
    message = overwrite_refusal_message(changes)

    assert "board.yaml" in message
    assert "src/main.c" not in message      # only the "update" kind collides
    assert "--preview" in message
    assert "--force" in message


# ---------------------------------------------------------------------------
# tan-cli#494 defect 10 -- a removed cwd
# ---------------------------------------------------------------------------


@pytest.mark.skipif(WINDOWS, reason="Windows refuses to remove a process's own cwd")
def test_a_preview_still_answers_from_a_cwd_that_has_been_removed(tmp_path):
    """`Path.cwd()` was called unguarded, so a cwd deleted out from under the
    process -- the extension re-spawning `tan` into a directory the user just
    renamed, or a CI step clearing its scratch dir -- turned the whole command
    into `init.internal-failure` / exit 5 with an empty envelope.

    Measured against the frozen oracle on the same argv in the same removed
    cwd: `target/debug/tan` exits 0 and emits the full 8-file preview, and so
    does this port's own `tan clean`. The fallback is
    `current_dir().unwrap_or_else(|_| PathBuf::from("."))`, which
    `clean_cmd._cli_workspace_root` and `presets_cmd.resolve_project_paths`
    already spell out; `init` -- the command that also PINS its answer into
    `.alp/sdk-path` -- was the one that skipped it.

    The child removes its OWN cwd (`subprocess` needs the directory to exist at
    spawn time), then runs the real `python -m tan`, so this exercises the argv
    parsing and stdout framing too, not just the helper.
    """
    gone = tmp_path / "gone"
    gone.mkdir()
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(
            [str(PACKAGE_ROOT), *([p] if (p := os.environ.get("PYTHONPATH")) else [])]
        ),
    }
    proc = subprocess.run(
        [
            sys.executable, "-c",
            "import os, runpy, sys\n"
            "os.rmdir(os.getcwd())\n"
            "sys.argv = ['tan', 'init', '--preview', '--template', 'minimal-app',"
            " '--format', 'json']\n"
            "runpy.run_module('tan', run_name='__main__')\n",
        ],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=str(gone), env=env,
    )
    body = envelope(proc)

    assert proc.returncode == 0, body["issues"]
    assert body["ok"] is True
    assert body["issues"] == []
    assert body["data"]["preview"] is True
    assert [c["relativePath"] for c in body["data"]["fileChanges"]], body["data"]
    assert not gone.exists()


# ---------------------------------------------------------------------------
# tan-cli#579 -- a SoM family with no vendored scaffold tree
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "template_id", ["zephyr-app", "sensor-starter", "edge-ai-starter", "board-diagnostics"]
)
def test_an_nxp_som_is_refused_instead_of_getting_the_alif_tree(template_id, tmp_path):
    """**tan-cli#579.** Measured on `dev` before this fix, for every one of
    these four templates::

        tan init --som E1M-NX9101 --template sensor-starter --format json
        -> exit 0, ok true, issues []
        -> board.yaml is the Alif tree verbatim (preset: e1m-evk,
           chips: [tmp112], "Reads the TMP112 temperature sensor on BRD_I2C"),
           with only `sku:`/`cores:` retargeted

    tan-cli#583 had already fixed the CORE id (it emits `m33` for NXP), which
    made the artefact MORE plausible, not less: the remaining files -- README,
    `src/main.c`, `prj.conf`, `CMakeLists.txt` -- stayed byte-identical to the
    Alif render, and `CMakeLists.txt` still passes `--core m55_hp` to the SDK
    loader, contradicting the `m33` in the board.yaml beside it.
    """
    proc = run_tan(
        "init", "--som", "E1M-NX9101", "--template", template_id, "--format", "json",
        cwd=tmp_path,
    )
    body = envelope(proc)

    assert proc.returncode == 2, body
    assert body["ok"] is False
    assert [i["code"] for i in body["issues"]] == ["init.som-unsupported"]
    assert "E1M-NX9101" in body["issues"][0]["message"]
    # The refusal must write NOTHING -- not a half-Alif project, not `.alp/`.
    assert list(tmp_path.iterdir()) == [], "a refused init must not touch disk"


def test_the_nxp_refusal_names_a_template_that_does_work(tmp_path):
    """The escape hatch is real, not just named: `minimal-app` is tan's own
    vendor-neutral template and scaffolds this SoM correctly."""
    refused = envelope(
        run_tan("init", "--som", "E1M-NX9101", "--template", "sensor-starter",
                "--format", "json", cwd=tmp_path)
    )
    assert "minimal-app" in refused["issues"][0]["message"]

    proc = run_tan(
        "init", "--som", "E1M-NX9101", "--template", "minimal-app", "--format", "json",
        cwd=tmp_path,
    )
    body = envelope(proc)

    assert proc.returncode == 0, body["issues"]
    board = (tmp_path / "board.yaml").read_text(encoding="utf-8")
    assert "sku: E1M-NX9101" in board
    assert "  m33:\n" in board


def test_the_refusal_is_a_coded_issue_in_text_mode_too(tmp_path):
    """Text mode still gets the message on stderr and the same exit code -- no
    traceback, no empty stdout."""
    proc = run_tan(
        "init", "--som", "E1M-NX9101", "--template", "sensor-starter", cwd=tmp_path
    )

    assert proc.returncode == 2
    assert "Traceback" not in proc.stderr, proc.stderr
    assert "E1M-NX9101" in proc.stderr
    assert list(tmp_path.iterdir()) == []


def test_an_alif_family_sku_that_is_not_the_tree_sku_still_scaffolds(tmp_path):
    """Scope guard on the refusal: it is per-FAMILY, not per-SKU. E1M-AEN301
    is a different Ensemble part from the tree's own E1M-AEN801, and the Alif
    tree is genuinely its family's scaffold -- it must keep working."""
    proc = run_tan(
        "init", "--som", "E1M-AEN301", "--template", "sensor-starter", "--format", "json",
        cwd=tmp_path,
    )
    body = envelope(proc)

    assert proc.returncode == 0, body["issues"]
    assert "sku: E1M-AEN301" in (tmp_path / "board.yaml").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# the envelope's `sdk` block (tan-cli#491 defect 5)
# ---------------------------------------------------------------------------


def _init_sdk_argv(sdk: Path, *extra: str) -> tuple[str, ...]:
    return ("init", "--template", "minimal-app", "--sdk-root", str(sdk), "--format", "json", *extra)


def test_every_init_outcome_carries_the_sdk_block(tmp_path):
    """tan-cli#491 defect 5. `init` passed no `sdk=` to `Envelope(...)` at all,
    so the key was ABSENT from all four outcomes -- preview, successful write,
    overwrite-guard refusal, and every `_emit_error` -- while the frozen v0.4.1
    oracle answers the identical argv with `sdk:{root,sourceTier}` (the `init`
    capture in `tests/fixtures/oracle_captures/test_oracle_parity.json`:
    `"sdk": {"root": "../rust-sdk", "sourceTier": "sdkRootFlag"}`). It was the
    only field naming WHICH checkout a run is about to permanently pin --
    `data.sdkPinned` is `null` on three of the four.

    All four asserted as ONE case, deliberately: the defect was that they
    DISAGREED with the oracle uniformly, and a fix to one path alone would
    leave the same hole on the other three.

    `root` is the ABSOLUTE path, not the `./sdk` typed -- `_Sdk.display`'s own
    tan-cli#263 divergence, which this field only reports."""
    sdk = _sdk_checkout(tmp_path / "sdk")
    expected = {"root": sdk.as_posix(), "sourceTier": "sdkRootFlag"}

    preview = envelope(run_tan(*_init_sdk_argv(sdk, "--preview"), cwd=tmp_path))
    assert preview.get("sdk") == expected, preview

    written = envelope(run_tan(*_init_sdk_argv(sdk), cwd=tmp_path))
    assert written.get("sdk") == expected, written
    assert written["data"]["sdkPinned"] == sdk.as_posix()

    (tmp_path / "board.yaml").write_text("# local edit\n", encoding="utf-8")
    refused = envelope(run_tan(*_init_sdk_argv(sdk), cwd=tmp_path))
    assert refused["exitCode"] == 3, refused
    assert [i["code"] for i in refused["issues"]] == ["init.would-overwrite"]
    assert refused.get("sdk") == expected, refused

    failed = envelope(
        run_tan(
            "init", "--template", "nope", "--sdk-root", str(sdk), "--format", "json",
            cwd=tmp_path,
        )
    )
    assert [i["code"] for i in failed["issues"]] == ["init.invalid-template"]
    assert failed.get("sdk") == expected, failed


def test_the_sdk_block_reports_the_discovery_tier_too(tmp_path):
    """The tier is the half `_resolve_sdk_root` was dropping. With no
    `--sdk-root` the wide ladder answers from the child `<ws>/alp-sdk`, and
    `sourceTier` must say `discovery` -- a block that hardcoded `sdkRootFlag`
    would pass the case above and be wrong here.

    HOME is redirected so a real `~/.alp/sdk-default` cannot answer the
    `globalDefault` tier first and change the expected tier."""
    _sdk_checkout(tmp_path / "alp-sdk")
    project = tmp_path / "proj"
    project.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    env = envelope(
        run_tan(
            "init", "--template", "minimal-app", "--preview", "--format", "json",
            cwd=project,
            env_extra={"HOME": str(home), "USERPROFILE": str(home)},
        )
    )
    assert env["sdk"] == {
        "root": (tmp_path / "alp-sdk").as_posix(),
        "sourceTier": "discovery",
    }, env


def test_an_sdk_root_that_is_not_a_checkout_reports_no_sdk_block(tmp_path):
    """The block is gated on the loader marker, matching
    `build_output.resolve_project_context` ("only what core's own loader-marker
    check accepted"). `_pin_sdk` silently declines to pin a non-checkout, so
    reporting `sdk` here would advertise a checkout that is about to be pinned
    and is not -- and `data.sdkPinned` proves it was not.

    tan-cli#642: this is the exact repro -- an unresolvable `--sdk-root` --
    and it must no longer be silent about it: `ok:true`/`exitCode:0` (the
    project still scaffolds), but a coded warning names the path that failed
    to resolve, and the project is still written with no `.alp/sdk-path`."""
    typo = tmp_path / "alp-sdk-typo"
    typo.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    env = envelope(
        run_tan(
            *_init_sdk_argv(typo),
            cwd=tmp_path,
            env_extra={"HOME": str(home), "USERPROFILE": str(home)},
        )
    )
    assert env["exitCode"] == 0, env
    assert "sdk" not in env, env
    assert env["data"]["sdkPinned"] is None
    assert not (tmp_path / ".alp" / "sdk-path").exists()
    assert [i["code"] for i in env["issues"]] == ["init.sdk-root-invalid"]
    warning = env["issues"][0]
    assert warning["severity"] == "warning"
    assert str(typo) in warning["message"]
    assert "sdkPinned" not in warning["message"]  # sanity: the path, not the field name


def test_sdk_root_unresolved_is_silent_when_no_sdk_root_was_passed_at_all(tmp_path):
    """`init.sdk-root-invalid` is scoped to an EXPLICIT `--sdk-root` that
    failed to resolve -- not to the ordinary case of no `--sdk-root` at all,
    which every other ladder tier already handles on its own terms (or not,
    by design: `minimal-app` scaffolds with no SDK reachable at all)."""
    home = tmp_path / "home"
    home.mkdir()
    env = envelope(
        run_tan(
            "init", "--template", "minimal-app", "--format", "json",
            cwd=tmp_path,
            env_extra={"HOME": str(home), "USERPROFILE": str(home)},
        )
    )
    assert env["exitCode"] == 0, env
    assert env["data"]["sdkPinned"] is None
    assert env["issues"] == []


def test_sdk_root_invalid_fires_for_an_sdk_root_a_bootstrap_relocation_broke(tmp_path):
    """tan-cli#642's more realistic route: nobody types a nonsense path, but a
    previously-valid `--sdk-root` can stop resolving between calls -- e.g.
    once `tan bootstrap` has relocated it. Simulated here by pointing
    `--sdk-root` at a directory that was NEVER a checkout to begin with (the
    observable shape -- 'the marker file is not there right now' -- is
    identical either way; `_is_sdk_checkout` cannot distinguish 'never was'
    from 'moved out from under this run')."""
    relocated_away = tmp_path / "sdk-relocated-elsewhere"
    relocated_away.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    env = envelope(
        run_tan(
            *_init_sdk_argv(relocated_away),
            cwd=tmp_path,
            env_extra={"HOME": str(home), "USERPROFILE": str(home)},
        )
    )
    assert env["exitCode"] == 0, env
    assert env["data"]["sdkPinned"] is None
    codes = [i["code"] for i in env["issues"]]
    assert "init.sdk-root-invalid" in codes, env["issues"]


def test_an_error_before_the_sdk_resolves_leaves_the_key_absent(tmp_path):
    """`_emit_error`'s `sdk` defaults to `None` because both handlers can be
    reached before `_resolve_sdk_root` has run. Proven with `--project` naming
    a path that is a FILE: `os.path.abspath` succeeds, the scaffold write
    refuses, and the envelope must still be an envelope."""
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory\n", encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir()
    env = envelope(
        run_tan(
            "init", "--template", "nope", "--project", str(blocker), "--format", "json",
            cwd=tmp_path,
            env_extra={"HOME": str(home), "USERPROFILE": str(home)},
        )
    )
    assert "sdk" not in env, env
    assert [i["code"] for i in env["issues"]] == ["init.invalid-template"]


def test_an_error_after_an_unresolved_sdk_root_still_omits_the_sdk_block(tmp_path):
    """The `_emit_error` sibling of
    `test_an_sdk_root_that_is_not_a_checkout_reports_no_sdk_block`: here
    `resolved_sdk` IS bound (unlike the case above, where the key is absent
    because `_resolve_sdk_root` never ran at all) but is not a real checkout,
    and the failure is an ordinary validation error (`init.invalid-template`)
    raised AFTER `_resolve_sdk_root` -- exactly the `_emit_error` call site at
    tan-cli#922, `_emit_error(json_mode, err, resolved_sdk)`.  `_sdk_reportable`
    must still gate that call the same way it gates `_emit_outcome`'s: nothing
    was pinned, so the envelope must not advertise a checkout that a
    `--sdk-root` typo never resolved to."""
    typo = tmp_path / "alp-sdk-typo"
    typo.mkdir()
    proc = run_tan(
        "init", "--template", "bogus-xyz", "--som", "E1M-AEN801",
        "--sdk-root", str(typo), "--format", "json", cwd=tmp_path,
    )
    env = envelope(proc)

    assert env["exitCode"] != 0, env
    assert "sdk" not in env, env
    assert [i["code"] for i in env["issues"]] == ["init.invalid-template"]


# ---------------------------------------------------------------------------
# --topology (tan-cli#996, alp-sdk#1652's --cores scaffold selector)
# ---------------------------------------------------------------------------


def _sdk_with_topology_catalog(tmp_path, records):
    """A fake SDK carrying one example per record in `records`
    (`[(example_src, {core_id: os, ...}), ...]`), and a catalog declaring
    each record's `cores:` topology in the real `catalog-v1.json` shape.
    Each example gets a trivial `board.yaml` + `src/main.c` so a resolved
    scaffold has something to write."""
    sdk = tmp_path / "sdk"
    (sdk / "scripts").mkdir(parents=True, exist_ok=True)
    (sdk / "scripts" / "alp_project.py").write_text("", encoding="utf-8")
    templates = []
    for idx, (example, cores) in enumerate(records):
        ex = sdk / "examples" / example
        (ex / "src").mkdir(parents=True)
        (ex / "board.yaml").write_text(
            "som:\n  sku: E1M-AEN801\ncores:\n  m55_hp:\n    app: ./src\n",
            encoding="utf-8",
        )
        (ex / "src" / "main.c").write_text("int main(void) { return 0; }\n", encoding="utf-8")
        templates.append({
            "id": f"tmpl-{idx}",
            "example": f"examples/{example}",
            "cores": [{"id": k, "os": v} for k, v in cores.items()],
        })
    cat = sdk / "metadata" / "templates"
    cat.mkdir(parents=True)
    (cat / "catalog-v1.json").write_text(
        json.dumps({"schemaVersion": 1, "templates": templates}), encoding="utf-8"
    )
    return sdk


def test_topology_resolves_the_one_matching_example(tmp_path):
    _sdk_with_topology_catalog(tmp_path, [
        ("gateway-demo", {"m33_sm": "zephyr"}),
        ("mailbox-demo", {"m55_hp": "zephyr", "m55_he": "zephyr"}),
    ])

    proc = run_tan(
        "init", "--topology", "m33_sm:zephyr", "--sdk-root", "./sdk",
        "--format", "json", cwd=tmp_path,
    )
    env = envelope(proc)

    assert proc.returncode == 0, env
    assert env["data"]["templateId"] == "example:gateway-demo", env
    assert (tmp_path / "board.yaml").is_file()


def test_topology_with_no_match_names_the_known_topologies(tmp_path):
    _sdk_with_topology_catalog(tmp_path, [("gateway-demo", {"m33_sm": "zephyr"})])

    proc = run_tan(
        "init", "--topology", "a55:yocto", "--sdk-root", "./sdk",
        "--format", "json", cwd=tmp_path,
    )
    env = envelope(proc)

    assert proc.returncode == 2, env
    err = issue(env)
    assert err["code"] == "init.topology-not-found"
    assert "m33_sm" in err["message"] and "zephyr" in err["message"], err


def test_topology_ambiguous_names_every_candidate_not_just_the_first(tmp_path):
    """The load-bearing case: two examples doing genuinely different things
    (an RPMsg demo vs a compute-offload demo, the same real-world shape the
    task's own example names) sharing one topology must name BOTH -- never
    silently pick one and hide the other from the customer."""
    _sdk_with_topology_catalog(tmp_path, [
        ("multicore/rpmsg-demo", {"m55_hp": "zephyr", "a32_cluster": "yocto"}),
        ("multicore/offload-demo", {"m55_hp": "zephyr", "a32_cluster": "yocto"}),
        ("gateway-demo", {"m33_sm": "zephyr"}),
    ])

    proc = run_tan(
        "init", "--topology", "m55_hp:zephyr,a32_cluster:yocto", "--sdk-root", "./sdk",
        "--format", "json", cwd=tmp_path,
    )
    env = envelope(proc)

    assert proc.returncode == 2, env
    err = issue(env)
    assert err["code"] == "init.topology-ambiguous"
    assert "tmpl-0" in err["message"], err
    assert "tmpl-1" in err["message"], err
    assert "tmpl-2" not in err["message"], err
    # Nothing was written -- an ambiguous selector must refuse, not guess.
    assert not (tmp_path / "board.yaml").exists()


def test_topology_and_template_together_is_a_coded_conflict(tmp_path):
    proc = run_tan(
        "init", "--topology", "m33_sm:zephyr", "--template", "minimal-app",
        "--format", "json", cwd=tmp_path,
    )
    env = envelope(proc)

    assert proc.returncode == 2, env
    err = issue(env)
    assert err["code"] == "init.scaffold-input-conflict"
    assert "--topology" in err["message"] and "--template" in err["message"], err


def test_topology_and_from_example_together_is_a_coded_conflict(tmp_path):
    proc = run_tan(
        "init", "--topology", "m33_sm:zephyr", "--from-example", "peripheral-io/hello-world",
        "--format", "json", cwd=tmp_path,
    )
    env = envelope(proc)

    assert proc.returncode == 2, env
    assert issue(env)["code"] == "init.scaffold-input-conflict"


def test_topology_without_an_sdk_checkout_is_a_coded_issue(tmp_path):
    """Mirrors `test_from_example_without_an_sdk_checkout_is_a_coded_issue`:
    the topology lives only in the SDK's live catalog, so this path needs a
    checkout exactly the way --from-example does."""
    proc = run_tan(
        "init", "--topology", "m33_sm:zephyr", "--format", "json", cwd=tmp_path,
    )
    env = envelope(proc)

    assert proc.returncode == 2, env
    assert issue(env)["code"] == "init.sdk-root-unresolved"


@pytest.mark.parametrize(
    "raw", ["m33_sm", "m33_sm:zephyr,m33_sm:yocto", "", ":zephyr", "m33_sm:"]
)
def test_topology_malformed_entries_are_a_coded_issue(raw, tmp_path):
    _sdk_with_topology_catalog(tmp_path, [("gateway-demo", {"m33_sm": "zephyr"})])

    proc = run_tan(
        "init", "--topology", raw, "--sdk-root", "./sdk", "--format", "json", cwd=tmp_path,
    )
    env = envelope(proc)

    assert proc.returncode == 2, (raw, env)
    assert issue(env)["code"] == "init.invalid-topology", raw


def test_topology_resolved_example_still_gets_the_som_support_check(tmp_path):
    """A topology-resolved example is an example (`template_id` starts
    "example:"), so it must share `--from-example`'s SoM-support warning --
    this is `is_example_shaped`'s whole point, proven end to end rather than
    only at the unit level."""
    sdk = tmp_path / "sdk"
    (sdk / "scripts").mkdir(parents=True)
    (sdk / "scripts" / "alp_project.py").write_text("", encoding="utf-8")
    ex = sdk / "examples" / "multicore" / "mproc-mailbox"
    ex.mkdir(parents=True)
    (ex / "board.yaml").write_text(
        "som:\n  sku: E1M-AEN801\ncores:\n  m55_hp:\n    app: ./src\n", encoding="utf-8",
    )
    cat = sdk / "metadata" / "templates"
    cat.mkdir(parents=True)
    (cat / "catalog-v1.json").write_text(
        json.dumps({"schemaVersion": 1, "templates": [{
            "id": "multicore-mailbox",
            "example": "examples/multicore/mproc-mailbox",
            "cores": [{"id": "m55_hp", "os": "zephyr"}, {"id": "m55_he", "os": "zephyr"}],
            "supported": {"som_skus": ["E1M-AEN801"]},
        }]}),
        encoding="utf-8",
    )

    proc = run_tan(
        "init", "--topology", "m55_hp:zephyr,m55_he:zephyr", "--sdk-root", "./sdk",
        "--som", "E1M-AEN301", "--format", "json", cwd=tmp_path,
    )
    env = envelope(proc)

    assert proc.returncode == 0, env
    codes = [i["code"] for i in env["issues"]]
    assert "init.example-som-unsupported" in codes, env["issues"]
