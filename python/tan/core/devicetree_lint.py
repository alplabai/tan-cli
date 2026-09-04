# SPDX-License-Identifier: Apache-2.0
"""Will Zephyr's devicetree LINT pass run for a build on this host?

Zephyr compiles the devicetree with its own Python `gen_defines.py`; `dtc`
is not needed for that and a build with no `dtc` anywhere still links a real
`zephyr.elf`. What `dtc` is used for is a SECOND, diagnostics-only pass --
`zephyr/cmake/modules/dts.cmake` runs the compiler over the assembled `.dts`
under `if(DTC)`, above the verbatim comment *"This is just to generate
warnings and errors; we discard the output"*, with `-E unit_address_vs_reg`
and (when the binary accepts it) `-Wunique_unit_address_if_enabled`. No
`dtc`, no `if(DTC)` body, no diagnostics -- and nothing on either side says
so, because the block is skipped rather than failed.

**Why this module exists at all: tan-cli#1176/#1178 changed the premise.**
`tan bootstrap` acquires the Zephyr SDK with `--no-hosttools`
(`tan.core.toolchain_provision.NO_HOSTTOOLS_FLAG`), so the install carries no
`hosttools/` tree -- and `hosttools/` is where the SDK's own `dtc` lives
(measured on a real `zephyr-sdk-1.0.1`:
`hosttools/sysroots/x86_64-pokysdk-linux/usr/bin/dtc`, `Version: DTC v1.7.0+`,
beside `openocd`/`fdtdump`/`fdtget`/`fdtoverlay`/`fdtput`). Before #1178 the
SDK put a `dtc` in CMake's reach on every install; now it does not, so the
lint pass silently stopped running on the default path. That is the whole of
tan-cli#1192.

**This partially reverses a decision `tan.commands.doctor_cmd`'s own module
docstring records as deliberate** -- `dtc`/`gperf` are named there as the
frozen Rust oracle's `--build` check vocabulary, NOT ported. That decision was
correct when it was made and its premise is gone: it was written when `dtc`
arrived with the SDK, so a `dtc` probe could only ever restate what
`zephyrSdk` already said. `gperf` stays unported; only `dtc` is reopened here,
and only as the devicetree-lint question, never as a bare is-it-on-PATH probe.

## The two CMake mechanisms this mirrors, and why BOTH are needed

`find_program(DTC dtc)` (`zephyr/cmake/modules/FindDtc.cmake`) searches
`CMAKE_PREFIX_PATH` BEFORE the ambient `PATH`, and the Zephyr SDK's own
`cmake/zephyr/host-tools.cmake` is what puts its `hosttools/` on that prefix
list -- `list(APPEND CMAKE_PREFIX_PATH ${HOST_TOOLS_HOME}/usr)`, where
`HOST_TOOLS_HOME` is the poky sysroot on Linux, `<sdk>/hosttools` on macOS,
and on WINDOWS is never given a `dtc`-bearing prefix at all (that branch
appends only `qemu`/`qemu-arc`/`openocd`, and the Windows hosttools archive
ships neither `dtc` nor `gperf` -- `tan.core.bootstrap`'s
`manual_install_windows` prose records the `7z l` measurement). So a probe
that looked only at `PATH` would report "no lint" on a host whose SDK carries
one, which is a report that lies; [`hosttools_bin_dir`] is the other half.

`find_package(Dtc 1.4.6)` (`dts.cmake`, line 10 of the same file) is the
second mechanism and the one that makes this check worth having a `warn` arm
at all. `FindDtc.cmake` runs `dtc --version`, scrapes `Version: DTC v?X.Y.Z`
out of it, hands the result to `find_package_handle_standard_args`, and then:

    if(NOT Dtc_FOUND)
      # DTC was found but version requirement is not met, or dtc was not working.
      # Treat it as DTC was never found by resetting the result from `find_program()`
      set(DTC DTC-NOTFOUND CACHE FILEPATH "Path to a program" FORCE)
    endif()

A `dtc` that IS installed but is older than [`DTC_MIN_VERSION`], or whose
`--version` does not run or does not parse, is therefore reset to
`DTC-NOTFOUND` and the lint is skipped -- with no message. That host is the
one this module has something to say to: somebody installed `dtc` on purpose,
believes the lint runs, and it does not. A host with NO `dtc` is the ordinary,
supported, documented post-#1178 state and gets no warning at all; see
`doctor_cmd.devicetree_lint_check` for the severity argument.

Pure: no subprocess, no filesystem, no environment. `doctor_cmd` owns the IO
(`_resolve_dtc`) and builds the `Check`.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import NamedTuple

#: `zephyr/cmake/modules/dts.cmake`'s own `find_package(Dtc 1.4.6)`. A `dtc`
#: below this is reset to `DTC-NOTFOUND` by `FindDtc.cmake` -- see the module
#: docstring. Compared component-wise, `>=`, non-EXACT, exactly as CMake's
#: `find_package_handle_standard_args` compares a `VERSION_VAR`.
DTC_MIN_VERSION: tuple[int, int, int] = (1, 4, 6)

#: `FindDtc.cmake`'s own scrape, transcribed: `string(REGEX MATCH "Version:
#: DTC v?([0-9]+[.][0-9]+[.][0-9]+).*" ...)`. Deliberately NOT anchored and
#: deliberately tolerant of a trailing suffix -- the SDK's own binary answers
#: `Version: DTC v1.7.0+` (measured), whose `+` CMake drops the same way this
#: does.
_DTC_VERSION_RE = re.compile(r"Version: DTC v?([0-9]+)[.]([0-9]+)[.]([0-9]+)")

#: Where the resolved `dtc` came from. `hosttools` means the Zephyr SDK's own
#: copy, reached through the `CMAKE_PREFIX_PATH` append `host-tools.cmake`
#: does; `path` means the ambient `PATH`, which CMake searches second;
#: `absent` means neither had one, which is what a `--no-hosttools` install
#: with no distro `dtc` looks like.
ORIGIN_HOSTTOOLS = "hosttools"
ORIGIN_PATH = "path"
ORIGIN_ABSENT = "absent"


class DtcResolution(NamedTuple):
    """What a host's `dtc` situation is, in the terms CMake would resolve it.

    `version` is `None` both when there is no binary to ask and when the
    binary answered something `FindDtc.cmake`'s own regex would not match --
    the two are distinguished by `origin`, not by this field, because CMake
    does not distinguish them either: both end at `DTC-NOTFOUND`.
    """

    path: str | None
    origin: str
    version: tuple[int, int, int] | None


def parse_dtc_version(text: str | None) -> tuple[int, int, int] | None:
    """`dtc --version` output -> `(major, minor, patch)`, or `None` when
    `FindDtc.cmake`'s regex would not have matched it either."""
    if not text:
        return None
    found = _DTC_VERSION_RE.search(text)
    if found is None:
        return None
    return (int(found.group(1)), int(found.group(2)), int(found.group(3)))


