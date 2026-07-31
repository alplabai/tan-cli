// SPDX-License-Identifier: Apache-2.0
//! `flash_args` typed accessors. Shared by every backend plan-builder; a
//! non-mapping `flash_args` VALUE (the AEN701 helper's `flash_args: TBD`
//! string) reads as an empty map -- every accessor below returns "absent"
//! for it. That is the only tolerance left: the bool/int accessors for a
//! PRESENT key are STRICT (`fa_bool_checked`/`fa_int_checked`) -- a
//! wrong-shaped scalar (e.g. a quoted `"true"`/`"921600"`) hard-errors
//! instead of silently falling back to the caller's default. There is no
//! tolerant `fa_bool`/`fa_int` dual left to reach for at the next new key.

use serde_yaml::Value;

fn fa_get<'a>(v: &'a Value, k: &str) -> Option<&'a Value> {
    v.as_mapping()?
        .iter()
        .find(|(key, _)| key.as_str() == Some(k))
        .map(|(_, val)| val)
}

/// The SDK's pending-placeholder sentinel, defined ONCE (tan-cli#222).
///
/// `TBD` is a deliberate alp-sdk convention: where the exact hardware
/// configuration is not yet known the field is marked `TBD` rather than
/// invented. So the convention itself PRODUCES the value, routinely, in files
/// tan reads — this is not malformed input, it is expected input.
///
/// The literal used to be written out at four sites (here,
/// [`flash_args_has_tbd`], `commands::image`'s `firmware_path` check, and
/// `sdk_catalogue::parse::is_tbd` — which is the copy four comments elsewhere
/// cite BY NAME as the definition of the convention). One definition instead,
/// because the failure mode of four copies is a fifth reader written without
/// one. That is not hypothetical here: `builders::fa_str_checked` WAS that
/// fifth reader, and it still carried the empty-only filter after the first
/// pass at #222 fixed [`fa_str`].
///
/// Two readers outside the flash path are knowingly NOT routed through this,
/// because they ask a different question of a different schema rather than
/// copying this one: `pinmux` drops a `TBD` `e1m_pad` as a sentinel ROW (the
/// silicon pad has no E1M edge ball at all), and `size::resolve_variant` skips
/// a `TBD` `silicon_variant` before falling through to a reverse SKU lookup.
/// Neither plans a flash write. Both compare untrimmed — tracked as #276, which
/// also carries the question of whether this constant belongs somewhere more
/// neutral than the flash backend it happens to sit in.
pub const PENDING_PLACEHOLDER: &str = "TBD";

/// Whether `s` is the pending-placeholder sentinel rather than a value.
///
/// Trims first: `mode: ' TBD '` is the same unfilled field as `mode: TBD`.
pub fn is_pending_placeholder(s: &str) -> bool {
    s.trim() == PENDING_PLACEHOLDER
}

/// Whether `flash_args` carries an unresolved `TBD` value anywhere in it -- a
/// bare `TBD` scalar, or a mapping/sequence value that trims to `TBD` (e.g.
/// `mode: TBD`, `device: TBD`). Recurses `Mapping`/`Sequence` directly over
/// `serde_yaml::Value`; no transcode.
///
/// Deliberately broader than `image.rs`'s `firmware_path` check (which only
/// inspects that single consumed field): a `TBD` ANYWHERE in `flash_args`
/// means the entry isn't finalised yet under the SDK's `TBD`
/// pending-placeholder convention, not just an unresolved firmware path. Do
/// not narrow this back to a single known key.
pub fn flash_args_has_tbd(v: &Value) -> bool {
    match v {
        Value::String(s) => is_pending_placeholder(s),
        Value::Mapping(m) => m.values().any(flash_args_has_tbd),
        Value::Sequence(a) => a.iter().any(flash_args_has_tbd),
        _ => false,
    }
}

/// A usable string sub-key; `None` when absent, empty, non-string, **or the
/// pending placeholder**.
///
/// The `TBD` clause is the fix for tan-cli#222, and it belongs HERE rather than
/// in the twelve call sites this accessor serves. (It is NOT every string read
/// in the flash path: `plan_swd_probe` takes four fields — `base`,
/// `jlink_device`, `interface`, `target` — through the strict
/// `builders::fa_str_checked`, which needed its own answer and got the opposite
/// one, an error rather than absent. See its doc comment.)
/// `TBD` is not empty, so the previous
/// `!s.is_empty()` filter returned it as a real value and every backend
/// received the literal string `TBD` as a runner name, a build directory, a hex
/// file, an OpenOCD config path or a `dd` destination. That is the same defect
/// measured in alp-sdk (`flash/mod.rs:307`, where `.filter(|s| !s.is_empty())`
/// let `TBD` resolve to `<build_root>/TBD` and a real flasher was spawned
/// against it) — in tan's shared accessor, so once, for every backend at once.
///
/// **Absent, not an error**, deliberately:
///
/// * `TBD` MEANS "not yet known", and that is exactly what absent means. The
///   caller's existing `unwrap_or_else(default)` / `if let Some` then handles it
///   as the unfilled field it is — no new branch anywhere.
/// * It matches what tan already does with the whole entry: `commands::flash`
///   SKIPS a target whose `flash_args` contains `TBD` rather than failing it
///   (`tbd_flash_args_on_a_registered_backend_skips_not_fails`).
/// * It matches the sibling accessors, which already read a key off a bare
///   `flash_args: TBD` as `Ok(None)`.
///
/// Every call site was audited for whether "absent" is safe there, and each is
/// either already guarded by a closed-set check that `TBD` also failed
/// (`plan_yocto_wic`'s required-`target` + `/dev/` prefix refusal,
/// `plan_xspi_flashwriter`'s `mtd0`/`mtd1`) or strictly improves — `build_dir`
/// now falls back to the computed Zephyr build dir instead of a literal `TBD`
/// directory, `target` to `flash`, `bs` to `4M`. No site was found where absent
/// is more dangerous than the placeholder was.
pub(super) fn fa_str(v: &Value, k: &str) -> Option<String> {
    fa_get(v, k)
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty() && !is_pending_placeholder(s))
        .map(str::to_string)
}

