//! Turning the markup of a notification body into plain text.
//!
//! The notification specification allows a small subset of HTML in bodies
//! (`<b>`, `<i>`, `<u>`, `<a href>`, `<img alt>`) and many applications
//! send Pango markup as well. The shell draws plain text, so the tags are
//! removed and the entities decoded. Text that only looks a little like
//! markup (`a < b`, `<user@example.org>`, `Vec<u8>`) is left alone.

/// The tags that are removed. Anything else in angle brackets stays.
const TAGS: [&str; 22] = [
    "a", "b", "big", "br", "code", "div", "em", "font", "i", "img", "p", "pre", "s", "small", "span", "strike", "strong",
    "sub", "sup", "tt", "u", "markup",
];

/// The longest tag that is looked for; a `<` without a `>` this close is text.
const MAX_TAG: usize = 2048;

/// Removes markup and decodes entities.
pub(crate) fn strip_markup(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    let mut rest = text;
    while let Some(position) = rest.find(['<', '&']) {
        out.push_str(&rest[..position]);
        rest = &rest[position..];
        if rest.starts_with('<') {
            match tag(rest) {
                Some((length, replacement)) => {
                    out.push_str(&replacement);
                    rest = &rest[length..];
                }
                None => {
                    out.push('<');
                    rest = &rest[1..];
                }
            }
        } else {
            match entity(rest) {
                Some((length, ch)) => {
                    out.push(ch);
                    rest = &rest[length..];
                }
                None => {
                    out.push('&');
                    rest = &rest[1..];
                }
            }
        }
    }
    out.push_str(rest);
    out
}

/// If `text` starts with a known tag: its length in bytes and what it
/// becomes (a line break, an image's alternative text, or nothing).
fn tag(text: &str) -> Option<(usize, String)> {
    let window = &text[..floor_boundary(text, MAX_TAG)];
    let end = window.find('>')?;
    let inner = &window[1..end];
    if inner.contains('<') {
        return None;
    }
    let inner = inner.strip_prefix('/').unwrap_or(inner);
    let name_end = inner.find(|ch: char| !ch.is_ascii_alphanumeric()).unwrap_or(inner.len());
    let (name, attributes) = inner.split_at(name_end);
    // After the name: the end, a space before attributes, or `/`.
    if !attributes.is_empty() && !attributes.starts_with(|ch: char| ch.is_ascii_whitespace() || ch == '/') {
        return None;
    }
    let name = name.to_ascii_lowercase();
    if !TAGS.contains(&name.as_str()) {
        return None;
    }
    let replacement = match name.as_str() {
        "br" => "\n".to_owned(),
        "img" => attribute(attributes, "alt").map(|alt| strip_entities(&alt)).unwrap_or_default(),
        _ => String::new(),
    };
    Some((end + 1, replacement))
}

/// The value of `name="..."` (or single quotes) among a tag's attributes.
fn attribute(attributes: &str, name: &str) -> Option<String> {
    let mut rest = attributes;
    while let Some(position) = rest.find(name) {
        let before_ok = rest[..position].chars().next_back().map(|ch| ch.is_ascii_whitespace()).unwrap_or(true);
        let after = rest[position + name.len()..].trim_start();
        if before_ok {
            if let Some(value) = after.strip_prefix('=') {
                let value = value.trim_start();
                let quote = value.chars().next().filter(|ch| *ch == '"' || *ch == '\'')?;
                let value = &value[1..];
                return value.find(quote).map(|end| value[..end].to_owned());
            }
        }
        rest = &rest[position + name.len()..];
    }
    None
}

fn strip_entities(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    let mut rest = text;
    while let Some(position) = rest.find('&') {
        out.push_str(&rest[..position]);
        rest = &rest[position..];
        match entity(rest) {
            Some((length, ch)) => {
                out.push(ch);
                rest = &rest[length..];
            }
            None => {
                out.push('&');
                rest = &rest[1..];
            }
        }
    }
    out.push_str(rest);
    out
}

