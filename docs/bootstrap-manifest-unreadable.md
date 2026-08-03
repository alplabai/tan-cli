<!-- SPDX-License-Identifier: Apache-2.0 -->
# `bootstrap.manifest` — the unreadable-`metadata/bootstrap.json` error

Answers tan-cli#111 against the **Python `tan`** — the executor that actually
ships from v0.5.0 — rather than against the retired Rust `crates/` oracle the
question was originally raised and answered for.

## The measured facts

Run against a real SDK checkout (`scripts/alp_project.py` marker present)
whose `metadata/bootstrap.json` cannot be read, two ways:

1. a **directory** at the manifest's path (the portable stand-in for
   `chmod 000`, reproducible on Windows);
2. a **non-UTF-8** file at the manifest's path.

Command, matching the extension's own Windows pre-flight exactly
(`alp-sdk-vscode` `9dda95a`, `src/alpCli/service.ts`):

```
tan bootstrap --no-pip --no-west --format json --sdk-root <sdk>
```

Both cases, both the source tree (`py -3.12 -m tan`, from `python/`) and the
frozen `python/dist/tan.exe`, produced the byte-identical `issues[]` shape
(only the OS-native error reason in `message` differs between the two
unreadable-input cases):

```json
{
  "command": "bootstrap",
  "ok": false,
  "exitCode": 2,
  "issues": [
    {
      "code": "bootstrap.manifest",
      "severity": "error",
      "message": "metadata/bootstrap.json could not be read: [Errno 13] Permission denied: '<path>'"
    }
  ]
}
```

- **`issue.code`: `"bootstrap.manifest"`**, verbatim.
- **`exitCode`: `2`.**
- **Ordering: strictly before any workspace side effect.** After both runs,
  the checkout's parent directory held nothing new — no `.venv`, no `.west`,
  no `zephyr/`. The refusal fires at the first manifest read, before the
  workspace-parent guard and before any venv/west/pip phase, so a pre-flight
  that does not recognise the code and falls through to a real `tan
  bootstrap` run costs the customer a second identical failure in **seconds**,
  not minutes, and leaves nothing to clean up.

## Why this needed re-measuring rather than reading a comment

`contract/issue-codes.json` already carries this exact answer. The Rust oracle
originally supplied the registry entry; the shipping Python emission is now
covered by `python/tests/gates/test_every_issue_code_is_registered.py` and
`test_frozen_issue_codes.py`:

```
"code": "bootstrap.manifest",
"status": "reserved",
"consumer": "none -- ...",
"note": "tan-cli#111: fires at the FIRST `load_facts(&sdk_root)` call, ...
  strictly BEFORE `select_workspace` ... A doubled run therefore costs
  seconds, not minutes, and leaves nothing on disk: no `.venv`, no `.west`,
  no relocation."
```

`docs/ROADMAP.md`'s Standing Rules are explicit that a Rust-side note is not
sufficient evidence for what the *Python* surface does — "measure the oracle
by RUNNING it," and a shipped Python surface has to be measured on its own
terms, not carried over from `crates/`. The run above is that measurement:
**the Python port reproduces the Rust oracle's code, exit code, and ordering
exactly — no drift found.** The code string is also shape-consistent with its
registered siblings (a bare `"manifest"` literal, `bootstrap.`-prefixed at
the framing site, matching e.g. `bootstrap.workspace-guard`,
`bootstrap.sdk-root-unresolved`) — nothing here needs correcting.

## Registry status

`bootstrap.manifest` remains registered at `status: "reserved"` — no consumer
binds it with `===` yet, so the spelling is not frozen as a compatibility
promise. The earlier version of this report said `contract/` could not be
edited and that the Python emit sites were not registered. That transition is
complete: the shared registry now contains every statically discoverable Python
issue code, and the Python source↔registry gates run in CI. This report remains
useful for the runtime ordering measurement that a static gate cannot prove.

## For a consumer (e.g. alp-sdk-vscode)

The spelling is safe to match on **today**, empirically, on both the Rust
`v0.4.x` line and the Python `v0.5.x` line. It is not yet a `frozen`
contractual promise — `status: "reserved"` means a rename would currently
cost nothing on tan's side of the wire by policy, even though none has
occurred in practice. Promote it to `frozen` once a real consumer binds to it,
filling in the consumer and effect fields as required by the registry policy.