/// Strict bool accessor for every behaviour-affecting `flash_args` bool key
/// (e.g. `reset`, `erase`, `use_openocd`, `use_pyocd`, `confirm`, `verify`).
/// `Value::as_bool` reads a quoted string like `"false"` as `None`, same as
/// absent -- a tolerant `unwrap_or(default)` reader would then silently
/// apply the caller's default and program the OPPOSITE of what was written.
/// `Ok(None)` only for genuinely absent/null (caller applies its default);
/// any other non-bool shape hard-errors instead of defaulting. `0`-means-
/// absent semantics do not apply here (a bare bool has no such quirk).
pub(super) fn fa_bool_checked(v: &Value, k: &str) -> Result<Option<bool>, String> {
    let Some(raw) = fa_get(v, k) else {
        return Ok(None);
    };
    if raw.is_null() {
        return Ok(None);
    }
    if let Some(b) = raw.as_bool() {
        return Ok(Some(b));
    }
    Err(format!(
        "flash_args.{k} must be a bare boolean (true/false, unquoted; got {raw:?}) -- refusing \
         to silently fall back to a default -- this plans a real flash write."
    ))
}

/// Strict int accessor: mirrors `fa_bool_checked` for every behaviour-
/// affecting `flash_args` int key (e.g. `jlink_speed`, `baud`, `jobs`,
/// `speed`). `Value::as_i64` reads a quoted string like `"8000"` as `None`,
/// same as absent -- a tolerant `unwrap_or(default)` reader would then
/// silently apply the caller's default with no warning. `0`-means-absent
/// semantics: an explicit `0` still yields `None`, i.e. "use the default";
/// any other non-integer shape hard-errors instead of defaulting.
pub(super) fn fa_int_checked(v: &Value, k: &str) -> Result<Option<i64>, String> {
    let Some(raw) = fa_get(v, k) else {
        return Ok(None);
    };
    if raw.is_null() {
        return Ok(None);
    }
    if let Some(n) = raw.as_i64() {
        return Ok((n != 0).then_some(n));
    }
    Err(format!(
        "flash_args.{k} must be a bare number (unquoted; got {raw:?}) -- refusing to silently \
         fall back to a default -- this plans a real flash write."
    ))
}

#[cfg(test)]
mod tests {
    use std::path::Path;

    use super::super::{FlashInputs, plan_yocto_wic};
    use super::*;

    #[test]
    fn non_mapping_flash_args_read_as_empty() {
        // The AEN701 helper's flash_args is the string "TBD" -- every
        // accessor, including the strict `_checked` ones, must read a key
        // off it as genuinely absent (Ok(None)/caller default), not error.
        let fa = Value::String("TBD".to_string());
        assert_eq!(fa_str(&fa, "runner"), None);
        assert_eq!(fa_bool_checked(&fa, "erase").unwrap(), None);
        assert_eq!(fa_int_checked(&fa, "speed").unwrap(), None);
    }

    #[test]
    fn fa_bool_checked_rejects_quoted_string_accepts_bare_bool() {
        // `reset: "false"` (quoted) must NOT silently read as absent -- a
        // tolerant reader would apply the caller's `true` default, flashing
        // with the OPPOSITE of what was written.
        let fa: Value = serde_yaml::from_str("reset: \"false\"").unwrap();
        let err = fa_bool_checked(&fa, "reset").unwrap_err();
        assert!(err.contains("reset"));

        let fa: Value = serde_yaml::from_str("reset: false").unwrap();
        assert_eq!(fa_bool_checked(&fa, "reset").unwrap(), Some(false));

        assert_eq!(fa_bool_checked(&Value::Null, "reset").unwrap(), None);
        let empty = Value::Mapping(Default::default());
        assert_eq!(fa_bool_checked(&empty, "reset").unwrap(), None);
    }

