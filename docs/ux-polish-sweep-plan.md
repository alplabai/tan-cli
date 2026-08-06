<!-- SPDX-License-Identifier: Apache-2.0 -->
# `tan` UX Polish Sweep Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the `tan` command surface legible to a first-time user — grouped help, help text written for users rather than maintainers, a next step on every dead-end refusal, and a default answer from `presets` and `examples`.

**Architecture:** Four independent changes, none touching the build/plan/flash logic and none touching the JSON envelope. Every change lands in one of three places the parity harness does not compare: Typer `--help` metadata, stderr refusal text, or a text-mode renderer. Design doc: [`docs/ux-polish-sweep-design.md`](ux-polish-sweep-design.md).

**Tech Stack:** Python ≥ 3.12, Typer 0.27.0, Click 8.4.2, pytest.

## Global Constraints

- **Interpreter:** `py -3.12` on Windows. A bare `python` on the maintainer box is 3.11.3 and `pip install -e .` fails outright on `requires-python = ">=3.12"` (`python/pyproject.toml:23`).
- **Working directory for all commands:** `python/`.
- **Gate:** `py -3.12 -m pytest tests -q` — **zero failures**. Not a count. `ci.yml:82-86` states this explicitly: *"Zero failures is the gate, not a count... Pinning a number would turn every landed port into a red build."*
- **`ALP_SDK_ROOT`** must be unset, or bound to alp-sdk `main`/`dev`/the pinned commit. An arbitrary feature branch turns ~400 `test_planner_emit_parity.py` cases red.
- **All new files carry** `# SPDX-License-Identifier: Apache-2.0` as line 1.
- **Never touch** `crates/`, `contract/`, or `target/debug/tan.exe`. They are the frozen oracle.
- **Never change `--format json` output** in this plan. Every task is text-mode, help-text, or stderr only.
- **Branch per task**, PR into `dev` (not `main`). No `Co-Authored-By: Claude` and no AI attribution in any commit message or PR body.
- **Conventional commit prefixes**, matching repo history: `feat(cli):`, `fix(build):`, `docs(...)`.
- **Keep pure logic out of the command/IO file** — it belongs in `tan/core/` or stays in the existing renderer function.

## Task Ordering

Tasks 1, 2 and 4 are file-disjoint and may run concurrently. Task 3 touches `tan/cli.py` (Task 1) and `tan/commands/build_cmd.py` (Task 2), so **Task 3 lands after Tasks 1 and 2**.

---

### Task 1: Group `tan --help` into six panels

**Files:**
- Modify: `python/tan/cli.py:79-110` (the `app.command(...)` registration table)
- Test: `python/tests/test_cli_skeleton.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: every `CommandInfo` in `app.registered_commands` gains a non-empty `rich_help_panel: str`. Task 3 does not read it, but must not drop it when editing the same file.

**Background the implementer needs:** `tan/cli.py` registers all 32 subcommands with explicit `app.command("name")(func)` calls, deliberately — PyInstaller follows the static import graph only, so an importlib auto-registry produces a frozen binary that cannot find its own commands. Do not restructure the table. `rich_help_panel` is a Typer 0.27 keyword on `app.command()`; it only affects rendering.

`_SUBCOMMAND_NAMES` (`tan/cli.py:128-131`) derives from this same table and is unaffected by adding a keyword argument.

**Panel assignment** (32 commands, each appearing exactly once):

| Panel string | Commands |
|---|---|
| `"Setup"` | `doctor` `bootstrap` `sdk` `completion` |
| `"Start a project"` | `init` `scaffold` `examples` `presets` `explain` |
| `"Configure"` | `validate` `generate` `migrate` `kconfig` `model` `lock` `quality` |
| `"Build & run"` | `build` `run` `clean` `size` `image` `renode` |
| `"Hardware"` | `flash` `monitor` `debug-config` `faultdecode` |
| `"Inspect & author"` | `inspect` `diff` `trace` `support-bundle` `pinmux` `new-som` |

- [ ] **Step 1: Write the failing test**

Append to `python/tests/test_cli_skeleton.py`:

```python
def test_every_registered_command_declares_a_help_panel():
    """Drift guard: a command registered without a panel silently falls into
    Typer's default "Commands" box, which is the flat 32-item list this
    grouping exists to replace. Derived from the registration table rather
    than a hand-kept name list, for the same reason `_SUBCOMMAND_NAMES` is."""
    from tan.cli import app

    unpanelled = sorted(
        info.name for info in app.registered_commands if not info.rich_help_panel
    )
    assert unpanelled == []


