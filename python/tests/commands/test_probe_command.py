# SPDX-License-Identifier: Apache-2.0
"""tan-cli#1406: `tan probe identify|read` -- READ-ONLY J-Link probes.

No hardware: `_spawn_jlink` is a stub J-Link answering fake banners."""
from __future__ import annotations

import json
import os

import pytest
from typer.testing import CliRunner

from tan.cli import app
from tan.commands import flash_cmd, probe_cmd
from tan.core import probe_plan as pp
from tan.core.jlink_probe import JLinkProbe
from tan.core.ram_run import preamble

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX executables / filenames")

SERIAL = "000999000001"
PROBE = JLinkProbe("3-4.2", SERIAL)
ITCM = (0x20003000, 0x00000101, 0x00000105, 0x00000109)


AP_ADDR = {"he": "0x00300000", "hp": "0x00200000"}


def _banner(ap="he", dpidr="0x4C013477"):
    out = f"Found SW-DP with ID {dpidr}\nDPv3 detected\n"
    if ap == "multiple":
        out += ("AP[2] (APAddr 0x00200000): AHB-AP (IDR: 0x34770008)\nAP[2]: Core found\n"
                "AP[3] (APAddr 0x00300000): AHB-AP (IDR: 0x34770008)\nAP[3]: Core found\n")
    elif ap in AP_ADDR:
        out += (f"AP[3] (APAddr {AP_ADDR[ap]}): AHB-AP (IDR: 0x34770008)\nAP[3]: Core found\n")
    return out + "CPUID register: 0x411FD220\nFound Cortex-M55 r1p0, Little endian.\n"


def _dump(addr, words):
    return f"{addr:08X} = " + " ".join(f"{w:08X}" for w in words) + "\n"


class FakeJlink:
    def __init__(self, monkeypatch, *, dpidr="0x4C013477", ap="he", mem=None, itcm_he=ITCM,
                 itcm_local=ITCM):
        self.scripts: list[str] = []
        self.dpidr, self.ap, self.mem = dpidr, ap, mem or {}
        self.itcm_he, self.itcm_local = itcm_he, itcm_local
        monkeypatch.setattr(flash_cmd, "_spawn_jlink", self._spawn)

    def _spawn(self, argv, script, capture, timeout, venv_bin=None, workspace=None,
               executable=None, **kw):
        self.scripts.append(script)
        out = _banner(self.ap, self.dpidr)
        if "ShowEmuList" in script:
            out = f"J-Link[0]: Connection: USB, Serial number: {SERIAL}, ProductName: J-Link\n"
        elif "mem32 0x0," in script:
            out += _dump(0, self.itcm_local) + _dump(0x58000000, self.itcm_he)
        elif "mem32" in script:
            for line in script.splitlines():
                if line.startswith("mem32"):
                    a, n = (int(x.strip().rstrip(","), 16) for x in line.split()[1:])
                    out += _dump(a, self.mem.get(a, tuple(range(n))))
        return flash_cmd._Outcome(success=True, stdout=out + "Script processing completed.\n",
                                  returncode=0)

    def probe_scripts(self):
        return [s for s in self.scripts if "ShowEmuList" not in s]


@pytest.fixture
def env(tmp_path, monkeypatch):
    tools = tmp_path / "tools"
    tools.mkdir()
    stub = tools / "JLinkExe"
    stub.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    os.chmod(stub, 0o755)
    monkeypatch.setenv("PATH", str(tools))
    monkeypatch.delenv("TAN_JLINK", raising=False)
    monkeypatch.setattr(flash_cmd, "enumerate_jlinks", lambda: [PROBE])
    return tmp_path


def _manifest(tmp_path, expect="0x4C013477", slices=None):
    """`slices`: [(core_id, flash_args-yaml)]; default one armed HE slice."""
    build = tmp_path / "build"
    build.mkdir(exist_ok=True)
    slices = slices or [("m55_he", f"{{jlink_device: Cortex-M55, expect_dpidr: '{expect}'}}")]
    body = "".join(
        f"- {{core_id: {c}, os: zephyr, output_artefact: z.elf, status: ok,\n"
        f"   flash_method: alif_mram_jlink, flash_args: {a}}}\n" for c, a in slices
    )
    (build / "system-manifest.yaml").write_text(
        f"schema_version: 1\nhw_info: {{sku: S}}\nslices:\n{body}helper_mcus: []\nboot_order: []\n",
        encoding="utf-8",
    )
    return str(build)