/// If `text` starts with an entity: its length in bytes and its character.
fn entity(text: &str) -> Option<(usize, char)> {
    let window = &text[..floor_boundary(text, 12)];
    let end = window.find(';')?;
    let name = &window[1..end];
    let ch = match name {
        "amp" => '&',
        "lt" => '<',
        "gt" => '>',
        "quot" => '"',
        "apos" => '\'',
        "nbsp" => ' ',
        _ => {
            let number = name.strip_prefix('#')?;
            let code = match number.strip_prefix(['x', 'X']) {
                Some(hex) => u32::from_str_radix(hex, 16).ok()?,
                None => number.parse::<u32>().ok()?,
            };
            char::from_u32(code).filter(|ch| !ch.is_control() || *ch == '\n')?
        }
    };
    Some((end + 1, ch))
}

/// The largest index not after `max` that is a character boundary.
fn floor_boundary(text: &str, max: usize) -> usize {
    if text.len() <= max {
        return text.len();
    }
    let mut index = max;
    while !text.is_char_boundary(index) {
        index -= 1;
    }
    index
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn removes_the_markup_of_the_specification() {
        assert_eq!(strip_markup("<b>Bold</b> and <i>italic</i> and <u>under</u>"), "Bold and italic and under");
        assert_eq!(strip_markup(r#"See <a href="https://example.org/?a=1&amp;b=2">the page</a>."#), "See the page.");
        assert_eq!(strip_markup(r#"<img src="x.png" alt="A &quot;cat&quot;"/> arrived"#), "A \"cat\" arrived");
        assert_eq!(strip_markup("<img src='x.png'> none"), " none");
        assert_eq!(strip_markup("one<br>two<br/>three<BR />four"), "one\ntwo\nthree\nfour");
        assert_eq!(strip_markup("<span foreground=\"red\">Pango</span> <tt>too</tt>"), "Pango too");
    }

    #[test]
    fn decodes_entities() {
        assert_eq!(strip_markup("Tom &amp; Jerry &lt;3 &gt; &quot;x&quot; &apos;y&apos;"), "Tom & Jerry <3 > \"x\" 'y'");
        assert_eq!(strip_markup("&#65;&#x42;&#X43; &#8364;"), "ABC €");
        // Decoded once: an escaped tag is text, not markup.
        assert_eq!(strip_markup("&lt;b&gt;not bold&lt;/b&gt;"), "<b>not bold</b>");
        assert_eq!(strip_markup("&amp;lt;"), "&lt;");
    }

    #[test]
    fn leaves_text_that_is_not_markup() {
        for text in [
            "a < b and b > a",
            "mail <user@example.org> now",
            "Vec<u8> and Option<String>",
            "<unknown>tag</unknown>",
            "R&D; AT&T &unknown; &#xZZ; &#0; &#7; &",
            "<",
            "<b",
            "5 <3 you",
            "< b>",
        ] {
            assert_eq!(strip_markup(text), text, "{text}");
        }
    }

    #[test]
    fn survives_hostile_input() {
        // No closing bracket anywhere near: linear work, text kept.
        let open = "<".repeat(50_000);
        assert_eq!(strip_markup(&open), open);
        let long_tag = format!("<a href=\"{}\">x</a>", "y".repeat(5000));
        assert!(strip_markup(&long_tag).ends_with("x"));
        let amps = "&".repeat(50_000);
        assert_eq!(strip_markup(&amps), amps);
        // Multi-byte characters around the look-ahead windows.
        let text = format!("<{}", "é".repeat(3000));
        assert_eq!(strip_markup(&text), text);
        let text = "&ééééééééé;";
        assert_eq!(strip_markup(text), text);
        assert_eq!(strip_markup("<b>\u{1F600}</b>&#x1F600;"), "\u{1F600}\u{1F600}");
        // A surrogate and a number that is no character.
        assert_eq!(strip_markup("&#xD800; &#1114112;"), "&#xD800; &#1114112;");
    }
}
