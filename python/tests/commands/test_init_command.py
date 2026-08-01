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


def test_cores_is_ignored_on_the_from_example_path(tmp_path):
    """`--som`/`--cores` are ignored for `--from-example`: the example ships
    its own board.yaml."""
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
    # `--sdk-root ./sdk` is recorded VERBATIM, not normalised to `sdk`: the Rust
    # returns an explicit `--sdk-root` as-is, and both binaries write this same
    # pointer file.
    assert env["data"]["sdkPinned"] == "./sdk"
    pointer = json.loads((tmp_path / "copy" / ".alp" / "sdk-path").read_text(encoding="utf-8"))
    assert pointer["sdkPath"] == "./sdk"


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
    assert env["data"]["sdkPinned"] == "./sdk"
    pointer = json.loads((tmp_path / "app" / ".alp" / "sdk-path").read_text(encoding="utf-8"))
    assert pointer["sdkPath"] == "./sdk"
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
