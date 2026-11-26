//! The clipboard, watched and set through clipboard control
//! (`zwlr_data_control_v1`). The compositor offers that protocol to the
//! programs it starts itself and to no application (ADR-0026); only the
//! desktop binds it, never the lock screen or the login screen.
//!
//! * Watching: each time the clipboard changes, its types decide what is
//!   read (`clipboard::plan`). A password manager's hint is read first and
//!   alone; content marked secret is never asked for. Reading happens on a
//!   thread, with a size limit and a time limit, and the item joins the
//!   history (`clipboard.rs`) when it arrives. Nothing is read while the
//!   session is locked.
//! * Pasting an item of the history: it goes on the clipboard (served from
//!   memory by the shell) and the compositor gives the focused window the
//!   key stroke that pastes (`paste` on the trusted control connection).
//! * Inserting an emoji: the same, but what was on the clipboard is read
//!   first and put back once the window has read the emoji, so the user's
//!   clipboard is not lost. That copy lives only until it is back.
//!
//! What the shell puts on the clipboard carries the type
//! `application/x-boswas-shell`, so the history does not take it in again.

use std::{
    io::{Read, Write},
    os::{fd::AsFd, unix::net::UnixStream},
    sync::{Arc, Mutex},
    time::{Duration, Instant},
};

use smithay_client_toolkit::reexports::calloop::{
    channel::{self, Sender},
    timer::{TimeoutAction, Timer},
    LoopHandle,
};
use tiny_skia::Pixmap;
use tracing::{debug, warn};
use wayland_client::{event_created_child, globals::GlobalList, protocol::wl_seat::WlSeat, Connection, Dispatch, Proxy, QueueHandle};
use wayland_protocols_wlr::data_control::v1::client::{
    zwlr_data_control_device_v1::{self, ZwlrDataControlDeviceV1},
    zwlr_data_control_manager_v1::ZwlrDataControlManagerV1,
    zwlr_data_control_offer_v1::{self, ZwlrDataControlOfferV1},
    zwlr_data_control_source_v1::{self, ZwlrDataControlSourceV1},
};

use crate::{
    clipboard::{self, Content, Kind, Plan},
    state::Flyout,
    Role, Shell,
};

/// Most types of one clipboard offer taken note of.
const MAX_TYPES: usize = 64;
/// How long reading one clipboard offer may take.
const READ_TIME: Duration = Duration::from_secs(3);
/// How long the copy of the clipboard before an emoji may take, and how
/// much of it is kept.
const SNAPSHOT_TIME: Duration = Duration::from_millis(1500);
const SNAPSHOT_BYTES: usize = 32 * 1024 * 1024;
const SNAPSHOT_TYPES: usize = 24;
/// How long the window has to read the emoji before the clipboard is put
/// back anyway, and the pause after it has read it.
const RESTORE_AFTER: Duration = Duration::from_secs(2);
const RESTORE_GRACE: Duration = Duration::from_millis(250);
/// How long writing to a reader may take.
const WRITE_TIME: Duration = Duration::from_secs(5);

/// The types of an offer, as they arrive.
#[derive(Debug, Default)]
pub struct Types(Mutex<Vec<String>>);

/// Data offered on the clipboard: (type, bytes).
pub type Offers = Vec<(String, Arc<Vec<u8>>)>;

/// An item as read on a thread (pictures already decoded there).
pub enum ReadItem {
    Text(String),
    Files(Vec<String>),
    Image { png: Vec<u8>, width: u32, height: u32, thumbnail: Option<Pixmap> },
}

/// What a thread reports back.
pub enum Done {
    Hints { generation: u64, values: Vec<(String, Vec<u8>)> },
    Item { generation: u64, item: Option<ReadItem> },
    Snapshot { insert: u64, data: Vec<(String, Vec<u8>)> },
    Written { source: u64 },
}

struct Current {
    offer: ZwlrDataControlOfferV1,
    types: Vec<String>,
    generation: u64,
    /// The shell's own data, when it is the shell's.
    own: Option<Offers>,
}

