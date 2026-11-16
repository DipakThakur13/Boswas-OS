//! The main output: kernel mode setting with dumb buffers.
//!
//! No GPU acceleration and no libdrm: the boot screen needs a frame buffer it
//! can write to and nothing more, which every KMS driver offers ("dumb
//! buffers"), including `simpledrm` on the firmware frame buffer. Each
//! connected display gets one XRGB8888 buffer; a frame copies the damaged
//! rows into it and tells the driver which rectangles changed.
//!
//! The display configuration found at start (normally the kernel's text
//! console) is remembered and put back when the boot screen is hidden or
//! quits, as Plymouth does.

use std::fs::{File, OpenOptions};
use std::io;
use std::os::fd::{AsFd, BorrowedFd};
use std::path::{Path, PathBuf};

use drm::buffer::{Buffer, DrmFourcc};
use drm::control::dumbbuffer::{DumbBuffer, DumbMapping};
use drm::control::{connector, crtc, framebuffer, ClipRect, Device as ControlDevice, Mode, ModeTypeFlags};
use drm::{Device, DriverCapability};

use crate::gfx::{Canvas, Rect};

use super::{copy_xrgb, Output};

/// Directory of DRM device nodes.
pub const DRI_DIR: &str = "/dev/dri";

struct Card(File);

impl AsFd for Card {
    fn as_fd(&self) -> BorrowedFd<'_> {
        self.0.as_fd()
    }
}

impl Device for Card {}
impl ControlDevice for Card {}

/// One display driven by the boot screen.
struct Head {
    crtc: crtc::Handle,
    connector: connector::Handle,
    mode: Mode,
    framebuffer: framebuffer::Handle,
    buffer: DumbBuffer,
    mapping: DumbMapping<'static>,
    stride: usize,
    size: (u32, u32),
    /// What the CRTC showed before the boot screen took it.
    previous: Option<crtc::Info>,
    /// Whether this head currently shows our frame buffer.
    shown: bool,
}

/// A DRM device with every connected display showing the boot screen.
pub struct DrmOutput {
    // Field order matters: the heads (and their mappings) go before the card.
    heads: Vec<Head>,
    card: Card,
    path: PathBuf,
    restore_on_drop: bool,
}

impl std::fmt::Debug for DrmOutput {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("DrmOutput").field("path", &self.path).field("heads", &self.sizes()).finish()
    }
}

/// `card0`, `card1`, ... in numeric order.
pub fn card_paths(dir: &Path) -> Vec<PathBuf> {
    let mut cards: Vec<(u32, PathBuf)> = std::fs::read_dir(dir)
        .into_iter()
        .flatten()
        .flatten()
        .filter_map(|entry| {
            let name = entry.file_name();
            let number = name.to_str()?.strip_prefix("card")?.parse::<u32>().ok()?;
            Some((number, entry.path()))
        })
        .collect();
    cards.sort();
    cards.into_iter().map(|(_, path)| path).collect()
}

/// The mode to use on a connector: what the display already shows if that is
/// one of its modes (no mode switch, no flicker), else the preferred mode,
/// else the first.
fn choose_mode(modes: &[Mode], current: Option<Mode>) -> Option<Mode> {
    if let Some(current) = current {
        if modes.iter().any(|m| m.size() == current.size() && m.vrefresh() == current.vrefresh()) {
            return Some(current);
        }
    }
    modes.iter().find(|m| m.mode_type().contains(ModeTypeFlags::PREFERRED)).or(modes.first()).copied()
}

/// Errors from "tell the driver what changed" that only mean the driver
/// needs no such hint.
fn flush_not_needed(err: &io::Error) -> bool {
    matches!(err.raw_os_error(), Some(libc::ENOSYS) | Some(libc::EINVAL) | Some(libc::ENOTTY) | Some(libc::EOPNOTSUPP))
}

impl DrmOutput {
    /// Open the first DRM device that has a connected display.
    pub fn open_any() -> io::Result<DrmOutput> {
        let cards = card_paths(Path::new(DRI_DIR));
        let mut last = io::Error::new(io::ErrorKind::NotFound, "no DRM device");
        for path in cards {
            match DrmOutput::open(&path) {
                Ok(out) => return Ok(out),
                Err(e) => last = e,
            }
        }
        Err(last)
    }

