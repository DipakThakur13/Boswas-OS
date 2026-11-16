//! Screen capture for other programs (wlr-screencopy): screenshot tools,
//! screen recorders, and the desktop portal behind screen sharing in
//! browsers and meetings.
//!
//! Nobody sees the screen without the user's say. A program that asks for a
//! picture waits until the shell has shown "Allow NAME to see your screen?"
//! and the user has answered. Allowing covers that program for as long as it
//! keeps capturing (a recording, a shared screen); a few seconds after its
//! last picture the permission is gone and the next request asks again.
//! While any program may capture, the shell shows an indicator with a stop
//! button. Sandboxed programs are not offered the protocol at all (they go
//! through the portal), and nothing is captured while the session is locked.
//!
//! The pictures are copied into the program's shared-memory buffer; that
//! copy is the one place in this file that needs `unsafe`.

use std::{
    collections::HashMap,
    sync::atomic::{AtomicBool, Ordering},
    time::{Duration, Instant},
};

use serde_json::json;
use smithay::{
    output::Output,
    reexports::{
        wayland_protocols_wlr::screencopy::v1::server::{
            zwlr_screencopy_frame_v1::{self, Flags, ZwlrScreencopyFrameV1},
            zwlr_screencopy_manager_v1::{self, ZwlrScreencopyManagerV1},
        },
        wayland_server::{
            backend::ClientId,
            protocol::{wl_buffer::WlBuffer, wl_shm},
            Client, DataInit, Dispatch, DisplayHandle, GlobalDispatch, New, Resource,
        },
    },
    utils::{Logical, Physical, Point, Rectangle, Size, Transform},
    wayland::shm::{with_buffer_contents, with_buffer_contents_mut, BufferData},
};
use tracing::debug;

use crate::state::{Boswas, ClientState, Trust};

/// How long after its last picture a program stays allowed.
pub const GRANT_IDLE: Duration = Duration::from_secs(5);
/// How long a refusal (or "stop") holds before the same program may ask
/// again, so that a retrying program cannot nag.
pub const DENIAL: Duration = Duration::from_secs(20);
/// How long the question stays open before it counts as "no".
pub const ASK_TIMEOUT: Duration = Duration::from_secs(60);

type Filter = Box<dyn for<'c> Fn(&'c Client) -> bool + Send + Sync>;

pub struct ScreencopyGlobal {
    filter: Filter,
}

/// What a frame object was created for.
pub struct FrameData {
    output: Output,
    /// The part of the output's buffer that is copied, in physical pixels
    /// of the output's own (unrotated) orientation. `None`: the capture
    /// failed when it was requested.
    region: Option<Rectangle<i32, Physical>>,
    overlay_cursor: bool,
    used: AtomicBool,
}

/// A frame whose buffer is attached, waiting for the next rendered frame.
pub struct Frame {
    frame: ZwlrScreencopyFrameV1,
    buffer: WlBuffer,
    pub output: Output,
    pub region: Rectangle<i32, Physical>,
    pub overlay_cursor: bool,
    with_damage: bool,
}

impl Frame {
    pub fn fail(self) {
        self.frame.failed();
    }

    /// Copies `rgba` (the whole output, `size` pixels, rows top to bottom)
    /// into the program's buffer and tells it the picture is there.
    pub fn finish(self, rgba: &[u8], size: Size<i32, Physical>, time: Duration) {
        match write_buffer(&self.buffer, rgba, size, self.region) {
            Ok(()) => {
                self.frame.flags(Flags::empty());
                if self.with_damage {
                    self.frame.damage(0, 0, self.region.size.w as u32, self.region.size.h as u32);
                }
                let secs = time.as_secs();
                self.frame.ready((secs >> 32) as u32, secs as u32, time.subsec_nanos());
            }
            Err(error) => {
                debug!("screen capture: {error}");
                self.frame.failed();
            }
        }
    }
}

enum Grant {
    Allowed { program: String, last_frame: Instant },
    Denied { until: Instant },
}

struct Ask {
    id: u64,
    client: ClientId,
    program: String,
    since: Instant,
}