/// Data the shell serves on the clipboard, until another program takes
/// it (the source is then cancelled).
struct Source {
    id: u64,
    offers: Offers,
}

enum Stage {
    /// Reading what is on the clipboard before the emoji goes on it.
    Snapshot,
    /// The emoji is on the clipboard and the paste was asked for.
    Pasting { source: u64, restore: Offers, paste: Option<u64> },
}

struct Insert {
    id: u64,
    text: String,
    stage: Stage,
}

/// The shell's side of clipboard control.
pub struct Watch {
    manager: ZwlrDataControlManagerV1,
    device: Option<ZwlrDataControlDeviceV1>,
    current: Option<Current>,
    generation: u64,
    sources: Vec<Source>,
    next_id: u64,
    sender: Sender<Done>,
    /// What the shell last put on the clipboard, and when.
    last_own: Option<(Offers, i64)>,
    /// When the item now on the clipboard was copied (for "Clear clipboard
    /// data").
    current_copied_at: Option<i64>,
    insert: Option<Insert>,
}

fn now() -> i64 {
    std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map_or(0, |d| d.as_secs() as i64)
}

impl Watch {
    /// Binds clipboard control, when the compositor offers it, and starts
    /// listening for what the reading threads report.
    pub fn bind(globals: &GlobalList, qh: &QueueHandle<Shell>, handle: &LoopHandle<'static, Shell>) -> Option<Self> {
        let manager: ZwlrDataControlManagerV1 = match globals.bind(qh, 1..=2, ()) {
            Ok(manager) => manager,
            Err(err) => {
                warn!("no clipboard control ({err}); there is no clipboard history and the emoji panel cannot insert");
                return None;
            }
        };
        let (sender, channel) = channel::channel::<Done>();
        let inserted = handle.insert_source(channel, |event, _, shell: &mut Shell| {
            if let channel::Event::Msg(done) = event {
                shell.clip_done(done);
            }
        });
        if let Err(err) = inserted {
            warn!("clipboard: {err}");
            return None;
        }
        Some(Self {
            manager,
            device: None,
            current: None,
            generation: 0,
            sources: Vec::new(),
            next_id: 1,
            sender,
            last_own: None,
            current_copied_at: None,
            insert: None,
        })
    }

    fn take_id(&mut self) -> u64 {
        let id = self.next_id;
        self.next_id += 1;
        id
    }
}

/// Reads one transfer to its end, within the limits. `None` when it took
/// too long, failed, or was larger than `cap` (the rest is then drained,
/// so the writing program sees a reader to the end).
fn read_transfer(mut stream: UnixStream, cap: usize, deadline: Instant) -> Option<Vec<u8>> {
    let mut out = Vec::new();
    let mut chunk = vec![0u8; 64 * 1024];
    let mut over = false;
    loop {
        let left = deadline.checked_duration_since(Instant::now()).filter(|d| !d.is_zero())?;
        stream.set_read_timeout(Some(left)).ok()?;
        match stream.read(&mut chunk) {
            Ok(0) => return (!over).then_some(out),
            Ok(n) if over || out.len() + n > cap => {
                over = true;
                out = Vec::new();
            }
            Ok(n) => out.extend_from_slice(&chunk[..n]),
            Err(err) if err.kind() == std::io::ErrorKind::Interrupted => continue,
            Err(_) => return None,
        }
    }
}

/// The most bytes read of an item of each kind.
fn cap(kind: Kind) -> usize {
    match kind {
        Kind::Text => clipboard::MAX_TEXT_BYTES,
        Kind::Html => 4 * clipboard::MAX_TEXT_BYTES,
        Kind::Files => 512 * 1024,
        Kind::Image => clipboard::MAX_IMAGE_BYTES,
    }
}

/// Turns what was read into an item (on the reading thread).
fn read_item(kind: Kind, bytes: &[u8]) -> Option<ReadItem> {
    if kind == Kind::Image {
        let picture = boswas_ui::icons::decode_image(bytes)?;
        let thumbnail = clipboard::thumbnail(&picture, clipboard::THUMBNAIL.0, clipboard::THUMBNAIL.1);
        return Some(ReadItem::Image { png: bytes.to_vec(), width: picture.width(), height: picture.height(), thumbnail });
    }
    match clipboard::content_from(kind, bytes)? {
        Content::Text(text) => Some(ReadItem::Text(text)),
        Content::Files(uris) => Some(ReadItem::Files(uris)),
        Content::Image { .. } => None,
    }
}

