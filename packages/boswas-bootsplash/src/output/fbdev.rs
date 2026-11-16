//! Fallback output: a legacy frame buffer device (`/dev/fb0`).
//!
//! Used when no DRM device is available (for example `vesafb` on old BIOS
//! machines). Supports the common packed-pixel formats: 32 bits per pixel
//! with 8-bit channels at any position, and 16 bits per pixel RGB565.

use std::fs::{File, OpenOptions};
use std::io;
use std::os::fd::AsFd;
use std::path::Path;

use crate::gfx::{Canvas, Rect};
use crate::sys::{self, FbFixScreeninfo, FbVarScreeninfo, Mapping};

use super::Output;

pub const DEFAULT_DEVICE: &str = "/dev/fb0";

/// `FB_TYPE_PACKED_PIXELS` and `FB_VISUAL_TRUECOLOR` / `FB_VISUAL_DIRECTCOLOR`.
const FB_TYPE_PACKED_PIXELS: u32 = 0;
const FB_VISUAL_TRUECOLOR: u32 = 2;
const FB_VISUAL_DIRECTCOLOR: u32 = 4;

/// How pixels are stored on the device.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum PixelFormat {
    /// 4 bytes per pixel; bit offsets of red, green and blue.
    Rgb32 { red: u32, green: u32, blue: u32 },
    /// 2 bytes per pixel, 5-6-5.
    Rgb565,
}

/// Geometry and format checked against the device's reported sizes.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Geometry {
    pub width: u32,
    pub height: u32,
    pub stride: usize,
    /// Byte offset of the visible area inside the mapping.
    pub offset: usize,
    pub format: PixelFormat,
    /// Bytes to map.
    pub length: usize,
}

/// Work out how to draw on a frame buffer, or explain why it is unsupported.
pub fn geometry(var: &FbVarScreeninfo, fix: &FbFixScreeninfo) -> Result<Geometry, &'static str> {
    if fix.kind != FB_TYPE_PACKED_PIXELS {
        return Err("not a packed-pixel frame buffer");
    }
    if fix.visual != FB_VISUAL_TRUECOLOR && fix.visual != FB_VISUAL_DIRECTCOLOR {
        return Err("not a true-colour frame buffer");
    }
    if var.xres == 0 || var.yres == 0 || var.xres > 16384 || var.yres > 16384 {
        return Err("implausible resolution");
    }
    let format = match var.bits_per_pixel {
        32 => {
            let ok = |f: &sys::FbBitfield| f.length == 8 && f.offset <= 24 && f.offset % 8 == 0;
            if !(ok(&var.red) && ok(&var.green) && ok(&var.blue)) {
                return Err("unsupported 32-bit channel layout");
            }
            PixelFormat::Rgb32 { red: var.red.offset, green: var.green.offset, blue: var.blue.offset }
        }
        16 => {
            let is565 = var.red.offset == 11
                && var.red.length == 5
                && var.green.offset == 5
                && var.green.length == 6
                && var.blue.offset == 0
                && var.blue.length == 5;
            if !is565 {
                return Err("unsupported 16-bit channel layout");
            }
            PixelFormat::Rgb565
        }
        _ => return Err("unsupported colour depth"),
    };
    let bytes = (var.bits_per_pixel / 8) as usize;
    let stride = fix.line_length as usize;
    if stride < var.xres as usize * bytes {
        return Err("line length shorter than a row");
    }
    let offset = var.yoffset as usize * stride + var.xoffset as usize * bytes;
    let needed = offset + (var.yres as usize - 1) * stride + var.xres as usize * bytes;
    let length = fix.smem_len as usize;
    if length < needed {
        return Err("frame buffer memory smaller than the screen");
    }
    Ok(Geometry { width: var.xres, height: var.yres, stride, offset, format, length })
}

/// Convert one row of XRGB8888 pixels into the device format.
pub fn convert_row(row: &[u32], format: PixelFormat, out: &mut [u8]) {
    match format {
        PixelFormat::Rgb32 { red, green, blue } => {
            for (px, dst) in row.iter().zip(out.chunks_exact_mut(4)) {
                let v = (((px >> 16) & 0xff) << red) | (((px >> 8) & 0xff) << green) | ((px & 0xff) << blue);
                dst.copy_from_slice(&v.to_le_bytes());
            }
        }
        PixelFormat::Rgb565 => {
            for (px, dst) in row.iter().zip(out.chunks_exact_mut(2)) {
                let v = ((((px >> 16) & 0xff) >> 3) << 11) | ((((px >> 8) & 0xff) >> 2) << 5) | ((px & 0xff) >> 3);
                dst.copy_from_slice(&(v as u16).to_le_bytes());
            }
        }
    }
}

/// A mapped frame buffer device.
#[derive(Debug)]
pub struct FbOutput {
    geometry: Geometry,
    map: Mapping,
    _file: File,
}

impl FbOutput {
    pub fn open(path: &Path) -> io::Result<FbOutput> {
        let file = OpenOptions::new().read(true).write(true).open(path)?;
        let var = sys::fb_var_screeninfo(file.as_fd())?;
        let fix = sys::fb_fix_screeninfo(file.as_fd())?;
        let geometry = geometry(&var, &fix).map_err(|why| io::Error::new(io::ErrorKind::Unsupported, why))?;
        let map = Mapping::new(file.as_fd(), geometry.length)?;
        Ok(FbOutput { geometry, map, _file: file })
    }
}