#[derive(Default)]
pub struct Screencopy {
    /// Frames to fill after the next rendered frame.
    ready: Vec<Frame>,
    /// Frames whose program waits for the user's answer.
    waiting: Vec<(ClientId, Frame)>,
    grants: HashMap<ClientId, Grant>,
    asking: Vec<Ask>,
    next_ask: u64,
    /// The programs last reported to the shell as capturing.
    reported: Vec<String>,
    /// Programs the user said no to, by name, and until when the answer
    /// holds: a tool that is started again and again does not ask again and
    /// again.
    refused: Vec<(String, Instant)>,
}

impl Screencopy {
    /// Offers the protocol to the clients `filter` accepts.
    pub fn new(dh: &DisplayHandle, filter: impl for<'c> Fn(&'c Client) -> bool + Send + Sync + 'static) -> Self {
        dh.create_global::<Boswas, ZwlrScreencopyManagerV1, _>(3, ScreencopyGlobal { filter: Box::new(filter) });
        Self::default()
    }

    /// The frames to fill now.
    pub fn take_ready(&mut self) -> Vec<Frame> {
        std::mem::take(&mut self.ready)
    }

    pub fn has_ready(&self) -> bool {
        !self.ready.is_empty()
    }

    /// The programs that may capture right now.
    fn capturing(&self) -> Vec<String> {
        let mut names: Vec<String> = self
            .grants
            .values()
            .filter_map(|grant| match grant {
                Grant::Allowed { program, .. } => Some(program.clone()),
                Grant::Denied { .. } => None,
            })
            .collect();
        names.sort();
        names.dedup();
        names
    }
}

/// The part of an output's buffer a capture covers: all of it, or the
/// requested rectangle (in the output's logical coordinates) clipped to the
/// output. `None` when nothing is left.
pub fn frame_region(
    mode: Size<i32, Physical>,
    transform: Transform,
    scale: f64,
    logical: Option<Rectangle<i32, Logical>>,
) -> Option<Rectangle<i32, Physical>> {
    if mode.w <= 0 || mode.h <= 0 {
        return None;
    }
    let Some(logical) = logical else {
        return Some(Rectangle::from_size(mode));
    };
    // As the user sees the output: rotated.
    let upright = transform.transform_size(mode);
    let wanted = logical.to_f64().to_physical(scale).to_i32_round::<i32>();
    let visible = wanted.intersection(Rectangle::from_size(upright))?;
    if visible.size.w <= 0 || visible.size.h <= 0 {
        return None;
    }
    // Into the buffer's own orientation.
    Some(transform.transform_rect_in(visible, &upright))
}

/// The name shown for a program that asks to capture: the file name of its
/// executable. (The kernel's short process name is not used: it is cut to
/// fifteen characters and a process can set its own.)
fn program_name(client: &Client, dh: &DisplayHandle) -> String {
    let pid = client.get_credentials(dh).ok().map(|credentials| credentials.pid);
    let executable = pid.and_then(|pid| std::fs::read_link(format!("/proc/{pid}/exe")).ok());
    let name = executable.as_deref().and_then(|path| path.file_name()).map(|name| name.to_string_lossy().into_owned());
    display_name(name.as_deref().unwrap_or_default())
}

/// A program's file name made fit to show: without the mark the kernel
/// adds to a replaced file, without control characters, of bounded length.
fn display_name(file_name: &str) -> String {
    let name = file_name.strip_suffix(" (deleted)").unwrap_or(file_name);
    let name: String = name.chars().filter(|c| !c.is_control()).take(48).collect();
    if name.trim().is_empty() {
        "A program".to_owned()
    } else {
        name
    }
}

