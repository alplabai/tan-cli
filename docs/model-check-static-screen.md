<!-- SPDX-License-Identifier: Apache-2.0 -->
# `tan model check` — reading the static NPU-eligibility screen

`tan model check` answers one question, offline and with no NPU toolchain
installed: **how much of this model can target the NPU on this SoM, and what
definitely cannot?** It walks the operators of every model your `board.yaml`
declares against the operator-support table alp-sdk publishes for every NPU
backend `som.sku` actually ships (`metadata/npu_ops/<backend>/`).

By default it is a **screen**, not a compile: nothing an ordinary run reports
proves that anything will execute on the NPU. (`--exact` can cross that line
for one backend — see below — and says so in the report's `basis`.)
This page exists so that the words it prints are not read as stronger
claims than they are.

> **`metadata/npu_ops/` is not on alp-sdk `dev` yet.** All three tables --
> `ethos_u/u85@vela-5.1.0.json`, `ethos_u/u55-u65@vela-5.1.0.json` and
> `drpai/onnx-i8@translator-1.12.json` -- arrive with **alp-sdk#1470, which
> is OPEN and `mergeable_state: dirty` (CONFLICTING)**. Against an alp-sdk
> checkout that does not carry them, no table resolves for any backend and
> every report comes back `undetermined`. The accompanying `reason` varies:
> `no-table-for-backend` ("absence of data, not evidence of no support") in
> the ordinary case, but `format-not-accepted` where the source-format gate
> refuses first and short-circuits before any table lookup. That is the
> correct answer for a missing table — but it is not the screen working; it
> is the screen having nothing
> to screen against. Everything below describes the command once those
> tables are reachable.

```sh
tan model check                 # text
tan model check --format json   # the full report, every field
tan model check --exact         # attempt a real vela compile (Ethos-U only)
```

`ok` and the exit code stay `0` for any run that completed, whatever the
*screening* verdicts read — reporting `partial`, `cpu-only` or
`undetermined` **is** the feature, never a failure. **One exception, and only
under `--exact`:** a certain arena/SRAM0-residency no-fit under `Sram_Only`
(`model.sram-no-fit`, see "`sramFit`: the SRAM0 residency + arena check"
below) is not a screening verdict at all — it is a hard physical fact about a
compiled blob, and it refuses at `ExitCode.VALIDATION_FAILURE` even though
the run itself completed. Every other completion stays exit `0`. The other
two codes specific to `check` are `model.check-sku-unresolved` (board-level:
`som.sku`'s NPU backends could not be resolved, so the whole run refuses) and
`model.check-failed` (per-model: an unreadable or unparseable source, so one
bad model does not abort the batch). The codes `model` shares across its
subcommands can also fire — `model.board-yaml-missing`,
`model.board-yaml-invalid`, `model.sdk-root-unresolved`,
`model.unknown-subcommand`, `model.internal-failure`.

## The vocabulary

The screen uses a **sound-negative / capped-positive** vocabulary. It
supersedes the retired `fits` | `cpu-fallback` | `no-fit` scheme, which
promised more than a static check can deliver.

Per operator — `data.models[].backends[].ops[].status`:

| `status` | Means | Soundness |
| --- | --- | --- |
| `npu-eligible` | The operator's **name** is in the backend's support table. | **Capped.** Not "will run" — see below. |
| `cpu-certain` | The operator's name is absent from the table. | **Sound.** It will run on the CPU. |
| `unknown` | Nothing was screened for this operator at all. | No claim either way. |

Each verdict carries a `reason`: `op-not-in-table` (the sound negative),
`constraint-unchecked` (the capped positive — see the next section),
`no-table-for-backend` or `format-not-accepted` (the two `unknown` paths).

Per backend — `data.models[].backends[].npuCoverage`. **The value is only
readable together with the report's `basis`**, because the two bases speak
different vocabularies out of the same key:

| `npuCoverage` | At which `basis` | Means |
| --- | --- | --- |
| `full-eligible` | `static-screen` | Every screened operator is `npu-eligible`. |
| `partial` | `static-screen` | Some screened operators are `npu-eligible`, some are `cpu-certain`. **Not a placement claim** -- see the capped positive above. |
| `partial` / `fits` / `cpu-only` | `compiled` | A real vela placement put some, all, or none of the operators on the NPU. |
| `partial` / `fits` / `cpu-only` | `bench`, corroborated | A published `metadata/model_perf/` point matched AND a real `--exact` compile also ran for the same identity: the report carries the COMPILE's real placement (the bench point has no placement data of its own to contribute -- see below), corroborated by the bench point's own arena/SRAM figures. |
| `undetermined` | `bench`, uncorroborated | A published point matched but no `--exact` compile ran alongside it (the ordinary case). alp-sdk's `metadata/model_perf/` schema carries no per-operator placement split for a bench point to report on its own, so the coverage word is withheld rather than borrowed from the static screen underneath -- `arenaBytes`/`reqSramKib`/latency are still real, `confidence: "certain"` measurements; the coverage word honestly is not. |
| `cpu-only` | `static-screen` | Nothing screened is `npu-eligible`. |
| `undetermined` | `static-screen` | Nothing was screened. **Absence of data, not evidence of no support.** |

Five distinct values, listed above in seven rows because `partial` and
`cpu-only` each appear at three bases -- and `fits` and `undetermined` at two
-- meaning a different thing at each, exactly
as `partial`/`fits` at `basis: "bench"` never originate there -- they only
ever arrive already-measured, riding along from a corroborating `--exact`
compile. A bench point that is NOT corroborated
by one (the common case: alp-sdk publishes no `--exact` toolchain requirement
alongside a point, and most `check` runs never pass `--exact` at all) reports
`undetermined`, not whatever the static screen guessed. A consumer matching
exhaustively on `(npuCoverage, basis)` pairs must therefore not assume
`basis: "bench"` implies a placement verdict -- read `confidence` for whether
the FIGURES are certain, and treat `npuCoverage: "undetermined"` at
`basis: "bench"` as "SRAM/latency are measured; placement is not" rather than
as the static-screen meaning of the same word.

**The pair set in that table is gated, the `Means` column is not.**
`tan.model.analyze.LEGITIMATE_COVERAGE_BY_BASIS` enumerates the legitimate
`basis -> {npuCoverage}` combinations, `BackendReport.__post_init__` refuses to
construct a report outside it, and
`tests/gates/test_model_check_doc_coverage_table.py` fails the build in both
directions -- a pair reachable in code but missing from the table, and a row
here for a combination code can no longer produce (tan-cli#1135, after this
page went false at five lines on tan-cli#1115). What each word MEANS at each
basis is prose and stays a review problem; only the pairs are mechanical.

`undetermined` is deliberate and load-bearing. A backend that ingests a
different source format than the one you handed it, or one that ships no
support table at all (`deepx_dxm1` ships none, by decision), reports
`undetermined` — never `cpu-only`. Both read to a customer as "won't run",
which is false: every backend degrades to **silent CPU fallback** rather than
refusing, so a fabricated negative would be the worst outcome this command
can produce. A `.tflite` model checked against a V2N/V2M SKU therefore reports
`drpai`/`deepx_dxm1` as `undetermined` with `reason: "format-not-accepted"`:
that is a verdict on the format/backend pairing, not on the model.

### Where `fits` may and may not appear

**The static screen never emits `fits`.** The word is reserved for
`basis: "compiled"` (a real compile actually placed the whole model on the
NPU) and `basis: "bench"` (a matched measurement in `metadata/model_perf/`
that ALSO corroborates a real `--exact` compile — see the table above; a bench
point alone never independently earns it). Only the `compiled` path DERIVES
it, from `tan.model.perf.coverage_from_placement` (the one function in tan
that may return `"fits"`, off a real vela compile's own "CPU/NPU operators = N"
summary) — the `bench` path never calls that function at all
(`tan.model.perf_apply` has no per-operator placement data to feed it, ever;
see `tan.model.perf`'s own module docstring, "DEAD FIELDS"). A `bench` report
that shows `"fits"` is CARRYING a `compiled` report's own real verdict
forward, not deriving a second one.

The boundary matters for a consumer: seeing `fits` is not a contradiction of
this page, it is the report telling you it is no longer a screen. Read `basis`
first; `npuCoverage` means what that basis licenses it to mean.

The reason a screen cannot deliver `fits` is specific to the silicon
toolchains. Vela attaches Generic constraints — quantization, per-axis quant,
dtype, zero-point, shape — to **every** operator, and further
operator-specific constraints on top of those; DRP-AI gates acceptance on
enumerated kernel × stride × padding × dilation × groups. The same operator
name is therefore accepted or rejected on tensor shape alone. A check that
compares operator **names** cannot see any of that, so it must not claim it
did.

## `basis` and `confidence`

Every backend report carries both:

- `basis` — `"static-screen"`, `"compiled"`, or `"bench"`.
- `confidence` — `"screening"` or `"certain"`. `"screening"` pairs with
  `"static-screen"`; `"certain"` pairs with `"compiled"` and `"bench"`.

A plain run reports `"static-screen"` / `"screening"`. Two things move a
report off that pairing, and **neither needs a flag you did not pass**:
`--exact` reaching a real vela compile (`"compiled"`), and alp-sdk publishing
a `metadata/model_perf/` point that matches your model's sha256, SKU, `hw_rev`
and backend (`"bench"`). No alp-sdk has published a `model_perf/` tree yet, so
the bench path is dormant today rather than absent — do not code against
`basis` being a constant.

At `basis: "static-screen"` the six footprint and latency fields —
`arenaBytes`, `reqSramKib`, `latencyMsMean`, `latencyMsP95`, `latencyRuns`,
`perfRef` — are all `null`. **`null` means "not measured", never zero.** A
name-level screen has no footprint to report, and inventing one from it is
exactly the estimate this vocabulary exists to keep separate from a
measurement. If you are sizing a module, these fields are where the number
would be; their absence is the answer.

The engine states the caveat in the report's own `notes` too, on every scored
report. The word in parentheses is the resolved table's own `stance` field
(`"screening"` in all three tables alp-sdk#1470 publishes), defaulting to
`"screening"` when a table omits it — it is table-sourced, not a literal:

> static screen (screening): operator-name membership against
> `<variant>@<toolchain>-<ver>.json` only. Eligible ops still carry unchecked
> quantization/shape/dtype constraints this check cannot verify -- the model
> will run either way, an unsupported op falls back to the CPU silently rather
> than failing. Only a real compile proves NPU execution.

`--exact` is the one upgrade path available today. Vela is a free, un-gated
`pip install`, so for `ethos_u` the offline constraint is soft: with `vela` on
PATH, `--exact` runs it for real and reports what vela **actually** placed,
lifting that backend's report to `basis: "compiled"` /
`confidence: "certain"`. It degrades back to the static screen — and says so
in a note — when `vela` is absent or the compile fails. `drpai` and
`deepx_dxm1` stay static-screen-only under `--exact`: the DRP-AI TVM checkout
and `dxcom` are license-gated, so the command reports that as a reason rather
than attempting either.

## `computeOnNpuPctMax` — why coverage is MAC-weighted

`computeOnNpuPctMax` is the share of the model's **multiply-accumulate work**
whose operators are `npu-eligible`, `0`–`100`. It is an **upper bound**, set
only at `basis: "static-screen"`.

It is MAC-weighted rather than op-counted for one reason: op-count coverage
hides a compute-dominant convolution backbone behind a wall of cheap
elementwise ops. A model where 90 % of the operators are `npu-eligible`
reshapes, adds and activations, but the two convolutions carrying nearly all
the arithmetic are `cpu-certain`, is not 90 % NPU-bound in any sense a
customer cares about. Weighting by MACs makes that model read as the near-zero
it is.

Two structured caveats travel with the number:

- **`uncostedCpuOpCount`.** The MAC estimator prices conv/dense operators
  only. A `cpu-certain` operator it could not price carries `macs: 0` and
  leaves the denominator entirely, so `computeOnNpuPctMax` can read `100.0`
  while real, unpriced CPU compute exists and `npuCoverage` still says
  `partial`. The count is a field, not only prose, so a consumer that reads
  the percentage but never renders `notes` still sees the caveat. It is
  deliberately **not** clamped — clamping would hide the exact gap this
  command exists to surface.
- **`npuPlacementPctReal` is a different number.** It is the *real*
  NPU-vs-CPU **op-count** placement ratio, set only where a placement was
  actually measured -- `basis: "compiled"` always, or `basis: "bench"` ONLY
  when a bench point corroborates a real `--exact` compile that ran alongside
  it (see "Where `fits` may and may not appear" above). An uncorroborated
  bench point (the ordinary case) has no placement of its own to report and
  leaves this `null`, same as the static screen. An op-count ratio and a
  MAC-weighted ratio answer different questions, so they are separate keys and
  are never both non-`null` on the same report.

## Known limit: ONNX sources screen as `undetermined`

`tan.model.tensorio.extract_ops` walks a **TFLite** flatbuffer. For a `.onnx`
source it returns `[]` today — ONNX operator extraction is out of scope for
this slice (tan-cli#782) and is a follow-on.

The consequence is concrete: an ONNX model checked against a V2N/V2M SKU
passes `drpai`/`deepx_dxm1`'s format gate (both accept only `onnx`), and both
report `npuCoverage: "undetermined"` — but by two different routes, with two
different notes:

- **`drpai`** resolves its table (`drpai/onnx-i8@translator-1.12.json`), then
  finds no operators to score:
  *"no operators were extracted for this source; nothing to score, so no
  coverage verdict is reported."*
- **`deepx_dxm1`** never gets that far — it ships no table at all, by
  decision:
  *"no NPU-ops support table for this backend/variant -- absence of data, not
  evidence of no support."*

Either way the verdict is the honest one — not a weaker answer than the model
deserves, and emphatically not `cpu-only`.

An **unreadable** source is a different thing and is not silently folded into
this: both extractors read the bytes before any format-dependent
short-circuit, so a missing or unreadable `.onnx` raises and surfaces as
`model.check-failed`, exactly like an unreadable `.tflite`.

For a `.tflite` source that extracts to `[]` because the reader itself is
missing, the note names the fix — `pip install tan-cli[model-io]` — rather
than reading as "this model genuinely has no operators".

## The JSON report

`--format json` emits the standard envelope
(`{command, ok, exitCode, project, data, issues}`, plus `sdk`). `data` is:

```text
{
  "schemaVersion": "1",
  "sku": "<som.sku>",
  "exact": false,
  "models": [
    {
      "name": "<board.yaml models[].name>",
      "source": "<absolute path>",
      "backends": [
        {
          "backend": "ethos_u" | "drpai" | "deepx_dxm1",
          "variant": "u55" | "u65" | "u85" | null,
          "table": "<path to the table that answered>" | null,
          "npuCoverage": "full-eligible" | "partial" | "cpu-only" | "undetermined"
                         | "fits",          // "fits" only at basis compiled/bench
          "computeOnNpuPctMax": <0-100> | null,
          "npuPlacementPctReal": <0-100> | null,
          "uncostedCpuOpCount": <int>,
          "basis": "static-screen" | "compiled" | "bench",
          "confidence": "screening" | "certain",
          "arenaBytes": <int> | null,
          "reqSramKib": <int> | null,
          "latencyMsMean": <float> | null,
          "latencyMsP95": <float> | null,
          "latencyRuns": <int> | null,
          "perfRef": "<bench/rig id, e.g. e1m-aen-evk-01>" | null,
          "sramFit": {
            "arena": {"verdict": "fits-unverified" | "no-fit" | "skipped",
                      "neededKib": <int> | null, "limitKib": <int> | null,
                      "reason": "<string>" | null},
            "sram0": {"verdict": "fits-unverified" | "no-fit" | "skipped",
                      "neededKib": <int> | null, "limitKib": <int> | null,
                      "reason": "<string>" | null},
            "blobKib": <int> | null
          } | null,
          "notes": ["..."],
          "ops": [
            {"op": "CONV_2D", "status": "npu-eligible",
             "reason": "constraint-unchecked", "macs": 1806336}
          ]
        }
      ]
    }
  ]
}
```

`variant` is `null` for every backend but `ethos_u`. `table` is `null`
whenever no table resolved — including on every run against an alp-sdk that
does not yet carry `metadata/npu_ops/` (see the note at the top).

## `sramFit`: the SRAM0 residency + arena check (#1288, `--exact` only)

`sramFit` is `null` at `basis: "static-screen"` — no compiled blob exists yet
to measure a fit against, and this block is never a guess. It appears only on
an ethos_u report at `basis: "compiled"` (a real `--exact` compile ran), and
only its `ethos_u` backend ever carries a non-null value — `drpai`/
`deepx_dxm1` never compile under `--exact` in this release (see above), so
their `sramFit` stays `null` too.

Both `arena` and `sram0` apply ONLY when the target's SoC spec resolves the
`Sram_Only` vela memory mode — `src/backends/inference/ethos_u_aen.cpp` pins
every NPU access to the SRAM AXI port there, so both the tensor arena and the
compiled blob's weights must be SRAM0-resident. Every other memory mode
(`Shared_Sram`, `Dedicated_Sram*`) may place the blob off SRAM0, so both axes
report `verdict: "skipped"` there, with a `reason`.

**Which `board.yaml` core decides the arena budget is scoped, not guessed
(#1288 review).** The Ethos-U NPU a target compiles for may declare its own
`paired_core` (e.g. an AEN SoM's high-perf/high-efficiency Ethos-U55 pairs to
`m55_hp`/`m55_he`), or it may pair to no core at all — real metadata, not a
gap: the E4/E6/E8's own Ethos-U85 declares none, and that NPU is exactly the
one `--exact`/`build` compile for by default on those SKUs. A core only
counts at all when it **PARTICIPATES** — not parked `os: "off"`, the same
one exclusion `tan.planner.orchestrator.iter_buildable_slices` uses to decide
which cores the planner builds a slice for at all — **and runs an inference
workload** — an `inference:` block declared on it (even empty), OR
`tflite-micro` named in `libraries:` (project-wide, or scoped to include
it): `inference:` is app-level TUNING, not a declaration of intent, so an
app that never overrides the arena default has no reason to write the block
at all, and the library is the signal that catches that case (mirrors
`tan.planner.kconfig`'s own `_slice_wants_inference`). A core `board.yaml`
declares for an unrelated reason (a peripheral, an app) but that runs no
inference workload at all is NOT a certain core, even if it happens to be
the target's own `paired_core` — and a PARKED core never counts even when a
project-wide `libraries:` entry (no `cores:` scoping) would otherwise seem
to include it: the E1M-AEN801 template's own `a32_cluster: {os: "off"}`
would wrongly count without this exclusion, turning a real `m55_hp`-only
certain no-fit into a merely `skipped` ambiguous range.

- **A paired core that runs inference, declared under `cores:`** — is
  CERTAIN: its own `inference.default_arena_kib` (`128` KiB, the schema
  default, when its `inference:` block omits one) is the arena budget, and a
  `no-fit` against it may refuse.
- **No paired core, and `board.yaml` runs inference on exactly one core** —
  that ONE core is used, CERTAIN, the same as above. (This is the
  AEN801-template case: the U85 pairs to nothing, but `m55_hp` is the
  board's only inference core, so ITS `default_arena_kib: 64` decides the
  U85's own arena fit — not the bare `128` default.)
- **No paired core, and `board.yaml` runs inference on MORE THAN ONE core**
  — nothing sourced picks between them: `arena` is a certain `no-fit` only if
  the model needs more than the LARGEST of their budgets, `fits-unverified`
  only if it needs no more than the SMALLEST, and otherwise `skipped`
  (ambiguous, naming the cores). `sram0` gets its OWN three-way split here,
  independent of what `arena` itself decided: `no-fit` only if the blob
  needs more than the SMALLEST budget can hold (a certain lower bound —
  every OTHER candidate needs even more), `fits-unverified` only if it fits
  even the LARGEST (every candidate fits), and otherwise `skipped` — a blob
  that would fit some candidates and not others is not a number `sram0` may
  assert either word about.
- **A paired core `board.yaml` never declares at all, a paired core
  `board.yaml` declares but that runs no inference workload, `board.yaml`
  runs inference on no core at all, or no `board.yaml` in hand** — `arena` is
  `skipped`, with a reason, NEVER a `128`-based guess (E1M-AEN801's own
  template never declares `m55_he`, so its high-efficiency Ethos-U55 target
  skips the arena check rather than comparing against an invented number).
  `sram0`, though, still catches a certain `no-fit` here when the blob alone
  is too big even for the SMALLEST arena `board.schema.json`'s own schema
  allows (`MIN_ARENA_KIB`, 16 KiB) — a real arena, whichever core eventually
  owns it, can never be smaller, so that failure holds regardless. Otherwise
  `sram0` is `skipped` too: it can prove a lower-bound failure, never a pass,
  with no core resolved at all.

`sram0` sums `ceil(len(compiled blob)/1024)` plus whichever arena figure the
rules above resolved (never `reqSramKib`) against the resolved SoC variant's
`sram_banks_kb.SRAM0`.

**Never a bare `"fits"`.** Every SoC ships `inference_arena_sram_kib: 0`
today, so there is no VERIFIED inference-arena SRAM budget anywhere in
metadata — a pass reads `"fits-unverified"`, never `"fits"`. Only a `no-fit`
reached through one of the CERTAIN paths above (or a certain LOWER BOUND, per
the unresolved case above) is real: it is plain ceil'd-KiB arithmetic against
a real, sourced number, so a failure is a hard physical fact, not a screen.
`tan model build` records a certain no-fit as a skipped coverage row for that
target, and refuses the model (no `.alpmodel` written) only when no ethos_u
target fits; `tan model check --exact` reports the SAME certain
no-fit as the `model.sram-no-fit` issue at `ExitCode.VALIDATION_FAILURE`,
alongside the model's own report (nothing about `sramFit` is hidden even
when the run refuses). This never offers or implies an MRAM placement — the
NPU reading weights in place from MRAM is unproven on the bench.

`data.schemaVersion` is versioned independently of `tan model build`'s and
`tan model doctor`'s — all three are different `data` shapes.

Text mode renders the same serialised values, one block per backend per model:

```text
Ethos-U55 (E1M-AEN501)  partial
  96% of compute (23/25 ops) is NPU-eligible   [upper bound, static screen]
  2 ops are certain CPU fallback: NORMALIZE, TOPK
  static screen (screening): operator-name membership against <table>.json only. …
  Exact:  pip install tan-cli[model-compile]  &&  tan model check --exact
```

The `N/M ops` figure counts only verdicts a real screen determined
(`npu-eligible` / `cpu-certain`). It is suppressed entirely on an
`undetermined` report, so an unscreened backend can never render as
"0/N ops are NPU-eligible" — the `cpu-only` misreading this whole vocabulary
exists to prevent.
