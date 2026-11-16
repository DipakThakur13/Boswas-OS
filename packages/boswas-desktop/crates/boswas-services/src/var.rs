//! Reading D-Bus values that other programs sent. Nothing here trusts the
//! type of a value: every reader returns `None` for a value of another
//! type, and text is cleaned and cut to a length before it is shown.

use std::collections::HashMap;

use zbus::zvariant::{OwnedValue, Value};

/// The properties of an interface, as `GetAll` returns them.
pub(crate) type Props = HashMap<String, OwnedValue>;

/// Looks through variants nested in variants (bounded: a peer cannot make
/// this loop for long, the bus limits nesting).
pub(crate) fn peel<'a, 'v>(mut value: &'a Value<'v>) -> &'a Value<'v> {
    while let Value::Value(inner) = value {
        value = inner;
    }
    value
}

pub(crate) fn as_str<'a>(value: &'a Value<'_>) -> Option<&'a str> {
    match peel(value) {
        Value::Str(text) => Some(text.as_str()),
        _ => None,
    }
}

pub(crate) fn as_path<'a>(value: &'a Value<'_>) -> Option<&'a str> {
    match peel(value) {
        Value::ObjectPath(path) => Some(path.as_str()),
        _ => None,
    }
}

pub(crate) fn as_bool(value: &Value<'_>) -> Option<bool> {
    match peel(value) {
        Value::Bool(flag) => Some(*flag),
        _ => None,
    }
}

pub(crate) fn as_i64(value: &Value<'_>) -> Option<i64> {
    match peel(value) {
        Value::U8(n) => Some(i64::from(*n)),
        Value::I16(n) => Some(i64::from(*n)),
        Value::U16(n) => Some(i64::from(*n)),
        Value::I32(n) => Some(i64::from(*n)),
        Value::U32(n) => Some(i64::from(*n)),
        Value::I64(n) => Some(*n),
        Value::U64(n) => i64::try_from(*n).ok(),
        _ => None,
    }
}

pub(crate) fn as_u32(value: &Value<'_>) -> Option<u32> {
    as_i64(value).and_then(|n| u32::try_from(n).ok())
}

pub(crate) fn as_f64(value: &Value<'_>) -> Option<f64> {
    match peel(value) {
        Value::F64(n) if n.is_finite() => Some(*n),
        Value::F64(_) => None,
        other => as_i64(other).map(|n| n as f64),
    }
}

/// The elements of an array.
pub(crate) fn as_list<'a, 'v>(value: &'a Value<'v>) -> Option<&'a [Value<'v>]> {
    match peel(value) {
        Value::Array(array) => Some(array.inner()),
        _ => None,
    }
}

/// The fields of a structure.
pub(crate) fn as_fields<'a, 'v>(value: &'a Value<'v>) -> Option<&'a [Value<'v>]> {
    match peel(value) {
        Value::Structure(structure) => Some(structure.fields()),
        _ => None,
    }
}

/// An array of bytes, at most `max` long.
pub(crate) fn as_bytes(value: &Value<'_>, max: usize) -> Option<Vec<u8>> {
    let list = as_list(value)?;
    if list.len() > max {
        return None;
    }
    list.iter()
        .map(|item| match item {
            Value::U8(byte) => Some(*byte),
            _ => None,
        })
        .collect()
}

/// The entries of a dictionary with text keys.
pub(crate) fn as_dict<'a, 'v>(value: &'a Value<'v>) -> Vec<(&'a str, &'a Value<'v>)> {
    match peel(value) {
        Value::Dict(dict) => dict.iter().filter_map(|(key, value)| Some((as_str(key)?, value))).collect(),
        _ => Vec::new(),
    }
}

pub(crate) fn prop<'a>(props: &'a Props, name: &str) -> Option<&'a Value<'static>> {
    props.get(name).map(|value| &**value)
}

pub(crate) fn prop_str(props: &Props, name: &str) -> Option<String> {
    prop(props, name).and_then(as_str).map(str::to_owned)
}

pub(crate) fn prop_path(props: &Props, name: &str) -> Option<String> {
    prop(props, name).and_then(as_path).map(str::to_owned)
}

pub(crate) fn prop_bool(props: &Props, name: &str) -> Option<bool> {
    prop(props, name).and_then(as_bool)
}

pub(crate) fn prop_u32(props: &Props, name: &str) -> Option<u32> {
    prop(props, name).and_then(as_u32)
}

pub(crate) fn prop_i64(props: &Props, name: &str) -> Option<i64> {
    prop(props, name).and_then(as_i64)
}

pub(crate) fn prop_f64(props: &Props, name: &str) -> Option<f64> {
    prop(props, name).and_then(as_f64)
}

