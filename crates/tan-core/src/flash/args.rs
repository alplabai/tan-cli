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
        Value::String(s) => s.trim() == "TBD",
        Value::Mapping(m) => m.values().any(flash_args_has_tbd),
        Value::Sequence(a) => a.iter().any(flash_args_has_tbd),
        _ => false,
    }
}

/// A non-empty string sub-key; `None` when absent, empty, or non-string.
pub(super) fn fa_str(v: &Value, k: &str) -> Option<String> {
    fa_get(v, k)
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
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
