<!-- SPDX-License-Identifier: Apache-2.0 -->
# Planner derivations still duplicated across alp-sdk and tan

Acceptance item (c) of tan-cli#509: the written list of which planner
derivations remain duplicated across the two repos after tan-cli#270, each
with the reason it stays duplicated rather than being emitted once by
alp-sdk.

Every claim below was re-measured for this document against two trees and
nothing else:

- **tan** -- this worktree, branched off `dev` at `3ff8890`
  (`perf(tool-lookup): drop the per-candidate Path from the Windows PATH
  walk (#811) (#874)`).
- **alp-sdk** -- a frozen checkout at
  `94378a056549c7377d714a7f2b68878aca8fea01`. This was the commit
  `python/tests/gates/test_planner_relocation_freshness.py` named as
  `PINNED_SDK_COMMIT` when this document was written (`:412` at the
  time). A later merge on this same PR (tan-cli#509) moved that pin to
  `eb96112ba7d1cc3b4084c985962ea31772177d74` (`:516` now) without
  re-running this document's measurements, so every count and
  hash-match verdict below is dated to `94378a05` unless stated
  otherwise.

## Corrections to the inventory in the issue

The issue's own comment (hkngln, 2026-08-12) measured this against
alp-sdk `7f662403`. Re-measured at `94378a05`, its substance holds -- six
identical, one dead-on-both, one live disagreement -- but three statements
in it are wrong as written and are corrected here.

1. **The TBD sentinel is duplicated across EIGHT sites inside
   `python/tan/planner/`, not three.** Two are named functions
   (`zephyr_board.py:101`, `slugs.py:42`); six are the expression written
   out inline (`carveout.py:134`, `carveout.py:182`, `partition.py:202`,
   `som_metadata.py:143`, `loader.py:258`, `kconfig.py:303`). alp-sdk has
   exactly ONE definition (`scripts/sentinels.py:22`) imported by nine
   modules. The asymmetry is the whole point of the entry, and stating it
   as "three" understates it by more than half. Full evidence in
   derivation 8.
2. **Both Ethos-U line numbers moved.** The issue cites alp-sdk
   `gen_zephyr_board.py:1246` and `tan/planner/zephyr_board.py:1275`. At
   the measured trees the `_accel, node = ethos_u` statement is at
   `scripts/gen_zephyr_board.py:1357` and
   `python/tan/planner/zephyr_board.py:1388`. The claim itself -- that the
   accelerator string is discarded on BOTH sides, so this is not drift --
   is confirmed.
3. **The `e6928625` attribution is unverifiable from here, and only the
   attribution.** The frozen alp-sdk oracle is an extract with no `.git`,
   so no commit in it can be resolved. Everything the example asserts
   about CONTENT was verified independently and is recorded under "the
   worked example" below, including the tan commit that closed the gap
   (`59c2b88`, PR #521, Fri Aug 7 2026).

A fourth statement is not wrong but is worth sharpening: "all four
`_sku_family`/`_sku_family_slug` copies" are identical **in code**, not
byte-identical. The two `_sku_family_slug` copies differ in three
docstring lines, each naming its own repo's sibling module. The regex,
the mapping and the raise are the same characters. See derivation 5.

## How this was measured

For each derivation the two source regions were extracted with `sed` and
compared with `diff` plus an md5 of the extracted span, so "identical"
means byte-identical over the named line range and not a reading of two
listings side by side. Where the count of something is claimed (52 pad
names, 99 pad names, 70 chip slugs) the count was produced by running or
grepping the code, never by eye.

The three staleness pins in
`python/tests/gates/test_planner_relocation_freshness.py` were also
re-hashed against the frozen tree, because a "these agree" verdict is
worth less if the audit that is supposed to notice them diverging is
itself stale:

- All **21** `PINNED_HASHES` entries (`scripts/alp_orchestrate/*.py`)
  matched at `94378a05` when this document was written --
  `PINNED_SDK_COMMIT` WAS `94378a05` then, so the relocated half of the
  planner was audited against exactly the tree measured here. Re-checked
  after the later merge that moved `PINNED_SDK_COMMIT` to
  `eb96112ba7d1cc3b4084c985962ea31772177d74`: still all 21 match there too
  (`test_relocated_planner_modules_match_the_pinned_sdk_audit` PASSED with
  `ALP_SDK_ROOT` bound to `eb96112b`) -- expected, since the table is by
  construction recalibrated to whatever `PINNED_SDK_COMMIT` names.
- **17 of 19** `HAND_PORT_HASHES` entries matched at `94378a05` (this
  document's frozen tree -- see above; not re-measured against
  `PINNED_SDK_COMMIT`'s later `eb96112b` value, since
  `HAND_PORT_PINNED_SDK_COMMIT` tracks neither), even though
  `HAND_PORT_PINNED_SDK_COMMIT` is
  `88318e759958529fbbd8fe9d481373681c0fa78d`, deliberately behind. The two
  that differed at `94378a05` are `scripts/alp_template.py` (frozen
  `5d453c5d72c565855090a4c1a77abdc359bae215171b335f82c4f268e95d9014`) and
  `scripts/alp_cli/doctor.py` (frozen
  `fe109d986def5f3942c0fe5c158b2a8cd971d9d04dcb5c118349dbb8e4819e1d`),
  both already argued for in that file's own comments at lines 720-739.
  **Neither is a source of any of the eight derivations below**, so the
  older hand-port pin does not weaken any verdict in this document.
  **Re-hashed again at `eb96112b` (tan-cli#896, 2026-08-25): 13 of 19
  match, not 17.** Four more sources diverged inside
  `94378a05..eb96112b`: `scripts/gen_zephyr_board.py` (`522ea3204`,
  docstring-only per the commit's own "No behaviour change; comments
  only" -- the fix landed in the board trees and
  `scripts/check_atoc_reservation.py`, not in this generator), and
  `scripts/alp_project_loader.py` /
  `scripts/alp_project_emit/__init__.py` /
  `scripts/alp_project_emit/west_libs.py` (`85b6b905a` threading
  `--metadata-root` through every resolver, plus `95eb64ab8`'s ten new
  `_CHIP_SUBSYSTEMS` entries in `__init__.py` -- both BEHAVIOURAL
  upstream). All three of the second group are already carried in
  `tan/planner/{som_metadata,slugs,project_emit/west_libs}.py` by
  tan-cli#868's earlier resync, confirmed by reading the current source
  rather than the resync's own commit message: the required
  `metadata_root` parameter and every one of the ten `_CHIP_SUBSYSTEMS`
  entries are present. `HAND_PORT_PINNED_SDK_COMMIT` still does not move
  -- `scripts/alp_template.py` / `scripts/alp_cli/doctor.py` are
  unchanged between `94378a05` and `eb96112b`, so moving it would either
  red this test for a gap this change does not close, or force their
  table entries to an unaudited value just to stay green. Full reasoning
  is the new comment block directly above `HAND_PORT_PINNED_SDK_COMMIT`'s
  assignment in `test_planner_relocation_freshness.py`, added by this
  same change. The "17 of 19" / "`94378a05`" sentence earlier in this
  same bullet is left exactly as originally written -- a dated
  measurement, not a live claim -- and this paragraph is the current one.

## The mechanism: DATA bound late, LOGIC bound early

`tan` binds alp-sdk's **data** at runtime and alp-sdk's **rules** at port
time. That asymmetry is what makes a duplicated derivation dangerous, and
it is visible in three lines:

```
python/tan/planner/paths.py:29    REPO = sdk_root()
python/tan/planner/paths.py:30    METADATA_ROOT = REPO / "metadata"
```

`REPO` is the bound alp-sdk checkout, not tan's own tree -- that file's
docstring says so at lines 16-18: "`REPO` still means *the alp-sdk
checkout*, never tan's own tree: `metadata/**`, `firmware/`,
`zephyr/sysbuild/` and the git commit all come from the SDK (ADR-0017 --
the generators relocated, the facts did not)."

So a field added to `metadata/**` in alp-sdk reaches a `tan` release with
no tan change at all, at whatever SDK revision the customer has bound.
The Python that decides what that field MEANS reaches tan only when
somebody hand-carries it. Data is instant; logic is manual. Every entry
below is a place where those two halves can be out of step and nothing at
runtime will say so.

### The worked example: `slugs.py` and `_DRIVER_STATUS_SUFFIX`

Verified against both trees.

**The data side is already in the SDK.** At `94378a05`,
`metadata/schemas/som-preset-v1.schema.json:89` declares
`nor_flash_driver_status` and `:94` declares `emmc_driver_status`, and
four presets carry them:

```
metadata/e1m_modules/E1M-V2N101.yaml:36   nor_flash_driver_status: none
metadata/e1m_modules/E1M-V2N101.yaml:38   emmc_driver_status:      none
metadata/e1m_modules/E1M-V2N102.yaml:36   nor_flash_driver_status: none
metadata/e1m_modules/E1M-V2N102.yaml:38   emmc_driver_status:      none
metadata/e1m_modules/E1M-V2M101.yaml:36   nor_flash_driver_status: none
metadata/e1m_modules/E1M-V2M101.yaml:38   emmc_driver_status:      none
metadata/e1m_modules/E1M-V2M102.yaml:32   nor_flash_driver_status: none
metadata/e1m_modules/E1M-V2M102.yaml:34   emmc_driver_status:      none
```

**The reader takes every key it finds.** Both sides walk `on_module`
un-enumerated:

```
scripts/alp_orchestrate/slugs.py:94      for key, val in on_module.items():
python/tan/planner/slugs.py:126          for key, val in on_module.items():
```

That loop is exactly how a new SDK field arrives in tan the instant the
customer's SDK ships it -- nothing in tan names the field, so nothing in
tan has to change for tan to start reading it.

**The filter that makes the field harmless is hand-carried.** It is
present on both sides today:

```
scripts/alp_orchestrate/slugs.py:66      _DRIVER_STATUS_SUFFIX = "_driver_status"
scripts/alp_orchestrate/slugs.py:95      if key in _ON_MODULE_NON_CHIP_FIELDS or key.endswith(_DRIVER_STATUS_SUFFIX):
python/tan/planner/slugs.py:98           _DRIVER_STATUS_SUFFIX = "_driver_status"
python/tan/planner/slugs.py:127          if key in _ON_MODULE_NON_CHIP_FIELDS or key.endswith(_DRIVER_STATUS_SUFFIX):
```

It was not always. `git log -S"_DRIVER_STATUS_SUFFIX" --
python/tan/planner/slugs.py` returns exactly one commit, `59c2b88`, dated
Fri Aug 7 2026, subject `fix(planner): re-sync tan/planner against
alp-sdk 53557a60, closing #320's recurrence (#521)`. Its body states the
consequence verbatim:

> slugs.py: add the `_DRIVER_STATUS_SUFFIX` filter (alp-sdk #1169) so
> `nor_flash_driver_status`/`emmc_driver_status` never read as a chip slug
> -- unfixed, the literal string "none" reached
> `CONFIG_ALP_SDK_CHIP_NONE=y`, an undeclared Kconfig symbol that aborted
> Zephyr configure on every V2N-family SKU.

So the example is real and it is CLOSED, not open: at the measured trees
both halves are in place and the derivation agrees. What it demonstrates
is the shape -- the field arrived through `metadata/**` on its own, the
rule interpreting it did not, and the interval between the two was a
Zephyr configure abort on four SKUs. Every derivation below is another
instance of the same shape waiting for its own upstream change.

## The eight duplicated derivations

### 1. `_e1m_gpio_canonical` -- the 52-name E1M GPIO pad order

| | |
| --- | --- |
| alp-sdk | `scripts/alp_project_emit/__init__.py:52-60` |
| tan | `python/tan/planner/project_emit/__init__.py:34-42` |
| Verdict | **IDENTICAL** -- `diff` clean over both spans, md5 `60bec1805952f7fc9b6367636abaf037` |

The function builds the positional `alp,pin-array` index used by two
emitters. Running the body produces **52** names, `IO0` first and `DAC1`
last: `IO0..IO25` (indices 0..25), `PWM0..PWM7` (26..33),
`ENC0_X/ENC0_Y..ENC3_X/ENC3_Y` (34..41), `ADC0..ADC7` (42..49), `DAC0`,
`DAC1` (50..51).

**Why it stays duplicated.** The order is not in `metadata/**` at all --
it is the ABI of the generated devicetree, whose human-readable source is
the SDK's C headers `include/alp/e1m_pinout.h` / `e1m_x_pinout.h`. Both
package roots make the same claim, in their own wording. alp-sdk
`scripts/alp_project_emit/__init__.py:46-49`:

> this is the "Devicetree / overlay invariant" documented in
> e1m_pinout.h / e1m_x_pinout.h.  Kept here (not duplicated in either
> leaf) so the two overlays cannot drift.

and tan `python/tan/planner/project_emit/__init__.py:23-26`:

> the "Devicetree / overlay invariant" documented in the SDK's
> `e1m_pinout.h` / `e1m_x_pinout.h`. Kept here, not duplicated in either
> leaf, so the two overlays cannot drift.

Both sentences describe the INTRA-repo duplication that was already
removed. The cross-repo one is a different problem neither addresses.

Emitting it once from alp-sdk would mean two things tan-cli#270 exists to
avoid. First, alp-sdk's own `dts.py` and `native_sim.py` consume it
in-process, so an emitted artefact would have the SDK reading a file it
generated. Second, `tan generate` renders all twelve targets in-process
rather than spawning `scripts/alp_project.py` -- proved by
`test_the_in_process_path_loads_none_of_the_sdks_python` in
`python/tests/parity/test_planner_emit_parity.py` -- so making tan ask
alp-sdk for the list would reintroduce a subprocess on the hot path for
52 strings.

It retires the same way `peripheral_kconfig` did: when the order is
declared in a registry under `metadata/registries/` and both repos become
loaders of it. Nothing smaller helps.

### 2. `_e1m_x_gpio_canonical` -- the 99-name E1M-X GPIO pad order

| | |
| --- | --- |
| alp-sdk | `scripts/alp_project_emit/__init__.py:63-76` |
| tan | `python/tan/planner/project_emit/__init__.py:45-58` |
| Verdict | **IDENTICAL** -- `diff` clean, md5 `ce20ab7ec503e5f5475414147ef3c11e` |

Counted the same way: **99** names, `IO0` first and `LCD_VSYNC` last.
`IO0..IO35` (0..35), `PWM0..PWM7` (36..43), `ENC0_X..ENC3_Y` (44..51),
`ADC0..ADC7` (52..59), `DAC0`/`DAC1` (60..61), then `I2C2_SDA`,
`I2C2_SCL`, `I2C3_SDA`, `I2C3_SCL`, `SPI2_MISO`, `SPI2_MOSI`,
`SPI2_SCLK`, `SPI2_CS0`, `SPI2_CS1`, `CAN1_H`, `CAN1_L` (62..72),
`LCD_B0..LCD_B23` (73..96), `LCD_HSYNC`, `LCD_VSYNC` (97..98).

It is listed separately from derivation 1 rather than folded into it
because the two are reached by DIFFERENT branches -- alp-sdk
`native_sim.py:55-56` selects on `_sku_form_factor(sku) == "e1m-x"`, tan
`native_sim.py:68-69` on the equivalent `ff == "e1m-x"` -- so a checkout
with no E1M-X board exercises one and not the other. A parity run over an
`examples/` tree that happened to be all-AEN would confirm 52 and say
nothing about 99.

**Why it stays duplicated.** Identical to derivation 1, and it retires
with it.

### 3. `_PERIPH_DT_WIRING` -- carrier peripheral DT-wiring catalog

| | |
| --- | --- |
| alp-sdk | `scripts/alp_project_emit/dts.py:260-338` |
| tan | `python/tan/planner/project_emit/dts.py:266-344` |
| Verdict | **IDENTICAL** -- `diff` clean, md5 `4ad76fa4539652f0903154dbcf6d2481`; the 27-line explanatory comment above each table is byte-identical too |

One family key, `"aen"`, carrying four peripherals: `i2c`, `gpio`, `adc`,
`i3c`. The values are literal devicetree source -- `pinmux =
<PIN_P5_6__I2C2_SCL_C>, <PIN_P5_7__I2C2_SDA_C>;`, `clock-frequency =
<I2C_BITRATE_STANDARD>;`, `alp-i2c0 = &i2c2;`, `pinmux =
<PIN_P7_6__LPI3C_SDA_B>;`, `alp-i3c0 = &lpi3c0;` -- plus the
`#include`s each fragment needs. The catalog is keyed by the value
`_sku_family()` returns, which is derivation 5.

**Why it stays duplicated.** These are code-generation templates, not
facts. A metadata home for them means inventing a schema that can express
a self-contained DTS fragment (the comment notes the fragments rely on DT
permitting repeated `/{}` and `&label{}` sections), and then a renderer
for that schema in both repos -- which is a larger duplicated derivation
than the one it replaces. The one genuinely factual part, which pad pair
carries LPI3C on the E1M-AEN801, is already argued in the 15-line comment
at alp-sdk `dts.py:296-310` / tan `dts.py:302-316` as a BOARD constraint
that no current metadata field expresses: "The E1M-AEN801 routes only
that pair, so on THIS SoM exactly one node may be enabled and firmware
picks the owner -- a board constraint, not a silicon one."

The near-term guard is not deduplication, it is that both sides' bytes
are pinned: alp-sdk `scripts/alp_project_emit/dts.py` is
`HAND_PORT_HASHES["scripts/alp_project_emit/dts.py"] =
cb6d4278e2fc886a23c28f2ef30b4ae9714738071219f7c29cbccbbeb1bc1782`, which
matches the frozen tree.

### 4. `_CHIP_SUBSYSTEMS` -- chip slug to Zephyr subsystem CONFIG_* keys

| | |
| --- | --- |
| alp-sdk | `scripts/alp_project_emit/__init__.py:85-166` |
| tan | `python/tan/planner/slugs.py:199-280` |
| Verdict | **IDENTICAL** -- `diff` clean, md5 `354704f7e86cc1303f37eb825348c7f3`; **70** slugs on each side, counted by grep over the extracted spans |

Note that the two tables are in DIFFERENTLY NAMED files. Upstream it sits
in the `alp_project_emit` package root; in tan it moved to `slugs.py`,
which says why at lines 13-19 and again at 188-198. Its consumer is the
same on both sides: alp-sdk `alp_orchestrate/kconfig.py:1852` and tan
`kconfig.py:1922`, both `_emit_chips(project, _CHIP_SUBSYSTEMS)`.

**Why it stays duplicated.** tan's own comment states the debt plainly at
`slugs.py:191-198`, and re-measuring confirms it:

> DEBT, stated plainly: this IS a hardware fact and it now lives in `tan`
> (ADR-0017 / I-26 says facts stay in `metadata/**`). It moved because no
> metadata file carries it -- a chip manifest's `bus: i2c` cannot
> distinguish `lsm6dso` -> `("I2C",)` from `tas2563` -> `("I2C",
> "GPIO")`, so deriving it from existing metadata would change the emitted
> Kconfig.

Both halves of that check out in the table: `"lsm6dso": ("I2C",)` at tan
`slugs.py:209` / alp-sdk `__init__.py:95`, and `"tas2563": ("I2C",
"GPIO")` at tan `slugs.py:207` / alp-sdk `__init__.py:93`. Deriving the
tuple from a manifest's declared bus would silently drop the `GPIO` from
`tas2563` and the emitted `CONFIG_GPIO=y` with it.

The retirement path is named and the precedent already exists: the
sibling table `_PERIPHERAL_KCONFIG` IS single-sourced, read on both sides
from `metadata/registries/peripheral-kconfig.json`, whose own
`_comment` field says "Consumed by scripts/alp_project.py and
scripts/alp_orchestrate/slugs.py so per-slice config emitters do not
duplicate this table." The equivalent file for chips --
`metadata/registries/chip-subsystems.json` -- **does not exist at
`94378a05`** (checked). Writing it in alp-sdk and turning both tables
into loaders retires this entry in one change, and it is the single
highest-value item on this list: 70 hardware facts, the largest duplicated
payload of the eight.

It is also invisible to tan's own I-26 gate. `python/tests/gates/
test_no_new_hardware_facts.py:30-36` matches five patterns -- `E1M-[A-Z0-9]+`,
`AE822[A-Z0-9]*`, `GD32G[A-Z0-9]*`, `addr_7bit`,
`CONFIG_ALP_SDK_WIFI_[A-Z0-9]+` -- and a chip slug such as `lsm6dso`
matches none of them, which is why `slugs.py` does not appear in that
gate's `ALLOWED` dict at all. The gate is not wrong; its stated scope is
narrower than this table.

### 5. `_sku_family` / `_sku_family_slug` -- SKU prefix to family directory

| | |
| --- | --- |
| alp-sdk | `scripts/alp_project_loader.py:50-58` and `scripts/gen_zephyr_board.py:225-232` |
| tan | `python/tan/planner/som_metadata.py:48-56` and `python/tan/planner/zephyr_board.py:257-264` |
| Verdict | **AGREE**. `_sku_family` is byte-identical (md5 `2c366bc2f4503c64a8649f650594abe7`). `_sku_family_slug` differs in exactly three DOCSTRING lines, each naming its own repo's sibling; the four code lines are the same characters. |

Four copies, two per repo. All four carry the same regex and the same
mapping, verbatim:

```
_SKU_FAMILY = re.compile(r"^E1M-(AEN|V2N|V2M|NX9)")
{"AEN": "aen", "V2N": "v2n", "V2M": "v2n-m1", "NX9": "imx93"}[m.group(1)]
```

The one behavioural difference is the exception type -- `_sku_family`
raises `ValueError(f"unrecognised SoM SKU pattern: {sku}")` and
`_sku_family_slug` raises `ZephyrBoardEmitError(f"unrecognised SoM SKU
pattern: {sku!r}")`, note the `!r` -- and that difference is **the same
on both sides**, so it is intra-repo shape, not cross-repo drift. tan
inherited it deliberately: `zephyr_board.py:258-260` says "kept
independent here, as it was in alp-sdk, so this module carries no
import-cycle risk on the fuller SKU-family table; the two must agree,
pinned by the test."

**Why it stays duplicated.** The intra-repo half is import-cycle
avoidance, stated upstream and copied to keep the port diffable. The
cross-repo half stays because the MAPPING has no on-disk source, and
alp-sdk `alp_project_loader.py:44-46` says so and names the coupling:

> The SKU-prefix -> family-dir map is a small, pure derivation with no
> second on-disk source, so we keep it inline.  When a new SoM family
> lands, add the entry here + update the schema's `som.sku` pattern.

Half of it IS already in `metadata/**`, which sharpens the cost rather
than excusing it. `metadata/schemas/board.schema.json:84` pins the
prefixes, verbatim and unwrapped:

```
"pattern": "^E1M-(AEN[3-8][0-9]{2}|V2N[0-9]{3}|V2M[0-9]{3}|NX9[0-9]{3})$",
```

What no file carries is the right-hand side -- that
`V2M` resolves to the directory `v2n-m1` and `NX9` to `imx93`, neither of
which is derivable from the prefix. Retiring this needs the registry AND
that schema pattern to move together, in alp-sdk, before tan can become a
reader.

Worth recording because it changes the cost estimate: the
ask-the-SDK-at-runtime path already exists and is already used, just not
on the hot path. `python/tan/commands/new_som_cmd.py:369-385`
(`_resolve_sku_family`) shells the TARGET SDK's own
`alp_project_loader._sku_family` rather than reproducing the table, and
the module comment at lines 66-71 explains that choice. The planner does
not do the same because the planner's whole reason for existing after
tan-cli#270 is that it runs in-process without spawning the SDK.

### 6. `_SOC_FAMILY_TOKEN` -- family directory to library hw_backends token

| | |
| --- | --- |
| alp-sdk | `scripts/alp_project_emit/west_libs.py:35-40` |
| tan | `python/tan/planner/libraries.py:55-60` |
| Verdict | **IDENTICAL** -- `diff` clean over both six-line spans, including the trailing comment |

Four entries, and the same `§D.lib.loader` comment introduces both:

```
"aen":    "alif_ensemble",
"v2n":    "renesas_rzv2n",
"v2n-m1": "renesas_rzv2n",     # DEEPX add-on; HW-acc tokens still resolve via host family.
"imx93":  "nxp_imx9",
```

Both sides consume it three lines later in the same shape -- alp-sdk
`west_libs.py:95-98`, tan `libraries.py:120-123` -- feeding
`_sku_family()`'s output straight into `.get(family)`.

**Why it stays duplicated.** This is the *closest* of the eight to being
trivially emittable, which is exactly why it needs saying out loud: the
right-hand side of the map is already written down in the SDK, in every
library manifest's `integration.zephyr.hw_backends` block, and the
left-hand side is derivation 5's output. What is missing is not the
facts, it is a declared mapping between them -- nothing reads the
manifests to build the reverse index, so both repos hand-write the four
lines instead.

It stays for a second reason worth weighing against fixing it in
isolation: tan's copy lives in `libraries.py` specifically so
`kconfig.py` can import it without dragging `alp_project` in
(`libraries.py:26-33`), and alp-sdk's lives in `west_libs.py` next to the
`--emit west-libraries` half that did NOT relocate. Any single-sourcing
has to keep both of those constraints. Four lines is not much payload,
but a wrong token silently emits no HW-backend Kconfig at all -- both
sides `return []` when `.get(family)` is `None` (alp-sdk
`west_libs.py:99-100`, tan `libraries.py:124-125`), so a missed entry
fails quiet, not loud.

### 7. The Ethos-U accelerator hardcode -- present on both sides, dead on both

| | |
| --- | --- |
| alp-sdk | produced at `scripts/gen_zephyr_board.py:907,909`; consumed at `:1357` |
| tan | produced at `python/tan/planner/zephyr_board.py:936,938`; consumed at `:1388` |
| Verdict | **IDENTICAL, and DEAD ON BOTH.** `_aen_ethos_u` (alp-sdk `:889-910`, tan `:918-939`) is byte-identical including its 15-line docstring, md5 `66765a592fb74855929a9180ea910fe0`. Both consumers destructure the same way. |

`_aen_ethos_u` returns `("ETHOS_U85_256", "ethosu85")` or
`("ETHOS_U55_256", "ethosu55")`, derived from
`caps.get("ethos_u85_count")` / `caps.get("ethos_u55_count")` on the SoC
spec. The single consumer of that tuple discards the first element on
both sides:

```
scripts/gen_zephyr_board.py:1357        _accel, node = ethos_u
python/tan/planner/zephyr_board.py:1388 _accel, node = ethos_u
```

Grepping both trees for the produced strings confirms `_accel` is never
read: `ETHOS_U85_256` and `ETHOS_U55_256` appear in
`scripts/gen_zephyr_board.py` only at the two `return` sites, and
elsewhere only in an unrelated allowlist (`scripts/alp_orchestrate/
kconfig.py:1193-1195`, mirrored at `python/tan/planner/kconfig.py:
1275-1281`) and in hand-written `examples/aen/*/prj.conf` files. Only the
second element -- the DT node label -- reaches the emitted board.

**Why it stays duplicated.** It is not a metadata problem, and that is the
point: the accelerator string is already derivable from the SDK's own
`metadata/socs/**` capability counts, which is where the function reads
its input from. It stays because it is dead weight in a HAND-PORT that
must stay diffable. Deleting the first tuple element on tan's side alone
would make `python/tan/planner/zephyr_board.py` diverge from
`scripts/gen_zephyr_board.py` for no behavioural gain, complicating the
next re-sync and reddening the symbol-level correspondence that
`python/tests/gates/test_hand_port_tan_side.py` exists to grow. The
correct order is upstream-first: drop it in alp-sdk, then re-sync tan and
bump the pin. Until then it is correctly classified as duplicated-but-not-
drifted, and it is the cheapest item on this list to retire.

### 8. The `TBD` placeholder sentinel -- the one live disagreement

| | |
| --- | --- |
| alp-sdk | `scripts/sentinels.py:22-30` -- ONE definition, imported by NINE modules |
| tan | EIGHT sites under `python/tan/planner/`, plus a NINTH definition with different semantics at `python/tan/core/pending.py:34-40` |
| Verdict | **DISAGREE.** The eight planner sites match alp-sdk exactly. `tan/core/pending.py` does not: `"tbd"` resolves `True` upstream and `False` there. |

alp-sdk normalises in one place:

```
scripts/sentinels.py:30   return isinstance(value, str) and value.strip().upper() == "TBD"
```

and imports it from nine modules -- `scripts/alp_project_loader.py:31`,
`scripts/gen_zephyr_board.py:81`, `scripts/check_e1m_pinout.py:48`,
`scripts/check_pin_conflicts.py:39`, `scripts/alp_orchestrate/
carveout.py:22`, `.../partition.py:23`, `.../loader.py:31`,
`.../kconfig.py:48`, `.../slugs.py:16`. Grepping the frozen tree for an
inline `.strip().upper() == "TBD"` finds exactly one hit, and it is
`sentinels.py:30` itself.

tan has no `sentinels.py`, by decision --
`python/tan/planner/zephyr_board.py:104-107` states it:

> RELOCATED spelling of alp-sdk `scripts/sentinels.py::is_tbd` (alp-sdk
> #1048), which has no counterpart under `tan/planner/`: it is imported
> upstream from both `scripts/` and `alp_orchestrate/`, and relocating it
> would add a module to the hand-port audit for one three-line function.

The consequence is eight sites:

| tan site | spelling | alp-sdk counterpart |
| --- | --- | --- |
| `planner/zephyr_board.py:112` (in `_is_tbd`, def at `:101`) | `value.strip().upper() == "TBD"` | `gen_zephyr_board.py:132,402,421,440` via `is_tbd` |
| `planner/slugs.py:48` (in `_is_tbd`, def at `:42`) | `value.strip().upper() == "TBD"` | `alp_orchestrate/slugs.py:86,130` via `is_tbd` |
| `planner/carveout.py:134` | `controller.strip().upper() == "TBD"` | `alp_orchestrate/carveout.py:133` `is_tbd(controller)` |
| `planner/carveout.py:182` | `base.strip().upper() == "TBD"` | `alp_orchestrate/carveout.py:177` `is_tbd(base)` |
| `planner/partition.py:202` | `cap.strip().upper() == "TBD"` | `alp_orchestrate/partition.py:204` `is_tbd(cap)` |
| `planner/som_metadata.py:143` | `declared.strip().upper() == "TBD"` | `alp_project_loader.py:323` `is_tbd(declared)` |
| `planner/loader.py:258` | `declared.strip().upper() == "TBD"` | `alp_orchestrate/loader.py:217` `is_tbd(declared)` |
| `planner/kconfig.py:303` | `provider.upper() == "TBD"` | `alp_orchestrate/kconfig.py:297` `is_tbd(provider)` |

All eight agree with alp-sdk. The last is spelled differently and is
still equivalent, but only because `kconfig.py:299` already did
`provider = provider.strip()` two lines above -- the comment at `:300-302`
says so. That is the kind of local reasoning a ninth copy has to redo.

**The disagreement.** `python/tan/core/pending.py:40` is:

```
return isinstance(value, str) and value.strip() == PENDING_PLACEHOLDER
```

with `PENDING_PLACEHOLDER = "TBD"` at `:31`. No `.upper()`. So
`is_pending_placeholder("tbd")` is `False` where `is_tbd("tbd")` is
`True`, and the same for `"Tbd"`. Its consumers are
`tan/core/size.py:344`, `tan/core/flash_plan.py:906` and
`tan/commands/size_cmd.py:294`.

**Why it stays duplicated, and why the fix is a judgement call rather
than a port.** The narrowness is deliberate and argued in that module's
own docstring at `pending.py:19-24`, verbatim:

> Trimmed before comparing -- a YAML `device: "  TBD  "` is the same
> unfilled field -- but deliberately NOT case-folded and NOT a substring
> test: `TBD-1234-XYZ` is a plausible part number and
> `flash_args.build_dir: /opt/TBDtool/x` a plausible path, and refusing
> either would block a legitimate value. `tbd` lowercase is not the
> sentinel alp-sdk emits; widening to it means widening the SDK's
> convention first, in one place, not here.

`pending.py` is a tan-native module (tan-cli#276), not a port of
`sentinels.py`, and its call sites are flash and size planning, where a
false positive refuses a legitimate flash. The planner's call sites are
codegen, where a false negative emits a placeholder as a real value. Two
different failure costs, two different answers, both defensible. This
entry is therefore recorded as **an open decision for alp-sdk to make
first**: either the SDK's `TBD` convention is case-insensitive everywhere
(in which case `pending.py` widens and the whole thing single-sources) or
it is not (in which case the eight planner sites are the ones that are
too wide). Nothing tan can do alone resolves it correctly.

**Two further tan-side spellings, outside the eight.** Recorded because
they change the shape of the problem from "two normalisations" to
"three", and neither was in the issue's measurement:

- `python/tan/commands/presets_cmd.py:216` -- `value.strip() == "TBD"`,
  the same rule as `pending.py` written out again rather than imported.
- `python/tan/commands/pinmux_cmd.py:240` -- `e1m_pad == "TBD"`, no
  normalisation at all. This one is a KNOWN follow-up, not an oversight:
  `tan/core/flash_plan.py:898` already names it, "so `tan.core.size` (and
  `pinmux`, once ported) can read the same rule".
- `python/tan/core/image_bundle.py:40` -- `PENDING_SENTINEL = "TBD"`, a
  second literal, also already declared open at `flash_plan.py:902-904`:
  "`tan image`'s own `image_bundle.PENDING_SENTINEL` is still a separate
  `"TBD"` literal; pointing it at the same module too is a follow-up
  outside flash_plan.py."

## Summary

| # | Derivation | alp-sdk | tan | Verdict |
| --- | --- | --- | --- | --- |
| 1 | `_e1m_gpio_canonical` (52 names) | `scripts/alp_project_emit/__init__.py:52-60` | `python/tan/planner/project_emit/__init__.py:34-42` | identical |
| 2 | `_e1m_x_gpio_canonical` (99 names) | `scripts/alp_project_emit/__init__.py:63-76` | `python/tan/planner/project_emit/__init__.py:45-58` | identical |
| 3 | `_PERIPH_DT_WIRING` | `scripts/alp_project_emit/dts.py:260-338` | `python/tan/planner/project_emit/dts.py:266-344` | identical |
| 4 | `_CHIP_SUBSYSTEMS` (70 slugs) | `scripts/alp_project_emit/__init__.py:85-166` | `python/tan/planner/slugs.py:199-280` | identical |
| 5 | `_sku_family` / `_sku_family_slug` | `scripts/alp_project_loader.py:50-58`, `scripts/gen_zephyr_board.py:225-232` | `python/tan/planner/som_metadata.py:48-56`, `python/tan/planner/zephyr_board.py:257-264` | agree (code identical; 3 docstring lines differ) |
| 6 | `_SOC_FAMILY_TOKEN` | `scripts/alp_project_emit/west_libs.py:35-40` | `python/tan/planner/libraries.py:55-60` | identical |
| 7 | Ethos-U accelerator hardcode | `scripts/gen_zephyr_board.py:907,909`, consumed `:1357` | `python/tan/planner/zephyr_board.py:936,938`, consumed `:1388` | identical, dead on both |
| 8 | `TBD` sentinel | `scripts/sentinels.py:22-30` (1 def, 9 importers) | 8 planner sites + `python/tan/core/pending.py:34-40` | **disagree** on `"tbd"` |

Ordered by what it would take to retire each:

- **Cheapest, upstream-only:** 7. Delete the unused tuple element in
  alp-sdk, re-sync, bump the pin.
- **Needs one new registry file:** 4 (`metadata/registries/
  chip-subsystems.json`, absent today, precedent set by
  `peripheral-kconfig.json`), then 1 and 2 (the pad order), then 6.
- **Needs a registry AND a schema change in lockstep:** 5. The other half
  of the map is the `som.sku` pattern at
  `metadata/schemas/board.schema.json:84` -- the same four prefixes the
  four Python copies branch on, already machine-readable and already in
  `metadata/**`; only the prefix-to-directory mapping is missing.
- **Needs a decision before any code moves:** 8.
- **Probably should not be single-sourced at all:** 3, where the payload
  is DTS source text and a metadata schema for it would be a bigger
  duplicated derivation than the table.

## What catches drift today, and where the net has holes

None of the eight is unguarded, and that is why "duplicated" is tolerable
rather than urgent. What guards them:

- **`PINNED_HASHES`** (`test_planner_relocation_freshness.py:516` onward)
  pins the sha256 of all 21 `scripts/alp_orchestrate/*.py` at
  `PINNED_SDK_COMMIT = 722320a1abe3cea675e99e97300b8a484b4e8464` (moved from
  `94378a056549c7377d714a7f2b68878aca8fea01` by an earlier PR, after this
  document was written, then to `eb96112ba7d1cc3b4084c985962ea31772177d74`,
  then to the current value by tan-cli#996/#1001). Covers derivations 4
  (via `slugs.py`, `339bffdb…`) and 6 (via `libraries.py`) and the six
  planner `TBD` call sites.
- **`HAND_PORT_HASHES`** (same file, `:824-844`) pins 19 sources outside
  `alp_orchestrate/` at `HAND_PORT_PINNED_SDK_COMMIT =
  722320a1abe3cea675e99e97300b8a484b4e8464` -- the same commit as
  `PINNED_SDK_COMMIT` above, for the first time since `1a9f753c`
  (tan-cli#996/#1001 advanced it from `88318e759958529fbbd8fe9d481373681c0fa78d`,
  closing tan-cli#913). Covers derivations 1, 2, 3, 5,
  6 and 7 -- and `scripts/sentinels.py` itself
  (`54c0b5c4211a638f1a6141340e76b2bc7e32935b8c61ba5e8948e2da1ab81d9c`,
  1186 bytes), which is in that table specifically because it has no tan
  file of its own to track. All six of those source hashes still match the
  frozen `94378a05` tree.
- **The byte-parity suite** (`python/tests/parity/
  test_planner_emit_parity.py`) imports both planners into one process and
  compares every emit for every `board.yaml` in the SDK's `examples/`. Any
  of derivations 1-6 drifting in a way that changes output reds here by
  name, file and first differing line.

Where the net does not reach, measured rather than assumed:

1. **`HAND_PORT_HASHES` audits only the upstream side.** That is
   tan-cli#778's finding verbatim, in
   `python/tests/gates/test_hand_port_tan_side.py:2-11`: a pin can be
   green while tan's counterpart is pre-fix, which is how `tan model
   build` shipped corrupted DRP-AI options. That gate now pins the
   MAPPING, plus one symbol-level check (`explain.py`) as the template;
   the rest is its own follow-up.
2. **The `sentinels.py` mapping is incomplete.**
   `test_hand_port_tan_side.py:146-152` declares
   `scripts/sentinels.py` unpairable with the reason "`sentinels.is_tbd`
   is spelled `_is_tbd` inside tan/planner/zephyr_board.py". Measured,
   that is one of EIGHT tan sites, and the reason names none of the other
   seven -- nor `tan/core/pending.py`, the one that actually disagrees.
   The gate's `test_every_unpairable_reason_names_the_tan_file_it_lives_in`
   is satisfied by naming one real file, so nothing there is failing; the
   picture it paints is just narrower than the code.
3. **The parity suite is gated on an SDK checkout that is going away.**
   It skips without `ALP_SDK_ROOT`, and
   `python/tests/parity/test_planner_parity_actually_ran.py:23-26` records
   that the relocation's STATED end state is alp-sdk no longer shipping
   `scripts/alp_orchestrate/` at all. Coverage of `tan/planner/**` is 83%
   with that suite and 27% without it. When the oracle disappears, the
   hash pins and the frozen oracle captures are what remain, and neither
   compares two live implementations.
4. **The I-26 gate cannot see derivations 4, 5 or 6.** Its five patterns
   (`test_no_new_hardware_facts.py:30-36`) match SKUs, two part-number
   families, `addr_7bit` and one Kconfig prefix. A chip slug
   (`lsm6dso`), a family directory (`v2n-m1`) and a SoC token
   (`renesas_rzv2n`) match none, which is why neither `slugs.py` nor
   `libraries.py` appears in its `ALLOWED` dict despite carrying 74
   hardware facts between them.
5. **Three pins, tracked independently -- two of the three currently share a
   commit, by coincidence of timing, not by mechanism.**
   `PINNED_SDK_COMMIT` and `HAND_PORT_PINNED_SDK_COMMIT` are both
   `722320a1abe3cea675e99e97300b8a484b4e8464` as of tan-cli#996/#1001 (at
   the time this document was written, `PINNED_SDK_COMMIT` was `94378a05`
   and `HAND_PORT_PINNED_SDK_COMMIT` was held at `88318e75`; the two later
   moved together for the first time since `1a9f753c`). The third,
   `STRICT_LOADERS_PINNED_SDK_COMMIT`
   (`26b0040e9a762c16aff5c7c53b2e19cc7583b2a4`), remains behind. All three
   are deliberately independent tables -- `test_planner_relocation_freshness.py:57-69`
   explains why a shared pin would certify unaudited drift away -- so the
   consequence still holds even while two read the same value: a change
   landing between two of them is audited by one table and not the other,
   and the next re-pin of either `PINNED_SDK_COMMIT` or
   `HAND_PORT_PINNED_SDK_COMMIT` alone will likely split them apart again.

## Adjacent duplications this inventory does NOT count

Recorded so a later reader does not mistake an omission for a miss.

- **`_valid_accel`** -- the eight-entry Ethos-U configuration allowlist at
  `scripts/alp_orchestrate/kconfig.py:1193-1195` and
  `python/tan/planner/kconfig.py:1275-1281`. Not counted separately: it
  is inside `kconfig.py`, one of the 21 files `PINNED_HASHES` covers as a
  whole-file relocation, so it is part of the mirror rather than a
  standalone hand-carried derivation.
- **`_ON_MODULE_NON_CHIP_FIELDS`** -- `scripts/alp_orchestrate/
  slugs.py:43-57` and `python/tan/planner/slugs.py:75-89`, verified
  identical while measuring the `slugs.py` example. Same reason: it rides
  inside a `PINNED_HASHES` file.
- **`_sku_form_factor`** -- `scripts/alp_project_loader.py:61-63` and
  `python/tan/planner/project_loader.py:73-75`. Identical, but it is a
  one-line consumer of derivation 5 rather than a fact of its own; fixing
  5 fixes it.
- **The whole of `tan/planner/`** is a duplicate of
  `scripts/alp_orchestrate/` by construction. That is the relocation, not
  drift, and it is what `PINNED_HASHES` exists to police. This document
  is about the derivations that are duplicated ON TOP of that -- the ones
  hand-carried out of files the relocation did not move.