impl ReadItem {
    fn into_content(self) -> Content {
        match self {
            ReadItem::Text(text) => Content::Text(text),
            ReadItem::Files(uris) => Content::Files(uris),
            ReadItem::Image { png, width, height, thumbnail } => {
                Content::Image { png: Arc::new(png), width, height, thumbnail: thumbnail.map(std::rc::Rc::new) }
            }
        }
    }
}

/// Writes `bytes` to a reader on a thread, with a time limit; reports
/// when done.
fn write_transfer(fd: std::os::fd::OwnedFd, bytes: Arc<Vec<u8>>, done: Option<(Sender<Done>, u64)>) {
    let spawned = std::thread::Builder::new().name("clipboard".into()).spawn(move || {
        let _ = rustix::io::ioctl_fionbio(&fd, true);
        let mut file = std::fs::File::from(fd);
        let deadline = Instant::now() + WRITE_TIME;
        let mut written = 0;
        while written < bytes.len() && Instant::now() < deadline {
            match file.write(&bytes[written..]) {
                Ok(0) => break,
                Ok(n) => written += n,
                Err(err) if err.kind() == std::io::ErrorKind::WouldBlock => std::thread::sleep(Duration::from_millis(2)),
                Err(err) if err.kind() == std::io::ErrorKind::Interrupted => {}
                Err(_) => break,
            }
        }
        drop(file);
        if let Some((sender, source)) = done {
            let _ = sender.send(Done::Written { source });
        }
    });
    if let Err(err) = spawned {
        warn!("clipboard: no thread to write with ({err})");
    }
}

impl Shell {
    /// Clipboard control for a seat (the desktop has one).
    pub fn clip_seat(&mut self, qh: &QueueHandle<Shell>, seat: &WlSeat) {
        if let Some(watch) = self.clip.as_mut().filter(|w| w.device.is_none()) {
            watch.device = Some(watch.manager.get_data_device(seat, qh, ()));
        }
    }

    /// Asks the current offer for `types` and reads them on a thread.
    /// Returns the readers' thread, or `None` when nothing could be asked.
    fn clip_receive(&mut self, generation: u64, types: &[String]) -> Option<Vec<(String, UnixStream)>> {
        let watch = self.clip.as_ref()?;
        let current = watch.current.as_ref().filter(|c| c.generation == generation)?;
        let mut pairs = Vec::new();
        for mime in types {
            let (reader, writer) = UnixStream::pair().ok()?;
            current.offer.receive(mime.clone(), writer.as_fd());
            pairs.push((mime.clone(), reader, writer));
        }
        // The descriptors are on their way once the requests are sent; the
        // shell's ends of the writers go, so the readers see the end.
        let _ = self.conn.flush();
        Some(pairs.into_iter().map(|(mime, reader, _writer)| (mime, reader)).collect())
    }

    /// The clipboard changed (or was emptied).
    fn clipboard_changed(&mut self, offer: Option<ZwlrDataControlOfferV1>) {
        let Some(watch) = self.clip.as_mut() else {
            if let Some(offer) = offer {
                offer.destroy();
            }
            return;
        };
        watch.generation += 1;
        let generation = watch.generation;
        if let Some(old) = watch.current.take() {
            old.offer.destroy();
        }
        watch.current_copied_at = None;
        let Some(offer) = offer else {
            return;
        };
        let types = offer.data::<Types>().map(|t| t.0.lock().map(|l| l.clone()).unwrap_or_default()).unwrap_or_default();
        let own = types.iter().any(|t| t == clipboard::OWN_TYPE);
        let own_data = if own { watch.last_own.clone() } else { None };
        if let Some((_, at)) = &own_data {
            watch.current_copied_at = Some(*at);
        }
        watch.current = Some(Current { offer, types: types.clone(), generation, own: own_data.map(|(data, _)| data) });
        if own || self.st.locked || !self.st.clips.enabled {
            return;
        }
        self.clip_plan(generation, false);
    }

