<!-- SPDX-License-Identifier: Apache-2.0 -->
# `tan monitor --capture`: input and baud change (tan-cli#1451)

`--capture` records the console headlessly (no terminal needed, works over
`rfc2217://` and `socket://` URLs). Two option groups let a bench session act
on the console while it records.

```sh
tan monitor --port rfc2217://gw:4001 --capture --duration 20 --log run.log \
  --send v --send m --send-on 'Press a key'
tan monitor --port rfc2217://gw:4001 --baud 115200 --capture --duration 60 --log wake.log \
  --reopen-at 23040 --on 'entering STOP'
```

## Sending input

* `--send TEXT` (repeatable) queues console input. Escapes `\xNN`, `\r`, `\n`,
  `\t` and `\\` are allowed; no newline is added, so single-key knobs (`v`, `m`,
  `l`, `s`) go out as exactly that one byte.
* All `--send` items leave as one ordered burst, `--send-gap SECONDS` apart
  (default 0.2). The burst goes out at the start of the capture, or:
  * `--send-after SECONDS` sends it that long after the capture starts, or
  * `--send-on REGEX` sends it when the first line (or unterminated partial
    line, e.g. a prompt) matching the regex arrives.

  The two are mutually exclusive; a second burst needs a second run.
* A write that fails or stalls (bounded to 1 s on `rfc2217://`) is
  `monitor.capture-send-failed` (exit 1).
* There is no interactive tee mode: the interactive console (no `--capture`)
  already sends keystrokes, but it cannot log to a file.

## Changing baud mid-capture

`--reopen-at BAUD --on REGEX` closes the port and reopens it at `BAUD` on the
first line matching `REGEX`, then keeps capturing into the same `--log`. The
use case is an app UART that comes up at about 23040 instead of 115200 after a
low-power wake. A failed reopen is `monitor.capture-reopen-failed` (exit 1).
The reopen happens at most once per run. Bytes the old port delivered after the
matching line are not recovered.

## Envelope

When any of these options is given, `data.capture.actions` is added:
`events` (`{action: "send", atSeconds, items, bytes}` and
`{action: "reopen", atSeconds, baud, matchedLine}`), `sendsPending` (0 once
sent) and `reopenPending` (true when the pattern never matched). All of the
options need `--capture`; a bad combination is `monitor.capture-bad-option`
(exit 2).
