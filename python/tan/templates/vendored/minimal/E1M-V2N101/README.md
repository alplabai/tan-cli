# hello-world

The canonical "first program" for the Alp SDK.  No peripherals,
no chips, no board-specific wiring -- just a periodic `printf`
loop so you can confirm the toolchain, flash flow, and log
console are wired correctly before chasing harder bugs.

If `[hello] tick 0` doesn't appear, the suspect list is short:

1.  **Toolchain** -- the build output completes cleanly but the
    image you flashed doesn't match the SoM (cross-compiled for
    the wrong core; mismatched Zephyr board target).
2.  **Flash flow** -- the image flashed but boot vector / SAU /
    secure-state config doesn't allow it to start.
3.  **Console wiring** -- the app is running but your terminal is
    on the wrong UART, wrong baud rate, or wrong USB enumeration.

Pin down which one BEFORE moving on to alp-sdk's
[`examples/peripheral-io/gpio-button-led`](https://github.com/alplabai/alp-sdk/tree/v0.18.0/examples/peripheral-io/gpio-button-led)
or
[`examples/peripheral-io/i2c-scanner`](https://github.com/alplabai/alp-sdk/tree/v0.18.0/examples/peripheral-io/i2c-scanner)
-- neither is part of this scaffolded project.

## What this shows

* The minimal `board.yaml` shape: `som.sku`, `preset`, one
  `cores.m55_hp` with an empty `peripherals: []`.
* That `prj.conf` stays empty -- all CONFIG_* selection comes from
  the loader-generated `alp.conf`.
* The Zephyr boot -> `main()` -> printf path with no peripheral
  initialisation in between.

## Build

From this directory, or from your own copy of it:

```bash
tan build
tan flash
```

To start your own copy: `tan init --from-example peripheral-io/hello-world`.

`tan` builds one image per Zephyr core on the SoM: a core this `board.yaml` does not declare gets the SDK's idle `alp-stock-shim` image (on an E1M-AEN SoM, `m55_he` here, in `build/m55_he-zephyr/`); see [docs/heterogeneous-builds.md](https://github.com/alplabai/alp-sdk/blob/v0.18.0/docs/heterogeneous-builds.md).

### Inside an SDK checkout (twister / CI)

These commands only work from an alp-sdk checkout, because `scripts/gen_example_alp_conf.py` writes `generated/alp.conf` there.

```bash
# Standalone, native_sim (host binary; no hardware needed):
# writes ./generated/alp.conf, which west reads below (#866)
tan generate --target zephyr-conf --core m33_sm --sdk-root "$ALP_SDK_ROOT" --output generated/alp.conf
west build -b native_sim/native/64 . \
    -- -DEXTRA_CONF_FILE=generated/alp.conf -DEXTRA_ZEPHYR_MODULES=$ALP_SDK_ROOT
west build -t run

# On real silicon, point -b at the SoM's Zephyr board target.
# Example for E1M-V2N101:
west build -b alp_e1m_v2n101_m33_sm/r9a09g056n48gbg/cm33 . -- -DEXTRA_CONF_FILE=generated/alp.conf
west flash --host <board-ip>
```

## Where the output lands

| SoM family   | Console route                                            |
|--------------|----------------------------------------------------------|
| E1M-AEN      | Alif first USART -> FTDI USB-UART on E1M-EVK (115200 8N1) |
| E1M-V2N      | Renesas SCIF -> FTDI USB-UART on E1M-X-EVK (115200 8N1)   |
| E1M-V2M      | Same as V2N                                              |
| native_sim   | Host binary stdout                                       |

Open the serial port with `tan monitor --port <port>` (115200 8N1):

```bash
tan monitor --port <your-serial-device>
```

Any terminal works too; `tio` is a good cross-platform one -- one
binary, one syntax on Linux, macOS, and Windows:

```
tio -b 115200 <your-serial-device>
```

Per-OS device naming (and the alternative terminals each OS
prefers) is documented in
[`docs/cross-platform-setup.md`](https://github.com/alplabai/alp-sdk/blob/v0.18.0/docs/cross-platform-setup.md)
section 7.7.

<!-- cross-platform-lint:ignore -->
Quick reference: Linux uses `/dev/ttyUSB*` or `/dev/ttyACM*`;
macOS uses `/dev/cu.usbserial-*` or `/dev/cu.usbmodem*`; Windows
uses `COM<N>` (visible in Device Manager under "Ports (COM &
LPT)").
<!-- cross-platform-lint:resume -->

## Expected output

```
[hello] Alp SDK hello-world starting
[hello] tick 0
[hello] tick 1
[hello] tick 2
[hello] tick 3
[hello] tick 4
[hello] done
```

## Customising

* **Loop forever.**  Swap the bounded `for` loop in `src/main.c`
  for the commented-out `TICKS_ON_REAL_SILICON` block.
* **Faster heartbeat.**  Drop `HELLO_TICK_PERIOD_MS` from `1000`
  to `100` (10 Hz) or `10` (100 Hz).  At <= 1 ms you'll start to
  see scheduler overhead.
* **Different log macro.**  Swap `printf` for `printk` (always
  ISR-safe) or Zephyr's `LOG_INF` (compile-time-filtered,
  requires `CONFIG_LOG=y` in `prj.conf`).

## Reference

- [`docs/firmware-quickstart.md`](https://github.com/alplabai/alp-sdk/blob/v0.18.0/docs/firmware-quickstart.md)
  -- single-OS bring-up walk-through.
- [`docs/cross-platform-setup.md`](https://github.com/alplabai/alp-sdk/blob/v0.18.0/docs/cross-platform-setup.md)
  -- Windows/macOS/Linux toolchain + serial-port notes.
- [`docs/troubleshooting.md`](https://github.com/alplabai/alp-sdk/blob/v0.18.0/docs/troubleshooting.md)
  -- common boot/flash/console failures + fixes.