    /// Reads what the plan for the current offer says.
    fn clip_plan(&mut self, generation: u64, hints_read: bool) {
        let Some(types) = self.clip.as_ref().and_then(|w| w.current.as_ref()).filter(|c| c.generation == generation).map(|c| c.types.clone()) else {
            return;
        };
        let (wanted, kind) = match clipboard::plan(&types, hints_read) {
            Plan::Skip | Plan::Nothing => return,
            Plan::Hints(hints) => (hints, None),
            Plan::Read { mime, kind } => (vec![mime], Some(kind)),
        };
        let Some(readers) = self.clip_receive(generation, &wanted) else {
            return;
        };
        let Some(sender) = self.clip.as_ref().map(|w| w.sender.clone()) else {
            return;
        };
        let spawned = std::thread::Builder::new().name("clipboard".into()).spawn(move || {
            let deadline = Instant::now() + READ_TIME;
            let done = match kind {
                None => Done::Hints {
                    generation,
                    values: readers.into_iter().filter_map(|(mime, r)| Some((mime, read_transfer(r, 256, deadline)?))).collect(),
                },
                Some(kind) => Done::Item {
                    generation,
                    item: readers.into_iter().next().and_then(|(_, r)| read_transfer(r, cap(kind), deadline)).and_then(|bytes| read_item(kind, &bytes)),
                },
            };
            let _ = sender.send(done);
        });
        if let Err(err) = spawned {
            warn!("clipboard: no thread to read with ({err})");
        }
    }

    fn current_generation(&self) -> Option<u64> {
        self.clip.as_ref()?.current.as_ref().map(|c| c.generation)
    }

    /// What a reading or writing thread reports.
    fn clip_done(&mut self, done: Done) {
        match done {
            Done::Hints { generation, values } => {
                if self.current_generation() != Some(generation) {
                    return;
                }
                if clipboard::hints_say_secret(&values) {
                    debug!("the clipboard holds something marked secret; not kept");
                    return;
                }
                self.clip_plan(generation, true);
            }
            Done::Item { generation, item } => {
                let Some(item) = item.filter(|_| self.current_generation() == Some(generation) && !self.st.locked) else {
                    return;
                };
                let now = now();
                if let Some(watch) = self.clip.as_mut() {
                    watch.current_copied_at = Some(now);
                }
                if self.st.clips.add(item.into_content(), now) && self.st.flyout == Some(Flyout::Clipboard) {
                    self.mark(Role::Flyout);
                }
            }
            Done::Snapshot { insert, data } => {
                let Some(watch) = self.clip.as_mut() else {
                    return;
                };
                let Some(pending) = watch.insert.take_if(|i| i.id == insert && matches!(i.stage, Stage::Snapshot)) else {
                    return;
                };
                let restore = data.into_iter().map(|(mime, bytes)| (mime, Arc::new(bytes))).collect();
                self.insert_paste(pending.id, pending.text, restore);
            }
            Done::Written { source } => {
                let insert = self.clip.as_ref().and_then(|w| w.insert.as_ref()).and_then(|i| match i.stage {
                    Stage::Pasting { source: s, .. } if s == source => Some(i.id),
                    _ => None,
                });
                if let Some(insert) = insert {
                    self.restore_later(insert, RESTORE_GRACE);
                }
            }
        }
    }

    /// Puts data on the clipboard, served by the shell. Returns the
    /// source's id.
    fn clip_offer(&mut self, offers: Offers) -> Option<u64> {
        let qh = self.qh.clone();
        let watch = self.clip.as_mut()?;
        watch.device.as_ref()?;
        let id = watch.take_id();
        let device = watch.device.as_ref()?;
        let source = watch.manager.create_data_source(&qh, id);
        for (mime, _) in &offers {
            source.offer(mime.clone());
        }
        if !offers.iter().any(|(m, _)| m == clipboard::OWN_TYPE) {
            source.offer(clipboard::OWN_TYPE.into());
        }
        device.set_selection(Some(&source));
        watch.last_own = Some((offers.clone(), now()));
        watch.sources.push(Source { id, offers });
        Some(id)
    }

