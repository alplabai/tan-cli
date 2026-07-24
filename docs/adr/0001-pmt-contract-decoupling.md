# 0001 — P/M/T contract-based SDK decoupling

- Status: Accepted
- Date: 2026-07-23
- Deciders: tan-cli maintainers
- Relates: alp-sdk ADR-0020 (SDK owns build execution), alp-sdk#864 (`--emit
  scaffold`), alp-sdk#855 (ADR-0020 rollout), alp-sdk#866 (bridge retirement)
- Parent rationale for: #1, #7, #12, #14, #15, #16

## Context

alp-sdk ADR-0020 decides *that* the SDK plans and `tan` executes: the SDK is a
plans-only backend, `tan` is the sole executor and the whole user command
surface. It does not, however, frame *why* that split keeps `tan` stable as the
SDK churns, nor state the bar by which we judge whether a given piece of `tan`
honours the split. This ADR is the tan-side complement that records the design
principle every current contract task serves, and the acceptance bar we hold new
work to.

The forcing observation: the **planner (Python) changes with every SDK
release** — new SoMs, new toolchains, new config derivations, new board
metadata. That is expected and correct; it is the planner's job. If `tan` had to
change in lockstep with the planner, the ADR-0020 split would buy nothing — it
would just move the same coupling across a repo boundary.

## Decision

`tan` stays adaptable because it consumes the planner's **contract output**
(bytes and commands, verbatim), never the planner's internals. Three contract
legs plus one version-skew guard *are* the decoupling mechanism:

- **Policy** — `executionPolicy` (the skip-vs-fail rule: `unknownBackend` fail,
  `missingTool` skip, `nullCommand` skip) plus the `schemaVersion` fail-loud skew
  guard. `tan` applies the policy the plan declares; it does not hardcode a copy
  of the planner's skip rules.
- **Metadata** — `--emit build-plan` plus the ~14 `--emit` surfaces, each
  `contents`-complete and byte-parity pinned (see `tests/parity/`). `tan`
  materialises the artefacts and runs the commands the plan carries; it does not
  re-derive what a slice's config or command should be.
- **Template** — a scaffold contract (`--emit scaffold`, alp-sdk#864). The
  convention-bearing scaffold files (CMake `--emit zephyr-conf` bridge, the
  empty-`prj.conf` pattern, `board.yaml` shape, SKU→core silicon facts) come
  from the SDK emit, vendored as generated-checked-in files at release time with
  a byte-parity gate — not re-authored as Rust in the executor.

### The acceptance bar

> `tan` is touched **only** on a `schemaVersion` bump.

A `schemaVersion` bump is a *breaking shape change* to the contract, which the
skew guard refuses loudly (it rejects an unsupported schema rather than silently
falling back to hand-ported behaviour — that fallback is exactly the drift this
principle forbids). An **additive** SDK change — a new SoM, changed config
values, a new field, a new toolchain — requires **zero** `tan` change, because
`tan` reads the contract output rather than reasoning about what produced it.

**Any place where `tan` must be hand-updated when the SDK makes an additive
change is a contract hole, and a contract hole is a bug** — either in `tan` (it
is reasoning about internals it should be reading as bytes) or in the contract
(the SDK is not emitting something `tan` needs). It is not "just how it is."

### Why not the alternatives

- **Why not reimplement the planner in Rust?** The planner encodes the SDK's
  silicon knowledge, which changes every release. A Rust reimplementation would
  have to be updated on every additive SDK change — the exact coupling this ADR
  exists to forbid — and would drift from the Python that customers actually run
  against. The contract is the interface; duplicating the producer defeats it.
- **Why not hand-sync a Rust mirror of the metadata/scaffold?** A hand-synced
  mirror (`ChipKconfig` catalogue #15, the retired scaffold generators #13/#14)
  is a contract hole by construction: it must be edited whenever the SDK's data
  changes, and it silently drifts when someone forgets. #14's scaffold
  generators already regressed a cross-core Kconfig leak precisely because a
  hand-ported generator diverged from the SDK. Vendor the SDK emit with a
  byte-parity gate instead, so drift fails a check instead of shipping.

## Worked instance — the per-slice `alp.conf` wiring (#1)

#1 asked who owns wiring the plan's per-slice `alp.conf` into each `west build`.
The resolution is a clean demonstration of this ADR, requiring **no `tan`
executor change**:

- **Option B (planner wires it) was chosen.** The alp-sdk planner
  (`fix/planner-wires-per-core-extra-conf-file`, ADR-0020 addendum 2026-07-20)
  appends `-DEXTRA_CONF_FILE=<abs>/build/<core>-zephyr/alp.conf` to every Zephyr
  slice's `command`. `tan` already materialises `configArtefacts` and runs each
  slice's `command` verbatim, so the per-core config reaches the build the moment
  the SDK pin advances — the **Metadata** leg working as intended (the command is
  contract output; `tan` executes bytes).
- The standalone `west build -b <board>` path (no plan) is covered by the
  **vendored scaffold's** CMakeLists, which layers `alp.conf` via
  `EXTRA_CONF_FILE` with the `--core` flag (per-core-scoped, no cross-core leak)
  — the **Template** leg.

`tan` owns neither generator; it consumes both contracts. The CI activation of
the planner change is tracked by #12 (the `PINNED_SDK_TAG` bump adds exactly one
`-DEXTRA_CONF_FILE` arg per Zephyr slice, whitelisted as an added-only delta),
and must not be added to `tan` before the pin can produce it.

## Consequences

- New `tan` work is judged against the acceptance bar: if it would need a
  hand-edit on an additive SDK change, it is a contract hole to close, not a
  feature to maintain.
- The known open holes this ADR is the parent rationale for: the hand-written
  Rust scaffold generators (#13/#14 — close via `--emit scaffold` vendoring), and
  the hand-synced Rust metadata catalogue (#15). Both are the coupling this ADR
  forbids and are tracked as bugs.
- New envelope/emit surfaces (e.g. `tan kconfig` #35) stay additive to the wire
  contract and are protected by the envelope drift gate (#7), so a consumer
  (the VS Code extension) cannot be broken silently.
- The version-skew guard is load-bearing: it must keep refusing an unsupported
  `schemaVersion` loudly. Softening it into a best-effort fallback would reopen
  the drift this ADR closes.
