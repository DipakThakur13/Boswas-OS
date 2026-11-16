//! Graphics devices for the sandboxed worker.
//!
//! The worker's sandbox allows file access below the graphics device
//! directory as it was when the sandbox was entered. Graphics devices
//! change during boot, though: when the real driver replaces the firmware
//! frame buffer the kernel removes the last node of `/dev/dri`, and with it
//! the directory, and creates both again for the new driver. A Landlock rule
//! belongs to the directory it was made for, not to its name, so the new
//! directory is outside the sandbox and the worker cannot open the new
//! device itself.
//!
//! Widening the sandbox to all of `/dev` would hand a compromised worker the
//! disks. Instead the supervisor, which is not sandboxed and reads nothing
//! but this channel, opens the devices and passes the open descriptors to
//! the worker over a socket pair:
//!
//! * the supervisor holds a descriptor of `/dev` from the moment it starts.
//!   `/dev` is one file system for the whole boot (it is moved, not
//!   replaced, when the system leaves the initramfs), so names are looked up
//!   relative to that descriptor and stay valid wherever the supervisor's
//!   own root directory points;
//! * the only request is "the graphics devices, please", and the only thing
//!   ever passed back is a character device with the DRM or frame buffer
//!   device number: `dri/cardN` and `fb0`. Nothing the worker sends names a
//!   path.
//!
//! The worker's sandbox is unchanged by this: no rule is added, and a
//! descriptor received this way gives access to that one device only.

use std::ffi::OsStr;
use std::fs::File;
use std::io::{self, IoSlice, IoSliceMut};
use std::mem::MaybeUninit;
use std::os::fd::{AsFd, BorrowedFd, OwnedFd};
use std::os::unix::ffi::OsStrExt;
use std::path::Path;
use std::time::{Duration, Instant};

use rustix::event::{poll, PollFd, PollFlags, Timespec};
use rustix::fs::{Dir, FileType, Mode, OFlags};
use rustix::net::{
    AddressFamily, RecvAncillaryBuffer, RecvAncillaryMessage, RecvFlags, SendAncillaryBuffer, SendAncillaryMessage,
    SendFlags, SocketFlags, SocketType,
};

use crate::output::drm::card_number;
use crate::output::Device;

/// Where device nodes live.
pub const DEVICE_DIR: &str = "/dev";
/// Most devices handed over in one answer.
pub const MAX_DEVICES: usize = 8;
/// Device numbers (major) of DRM devices and of frame buffers.
const DRM_MAJOR: u32 = 226;
const FB_MAJOR: u32 = 29;

/// The request, followed by one byte that the answer repeats.
const REQUEST: u8 = b'D';
/// In an answer: one byte per descriptor passed with it.
const CARD: u8 = b'c';
const FRAME_BUFFER: u8 = b'f';

/// A connected pair of sockets that keep message boundaries: one end for
/// the supervisor ([`Server::serve`]), one for the worker ([`Client`]).
pub fn channel() -> io::Result<(OwnedFd, OwnedFd)> {
    Ok(rustix::net::socketpair(AddressFamily::UNIX, SocketType::SEQPACKET, SocketFlags::CLOEXEC, None)?)
}

/// The supervisor's side: opens graphics devices on request.
#[derive(Debug)]
pub struct Server {
    dev: OwnedFd,
    /// Pass on nothing but DRM and frame buffer device nodes.
    devices_only: bool,
}

impl Server {
    /// `dev` is the directory of device nodes, [`DEVICE_DIR`].
    pub fn new(dev: &Path) -> io::Result<Server> {
        let dev = rustix::fs::open(dev, OFlags::RDONLY | OFlags::DIRECTORY | OFlags::CLOEXEC, Mode::empty())?;
        Ok(Server { dev, devices_only: true })
    }

    fn open(&self, name: &str, major: u32) -> Option<OwnedFd> {
        let flags = OFlags::RDWR | OFlags::CLOEXEC | OFlags::NOFOLLOW | OFlags::NOCTTY;
        let fd = rustix::fs::openat(&self.dev, name, flags, Mode::empty()).ok()?;
        if self.devices_only {
            let stat = rustix::fs::fstat(&fd).ok()?;
            let is_device = FileType::from_raw_mode(stat.st_mode) == FileType::CharacterDevice;
            if !is_device || rustix::fs::major(stat.st_rdev) != major {
                return None;
            }
        }
        Some(fd)
    }