/// Copies the `region` of an RGBA picture of `size` pixels into a client's
/// XRGB8888 or ARGB8888 buffer of exactly the region's size.
fn write_buffer(buffer: &WlBuffer, rgba: &[u8], size: Size<i32, Physical>, region: Rectangle<i32, Physical>) -> Result<(), String> {
    let (width, height) = (size.w as usize, size.h as usize);
    if rgba.len() != width * height * 4 {
        return Err("the rendered picture has an unexpected size".into());
    }
    let inside = region.loc.x >= 0
        && region.loc.y >= 0
        && region.size.w > 0
        && region.size.h > 0
        && region.loc.x + region.size.w <= size.w
        && region.loc.y + region.size.h <= size.h;
    if !inside {
        return Err("the output changed while it was being captured".into());
    }
    let (rx, ry) = (region.loc.x as usize, region.loc.y as usize);
    let (rw, rh) = (region.size.w as usize, region.size.h as usize);
    // One row at a time, converted from RGBA to the buffer's byte order
    // (blue, green, red, alpha).
    let mut row = vec![0u8; rw * 4];
    let result = with_buffer_contents_mut(buffer, |pointer, len, data| {
        if !buffer_fits(&data, region.size) {
            return Err("the buffer does not fit the frame".to_owned());
        }
        let (offset, stride) = (data.offset as usize, data.stride as usize);
        let needed = offset.checked_add(stride.checked_mul(rh - 1).ok_or("overflow")?).and_then(|n| n.checked_add(rw * 4)).ok_or("overflow")?;
        if needed > len {
            return Err("the buffer is smaller than it claims".to_owned());
        }
        for y in 0..rh {
            let source = &rgba[((ry + y) * width + rx) * 4..][..rw * 4];
            for (out, pixel) in row.chunks_exact_mut(4).zip(source.chunks_exact(4)) {
                out[0] = pixel[2];
                out[1] = pixel[1];
                out[2] = pixel[0];
                out[3] = 0xFF;
            }
            // SAFETY: `pointer` is valid for `len` bytes while this closure
            // runs (smithay holds the pool's mapping and turns a fault from
            // a truncated file into an error), and the destination range
            // `offset + y * stride .. + rw * 4` was checked above to end
            // within `len`. The source is a local vector of exactly
            // `rw * 4` bytes that cannot overlap the client's memory. Only
            // raw copies are made: no reference into the shared memory is
            // created, so a client writing to it at the same time can at
            // worst garble its own picture.
            unsafe {
                std::ptr::copy_nonoverlapping(row.as_ptr(), pointer.add(offset + y * stride), rw * 4);
            }
        }
        Ok(())
    });
    match result {
        Ok(inner) => inner,
        Err(error) => Err(format!("the buffer cannot be written: {error}")),
    }
}

/// Whether a client's buffer is what the `buffer` event asked for.
fn buffer_fits(data: &BufferData, size: Size<i32, Physical>) -> bool {
    matches!(data.format, wl_shm::Format::Xrgb8888 | wl_shm::Format::Argb8888)
        && data.width == size.w
        && data.height == size.h
        && data.stride == size.w * 4
        && data.offset >= 0
}

impl Boswas {
    /// A frame's buffer arrived: fill it after the next rendered frame if
    /// its program may capture, otherwise ask the user first.
    fn screencopy_submit(&mut self, client: &Client, frame: Frame) {
        if self.lock.locked {
            return frame.fail();
        }
        let id = client.id();
        let now = Instant::now();
        let trusted = client.get_data::<ClientState>().is_some_and(|data| data.trust == Trust::Shell);
        let allowed = trusted
            || match self.screencopy.grants.get_mut(&id) {
                Some(Grant::Allowed { last_frame, .. }) if now.duration_since(*last_frame) < GRANT_IDLE => {
                    *last_frame = now;
                    true
                }
                Some(Grant::Denied { until }) if now < *until => return frame.fail(),
                _ => false,
            };
        if allowed {
            // A plain copy wants the screen as it is now; a copy "with
            // damage" waits until something changes.
            if !frame.with_damage {
                self.schedule_redraw();
            }
            self.screencopy.ready.push(frame);
            return;
        }
        self.screencopy.grants.remove(&id);
        self.screencopy.waiting.push((id.clone(), frame));
        if self.screencopy.asking.iter().any(|ask| ask.client == id) {
            return;
        }
        if !self.ipc_has_subscriber() {
            // Nobody can ask the user: the answer is no.
            return self.screencopy_answer_client(&id, None, false);
        }
        let program = program_name(client, &self.display_handle);
        if self.screencopy.refused.iter().any(|(name, until)| *name == program && now < *until) {
            return self.screencopy_answer_client(&id, None, false);
        }
        self.screencopy.next_ask += 1;
        let ask = self.screencopy.next_ask;
        self.screencopy.asking.push(Ask {
            id: ask,
            client: id,
            program: program.clone(),
            since: now,
        });
        self.ipc_event("capture-request", json!({ "id": ask, "program": program }));
    }