def _run(tmp_path, verb, address=None, count=None, *, core=None, build_root=None, **kw):
    return probe_cmd._run(
        verb, address, count, core, build_root or str(tmp_path / "build"), None, "3-4.2", None,
        str(tmp_path), enumerate_probes=lambda: [PROBE], **kw,
    )


def codes(issues):
    return [i.code for i in issues]


# ── identify ────────────────────────────────────────────────────────────────


def test_identify_happy_path_matches_the_manifest(env, monkeypatch):
    jl = FakeJlink(monkeypatch)
    rc, data, issues, _ = _run(env, "identify", build_root=_manifest(env), core="m55_he")
    assert rc == 0 and issues == []
    ident = data["identity"]
    assert ident["dpidr"] == "0x4C013477" and ident["expectedDpidr"] == "0x4C013477"
    assert ident["dpidrMatch"] is True
    assert ident["apAddr"] == "0x00300000" and ident["cpuid"] == "0x411FD220"
    assert ident["core"] == "he" and ident["itcmVerdict"] == "match"
    assert "isolation" in ident and data["writes"] is False
    assert len(jl.probe_scripts()) == 2


def test_the_schema_version_is_the_string_every_other_command_uses(env, monkeypatch):
    FakeJlink(monkeypatch)
    _rc, data, _i, _l = _run(env, "identify", build_root=_manifest(env))
    assert data["schemaVersion"] == "1" == flash_cmd._data(".")["schemaVersion"]


def test_identify_runs_the_preflight_script_exactly(env, monkeypatch):
    jl = FakeJlink(monkeypatch)
    _run(env, "identify", build_root=_manifest(env))
    from tan.core.flash_plan import FlashInputs, flow_d_preflight_script

    flash_args = {"jlink_device": "Cortex-M55", "expect_dpidr": "0x4C013477", "jlink_serial": SERIAL}
    shared, _expected = flow_d_preflight_script(
        FlashInputs(artefact="x.elf", flash_args=flash_args, core_id="m55_he", sku="S",
                    dry_run=False, force_confirm=False)
    )
    assert jl.probe_scripts()[0] == shared


def test_identify_flags_a_mismatch_and_runs_no_second_session(env, monkeypatch):
    jl = FakeJlink(monkeypatch, dpidr="0x0BE12477")
    rc, data, issues, _ = _run(env, "identify", build_root=_manifest(env), core="m55_he")
    assert rc == 1 and codes(issues) == ["probe.dpidr-mismatch"]
    assert data["identity"]["dpidr"] == "0x0BE12477" and data["identity"]["dpidrMatch"] is False
    assert len(jl.probe_scripts()) == 1  # C: no core-check against the wrong board


def test_identify_unread_dpidr_stops_after_session_one(env, monkeypatch):
    jl = FakeJlink(monkeypatch)
    monkeypatch.setattr(jl, "dpidr", "garbage")
    rc, _d, issues, _ = _run(env, "identify", build_root=_manifest(env), core="m55_he")
    assert rc == 1 and codes(issues) == ["probe.dpidr-unread"] and len(jl.probe_scripts()) == 1


def test_identify_without_a_manifest_says_the_match_was_skipped(env, monkeypatch):
    FakeJlink(monkeypatch)
    rc, data, issues, _ = _run(env, "identify")
    assert rc == 0 and codes(issues) == ["probe.itcm-not-checked", "probe.no-manifest"]
    assert all(i.severity == "info" for i in issues)
    assert data["identity"]["expectedDpidr"] is None and data["identity"]["dpidrMatch"] is None


@pytest.mark.parametrize("core", [None, "m55_hp"])
def test_identify_reads_the_he_window_only_for_an_he_target(env, monkeypatch, core):
    jl = FakeJlink(monkeypatch, ap="hp")
    slices = [("m55_hp", "{expect_dpidr: '0x4C013477'}")]
    rc, data, issues, _ = _run(env, "identify", build_root=_manifest(env, slices=slices), core=core)
    assert rc == 0, issues
    assert data["identity"]["itcmVerdict"] == "not-checked" and data["identity"]["core"] == "hp"
    assert not any("mem32" in sc for sc in jl.probe_scripts())