def test_help_renders_the_six_panels():
    p = run("--help")
    assert p.returncode == 0
    for panel in (
        "Setup",
        "Start a project",
        "Configure",
        "Build & run",
        "Hardware",
        "Inspect & author",
    ):
        assert panel in p.stdout, f"missing panel: {panel}"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `py -3.12 -m pytest tests/test_cli_skeleton.py -q -k "help_panel or six_panels"`

Expected: both FAIL. The first lists all 32 names in `unpanelled`; the second fails on the first panel string not found in stdout.

- [ ] **Step 3: Add the panel keyword to each registration**

In `python/tan/cli.py`, replace the block at lines 79-110. Every line gains `rich_help_panel=`; the existing `context_settings=` arguments on `lock`/`migrate`/`quality` are preserved.

```python
app.command("bootstrap", rich_help_panel="Setup")(bootstrap)
app.command("build", rich_help_panel="Build & run")(build)
app.command("clean", rich_help_panel="Build & run")(clean)
app.command("completion", rich_help_panel="Setup")(completion)
app.command("debug-config", rich_help_panel="Hardware")(debug_config)
app.command("diff", rich_help_panel="Inspect & author")(diff)
app.command("doctor", rich_help_panel="Setup")(doctor)
app.command("examples", rich_help_panel="Start a project")(examples)
app.command("explain", rich_help_panel="Start a project")(explain)
app.command("faultdecode", rich_help_panel="Hardware")(faultdecode)
app.command("flash", rich_help_panel="Hardware")(flash)
app.command("generate", rich_help_panel="Configure")(generate)
app.command("image", rich_help_panel="Build & run")(image)
app.command("init", rich_help_panel="Start a project")(init)
app.command("inspect", rich_help_panel="Inspect & author")(inspect)
app.command("kconfig", rich_help_panel="Configure")(kconfig)
app.command(
    "lock", context_settings=FORWARD_CONTEXT_SETTINGS, rich_help_panel="Configure"
)(lock)
app.command(
    "migrate", context_settings=FORWARD_CONTEXT_SETTINGS, rich_help_panel="Configure"
)(migrate)
app.command("model", rich_help_panel="Configure")(model)
app.command("monitor", rich_help_panel="Hardware")(monitor)
app.command("new-som", rich_help_panel="Inspect & author")(new_som)
app.command("pinmux", rich_help_panel="Inspect & author")(pinmux)
app.command("presets", rich_help_panel="Start a project")(presets)
app.command(
    "quality", context_settings=FORWARD_CONTEXT_SETTINGS, rich_help_panel="Configure"
)(quality)
app.command("renode", rich_help_panel="Build & run")(renode)
app.command("run", rich_help_panel="Build & run")(run)
app.command("scaffold", rich_help_panel="Start a project")(scaffold)
app.command("sdk", rich_help_panel="Setup")(sdk)
app.command("size", rich_help_panel="Build & run")(size)
app.command("support-bundle", rich_help_panel="Inspect & author")(support_bundle)
app.command("trace", rich_help_panel="Inspect & author")(trace)
app.command("validate", rich_help_panel="Configure")(validate)
```

Update the comment block at `tan/cli.py:73-78` to note that the panel string is part of each registration, so a new command gets grouped by the same one-line edit that registers it.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `py -3.12 -m pytest tests/test_cli_skeleton.py -q`

Expected: PASS.

- [ ] **Step 5: Eyeball the real output**

