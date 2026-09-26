- **`tan build`'s planner no longer offers `flash_device: ospi0`/`ospi1` on a
  SKU whose preset declares that part not fitted.** ADR-0026 lockstep for
  alp-sdk#2311: `_known_flash_devices()` (`tan/planner/partition.py`) still
  advertised an `on_module.ospi_memories:` key as a legal
  `storage[].flash_device:` target even when the SoM preset marks it
  `assembled: false`, and `_resolve_flash_device()` resolved it to a live
  descriptor from `capacity_mbit` alone — on `E1M-AEN801`, whose preset
  declares `ospi0`/`ospi1` unfitted, a `storage:` entry naming `ospi0`
  silently planned a 32 MiB partition on silicon that carries no such part.

  Both are fixed: `_known_flash_devices()` excludes an `assembled: false` key
  from the advertised set, and `_resolve_flash_device()` refuses it directly
  (defense in depth for a hand-built project bypassing the loader's
  cross-check), naming the SKU, the device, and the declaring preset file.
  `assembled: optional` and an absent key (schema default: `true`) are
  unaffected. Ported line-for-line from alp-sdk's
  `scripts/alp_orchestrate/partition.py`.