    /// A program reads what the shell put on the clipboard.
    fn clip_send(&mut self, id: u64, mime: &str, fd: std::os::fd::OwnedFd) {
        let Some(watch) = self.clip.as_ref() else {
            return;
        };
        let Some(source) = watch.sources.iter().find(|s| s.id == id) else {
            return;
        };
        let bytes = source.offers.iter().find(|(m, _)| m == mime).map(|(_, b)| b.clone()).unwrap_or_default();
        write_transfer(fd, bytes, Some((watch.sender.clone(), id)));
    }

    fn clip_cancelled(&mut self, id: u64) {
        if let Some(watch) = self.clip.as_mut() {
            watch.sources.retain(|s| s.id != id);
        }
    }

    /// Enter or a click on an item: it goes on the clipboard and into the
    /// window that had the keyboard.
    pub fn clip_paste(&mut self, id: u64) {
        let Some(clip) = self.st.clips.get(id).cloned() else {
            return;
        };
        // The flyout goes at once: the window gets the keyboard back.
        self.drop_overlays();
        if self.clip_offer(clip.content.offers()).is_none() {
            return;
        }
        self.st.clips.add(clip.content, now());
        // The compositor has the clipboard and the focus right before it
        // reads the request on the other connection.
        let _ = self.conn.roundtrip();
        self.send_control("paste");
    }

    /// Inserts text into the focused window (the emoji panel), and gives
    /// the clipboard back what it held.
    pub fn insert_text(&mut self, text: &str) {
        let Some(watch) = self.clip.as_mut() else {
            warn!("no clipboard control: the emoji panel cannot insert");
            return;
        };
        if watch.insert.is_some() {
            return;
        }
        let id = watch.take_id();
        let current = watch.current.as_ref().map(|c| (c.generation, c.own.clone(), c.types.clone()));
        match current {
            None => self.insert_paste(id, text.to_owned(), Vec::new()),
            Some((_, Some(own), _)) => self.insert_paste(id, text.to_owned(), own),
            Some((generation, None, types)) => {
                let wanted: Vec<String> = types.into_iter().take(SNAPSHOT_TYPES).collect();
                let Some(readers) = self.clip_receive(generation, &wanted) else {
                    return self.insert_paste(id, text.to_owned(), Vec::new());
                };
                let Some(watch) = self.clip.as_mut() else {
                    return;
                };
                watch.insert = Some(Insert { id, text: text.to_owned(), stage: Stage::Snapshot });
                let sender = watch.sender.clone();
                let spawned = std::thread::Builder::new().name("clipboard".into()).spawn(move || {
                    let deadline = Instant::now() + SNAPSHOT_TIME;
                    let mut total = 0usize;
                    let mut data = Vec::new();
                    for (mime, reader) in readers {
                        if let Some(bytes) = read_transfer(reader, SNAPSHOT_BYTES - total, deadline) {
                            total += bytes.len();
                            data.push((mime, bytes));
                        }
                    }
                    let _ = sender.send(Done::Snapshot { insert: id, data });
                });
                if spawned.is_err() {
                    if let Some(watch) = self.clip.as_mut() {
                        watch.insert = None;
                    }
                }
            }
        }
    }

    /// The emoji goes on the clipboard and is pasted.
    fn insert_paste(&mut self, id: u64, text: String, restore: Offers) {
        let bytes = Arc::new(text.clone().into_bytes());
        let offers: Offers = clipboard::TEXT_TYPES.iter().map(|m| ((*m).to_owned(), bytes.clone())).collect();
        let Some(source) = self.clip_offer(offers) else {
            return;
        };
        let _ = self.conn.roundtrip();
        let paste = self.control_with("paste", serde_json::json!({}));
        if let Some(watch) = self.clip.as_mut() {
            watch.insert = Some(Insert { id, text, stage: Stage::Pasting { source, restore, paste } });
        }
        self.restore_later(id, RESTORE_AFTER);
    }