@pytest.mark.parametrize("ap,expect_codes,rc", [
    ("hp", ["probe.itcm-not-checked", "probe.core-mismatch"], 1),
    ("multiple", ["probe.itcm-not-checked", "probe.core-mismatch"], 1),  # includes the HP AP
    ("unidentified", ["probe.itcm-not-checked"], 0),
])
@pytest.mark.parametrize("claim", ["core", "slice"])
def test_the_itcm_session_follows_the_actual_attach_not_the_claim(env, monkeypatch, ap, expect_codes, rc, claim):
    jl = FakeJlink(monkeypatch, ap=ap)
    root = _manifest(env)  # one armed HE slice
    got_rc, data, issues, _ = _run(env, "identify", build_root=root,
                                   core="m55_he" if claim == "core" else None)
    assert not any("mem32" in sc for sc in jl.scripts), jl.scripts
    assert data["identity"]["itcmVerdict"] == "not-checked"
    assert got_rc == rc and codes(issues) == expect_codes


def test_the_skipped_itcm_check_is_explained(env, monkeypatch):
    FakeJlink(monkeypatch, ap="hp")
    slices = [("m55_he", "{jlink_serial: '1'}"), ("m55_hp", "{jlink_serial: '1'}")]
    _rc, _d, issues, _ = _run(env, "identify", build_root=_manifest(env, slices=slices))
    note = [i for i in issues if i.code == "probe.itcm-not-checked"][0]
    assert note.severity == "info" and "--core m55_he" in note.message
    _rc, _d, issues, _ = _run(env, "identify", core="m55_hp")
    assert "m55_hp" in [i for i in issues if i.code == "probe.itcm-not-checked"][0].message


def test_the_window_message_is_core_neutral(env, monkeypatch):
    FakeJlink(monkeypatch)
    _rc, _d, issues, _ = _run(env, "read", "0x50000000", "1", core="m55_hp")
    assert "every core" in issues[0].message and "unhaltable" in issues[0].message
    assert "HE session must not" not in issues[0].message


def test_identify_he_target_from_the_manifest_slice_runs_the_core_check(env, monkeypatch):
    jl = FakeJlink(monkeypatch)
    _run(env, "identify", build_root=_manifest(env))  # no --core: the one slice is HE
    assert any("mem32 0x0," in sc for sc in jl.probe_scripts())


@pytest.mark.parametrize("core,ap", [("m55_he", "hp"), ("m55_hp", "he")])
def test_identify_with_a_contradicted_core_is_an_error(env, monkeypatch, core, ap):
    FakeJlink(monkeypatch, ap=ap)
    rc, _d, issues, _ = _run(env, "identify", core=core)
    assert rc == 1 and "probe.core-mismatch" in codes(issues)


# ── read ────────────────────────────────────────────────────────────────────


def test_read_happy_path_returns_hex_words_and_the_attach(env, monkeypatch):
    jl = FakeJlink(monkeypatch, mem={0x80010000: (0xAABBCCDD, 1, 2, 3)})
    rc, data, issues, _ = _run(env, "read", "0x80010000", "4")
    assert rc == 0 and issues == []
    assert data["read"]["data"] == ["0xAABBCCDD", "0x00000001", "0x00000002", "0x00000003"]
    assert data["read"]["address"] == "0x80010000" and data["read"]["words"] == 4
    assert data["read"]["attached"]["apAddr"] == "0x00300000"
    assert [l for l in jl.probe_scripts()[0].splitlines() if l.startswith("mem32")] == [
        "mem32 0x80010000, 0x4"
    ]
    assert len(jl.probe_scripts()) == 1  # never a core-check session


def test_read_defaults_to_four_words_and_accepts_decimal(env, monkeypatch):
    FakeJlink(monkeypatch)
    rc, data, _i, _l = _run(env, "read", "2147549184")
    assert rc == 0 and data["read"]["address"] == "0x80010000" and data["read"]["words"] == 4


def test_read_too_large_is_refused_before_any_spawn(env, monkeypatch):
    jl = FakeJlink(monkeypatch)
    rc, _d, issues, _ = _run(env, "read", "0x80010000", "257")
    assert rc != 0 and codes(issues) == ["probe.read-too-large"] and jl.scripts == []
    rc, data, _i, _l = _run(env, "read", "0x80010000", "256")
    assert rc == 0 and data["read"]["words"] == 256


# The `ap` dimension adds no branch coverage (the refusal precedes any spawn); it is here to
# pin that no attach, however it looks, can unlock the window or reach the J-Link.
@pytest.mark.parametrize("ap", ["hp", "multiple", "unidentified", "he", "conflict-hp"])
@pytest.mark.parametrize("core", [None, "m55_he", "m55_hp"])
@pytest.mark.parametrize("addr,words", [("0x50000000", "4"), ("0x58000000", "1"),
                                         ("0x5FFFFFFC", "1"), ("0x4FFFFFFC", "2")])
