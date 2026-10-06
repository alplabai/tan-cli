# SPDX-License-Identifier: Apache-2.0
"""`colors`: the default console filter for `tan monitor` (tan-cli#1315).

A serial console shows bytes from a device nobody vouches for. Passing them
straight to the user's terminal lets that device write the clipboard (OSC 52),
retitle the window, and move or erase the screen. miniterm's own `default`
filter is safe but prints Zephyr's colours as literal `ESC[1;32m` text. This
filter keeps colours and nothing else, as an ALLOWLIST (a denylist of known
bad sequences loses to the next terminal quirk):

* a character is output only if it is printable (`unicodedata` category not
  in Cc, Cf, Cs, Co, Cn, Zl, Zp, which also removes bidi/format controls and
  DEL) or one of `\\r \\n \\t \\b`;
* the ONLY escape ever output is an SGR sequence the filter builds itself,
  `ESC [ <codes> m`, re-emitted canonically from the parsed codes and never
  passed through as received. Allowed codes: 0 1 2 3 4 22 23 24 27 30-37 39
  40-47 49 90-97 100-107, and 38/48 with `;5;n` or `;2;r;g;b` (0-255). Any
  other code (conceal 8, blink 5/6, ...) is dropped and the rest kept; a
  malformed 38/48 drops the remainder. The colon form (`38:2::1:2:3`) is not
  recognised, so that sequence is neutralised whole;
* every other ESC sequence (non-SGR CSI, OSC, DCS, APC, PM, SOS, charset
  selects, lone ESC), every C1 control (U+0080-U+009F; U+009B/9D/90/98/9E/9F
  start a sequence exactly like their 7-bit forms), CAN, SUB, DEL and every
  other control character becomes one visible placeholder. CAN and SUB abort
  a sequence in progress, as ECMA-48 terminals do;
* the placeholder is `·`, or `?` when the output stream cannot encode it; a
  `\\r \\n \\t \\b` following a dropped escape is still honoured;
* it is stream-safe (a sequence split across reads is buffered) and bounded:
  an over-long or unterminated sequence emits a placeholder and resyncs to
  ground state without printing the buffered payload.

Because the only escape that can reach the terminal is one the filter
generated, the terminal is always in ground state when it sees it, which
removes the filter-vs-terminal parser differential.

The class is defined by exec'ing `SOURCE`, so the same text can also be run
inside a spawned `python -c` (`BOOTSTRAP`) where `tan` is not importable
(frozen builds), and the in-process and subprocess consoles cannot drift.
"""

from __future__ import annotations

SOURCE = r'''
import sys as _sys
import unicodedata as _ud


def _placeholder():
    enc = getattr(_sys.stdout, "encoding", None) or "ascii"
    try:
        "·".encode(enc)
        return "·"
    except (UnicodeError, LookupError):
        return "?"


class ColorsFilter:
    PLACEHOLDER = "·"  # instances pick `?` when stdout cannot encode it
    _KEEP = "\r\n\t\b"
    _BAD_CATEGORIES = frozenset(["Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"])
    _SGR_SIMPLE = frozenset(
        [0, 1, 2, 3, 4, 22, 23, 24, 27, 39, 49,
         *range(30, 38), *range(40, 48), *range(90, 98), *range(100, 108)]
    )
    _MAX_PARAMS = 32
    _MAX_CSI = 64
    _MAX_STR = 4096
    _MAX_INTER = 16

    def __init__(self):
        self.PLACEHOLDER = _placeholder()
        self._state = "ground"  # ground|esc|inter|csi|str|stresc
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

    def _reset(self):
        self._state = "ground"
        self._buf = ""

    def _drop(self, out):
        out.append(self.PLACEHOLDER)
        self._reset()

    def _printable(self, ch):
        return ch in self._KEEP or _ud.category(ch) not in self._BAD_CATEGORIES

    def _sgr(self, params):
        # `params` is already known to match ^[0-9;]{0,32}$
        if params == "":
            return "\x1b[0m"
        vals = [int(p) if p else 0 for p in params.split(";")]
        kept, i = [], 0
        while i < len(vals):
            v = vals[i]
            if v in (38, 48):
                mode = vals[i + 1] if i + 1 < len(vals) else None
                if mode == 5 and i + 2 < len(vals) and vals[i + 2] <= 255:
                    kept += [v, 5, vals[i + 2]]
                    i += 3
                    continue
                if mode == 2 and i + 4 < len(vals) and max(vals[i + 2:i + 5]) <= 255:
                    kept += [v, 2, *vals[i + 2:i + 5]]
                    i += 5
                    continue
                break  # malformed extended colour: drop the remainder
            if v in self._SGR_SIMPLE:
                kept.append(v)
            i += 1
        if not kept:
            return ""
        return "\x1b[" + ";".join(str(k) for k in kept) + "m"

    def _step(self, ch, out):
        st = self._state
        o = ord(ch)
        if st == "ground":
            if ch == "\x1b":
                self._state = "esc"
            elif o == 0x9B:
                self._state, self._buf = "csi", ""
            elif o in (0x90, 0x98, 0x9D, 0x9E, 0x9F):
                self._state, self._buf = "str", ""
            elif self._printable(ch):
                out.append(ch)
            else:
                out.append(self.PLACEHOLDER)  # C0, C1, DEL, CAN, SUB, format, ...
            return
        if ch in "\x18\x1a":  # CAN / SUB abort any sequence in progress
            self._drop(out)
            return
        if st == "esc":
            if ch == "[":
                self._state, self._buf = "csi", ""
            elif ch in "]PX^_":
                self._state, self._buf = "str", ""
            elif 0x20 <= o <= 0x2F:
                self._state, self._buf = "inter", ""
            elif ch == "\x1b":
                out.append(self.PLACEHOLDER)  # ESC ESC: still in esc
            elif 0x30 <= o <= 0x7E:
                self._drop(out)  # two-byte sequence (ESC c, ESC =, ...)
            else:
                self._drop(out)
                self._step(ch, out)  # honour newline etc.; C1 starts its own sequence
        elif st == "inter":
            if 0x20 <= o <= 0x2F:
                self._buf += ch
                if len(self._buf) > self._MAX_INTER:
                    self._drop(out)
            elif 0x30 <= o <= 0x7E:
                self._drop(out)  # ESC ( B etc.
            elif ch == "\x1b":
                self._drop(out)
                self._state = "esc"
            else:
                self._drop(out)
                self._step(ch, out)
        elif st == "csi":
            if 0x20 <= o <= 0x3F:
                self._buf += ch
                if len(self._buf) > self._MAX_CSI:
                    self._drop(out)
            elif 0x40 <= o <= 0x7E:
                params = self._buf
                if (
                    ch == "m"
                    and len(params) <= self._MAX_PARAMS
                    and all(c in "0123456789;" for c in params)
                ):
                    out.append(self._sgr(params))
                    self._reset()
                else:
                    self._drop(out)
            elif ch == "\x1b":
                self._drop(out)
                self._state = "esc"
            else:
                self._drop(out)  # any other control or non-ASCII aborts it
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
            if ch == "\\":
                self._drop(out)  # ESC \ = ST
            else:
                self._drop(out)  # anything else aborts the string...
                self._state = "esc"  # ...and the ESC starts a new sequence
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
