# tan

The Alp Lab build CLI. One `board.yaml` describes a whole module — every core,
MCU and MPU alike — and `tan` turns it into firmware.

```bash
pip install alp-tan
tan --version
tan doctor          # is this host able to build and flash?
tan build           # build every core the module declares
```

The distribution is named `alp-tan` because `tan` is taken on PyPI; the command
you run is still `tan`.

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
