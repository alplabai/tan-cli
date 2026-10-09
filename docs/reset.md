<!-- SPDX-License-Identifier: Apache-2.0 -->
# `tan reset`: one bare nRESET pulse (tan-cli#1452)

```sh
tan reset [--pulse-ms 100] [--probe-usb-path 3-4.2 | --probe-serial SN] [--jlink PATH] \
          [--device Cortex-M55] [--speed 4000] [--format json]
JLINK_RUN_PLACE=aen-evk-02 tan reset --probe-usb-path 3-4.2
```

The recovery from a non-waking STOP on the AEN bench is one clean pin pulse.
`tan reset` sends exactly this J-Link Commander session and nothing else:

```
SelectEmuBySN <serial>      (only when a probe is selected)
si SWD / speed / device / connect
r0
Sleep <pulse-ms>
r1
exit
```

It is **not** the pin reset `tan flash` runs (`RSetType 2; r; g`, retried): no
halt, no `RSetType`, no `g`, no connect-under-reset, no retry. Nothing is
written, erased or flashed. A test holds the generated script to that verb list.

* `--pulse-ms` is how long nRESET is held low: default 100, range 1 to 10000
  (outside it is `reset.bad-argument`, exit 2, nothing spawned).
* Probe selection, the trusted J-Link binary and the `ShowEmuList` check before
  the spawn are `tan flash`'s: `--probe-usb-path` / `--probe-serial` pick the
  probe, `TAN_PROBE_USB_PATH` is exported to the spawn, and a refusal reuses
  `flash.probe-ambiguous` / `-not-found` / `-selector-conflict` / `-verify-failed`.
* `JLINK_RUN_PLACE` is read from the environment of the invocation and inherited
  by the `JLinkExe` spawn, so a board-farm shim picks the place per command, as
  for `tan flash`. The envelope reports it in `data.place` (`null` when unset).
* Envelope `data`: `pulseMs`, `place`, `writes` (always `false`), `jlink`
  (`binary`, `binarySource`, `device`), `probe` (selection echo), `script`
  (the exact lines sent). Success needs the Commander's
  `Script processing completed.`; otherwise `reset.failed` (exit 1).
* `reset.internal-failure` (exit 5) is a tan bug.
