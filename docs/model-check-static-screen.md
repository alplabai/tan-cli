<!-- SPDX-License-Identifier: Apache-2.0 -->
# `tan model check` — reading the static NPU-eligibility screen

`tan model check` answers one question, offline and with no NPU toolchain
installed: **how much of this model can target the NPU on this SoM, and what
definitely cannot?** It walks the operators of every model your `board.yaml`
declares against the operator-support table alp-sdk publishes for every NPU
backend `som.sku` actually ships (`metadata/npu_ops/<backend>/`).

It is a **screen**, not a compile. Nothing it reports proves that anything
will execute on the NPU. This page exists so that the words it prints are not
read as stronger claims than they are.

```sh
tan model check                 # text
tan model check --format json   # the full report, every field
tan model check --exact         # attempt a real vela compile (Ethos-U only)
```

`ok` and the exit code stay `0` for any run that completed, whatever the
verdicts read — reporting `partial`, `cpu-only` or `undetermined` **is** the
feature, never a failure. Only a run that could not complete is non-zero: an
unresolvable `som.sku` (`model.check-sku-unresolved`, board-level, refuses the
whole run) or an unreadable model source (`model.check-failed`, per-model, so
one bad model does not abort the batch).

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

Per backend — `data.models[].backends[].npuCoverage`:

| `npuCoverage` | Means |
| --- | --- |
| `full-eligible` | Every screened operator is `npu-eligible`. |
| `partial` | Some are, some are `cpu-certain`. |
| `cpu-only` | Every screened operator is `cpu-certain`. |
| `undetermined` | Nothing was screened. **Absence of data, not evidence of no support.** |

`undetermined` is deliberate and load-bearing. A backend that ingests a
different source format than the one you handed it, or one that ships no
support table at all (`deepx_dxm1` ships none, by decision), reports
`undetermined` — never `cpu-only`. Both read to a customer as "won't run",
which is false: every backend degrades to **silent CPU fallback** rather than
refusing, so a fabricated negative would be the worst outcome this command
can produce. A `.tflite` model checked against a V2N/V2M SKU therefore reports
`drpai`/`deepx_dxm1` as `undetermined` with `reason: "format-not-accepted"`:
that is a verdict on the format/backend pairing, not on the model.

### `fits` is not in this vocabulary

`tan model check`'s static screen **never** emits `fits`. The word is reserved
for `basis: "compiled"` (a real compile actually placed the whole model on the
NPU) and `basis: "bench"` (a matched measurement in
`metadata/model_perf/`) — the only two surfaces that have the evidence for it.

The reason is specific to the silicon toolchains. Vela attaches Generic
constraints — quantization, per-axis quant, dtype, zero-point, shape — to
every operator and Specific constraints to 30 of its 70; DRP-AI gates
acceptance on enumerated kernel × stride × padding × dilation × groups. The
same operator name is accepted or rejected on tensor shape alone. A screen
that compares operator **names** cannot see any of that, so it must not claim
it did.

## `basis` and `confidence`

Every backend report carries both:

- `basis` — `"static-screen"`, `"compiled"`, or `"bench"`. On an ordinary run
  it is always `"static-screen"`.
- `confidence` — `"screening"` or `"certain"`. On an ordinary run it is
  always `"screening"`.

At `basis: "static-screen"` the six footprint and latency fields —
`arenaBytes`, `reqSramKib`, `latencyMsMean`, `latencyMsP95`, `latencyRuns`,
`perfRef` — are all `null`. **`null` means "not measured", never zero.** A
name-level screen has no footprint to report, and inventing one from it is
exactly the estimate this vocabulary exists to keep separate from a
measurement. If you are sizing a module, these fields are where the number
would be; their absence is the answer.

The engine states the caveat in the report's own `notes` too, on every scored
report:

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
  measured (`basis: "compiled"` or `"bench"`). An op-count ratio and a
  MAC-weighted ratio answer different questions, so they are separate keys and
  are never both non-`null` on the same report.

## Known limit: ONNX sources screen as `undetermined`

`tan.model.tensorio.extract_ops` walks a **TFLite** flatbuffer. For a `.onnx`
source it returns `[]` today — ONNX operator extraction is out of scope for
this slice (tan-cli#782) and is a follow-on.

The consequence is concrete: an ONNX model checked against a V2N/V2M SKU
passes `drpai`/`deepx_dxm1`'s format gate (both ingest ONNX), then finds no
operators to score, and so reports `npuCoverage: "undetermined"` with the note
*"no operators were extracted for this source; nothing to score, so no
coverage verdict is reported."* That is the honest answer — not a weaker one
than the model deserves, and emphatically not `cpu-only`.

An **unreadable** source is a different thing and is not silently folded into
this: both extractors read the bytes before any format-dependent
short-circuit, so a missing or unreadable `.onnx` raises and surfaces as
`model.check-failed`, exactly like an unreadable `.tflite`.

For a `.tflite` source that extracts to `[]` because the reader itself is
missing, the note names the fix — `pip install alp-tan[model-io]` — rather
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
          "npuCoverage": "full-eligible" | "partial" | "cpu-only" | "undetermined",
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
          "perfRef": "<capture reference>" | null,
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

`data.schemaVersion` is versioned independently of `tan model build`'s and
`tan model doctor`'s — all three are different `data` shapes.

Text mode renders the same serialised values, one block per backend per model:

```text
Ethos-U55 (E1M-AEN501)  partial
  96% of compute (23/25 ops) is NPU-eligible   [upper bound, static screen]
  2 ops are certain CPU fallback: NORMALIZE, TOPK
  static screen (screening): operator-name membership against <table>.json only. …
  Exact:  pip install alp-tan[model-compile]  &&  tan model check --exact
```

The `N/M ops` figure counts only verdicts a real screen determined
(`npu-eligible` / `cpu-certain`). It is suppressed entirely on an
`undetermined` report, so an unscreened backend can never render as
"0/N ops are NPU-eligible" — the `cpu-only` misreading this whole vocabulary
exists to prevent.
