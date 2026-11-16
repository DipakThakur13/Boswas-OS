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
//!
//! A buffer is filled before it is put on its display, and filled again
//! right after: some drivers move a buffer to video memory when it is first
//! shown and what was written into it before is then gone (seen with
//! `bochs` after it replaced the firmware frame buffer). See [`frame_rects`].

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

/// The number of a card node: `card12` is 12. `renderD128`, `by-path` and
/// other entries of the directory are not cards.
pub fn card_number(name: &std::ffi::OsStr) -> Option<u32> {
    let digits = name.to_str()?.strip_prefix("card")?;
    if digits.is_empty() || !digits.bytes().all(|b| b.is_ascii_digit()) {
        return None;
    }
    digits.parse().ok()
}

/// `card0`, `card1`, ... in numeric order. A directory that does not exist
/// has no cards; one that cannot be read (the sandbox denies a directory
/// that was created again after it was entered) is an error.
pub fn card_paths(dir: &Path) -> io::Result<Vec<PathBuf>> {
    let entries = match std::fs::read_dir(dir) {
        Ok(entries) => entries,
        Err(e) if e.kind() == io::ErrorKind::NotFound => return Ok(Vec::new()),
        Err(e) => return Err(e),
    };
    let mut cards: Vec<(u32, PathBuf)> =
        entries.flatten().filter_map(|entry| Some((card_number(&entry.file_name())?, entry.path()))).collect();
    cards.sort();
    Ok(cards.into_iter().map(|(_, path)| path).collect())
}

/// The rectangles of a frame to copy into a head's buffer: the ones that
/// changed, or the whole screen when the buffer has just been put on its
/// display (`first_shown`), because that step can discard what the buffer
/// held.
fn frame_rects(screen: Rect, damage: &[Rect], first_shown: bool) -> Vec<Rect> {
    if first_shown {
        return vec![screen];
    }
    damage.iter().map(|rect| rect.intersect(&screen)).filter(|rect| !rect.is_empty()).collect()
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
        let cards = card_paths(Path::new(DRI_DIR))?;
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
        DrmOutput::from_file(OpenOptions::new().read(true).write(true).open(path)?, path)
    }

    /// Use a DRM device that is already open (`path` is for messages only)
    /// and show a (still empty) buffer on every connected display.
    pub fn from_file(file: File, path: &Path) -> io::Result<DrmOutput> {
        let card = Card(file);
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
        let mut first_shown = false;
        if !head.shown {
            // The buffer holds a complete frame before it goes on screen, so
            // no half-drawn picture is ever visible...
            copy_xrgb(canvas, &screen, &mut head.mapping, head.stride);
            card.set_crtc(head.crtc, Some(head.framebuffer), (0, 0), &[head.connector], Some(head.mode))?;
            head.shown = true;
            // ...and is filled once more below, now that it is on screen.
            first_shown = true;
        }
        let rects = frame_rects(screen, damage, first_shown);
        let mut clips = Vec::with_capacity(rects.len());
        for rect in &rects {
            copy_xrgb(canvas, rect, &mut head.mapping, head.stride);
            clips.push(ClipRect::new(rect.x as u16, rect.y as u16, rect.right() as u16, rect.bottom() as u16));
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
        self.restore_previous();
        // Also for a device that was opened while the screen was hidden and
        // has shown nothing yet: holding the master role would keep every
        // other program from driving the display.
        let _ = self.card.release_master_lock();
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
            card_paths(&dir).unwrap().iter().map(|p| p.file_name().unwrap().to_string_lossy().into_owned()).collect();
        std::fs::remove_dir_all(&dir).unwrap();
        assert_eq!(names, ["card0", "card2", "card10"]);
        assert!(card_paths(Path::new("/nonexistent/dri")).unwrap().is_empty());
        // Anything else that goes wrong is reported, not taken for "no cards".
        assert!(card_paths(Path::new("/dev/null")).is_err());
        assert_eq!(card_number(std::ffi::OsStr::new("card7")), Some(7));
        for other in ["card", "card-1", "card+1", "card0-HDMI-A-1", "renderD128"] {
            assert_eq!(card_number(std::ffi::OsStr::new(other)), None, "{other}");
        }
    }

    #[test]
    fn a_buffer_that_was_just_put_on_its_display_gets_the_whole_frame() {
        let screen = Rect::new(0, 0, 1024, 768);
        let ring = Rect::new(491, 577, 42, 42);
        // An ordinary frame: what changed, cut to the screen, nothing empty.
        assert_eq!(frame_rects(screen, &[ring], false), [ring]);
        assert_eq!(
            frame_rects(screen, &[Rect::new(1000, 700, 100, 100), Rect::new(2000, 0, 5, 5)], false),
            [Rect::new(1000, 700, 24, 68)]
        );
        assert!(frame_rects(screen, &[], false).is_empty());
        // The first frame after the mode was set: everything, whatever the
        // scene reported as changed (the mode-set may have emptied the buffer).
        assert_eq!(frame_rects(screen, &[ring], true), [screen]);
        assert_eq!(frame_rects(screen, &[], true), [screen]);
        assert_eq!(frame_rects(screen, &[screen], true), [screen]);
    }

    #[test]
    fn not_a_drm_device() {
        // /dev/null accepts the open but not the ioctls.
        assert!(DrmOutput::open(Path::new("/dev/null")).is_err());
        assert!(DrmOutput::open(Path::new("/nonexistent/card0")).is_err());
        assert!(DrmOutput::from_file(File::open("/dev/null").unwrap(), Path::new("null")).is_err());
    }

    #[test]
    fn flush_errors_that_mean_no_flush_needed() {
        assert!(flush_not_needed(&io::Error::from_raw_os_error(libc::ENOSYS)));
        assert!(flush_not_needed(&io::Error::from_raw_os_error(libc::EINVAL)));
        assert!(!flush_not_needed(&io::Error::from_raw_os_error(libc::ENODEV)));
        assert!(!flush_not_needed(&io::Error::from_raw_os_error(libc::EIO)));
    }
}
