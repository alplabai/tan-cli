# SPDX-License-Identifier: Apache-2.0
"""`colors`: the default console filter for `tan monitor` (tan-cli#1315).

A serial console shows bytes from a device nobody vouches for. Passing them
straight to the user's terminal lets that device write the clipboard (OSC 52),
retitle the window, and move or erase the screen. miniterm's own `default`
filter is safe but prints Zephyr's colours as literal `ESC[1;32m` text. This
filter keeps colours and nothing else:

* SGR sequences `ESC [ <digits and ;> m` pass through unchanged;
* `\\r \\n \\t \\b` pass through;
* every other escape sequence (CSI that is not SGR, OSC, DCS, APC, PM, SOS,
  charset selects, lone ESC), the 8-bit C1 controls, and every other control
  character is replaced by one visible placeholder.

It is stream-safe: a sequence split across reads is buffered until complete.

The class is defined by exec'ing `SOURCE`, so the same text can also be run
inside a spawned `python -c` (`BOOTSTRAP`) where `tan` is not importable
(frozen builds), and the in-process and subprocess consoles cannot drift.
"""

from __future__ import annotations

SOURCE = r'''
class ColorsFilter:
    PLACEHOLDER = "·"
    _KEEP = "\r\n\t\b"
    _MAX_CSI = 64
    _MAX_STR = 4096

    def __init__(self):
        self._state = "normal"  # normal|esc|inter|csi|str|stresc
        self._buf = ""

    def tx(self, text):
        return text

    def echo(self, text):
        return text

    def rx(self, text):
        out = []
        for ch in text:
            self._step(ch, out)
        return "".join(out)

    def _drop(self, out):
        out.append(self.PLACEHOLDER)
        self._state = "normal"
        self._buf = ""

    def _step(self, ch, out):
        st = self._state
        o = ord(ch)
        if st == "normal":
            if ch == "\x1b":
                self._state = "esc"
            elif o == 0x9B:
                self._state, self._buf = "csi", ""
            elif o in (0x90, 0x98, 0x9D, 0x9E, 0x9F):
                self._state, self._buf = "str", ""
            elif ch in self._KEEP or (o >= 0x20 and o != 0x7F and not 0x80 <= o <= 0x9F):
                out.append(ch)
            else:
                out.append(self.PLACEHOLDER)
        elif st == "esc":
            if ch == "[":
                self._state, self._buf = "csi", ""
            elif ch in "]PX^_":
                self._state, self._buf = "str", ""
            elif 0x20 <= o <= 0x2F:
                self._state = "inter"
            elif ch == "\x1b":
                out.append(self.PLACEHOLDER)  # ESC ESC: stay in esc
            else:
                self._drop(out)  # two-byte sequence (ESC c, ESC =, ...)
        elif st == "inter":
            if 0x20 <= o <= 0x2F:
                pass
            else:
                self._drop(out)  # ESC ( B etc.: final byte consumed
                if o < 0x20 or ch == "\x1b":
                    self._step(ch, out)
        elif st == "csi":
            if 0x30 <= o <= 0x3F or 0x20 <= o <= 0x2F:
                self._buf += ch
                if len(self._buf) > self._MAX_CSI:
                    self._drop(out)
            elif 0x40 <= o <= 0x7E:
                params = self._buf
                if ch == "m" and all(c in "0123456789;" for c in params):
                    out.append("\x1b[" + params + "m")
                    self._state, self._buf = "normal", ""
                else:
                    self._drop(out)
            else:
                self._drop(out)  # control char inside CSI aborts it
                self._step(ch, out)
        elif st == "str":
            if ch == "\x07" or o == 0x9C:
                self._drop(out)
            elif ch == "\x1b":
                self._state = "stresc"
            else:
                self._buf += ch
                if len(self._buf) > self._MAX_STR:
                    self._drop(out)
        elif st == "stresc":
            self._drop(out)  # ST (ESC \\) ends the string; anything else aborts it
            if ch != "\\":
                self._state = "esc"  # ...and starts a new escape sequence
                if ch != "\x1b":
                    self._step(ch, out)
'''

#: Run by `python -c` for the spawned console: define the filter, register it
#: in miniterm's own table, hand over to miniterm's `main()` (argv follows).
BOOTSTRAP = (
    SOURCE
    + "\nfrom serial.tools import miniterm as _m\n"
    + "_m.TRANSFORMATIONS['colors'] = ColorsFilter\n"
    + "_m.main()\n"
)

_ns: dict = {}
exec(compile(SOURCE, "<tan.core.console_filter.SOURCE>", "exec"), _ns)  # noqa: S102
ColorsFilter = _ns["ColorsFilter"]