def test_the_window_is_refused_on_every_attach_and_never_spawns(env, monkeypatch, ap, core, addr, words):
    jl = FakeJlink(monkeypatch, ap=ap.replace("conflict-", ""))
    rc, data, issues, _ = _run(env, "read", addr, words, core=core, build_root=_manifest(env))
    assert rc != 0 and codes(issues) == ["probe.read-unsafe-region"]
    assert jl.scripts == [] and "read" not in data


@pytest.mark.parametrize("addr,words", [("0x4FFFFFFC", "1"), ("0x60000000", "1")])
def test_the_window_edges_are_exact(env, monkeypatch, addr, words):
    FakeJlink(monkeypatch)
    rc, data, issues, _ = _run(env, "read", addr, words)
    assert rc == 0, issues
    assert data["read"]["address"] == f"0x{int(addr, 16):08X}"


@pytest.mark.parametrize("addr,words", [("0xFFFFFFFC", "2"), ("4294967296", "1"), ("0x80010000", "0"),
                                         ("0x80010000", "abc"), ("0x80010000", "-1"),
                                         ("0x80010000;erase", "4"), ("-4", "4"), ("1e3", "4"),
                                         ("0x8001000G", "4"), ("", "4"), ("0x1_0000", "4"),
                                         ("0x80010002", "4")])
def test_read_refuses_bad_ranges_and_numbers(env, monkeypatch, addr, words):
    jl = FakeJlink(monkeypatch)
    rc, _d, issues, _ = _run(env, "read", addr, words)
    assert rc != 0 and codes(issues) == ["probe.bad-argument"] and jl.scripts == []


def test_read_checks_its_own_banner_dpidr_against_the_manifest(env, monkeypatch):
    jl = FakeJlink(monkeypatch, dpidr="0x0BE12477")
    rc, data, issues, _ = _run(env, "read", "0x80010000", "4", build_root=_manifest(env))
    assert rc == 1 and codes(issues) == ["probe.dpidr-mismatch"] and "read" not in data
    assert len(jl.probe_scripts()) == 1


def test_read_with_a_contradicted_core_returns_an_error_and_no_words(env, monkeypatch):
    FakeJlink(monkeypatch, ap="hp")
    rc, data, issues, _ = _run(env, "read", "0x80010000", "4", core="m55_he")
    assert rc == 1 and codes(issues) == ["probe.core-mismatch"] and "read" not in data


# ── manifest handling ───────────────────────────────────────────────────────


def test_flash_args_apply_even_without_expect_dpidr(env, monkeypatch):
    jl = FakeJlink(monkeypatch)
    slices = [("m55_he", f"{{jlink_serial: '{SERIAL}', jlink_speed: 1000}}")]
    _run(env, "read", "0x80010000", "1", build_root=_manifest(env, slices=slices))
    assert "speed 1000" in jl.probe_scripts()[0] and f"SelectEmuBySN {SERIAL}" in jl.probe_scripts()[0]


def test_slices_with_different_serials_are_ambiguous_without_core(env, monkeypatch):
    jl = FakeJlink(monkeypatch)
    slices = [("m55_he", "{jlink_serial: '111'}"), ("m55_hp", "{jlink_serial: '222'}")]
    root = _manifest(env, slices=slices)
    rc, _d, issues, _ = _run(env, "identify", build_root=root)
    assert rc == 1 and codes(issues) == ["probe.failed"] and "--core" in issues[0].message
    assert jl.scripts == []
    FakeJlink(monkeypatch)
    rc, _d, issues, _ = _run(env, "read", "0x80010000", "1", core="m55_hp", build_root=root)
    assert "probe.failed" not in codes(issues)


def test_an_unusable_manifest_is_a_warning_not_a_silent_note(env, monkeypatch):
    FakeJlink(monkeypatch)
    build = env / "build"
    build.mkdir()
    (build / "system-manifest.yaml").write_text("schema_version: [", encoding="utf-8")
    rc, data, issues, _ = _run(env, "read", "0x80010000", "1")
    assert rc == 0 and codes(issues) == ["probe.manifest-unusable"]
    assert issues[0].severity == "warning" and data["manifestPresent"] is True


