# SPDX-License-Identifier: Apache-2.0
"""`tan bootstrap`'s `zephyr/patches.yml` phase (tan-cli#1296).

alp-sdk carries patches against Zephyr and its modules in `zephyr/patches.yml`
(e.g. `ipm_arm_mhuv2.c`'s `poll_out`, hal_alif's `se_service_boot_cpu`).
`scripts/bootstrap.sh` applies them and documents them as required to build;
`tan bootstrap` did not, and a tan-made workspace failed to compile the
`peripheral-io/hello-world` example for E1M-AEN801 (m55_hp slice) with
`'struct ipm_driver_api' has no member named 'poll_out'`. This mirrors that
script's algorithm -- the SDK's own `scripts/verify_west_patches.py` is the
oracle for "is it applied"; tan only drives it (it must not re-implement the
SDK's patch knowledge).
"""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from tan.core.west_patches import classify_verify, output_tail, parse_unapplied

if TYPE_CHECKING:  # bootstrap_cmd imports THIS module, so no runtime import back
    from tan.commands.bootstrap_cmd import Log, Runner, VenvBin, Workspace

#: Issue-code suffixes (`bootstrap.<suffix>`); `FAILED` is a `WORKSPACE_BLOCKING`
#: member: a tree that lacks the patches cannot build what needs them.
FAILED = "west-patches-failed"
UNCHECKED = "west-patches-unchecked"

_VERIFIER = Path("scripts") / "verify_west_patches.py"
_PATCHES_YML = Path("zephyr") / "patches.yml"

_OPT_OUT = (
    " Pass `--no-patches` to skip this step (zephyr/patches.yml is then NOT applied, "
    "and builds that need those patches will not compile)."
)


def _detail(*chunks: str) -> str:
    tail = output_tail("\n".join(chunk for chunk in chunks if chunk))
    return f"\n{tail}" if tail else ""


def _fail(log: Log, message: str, *output: str) -> None:
    log.warn(FAILED, f"{message}{_OPT_OUT}{_detail(*output)}")


def _report(
    ws: Workspace, log: Log, verdict: str, out: str, err: str, *, after_apply: bool
) -> bool:
    """Act on a verifier verdict. True when the phase is finished; False only
    for the one verdict that means "go apply" (`unapplied`, before applying)."""
    if verdict == "applied":
        when = "verified applied" if after_apply else "already applied"
        log.line(f"zephyr/patches.yml {when} in {ws.workspace_dir}")
    elif verdict == "unchecked":
        # Everything inspectable is patched; a module the file names just is not
        # in this workspace. Normal for a narrow one, so a warning.
        log.warn(
            UNCHECKED,
            "some zephyr/patches.yml modules are not in this workspace, so their patches "
            f"could not be checked.{_detail(out, err)}",
        )
    elif verdict == "unrunnable":
        # Exit 2 / never launched: the patch state is UNKNOWN, so do not claim
        # "not applied".
        _fail(
            log,
            "could not run or inspect the zephyr/patches.yml verifier "
            "(scripts/verify_west_patches.py) -- the patch state is unknown.",
            out, err,
        )
    elif after_apply:
        _fail(
            log,
            f"zephyr/patches.yml is still not applied in {ws.workspace_dir} after "
            "`west patch apply`.",
            out, err,
        )
    else:
        return False
    return True


def _apply_missing(
    ws: Workspace, venv: VenvBin, log: Log, runner: Runner, modules: list[str]
) -> bool:
    """`west patch apply` per module. False (after recording the error) on the
    first failure."""
    # Late import: `bootstrap_cmd` imports this module, so a top-level import
    # of its `_west_argv` would be circular.
    from tan.commands.bootstrap_cmd import _west_argv

    for module in modules:
        # PER MODULE, never a bare `west patch apply`: it is not idempotent, so
        # on a partially patched workspace (zephyr patched, mcuboot fresh) it
        # re-applies the patched module's patches and dies on the first.
        # `--dst-module` is a flag of `west patch` ITSELF, so it precedes
        # `apply`; the other order is a usage error.
        log.line(f"Applying zephyr/patches.yml for module '{module}'")
        failure = runner.run(
            _west_argv(venv, ["patch", "--dst-module", module, "apply"]), cwd=ws.workspace_dir
        )
        if failure is not None:
            # `clean` is the zephyr `west patch` default pair `git checkout .`
            # + `git clean -d -f -x` run in the module: it is the way out of a
            # half-applied patch, and it discards uncommitted work there.
            _fail(
                log,
                f"`west patch --dst-module {module} apply` failed, so the module may be "
                f"half-patched. To recover, run `west patch --dst-module {module} clean` "
                f"from {ws.workspace_dir} (it runs `git checkout .` and `git clean -d -f "
                f"-x` in that module, discarding uncommitted changes there), then re-run "
                f"`tan bootstrap`.",
                failure,
            )
            return False
    return True


def patches_phase(
    ws: Workspace, venv: VenvBin, log: Log, runner: Runner, sdk_root: str
) -> None:
    """Verify, then apply only what is missing, then verify again.

    Non-fatal like `pip_phase`/`toolchain_phase`: a failure is a recorded
    `bootstrap.west-patches-failed` (which blocks `complete.` unless
    `--allow-partial`), never an abort, so it cannot skip the toolchain phase.
    """
    sdk = Path(sdk_root)
    verifier = sdk / _VERIFIER
    if not verifier.is_file() or not (sdk / _PATCHES_YML).is_file():
        # An older SDK tag simply has no patch set to apply -- not an error.
        log.line("Skipping zephyr/patches.yml: this alp-sdk checkout carries no patch verifier")
        return

    # Run from the SDK root, like bootstrap.sh, so the verifier resolves its
    # own `patches.yml` the way it was written to.
    verify = [
        str(venv.python), str(verifier),
        "--topdir", str(ws.workspace_dir), "--west", str(venv.west),
    ]
    code, out, err = runner.run_status(verify, cwd=sdk)
    if runner.dry_run:
        log.line("Would verify zephyr/patches.yml and apply any missing patches (--dry-run)")
        return
    if _report(ws, log, classify_verify(code), out, err, after_apply=False):
        return

    code, listing, list_err = runner.run_status([*verify, "--list-unapplied"], cwd=sdk)
    modules = parse_unapplied(listing) if code == 0 else []
    if not modules:
        _fail(
            log,
            "zephyr/patches.yml is not applied but the verifier named no module to patch "
            "-- run scripts/verify_west_patches.py directly to see why.",
            out, err, list_err,
        )
        return

    if _apply_missing(ws, venv, log, runner, modules):
        # `west patch apply` exits 0 on three do-nothing paths, so its status is
        # not evidence; the verifier is.
        code, out, err = runner.run_status(verify, cwd=sdk)
        _report(ws, log, classify_verify(code), out, err, after_apply=True)