/// A list of object paths (`ao`).
pub(crate) fn prop_paths(props: &Props, name: &str) -> Vec<String> {
    prop(props, name)
        .and_then(as_list)
        .map(|list| list.iter().filter_map(as_path).map(str::to_owned).collect())
        .unwrap_or_default()
}

/// Text from another program, made safe to show: control characters are
/// dropped (line breaks and tabs become spaces unless `multiline`), it is
/// trimmed and cut to `max` characters.
pub(crate) fn clean(text: &str, max: usize, multiline: bool) -> String {
    let mut out = String::with_capacity(text.len().min(max * 4));
    let mut count = 0;
    for ch in text.chars() {
        let ch = match ch {
            '\n' if multiline => '\n',
            '\n' | '\t' | '\r' => ' ',
            // Bidirectional overrides and other format controls can
            // disguise text; the zero-width joiners stay, emoji need them.
            '\u{202a}'..='\u{202e}' | '\u{2066}'..='\u{2069}' => continue,
            ch if ch.is_control() => continue,
            ch => ch,
        };
        if count == max {
            break;
        }
        out.push(ch);
        count += 1;
    }
    let trimmed = out.trim();
    if trimmed.len() == out.len() {
        out
    } else {
        trimmed.to_owned()
    }
}

/// One line of text for a label.
pub(crate) fn label(text: &str) -> String {
    clean(text, 256, false)
}

/// Decodes the `%XX` escapes of a URI path.
pub(crate) fn percent_decode(text: &str) -> Option<String> {
    let bytes = text.as_bytes();
    let mut out = Vec::with_capacity(bytes.len());
    let mut i = 0;
    while i < bytes.len() {
        if bytes[i] == b'%' {
            let hex = bytes.get(i + 1..i + 3)?;
            let hex = std::str::from_utf8(hex).ok()?;
            out.push(u8::from_str_radix(hex, 16).ok()?);
            i += 3;
        } else {
            out.push(bytes[i]);
            i += 1;
        }
    }
    String::from_utf8(out).ok()
}

/// The local path of a `file://` URI: an absolute path without NUL, and
/// nothing for any other scheme or for a URI that names another host.
pub(crate) fn file_uri_path(uri: &str) -> Option<String> {
    let rest = uri.strip_prefix("file://")?;
    let rest = match rest.strip_prefix("localhost/") {
        Some(path) => format!("/{path}"),
        None => rest.to_owned(),
    };
    if !rest.starts_with('/') {
        return None;
    }
    let path = percent_decode(&rest)?;
    if path.contains('\0') || path.len() > 4096 {
        return None;
    }
    Some(path)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn readers_refuse_other_types() {
        let text = Value::from("hello");
        let number = Value::from(7u32);
        assert_eq!(as_str(&text), Some("hello"));
        assert_eq!(as_str(&number), None);
        assert_eq!(as_u32(&text), None);
        assert_eq!(as_u32(&number), Some(7));
        assert_eq!(as_u32(&Value::from(-1i32)), None);
        assert_eq!(as_bool(&number), None);
        assert_eq!(as_f64(&Value::from(f64::NAN)), None);
        assert_eq!(as_f64(&Value::from(2u8)), Some(2.0));
        let nested = Value::Value(Box::new(Value::Value(Box::new(Value::from(true)))));
        assert_eq!(as_bool(&nested), Some(true));
    }

    #[test]
    fn bytes_are_bounded() {
        let bytes = Value::from(vec![1u8, 2, 3]);
        assert_eq!(as_bytes(&bytes, 3), Some(vec![1, 2, 3]));
        assert_eq!(as_bytes(&bytes, 2), None);
        assert_eq!(as_bytes(&Value::from(vec!["a"]), 8), None);
    }

    #[test]
    fn clean_removes_what_cannot_be_shown() {
        assert_eq!(clean("  a\tb\nc\u{7}\u{202e}d  ", 64, false), "a b cd");
        assert_eq!(clean("a\nb", 64, true), "a\nb");
        assert_eq!(clean("abcdef", 3, false), "abc");
        assert_eq!(clean("äöü", 2, false), "äö");
    }

    #[test]
    fn file_uris_only() {
        assert_eq!(file_uri_path("file:///tmp/a%20b.png").as_deref(), Some("/tmp/a b.png"));
        assert_eq!(file_uri_path("file://localhost/tmp/x").as_deref(), Some("/tmp/x"));
        assert_eq!(file_uri_path("https://example.org/a.png"), None);
        assert_eq!(file_uri_path("file://host/tmp/x"), None);
        assert_eq!(file_uri_path("file:///tmp/%zz"), None);
        assert_eq!(file_uri_path("file:///tmp/%00"), None);
    }
}