    /// The user's answer to the question `ask` (`capture-answer` on the
    /// shell's control connection). Returns whether the question was open.
    pub fn screencopy_answer(&mut self, ask: u64, allow: bool) -> bool {
        let Some(position) = self.screencopy.asking.iter().position(|a| a.id == ask) else {
            return false;
        };
        let ask = self.screencopy.asking.remove(position);
        self.screencopy_answer_client(&ask.client, Some(ask.program), allow);
        true
    }

    fn screencopy_answer_client(&mut self, client: &ClientId, program: Option<String>, allow: bool) {
        let now = Instant::now();
        let (mine, others): (Vec<_>, Vec<_>) = std::mem::take(&mut self.screencopy.waiting).into_iter().partition(|(id, _)| id == client);
        self.screencopy.waiting = others;
        if allow && !self.lock.locked {
            self.screencopy.grants.insert(
                client.clone(),
                Grant::Allowed {
                    program: program.unwrap_or_else(|| "A program".to_owned()),
                    last_frame: now,
                },
            );
            self.screencopy.ready.extend(mine.into_iter().map(|(_, frame)| frame));
            self.schedule_redraw();
        } else {
            self.screencopy.grants.insert(client.clone(), Grant::Denied { until: now + DENIAL });
            if let Some(program) = program {
                self.screencopy.refused.retain(|(name, _)| *name != program);
                self.screencopy.refused.push((program, now + DENIAL));
            }
            for (_, frame) in mine {
                frame.fail();
            }
        }
        self.screencopy_report();
    }

    /// The user pressed "Stop" on the capture indicator: every permission
    /// ends now.
    pub fn screencopy_stop(&mut self) {
        let until = Instant::now() + DENIAL;
        for grant in self.screencopy.grants.values_mut() {
            if let Grant::Allowed { program, .. } = grant {
                self.screencopy.refused.push((std::mem::take(program), until));
            }
            *grant = Grant::Denied { until };
        }
        for frame in self.screencopy.take_ready() {
            frame.fail();
        }
        self.screencopy_report();
    }

    /// Ends permissions that were not used for a while, questions nobody
    /// answered, and everything when the session locks. Called once a
    /// second.
    pub fn screencopy_tick(&mut self) {
        let now = Instant::now();
        let locked = self.lock.locked;
        self.screencopy.grants.retain(|_, grant| match grant {
            Grant::Allowed { last_frame, .. } => !locked && now.duration_since(*last_frame) < GRANT_IDLE,
            Grant::Denied { until } => now < *until,
        });
        self.screencopy.refused.retain(|(_, until)| now < *until);
        let expired: Vec<u64> = self
            .screencopy
            .asking
            .iter()
            .filter(|ask| locked || now.duration_since(ask.since) >= ASK_TIMEOUT)
            .map(|ask| ask.id)
            .collect();
        for ask in expired {
            self.screencopy_answer(ask, false);
            self.ipc_event("capture-request-closed", json!({ "id": ask }));
        }
        if locked {
            for frame in self.screencopy.take_ready() {
                frame.fail();
            }
        }
        // A frame "with damage" that has waited through a quiet second is
        // answered anyway, so that a recording of a still screen goes on.
        if self.screencopy.has_ready() {
            self.schedule_redraw();
        }
        self.screencopy_report();
    }

    /// Tells the shell which programs may capture, when that changed.
    fn screencopy_report(&mut self) {
        let capturing = self.screencopy.capturing();
        if capturing != self.screencopy.reported {
            self.screencopy.reported.clone_from(&capturing);
            self.ipc_event("capturing", json!({ "programs": capturing }));
        }
    }

    /// The capture state for a shell that has just connected.
    pub fn screencopy_json(&self) -> serde_json::Value {
        json!({ "programs": self.screencopy.capturing() })
    }
}

impl GlobalDispatch<ZwlrScreencopyManagerV1, ScreencopyGlobal> for Boswas {
    fn bind(
        _state: &mut Self,
        _dh: &DisplayHandle,
        _client: &Client,
        resource: New<ZwlrScreencopyManagerV1>,
        _global: &ScreencopyGlobal,
        data_init: &mut DataInit<'_, Self>,
    ) {
        data_init.init(resource, ());
    }

    fn can_view(client: Client, global: &ScreencopyGlobal) -> bool {
        (global.filter)(&client)
    }
}

