//! Surfaces of a single colour as single-pixel buffers.
//!
//! The invisible surface behind a flyout (which takes the click that closes
//! it) and the smoke behind a dialog are one pixel stretched over the
//! screen. As a pixel of shared memory, the compositor scales a picture to
//! draw them, and an invisible one that appears makes it draw the whole
//! screen again (about 13 ms at 2560 x 1440 without a GPU) only to show
//! nothing. As a `wp_single_pixel_buffer_v1` buffer, the compositor fills
//! the colour instead, and leaves a fully transparent one out of the
//! picture altogether: opening a flyout then draws only the flyout.

use wayland_client::{
    delegate_noop,
    globals::GlobalList,
    protocol::wl_buffer::WlBuffer,
    QueueHandle,
};
use wayland_protocols::wp::single_pixel_buffer::v1::client::wp_single_pixel_buffer_manager_v1::WpSinglePixelBufferManagerV1;

use crate::Shell;

/// Colours kept as buffers at most (the transparent pixel and the smoke of
/// each scheme are all the shell asks for).
const MAX_KEPT: usize = 8;

#[derive(Default)]
pub struct Solid {
    manager: Option<WpSinglePixelBufferManagerV1>,
    /// Buffers made so far, by colour (premultiplied RGBA). A buffer of one
    /// colour serves every surface of that colour, and is never changed.
    kept: Vec<([u8; 4], WlBuffer)>,
}

impl Solid {
    pub fn bind(globals: &GlobalList, qh: &QueueHandle<Shell>) -> Self {
        Self {
            manager: globals.bind(qh, 1..=1, ()).ok(),
            kept: Vec::new(),
        }
    }

    /// A buffer of one premultiplied colour, or `None` when the compositor
    /// does not offer single-pixel buffers (a pixel of shared memory is
    /// sent instead).
    pub fn buffer(&mut self, rgba: [u8; 4], qh: &QueueHandle<Shell>) -> Option<WlBuffer> {
        let manager = self.manager.as_ref()?;
        if let Some((_, buffer)) = self.kept.iter().find(|(colour, _)| *colour == rgba) {
            return Some(buffer.clone());
        }
        if self.kept.len() >= MAX_KEPT {
            return None;
        }
        let [r, g, b, a] = rgba.map(channel);
        let buffer = manager.create_u32_rgba_buffer(r, g, b, a, qh, ());
        self.kept.push((rgba, buffer.clone()));
        Some(buffer)
    }
}

/// A channel of eight bits as the protocol's 32 (255 is `u32::MAX`).
pub fn channel(value: u8) -> u32 {
    u32::from(value) * 0x0101_0101
}

delegate_noop!(Shell: WpSinglePixelBufferManagerV1);
delegate_noop!(Shell: ignore WlBuffer);

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn channels_span_the_whole_range() {
        assert_eq!(channel(0), 0);
        assert_eq!(channel(255), u32::MAX);
        assert_eq!(channel(128), 0x8080_8080);
        // What the compositor reads back as eight bits is the value sent.
        for value in 0..=255u8 {
            assert_eq!((channel(value) / (u32::MAX / 255)) as u8, value);
        }
    }
}
