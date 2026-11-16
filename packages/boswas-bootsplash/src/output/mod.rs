//! Where the picture goes: DRM/KMS, a legacy frame buffer, or files.
//!
//! An output owns one or more displays ("heads"). The daemon keeps one
//! [`crate::scene::Scene`] per head and hands each new frame to
//! [`Output::present`] together with the rectangles that changed, so only
//! those pixels are copied and flushed.

use std::fs::File;
use std::io;
use std::path::Path;
use std::time::{Duration, Instant};

use crate::gfx::{Canvas, Rect};

pub mod drm;
pub mod fbdev;
pub mod headless;

/// A display back end.
pub trait Output {
    /// Short name for messages: `drm`, `fbdev` or `headless`.
    fn name(&self) -> &'static str;

    /// Size in pixels of each head.
    fn sizes(&self) -> Vec<(u32, u32)>;

    /// Copy the damaged parts of `canvas` to head `index` and flush them.
    /// An error means the device is gone and the output must be reopened.
    fn present(&mut self, index: usize, canvas: &Canvas, damage: &[Rect]) -> io::Result<()>;

    /// False once the device has been removed (for example when the
    /// firmware frame buffer is replaced by the real graphics driver).
    fn alive(&mut self) -> bool {
        true
    }

    /// Give the screen back to the text console.
    fn hide(&mut self) {}

    /// Take the screen again after [`Output::hide`]. The caller repaints.
    fn show(&mut self) -> io::Result<()> {
        Ok(())
    }

    /// Stop driving the display but leave the last frame on it, so the next
    /// program can take over without a blank screen in between. Returns
    /// false when this output cannot do that.
    fn retain(&mut self) -> bool {
        false
    }

    /// After [`Output::retain`]: true once another program shows its own
    /// picture on every head.
    fn taken_over(&mut self) -> bool {
        true
    }

    /// Do not restore the text console when this output is dropped.
    fn abandon(&mut self) {}

    /// Save the frame of head 0 as an image file (headless output only).
    fn snapshot(&mut self, _name: &str, _canvas: &Canvas) -> io::Result<()> {
        Err(io::Error::new(io::ErrorKind::Unsupported, "snapshots need the headless output"))
    }
}

/// Open the best available display: DRM/KMS first, then `/dev/fb0`.
/// Graphics devices can appear a moment after the boot screen starts, so
/// keep trying for `wait`.
pub fn open_display(wait: Duration) -> io::Result<Box<dyn Output>> {
    let deadline = Instant::now() + wait;
    let mut last;
    loop {
        match drm::DrmOutput::open_any() {
            Ok(out) => return Ok(Box::new(out)),
            Err(e) => last = e,
        }
        match fbdev::FbOutput::open(std::path::Path::new(fbdev::DEFAULT_DEVICE)) {
            Ok(out) => return Ok(Box::new(out)),
            Err(e) if last.kind() == io::ErrorKind::NotFound => last = e,
            Err(_) => {}
        }
        if Instant::now() >= deadline {
            return Err(io::Error::new(last.kind(), format!("no usable display: {last}")));
        }
        std::thread::sleep(Duration::from_millis(50));
    }
}

/// Look for a display again after the one in use was lost, from inside the
/// sandbox. A DRM device is preferred wherever it comes from: opened here
/// (possible while the device directory is the one the sandbox was entered
/// with), or opened by the supervisor and handed over through `helper`
/// (needed once that directory has been made again for a new driver). The
/// frame buffer is the last choice.
pub fn reopen_display(helper: Option<&mut crate::broker::Client>, wait: Duration) -> io::Result<Box<dyn Output>> {
    let direct = match drm::DrmOutput::open_any() {
        Ok(out) => return Ok(Box::new(out)),
        Err(e) => e,
    };
    if let Some(helper) = helper {
        if let Ok(out) = helper.devices(wait).and_then(open_devices) {
            return Ok(out);
        }
    }
    match fbdev::FbOutput::open(Path::new(fbdev::DEFAULT_DEVICE)) {
        Ok(out) => Ok(Box::new(out)),
        Err(_) => Err(io::Error::new(direct.kind(), format!("no usable display: {direct}"))),
    }
}

/// A graphics device that is already open.
#[derive(Debug)]
pub enum Device {
    /// A DRM device (`/dev/dri/cardN`).
    Card(File),
    /// A legacy frame buffer (`/dev/fb0`).
    FrameBuffer(File),
}

/// The same choice as [`open_display`] among devices that another process
/// opened and handed over (see [`crate::broker`]): the first DRM device with
/// a connected display, else a frame buffer.
pub fn open_devices(devices: Vec<Device>) -> io::Result<Box<dyn Output>> {
    let mut last = io::Error::new(io::ErrorKind::NotFound, "no graphics device");
    let mut frame_buffers = Vec::new();
    for device in devices {
        match device {
            Device::Card(file) => match drm::DrmOutput::from_file(file, Path::new(drm::DRI_DIR)) {
                Ok(out) => return Ok(Box::new(out)),
                Err(e) => last = e,
            },
            Device::FrameBuffer(file) => frame_buffers.push(file),
        }
    }
    for file in frame_buffers {
        match fbdev::FbOutput::from_file(file) {
            Ok(out) => return Ok(Box::new(out)),
            Err(e) => last = e,
        }
    }
    Err(io::Error::new(last.kind(), format!("no usable display: {last}")))
}

/// Copy the rows of `rect` from the canvas into a little-endian XRGB8888
/// buffer with the given stride. Shared by the DRM and fbdev outputs.
pub(crate) fn copy_xrgb(canvas: &Canvas, rect: &Rect, target: &mut [u8], stride: usize) {
    let rect = rect.intersect(&canvas.bounds());
    for y in rect.y..rect.bottom() {
        let row = canvas.row(y as u32, rect.x as u32, rect.w as u32);
        let start = y as usize * stride + rect.x as usize * 4;
        let Some(dest) = target.get_mut(start..start + row.len() * 4) else {
            return;
        };
        for (px, out) in row.iter().zip(dest.chunks_exact_mut(4)) {
            out.copy_from_slice(&px.to_le_bytes());
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::gfx::Rgb;

    #[test]
    fn copy_respects_stride_and_damage() {
        let mut canvas = Canvas::new(4, 3);
        canvas.fill_rect(&Rect::new(1, 1, 2, 1), Rgb::new(0x11, 0x22, 0x33));
        let stride = 4 * 4 + 8; // padded rows
        let mut target = vec![0xaau8; stride * 3];
        copy_xrgb(&canvas, &Rect::new(1, 1, 2, 1), &mut target, stride);
        let px = &target[stride + 4..stride + 8];
        assert_eq!(px, &[0x33, 0x22, 0x11, 0x00]);
        // Bytes outside the damage are untouched.
        assert_eq!(target[0], 0xaa);
        assert_eq!(target[stride], 0xaa);
        assert_eq!(target[stride + 12], 0xaa);
        // Damage outside the canvas or a short target is ignored.
        copy_xrgb(&canvas, &Rect::new(-5, -5, 100, 100), &mut target, stride);
        copy_xrgb(&canvas, &Rect::new(0, 0, 4, 3), &mut [0u8; 7], stride);
    }

    #[test]
    fn handed_over_files_that_are_no_graphics_devices_are_refused() {
        assert_eq!(open_devices(Vec::new()).err().unwrap().kind(), io::ErrorKind::NotFound);
        let null = || File::open("/dev/null").unwrap();
        assert!(open_devices(vec![Device::FrameBuffer(null()), Device::Card(null())]).is_err());
    }
}
