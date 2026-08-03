# SPDX-License-Identifier: Apache-2.0
"""`tan.core.renode_sim` unit tests -- ported from
`crates/tan-core/src/renode/sim.rs`'s own `#[cfg(test)]` module, which is the
oracle for every case here. Several of these were additionally cross-checked
live: driving the shipped `tan.exe` oracle's `--sim-mode` control socket over
a real TCP connection produced the exact same replies pinned below (a
`WriteBytes` -> `ok`, a `ReadBytes` -> lowercase space-separated `0xnn`
tokens, a bare command -> `ok`)."""
from __future__ import annotations

import pytest

from tan.core.renode_sim import (
    SimError,
    MonitorLine,
    build_sim_descriptor,
    build_sim_renode_argv,
    build_sim_resc_text,
    classify_monitor_line,
    dispatch_control_line,
    normalize_readbytes_output,
    parse_int_auto,
    ready_marker,
    sim_profile_deferred_message,
    translate_control_command,
)


# ── sim-descriptor.json ──────────────────────────────────────────────────────


def test_descriptor_has_exactly_the_four_schema_keys_in_order():
    d = build_sim_descriptor(40001, 40002)
    assert list(d.keys()) == ["control_socket", "uart_socket", "framebuffers", "peripherals"]
    assert d["control_socket"] == "tcp://127.0.0.1:40001"
    assert d["uart_socket"] == "tcp://127.0.0.1:40002"
    assert d["framebuffers"] == []
    assert d["peripherals"] == []


def test_descriptor_serialises_with_the_socket_uri_scheme():
    import json

    text = json.dumps(build_sim_descriptor(1, 65535))
    assert '"tcp://127.0.0.1:1"' in text
    assert '"tcp://127.0.0.1:65535"' in text


# ── generated boot script + argv ─────────────────────────────────────────────


def test_resc_boots_headless_platform_elf_start():
    text = build_sim_resc_text("/m/p.repl", "/b/fw.elf", None)
    assert 'mach create "v2n_sim"' in text
    assert "machine LoadPlatformDescription @/m/p.repl" in text
    assert "sysbus LoadELF @/b/fw.elf" in text
    assert text.rstrip("\n").endswith("start")
    # Deferred half: no wired-UART socket terminal yet.
    assert "CreateServerSocketTerminal" not in text
    assert "connector Connect" not in text
    # No vtor -> no write at all (Renode keeps its own guess).
    assert "0xE000ED08" not in text
    # The generated script NEVER includes the SDK's own `.resc` (the plain
    # path's `i @...`).
    assert "i @" not in text


def test_resc_seeds_the_secure_vtor_after_loadelf_and_before_start():
    text = build_sim_resc_text("p.repl", "fw.elf", 0x0800_3000)
    assert "sysbus WriteDoubleWord 0xE000ED08 0x8003000" in text
    load = text.find("LoadELF")
    vtor = text.find("0xE000ED08")
    start = text.rfind("start")
    assert load < vtor < start


def test_sim_argv_is_the_exact_headless_contract():
    argv = build_sim_renode_argv("/opt/renode/renode", "/b/.sim-boot.resc")
    assert argv == [
        "/opt/renode/renode",
        "--disable-xwt",
        "--plain",
        "--console",
        "-e",
        "i @/b/.sim-boot.resc",
    ]
    assert "--hide-monitor" not in argv


# ── ReadBytes normalisation ───────────────────────────────────────────────────


def test_normalize_readbytes_lowercases_and_flattens_the_bracketed_list():
    out = "[\n0xDE, 0xAD, 0xBE, 0xEF, \n]\n"
    assert normalize_readbytes_output(out, 4) == "0xde 0xad 0xbe 0xef"


def test_normalize_readbytes_ignores_the_echoed_command_address():
    # Regression: the echoed `sysbus ReadBytes 0x20000000 4` line carries
    # 0x20000000, which masks to 0x00 -- it must NOT leak in as a byte.
    out = "sysbus ReadBytes 0x20000000 4\n[\n0xDE, 0xAD, 0xBE, 0xEF, \n]\n"
    assert normalize_readbytes_output(out, 4) == "0xde 0xad 0xbe 0xef"


def test_normalize_readbytes_short_read_is_an_error_never_padded():
    with pytest.raises(SimError) as excinfo:
        normalize_readbytes_output("[ 0x01, 0x02, ]", 4)
    assert "expected 4" in str(excinfo.value)


def test_normalize_readbytes_masks_wide_tokens_to_their_low_byte():
    assert (
        normalize_readbytes_output("[ 0xDEAD, 0x5, 0x1234567890ABCDEF12 ]", 3)
        == "0xad 0x05 0x12"
    )


def test_normalize_readbytes_falls_back_to_the_whole_output_without_brackets():
    assert normalize_readbytes_output("0x41 0x42", 2) == "0x41 0x42"


