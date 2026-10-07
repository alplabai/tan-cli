# SPDX-License-Identifier: Apache-2.0
"""`tan doctor`'s stale-install verdict (`tan.core.doctor_stale`)."""
import json

from tan.commands import doctor_cmd
from tan.core import doctor_stale as ds

SHA_A = "a" * 40
SHA_B = "b" * 40


def _vcs(rev="dev", commit=SHA_A):
    return ds.read_install_origin(
        json.dumps(
            {
                "url": "https://github.com/alplabai/tan-cli.git",
                "subdirectory": "python",
                "vcs_info": {"vcs": "git", "commit_id": commit, "requested_revision": rev},
            }
        )
    )


def test_no_provenance_is_no_verdict():
    assert ds.read_install_origin(None) is None
    assert ds.read_install_origin("not json") is None
    assert ds.read_install_origin('{"url": "https://x/tan.whl", "archive_info": {}}') is None
    assert ds.stale_verdict("0.7.0", None) is None


def test_vcs_install_behind_the_branch_tip_warns_with_reinstall_command():
    v = ds.stale_verdict("0.7.0", _vcs(), remote_commit=SHA_B)
    assert v is not None
    assert "aaaaaaaa" in v.detail and "bbbbbbbb" in v.detail and "dev" in v.detail
    assert v.fix == (
        'pipx install --force "git+https://github.com/alplabai/tan-cli.git@dev#subdirectory=python"'
    )


def test_vcs_install_at_the_tip_or_unreachable_remote_is_quiet():
    assert ds.stale_verdict("0.7.0", _vcs(), remote_commit=SHA_A) is None
    assert ds.stale_verdict("0.7.0", _vcs(), remote_commit=None) is None


def test_remote_head_never_asks_for_a_pinned_sha_or_without_git():
    assert ds.remote_head(_vcs(rev=SHA_A), "/usr/bin/git") is None
    assert ds.remote_head(_vcs(), None) is None


def test_remote_head_parses_ls_remote(monkeypatch):
    seen = {}

    def fake_probe(argv, timeout):
        seen["argv"], seen["timeout"] = argv, timeout
        return f"{SHA_B}\trefs/heads/dev\n"

    monkeypatch.setattr(ds, "probe", fake_probe)
    assert ds.remote_head(_vcs(), "/usr/bin/git") == SHA_B
    assert seen["argv"][-1] == "refs/heads/dev"
    assert seen["timeout"] == ds.LS_REMOTE_TIMEOUT_S
    monkeypatch.setattr(ds, "probe", lambda argv, timeout: None)
    assert ds.remote_head(_vcs(), "/usr/bin/git") is None


def test_path_install_compares_source_version(tmp_path):
    (tmp_path / "python" / "tan").mkdir(parents=True)
    (tmp_path / "python" / "tan" / "version.py").write_text('# c\nTAN_VERSION = "0.8.0"\n')
    origin = ds.read_install_origin(json.dumps({"url": tmp_path.as_uri(), "dir_info": {}}))
    assert origin is not None and origin.kind == "path"
    src = ds.source_version(origin)
    assert src == "0.8.0"
    v = ds.stale_verdict("0.7.0", origin, source_ver=src)
    assert v is not None and "0.8.0" in v.detail
    assert ds.stale_verdict("0.8.0", origin, source_ver=src) is None
    assert ds.stale_verdict("0.7.0", origin, source_ver=None) is None


def test_check_is_warn_when_behind_and_pass_otherwise():
    behind = ds.stale_verdict("0.7.0", _vcs(), remote_commit=SHA_B)
    c = doctor_cmd.tan_install_check(behind, "0.7.0")
    assert (c.name, c.status, c.scope) == ("tanInstall", "warn", "host")
    assert c.fix == behind.fix
    ok = doctor_cmd.tan_install_check(None, "0.7.0")
    assert ok.status == "pass"
    # warn -> issue under the registered code; pass raises none
    codes = [i.code for i in doctor_cmd.checks_to_issues([c, ok])]
    assert codes == ["doctor.tan-install"]