    /// Gives the clipboard back after `delay`.
    fn restore_later(&mut self, insert: u64, delay: Duration) {
        let inserted = self.loop_handle.insert_source(Timer::from_duration(delay), move |_, _, shell: &mut Shell| {
            shell.restore_clipboard(insert);
            TimeoutAction::Drop
        });
        if inserted.is_err() {
            self.restore_clipboard(insert);
        }
    }

    /// Puts back what was on the clipboard before an emoji was inserted.
    fn restore_clipboard(&mut self, insert: u64) {
        let Some(watch) = self.clip.as_mut() else {
            return;
        };
        let Some(Insert { stage: Stage::Pasting { restore, .. }, .. }) = watch.insert.take_if(|i| i.id == insert && matches!(i.stage, Stage::Pasting { .. })) else {
            return;
        };
        if restore.is_empty() {
            if let Some(device) = watch.device.as_ref() {
                device.set_selection(None);
            }
            watch.last_own = None;
        } else {
            self.clip_offer(restore);
        }
    }

    /// The compositor's answer to a `paste` (true when it was one of the
    /// shell's clipboard requests).
    pub fn clip_reply(&mut self, id: u64, ok: bool) -> bool {
        let insert = self.clip.as_ref().and_then(|w| w.insert.as_ref()).and_then(|i| match i.stage {
            Stage::Pasting { paste: Some(p), .. } if p == id => Some(i.id),
            _ => None,
        });
        match insert {
            Some(insert) => {
                if !ok {
                    // Nothing was pasted (no window had the keyboard): the
                    // clipboard is given back at once.
                    self.restore_clipboard(insert);
                }
                true
            }
            None => false,
        }
    }

    /// The clipboard settings as the compositor sends them.
    pub fn clip_settings(&mut self, enabled: bool, cleared_at: i64) {
        let cleared = self.st.clips.configure(enabled, cleared_at);
        if let Some(watch) = self.clip.as_mut() {
            if cleared && watch.current_copied_at.is_some_and(|at| at < cleared_at) {
                if let Some(device) = watch.device.as_ref() {
                    device.set_selection(None);
                }
            }
        }
        if self.st.flyout == Some(Flyout::Clipboard) {
            self.mark(Role::Flyout);
        }
    }
}

impl Dispatch<ZwlrDataControlManagerV1, ()> for Shell {
    fn event(_: &mut Self, _: &ZwlrDataControlManagerV1, _: <ZwlrDataControlManagerV1 as Proxy>::Event, _: &(), _: &Connection, _: &QueueHandle<Self>) {}
}

impl Dispatch<ZwlrDataControlDeviceV1, ()> for Shell {
    fn event(shell: &mut Self, device: &ZwlrDataControlDeviceV1, event: zwlr_data_control_device_v1::Event, _: &(), _: &Connection, _: &QueueHandle<Self>) {
        match event {
            zwlr_data_control_device_v1::Event::Selection { id } => shell.clipboard_changed(id),
            // The primary selection (middle click) is not kept.
            zwlr_data_control_device_v1::Event::PrimarySelection { id: Some(offer) } => offer.destroy(),
            zwlr_data_control_device_v1::Event::Finished => {
                device.destroy();
                if let Some(watch) = shell.clip.as_mut() {
                    watch.device = None;
                }
            }
            _ => {}
        }
    }

    event_created_child!(Shell, ZwlrDataControlDeviceV1, [
        zwlr_data_control_device_v1::EVT_DATA_OFFER_OPCODE => (ZwlrDataControlOfferV1, Types::default()),
    ]);
}

impl Dispatch<ZwlrDataControlOfferV1, Types> for Shell {
    fn event(_: &mut Self, _: &ZwlrDataControlOfferV1, event: zwlr_data_control_offer_v1::Event, types: &Types, _: &Connection, _: &QueueHandle<Self>) {
        if let zwlr_data_control_offer_v1::Event::Offer { mime_type } = event {
            if let Ok(mut list) = types.0.lock() {
                if list.len() < MAX_TYPES && mime_type.len() <= 255 && !list.contains(&mime_type) {
                    list.push(mime_type);
                }
            }
        }
    }
}