Run: `py -3.12 -m tan --help`

Expected: six titled boxes instead of one `Commands` box. Confirm no command is missing and none appears twice.

- [ ] **Step 6: Run the full suite**

Run: `py -3.12 -m pytest tests -q`

Expected: zero failures. If a test asserts on the literal `--help` body, update its expectation — those are the port's own tests, not a contract.

- [ ] **Step 7: Commit**

```bash
git add python/tan/cli.py python/tests/test_cli_skeleton.py
git commit -m "feat(cli): group the 32 subcommands into six help panels"
```

---

### Task 2: Remove the port's archaeology from `build --help`

**Files:**
- Modify: `python/tan/commands/build_cmd.py:172` (`_DEFERRED_HELP`)
- Modify: `python/tan/commands/build_cmd.py:1213-1228` (`--native` and `--execute` help strings)
- Test: `python/tests/commands/test_build_command.py`

**Interfaces:**
- Consumes: nothing.
- Produces: nothing other tasks read. `_DEFERRED_HELP` stays a module-level `str` with the same name.

**Background the implementer needs:** `_DEFERRED_HELP` is attached to **twelve** `build` options at `build_cmd.py:1252-1265` — `--plan`, `--target`, `--all`, `--manifest`, `--manifest-from`, `--no-auto-bootstrap`, `--pristine`, `--verbose`, `--quiet`, `--no-color`, `--non-interactive`, `--ci`. One constant, twelve sites; changing the constant changes all twelve.

**Do NOT hide these options** with `hidden=True`. Five of them (`--verbose`, `--quiet`, `--no-color`, `--non-interactive`, `--ci`) are flags every other command implements and a user will reasonably type. Hiding them from `--help` and then refusing them at exit 1 via `deferred_cmd.py`'s `cli.command-deferred` is strictly worse than listing them honestly.

Do not put a version number in the replacement string. The comment at `build_cmd.py:168-171` records why: it previously said "Deferred to v0.6.0" while the release it meant was renumbered to 0.5.0, and *"a help string that names the release a flag will appear in is a promise tan cannot keep true."* The issue link is the durable pointer.

- [ ] **Step 1: Write the failing test**

Append to `python/tests/commands/test_build_command.py`:

```python
def test_build_help_carries_no_port_archaeology():
    """`--help` is user documentation, not a changelog. "v0.4.1" is the Rust
    release this Python port replaced; a user has never heard of it, and the
    rationale it belongs to already lives in the surrounding docstrings."""
    from typer.testing import CliRunner
    import typer

    from tan.commands.build_cmd import build

    app = typer.Typer()
    app.command("build")(build)
    output = CliRunner().invoke(app, ["build", "--help"]).output

    assert "v0.4.1" not in output
    assert "ADDED BY THIS PORT" not in output
    assert "parity gap" not in output
    # The twelve deferred options stay LISTED -- hiding a flag a user will
    # type, then refusing it at exit 1, is worse than naming it.
    assert "--verbose" in output
    assert "--non-interactive" in output
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `py -3.12 -m pytest tests/commands/test_build_command.py -q -k archaeology`

Expected: FAIL on `assert "v0.4.1" not in output`.

- [ ] **Step 3: Rewrite the three help strings**

In `python/tan/commands/build_cmd.py`, replace `_DEFERRED_HELP` at line 172:

```python
_DEFERRED_HELP = "Accepted by other commands; not implemented for `build` yet (tan-cli#427)."
```

Replace the `--native` help at lines 1213-1217:

```python
    native: bool = typer.Option(
        False,
        "--native",
        help="Build natively: materialise the plan, then run each slice's command. "
        "The default when no plan-mode flag is given. Does NOT override the --plan "
        "implied by --plan-from -- use --execute for that.",
    ),
```

Replace the `--execute` help at lines 1218-1228:

```python
    execute: bool = typer.Option(
        False,
        "--execute",
        help="Materialise the plan AND run each slice's command, even when the plan "
        "came from --plan-from -- run a pinned, reviewed plan file reproducibly. "
        "Implies --materialise (nothing can run that was never written); reports the "
        "ordinary build result.",
    ),
