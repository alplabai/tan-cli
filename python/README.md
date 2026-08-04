# tan

The Alp Lab build CLI. One `board.yaml` describes a whole module — every core,
MCU and MPU alike — and `tan` turns it into firmware.

## Install

Install from a checkout. `tan` is **not on PyPI** — the `alp-tan` distribution
name is reserved in `pyproject.toml` and has never been published, so
`pip install alp-tan` answers `ERROR: No matching distribution found`
(`https://pypi.org/pypi/alp-tan/json` → `404`). The release workflow has no
PyPI job: publishing there is a separate decision nobody has taken
(`.github/workflows/release.yml`).

```bash
git clone https://github.com/alplabai/tan-cli && cd tan-cli
python3 -m pip install ./python     # Linux / macOS
py -m pip install ./python          # Windows
```

```bash
tan --version
tan init            # scaffold a project (add --preview to see the plan first)
tan doctor          # is this host able to build and flash?
tan build           # build every core the module declares
```

Add the `monitor` extra — `pip install "./python[monitor]"` — if you want `tan
monitor`; it needs pyserial, which a default install leaves out.

**Use `-m pip`, not a bare `pip` or `pip3`.** `tan` requires Python **3.12 or
newer**, and on a machine with several interpreters a bare `pip` can install into
a different one than the `python3` you will actually build with — on Windows,
`python3` is frequently the Microsoft Store stub rather than a real interpreter.
`python3 -m pip` cannot drift from the interpreter that runs it, so the
`requires-python = ">=3.12"` check either passes honestly or refuses that
interpreter, instead of installing somewhere you will not find it. (An old
`setuptools` on the wrong interpreter can fail earlier and for a different
reason — `project.license` must be valid exactly by one definition, measured
here on setuptools 68-era metadata handling; that is tan-cli#382, not a version
mismatch.)

**Not building from source?** Every version tag publishes a prebuilt archive per
platform, and `install.sh` / `install.ps1` fetch and verify one for you — see
the root [`README.md`](../README.md#install). That is the supported path for
someone who just wants the command.

`tan init` needs no alp-sdk checkout: the scaffold templates ship inside `tan`.

The distribution is named `alp-tan` rather than `tan` because `tan` is already
taken on PyPI (an unrelated code formatter); the command you run is still `tan`.
Reserving the name is not the same as occupying it — nothing is uploaded under
either.

## What it does

`tan` is the executor: it consumes a build plan produced from your `board.yaml`
and drives the right tool for each core — `west` for Zephyr on Cortex-M,
`bitbake` for Yocto on Cortex-A, `cmake` for bare metal. You never pick an OS or
a toolchain; both are derived from each core's architecture.

## Requirements

Python 3.12 or newer — the floor Zephyr's own CMake configure enforces. Run
`tan doctor` to check the rest of the host; it names what is missing and where
to get it.

## Licence

Apache-2.0.