# ── control-line translation (the three verbs) ───────────────────────────────


def test_translate_readbytes_forwards_verbatim_and_carries_the_count():
    count, cmds = translate_control_command("sysbus ReadBytes 0x1000 8")
    assert count == 8
    assert cmds == ["sysbus ReadBytes 0x1000 8"]


def test_translate_writebytes_expands_to_ordered_lowercase_writebyte():
    count, cmds = translate_control_command("sysbus WriteBytes 0x20000000 0xde 0xad 0xbe 0xef")
    assert count is None
    assert cmds == [
        "sysbus WriteByte 0x20000000 0xde",
        "sysbus WriteByte 0x20000001 0xad",
        "sysbus WriteByte 0x20000002 0xbe",
        "sysbus WriteByte 0x20000003 0xef",
    ]


def test_translate_writebytes_masks_oversized_bytes():
    _count, cmds = translate_control_command("sysbus WriteBytes 0x100 0x1de 256")
    assert cmds == ["sysbus WriteByte 0x100 0xde", "sysbus WriteByte 0x101 0x0"]


def test_translate_rejects_a_writebytes_with_no_data():
    with pytest.raises(SimError, match="no data bytes"):
        translate_control_command("sysbus WriteBytes 0x20000000")


def test_translate_rejects_malformed_bases_and_counts():
    with pytest.raises(SimError, match="^malformed WriteBytes"):
        translate_control_command("sysbus WriteBytes zzz 0xde")
    with pytest.raises(SimError, match="^malformed ReadBytes"):
        translate_control_command("sysbus ReadBytes 0x1000 xx")
    # Signed tokens are rejected rather than masked (documented divergence).
    with pytest.raises(SimError, match="^malformed WriteBytes"):
        translate_control_command("sysbus WriteBytes -1 0xde")


def test_translate_accepts_decimal_and_the_other_python_radices():
    count, _cmds = translate_control_command("sysbus ReadBytes 4096 4")
    assert count == 4
    _count, cmds = translate_control_command("sysbus WriteBytes 0o20 0b1010")
    assert cmds == ["sysbus WriteByte 0x10 0xa"]


def test_documented_int_token_divergences_from_python_hold():
    # A leading-zero decimal: accepted as 10 here, `int("010", 0)` raises.
    assert parse_int_auto("010") == 10
    # PEP 515 digit separators: rejected here, `int("1_0", 0)` is 10.
    assert parse_int_auto("1_0") is None
    with pytest.raises(SimError, match="^malformed ReadBytes"):
        translate_control_command("sysbus ReadBytes 0x1000 1_0")


def test_a_writebytes_address_overflow_names_the_arithmetic_not_a_token():
    with pytest.raises(SimError) as excinfo:
        translate_control_command("sysbus WriteBytes 0xFFFFFFFFFFFFFFFF 0x1 0x2")
    text = str(excinfo.value)
    assert text.startswith("malformed WriteBytes")
    assert "overflows a 64-bit address" in text
    assert "invalid integer token" not in text


def test_translate_forwards_an_inject_template_verbatim():
    line = "sysbus.iic8.i2c_tmp112 Temperature 85"
    count, cmds = translate_control_command(line)
    assert count is None
    assert cmds == [line]


# ── the deferred-profile warning + the readiness marker ─────────────────────


def test_the_deferred_profile_warning_states_the_empty_arrays_and_silent_uart():
    m = sim_profile_deferred_message("E1M-V2N101")
    assert "E1M-V2N101" in m
    assert "framebuffers" in m
    assert "peripherals" in m
    assert "BOTH empty" in m
    assert "streams NOTHING" in m
    assert "tan-cli#77" in m
    assert "WIRED hardware UART" not in m


def test_the_deferred_profile_warning_fires_for_aen801_and_names_its_wired_console():
    m = sim_profile_deferred_message("E1M-AEN801")
    assert "E1M-AEN801" in m
    assert "BOTH empty" in m
    assert "WIRED hardware UART" in m
    assert "deferred as well" in m


def test_the_ready_marker_carries_the_consumers_poll_token():
    line = ready_marker(60)
    assert "ready (timeout" in line
    assert line == "tan renode --sim-mode: ready (timeout 60s)."


# ── the full control-socket dispatch (the retired e2e's four assertions) ────