    /// Open one DRM device and show a (still empty) buffer on every
    /// connected display.
    pub fn open(path: &Path) -> io::Result<DrmOutput> {
        let card = Card(OpenOptions::new().read(true).write(true).open(path)?);
        if card.get_driver_capability(DriverCapability::DumbBuffer)? == 0 {
            return Err(io::Error::new(io::ErrorKind::Unsupported, "the device has no dumb buffers"));
        }
        // Opening the device normally makes us the master already; asking
        // again is harmless and covers a device another program just left.
        let _ = card.acquire_master_lock();
        let mut out = DrmOutput { heads: Vec::new(), card, path: path.to_path_buf(), restore_on_drop: true };
        out.setup_heads()?;
        Ok(out)
    }

    fn setup_heads(&mut self) -> io::Result<()> {
        let card = &self.card;
        let resources = card.resource_handles()?;
        let mut taken: Vec<crtc::Handle> = Vec::new();
        let mut heads = Vec::new();
        let mut last_error = None;

        // Use the kernel's cached connector state first; probe only when
        // that finds nothing, because probing can take a noticeable time.
        for force_probe in [false, true] {
            for &handle in resources.connectors() {
                let Ok(info) = card.get_connector(handle, force_probe) else { continue };
                if info.state() == connector::State::Disconnected || info.modes().is_empty() {
                    continue;
                }
                // The CRTC already feeding this connector, else any CRTC it can use.
                let current = info
                    .current_encoder()
                    .and_then(|e| card.get_encoder(e).ok())
                    .and_then(|e| e.crtc())
                    .filter(|c| !taken.contains(c));
                let crtc_handle = current.or_else(|| {
                    info.encoders()
                        .iter()
                        .filter_map(|&e| card.get_encoder(e).ok())
                        .flat_map(|e| resources.filter_crtcs(e.possible_crtcs()))
                        .find(|c| !taken.contains(c))
                });
                let Some(crtc_handle) = crtc_handle else { continue };
                let previous = card.get_crtc(crtc_handle).ok();
                let current_mode = previous.as_ref().and_then(|p| p.mode());
                let Some(mode) = choose_mode(info.modes(), current_mode) else { continue };
                match Self::create_head(card, crtc_handle, handle, mode, previous) {
                    Ok(head) => {
                        taken.push(crtc_handle);
                        heads.push(head);
                    }
                    Err(e) => last_error = Some(e),
                }
            }
            if !heads.is_empty() {
                break;
            }
        }
        if heads.is_empty() {
            return Err(last_error.unwrap_or_else(|| io::Error::new(io::ErrorKind::NotFound, "no connected display")));
        }
        self.heads = heads;
        Ok(())
    }

    fn create_head(
        card: &Card,
        crtc: crtc::Handle,
        connector: connector::Handle,
        mode: Mode,
        previous: Option<crtc::Info>,
    ) -> io::Result<Head> {
        let (width, height) = mode.size();
        let (width, height) = (width as u32, height as u32);
        if width == 0 || height == 0 {
            return Err(io::Error::new(io::ErrorKind::InvalidData, "mode without a size"));
        }
        let buffer = card.create_dumb_buffer((width, height), DrmFourcc::Xrgb8888, 32)?;
        let framebuffer = match card.add_framebuffer(&buffer, 24, 32) {
            Ok(fb) => fb,
            Err(e) => {
                let _ = card.destroy_dumb_buffer(buffer);
                return Err(e);
            }
        };
        // The mapping borrows the buffer description for as long as it lives;
        // giving that description a 'static home (a few bytes, once per
        // display) lets the mapping be stored next to the device.
        let description: &'static mut DumbBuffer = Box::leak(Box::new(buffer));
        let mapping = match card.map_dumb_buffer(description) {
            Ok(m) => m,
            Err(e) => {
                let _ = card.destroy_framebuffer(framebuffer);
                let _ = card.destroy_dumb_buffer(buffer);
                return Err(e);
            }
        };
        let stride = buffer.pitch() as usize;
        if stride < width as usize * 4 || mapping.len() < stride * (height as usize - 1) + width as usize * 4 {
            let _ = card.destroy_framebuffer(framebuffer);
            return Err(io::Error::new(io::ErrorKind::InvalidData, "dumb buffer smaller than the mode"));
        }
        Ok(Head {
            crtc,
            connector,
            mode,
            framebuffer,
            buffer,
            mapping,
            stride,
            size: (width, height),
            previous,
            shown: false,
        })
    }

    /// Put back what each display showed before the boot screen.
    fn restore_previous(&mut self) {
        for head in self.heads.iter_mut().filter(|h| h.shown) {
            head.shown = false;
            if let Some(previous) = &head.previous {
                if previous.framebuffer().is_some() {
                    let _ = self.card.set_crtc(
                        head.crtc,
                        previous.framebuffer(),
                        previous.position(),
                        &[head.connector],
                        previous.mode(),
                    );
                }
            }
        }
    }
}