    /// Every DRM device, in the order of their numbers, then the frame
    /// buffer: what can be opened right now.
    fn devices(&self) -> Vec<(u8, OwnedFd)> {
        let flags = OFlags::RDONLY | OFlags::DIRECTORY | OFlags::CLOEXEC | OFlags::NOFOLLOW;
        let mut cards: Vec<u32> = rustix::fs::openat(&self.dev, "dri", flags, Mode::empty())
            .and_then(Dir::new)
            .map(|dir| {
                dir.flatten().filter_map(|entry| card_number(OsStr::from_bytes(entry.file_name().to_bytes()))).collect()
            })
            .unwrap_or_default();
        cards.sort_unstable();
        let mut found = Vec::new();
        for number in cards {
            if found.len() == MAX_DEVICES - 1 {
                break;
            }
            if let Some(fd) = self.open(&format!("dri/card{number}"), DRM_MAJOR) {
                found.push((CARD, fd));
            }
        }
        if let Some(fd) = self.open("fb0", FB_MAJOR) {
            found.push((FRAME_BUFFER, fd));
        }
        found
    }

    /// Answer one request waiting on `channel`. Returns false once the
    /// worker has closed its end. A message that is not a request is
    /// ignored; an answer the worker does not take is dropped.
    pub fn serve(&self, channel: BorrowedFd<'_>) -> io::Result<bool> {
        let mut request = [0u8; 8];
        let length = match rustix::net::recv(channel, &mut request[..], RecvFlags::DONTWAIT) {
            Ok((length, _)) => length,
            Err(rustix::io::Errno::AGAIN) | Err(rustix::io::Errno::INTR) => return Ok(true),
            Err(e) => return Err(e.into()),
        };
        if length == 0 {
            return Ok(false);
        }
        if length != 2 || request[0] != REQUEST {
            return Ok(true);
        }
        let devices = self.devices();
        let mut answer = vec![request[1]];
        answer.extend(devices.iter().map(|(kind, _)| *kind));
        let descriptors: Vec<BorrowedFd<'_>> = devices.iter().map(|(_, fd)| fd.as_fd()).collect();
        let mut space = [MaybeUninit::<u8>::uninit(); rustix::cmsg_space!(ScmRights(MAX_DEVICES))];
        let mut control = SendAncillaryBuffer::new(&mut space);
        if !descriptors.is_empty() {
            control.push(SendAncillaryMessage::ScmRights(&descriptors));
        }
        let flags = SendFlags::DONTWAIT | SendFlags::NOSIGNAL;
        match rustix::net::sendmsg(channel, &[IoSlice::new(&answer)], &mut control, flags) {
            Ok(_) | Err(rustix::io::Errno::AGAIN) => Ok(true),
            Err(rustix::io::Errno::PIPE) => Ok(false),
            Err(e) => Err(e.into()),
        }
        // The supervisor's own descriptors of the devices close here.
    }
}

/// The worker's side: asks the supervisor for the graphics devices.
#[derive(Debug)]
pub struct Client {
    channel: OwnedFd,
    sequence: u8,
}

impl Client {
    pub fn new(channel: OwnedFd) -> Client {
        Client { channel, sequence: 0 }
    }

