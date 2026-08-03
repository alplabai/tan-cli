<!-- SPDX-License-Identifier: Apache-2.0 -->
# Historical audit: can the Python `tan` ship as release assets?

> **Historical measurement record — not the current release contract.** This
> document captures the investigation that enabled the Python release and keeps
> intermediate failures/results verbatim. It therefore mentions an eight-asset
> experiment, `--onefile`, open blockers, and requested fixes that were later
> resolved. Current releases build four `--onedir` archives; use
> [`docs/release-contract.md`](release-contract.md), the root README, and
> `.github/workflows/release.yml` for current instructions.
>
> Current state: `python/pyproject.toml` installs correctly, `monitor_cmd.py` is
> committed, the planner omits unset `firmware_path`, release CI runs with
> `sdk_parity: true`, and neither `alp-tan` on PyPI nor `@alplabai/tan` on npm is
> published. Apparent contradictions below are chronology within the audit, not
> current work items.

**Yes — all eight assets now exist, built and verified in CI.** Run
[30555358227](https://github.com/alplabai/tan-cli/actions/runs/30555358227):
8/8 green, each through `--version`, `generate --help` carrying `--output`,
`init` writing the vendored tree, and a real `generate --output` emit, plus an
architecture verdict on all four assets where a wrong runner label would ship a
mislabelled binary, plus `checksums.txt` over exactly 8 files. Sizes and verdicts
in §1.0. All four blockers are closed; §0 records how, and what remains is one
in-flight fix of the maintainer's (`firmware_path`, §3.4a) plus release decisions
that are theirs to make.

## 0. Blocker status

| # | Blocker | State | Proof |
|---|---|---|---|
| 1 | `click` undeclared | **closed** by `f55c9b8` (declares `click>=8.1`, moves `pyserial` to a `monitor` extra, adds `tests/gates/test_declared_dependencies.py`) | rebuilt from the declared set: 13517584 B, 4/4 proofs, no `ModuleNotFoundError` |
| 1b | `[project.optional-dependencies]` was inserted **above** `classifiers` in `python/pyproject.toml`, so `classifiers` became an extras group | **OPEN — maintainer's file.** Breaks every install, including the `pip install -e .` that blocker 1 exists to protect | `configuration error: 'project.optional-dependencies.classifiers[0]' must be pep508` |
| 2 | size gate rejected a correct build, and a pipe defeated it | **closed** | ceilings now live once in `python/scripts/artifact_ceilings.env`; over-ceiling artifacts are quarantined so `cp` fails even when the pipe swallows the status |
| 3 | four version sources disagreed | **closed** | `python/scripts/version_check.py` + `release.yml`'s rewritten `verify-version` + `npm-shim` bumped to `0.5.0-dev`; `--tag v0.5.0` correctly fails a `-dev` tree |
| 4 | no `pytest` on the release gate path | **closed** | `ci.yml` gains a `python` job (`881 passed, 282 skipped, 0 failed`); the `sdk_parity` input exists to un-skip the parity family but `release.yml` passes **false** — see §3.4, it is 25 red against alp-sdk main for a reason a tag cannot fix |

### 0.0 Round-2 status (after `9381fbc` + `729234a`)

`python/pyproject.toml` is installable again — `pip install -e ./python` resolves
`alp-tan 0.5.0.dev0` with `click 8.4.2` and **no** pyserial, and the metadata
reads back `classifiers: 7`,
`requires: [..., 'pyserial>=3.5; extra == "monitor"']`. The build recipe and the
`ci.yml` python job both work on it: frozen from that install, 13571067 B, four
proofs green. Full suite on the merged tree: **946 passed, 310 skipped, 0
failed**.

| Item | State |
|---|---|
| three remaining assets | **BUILT AND VERIFIED** in CI once the workflow reached `main` (§1.0) |
| `tan monitor` without pyserial | **fixed** (`bd7079d`), confirmed on a frozen build — §0.2 |
| `[monitor]` in the release binary | **shipping** — §0.3, +73392 B |
| npm-shim libc mismatch | **fixed and pinned** — §6c |
| `sdk_parity` against `design/tan-python-port` | **still red**, same 25 — §3.4a |

### 0.2 `tan monitor` on a no-extras build — FIXED (`bd7079d`), confirmed frozen

Rebuilt frozen from the declared deps only (pyserial absent from the build venv),
both paths:

```
$ tan monitor --port COM7 --format json
{"command":"monitor","ok":false,"exitCode":1,...,"issues":[{"code":"monitor.pyserial-missing",
 "message":"pyserial is required for `tan monitor`. Install it with `pip install \"alp-tan[monitor]\"`.
  A frozen `tan` binary bundles it at build time, so a binary built without that extra cannot gain it here."}]}
exit = 1
```

Identical for `tan monitor --format json` with no `--port`, identical in text
mode, zero tracebacks, and `--version` / `generate --output` / `init` still pass
the four proofs. The guard sits at `_available_ports()` — the choke point — so
the frozen path can no longer reach an unguarded `serial.tools` import, and the
message is honest about the one thing a person holding a `--onefile` binary
cannot do about it.

The original finding, for the record:

`monitor` exists in a commit now, and the degradation is not the intended one:

```
$ tan monitor --port COM7 --format json
{"command":"monitor","ok":false,"exitCode":5,...,"issues":[{"code":"monitor.internal-failure",
 "severity":"error","message":"monitor failed unexpectedly: ModuleNotFoundError: No module named 'serial'"}]}
```

Zero tracebacks, so the envelope guard holds. But `monitor.internal-failure` at
exit 5 means "tan bug", and the actionable code the module already carries —
`monitor.pyserial-missing`, "pyserial is required. Install via `pip install
pyserial`." (`monitor_cmd.py:119-124`) — **cannot fire on a frozen binary at
all**:

- `monitor_cmd.py:108` sets `using_this_interpreter = not getattr(sys, "frozen",
  False) and bool(sys.executable)`, so on a PyInstaller build it is `False` and
  the guarded `import serial` at 111-124 is skipped by design (the child gets a
  PATH `python`, which is meant to report its own missing pyserial).
- But `_available_ports()` at line 76 does `from serial.tools import list_ports`
  **unguarded**, and lines 126/128 call it in-process before any child is
  spawned — for both `--port` given and omitted.

So every frozen no-extras build misclassifies a missing optional dependency as an
internal failure. Your file (`python/tan/commands/monitor_cmd.py`), reported not
touched. Two fixes are possible and they are not equivalent: guard
`_available_ports()` (then the message is right on every build), or ship the
extra (then the path is never reached) — see §0.3.

### 0.3 The release binary carries `monitor` — DECIDED, and wired

`python-binaries.yml` installs `-e ".[monitor]"` on all four build lines (the
quotes matter: `[monitor]` is a bash glob character class). `ci.yml` deliberately
does **not** — it must keep testing the extras-less shape a customer's
`pip install alp-tan` produces, which is the only shape in which
`test_declared_dependencies.py` can catch an extras-only import escaping to
module scope. That asymmetry is intentional and is stated at both ends.

Measured after `bd7079d`, one host, clean venvs:

| build | bytes | vs DEFAULT ceiling 16500000 |
|---|---|---|
| `pip install -e .` | 13574636 | 2925364 B headroom |
| `pip install -e ".[monitor]"` | 13648028 | 2851972 B headroom |

**+73392 B, 0.54%.** The ceiling does not decide this — there is 2.8 MB of room
either way. What it buys is a `monitor` that works, and pyserial cannot be added
to a `--onefile` binary afterwards, so a build without it ships a command that can
never work on the machine holding it:

```
$ tan monitor --format json        # [monitor] build
{"command":"monitor","ok":false,"exitCode":1,...,"issues":[{"code":"monitor.no-port",
 "message":"no --port given -- available serial ports: COM31  Standard Serial over Bluetooth link (COM31); ..."}]}
```

Recommendation: ship it (`-e .[monitor]` on the three install lines in
`python-binaries.yml`; the comment there carries these numbers). It is release
CONTENT, so the call is yours — and note it only hides §0.2 rather than fixing
it: a source install still takes the guarded path, and a frozen build with the
extra never reaches the unguarded one.

### Historical requests (all resolved)

The four requests below were the handoff at this point in the audit. They are
retained as evidence of what was blocking that snapshot; none remains an open
instruction.

1. **`python/pyproject.toml`: move `[project.optional-dependencies]` below the
   `classifiers` array** (or move `classifiers` above line 72). One table
   placement, no value changes. Until then `pip install ./python` cannot run at
   all, which also means the new `ci.yml` python job and
   `scripts/build_binary.sh`'s `pip install -e .` recipe are red for a reason
   unrelated to either.
2. **At release time only, two lines:** `python/tan/version.py`'s
   `TAN_VERSION = "0.5.0-dev"` → `"0.5.0"`, and `python/pyproject.toml`'s
   `version = "0.5.0.dev0"` → `"0.5.0"`. Nothing else: the two files already
   agree under the SemVer↔PEP 440 mapping, so no change is needed today, and
   `npm-shim/package.json` is bumped here already. `version_check.py --tag
   v0.5.0` is what enforces it.
3. **`python/tan/commands/monitor_cmd.py` is still uncommitted**, so `tan
   monitor` does not exist in any commit (`No such command 'monitor'. Did you
   mean 'doctor'?`). The pyserial-extra degradation therefore could not be
   exercised here — see §0.1.
4. **`tan/planner/manifest.py` emits `firmware_path: null` where alp-sdk omits
   the key**, which fails 25 planner byte-parity tests against alp-sdk main and
   is the one thing keeping `sdk_parity` off the release gate (§3.4). Also your
   file — `python/tan/planner/**`.

### 0.1 What "pyserial absent" could and could not be shown

The default build installs no extras, so pyserial is absent from the artifact,
and that costs nothing measurable: `tan --version` → `tan 0.5.0-dev`, and
`generate --target zephyr-conf --output` emitted a real `alp.conf`
(`"ok":true`). What could not be shown is `tan monitor` degrading, because the
command is not in the tree — `monitor_cmd.py` and its test are untracked in the
python-executor worktree. Once committed, the frozen binary will still not carry
pyserial (deliberate: an extra is optional at runtime by definition), so if a
release ever wants `monitor` to work from the binary, build with
`pip install -e .[monitor]` and raise the ceilings — that changes the artifact
being gated. Both facts are recorded at the pin in
`.github/workflows/python-binaries.yml`.

Nothing in this document was published: no tag, no release, no PR. It records
what was actually run, with output.

**Host used for every measurement below:** Windows 11 x86_64 (`AMD64`), Python
3.12.10 (`py -3.12`), PyInstaller 6.21.0, clean venv holding only
`typer rich pyyaml jsonschema click pyinstaller`; WSL2 `Ubuntu-22.04` (glibc
2.35) with Docker Engine 29.1.3; QEMU arm64 via
`docker run --privileged --rm tonistiigi/binfmt --install arm64`.

## 1. Per-asset result

| Asset | Buildable | How | Verified how | Open issue |
|---|---|---|---|---|
| `tan-x86_64-pc-windows-msvc.exe` | **PROVED here** | native Windows, `scripts/build_binary.sh`, 13517737 B | 4/4 proofs on the host | — |
| `tan-x86_64-unknown-linux-gnu` | **PROVED here** | `docker python:3.12-slim-bullseye` (Debian 11, glibc 2.31) + `binutils`, 14042352 B | 4/4 in `debian:bullseye-slim` with **no Python installed** | floor is glibc **2.29**, not 2.31 — see §4.2 |
| `tan-x86_64-unknown-linux-musl` | **PROVED here** | `docker python:3.12-alpine`, 14998816 B | 4/4 in `alpine:3.20` with **no Python installed** | 1184 B under the size gate — §3.2 |
| `tan-aarch64-unknown-linux-gnu` | **PROVED here** | `docker --platform linux/arm64 python:3.12-slim-bullseye` under QEMU, 162.9 s, 13840528 B | 4/4 in arm64 `debian:bullseye-slim` under QEMU, **no Python** | CI should use `ubuntu-24.04-arm` natively, not QEMU |
| `tan-aarch64-unknown-linux-musl` | **PROVED here** | `docker --platform linux/arm64 python:3.12-alpine` under QEMU, 15276264 B, `OK: dist/tan is 15276264 B (ceiling 18000000, libc=musl)` | 4/4 in arm64 `alpine:3.20` under QEMU, **no Python** | was rejected by the old single ceiling; §3.2 closed |
| `tan-aarch64-pc-windows-msvc.exe` | **NOT built here** (no ARM Windows host) | `windows-11-arm` runner: image ships Python 3.13.14 and tool-cache **3.12.10**; PyInstaller publishes `pyinstaller-6.21.0-py3-none-win_arm64.whl`, so the bootloader is prebuilt | — | unproved: nobody has run PyInstaller on that image in this repo |
| `tan-x86_64-apple-darwin` | **NOT built here** (no macOS host) | `macos-15-intel` (Intel, available to private repos); `pyinstaller-6.21.0-py3-none-macosx_10_13_universal2.whl` | — | **`macos-latest` is arm64** — using it silently produces an arm64 binary under the x86_64 name |
| `tan-aarch64-apple-darwin` | **NOT built here** (no macOS host) | `macos-latest` (arm64), same universal2 wheel | — | unproved |

Every asset name above is byte-identical to `release.yml`'s matrix
(`.github/workflows/release.yml:107,111,118,123,128,133,138,142`) and to what
`releaseAssetForTarget` builds at `alp-sdk-vscode/src/alpCli/service.ts:307`:
`` `tan-${target}${platform === "win32" ? ".exe" : ""}` ``.

The four proofs, run by `python/scripts/verify_binary.sh` (added here):
`--version`; `generate --help` carries `--output`; `init --template zephyr-app`
writes `board.yaml` + `src/main.c` (proves the vendored-template `--add-data`);
`generate --target zephyr-conf --output ./out/alp.conf --sdk-root <sdk>` emits a
file containing `CONFIG_` lines. A binary that starts but cannot emit fails.

### 1.0 ALL EIGHT ASSETS BUILT AND VERIFIED IN CI

`python-binaries.yml` landed on `main` (`4b39a86`, PR #247), and dispatching it
with `--ref chore/python-freeze-release` ran this branch's version — `main` only
had to carry the file, as predicted. Run
[30555358227](https://github.com/alplabai/tan-cli/actions/runs/30555358227):
**8/8 success plus the checksums job**, every asset built with
`pip install -e ".[monitor]"`, every one through the same four proofs.

| Asset | Runner | Bytes | Ceiling | Arch verdict |
|---|---|---|---|---|
| `tan-x86_64-pc-windows-msvc.exe` | `windows-latest` | 14638614 | 16500000 | `PE machine 0x8664 (want 0x8664)` |
| `tan-aarch64-pc-windows-msvc.exe` | `windows-11-arm` | 14345471 | 16500000 | `PE machine 0xaa64 (want 0xaa64)` |
| `tan-x86_64-unknown-linux-gnu` | `ubuntu-latest` + Debian 11 | 14172632 | 16500000 | — |
| `tan-aarch64-unknown-linux-gnu` | `ubuntu-24.04-arm` + Debian 11 | 13971392 | 16500000 | — |
| `tan-x86_64-unknown-linux-musl` | `ubuntu-latest` + Alpine | 15132432 | 18000000 | — |
| `tan-aarch64-unknown-linux-musl` | `ubuntu-24.04-arm` + Alpine | 15406288 | 18000000 | — |
| `tan-x86_64-apple-darwin` | `macos-15-intel` | 14408848 | 16500000 | `Mach-O 64-bit executable x86_64` |
| `tan-aarch64-apple-darwin` | `macos-latest` | 13739040 | 16500000 | `Mach-O 64-bit executable arm64` |

The arch checks are the point of the exercise and they hold: the Intel asset came
off `macos-15-intel` as `x86_64`, the Apple-silicon asset off `macos-latest` as
`arm64`, and the ARM Windows asset off `windows-11-arm` with COFF machine
`0xaa64`. A wrong runner label would have produced a correctly named asset of the
wrong architecture, and only these two steps would have caught it.

**macOS against `DEFAULT=16500000`: 14408848 and 13739040 — both UNDER, with
2.09 MB and 2.76 MB of headroom.** So macOS does not need its own ceiling line;
the recorded guidance (give macOS its own line rather than raise the shared
ceiling) applies only above DEFAULT and was not triggered. Largest asset overall
is still `aarch64-unknown-linux-musl` at 15406288 against its 18000000 musl
ceiling.

`checksums.txt` generated over exactly 8 files, `sha256sum` format:

```
ad147dc7fc8cc92b036ba9bfbf0c6b871b9d7c93d3ef066a0073559567d1f507  tan-aarch64-apple-darwin
e18244ac891895dae99530858320c43a460fbd0b73f082e5fb1434b492f12ce5  tan-aarch64-pc-windows-msvc.exe
9f1212207cd6c00d98525bf6d3840a74cec9cdabd415217c24ea581e4f2053fe  tan-aarch64-unknown-linux-gnu
8308871dc692370130b5944f257e380909a21bdeb373bd5db734a542e57001fc  tan-aarch64-unknown-linux-musl
ea500a763abb04401ba58179e61bd449f0c900fc756b7c7483440041dbe0c43a  tan-x86_64-apple-darwin
717189a8a7cf0f04f046f02b86ebab5e3d1e3423f74a846883b12e36822466a2  tan-x86_64-pc-windows-msvc.exe
1d1a63b7f072a6eb4c507fef64a0a12a0b82dc6f29df64b07a283f61f54e307c  tan-x86_64-unknown-linux-gnu
e2350c519df91114c8210a4edbb7c9d1736ba5b00ded0452286d267111d980fd  tan-x86_64-unknown-linux-musl
```

Two runs were burned on one defect, and it was in the PROOF, not in any binary:
`grep -- "--output"` on rendered help. macOS runners give rich a colour-capable
terminal and rich styles a flag's leading dash separately
(`ESC[1;36m-ESC[0mESC[1;36m-output`), so the literal string is not in the bytes;
Windows and Linux passed only because neither emitted colour. Fixed by asking for
`NO_COLOR=1`, stripping escapes anyway, and reducing to letters/digits/hyphens —
verified against a byte-level reproduction of the macOS output, with a stub whose
help carries only `--target` as the negative control.

### 1.2 (historical) The three remaining assets could not be dispatched

Branch pushed (`chore/python-freeze-release`), then:

```
$ gh workflow run python-binaries.yml --ref chore/python-freeze-release -f verify=true
HTTP 404: workflow python-binaries.yml not found on the default branch
(https://api.github.com/repos/alplabai/tan-cli/actions/workflows/python-binaries.yml)
```

The default branch is `main`, and
`gh api repos/alplabai/tan-cli/contents/.github/workflows/python-binaries.yml?ref=main`
is `404` too. GitHub registers `workflow_dispatch` workflows from the **default
branch only**; a workflow that exists on a topic branch is not dispatchable from
anywhere, on any ref.

**What unblocks it:** `.github/workflows/python-binaries.yml` has to exist on
`main` — a merge of this branch, or a cherry-pick of that one file. After that,
`--ref chore/python-freeze-release` runs the version on THIS branch, so `main`
only needs the file to exist, not to be current. No other route was taken: adding
a `push:` trigger would make an 8-asset matrix run on every push and is the
"somewhere it doesn't belong" this was told to avoid, and nothing else in the repo
has a dispatchable workflow that could stand in.

Consequences for the three assets, unchanged from the last round: the runner
labels (`windows-11-arm`, `macos-15-intel`, `macos-latest`) and the PyInstaller
wheels (`win_arm64`, `macosx_10_13_universal2`) all exist, so this is one CI run
away — not a technical unknown. **macOS remains unmeasured against
`TAN_MAX_ARTIFACT_BYTES_DEFAULT=16500000`**; if a macOS build lands between that
and ~34 MB, `artifact_ceilings.env` says to give macOS its own line rather than
raise DEFAULT for every platform, and that guidance stands untested.

### 1.1 Why "eight from three runners" does not carry over

`release.yml` gets 8 assets from 3 runner types because **cargo
cross-compiles**: `windows-latest` builds both MSVC targets,
`ubuntu-latest` + `cargo-zigbuild` builds all four Linux targets,
`macos-latest` builds both Darwin targets. PyInstaller freezes *the interpreter
it is running*, so it produces exactly one OS+arch per host. A Python release
needs **eight native hosts**. All eight labels exist and are available to
private repositories (GitHub docs, 2026-07): `windows-latest`,
`windows-11-arm`, `ubuntu-latest`, `ubuntu-24.04-arm`, `macos-15-intel`,
`macos-latest`. Private repos get 2 CPUs instead of 4 on the Linux/Windows
runners, so build minutes roughly double against the public numbers.

## 2. What the extension actually downloads

Both Linux arches map to the **musl** assets, never `-gnu`
(`alp-sdk-vscode/src/alpCli/service.ts:42-43`):

```
42:  "linux/x64": "x86_64-unknown-linux-musl",
43:  "linux/arm64": "aarch64-unknown-linux-musl",
```

The binary is written raw to one cached path — `<globalStorageUri>/cli/tan`
(`vscodeAdapter.ts:98-99,133`) — chmod 0o755 on non-Windows
(`download.ts:124-128`, again at `adapterCore.ts:142-143`), and **nothing
unpacks it**. `--onefile` is therefore mandatory, and `--onedir` is unusable.
`SUPPORTED_CLI_VERSION = "0.4.0"` (`service.ts:27`).

## 3. Blockers

### 3.1 `click` is undeclared — this also breaks `pip install`

`python/tan/cli.py:15` is `from click.testing import CliRunner`. typer 0.27.0
no longer depends on click:

```
$ python -c "from importlib.metadata import requires; print(requires('typer'))"
['shellingham>=1.3.0', 'rich>=13.8.0', 'annotated-doc>=0.0.2', 'colorama; platform_system == "Windows"']
```

`python/pyproject.toml:47` declares
`dependencies = ["typer>=0.12", "rich>=13", "pyyaml>=6", "jsonschema>=4.18"]`.
So a clean environment has no click, and the first frozen build died before
printing a byte:

```
Traceback (most recent call last):
  File "__main__.py", line 3, in <module>
  File "pyimod02_importers.py", line 457, in exec_module
  File "tan\cli.py", line 15, in <module>
    from click.testing import CliRunner
ModuleNotFoundError: No module named 'click'
[PYI-47404:ERROR] Failed to execute script '__main__' due to unhandled exception!
```

This is not a freezing artefact: `pip install alp-tan` into a fresh venv fails
the same way with a current typer. **Fix: declare `click` in
`python/pyproject.toml`, or use `typer.testing.CliRunner`** (typer ships one —
`typer.testing.CliRunner` resolves in the same venv). Everything below was
built with `click` installed explicitly.

### 3.2 The size gate already rejects a correct aarch64 musl build — CLOSED

**Closed as follows.** Both ceilings now live once, in
`python/scripts/artifact_ceilings.env`, which `build_binary.sh` SOURCES and
`test_packaged_binary.py` PARSES — the drift is structurally impossible rather
than commented against. `TAN_MAX_ARTIFACT_BYTES_DEFAULT=16500000` and
`TAN_MAX_ARTIFACT_BYTES_MUSL=18000000`, each ≥1.2 MB above the largest measured
clean build of its class and each under 52% of the 34349423 B dirty measurement.
The script now detects the libc it is building against (`ldd --version`, with an
`ld-musl-*` fallback) and prints which ceiling it applied:
`OK: dist/tan is 15276264 B (ceiling 18000000, libc=musl)`.

The pipe defeat is closed differently, because it cannot be closed by an exit
status: `bash scripts/build_binary.sh | tail -3` returns `tail`'s status no
matter what the script does. So an over-ceiling artifact is **quarantined** —
moved to `dist/tan.oversized` — and the name every consumer copies from stops
existing. Proved with the real script and the real pipe, ceiling forced to 1000:

```
ERROR: dist/tan.exe was 13517882 B (ceiling 1000, libc=default).
       Quarantined as dist/tan.exe.oversized; nothing is left to ship.
pipeline exit status = 0  <-- tail's status, the original defect
cp: cannot stat 'dist/tan.exe': No such file or directory
cp exit = 1
```

A quarantined artifact also fails the conformance suite loudly instead of
skipping it (`dist/` is gitignored, so "gate tripped" and "not built yet" used to
look identical), and a clean rebuild clears the quarantine.

The historical measurements that motivated all of this:

`python/scripts/build_binary.sh:73` sets `MAX_ARTIFACT_BYTES=15000000` as a
dirty-interpreter guard, and `python/tests/conformance/test_packaged_binary.py:28`
pins the same number with `assert size < MAX_ARTIFACT_BYTES`. Measured from
clean venvs (only `typer rich pyyaml jsonschema click pyinstaller` installed):

| asset | bytes | headroom |
|---|---|---|
| `tan-x86_64-pc-windows-msvc.exe` | 13517737 | 1482263 |
| `tan-aarch64-unknown-linux-gnu` | 13840528 | 1159472 |
| `tan-x86_64-unknown-linux-gnu` | 14042352 | 957648 |
| `tan-x86_64-unknown-linux-musl` | 14998816 | **1184** |
| `tan-aarch64-unknown-linux-musl` | **15277408** | **−277408 (FAILS)** |

```
ERROR: dist/tan is 15277408 B (ceiling 15000000).
       Built from a dirty interpreter -- PyInstaller bundled modules
       tan never imports. Rebuild from a clean venv (see header).
```

That build is not dirty — the same binary then passed all four proofs in a
Python-free arm64 `alpine:3.20`. musl links statically, so both musl assets are
larger than their glibc twins by ~1 MB (x86_64) to ~1.4 MB (aarch64), and the
x86_64 one clears the gate by 1184 bytes purely by luck. **Raise the ceiling
(both places move together; one is under `python/tests/`, which this branch does
not touch) or make it per-libc.** Note also that the ceiling exists to catch a
dirty interpreter, and at these margins it can no longer tell the two apart.

One CI trap found while measuring: the failure is invisible if the script is
piped. `bash scripts/build_binary.sh 2>&1 | tail -3` returns `tail`'s status, so
`set -e` did not fire and the oversized artifact was copied out anyway. Any job
that runs the script must not pipe it (`python-binaries.yml` does not).

### 3.3 `verify-version` compares the tag to `Cargo.toml` — CLOSED

**Decision: `python/tan/version.py`'s `TAN_VERSION` is the single source of
truth.** It is the string the artifact PRINTS and the one alp-sdk-vscode compares
against `SUPPORTED_CLI_VERSION`, and it is SemVer, which is what the tag is.
`python/pyproject.toml` must carry its PEP 440 rendering; `npm-shim/package.json`
must equal it exactly (postinstall derives `TAG = v${pkg.version}`);
**`Cargo.toml` is no longer read as a release version** — it versions the Rust
crates on their own cadence.

`python/scripts/version_check.py` implements the reconciliation explicitly
(`0.5.0-dev` ↔ `0.5.0.dev0`, `-rc.1` ↔ `rc1`, …) instead of string-comparing
across the two spellings, refuses forms it cannot round-trip (`0.5.0+abc123`,
`0.5.0-nightly`), carries a `--selftest`, and imports only the stdlib so the gate
cannot fail for an unrelated reason. `release.yml`'s `verify-version` now runs
`python python/scripts/version_check.py --selftest --tag "$GITHUB_REF_NAME"`, and
`ci.yml` runs `--selftest --self` on every push so drift is caught before a tag
exists. Observed:

```
selftest: OK
TAN_VERSION (source of truth) : 0.5.0-dev
python/pyproject.toml         : 0.5.0.dev0 (want 0.5.0.dev0)
npm-shim/package.json         : 0.5.0-dev (want 0.5.0-dev)
versions agree
```

One consequence had to be handled: with `verify-version` no longer gating on
`Cargo.toml`, a `v0.5.0` tag now REACHES `publish_crates`, which would have put
`alp-tan-cli@0.4.1-dev` on crates.io while its summary claimed `0.5.0` — and a
crates.io publish is unretractable. That job now decides whether the tag is a
Rust release by comparing `Cargo.toml` to the tag, and when it is not, skips with
a step-summary line saying so (an annotation alone is what #151 proved unreadable).

The four sources as they were:

```
67:          cargo_version="$(grep -m1 '^version = ' Cargo.toml | sed -E 's/version = "([^"]+)"/\1/')"
```

Four version sources exist today and three disagree:

| file:line | value |
|---|---|
| `Cargo.toml:7` | `0.4.1-dev` |
| `npm-shim/package.json:3` | `0.4.1-dev` |
| `python/tan/version.py:12` | `0.5.0-dev` |
| `python/pyproject.toml:13` | `0.5.0.dev0` |

A `v0.5.0` tag fails `verify-version` today, on the Rust version, before any
asset is built. **What should change:** the job must read the version the
released artifact actually reports — `TAN_VERSION` in `python/tan/version.py`,
which is what `tan --version` prints — and must additionally reconcile
`python/pyproject.toml`, whose PEP 440 spelling differs from the SemVer tag
(`0.5.0.dev0` vs `0.5.0-dev`; a release tag `v0.5.0` needs `version = "0.5.0"`
in both). Keep the `npm-shim` check exactly as it is: `postinstall.js` derives
the asset tag as `` TAG = v${pkg.version} ``, so the shim must be bumped in the
same commit or `npm i` downloads a tag that does not exist.

Suggested replacement for the first step, same grep-one-line style as the
existing checks:

```bash
tag="${GITHUB_REF_NAME#v}"
py_version="$(grep -m1 '^TAN_VERSION = ' python/tan/version.py | sed -E 's/.*"([^"]+)".*/\1/')"
proj_version="$(grep -m1 '^version = ' python/pyproject.toml | sed -E 's/version = "([^"]+)"/\1/')"
echo "tag=$tag  tan/version.py=$py_version  pyproject=$proj_version"
[ "$tag" = "$py_version" ] || { echo "::error::tag v$tag != TAN_VERSION $py_version"; exit 1; }
[ "$tag" = "$proj_version" ] || { echo "::error::tag v$tag != pyproject version $proj_version"; exit 1; }
```

### 3.4 The release would ship untested — CLOSED

`ci.yml` now has a `python` job: `setup-python 3.12` → `pip install -e ./python`
(the DECLARED set, so a wrong declaration fails CI rather than a customer) →
`version_check.py --selftest --self` → `python -m pytest tests -q`. Zero failures
is the gate, never a count: skips and xfails are green and the suite grows as
ports land. `release.yml`'s `gates` job already calls this workflow, so a tag
cannot publish a freeze of code no CI step exercised.

Measured while wiring it: a bare run is `881 passed, 282 skipped` — and the skips
are not noise. 34 groups want `ALP_SDK_ROOT` (planner byte-parity, planner
binding) and the rest want a Rust `tan` to diff against (`TAN_RUST_BINARY`). A
gate that silently skips every parity case is most of a gate, so `ci.yml` takes a
`sdk_parity` input which checks out `alplabai/alp-sdk` and exports
`ALP_SDK_ROOT`.

**But `release.yml` passes `sdk_parity: false`, on purpose.** With it on, against
public alp-sdk main (v0.14.0, `ef79eab`):

```
FAILED tests/parity/test_planner_emit_parity.py::test_every_mode_is_byte_identical[v2n-temp-sensor]
FAILED tests/parity/test_planner_emit_parity.py::test_every_mode_is_byte_identical[v2n-xspi-flash-readwrite]
25 failed, 1622 passed, 209 skipped in 813.40s (0:13:33)
```

One real divergence, not a flake and not a line-ending artefact — the first diff
is:

```
--emit system-manifest differs -- line 28:
    sdk: '  flash_method: swd_probe'
    tan: '  firmware_path: null'
```

`tan/planner/manifest.py:93,118` writes `firmware_path` unconditionally
(`None` included); alp-sdk's `scripts/alp_orchestrate/manifest.py:92-93` emits it
only when it is not None, and says so in its own docstring ("`firmware_path` is
entirely ABSENT from …"). That is in `python/tan/planner/**`, which this branch
does not touch. **Flip `sdk_parity` to `true` once it is closed** — a blocking
gate that cannot go green is not a gate, and the 13.5-minute run time is the
smaller problem.

`TAN_RUST_BINARY` is deliberately NOT wired at all: the port intentionally
diverges from the shipped 0.4.x Rust (`--output` does not exist there), so
pinning Rust parity into the release gate would encode a comparison the port is
meant to break.

### 3.4a Re-run against `design/tan-python-port` — still not green

Re-run with `ALP_SDK_ROOT` bound to a checkout of `design/tan-python-port`
(`ac903335`, "Merge origin/main into design/tan-python-port", 0 commits behind
main): **`25 failed, 1715 passed, 209 skipped in 848.80s`** — the same 25 as
against public main, all
`test_planner_emit_parity.py::test_every_mode_is_byte_identical[...]`. The
divergence reproduces byte for byte:

```
--emit system-manifest differs -- line 28:
    sdk: '  flash_method: swd_probe'
    tan: '  firmware_path: null'
+   firmware_path: null
    flash_method: swd_probe
```

So the stale-oracle theory is not what is failing here. Both sides were checked
directly:

* the port worktree's oracle keeps the original rule —
  `scripts/alp_orchestrate/manifest.py:92-94`, `firmware_path = entry.get(...)`
  then `if firmware_path is not None:` before writing the key;
* tan still writes it unconditionally — `tan/planner/manifest.py:93` and `:118`
  in the merged tree (`93aa016`, i.e. including `9381fbc` + `729234a`).

**Verdict: `sdk_parity` cannot go green yet, and pointing it at the post-merge
worktree does not change that.** The fix has to land on the tan side; when it
does, this is a one-word edit in `release.yml` (`sdk_parity: false` → `true`) and
a re-run. Nothing was changed here to make it pass.

The original finding, for the record:

`release.yml:92-93`'s `gates` job calls `ci.yml`, and `ci.yml` runs **only**
Rust steps: `fmt`, `clippy`, `build`, `test`, MSRV `check`. There is no
`pytest` anywhere in it. The Python suite (1063 passed, 25 skipped, 4 xfailed
locally) runs in no CI job, so `gates` proves nothing about the artifact a
Python release publishes. Add a `python` job to `ci.yml` (setup-python 3.12 →
`pip install -e python` → `python -m pytest python/tests`) before shipping.

## 3.5 The two acceptance gates already on `main`

`main` carries two required checks that matter more to a Python release than
anything added here, and neither is in `ci.yml`:

| check | file | what it runs |
|---|---|---|
| `first blink -- tan bootstrap -> init -> build` | `.github/workflows/parity.yml`, job `first-blink` | `cargo build --locked --bin tan`, then `target/debug/tan bootstrap --non-interactive` → `doctor` → `init` → `build` against a pinned alp-sdk ref |
| `first install -- install.sh -> bootstrap -> init -> build` | `.github/workflows/getting-started.yml`, job `first-install` | `shellcheck --shell=sh install.sh`, then `./install.sh` for real — "a genuine GitHub Releases download of the LATEST published release", verified against that release's `checksums.txt` — then the same customer path, ending in a real Zephyr build |

They behave very differently once the Python `tan` is the shipped artifact:

* **`first install` becomes the acceptance test for the published binary, by
  itself.** It takes the LATEST release with no `--version` pin and `install.sh`
  maps Linux to musl, so the day a Python release is published this gate starts
  downloading `tan-x86_64-unknown-linux-musl`, verifying its sha256 against the
  published `checksums.txt`, and driving bootstrap → init → build with it. That
  is the build half of Target 1 proved on every PR, with no bench and nothing new
  to write. Two consequences worth planning for: the gate is only as green as the
  newest release, so a bad Python release turns a required check red repo-wide
  until the next one; and it fails closed on a missing `checksums.txt` entry, so
  the release must publish all eight assets plus checksums or this check reports
  the release broken rather than the download.
* **`first blink` does NOT follow.** It compiles `target/debug/tan` from the Rust
  crates, so it keeps testing the Rust binary no matter what the release ships.
  Repointing it at the Python `tan` (build the frozen binary, or
  `pip install -e ./python` and use the console script) is a separate maintainer
  decision — until then the two checks cover different binaries, which is worth
  knowing before reading them as one signal.

Neither needed a change here; both belong in the same picture as the
`verify-version` rewrite (§3.3) and the `ci.yml` python job (§3.4).

## 4. Measurements

### 4.1 Startup: `--onefile` unpacks on every invocation

`tan --version`, wall-clock ms, one process per row-entry:

| path | runs (ms) | min |
|---|---|---|
| Windows frozen `tan.exe --version` | 966, 904, 985, 868, 869 | **868** |
| Windows source `python -m tan --version` | 608, 418, 440, 361, 372 | **361** |
| Linux frozen `-gnu --version` (in `python:3.12-slim-bullseye`) | 324, 304, 294, 316, 295, 290 | **290** |
| Linux frozen `-musl --version` (in `python:3.12-alpine`) | 387, 366, 371, 391, 396, 382 | **366** |

A real emit, `generate --target zephyr-conf --output ./out/alp.conf --sdk-root <sdk>`:

| path | runs (ms) |
|---|---|
| Windows frozen | 929, 889, 957 |
| Windows source | 626, 564, 581 |

So on Windows the frozen binary costs **~+330 ms per invocation** against the
source path for the same work (~+500 ms on the bare `--version`), and the work
itself is only ~30-60 ms — nearly all of the frozen cost is unpack. It fits the
extension's 3 s `--version` probe with ~2 s to spare. Linux is 3x cheaper than
Windows (290-396 ms total).

### 4.2 glibc floor: 2.29, and it is set by bundled CPython, not the bootloader

`objdump -T` on the `-gnu` binary reports only the bootloader's own needs
(highest `GLIBC_2.14`), which is misleading. The real floor comes from the
bundled `libpython3.12.so.1.0`. Built on Debian 11, run on Debian 10:

```
ldd (Debian GLIBC 2.28-10+deb10u3) 2.28
[PYI-10:ERROR] Failed to load Python shared library '/tmp/_MEIClcMag/libpython3.12.so.1.0': /lib/x86_64-linux-gnu/libm.so.6: version `GLIBC_2.29' not found (required by /tmp/_MEIClcMag/libpython3.12.so.1.0)
```

On glibc 2.31 (`debian:bullseye-slim`) the same binary passes all four proofs.
`release.yml` pins the Rust floor with `zigbuild_target:
x86_64-unknown-linux-gnu.2.31`, so **building in `python:3.12-slim-bullseye`
holds that floor; building on the runner's own image would raise it to 2.39**
(ubuntu-24.04) and break every older host silently. There is no PyInstaller
equivalent of zigbuild's floor flag — the build image *is* the floor.

### 4.3 The 96-example multiplier

In alp-sdk (public `main` = `ef79eab`, and `dev`) the configure-time shell-out
is still `alp_project.py`, not `tan`:

```
17:execute_process(
18-    COMMAND ${Python3_EXECUTABLE} ${ALP_SDK_ROOT}/scripts/alp_project.py
19-            --input ${CMAKE_CURRENT_SOURCE_DIR}/board.yaml
20-            --emit zephyr-conf --core m55_hp
```

Counted across the checkout: **125** `CMakeLists.txt` shell it, for **105**
`--emit` invocations (96 `--emit zephyr-conf`, 8 `--emit ipc-contract-h`, 1
other). **No `cmake/alp.cmake` exists on public `main` or `dev`** — grep found
`cmake/alp-sdk-config.cmake.in` and `cmake/alp-sdk-warnings.cmake` only — so the
"shells `tan` on every configure" state is not in the public SDK yet. When it
lands, at the numbers above: 105 x ~0.93 s ≈ **98 s** of frozen-`tan` startup
per full sweep on Windows, against 105 x ~0.59 s ≈ 62 s from source (**+36 s**),
and ≈32 s on Linux. Per single example the delta is a third of a second.

## 5. Freeze hazards

Audited across `python/tan/**` and then checked against the built binaries:

| hazard | state |
|---|---|
| dynamic command registry | **absent by design.** All 15 commands are literal `from tan.commands.x import y` at `tan/cli.py:18-32`; `tan/commands/__init__.py:2-8` records why a `pkgutil`/`importlib` registry is a trap. No `pkgutil.iter_modules`/`walk_packages`/`__import__`/`entry_points` anywhere. |
| `importlib.util.find_spec` | one use, `doctor_cmd.py:772-779`, called only as `_has_module("fdt")` — a literal-name presence probe, never an import. |
| package data | `tan/templates/__init__.py:34` `VENDORED_ROOT = Path(__file__).resolve().parent / "vendored"`. Needs the `--add-data` at `build_binary.sh:64`; **empirically proved present** — frozen `tan init --template zephyr-app` wrote all 6 files on Windows, x86_64 gnu/musl and aarch64 gnu. Nothing else in the package reads its own `contract/` or `metadata/` (those paths hang off the external SDK root, `tan/planner_root.py`). |
| `sys.executable` | **zero uses.** `build_cmd.py:147-169` deliberately returns the PATH name `"python"`/`"python3"` because frozen, `sys.executable` is `tan` itself and the value is baked into every Zephyr slice as `-DPython3_EXECUTABLE`. |
| `sys._MEIPASS` baked into an outliving artefact | none found, and none observed: every file the frozen binary emitted (`alp.conf`, `cmake-args`) was grepped for `_MEI` — no hits. `kconfig_symbols.py:280-317` writes its generated dumper into `tempfile.mkdtemp()`, explicitly to avoid a `_MEIPASS` path landing in a CMake command that outlives the process. |
| broken pipe | `tan generate --help | grep -q --` prints `OSError: [Errno 22] Invalid argument` from the cp1252 stdout wrapper on Windows *after* the pipe closes. Cosmetic (exit status is the consumer's), but it is a traceback on stderr; `verify_binary.sh` writes help to a file instead of piping. |
| build-host requirement | PyInstaller needs `objdump`: `ERROR: On Linux, objdump is required.` — install `binutils` in both Linux images. No compiler needed: PyInstaller 6.21.0 publishes `musllinux_1_1_x86_64` and `musllinux_1_1_aarch64` wheels, so the musl bootloader is prebuilt too. |

## 6. Concrete `release.yml` changes

1. **`verify-version`** — replace the `Cargo.toml` grep with the Python
   version sources (§3.3). Keep the `npm-shim` step.
2. **`gates`** — `ci.yml` must gain a Python job, or the release ships an
   artifact no CI job ever ran (§3.4).
3. **`build`** — replace the 8-entry cargo matrix + `setup-rust-toolchain` +
   `cargo-zigbuild` steps with the per-host PyInstaller jobs in
   `.github/workflows/python-binaries.yml` (added here, `workflow_dispatch`
   only, publishes nothing). Runner mapping: `windows-latest`,
   `windows-11-arm`, `ubuntu-latest` x2 (in `python:3.12-slim-bullseye` /
   `python:3.12-alpine`), `ubuntu-24.04-arm` x2 (same images),
   `macos-15-intel`, `macos-latest`. Do **not** collapse the macOS pair onto
   `macos-latest`: it is arm64, and the result would be a correctly named
   x86_64 asset containing an arm64 binary — the extension selects by name and
   cannot detect it.
4. **`stage asset`** — `cp python/dist/tan{,.exe} <asset>` instead of
   `cp target/<triple>/release/tan<ext>`.
5. **`checksums.txt`** — unchanged. `sha256sum * > checksums.txt` inside
   `assets/` (`release.yml:241-244`) still covers whatever landed; note it
   globs **9** files today, because `envelope-contract.json` is staged into the
   same directory at `release.yml:211` before it runs.
6. **`publish_crates`** — a Python release cannot publish `tan-core`/`tan-cli`
   at the tag version (`Cargo.toml` stays `0.4.x`); gate that job off, or keep
   the Rust crate versions on their own cadence and never tag them from here.
7. **CHANGELOG** — the body slice is `^## \[<version>\]` (`release.yml:260`), so
   a `## [0.5.0]` section must exist or the release body is empty.

## 6b. Found while closing the four

1. **`python/pyproject.toml` cannot be installed** (see §0). `[project.optional-dependencies]`
   sits above the `classifiers` array, so `classifiers` parses as an extras group
   and setuptools rejects the file:
   `configuration error: 'project.optional-dependencies.classifiers[0]' must be pep508`.
   `project.classifiers` is also simply gone from the metadata. Maintainer's file;
   one table move.
## 6c. The npm-shim libc mismatch — FIXED and PINNED

`npm-shim/postinstall.js` now maps `linux/x64` → `x86_64-unknown-linux-musl` and
`linux/arm64` → `aarch64-unknown-linux-musl`, matching `install.sh:42`, the VS
Code extension, and `docs/release-contract.md`'s own "Targets published" table
(which already said musl, and already records that it once said the opposite). The
comment that claimed the three agreed now says what is true and why: the `-gnu`
assets carry a glibc floor, so an Alpine `npm i -g @alplabai/tan` downloaded a
binary that cannot execute.

The pin is `npm-shim/test/libc-mapping.test.js`, four cases on node's built-in
runner, no dependencies, wired into `ci.yml` as the `shim` job. It compares the
shim against the CONTRACT DOC rather than against install.sh, because two
consumers can agree and both be wrong about what the release publishes:

```
✔ the shim serves exactly the contract's platform -> triple mapping
✔ both Linux arches resolve to musl, never gnu
✔ install.sh maps Linux to the same musl target as the shim
✔ every asset the shim can name is one the release actually publishes
ℹ pass 4  ℹ fail 0
```

To prove the pin can actually fail, `linux/x64` was flipped back to
`x86_64-unknown-linux-gnu` — all four cases went red — then reverted:

```
✖ the shim serves exactly the contract's platform -> triple mapping
✖ both Linux arches resolve to musl, never gnu
✖ install.sh maps Linux to the same musl target as the shim
✖ every asset the shim can name is one the release actually publishes
ℹ pass 0  ℹ fail 4
```

`postinstall.js` gained a `require.main === module` guard and a `module.exports`
so the test imports the real table instead of re-parsing the file as text — a
copy of the table is exactly what this test exists to prevent.

## 6b (historical) Found while closing the four

2. **The npm shim and the extension disagree about Linux.** — now §6c.
   `npm-shim/postinstall.js:36-37` maps `linux/x64`/`linux/arm64` to
   `x86_64-unknown-linux-gnu`/`aarch64-unknown-linux-gnu`, while `install.sh:42`
   uses `unknown-linux-musl` and the extension's `TARGETS`
   (`service.ts:42-43`) uses musl too. The shim's own comment claims it is "the
   same platform -> triple table as install.sh/install.ps1 and the VS Code
   extension's releaseAssetForTarget", which is false for Linux. Consequence: an
   Alpine (musl) host installing via npm downloads a glibc binary that cannot
   run, and every npm Linux user inherits the glibc 2.29 floor the musl asset
   does not have. Left alone deliberately — changing what npm serves is a
   behaviour decision, not a blocker fix.
3. **The dirty-interpreter signal is weaker than the ceiling comment implied.**
   A build venv carrying numpy 2.5.1 produced 13516327 B against 13517584 B
   without it — no inflation, because PyInstaller bundles what the import graph
   reaches and `tan` never imports numpy. The 34349423 B figure came from
   pywin32/Pillow-class packages whose hooks collect data files regardless. Now
   recorded in `artifact_ceilings.env`, and it is the second reason
   `build_binary.sh` recommends `pip install -e .` rather than trusting size to
   notice a rich environment.
4. **`npm-shim/postinstall.js` advertised the wrong source-install path** for a
   platform with no asset: `cargo install alp-tan-cli --locked`, which will not
   resolve at 0.5.0 because the release is no longer built from the crates.
   Changed to `pip install alp-tan`.

## 7. Not proved, and the fallback

- ~~`tan-aarch64-pc-windows-msvc.exe`, `tan-x86_64-apple-darwin`,
  `tan-aarch64-apple-darwin`~~ — **now proved in CI** (§1.0), on
  `windows-11-arm`, `macos-15-intel` and `macos-latest` respectively.
- The aarch64 Linux pair was originally proved here under **QEMU**; CI has since
  built both on native `ubuntu-24.04-arm` silicon, which is what the workflow
  uses. For the record QEMU was ~12x slower (162.9 s vs 13.7 s for the same
  PyInstaller run) and is a build-host difference only — the artefact is a real
  arm64 binary either way.
- Still not proved anywhere: that any of these eight actually RUN on a customer's
  machine rather than a runner. The four proofs cover start, flag surface,
  scaffold and emit; they do not cover `tan build` driving a real Zephyr
  toolchain, which is what `first install` (§3.5) will cover automatically once a
  Python release is the latest one.
- `pip install` needs none of this machinery. `alp-tan` is not on PyPI
  (`https://pypi.org/pypi/alp-tan/json` → `404`), and once §3.1 is fixed
  `pip install alp-tan` is a working distribution path for anyone with Python
  3.12+, with no per-arch matrix, no size ceiling and no unpack cost. The eight
  binaries exist for one consumer: the VS Code extension, which downloads a raw
  binary and cannot `pip install`.