impl Dispatch<ZwlrScreencopyManagerV1, ()> for Boswas {
    fn request(
        state: &mut Self,
        _client: &Client,
        _manager: &ZwlrScreencopyManagerV1,
        request: zwlr_screencopy_manager_v1::Request,
        _data: &(),
        _dh: &DisplayHandle,
        data_init: &mut DataInit<'_, Self>,
    ) {
        let (frame, overlay_cursor, output, logical) = match request {
            zwlr_screencopy_manager_v1::Request::CaptureOutput {
                frame,
                overlay_cursor,
                output,
            } => (frame, overlay_cursor, output, None),
            zwlr_screencopy_manager_v1::Request::CaptureOutputRegion {
                frame,
                overlay_cursor,
                output,
                x,
                y,
                width,
                height,
            } => (
                frame,
                overlay_cursor,
                output,
                Some(Rectangle::new(Point::from((x, y)), Size::from((width.max(0), height.max(0))))),
            ),
            _ => return,
        };
        let known = Output::from_resource(&output).filter(|output| state.space.outputs().any(|o| o == output));
        let region = known.as_ref().filter(|_| !state.lock.locked).and_then(|output| {
            let mode = output.current_mode()?;
            frame_region(mode.size, output.current_transform(), output.current_scale().fractional_scale(), logical)
        });
        // The frame object exists either way; a capture that cannot happen
        // says so at once.
        let output = known.or_else(|| state.space.outputs().next().cloned());
        let Some(output) = output else {
            // No output at all: there is nothing a frame could refer to.
            let frame = data_init.init(
                frame,
                FrameData {
                    output: Output::new(
                        "none".into(),
                        smithay::output::PhysicalProperties {
                            size: (0, 0).into(),
                            subpixel: smithay::output::Subpixel::Unknown,
                            make: String::new(),
                            model: String::new(),
                        },
                    ),
                    region: None,
                    overlay_cursor: false,
                    used: AtomicBool::new(true),
                },
            );
            return frame.failed();
        };
        let frame = data_init.init(
            frame,
            FrameData {
                output,
                region,
                overlay_cursor: overlay_cursor != 0,
                used: AtomicBool::new(region.is_none()),
            },
        );
        match region {
            Some(region) => {
                frame.buffer(wl_shm::Format::Xrgb8888, region.size.w as u32, region.size.h as u32, region.size.w as u32 * 4);
                if frame.version() >= 3 {
                    frame.buffer_done();
                }
            }
            None => frame.failed(),
        }
    }
}

