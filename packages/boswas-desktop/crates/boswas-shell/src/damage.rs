//! What changed between two drawings of a surface, and whether a drawing
//! covers its surface completely.
//!
//! The shell draws every surface on the CPU and sends the whole picture.
//! Telling the compositor only the part that changed lets it upload and
//! compose just that part; a drawing that changed nothing is not sent at
//! all (the pointer moved onto the desktop, say). A surface without
//! transparent pixels is declared opaque, so the compositor neither blends
//! it nor draws what lies behind it.

/// A rectangle in buffer pixels: x, y, width, height.
pub type PixelRect = (u32, u32, u32, u32);

/// The smallest rectangle that holds every pixel in which `before` and
/// `after` differ, or `None` when they are the same. Both are `width` x
/// `height` pixels of four bytes, row after row.
pub fn changed_rect(before: &[u8], after: &[u8], width: u32, height: u32) -> Option<PixelRect> {
    let row = width as usize * 4;
    if row == 0 || before.len() != after.len() || before.len() != row * height as usize {
        return (width > 0 && height > 0).then_some((0, 0, width, height));
    }
    let (mut top, mut bottom) = (None, 0usize);
    let (mut left, mut right) = (width as usize, 0usize);
    for (y, (a, b)) in before.chunks_exact(row).zip(after.chunks_exact(row)).enumerate() {
        if a == b {
            continue;
        }
        top.get_or_insert(y);
        bottom = y;
        let (first, last) = differing_pixels(a, b);
        left = left.min(first);
        right = right.max(last);
    }
    let top = top?;
    Some((left as u32, top as u32, (right - left + 1) as u32, (bottom - top + 1) as u32))
}

/// The first and the last pixel (four bytes each) in which two rows of the
/// same length differ; (0, 0) when they do not. Compared eight bytes at a
/// time: rows of a large surface are compared on every frame it changes.
fn differing_pixels(a: &[u8], b: &[u8]) -> (usize, usize) {
    let word = |s: &[u8], i: usize| u64::from_le_bytes(s[i * 8..i * 8 + 8].try_into().unwrap_or([0; 8]));
    let words = a.len().min(b.len()) / 8;
    let byte_at = |i: usize, x: u64, y: u64, last: bool| {
        // Little endian: byte k of the word is bits 8k..8k+8.
        let bits = x ^ y;
        let k = if last { 7 - bits.leading_zeros() as usize / 8 } else { bits.trailing_zeros() as usize / 8 };
        i * 8 + k
    };
    let tail = || {
        let start = words * 8;
        (start..a.len().min(b.len())).filter(|&i| a[i] != b[i])
    };
    let first = (0..words)
        .find(|&i| word(a, i) != word(b, i))
        .map(|i| byte_at(i, word(a, i), word(b, i), false))
        .or_else(|| tail().next());
    let last = tail()
        .next_back()
        .or_else(|| (0..words).rev().find(|&i| word(a, i) != word(b, i)).map(|i| byte_at(i, word(a, i), word(b, i), true)));
    match (first, last) {
        (Some(first), Some(last)) => (first / 4, last / 4),
        _ => (0, 0),
    }
}

/// Whether no pixel of a drawing (four bytes per pixel, alpha last) lets
/// what is behind it show through.
pub fn is_opaque(pixels: &[u8]) -> bool {
    // Two pixels at a time: a whole screen is millions of them.
    const ALPHAS: u64 = 0xff00_0000_ff00_0000;
    let pairs = pixels.chunks_exact(8);
    let rest = pairs.remainder();
    !pixels.is_empty()
        && rest.chunks_exact(4).all(|p| p[3] == 255)
        && pairs.into_iter().all(|pair| u64::from_le_bytes(pair.try_into().unwrap_or([0; 8])) & ALPHAS == ALPHAS)
}

/// The rows `area` covers in a picture `width` pixels wide: for each, the
/// byte range of the part inside `area`.
fn area_rows(width: u32, area: PixelRect) -> impl Iterator<Item = std::ops::Range<usize>> {
    let (x, y, w, h) = (area.0 as usize, area.1 as usize, area.2 as usize, area.3 as usize);
    let stride = width as usize * 4;
    (y..y + h).map(move |row| row * stride + x * 4..row * stride + (x + w) * 4)
}

/// [`changed_rect`] within `area` only: the smallest rectangle inside it in
/// which `before` and `after` (pictures `width` pixels wide) differ.
pub fn changed_in(before: &[u8], after: &[u8], width: u32, area: PixelRect) -> Option<PixelRect> {
    if before.len() != after.len() || area.2 == 0 || area.3 == 0 {
        return None;
    }
    let (mut top, mut bottom) = (None, 0usize);
    let (mut left, mut right) = (usize::MAX, 0usize);
    for (y, range) in area_rows(width, area).enumerate() {
        if range.end > before.len() {
            break;
        }
        let (a, b) = (&before[range.clone()], &after[range]);
        if a == b {
            continue;
        }
        top.get_or_insert(y);
        bottom = y;
        let (first, last) = differing_pixels(a, b);
        left = left.min(first);
        right = right.max(last);
    }
    let top = top?;
    Some((area.0 + left as u32, area.1 + top as u32, (right - left + 1) as u32, (bottom - top + 1) as u32))
}

