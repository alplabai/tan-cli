- **The generated V2N/V2M `m33_sm` board tree no longer wires `&i2c8`
  (RIIC8/BRD_I2C) as a CM33 Zephyr device.** ADR-0020 lockstep for
  alp-sdk#2288: BRD_I2C moved to the Cortex-A55/Linux side of the SoM (it is
  the on-module housekeeping bus the GD32 supervisor uses, and only Linux
  ever masters it — `metadata/e1m_modules/v2n/core-ownership.yaml`), so the
  CM33 must never open it. `tan/planner/zephyr_board.py`'s `_v2n_dts` no
  longer emits a `brd_i2c` alias or a `pinctrl-0`/`clock-frequency`/`status
  = "okay"` node for it — the node stays present with `status = "disabled"`
  only, so `alp_i2c_open()` on this board resolves no `bus_id` for it.
  `_v2n_defconfig` now asserts `brd_i2c.get("status") != "disabled"` (was
  `!= "enabled"`) and drops `CONFIG_I2C=y` — the on-module GD32G553
  supervisor bridge is SPI-only from the CM33's side now. Ported
  line-for-line from alp-sdk's `scripts/gen_zephyr_board.py`.