```

Move the dropped rationale into the `build` function's docstring, alongside the existing note at `build_cmd.py:61`. Add these two sentences there:

```
`--execute` is ADDED BY THIS PORT, not a v0.4.1 flag: there `--plan-from`
implies `--plan` and outranks `--native`, so a file-supplied plan cannot be
dispatched at all. Deliberate, not a parity gap. `--native` keeps v0.4.1's
behaviour of NOT overriding the `--plan` that `--plan-from` implies, which
`test_plan_from_shows_the_plan_and_writes_nothing_even_with_native` pins.
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `py -3.12 -m pytest tests/commands/test_build_command.py -q -k archaeology`

Expected: PASS.

- [ ] **Step 5: Run the full suite**

Run: `py -3.12 -m pytest tests -q`

Expected: zero failures. `test_plan_from_shows_the_plan_and_writes_nothing_even_with_native` must still pass — this task changes no behaviour, only help text.

- [ ] **Step 6: Commit**

```bash
git add python/tan/commands/build_cmd.py python/tests/commands/test_build_command.py
git commit -m "docs(build): write --help for users, not for the port's changelog"
```

---

### Task 3: A next step on every dead-end refusal

**Files:**
- Modify: `python/tan/commands/build_cmd.py:668-681` (two `build.plan-unavailable` refusals)
- Modify: `python/tan/commands/sdk_cmd.py:959-973` (the `not-ported` refusal)
- Modify: `python/tan/commands/init_cmd.py:679-695` (the `init.would-overwrite` refusal)
- Modify: `python/tan/cli.py:610` (`ctx.fail("a command is required")`)
- Test: `python/tests/test_cli_skeleton.py`, `python/tests/commands/test_init_command.py`

**Interfaces:**
- Consumes: Task 1's panelled `tan/cli.py` and Task 2's rewritten `build_cmd.py`. Land after both.
- Produces: nothing other tasks read.

**Background the implementer needs:**

`FileChange` is `@dataclass` with fields `relative_path: str` and `kind: str` (`tan/core/scaffold.py:170-174`). `init_cmd.py:679` already holds the full `changes` list and tests `any(c.kind == "update" for c in changes)` — so the message can name which files, with no new plumbing.

The bare-`tan` refusal is safe to extend: measured, `tan` with no arguments writes **0 bytes to stdout**, 612 to stderr, and exits 2. The frozen parity case `([], ENVELOPE, None)` (`tests/parity/test_oracle_parity.py:79`) asserts stdout stays empty, which it does — `ctx.fail` writes to stderr.

**Hazard to check, not a blocker:** under `--format json`, `cli.py:824` folds the tee'd stderr **verbatim** into the usage-error envelope's `data.message` and `issues[0].message`. Wording added to the bare-`tan` refusal is therefore visible in the JSON channel. No frozen case covers a bare `--format json` invocation, so this is a review item — Step 6 below checks it by hand.

- [ ] **Step 1: Write the failing tests**

Append to `python/tests/test_cli_skeleton.py`:

```python
def test_bare_invocation_points_a_new_user_somewhere():
    """"a command is required" is true and useless. A first-time user needs
    the three verbs that get them from nothing to a running build."""
    p = run()
    assert p.returncode == 2
    assert p.stdout == ""          # unchanged: stdout is the envelope channel
    assert "tan doctor" in p.stderr
    assert "tan init" in p.stderr
```

Append to `python/tests/commands/test_init_command.py`:

```python
def test_would_overwrite_names_the_files_and_offers_preview(tmp_path):
    """"One or more files" is the one fact the user already knows. The command
    holds the FileChange list; naming the paths and offering --preview is what
    turns a dead end into a next step."""
    from tan.core.scaffold import FileChange

    changes = [
        FileChange(relative_path="board.yaml", kind="update"),
        FileChange(relative_path="src/main.c", kind="create"),
    ]
    message = overwrite_refusal_message(changes)

    assert "board.yaml" in message
    assert "src/main.c" not in message      # only the "update" kind collides
    assert "--preview" in message
    assert "--force" in message
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `py -3.12 -m pytest tests/test_cli_skeleton.py tests/commands/test_init_command.py -q -k "new_user or names_the_files"`

Expected: the first FAILS on `"tan doctor" in p.stderr`; the second FAILS with `NameError: name 'overwrite_refusal_message' is not defined`.

- [ ] **Step 3: Add the `init` message helper and use it**

In `python/tan/commands/init_cmd.py`, add above the `_Outcome` construction at line 679:

```python
def overwrite_refusal_message(changes: list[FileChange]) -> str:
    """Name the files that actually collide, and offer the non-destructive
    option first. `--force` is the only escape the old wording named, which
    made overwriting look like the intended next step rather than the last
    resort."""
    colliding = [c.relative_path for c in changes if c.kind == "update"]
    listed = ", ".join(sorted(colliding))
    return (
        f"{len(colliding)} file(s) would be overwritten: {listed}. "
        "Run with --preview to see the full plan, --destination <DIR> or "
        "--name <NAME> to write somewhere else, or --force to overwrite."
    )
```

Import `FileChange` at the top of the module if it is not already imported (`from tan.core.scaffold import FileChange`).

Replace the hardcoded string at line 691:

```python
                Issue(
                    "init.would-overwrite",
                    "error",
                    overwrite_refusal_message(changes),
                )
```

The issue code `init.would-overwrite` and `ExitCode.WRITE_FAILURE` are unchanged — only the message text moves.

Make sure the test imports it: add `from tan.commands.init_cmd import overwrite_refusal_message` to `python/tests/commands/test_init_command.py`.

- [ ] **Step 4: Extend the three remaining refusals**

In `python/tan/commands/build_cmd.py`, the no-SDK refusal at lines 668-673:

```python
        raise BuildError(
            "build.plan-unavailable",
            "no alp-sdk checkout found -- pass `--sdk-root <PATH>` or run from a project "
            "beside one. Planning reads the SDK's `metadata/**`. Run `tan doctor` to see "
            "which checkout tan resolves, if any.",
            ExitCode.RUNTIME_FAILURE,
        )
```

The no-board.yaml refusal at lines 676-681:

```python
    if board_yaml is None:
        raise BuildError(
            "build.plan-unavailable",
            "no board.yaml found -- pass `--board-yaml <PATH>` or run from a project. "
            "Run `tan init` to create one, or `tan examples` to list ready-made projects.",
            ExitCode.RUNTIME_FAILURE,
        )
```

In `python/tan/commands/sdk_cmd.py`, the `text_lines` at lines 969-972:

```python
        text_lines=[
            f"sdk {subcommand}: not available in this build of tan.",
            "Use `--sdk-root <path>` to point a command at a checkout directly,",
            "or clone one: `git clone https://github.com/alplabai/alp-sdk`.",
            "`tan doctor` reports which checkout tan currently resolves.",
        ],
```

Leave the `message=` field at lines 962-967 unchanged — it is the JSON-mode wording and this plan does not change JSON output.

In `python/tan/cli.py`, line 610:

```python
        ctx.fail(
            "a command is required.\n"
            "New here? `tan doctor` checks this host, `tan init` creates a project, "
            "`tan build` builds it.\n"
            "`tan --help` lists all commands by category."
        )
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `py -3.12 -m pytest tests/test_cli_skeleton.py tests/commands/test_init_command.py -q`

Expected: PASS.

- [ ] **Step 6: Check the JSON channel by hand**

Run: `py -3.12 -m tan --format json`

Expected: exactly one JSON document on stdout. Confirm `data.message` and `issues[0].message` carry the new multi-line text and that the document still parses:

```bash
py -3.12 -m tan --format json | py -3.12 -c "import json,sys; d=json.load(sys.stdin); print(d['exitCode'], d['issues'][0]['code'])"
```