def format_version(version: tuple[int, int, int]) -> str:
    return ".".join(str(part) for part in version)


def hosttools_bin_dir(sdk_install_dir: Path, host_os: str, host_arch: str) -> Path | None:
    """The directory `find_program(DTC dtc)` would find the SDK's own `dtc`
    in, given an install at `sdk_install_dir` -- or `None` on a host whose
    SDK never puts one on `CMAKE_PREFIX_PATH`.

    One branch per branch of `<sdk>/cmake/zephyr/host-tools.cmake`, whose
    `list(APPEND CMAKE_PREFIX_PATH ${HOST_TOOLS_HOME}/usr)` is what makes the
    bundle findable; `find_program` appends `bin` to a prefix itself.

    * `linux` -- `hosttools/sysroots/<arch>-pokysdk-linux/usr/bin`, the arch
      token coming from `cmake_host_system_information(... OS_PLATFORM)`, i.e.
      `uname -m`, which is the same token `doctor_cmd._host_os_arch_tags`
      already normalises (`x86_64`/`aarch64`).
    * `macos` -- `hosttools/usr/bin`; the per-component `opt/` trees are
      symlinked into that one directory by the SDK itself.
    * `windows` -- `None`, and that is a fact about the SDK, not a gap here:
      the Windows branch appends only `qemu`, `qemu-arc` and `openocd`
      prefixes, and the Windows hosttools archive ships no `dtc` to append a
      prefix for. On Windows `dtc` can only ever come from `PATH`.
    """
    if host_os == "linux":
        return (
            sdk_install_dir
            / "hosttools"
            / "sysroots"
            / f"{host_arch}-pokysdk-linux"
            / "usr"
            / "bin"
        )
    if host_os == "macos":
        return sdk_install_dir / "hosttools" / "usr" / "bin"
    return None


def lint_will_run(resolution: DtcResolution) -> bool:
    """`Dtc_FOUND`, computed the way `FindDtc.cmake` computes it: a binary was
    resolved AND its version parsed AND that version is at least
    [`DTC_MIN_VERSION`]. Anything else is the `set(DTC DTC-NOTFOUND ... FORCE)`
    reset, and `dts.cmake`'s `if(DTC)` block does not run."""
    if resolution.path is None or resolution.version is None:
        return False
    return resolution.version >= DTC_MIN_VERSION