impl Output for DrmOutput {
    fn name(&self) -> &'static str {
        "drm"
    }

    fn sizes(&self) -> Vec<(u32, u32)> {
        self.heads.iter().map(|h| h.size).collect()
    }

    fn present(&mut self, index: usize, canvas: &Canvas, damage: &[Rect]) -> io::Result<()> {
        let DrmOutput { heads, card, .. } = self;
        let Some(head) = heads.get_mut(index) else {
            return Err(io::Error::new(io::ErrorKind::InvalidInput, "no such head"));
        };
        let screen = Rect::new(0, 0, head.size.0 as i32, head.size.1 as i32);
        let mut clips = Vec::with_capacity(damage.len());
        for rect in damage {
            let rect = rect.intersect(&screen);
            if rect.is_empty() {
                continue;
            }
            copy_xrgb(canvas, &rect, &mut head.mapping, head.stride);
            clips.push(ClipRect::new(rect.x as u16, rect.y as u16, rect.right() as u16, rect.bottom() as u16));
        }
        if !head.shown {
            // The buffer holds a complete frame before it goes on screen.
            card.set_crtc(head.crtc, Some(head.framebuffer), (0, 0), &[head.connector], Some(head.mode))?;
            head.shown = true;
        }
        if clips.is_empty() {
            return Ok(());
        }
        match card.dirty_framebuffer(head.framebuffer, &clips) {
            Ok(()) => Ok(()),
            Err(e) if flush_not_needed(&e) => Ok(()),
            Err(e) => Err(e),
        }
    }

    fn alive(&mut self) -> bool {
        // Any ioctl on a removed device fails with ENODEV.
        self.card.get_driver_capability(DriverCapability::DumbBuffer).is_ok()
    }

    fn hide(&mut self) {
        if self.heads.iter().any(|h| h.shown) {
            self.restore_previous();
            let _ = self.card.release_master_lock();
        }
    }

    fn show(&mut self) -> io::Result<()> {
        // The next frame is a full repaint, which puts each buffer back on
        // its display (see `present`).
        self.card.acquire_master_lock()
    }

    fn retain(&mut self) -> bool {
        if !self.heads.iter().any(|h| h.shown) {
            return false;
        }
        // Without the master role this process no longer drives the display,
        // but its frame buffer stays on screen until the next master (the
        // greeter's compositor) shows its own.
        self.card.release_master_lock().is_ok()
    }

    fn taken_over(&mut self) -> bool {
        self.heads.iter().all(|head| match self.card.get_crtc(head.crtc) {
            Ok(info) => info.framebuffer() != Some(head.framebuffer),
            Err(_) => true,
        })
    }

    fn abandon(&mut self) {
        self.restore_on_drop = false;
    }
}

impl Drop for DrmOutput {
    fn drop(&mut self) {
        if self.restore_on_drop {
            self.restore_previous();
        }
        for head in self.heads.drain(..) {
            let Head { framebuffer, buffer, mapping, .. } = head;
            drop(mapping);
            let _ = self.card.destroy_framebuffer(framebuffer);
            let _ = self.card.destroy_dumb_buffer(buffer);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn card_nodes_are_listed_in_numeric_order() {
        let dir = std::env::temp_dir().join(format!("boswas-bootsplash-dri-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        for name in ["card10", "card2", "card0", "renderD128", "controlD64", "cardX", "by-path"] {
            std::fs::write(dir.join(name), b"").unwrap();
        }
        let names: Vec<String> =
            card_paths(&dir).iter().map(|p| p.file_name().unwrap().to_string_lossy().into_owned()).collect();
        std::fs::remove_dir_all(&dir).unwrap();
        assert_eq!(names, ["card0", "card2", "card10"]);
        assert!(card_paths(Path::new("/nonexistent/dri")).is_empty());
    }

    #[test]
    fn not_a_drm_device() {
        // /dev/null accepts the open but not the ioctls.
        assert!(DrmOutput::open(Path::new("/dev/null")).is_err());
        assert!(DrmOutput::open(Path::new("/nonexistent/card0")).is_err());
    }

    #[test]
    fn flush_errors_that_mean_no_flush_needed() {
        assert!(flush_not_needed(&io::Error::from_raw_os_error(libc::ENOSYS)));
        assert!(flush_not_needed(&io::Error::from_raw_os_error(libc::EINVAL)));
        assert!(!flush_not_needed(&io::Error::from_raw_os_error(libc::ENODEV)));
        assert!(!flush_not_needed(&io::Error::from_raw_os_error(libc::EIO)));
    }
}
