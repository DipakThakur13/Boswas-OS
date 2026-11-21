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
        // The first and the last pixel of the row that differ.
        let first = a.chunks_exact(4).zip(b.chunks_exact(4)).position(|(p, q)| p != q).unwrap_or(0);
        let last = a.chunks_exact(4).zip(b.chunks_exact(4)).rposition(|(p, q)| p != q).unwrap_or(first);
        left = left.min(first);
        right = right.max(last);
    }
    let top = top?;
    Some((left as u32, top as u32, (right - left + 1) as u32, (bottom - top + 1) as u32))
}

/// Whether no pixel of a drawing (four bytes per pixel, alpha last) lets
/// what is behind it show through.
pub fn is_opaque(pixels: &[u8]) -> bool {
    !pixels.is_empty() && pixels.chunks_exact(4).all(|p| p[3] == 255)
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
    fn opaque_means_every_pixel() {
        let mut a = picture(8, 8, 255);
        assert!(is_opaque(&a));
        a[4 * 20 + 3] = 254;
        assert!(!is_opaque(&a));
        assert!(!is_opaque(&[]));
    }
}