/// Copies the part `rect` of `from` into `to`, both pictures `width`
/// pixels wide.
pub fn copy_rect(from: &[u8], to: &mut [u8], width: u32, rect: PixelRect) {
    for range in area_rows(width, rect) {
        if range.end > from.len() || range.end > to.len() {
            break;
        }
        to[range.clone()].copy_from_slice(&from[range]);
    }
}

/// Whether no pixel of the part `rect` of a picture `width` pixels wide is
/// transparent at all.
pub fn is_opaque_in(pixels: &[u8], width: u32, rect: PixelRect) -> bool {
    area_rows(width, rect).all(|range| pixels.get(range).is_some_and(|row| row.chunks_exact(4).all(|p| p[3] == 255)))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn picture(width: u32, height: u32, fill: u8) -> Vec<u8> {
        vec![fill; (width * height * 4) as usize]
    }

    fn set(pixels: &mut [u8], width: u32, x: u32, y: u32, value: u8) {
        let at = ((y * width + x) * 4) as usize;
        pixels[at..at + 4].fill(value);
    }

    #[test]
    fn the_same_drawing_has_nothing_to_send() {
        let a = picture(40, 30, 7);
        assert_eq!(changed_rect(&a, &a.clone(), 40, 30), None);
    }

    #[test]
    fn a_change_is_bounded_tightly() {
        let a = picture(40, 30, 0);
        let mut b = a.clone();
        set(&mut b, 40, 5, 3, 9);
        assert_eq!(changed_rect(&a, &b, 40, 30), Some((5, 3, 1, 1)));
        set(&mut b, 40, 31, 20, 9);
        assert_eq!(changed_rect(&a, &b, 40, 30), Some((5, 3, 27, 18)));
        // A change in one channel of the last pixel counts.
        let mut c = a.clone();
        c[(40 * 30 * 4) - 1] = 1;
        assert_eq!(changed_rect(&a, &c, 40, 30), Some((39, 29, 1, 1)));
    }

    #[test]
    fn a_drawing_of_another_size_is_sent_whole() {
        let a = picture(40, 30, 0);
        let b = picture(20, 30, 0);
        assert_eq!(changed_rect(&a, &b, 20, 30), Some((0, 0, 20, 30)));
        assert_eq!(changed_rect(&[], &b, 20, 30), Some((0, 0, 20, 30)));
        assert_eq!(changed_rect(&[], &[], 0, 0), None);
    }

    #[test]
    fn the_first_and_last_differing_pixels_are_found_in_every_position() {
        // Rows of 1 to 9 pixels (odd lengths leave a tail of four bytes),
        // with a difference in each byte of each pixel.
        for pixels in 1..10usize {
            let a = vec![7u8; pixels * 4];
            for first in 0..pixels {
                for last in first..pixels {
                    for byte in 0..4 {
                        let mut b = a.clone();
                        b[first * 4 + byte] = 9;
                        b[last * 4 + (3 - byte)] = 9;
                        assert_eq!(differing_pixels(&a, &b), (first, last), "{pixels} pixels, {first}..{last}, byte {byte}");
                    }
                }
            }
        }
    }

    #[test]
    fn a_part_is_compared_copied_and_checked_on_its_own() {
        let a = picture(40, 30, 0);
        let mut b = a.clone();
        set(&mut b, 40, 12, 7, 9);
        set(&mut b, 40, 15, 9, 9);
        set(&mut b, 40, 39, 29, 9);
        // Inside the part: the change there, bounded tightly; the change
        // outside it is not seen.
        assert_eq!(changed_in(&a, &b, 40, (10, 5, 10, 10)), Some((12, 7, 4, 3)));
        assert_eq!(changed_in(&a, &b, 40, (0, 0, 5, 5)), None);
        let mut c = a.clone();
        copy_rect(&b, &mut c, 40, (10, 5, 10, 10));
        assert_eq!(changed_in(&b, &c, 40, (10, 5, 10, 10)), None, "the part is copied");
        assert_eq!(changed_rect(&b, &c, 40, 30), Some((39, 29, 1, 1)), "and nothing else");
        let opaque = picture(8, 8, 255);
        let mut holed = opaque.clone();
        holed[(8 * 6 + 6) * 4 + 3] = 0;
        assert!(is_opaque_in(&holed, 8, (0, 0, 4, 4)));
        assert!(!is_opaque_in(&holed, 8, (5, 5, 3, 3)));
    }

    #[test]
    fn opaque_means_every_pixel() {
        let mut a = picture(8, 8, 255);
        assert!(is_opaque(&a));
        a[4 * 20 + 3] = 254;
        assert!(!is_opaque(&a));
        assert!(!is_opaque(&[]));
    }
}
