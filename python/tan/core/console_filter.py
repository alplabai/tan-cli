# SPDX-License-Identifier: Apache-2.0
"""`colors`: the default console filter for `tan monitor` (tan-cli#1315).

A serial console shows bytes from a device nobody vouches for. Passing them
straight to the user's terminal lets that device write the clipboard (OSC 52),
retitle the window, and move or erase the screen. miniterm's own `default`
filter is safe but prints Zephyr's colours as literal `ESC[1;32m` text. This
filter keeps colours and nothing else:

* SGR sequences `ESC [ <digits and ;> m` pass through, minus the conceal (8)
  and blink (5, 6) attributes, which would let a device hide or flash text
  (the rest of the sequence is kept; 38/48/58 extended-colour operands are
  never mistaken for attributes). The colon form (`ESC [ 38:2::1:2:3 m`) is
  dropped, not translated;
* `\\r \\n \\t \\b` pass through;
* every other escape sequence (CSI that is not SGR, OSC, DCS, APC, PM, SOS,
  charset selects, lone ESC), the 8-bit C1 controls, and every other control
  character is replaced by one visible placeholder. So are bidi/format
  controls that can reorder or hide text (U+202A-202E, U+2066-2069,
  U+2028/2029, U+200B-200F, U+061C, U+FEFF). The placeholder is `·`, or `?`
  when the output stream cannot encode it. After a dropped escape, a
  following `\r \n \t \b` is still honoured (a stray ESC never eats a newline).

It is stream-safe: a sequence split across reads is buffered until complete.

The class is defined by exec'ing `SOURCE`, so the same text can also be run
inside a spawned `python -c` (`BOOTSTRAP`) where `tan` is not importable
(frozen builds), and the in-process and subprocess consoles cannot drift.
"""

from __future__ import annotations

SOURCE = r'''
import sys as _sys


def _placeholder():
    enc = getattr(_sys.stdout, "encoding", None) or "ascii"
    try:
        "\u00b7".encode(enc)
        return "\u00b7"
    except (UnicodeError, LookupError):
        return "?"


class ColorsFilter:
    PLACEHOLDER = "\u00b7"  # instances pick `?` when stdout cannot encode it
    _BIDI = frozenset(
        [*range(0x202A, 0x202F), *range(0x2066, 0x206A), 0x2028, 0x2029,
         *range(0x200B, 0x2010), 0x061C, 0xFEFF]
    )
    _KEEP = "\r\n\t\b"
    _MAX_CSI = 64
    _MAX_STR = 4096

    def __init__(self):
        self.PLACEHOLDER = _placeholder()
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

    def _sgr(self, params):
        if not params:
            return "\x1b[m"
        kept, parts, i = [], params.split(";"), 0
        while i < len(parts):
            p = parts[i]
            n = int(p) if p else 0
            if n in (38, 48, 58) and i + 1 < len(parts):
                width = {"5": 3, "2": 5}.get(parts[i + 1], 1)
                kept += parts[i : i + width]
                i += width
                continue
            if n not in (5, 6, 8):
                kept.append(p)
            i += 1
        return "\x1b[" + ";".join(kept) + "m" if kept else ""

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
            elif ch in self._KEEP or (
                o >= 0x20 and o != 0x7F and not 0x80 <= o <= 0x9F and o not in self._BIDI
            ):
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
                if o < 0x20:
                    self._step(ch, out)  # ...but a newline etc. is still honoured
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
                    out.append(self._sgr(params))
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