def test_a_part_number_jlink_device_is_substituted_and_reported(env, monkeypatch):
    jl = FakeJlink(monkeypatch)
    slices = [("m55_he", "{jlink_device: AE822FA0E5597LS0_M55_HE}")]
    _rc, data, _i, _l = _run(env, "read", "0x80010000", "1", build_root=_manifest(env, slices=slices))
    assert data["jlink"]["device"] == "Cortex-M55"
    assert data["jlink"]["deviceSubstitutedFrom"] == "AE822FA0E5597LS0_M55_HE"
    assert "device Cortex-M55" in jl.probe_scripts()[0]


# ── scripts, transcript, envelope ───────────────────────────────────────────

ALLOWED = {"selectemubysn", "si", "speed", "device", "connect", "mem32", "exit", "exec", "showemulist"}


def test_no_probe_script_contains_a_mutating_verb(env, monkeypatch):
    jl = FakeJlink(monkeypatch)
    _run(env, "identify", build_root=_manifest(env))
    _run(env, "read", "0x80010000", "4")
    scripts = jl.scripts[:]
    pre = preamble("123", 4000, "Cortex-M55")
    scripts += [pp.identity_script(pre), pp.core_script(pre), pp.read_script(pre, 0x80010000, 256)]
    assert len(scripts) >= 5
    for script in scripts:
        verbs = pp.script_verbs(script)
        assert not set(verbs) & set(pp.FORBIDDEN_VERBS), script
        assert set(verbs) <= ALLOWED, script


def test_real_spawn_path_prefix_name_and_a_failing_session(env, monkeypatch):
    """Through the REAL `_spawn_jlink`: the first line of every script on disk is exactly
    `exec DisableAutoUpdateFW`, the temp files are `tan-probe-*.jlink`, and a FAILING
    session still only ever sent allowlisted verbs."""
    seen = []

    def fake_spawn(argv, capture, timeout, venv_bin=None, workspace=None, executable=None, **kw):
        path = argv[-1]
        seen.append((os.path.basename(path), open(path, encoding="utf-8").read()))
        return flash_cmd._Outcome(success=False, stderr="Cannot connect to target.", returncode=1)

    monkeypatch.setattr(flash_cmd, "_spawn", fake_spawn)
    rc, _d, issues, _ = _run(env, "identify", build_root=_manifest(env))
    assert rc == 1 and issues
    assert seen, "nothing was spawned"
    for name, text in seen:
        assert name.startswith("tan-probe-") and name.endswith(".jlink")
        assert text.splitlines()[0] == "exec DisableAutoUpdateFW"
        assert set(pp.script_verbs(text)) <= ALLOWED
        assert not set(pp.script_verbs(text)) & set(pp.FORBIDDEN_VERBS)


def test_the_envelope_records_the_exact_text_sent_and_a_transcript(env, monkeypatch):
    FakeJlink(monkeypatch)
    root = _manifest(env)
    _rc, data, _i, _l = _run(env, "read", "0x80010000", "2", build_root=root)
    sent = data["scripts"]["read"]
    assert sent[0] == "exec DisableAutoUpdateFW" and "mem32 0x80010000, 0x2" in sent
    assert data["guard"]["script"] == ["exec DisableAutoUpdateFW", "ShowEmuList", "exit"]
    log = data["transcriptPath"]
    assert log.startswith(os.path.join(root, "flash-logs", "probe-read-"))
    assert "exec DisableAutoUpdateFW" in open(log, encoding="utf-8").read()


def test_without_a_build_root_the_transcript_goes_to_the_stable_cache_dir(env, monkeypatch):
    FakeJlink(monkeypatch)
    monkeypatch.setenv("XDG_CACHE_HOME", str(env / "xdg"))
    _rc, data, _i, _l = _run(env, "read", "0x80010000", "1", build_root=str(env / "nope"))
    assert os.path.isfile(data["transcriptPath"])
    assert data["transcriptPath"].startswith(str(env / "xdg" / "tan" / "probe-logs"))


