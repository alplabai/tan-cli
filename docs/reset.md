<!-- SPDX-License-Identifier: Apache-2.0 -->
# `tan reset`: one bare nRESET pulse (tan-cli#1452)

```sh
tan reset [--pulse-ms 100] [--probe-usb-path 3-4.2 | --probe-serial SN] [--jlink PATH] \
          [--confirm-console PORT --expect REGEX [--confirm-baud 115200] [--confirm-timeout 30] [--confirm-window 3]] \
          [--format json]
JLINK_RUN_PLACE=aen-evk-02 tan reset --probe-usb-path 3-4.2
```

The recovery from a non-waking STOP on the AEN bench is one clean pin pulse.
`tan reset` sends exactly this J-Link Commander session and nothing else:

```
SelectEmuBySN <serial>      (only when a probe is selected)
r0
sleep <pulse-ms>
r1
q
```

JLinkExe is started as `JLinkExe -NoGui 1 -ExitOnError 1 -CommanderScript <file>` (the argv
of the bench-proven invocation): no `-if`, no `-autoconnect`, no `-device`, no `-speed`, no
`connect`. Commander toggles the reset pin without
attaching to the target; a `connect` against an Alif target in STOP either fails
(the debug domain is gated) or disturbs the evidence this verb exists to keep.
The script matches the bench-proven one (`r0`, `sleep 100`, `r1`, `q`).

It is **not** the pin reset `tan flash` runs (`RSetType 2; r; g`, retried): no
`connect`, no halt, no `RSetType`, no `g`, no connect-under-reset, no retry. Nothing is
written, erased or flashed. A test holds the generated script to that verb list.

* `--pulse-ms` is how long nRESET is held low: default 100, range 1 to 10000
  (outside it is `reset.bad-argument`, exit 2, nothing spawned).
* Probe selection, the trusted J-Link binary and the `ShowEmuList` check before
  the spawn are `tan flash`'s: `--probe-usb-path` / `--probe-serial` pick the
  probe, `TAN_PROBE_USB_PATH` is exported to the spawn, and a refusal reuses
  `flash.probe-ambiguous` / `-not-found` / `-selector-conflict` / `-verify-failed`.
* `JLINK_RUN_PLACE` is read from the environment of the invocation and inherited
  by the `JLinkExe` spawn (always pair it with `--probe-usb-path`, so the place's
  probe is the one verified), so a board-farm shim picks the place per command, as
  for `tan flash`. The envelope reports it in `data.place` (`null` when unset).
* Envelope `data`: `pulseMs`, `place`, `writes` (always `false`), `jlink`
  (`binary`, `binarySource`), `probe` (selection echo), `script`
  (the exact lines sent). Success needs the Commander's
  `Script processing completed.` and none of `FAILED`, `Cannot connect`,
  `Could not open` in the output; otherwise `reset.failed` (exit 1).
* `reset.internal-failure` (exit 5) is a tan bug.

## One spawn, and what it proves

* With `JLINK_RUN_PLACE` set and `--probe-usb-path` given, `tan reset` makes ONE
  J-Link spawn (`data.singleSpawn: true`) and does not run the `ShowEmuList`
  verification pass first: each pass through the board-farm wrapper costs tens of
  seconds, longer than a STOP window. The wrapper itself refuses a
  `TAN_PROBE_USB_PATH` that is not its place's port (exit 96) before it opens any
  probe, and tan checks afterwards that the output carries
  `TAN_PROBE_ISOLATED_USB_PATH=<that path>` (`data.probe.isolation:
  wrapper-attested:<path>`); a missing or different handshake is
  `flash.probe-verify-failed`, saying the pulse already ran. In that mode the
  script has no `SelectEmuBySN` line (the wrapper's mask leaves one emulator).
  Without a place, the `tan flash` guard runs first as before.
* Exit 0 means **the pulse was sent**, not that the board rebooted (a target in
  STOP can ignore it). `data.resetObserved` is `"unknown"` and the info issue
  `reset.boot-not-confirmed` says so, unless you pass `--confirm-console PORT
  --expect REGEX`: the console (a local tty or `rfc2217://`) is opened before the
  pulse; after J-Link exits its queue is dropped and it is read for up to
  `--confirm-timeout`. A matching line gives `resetObserved: true` and
  `data.console`; none gives `reset.boot-not-observed` (exit 1). Only bytes
  arriving after J-Link exits count, so pick a banner printed some time after
  reset. `--confirm-console` needs pyserial: install `tan-cli[monitor]`
  (`pip install -e "./python[monitor]"` for a checkout; a bare editable install
  lacks it and the error says so).

## Confirmation window, and what an nRESET pulse cannot do

* The FIRST `--expect` match must arrive within `--confirm-window` (default 3 s)
  of J-Link exiting; `data.console.matchLatencySeconds` records it. A later match
  is **not** a reset: it is recorded as `data.console.lateMatchAtSeconds` and the
  run is `reset.boot-not-observed` (exit 1). `--confirm-timeout` is only the
  overall read limit. `--confirm-console` is validated up front: a malformed URL
  (empty or non-numeric port, no host, unknown scheme) is `reset.bad-port`, exit 2.
* **Hardware fact:** on an Alif E8 in a correctly configured STOP, an nRESET pulse
  does not reboot the SoC; use a power cycle. Bench evidence (alp-sdk #2798 U8,
  e1m-aen-evk-02, 2026-10-09): the raw bench script `r0`, `sleep 100`, `r1`, `q`
  behaves the same, and a 30 s confirm window falsely accepted the RV-3028
  alarm-wake banner that arrived 9.4 s after J-Link exit, which is why the window
  exists.
* `data.timing` records `prepSeconds` and `jlinkSpawnSeconds`. Under the board-farm
  wrapper the spawn includes a 39-50 s preamble before the pulse, longer than a
  STOP window; that latency is the wrapper's, not tan's.
* Under a wrapper the script relies on the wrapper's USB mask (and the
  `TAN_PROBE_ISOLATED_USB_PATH` handshake) instead of a `SelectEmuBySN` line.