impl Output for FbOutput {
    fn name(&self) -> &'static str {
        "fbdev"
    }

    fn sizes(&self) -> Vec<(u32, u32)> {
        vec![(self.geometry.width, self.geometry.height)]
    }

    fn present(&mut self, _index: usize, canvas: &Canvas, damage: &[Rect]) -> io::Result<()> {
        let g = self.geometry;
        let bytes = match g.format {
            PixelFormat::Rgb32 { .. } => 4,
            PixelFormat::Rgb565 => 2,
        };
        let screen = Rect::new(0, 0, g.width as i32, g.height as i32);
        let memory = self.map.as_mut_slice();
        for rect in damage {
            let rect = rect.intersect(&screen).intersect(&canvas.bounds());
            for y in rect.y..rect.bottom() {
                let row = canvas.row(y as u32, rect.x as u32, rect.w as u32);
                let start = g.offset + y as usize * g.stride + rect.x as usize * bytes;
                if let Some(out) = memory.get_mut(start..start + row.len() * bytes) {
                    convert_row(row, g.format, out);
                }
            }
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::sys::FbBitfield;

    fn var32() -> FbVarScreeninfo {
        FbVarScreeninfo {
            xres: 1024,
            yres: 768,
            xres_virtual: 1024,
            yres_virtual: 768,
            bits_per_pixel: 32,
            red: FbBitfield { offset: 16, length: 8, msb_right: 0 },
            green: FbBitfield { offset: 8, length: 8, msb_right: 0 },
            blue: FbBitfield { offset: 0, length: 8, msb_right: 0 },
            ..Default::default()
        }
    }

    fn fix(line_length: u32, smem_len: u32) -> FbFixScreeninfo {
        FbFixScreeninfo { visual: FB_VISUAL_TRUECOLOR, line_length, smem_len, ..Default::default() }
    }

    #[test]
    fn accepts_the_usual_32_bit_layout() {
        let g = geometry(&var32(), &fix(4096, 4096 * 768)).unwrap();
        assert_eq!((g.width, g.height, g.stride, g.offset), (1024, 768, 4096, 0));
        assert_eq!(g.format, PixelFormat::Rgb32 { red: 16, green: 8, blue: 0 });
    }

    #[test]
    fn honours_panning_offsets() {
        let mut var = var32();
        var.yoffset = 10;
        var.xoffset = 2;
        let g = geometry(&var, &fix(4096, 4096 * 800)).unwrap();
        assert_eq!(g.offset, 10 * 4096 + 8);
    }

    #[test]
    fn rejects_what_it_cannot_draw() {
        assert!(geometry(&var32(), &fix(4096, 1000)).is_err(), "memory too small");
        assert!(geometry(&var32(), &fix(100, 4096 * 768)).is_err(), "stride too small");
        let mut var = var32();
        var.bits_per_pixel = 8;
        assert!(geometry(&var, &fix(1024, 1024 * 768)).is_err());
        let mut var = var32();
        var.red.length = 10;
        assert!(geometry(&var, &fix(4096, 4096 * 768)).is_err());
        let mut var = var32();
        var.xres = 0;
        assert!(geometry(&var, &fix(4096, 4096 * 768)).is_err());
        let mut f = fix(4096, 4096 * 768);
        f.visual = 3; // pseudo colour
        assert!(geometry(&var32(), &f).is_err());
        f = fix(4096, 4096 * 768);
        f.kind = 1; // planes
        assert!(geometry(&var32(), &f).is_err());
    }

    #[test]
    fn converts_pixels() {
        let row = [0x00112233u32, 0x00ff8040];
        let mut out = [0u8; 8];
        convert_row(&row, PixelFormat::Rgb32 { red: 16, green: 8, blue: 0 }, &mut out);
        assert_eq!(out, [0x33, 0x22, 0x11, 0x00, 0x40, 0x80, 0xff, 0x00]);
        // BGR order (red in the low byte).
        convert_row(&row, PixelFormat::Rgb32 { red: 0, green: 8, blue: 16 }, &mut out);
        assert_eq!(&out[..4], &[0x11, 0x22, 0x33, 0x00]);
        let mut out16 = [0u8; 4];
        convert_row(&[0x00ffffff, 0x00ff0000], PixelFormat::Rgb565, &mut out16);
        assert_eq!(out16, [0xff, 0xff, 0x00, 0xf8]);
    }

    #[test]
    fn rgb565_geometry() {
        let var = FbVarScreeninfo {
            xres: 800,
            yres: 600,
            bits_per_pixel: 16,
            red: FbBitfield { offset: 11, length: 5, msb_right: 0 },
            green: FbBitfield { offset: 5, length: 6, msb_right: 0 },
            blue: FbBitfield { offset: 0, length: 5, msb_right: 0 },
            ..Default::default()
        };
        let g = geometry(&var, &fix(1600, 1600 * 600)).unwrap();
        assert_eq!(g.format, PixelFormat::Rgb565);
    }

    #[test]
    fn a_missing_device_is_an_error() {
        assert!(FbOutput::open(Path::new("/nonexistent/fb0")).is_err());
        assert!(FbOutput::open(Path::new("/dev/null")).is_err());
    }
}