Expected: `2 cli.parse-error`.

- [ ] **Step 7: Run the full suite**

Run: `py -3.12 -m pytest tests -q`

Expected: zero failures. Pay attention to `tests/commands/test_cli_global_flags.py` — line 422 asserts `"a command is required" not in leading.stderr` for a *different* argv, which stays true.

- [ ] **Step 8: Commit**

```bash
git add python/tan/cli.py python/tan/commands/build_cmd.py python/tan/commands/sdk_cmd.py python/tan/commands/init_cmd.py python/tests/test_cli_skeleton.py python/tests/commands/test_init_command.py
git commit -m "feat(cli): give every dead-end refusal a next step"
```

---

### Task 4: Make `presets` and `examples` answer by default

**Files:**
- Modify: `python/tan/commands/presets_cmd.py:543-552` (`render_presets_text`) and `:634` (its caller)
- Modify: `python/tan/commands/examples_cmd.py:279-327` (`render_examples_text`) and the `examples` option list
- Test: `python/tests/commands/test_presets_command.py`, `python/tests/commands/test_examples_command.py`

**Interfaces:**
- Consumes: nothing from earlier tasks. File-disjoint; may run concurrently with Tasks 1 and 2.
- Produces: `render_presets_text` changes signature from `(skus: list[str], board_libraries: list[str], verbose: bool)` to `(soms: list[Som], board_libraries: list[str], verbose: bool)`. No other module calls it.

**Background the implementer needs:**

`Som` is `@dataclass(frozen=True)` with `sku: str`, `display_name: str`, `family: str`, `cores: tuple[SomCore, ...]` (`presets_cmd.py:126-141`). `SomCore` is `id: str`, `os: str` (`:114-123`). The caller at `presets_cmd.py:609` currently discards everything but the SKU: `skus = [s.sku for s in soms]`.

`Example` is `id: str`, `source_dir: str`, `title: str`, `description: str` (`examples_cmd.py:98-106`). There is **no SoM field** — a `--som` filter is impossible without new alp-sdk metadata and is out of scope. The category is the `id` prefix before the first `/`. Measured on a real checkout, there are 12: `aen`, `ai`, `audio`, `bringup`, `camera-vision`, `connectivity`, `display`, `multicore`, `peripheral-io`, `power-timing`, `testing`, `v2n`.

`examples` **already has** `--filter` (case-insensitive substring on id or title, `example_matches_filter` at `:271`) and `--verbose` (appends the description). This task adds `--category` alongside them; it does not replace them.

`render_examples_text`'s three empty-result branches (`:308-317`) were split deliberately by tan-cli#400. Do not collapse them; add the category branch as a fourth case.

**JSON output is untouched in both commands.** `["presets", "--format", "json"]` is a frozen parity case (`test_oracle_parity.py:96`) and `examples`'s no-SDK JSON refusal is frozen at `test_command_surface_oracle_parity.py:366`.

- [ ] **Step 1: Write the failing tests**

Append to `python/tests/commands/test_presets_command.py`:

```python
def test_presets_text_lists_skus_with_display_names_by_default():
    """Three integers answer no question a user has. The SoM entries carry a
    display name already; the default output should show it."""
    from tan.commands.presets_cmd import Som, SomCore, render_presets_text

    soms = [
        Som(
            sku="E1M-AEN301",
            display_name="E1M-AEN301 (Alif Ensemble E3)",
            family="alif-ensemble",
            cores=(SomCore(id="m55_hp", os="zephyr"), SomCore(id="m55_he", os="zephyr")),
        )
    ]
    lines = render_presets_text(soms, ["lib-a"], verbose=False)

    assert lines[0] == "presets: skus=1 libraries=8 boardLibraries=1"
    assert any("E1M-AEN301" in line and "Alif Ensemble E3" in line for line in lines)
    # family/cores are the --verbose tier, not the default
    assert not any("m55_hp" in line for line in lines)


def test_presets_text_verbose_adds_family_and_cores():
    from tan.commands.presets_cmd import Som, SomCore, render_presets_text

    soms = [
        Som(
            sku="E1M-AEN301",
            display_name="E1M-AEN301 (Alif Ensemble E3)",
            family="alif-ensemble",
            cores=(SomCore(id="m55_hp", os="zephyr"),),
        )
    ]
    lines = render_presets_text(soms, [], verbose=True)

    assert any("alif-ensemble" in line for line in lines)
    assert any("m55_hp" in line and "zephyr" in line for line in lines)
```

