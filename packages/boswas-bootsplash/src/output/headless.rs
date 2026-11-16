//! An output that draws to memory and saves frames as files.
//!
//! Used by the tests and to produce review images; it needs no graphics
//! device. Frames are only written on request ([`Output::snapshot`]).

use std::io;
use std::path::PathBuf;

use crate::gfx::{Canvas, Rect};
use crate::png;

use super::Output;

/// Frames go nowhere until a snapshot is asked for.
#[derive(Debug)]
pub struct HeadlessOutput {
    size: (u32, u32),
    dir: PathBuf,
    /// Number of `present` calls and the pixels they covered.
    pub frames: u64,
    pub pixels: u64,
}

impl HeadlessOutput {
    pub fn new(width: u32, height: u32, dir: PathBuf) -> io::Result<Self> {
        if width == 0 || height == 0 || width > 16384 || height > 16384 {
            return Err(io::Error::new(io::ErrorKind::InvalidInput, "headless size must be 1..=16384 pixels each way"));
        }
        std::fs::create_dir_all(&dir)?;
        Ok(Self { size: (width, height), dir, frames: 0, pixels: 0 })
    }
}

/// The canvas as a binary PPM (P6) file.
pub fn encode_ppm(canvas: &Canvas) -> Vec<u8> {
    let mut out = format!("P6\n{} {}\n255\n", canvas.width(), canvas.height()).into_bytes();
    out.reserve(canvas.pixels().len() * 3);
    for &px in canvas.pixels() {
        out.extend_from_slice(&[(px >> 16) as u8, (px >> 8) as u8, px as u8]);
    }
    out
}

impl Output for HeadlessOutput {
    fn name(&self) -> &'static str {
        "headless"
    }

    fn sizes(&self) -> Vec<(u32, u32)> {
        vec![self.size]
    }

    fn present(&mut self, _index: usize, _canvas: &Canvas, damage: &[Rect]) -> io::Result<()> {
        self.frames += 1;
        self.pixels += damage.iter().map(|r| r.area() as u64).sum::<u64>();
        Ok(())
    }

    /// `NAME.ppm` is written as PPM, anything else as PNG (`.png` is added
    /// when missing). The name was validated by the protocol parser.
    fn snapshot(&mut self, name: &str, canvas: &Canvas) -> io::Result<()> {
        let (file, data) = if name.ends_with(".ppm") {
            (name.to_owned(), encode_ppm(canvas))
        } else if name.ends_with(".png") {
            (name.to_owned(), png::encode(canvas))
        } else {
            (format!("{name}.png"), png::encode(canvas))
        };
        // Write to a temporary name first so a reader never sees half a file.
        let target = self.dir.join(&file);
        let partial = self.dir.join(format!(".{file}.partial"));
        std::fs::write(&partial, data)?;
        std::fs::rename(&partial, &target)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::gfx::Rgb;

    #[test]
    fn snapshots_are_written_in_both_formats() {
        let dir = std::env::temp_dir().join(format!("boswas-bootsplash-headless-{}", std::process::id()));
        let mut out = HeadlessOutput::new(4, 2, dir.clone()).unwrap();
        let mut canvas = Canvas::new(4, 2);
        canvas.fill_rect(&canvas.bounds(), Rgb::new(9, 8, 7));
        out.present(0, &canvas, &[canvas.bounds()]).unwrap();
        assert_eq!((out.frames, out.pixels), (1, 8));
        out.snapshot("a", &canvas).unwrap();
        out.snapshot("b.ppm", &canvas).unwrap();
        assert!(std::fs::read(dir.join("a.png")).unwrap().starts_with(&[0x89, b'P', b'N', b'G']));
        let ppm = std::fs::read(dir.join("b.ppm")).unwrap();
        assert!(ppm.starts_with(b"P6\n4 2\n255\n"));
        assert_eq!(&ppm[ppm.len() - 3..], &[9, 8, 7]);
        std::fs::remove_dir_all(&dir).unwrap();
        assert!(HeadlessOutput::new(0, 10, dir).is_err());
    }
}
