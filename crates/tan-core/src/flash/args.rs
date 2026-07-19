// SPDX-License-Identifier: Apache-2.0
//! `flash_args` typed accessors (tolerant: a non-mapping value reads as empty).
//! Shared by every backend plan-builder; a non-mapping `flash_args` value (the
//! AEN701 helper's `flash_args: TBD` string) reads as an empty map.

use serde_yaml::Value;

fn fa_get<'a>(v: &'a Value, k: &str) -> Option<&'a Value> {
    v.as_mapping()?
        .iter()
        .find(|(key, _)| key.as_str() == Some(k))
        .map(|(_, val)| val)
}

/// A non-empty string sub-key; `None` when absent, empty, or non-string.
pub(super) fn fa_str(v: &Value, k: &str) -> Option<String> {
    fa_get(v, k)
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
        .map(str::to_string)
}

/// A bool sub-key with a default when absent/non-bool.
pub(super) fn fa_bool(v: &Value, k: &str, default: bool) -> bool {
    fa_get(v, k).and_then(Value::as_bool).unwrap_or(default)
}

/// A bool sub-key, `None` when absent (distinguishes present-false from absent).
pub(super) fn fa_bool_opt(v: &Value, k: &str) -> Option<bool> {
    fa_get(v, k).and_then(Value::as_bool)
}

/// An int sub-key with `or`-semantics: absent OR zero yields the default.
pub(super) fn fa_int(v: &Value, k: &str, default: i64) -> i64 {
    fa_get(v, k)
        .and_then(Value::as_i64)
        .filter(|n| *n != 0)
        .unwrap_or(default)
}

/// A truthy int sub-key; `None` when absent or zero.
pub(super) fn fa_int_opt(v: &Value, k: &str) -> Option<i64> {
    fa_get(v, k).and_then(Value::as_i64).filter(|n| *n != 0)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn non_mapping_flash_args_read_as_empty() {
        // The AEN701 helper's flash_args is the string "TBD".
        let fa = Value::String("TBD".to_string());
        assert_eq!(fa_str(&fa, "runner"), None);
        assert!(!fa_bool(&fa, "erase", false));
        assert_eq!(fa_int(&fa, "speed", 921600), 921600);
    }
}