impl Dispatch<ZwlrDataControlSourceV1, u64> for Shell {
    fn event(shell: &mut Self, source: &ZwlrDataControlSourceV1, event: zwlr_data_control_source_v1::Event, id: &u64, _: &Connection, _: &QueueHandle<Self>) {
        match event {
            zwlr_data_control_source_v1::Event::Send { mime_type, fd } => shell.clip_send(*id, &mime_type, fd),
            zwlr_data_control_source_v1::Event::Cancelled => {
                source.destroy();
                shell.clip_cancelled(*id);
            }
            _ => {}
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_transfer_is_read_to_its_end_within_its_limits() {
        let (reader, mut writer) = UnixStream::pair().unwrap();
        let sender = std::thread::spawn(move || {
            writer.write_all(b"hello ").unwrap();
            std::thread::sleep(Duration::from_millis(20));
            writer.write_all(b"world").unwrap();
        });
        assert_eq!(read_transfer(reader, 100, Instant::now() + Duration::from_secs(2)).unwrap(), b"hello world");
        sender.join().unwrap();
        // Larger than allowed: nothing, but read to the end.
        let (reader, mut writer) = UnixStream::pair().unwrap();
        let sender = std::thread::spawn(move || writer.write_all(&[7u8; 300_000]).is_ok());
        assert_eq!(read_transfer(reader, 1000, Instant::now() + Duration::from_secs(2)), None);
        assert!(sender.join().unwrap(), "the writer could write everything");
        // A writer that never finishes: nothing, after the time limit.
        let (reader, _writer) = UnixStream::pair().unwrap();
        let started = Instant::now();
        assert_eq!(read_transfer(reader, 1000, Instant::now() + Duration::from_millis(100)), None);
        assert!(started.elapsed() < Duration::from_secs(2));
    }

    #[test]
    fn what_is_read_becomes_an_item() {
        assert!(matches!(read_item(Kind::Text, b"copied"), Some(ReadItem::Text(t)) if t == "copied"));
        assert!(matches!(read_item(Kind::Files, b"file:///a\r\n"), Some(ReadItem::Files(f)) if f == ["file:///a"]));
        assert!(read_item(Kind::Image, b"not a picture").is_none());
        let png = boswas_ui::icons::encode_png(&Pixmap::new(1200, 300).unwrap()).unwrap();
        match read_item(Kind::Image, &png) {
            Some(ReadItem::Image { width: 1200, height: 300, thumbnail: Some(t), .. }) => {
                assert!(t.width() <= clipboard::THUMBNAIL.0 && t.height() <= clipboard::THUMBNAIL.1);
                assert_eq!((t.width(), t.height()), (640, 160), "the shape is kept");
            }
            _ => panic!("a picture"),
        }
    }

    #[test]
    fn a_reader_gets_everything_and_the_shell_hears_of_it() {
        let (sender, channel) = channel::channel::<Done>();
        let (mut reader, writer) = UnixStream::pair().unwrap();
        write_transfer(writer.into(), Arc::new(b"pasted".to_vec()), Some((sender, 7)));
        let mut text = Vec::new();
        reader.read_to_end(&mut text).unwrap();
        assert_eq!(text, b"pasted");
        let mut event_loop: smithay_client_toolkit::reexports::calloop::EventLoop<Option<u64>> =
            smithay_client_toolkit::reexports::calloop::EventLoop::try_new().unwrap();
        event_loop
            .handle()
            .insert_source(channel, |event, _, written: &mut Option<u64>| {
                if let channel::Event::Msg(Done::Written { source }) = event {
                    *written = Some(source);
                }
            })
            .unwrap();
        let mut written = None;
        let deadline = Instant::now() + Duration::from_secs(5);
        while written.is_none() && Instant::now() < deadline {
            event_loop.dispatch(Some(Duration::from_millis(50)), &mut written).unwrap();
        }
        assert_eq!(written, Some(7));
    }
}