    #[test]
    fn fa_int_checked_rejects_quoted_string_accepts_bare_int_and_zero_is_absent() {
        // `jlink_speed: "8000"` (quoted) must NOT silently read as absent --
        // a tolerant reader would apply the caller's 4000 default with no
        // warning.
        let fa: Value = serde_yaml::from_str("jlink_speed: \"8000\"").unwrap();
        let err = fa_int_checked(&fa, "jlink_speed").unwrap_err();
        assert!(err.contains("jlink_speed"));

        let fa: Value = serde_yaml::from_str("jlink_speed: 9600").unwrap();
        assert_eq!(fa_int_checked(&fa, "jlink_speed").unwrap(), Some(9600));

        // explicit 0 preserves the "0 means use the default" semantics.
        let fa: Value = serde_yaml::from_str("jlink_speed: 0").unwrap();
        assert_eq!(fa_int_checked(&fa, "jlink_speed").unwrap(), None);

        assert_eq!(fa_int_checked(&Value::Null, "jlink_speed").unwrap(), None);
    }

    /// tan-cli#222, the defect itself. The suite passed with `fa_str` returning
    /// `TBD` as a real value, so NOTHING asserted this — a literal `TBD` reached
    /// every backend as a runner name, a build directory, a hex file, an OpenOCD
    /// config path or a `dd` destination.
    #[test]
    fn fa_str_reads_the_pending_placeholder_as_absent_not_as_a_value() {
        let fa: Value =
            serde_yaml::from_str("runner: TBD\ndevice: ' TBD '\nbuild_dir: /real/path\nempty: ''")
                .unwrap();

        // The fix: unfilled reads as unfilled, so the caller's own default or
        // `if let Some` handles it as the absent field it is.
        assert_eq!(fa_str(&fa, "runner"), None);
        // Trimmed, because `mode: ' TBD '` is the same unfilled field.
        assert_eq!(fa_str(&fa, "device"), None);
        // Unchanged for the cases that already worked.
        assert_eq!(fa_str(&fa, "build_dir").as_deref(), Some("/real/path"));
        assert_eq!(fa_str(&fa, "empty"), None);
        assert_eq!(fa_str(&fa, "missing"), None);
    }

    /// The over-match this must NOT become. `TBD` is the WHOLE value or it is
    /// not the placeholder: eating any value that merely contains those three
    /// letters would silently drop real device paths and real runner names,
    /// which is a worse bug than the one being fixed.
    #[test]
    fn a_value_that_merely_contains_tbd_is_still_a_value() {
        let fa: Value =
            serde_yaml::from_str("a: TBD-1\nb: /dev/TBDX\nc: mytbd\nd: TBDTBD\ne: 'TBD extra'")
                .unwrap();
        for key in ["a", "b", "c", "d", "e"] {
            assert!(
                fa_str(&fa, key).is_some(),
                "{key} is a real value and must survive"
            );
        }
        assert!(!is_pending_placeholder("TBD-1"));
        assert!(!is_pending_placeholder("tbd"), "case-sensitive by design");
        assert!(is_pending_placeholder("TBD"));
        assert!(is_pending_placeholder("  TBD\t"));
    }

    /// The customer-visible half, on the path that writes to a block device.
    /// Before the fix `target: TBD` passed `fa_str`, then survived to the
    /// `/dev/` prefix check and was refused with `refusing target 'TBD'` — safe,
    /// but by luck of a second guard rather than by the accessor. It is now the
    /// required-field error, which names the field to fill in.
    #[test]
    fn a_pending_wic_target_is_a_required_field_error_not_a_device_path() {
        let fa: Value = serde_yaml::from_str("target: TBD").unwrap();
        let inp = FlashInputs {
            artefact: Path::new("/build/core-image.wic"),
            flash_args: &fa,
            dry_run: true,
            force_confirm: false,
            core_id: "a55",
            sku: "E1M-V2N101",
        };
        let err = plan_yocto_wic(&inp, |_| true).expect_err("TBD is not a block device");
        assert!(
            err.contains("flash_args.target is required"),
            "must name the unfilled field: {err}"
        );
        assert!(
            !err.contains("refusing target 'TBD'"),
            "must not report the placeholder as an attempted destination: {err}"
        );
    }

    #[test]
    fn flash_args_has_tbd_scans_mapping_sequence_and_bare_scalar() {
        let mapping: Value = serde_yaml::from_str("mode: TBD\ndevice: usb0").unwrap();
        assert!(flash_args_has_tbd(&mapping));

        let nested_seq: Value =
            serde_yaml::from_str("targets:\n- ok\n- nested:\n    val: TBD").unwrap();
        assert!(flash_args_has_tbd(&nested_seq));

        let no_tbd: Value = serde_yaml::from_str("mode: usb\ndevice: usb0").unwrap();
        assert!(!flash_args_has_tbd(&no_tbd));

        let bare = Value::String("TBD".to_string());
        assert!(flash_args_has_tbd(&bare));

        // trims whitespace, matching the trim() == "TBD" comparison.
        let padded: Value = serde_yaml::from_str("mode: ' TBD '").unwrap();
        assert!(flash_args_has_tbd(&padded));
    }
}