Append to `python/tests/commands/test_examples_command.py`:

```python
def test_examples_text_names_the_categories_on_an_unfiltered_run():
    """100 flat lines with no hint a taxonomy exists. The category is already
    the id prefix -- surfacing it costs no new data."""
    from tan.commands.examples_cmd import Example, render_examples_text

    examples = [
        Example(id="ai/cold-chain", source_dir="ai/cold-chain", title="Cold chain", description=""),
        Example(id="audio/i2s-tone", source_dir="audio/i2s-tone", title="I2S tone", description=""),
    ]
    lines = render_examples_text(examples, filter_=None, category=None, verbose=False, sdk_resolved=True)
    joined = "\n".join(lines)

    assert "ai" in joined and "audio" in joined
    assert "--category" in joined


def test_examples_category_filter_narrows_and_reports_an_empty_match():
    from tan.commands.examples_cmd import Example, example_matches_category, render_examples_text

    entry = Example(id="ai/cold-chain", source_dir="ai/cold-chain", title="Cold chain", description="")
    assert example_matches_category(entry, "ai")
    assert not example_matches_category(entry, "audio")

    lines = render_examples_text([], filter_=None, category="nope", verbose=False, sdk_resolved=True)
    assert lines == ['examples: no example projects in category "nope".']
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `py -3.12 -m pytest tests/commands/test_presets_command.py tests/commands/test_examples_command.py -q -k "display_names or verbose_adds or categories or category_filter"`

Expected: all FAIL — `render_presets_text` still takes `list[str]`, and `render_examples_text` has no `category` parameter.

- [ ] **Step 3: Rewrite `render_presets_text`**

Replace `python/tan/commands/presets_cmd.py:543-552`:

```python
def render_presets_text(
    soms: list[Som], board_libraries: list[str], verbose: bool
) -> list[str]:
    """The count line, then one line per SoM.

    Takes `Som` rather than the bare SKU strings it used to: the count line
    answered nothing on its own, and every field the fuller answer needs was
    already parsed and then thrown away by the caller.
    """
    lines = [
        f"presets: skus={len(soms)} libraries={len(LIBRARIES)} "
        f"boardLibraries={len(board_libraries)}"
    ]
    if not soms:
        return lines
    sku_width = max(len(s.sku) for s in soms)
    for som in soms:
        lines.append(f"  {som.sku:<{sku_width}}  {som.display_name}")
        if verbose:
            cores = ", ".join(f"{c.id} ({c.os})" for c in som.cores)
            lines.append(f"  {'':<{sku_width}}  family: {som.family}; cores: {cores}")
    return lines
```

Update the caller at `presets_cmd.py:634`:

```python
        for line in render_presets_text(soms, board_libraries, verbose):
```

Leave `skus = [s.sku for s in soms]` at `:609` in place — the JSON payload at `:621` still uses it, and that payload is parity-frozen.

- [ ] **Step 4: Add `--category` to `examples`**

In `python/tan/commands/examples_cmd.py`, add beside `example_matches_filter` at `:271`:

```python
def example_category(entry: Example) -> str:
    """The catalogue's top-level directory -- `ai` for `ai/cold-chain-monitor`.
    Derived from the id rather than carried as a field: the SDK emits no
    category, and the id prefix IS the tree it came from."""
    return entry.id.split("/", 1)[0]


def example_matches_category(entry: Example, category: str) -> bool:
    return example_category(entry).lower() == category.lower()