def test_the_cli_emits_the_envelope_shape(env, monkeypatch):
    FakeJlink(monkeypatch, mem={0x80010000: ITCM})
    result = CliRunner().invoke(
        app, ["probe", "read", "0x80010000", "4", "--build-root", str(env / "build"),
              "--project", str(env), "--format", "json"],
    )
    doc = json.loads(result.stdout)
    assert result.exit_code == 0 and doc["command"] == "probe" and doc["ok"] is True
    assert doc["exitCode"] == 0 and doc["issues"] == []
    assert doc["data"]["schemaVersion"] == "1"
    assert doc["data"]["verb"] == "read" and doc["data"]["read"]["data"][0] == "0x20003000"
    bad = CliRunner().invoke(app, ["probe", "read", "0x80010000", "999", "--format", "json"])
    doc = json.loads(bad.stdout)
    assert bad.exit_code != 0 and doc["ok"] is False and doc["issues"][0]["code"] == "probe.read-too-large"
    unk = CliRunner().invoke(app, ["probe", "bogus", "--format", "json"])
    assert json.loads(unk.stdout)["issues"][0]["code"] == "probe.unknown-verb"


# ── the pure decisions (tan.core.probe_plan) ────────────────────────────────


@pytest.mark.parametrize("addr,words,refused", [
    (0x4FFFFFFC, 1, False), (0x4FFFFFFC, 2, True), (0x50000000, 1, True), (0x5FFFFFFC, 1, True),
    (0x60000000, 1, False), (0x58000000, 256, True),
])
def test_window_rule(addr, words, refused):
    assert (pp.window_refusal(addr, words) is not None) is refused


def test_select_slice():
    he, hp = ("m55_he", {"jlink_serial": "1", "expect_dpidr": "0xA"}), ("m55_hp", {"jlink_serial": "2"})
    assert pp.select_slice([he], None) == (
        {"jlink_serial": "1", "expect_dpidr": "0xA"}, "m55_he", None, None)
    assert pp.select_slice([he, hp], "m55_hp")[1:] == ("m55_hp", None, None)
    assert pp.select_slice([he, hp], None)[2] is not None
    same = [("m55_he", {"jlink_serial": "1"}), ("m55_hp", {"jlink_serial": "1", "expect_dpidr": "0xA"})]
    args, selected, problem, _note = pp.select_slice(same, None)
    assert args["expect_dpidr"] == "0xA" and selected is None and problem is None
    assert pp.select_slice([("m55_he", None)], None) == ({}, None, None, None)
    # a serial pinned by exactly one slice is carried onto the chosen (armed) args
    pinned = [("m55_he", {"expect_dpidr": "0xA"}), ("m55_hp", {"jlink_serial": "7"})]
    assert pp.select_slice(pinned, None)[0] == {"expect_dpidr": "0xA", "jlink_serial": "7"}
    # --core naming a core with no slice is reported, not silently {}
    args, selected, problem, note = pp.select_slice([he], "m55_hp")
    assert args == {} and selected is None and problem is None and "m55_hp" in note
    assert pp.select_slice([], "m55_hp") == ({}, None, None, None)


def test_itcm_session_and_claimed_core_rules():
    assert pp.itcm_check_allowed(True, "he")
    for verdict in ("hp", "multiple", "unidentified", "conflict-hp"):
        assert not pp.itcm_check_allowed(True, verdict)
    assert not pp.itcm_check_allowed(False, "he")
    assert pp.claimed_core("m55_hp", "m55_he") == "m55_hp"
    assert pp.claimed_core(None, "m55_he") == "m55_he" and pp.claimed_core(None, "weird") is None


def test_target_and_contradiction_and_dpidr_rules():
    assert pp.is_he_target("m55_he", None) and pp.is_he_target(None, "m55_he")
    assert not pp.is_he_target("m55_hp", "m55_he") and not pp.is_he_target(None, None)
    assert pp.core_contradiction("m55_he", "conflict-hp") and pp.core_contradiction("m55_hp", "he")
    assert pp.core_contradiction("m55_he", "conflict")  # tan-cli#1488
    assert pp.core_contradiction("m55_he", "unidentified") is None
    assert pp.core_contradiction(None, "hp") is None
    assert pp.dpidr_state(None, "0x1") is None and pp.dpidr_state("0x4C013477", None) == "unread"
    assert pp.dpidr_state("0x4c013477", "0x4C013477") is None
    assert pp.dpidr_state("0x4C013477", "0x0BE12477") == "mismatch"


def test_a_core_with_no_slice_is_reported_in_the_envelope(env, monkeypatch):
    FakeJlink(monkeypatch, ap="hp")
    rc, _d, issues, _ = _run(env, "read", "0x80010000", "1", core="m55_hp", build_root=_manifest(env))
    assert rc == 0 and codes(issues) == ["probe.no-manifest"] and "no slice" in issues[0].message