    /// The graphics devices that exist now, opened by the supervisor: DRM
    /// devices first, in the order of their numbers. Waits at most `wait`
    /// for the answer.
    pub fn devices(&mut self, wait: Duration) -> io::Result<Vec<Device>> {
        self.sequence = self.sequence.wrapping_add(1);
        let flags = SendFlags::DONTWAIT | SendFlags::NOSIGNAL;
        rustix::net::send(&self.channel, &[REQUEST, self.sequence], flags)?;
        let deadline = Instant::now() + wait;
        loop {
            let left = deadline.saturating_duration_since(Instant::now());
            let spec = Timespec { tv_sec: left.as_secs() as i64, tv_nsec: left.subsec_nanos() as _ };
            let ready = {
                let mut fds = [PollFd::new(&self.channel, PollFlags::IN)];
                poll(&mut fds, Some(&spec)).unwrap_or(0)
            };
            if ready == 0 {
                return Err(io::Error::new(io::ErrorKind::TimedOut, "no answer from the supervisor"));
            }
            let mut answer = [0u8; 1 + MAX_DEVICES];
            let mut space = [MaybeUninit::<u8>::uninit(); rustix::cmsg_space!(ScmRights(MAX_DEVICES))];
            let mut control = RecvAncillaryBuffer::new(&mut space);
            let flags = RecvFlags::CMSG_CLOEXEC | RecvFlags::DONTWAIT;
            let length = match rustix::net::recvmsg(&self.channel, &mut [IoSliceMut::new(&mut answer)], &mut control, flags)
            {
                Ok(message) => message.bytes.min(answer.len()),
                Err(rustix::io::Errno::AGAIN) | Err(rustix::io::Errno::INTR) => continue,
                Err(e) => return Err(e.into()),
            };
            let mut descriptors: Vec<OwnedFd> = Vec::new();
            for message in control.drain() {
                if let RecvAncillaryMessage::ScmRights(received) = message {
                    descriptors.extend(received);
                }
            }
            if length == 0 {
                return Err(io::Error::new(io::ErrorKind::UnexpectedEof, "the supervisor is gone"));
            }
            if answer[0] != self.sequence {
                // The answer to an earlier request that was given up on: its
                // devices may be gone by now. They are closed here.
                continue;
            }
            let kinds = &answer[1..length];
            if kinds.len() != descriptors.len() {
                return Err(io::Error::new(io::ErrorKind::InvalidData, "malformed answer from the supervisor"));
            }
            return kinds
                .iter()
                .zip(descriptors)
                .map(|(kind, fd)| match *kind {
                    CARD => Ok(Device::Card(File::from(fd))),
                    FRAME_BUFFER => Ok(Device::FrameBuffer(File::from(fd))),
                    _ => Err(io::Error::new(io::ErrorKind::InvalidData, "unknown kind of device")),
                })
                .collect();
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::{Read, Seek, Write};

    /// A directory laid out like `/dev`, with files in place of device nodes.
    fn fake_dev(name: &str) -> std::path::PathBuf {
        let dir = std::env::temp_dir().join(format!("boswas-bootsplash-dev-{}-{name}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(dir.join("dri/by-path")).unwrap();
        for (file, contents) in
            [("dri/card1", "one"), ("dri/card0", "zero"), ("dri/card10", "ten"), ("dri/renderD128", "render"), ("fb0", "fb")]
        {
            std::fs::write(dir.join(file), contents).unwrap();
        }
        dir
    }

    fn contents(mut file: &File) -> String {
        let mut text = String::new();
        file.rewind().unwrap();
        file.read_to_string(&mut text).unwrap();
        text
    }

    /// Run a server for `requests` requests next to the calling test.
    fn serving(server: Server, end: OwnedFd, requests: usize) -> std::thread::JoinHandle<bool> {
        std::thread::spawn(move || {
            let mut open = true;
            for _ in 0..requests {
                let mut fds = [PollFd::new(&end, PollFlags::IN)];
                poll(&mut fds, Some(&Timespec { tv_sec: 5, tv_nsec: 0 })).unwrap();
                open = server.serve(end.as_fd()).unwrap();
            }
            open
        })
    }

    #[test]
    fn open_devices_cross_the_channel_in_order() {
        let dir = fake_dev("order");
        let server = Server { devices_only: false, ..Server::new(&dir).unwrap() };
        let (ours, theirs) = channel().unwrap();
        let mut client = Client::new(theirs);
        let server = serving(server, ours, 1);
        let devices = client.devices(Duration::from_secs(5)).unwrap();
        assert!(server.join().unwrap());
        let seen: Vec<(bool, String)> = devices
            .iter()
            .map(|device| match device {
                Device::Card(file) => (true, contents(file)),
                Device::FrameBuffer(file) => (false, contents(file)),
            })
            .collect();
        let expected = [(true, "zero"), (true, "one"), (true, "ten"), (false, "fb")];
        assert_eq!(seen, expected.map(|(card, text)| (card, text.to_owned())));
        // The descriptors are open for writing, as a display needs them.
        if let Device::Card(file) = &devices[0] {
            let mut file = file;
            file.write_all(b"!").unwrap();
        }
        assert_eq!(std::fs::read_to_string(dir.join("dri/card0")).unwrap(), "zero!");
        std::fs::remove_dir_all(dir).unwrap();
    }

    #[test]
    fn devices_that_appear_after_the_directory_was_made_again_are_found() {
        // What happens when a driver replaces the firmware frame buffer: the
        // device directory goes away and a new one takes its name.
        let dir = fake_dev("again");
        let server = Server { devices_only: false, ..Server::new(&dir).unwrap() };
        let (ours, theirs) = channel().unwrap();
        let mut client = Client::new(theirs);
        let server = serving(server, ours, 3);
        assert_eq!(client.devices(Duration::from_secs(5)).unwrap().len(), 4);
        std::fs::remove_dir_all(dir.join("dri")).unwrap();
        std::fs::remove_file(dir.join("fb0")).unwrap();
        assert!(client.devices(Duration::from_secs(5)).unwrap().is_empty(), "nothing there for a moment");
        std::fs::create_dir(dir.join("dri")).unwrap();
        std::fs::write(dir.join("dri/card1"), "the new driver").unwrap();
        let devices = client.devices(Duration::from_secs(5)).unwrap();
        assert!(matches!(&devices[..], [Device::Card(file)] if contents(file) == "the new driver"));
        assert!(server.join().unwrap());
        std::fs::remove_dir_all(dir).unwrap();
    }

    #[test]
    fn only_graphics_device_nodes_are_handed_over() {
        // Files with the right names are not devices...
        let dir = fake_dev("strict");
        let server = Server::new(&dir).unwrap();
        assert!(server.devices().is_empty());
        std::fs::remove_dir_all(dir).unwrap();
        // ...and a device with another number is not a graphics device.
        let dev = Server::new(Path::new(DEVICE_DIR)).unwrap();
        assert!(dev.open("null", DRM_MAJOR).is_none() && dev.open("null", FB_MAJOR).is_none());
        assert!(dev.open("null", 1).is_some(), "the check is the device number");
    }

    #[test]
    fn late_and_missing_answers() {
        let (ours, theirs) = channel().unwrap();
        let mut client = Client::new(theirs);
        // Nobody answers in time.
        let begun = Instant::now();
        let error = client.devices(Duration::from_millis(100)).unwrap_err();
        assert_eq!(error.kind(), io::ErrorKind::TimedOut);
        assert!(begun.elapsed() < Duration::from_secs(2));
        // The answer to that request arrives late; the next request does not take it for its own.
        let dir = fake_dev("late");
        let server = Server { devices_only: false, ..Server::new(&dir).unwrap() };
        assert!(server.serve(ours.as_fd()).unwrap());
        std::fs::remove_dir_all(dir.join("dri")).unwrap();
        let server = serving(server, ours, 2);
        let devices = client.devices(Duration::from_secs(5)).unwrap();
        assert!(matches!(&devices[..], [Device::FrameBuffer(_)]), "{devices:?}");
        // The worker goes away: the server sees its end closed.
        drop(client);
        assert!(!server.join().unwrap());
        std::fs::remove_dir_all(dir).unwrap();
        // The supervisor is gone: an error, not a hang.
        let (ours, theirs) = channel().unwrap();
        drop(ours);
        assert!(Client::new(theirs).devices(Duration::from_secs(5)).is_err());
    }

    #[test]
    fn requests_that_are_not_understood_are_ignored() {
        let dir = fake_dev("odd");
        let server = Server { devices_only: false, ..Server::new(&dir).unwrap() };
        let (ours, theirs) = channel().unwrap();
        for odd in [&b"X"[..], b"/etc/shadow", b"D"] {
            rustix::net::send(&theirs, odd, SendFlags::empty()).unwrap();
            assert!(server.serve(ours.as_fd()).unwrap());
        }
        // Nothing was sent back for them.
        let mut fds = [PollFd::new(&theirs, PollFlags::IN)];
        assert_eq!(poll(&mut fds, Some(&Timespec { tv_sec: 0, tv_nsec: 50_000_000 })).unwrap(), 0);
        // Nothing waiting is not an error either.
        assert!(server.serve(ours.as_fd()).unwrap());
        std::fs::remove_dir_all(dir).unwrap();
    }
}