```

Change `render_examples_text`'s signature and its empty branches (`:279-317`):

```python
def render_examples_text(
    examples: list[Example],
    filter_: str | None,
    category: str | None,
    verbose: bool,
    sdk_resolved: bool,
) -> list[str]:
```

Add the category branch inside the existing `if not examples:` block, after the `filter_` branch and before the final `return`:

```python
        if category is not None:
            return [
                f"examples: no example projects in category "
                f"{json.dumps(category, ensure_ascii=False)}."
            ]
```

Append the category hint after the entry loop, only on an unfiltered run:

```python
    if filter_ is None and category is None:
        categories = sorted({example_category(e) for e in examples})
        lines.append("")
        lines.append(f"categories: {' '.join(categories)}")
        lines.append("narrow with --category <NAME>, or --filter <TEXT> to search.")
    return lines
```

Add the option to the `examples` command signature, immediately after the
existing `--filter` at `examples_cmd.py:373-375`:

```python
    category: str = typer.Option(
        None,
        "--category",
        metavar="NAME",
        help="Only examples in this catalogue category (a bare `tan examples` "
        "prints the list).",
    ),
```

Apply it in the command body directly after the existing filter at
`examples_cmd.py:421-422`, so both narrowings compose:

```python
        if filter_ is not None:
            found = [e for e in found if example_matches_filter(e, filter_)]
        if category is not None:
            found = [e for e in found if example_matches_category(e, category)]
```

Update the single render call at `examples_cmd.py:459`:

```python
        for line in render_examples_text(found, filter_, category, verbose, sdk is not None):
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `py -3.12 -m pytest tests/commands/test_presets_command.py tests/commands/test_examples_command.py -q`

Expected: PASS. Existing tests in both files that assert the old count-only `presets` line or call `render_examples_text` with four positional arguments will fail — update them to the new shape. That is expected churn, not a regression.

- [ ] **Step 6: Confirm the JSON output did not move**

Run: `py -3.12 -m pytest tests/parity -q`

Expected: zero failures. `["presets", "--format", "json"]` is frozen; if it goes red, the JSON payload was touched and must be reverted.

- [ ] **Step 7: Eyeball both commands**

```bash
py -3.12 -m tan presets
py -3.12 -m tan presets --verbose
py -3.12 -m tan examples
py -3.12 -m tan examples --category audio
```

Expected: `presets` lists 11 SKUs with display names; `examples` ends with the 12-category line; `--category audio` narrows the list.

- [ ] **Step 8: Run the full suite**

Run: `py -3.12 -m pytest tests -q`

Expected: zero failures.

- [ ] **Step 9: Commit**

```bash
git add python/tan/commands/presets_cmd.py python/tan/commands/examples_cmd.py python/tests/commands/test_presets_command.py python/tests/commands/test_examples_command.py
git commit -m "feat(cli): presets and examples answer by default, and examples gains --category"
```

---

## Final verification

- [ ] **Full gate on the merged result**

```bash
py -3.12 -m pytest tests -q
py -3.12 scripts/version_check.py --selftest --self
```

Expected: zero pytest failures; `version_check.py` exits 0.

- [ ] **Confirm no JSON envelope moved**

```bash
py -3.12 -m pytest tests/parity tests/conformance -q
```

Expected: zero failures. This plan changes no envelope; a red here means something leaked out of text mode.

## Out of scope (recorded, not deferred silently)

- **`Issue.fix`.** `Issue` is `{code, severity, message}` with 108 construction sites; `doctor`'s `Check` carries a `fix` that is dropped on conversion, so `issues[]` — the channel alp-sdk-vscode reads — carries no remediation anywhere. Highest-value change available; needs a declared oracle divergence plus golden regeneration. Own sub-project.
- **The onboarding cliff.** `tan sdk install` and `tan sdk switch` both refuse. `tan` is inert without an alp-sdk checkout and the obvious command to get one does not work. Real feature work. Task 3 only softens the refusal; it does not fix it.
- **Completion-script de-duplication.** Investigated and withdrawn — the drift guard already exists and is green. See item 2 of the design doc.