class _FakeMonitor:
    """A fake Renode monitor: a byte-addressed memory plus a property store,
    answering the same monitor vocabulary the bridge emits. Port of
    `crates/tan-core/src/renode/sim.rs`'s own `FakeMonitor` test double."""

    def __init__(self) -> None:
        self.mem: dict[int, int] = {}
        self.props: dict[str, str] = {}

    def command(self, cmd: str) -> str:
        parts = cmd.split()
        if len(parts) == 4 and parts[0] == "sysbus" and parts[1] == "WriteByte":
            addr, val = int(parts[2], 0), int(parts[3], 0) & 0xFF
            self.mem[addr] = val
            return ""  # a write prints nothing
        if len(parts) == 4 and parts[0] == "sysbus" and parts[1] == "ReadBytes":
            addr, count = int(parts[2], 0), int(parts[3], 0)
            body = ", ".join(f"0x{self.mem.get(addr + i, 0):02X}" for i in range(count))
            return f"{cmd}\n[\n{body}, \n]\n"
        if len(parts) == 3:
            node, prop, value = parts
            self.props[f"{node} {prop}"] = value
            return ""  # a property SET prints nothing
        if len(parts) == 2:
            node, prop = parts
            return self.props.get(f"{node} {prop}", "")
        raise RuntimeError(f"No such command {cmd!r}")


def test_control_socket_round_trips_the_four_end_to_end_assertions():
    fake = _FakeMonitor()

    def cmd(line: str) -> str:
        return dispatch_control_line(line, fake.command)

    # 1. a WriteBytes replies `ok` (per-byte WriteByte prints nothing).
    assert cmd("sysbus WriteBytes 0x08010000 0xde 0xad 0xbe 0xef") == "ok"
    # 2. the ReadBytes reply is lowercase, space-separated, one line.
    assert cmd("sysbus ReadBytes 0x08010000 4") == "0xde 0xad 0xbe 0xef"
    # 3. an inject (property SET) replies `ok`.
    assert cmd("sysbus.iic8.i2c_tmp112 Temperature 85") == "ok"
    # 4. a property GET echoes the value back.
    assert "85" in cmd("sysbus.iic8.i2c_tmp112 Temperature")


def test_a_malformed_line_replies_err_and_never_panics():
    fake = _FakeMonitor()
    reply = dispatch_control_line("sysbus WriteBytes 0x100", fake.command)
    assert reply.startswith("ERR ")
    assert "no data bytes" in reply


def test_a_monitor_error_replies_err_carrying_the_reason():
    def run(_line: str) -> str:
        raise RuntimeError("Renode monitor is unusable")

    assert dispatch_control_line("bogus", run) == "ERR Renode monitor is unusable"


def test_a_short_read_replies_err_rather_than_a_padded_answer():
    def run(_line: str) -> str:
        return "[ 0x01, 0x02, ]"

    reply = dispatch_control_line("sysbus ReadBytes 0x100 8", run)
    assert reply.startswith("ERR ")
    assert "expected 8" in reply


def test_every_reply_is_exactly_one_line():
    def multi_ok(_line: str) -> str:
        return "a\nb\nc"

    def multi_err(_line: str) -> str:
        raise RuntimeError("line one\nline two")

    def multi_readbytes(_line: str) -> str:
        return "[\n0x1,\n0x2,\n]"

    for reply in [
        dispatch_control_line("get thing", multi_ok),
        dispatch_control_line("get thing", multi_err),
        dispatch_control_line("sysbus ReadBytes 0x1 2", multi_readbytes),
    ]:
        assert "\n" not in reply
        assert "\r" not in reply


# ── monitor line classification ──────────────────────────────────────────────


def test_only_the_bare_sentinel_terminates_a_command():
    sent = "__ALP_SIM_DONE_7__"
    cmd = "sysbus ReadBytes 0x1000 4"
    assert classify_monitor_line(sent, sent, cmd) is MonitorLine.DONE
    assert classify_monitor_line(f"  {sent}  ", sent, cmd) is MonitorLine.DONE
    # The echoed INPUT carries the sentinel but is not it -- dropping it is
    # what keeps `echo "..."` out of the captured output.
    assert classify_monitor_line(f'echo "{sent}"', sent, cmd) is MonitorLine.IGNORE


def test_errors_surface_and_info_warning_and_the_command_echo_do_not():
    sent = "__ALP_SIM_DONE_1__"
    cmd = "sysbus WriteByte 0x0 0x1"
    assert (
        classify_monitor_line("12:00:00.1 [ERROR] sysbus: no peripheral", sent, cmd)
        is MonitorLine.ERROR
    )
    # [ERROR] is checked BEFORE [INFO]/[WARNING]: a line carrying both must
    # still surface as an error.
    assert classify_monitor_line("[INFO] and [ERROR] together", sent, cmd) is MonitorLine.ERROR
    assert (
        classify_monitor_line("12:00:00.1 [INFO] machine: started", sent, cmd)
        is MonitorLine.IGNORE
    )
    assert (
        classify_monitor_line("12:00:00.1 [WARNING] cpu: slow", sent, cmd) is MonitorLine.IGNORE
    )
    assert classify_monitor_line(cmd, sent, cmd) is MonitorLine.IGNORE
    assert classify_monitor_line(f"(monitor) {cmd}", sent, cmd) is MonitorLine.IGNORE
    assert classify_monitor_line("[\n0xDE, ]", sent, cmd) is MonitorLine.OUTPUT
