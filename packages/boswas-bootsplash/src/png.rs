//! A minimal PNG writer (8-bit RGB, stored deflate blocks).
//!
//! Only used to save frames in headless mode, for tests and review; nothing
//! at boot reads or writes image files. Writing uncompressed data keeps this
//! to a page of code with no dependencies.

use crate::gfx::Canvas;

fn crc32(chunks: &[&[u8]]) -> u32 {
    let mut crc = 0xffff_ffffu32;
    for chunk in chunks {
        for &byte in *chunk {
            crc ^= byte as u32;
            for _ in 0..8 {
                crc = if crc & 1 != 0 { (crc >> 1) ^ 0xedb8_8320 } else { crc >> 1 };
            }
        }
    }
    !crc
}

fn adler32(data: &[u8]) -> u32 {
    let (mut a, mut b) = (1u32, 0u32);
    for chunk in data.chunks(5552) {
        for &byte in chunk {
            a += byte as u32;
            b += a;
        }
        a %= 65521;
        b %= 65521;
    }
    (b << 16) | a
}

fn chunk(out: &mut Vec<u8>, kind: &[u8; 4], data: &[u8]) {
    out.extend_from_slice(&(data.len() as u32).to_be_bytes());
    out.extend_from_slice(kind);
    out.extend_from_slice(data);
    out.extend_from_slice(&crc32(&[kind, data]).to_be_bytes());
}

/// Encode the canvas as a PNG file.
pub fn encode(canvas: &Canvas) -> Vec<u8> {
    let (w, h) = (canvas.width(), canvas.height());
    let mut raw = Vec::with_capacity((w as usize * 3 + 1) * h as usize);
    for y in 0..h {
        raw.push(0); // filter: none
        for &px in canvas.row(y, 0, w) {
            raw.extend_from_slice(&[(px >> 16) as u8, (px >> 8) as u8, px as u8]);
        }
    }
    let mut zlib = vec![0x78, 0x01];
    let mut blocks = raw.chunks(65535).peekable();
    if blocks.peek().is_none() {
        zlib.extend_from_slice(&[1, 0, 0, 0xff, 0xff]);
    }
    while let Some(block) = blocks.next() {
        zlib.push(u8::from(blocks.peek().is_none()));
        zlib.extend_from_slice(&(block.len() as u16).to_le_bytes());
        zlib.extend_from_slice(&(!(block.len() as u16)).to_le_bytes());
        zlib.extend_from_slice(block);
    }
    zlib.extend_from_slice(&adler32(&raw).to_be_bytes());

    let mut out = vec![0x89, b'P', b'N', b'G', 0x0d, 0x0a, 0x1a, 0x0a];
    let mut header = Vec::with_capacity(13);
    header.extend_from_slice(&w.to_be_bytes());
    header.extend_from_slice(&h.to_be_bytes());
    header.extend_from_slice(&[8, 2, 0, 0, 0]); // 8-bit, RGB
    chunk(&mut out, b"IHDR", &header);
    chunk(&mut out, b"IDAT", &zlib);
    chunk(&mut out, b"IEND", &[]);
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::gfx::{Rect, Rgb};

    #[test]
    fn known_checksums() {
        assert_eq!(crc32(&[b"123456789"]), 0xcbf4_3926);
        assert_eq!(crc32(&[b"1234", b"56789"]), 0xcbf4_3926);
        assert_eq!(adler32(b"Wikipedia"), 0x11e6_0398);
    }

    #[test]
    fn structure_of_a_small_image() {
        let mut canvas = Canvas::new(3, 2);
        canvas.fill_rect(&Rect::new(0, 0, 3, 2), Rgb::new(1, 2, 3));
        let png = encode(&canvas);
        assert_eq!(&png[..8], &[0x89, b'P', b'N', b'G', 0x0d, 0x0a, 0x1a, 0x0a]);
        assert_eq!(&png[12..16], b"IHDR");
        assert_eq!(u32::from_be_bytes(png[16..20].try_into().unwrap()), 3);
        assert_eq!(u32::from_be_bytes(png[20..24].try_into().unwrap()), 2);
        assert_eq!(&png[png.len() - 8..png.len() - 4], b"IEND");
        // The first scanline: filter byte, then RGB triples.
        let idat = png.windows(4).position(|w| w == b"IDAT").unwrap();
        assert_eq!(&png[idat + 4 + 7..idat + 4 + 7 + 4], &[0, 1, 2, 3]);
    }

    #[test]
    fn large_images_use_several_blocks() {
        let canvas = Canvas::new(400, 100);
        let png = encode(&canvas);
        assert!(png.len() > 400 * 100 * 3);
    }
}
