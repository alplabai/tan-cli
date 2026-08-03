# tan

The Alp Lab build CLI. One `board.yaml` describes a whole module — every core,
MCU and MPU alike — and `tan` turns it into firmware.

## Install

`alp-tan` is not published on PyPI yet. Use the repository's signed GitHub
release assets/installers (see the [top-level README](../README.md)), or install
from a checkout:

```bash
git clone https://github.com/alplabai/tan-cli
cd tan-cli
python3 -m pip install ./python       # Linux / macOS
py -3.12 -m pip install .\python      # Windows
```

```bash
tan --version
tan init            # scaffold a project (add --preview to see the plan first)
tan doctor          # is this host able to build and flash?
tan build           # build every core the module declares
```

**Use `-m pip`, not a bare `pip` or `pip3`.** `tan` requires Python **3.12 or
newer**, and on a machine with several interpreters a bare `pip` can install into
a different one than the `python3` you will actually build with — on Windows,
`python3` is frequently the Microsoft Store stub rather than a real interpreter.
`python3 -m pip` cannot drift from the interpreter that runs it, so the version
check either passes honestly or fails with `Package 'alp-tan' requires a
different Python`, instead of installing somewhere you will not find it.

`tan init` needs no alp-sdk checkout: the scaffold templates ship inside `tan`.
Most planning/build commands do need a checkout for metadata, schemas, examples,
and tooling; pass it with `--sdk-root` or place it beside the project.

The package metadata uses the distribution name `alp-tan` because `tan` is
already taken on PyPI (an unrelated code formatter); the command you run is
still `tan`. That naming reservation does not mean a PyPI release exists.

## What it does

`tan` contains both the relocated planner and the executor. It produces a build
plan from your `board.yaml` plus alp-sdk metadata, then drives the right tool for
each core — `west` for Zephyr on Cortex-M, `bitbake` for Yocto on Cortex-A,
`cmake` for bare metal. You never pick an OS or a toolchain; both are derived
from each core's architecture.

## Requirements

Python 3.12 or newer — the floor Zephyr's own CMake configure enforces. Run
`tan doctor` to check the rest of the host; it names what is missing and where
to get it.

## Licence

Apache-2.0.