impl Dispatch<ZwlrScreencopyFrameV1, FrameData> for Boswas {
    fn request(
        state: &mut Self,
        client: &Client,
        frame: &ZwlrScreencopyFrameV1,
        request: zwlr_screencopy_frame_v1::Request,
        data: &FrameData,
        _dh: &DisplayHandle,
        _data_init: &mut DataInit<'_, Self>,
    ) {
        let (buffer, with_damage) = match request {
            zwlr_screencopy_frame_v1::Request::Copy { buffer } => (buffer, false),
            zwlr_screencopy_frame_v1::Request::CopyWithDamage { buffer } => (buffer, true),
            _ => return,
        };
        let Some(region) = data.region else {
            // The capture had failed already; nothing more is said.
            return;
        };
        if data.used.swap(true, Ordering::SeqCst) {
            frame.post_error(zwlr_screencopy_frame_v1::Error::AlreadyUsed, "the frame was already copied");
            return;
        }
        let fits = with_buffer_contents(&buffer, |_, _, buffer| buffer_fits(&buffer, region.size)).unwrap_or(false);
        if !fits {
            frame.post_error(zwlr_screencopy_frame_v1::Error::InvalidBuffer, "the buffer does not fit the frame");
            return;
        }
        state.screencopy_submit(
            client,
            Frame {
                frame: frame.clone(),
                buffer,
                output: data.output.clone(),
                region,
                overlay_cursor: data.overlay_cursor,
                with_damage,
            },
        );
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn rect<Kind>(x: i32, y: i32, w: i32, h: i32) -> Rectangle<i32, Kind> {
        Rectangle::new(Point::from((x, y)), Size::from((w, h)))
    }

    #[test]
    fn a_whole_output_is_its_mode() {
        let mode = Size::from((1920, 1080));
        assert_eq!(frame_region(mode, Transform::Normal, 1.0, None), Some(rect(0, 0, 1920, 1080)));
        // Rotating or scaling the output does not change its buffer.
        assert_eq!(frame_region(mode, Transform::_90, 1.5, None), Some(rect(0, 0, 1920, 1080)));
        assert_eq!(frame_region(Size::from((0, 1080)), Transform::Normal, 1.0, None), None);
    }

    #[test]
    fn a_region_is_scaled_and_clipped_to_the_output() {
        let mode = Size::from((1920, 1080));
        assert_eq!(frame_region(mode, Transform::Normal, 1.0, Some(rect(10, 20, 300, 200))), Some(rect(10, 20, 300, 200)));
        // Logical coordinates: at 150 % one logical pixel is 1.5 physical.
        assert_eq!(frame_region(mode, Transform::Normal, 1.5, Some(rect(10, 20, 300, 200))), Some(rect(15, 30, 450, 300)));
        // What hangs over the edge is cut off; what is outside is nothing.
        assert_eq!(frame_region(mode, Transform::Normal, 1.0, Some(rect(1900, 1000, 300, 200))), Some(rect(1900, 1000, 20, 80)));
        assert_eq!(frame_region(mode, Transform::Normal, 1.0, Some(rect(-50, -50, 100, 100))), Some(rect(0, 0, 50, 50)));
        assert_eq!(frame_region(mode, Transform::Normal, 1.0, Some(rect(5000, 0, 10, 10))), None);
        assert_eq!(frame_region(mode, Transform::Normal, 1.0, Some(rect(0, 0, 0, 10))), None);
    }

    #[test]
    fn a_region_of_a_rotated_output_stays_inside_its_buffer() {
        let mode: Size<i32, Physical> = Size::from((1920, 1080));
        for transform in [Transform::_90, Transform::_180, Transform::_270, Transform::Flipped, Transform::Flipped90] {
            // The user sees a rotated output: 1080 wide and 1920 high for
            // a quarter turn.
            let upright = transform.transform_size(mode);
            let whole = frame_region(mode, transform, 1.0, Some(Rectangle::from_size(Size::from((upright.w, upright.h))))).unwrap();
            assert_eq!(whole, rect(0, 0, 1920, 1080), "{transform:?}");
            let part = frame_region(mode, transform, 1.0, Some(rect(100, 50, 300, 200))).unwrap();
            assert!(Rectangle::from_size(mode).contains_rect(part), "{transform:?}: {part:?}");
            let turned = matches!(transform, Transform::_90 | Transform::_270 | Transform::Flipped90);
            assert_eq!((part.size.w, part.size.h), if turned { (200, 300) } else { (300, 200) }, "{transform:?}");
        }
    }

    #[test]
    fn program_names_are_made_fit_to_show() {
        assert_eq!(display_name("xdg-desktop-portal-wlr"), "xdg-desktop-portal-wlr");
        assert_eq!(display_name("obs (deleted)"), "obs");
        assert_eq!(display_name("evil\u{1b}[31m\nname"), "evil[31mname");
        assert_eq!(display_name(""), "A program");
        assert_eq!(display_name("   "), "A program");
        assert_eq!(display_name(&"x".repeat(200)).chars().count(), 48);
    }

    #[test]
    fn only_a_buffer_of_the_announced_shape_is_accepted() {
        let size: Size<i32, Physical> = Size::from((640, 480));
        let good = BufferData {
            offset: 0,
            width: 640,
            height: 480,
            stride: 2560,
            format: wl_shm::Format::Xrgb8888,
        };
        assert!(buffer_fits(&good, size));
        assert!(buffer_fits(&BufferData { format: wl_shm::Format::Argb8888, ..good }, size));
        assert!(buffer_fits(&BufferData { offset: 4096, ..good }, size));
        assert!(!buffer_fits(&BufferData { width: 639, ..good }, size));
        assert!(!buffer_fits(&BufferData { height: 481, ..good }, size));
        assert!(!buffer_fits(&BufferData { stride: 2564, ..good }, size));
        assert!(!buffer_fits(&BufferData { offset: -4, ..good }, size));
        assert!(!buffer_fits(&BufferData { format: wl_shm::Format::Rgb565, ..good }, size));
    }
}
