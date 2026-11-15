//! The only module with `unsafe` code: thin wrappers around the few system
//! calls that have no safe binding in the crates this program uses.
//!
//! * console ioctls (`KDSETMODE`, `KDGETMODE`, `KDGKBLED`, `VT_GETSTATE`)
//! * frame buffer ioctls and `mmap` for the fbdev fallback
//! * `signalfd`, so termination signals arrive in the event loop
//!
//! Each wrapper checks the return value and exposes a safe signature; the
//! rest of the crate is built with `#![deny(unsafe_code)]`.
#![allow(unsafe_code)]

use std::io;
use std::os::fd::{AsRawFd, BorrowedFd, FromRawFd, OwnedFd};

const KDSETMODE: libc::c_ulong = 0x4B3A;
const KDGETMODE: libc::c_ulong = 0x4B3B;
const KDGKBLED: libc::c_ulong = 0x4B64;
const VT_GETSTATE: libc::c_ulong = 0x5603;
const FBIOGET_VSCREENINFO: libc::c_ulong = 0x4600;
const FBIOGET_FSCREENINFO: libc::c_ulong = 0x4602;

/// `KD_TEXT`: the kernel draws the text console.
pub const KD_TEXT: i32 = 0;
/// `KD_GRAPHICS`: the kernel leaves the screen to a program.
pub const KD_GRAPHICS: i32 = 1;
/// Caps Lock bit of `KDGKBLED`.
pub const LED_CAPS: u8 = 0x04;

fn check(ret: libc::c_int) -> io::Result<()> {
    if ret < 0 {
        Err(io::Error::last_os_error())
    } else {
        Ok(())
    }
}

/// Current console mode (`KD_TEXT` or `KD_GRAPHICS`). Fails on anything that
/// is not a virtual console.
pub fn kd_get_mode(fd: BorrowedFd<'_>) -> io::Result<i32> {
    let mut mode: libc::c_int = 0;
    // SAFETY: KDGETMODE writes one int through the pointer, which is valid
    // for the duration of the call; the descriptor is borrowed and open.
    check(unsafe { libc::ioctl(fd.as_raw_fd(), KDGETMODE, &mut mode as *mut libc::c_int) })?;
    Ok(mode)
}

/// Switch the console between text and graphics mode.
pub fn kd_set_mode(fd: BorrowedFd<'_>, mode: i32) -> io::Result<()> {
    // SAFETY: KDSETMODE takes the mode by value; no memory is passed.
    check(unsafe { libc::ioctl(fd.as_raw_fd(), KDSETMODE, mode as libc::c_ulong) })
}

/// Keyboard lock-key state of the console (see [`LED_CAPS`]).
pub fn kb_leds(fd: BorrowedFd<'_>) -> io::Result<u8> {
    let mut flags: libc::c_char = 0;
    // SAFETY: KDGKBLED writes one char through the pointer, valid for the call.
    check(unsafe { libc::ioctl(fd.as_raw_fd(), KDGKBLED, &mut flags as *mut libc::c_char) })?;
    Ok(flags as u8)
}

#[repr(C)]
#[derive(Default)]
struct VtStat {
    v_active: libc::c_ushort,
    v_signal: libc::c_ushort,
    v_state: libc::c_ushort,
}

/// Number of the virtual console in the foreground.
pub fn vt_active(fd: BorrowedFd<'_>) -> io::Result<u16> {
    let mut stat = VtStat::default();
    // SAFETY: VT_GETSTATE fills a `struct vt_stat` (three unsigned shorts),
    // which `VtStat` matches field for field; the pointer is valid for the call.
    check(unsafe { libc::ioctl(fd.as_raw_fd(), VT_GETSTATE, &mut stat as *mut VtStat) })?;
    Ok(stat.v_active)
}

/// `struct fb_bitfield` from `linux/fb.h`.
#[repr(C)]
#[derive(Clone, Copy, Debug, Default)]
pub struct FbBitfield {
    pub offset: u32,
    pub length: u32,
    pub msb_right: u32,
}

/// `struct fb_var_screeninfo` from `linux/fb.h`.
#[repr(C)]
#[derive(Clone, Copy, Debug, Default)]
pub struct FbVarScreeninfo {
    pub xres: u32,
    pub yres: u32,
    pub xres_virtual: u32,
    pub yres_virtual: u32,
    pub xoffset: u32,
    pub yoffset: u32,
    pub bits_per_pixel: u32,
    pub grayscale: u32,
    pub red: FbBitfield,
    pub green: FbBitfield,
    pub blue: FbBitfield,
    pub transp: FbBitfield,
    pub nonstd: u32,
    pub activate: u32,
    pub height: u32,
    pub width: u32,
    pub accel_flags: u32,
    pub pixclock: u32,
    pub left_margin: u32,
    pub right_margin: u32,
    pub upper_margin: u32,
    pub lower_margin: u32,
    pub hsync_len: u32,
    pub vsync_len: u32,
    pub sync: u32,
    pub vmode: u32,
    pub rotate: u32,
    pub colorspace: u32,
    pub reserved: [u32; 4],
}

/// `struct fb_fix_screeninfo` from `linux/fb.h`.
#[repr(C)]
#[derive(Clone, Copy, Debug, Default)]
pub struct FbFixScreeninfo {
    pub id: [u8; 16],
    pub smem_start: libc::c_ulong,
    pub smem_len: u32,
    pub kind: u32,
    pub type_aux: u32,
    pub visual: u32,
    pub xpanstep: u16,
    pub ypanstep: u16,
    pub ywrapstep: u16,
    pub line_length: u32,
    pub mmio_start: libc::c_ulong,
    pub mmio_len: u32,
    pub accel: u32,
    pub capabilities: u16,
    pub reserved: [u16; 2],
}

/// Variable screen information of a frame buffer device.
pub fn fb_var_screeninfo(fd: BorrowedFd<'_>) -> io::Result<FbVarScreeninfo> {
    let mut info = FbVarScreeninfo::default();
    // SAFETY: the kernel fills a `struct fb_var_screeninfo`; `FbVarScreeninfo`
    // has the same layout (`repr(C)`, 160 bytes) and outlives the call.
    check(unsafe { libc::ioctl(fd.as_raw_fd(), FBIOGET_VSCREENINFO, &mut info as *mut FbVarScreeninfo) })?;
    Ok(info)
}

/// Fixed screen information of a frame buffer device.
pub fn fb_fix_screeninfo(fd: BorrowedFd<'_>) -> io::Result<FbFixScreeninfo> {
    let mut info = FbFixScreeninfo::default();
    // SAFETY: as above, for `struct fb_fix_screeninfo`.
    check(unsafe { libc::ioctl(fd.as_raw_fd(), FBIOGET_FSCREENINFO, &mut info as *mut FbFixScreeninfo) })?;
    Ok(info)
}

/// A shared, writable memory mapping of a device, unmapped on drop.
pub struct Mapping {
    ptr: *mut u8,
    len: usize,
}

// SAFETY: the mapping is plain memory owned by this value; nothing else
// refers to it, so moving it to another thread is sound.
unsafe impl Send for Mapping {}

impl std::fmt::Debug for Mapping {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "Mapping({} bytes)", self.len)
    }
}

impl Mapping {
    /// Map `len` bytes of `fd` from offset 0, shared and writable.
    pub fn new(fd: BorrowedFd<'_>, len: usize) -> io::Result<Mapping> {
        if len == 0 {
            return Err(io::Error::new(io::ErrorKind::InvalidInput, "empty mapping"));
        }
        // SAFETY: a fresh mapping at an address chosen by the kernel; `len`
        // is non-zero and the descriptor is open. The result is checked
        // against MAP_FAILED before use.
        let ptr = unsafe {
            libc::mmap(
                std::ptr::null_mut(),
                len,
                libc::PROT_READ | libc::PROT_WRITE,
                libc::MAP_SHARED,
                fd.as_raw_fd(),
                0,
            )
        };
        if ptr == libc::MAP_FAILED {
            return Err(io::Error::last_os_error());
        }
        Ok(Mapping { ptr: ptr.cast(), len })
    }

    pub fn len(&self) -> usize {
        self.len
    }

    pub fn is_empty(&self) -> bool {
        self.len == 0
    }

    /// The mapped bytes.
    pub fn as_mut_slice(&mut self) -> &mut [u8] {
        // SAFETY: `ptr` points to `len` mapped, writable bytes that stay
        // mapped until `self` is dropped, and `&mut self` makes this the only
        // reference to them.
        unsafe { std::slice::from_raw_parts_mut(self.ptr, self.len) }
    }
}

impl Drop for Mapping {
    fn drop(&mut self) {
        // SAFETY: exactly the region returned by mmap in `new`.
        unsafe {
            libc::munmap(self.ptr.cast(), self.len);
        }
    }
}

/// Block the given signals for this thread (and the threads and processes it
/// starts) and return a descriptor that becomes readable when one arrives.
pub fn signal_fd(signals: &[i32]) -> io::Result<OwnedFd> {
    // SAFETY: `set` is initialised by sigemptyset before use; all pointers
    // are valid for the calls; the returned descriptor is new and owned here.
    unsafe {
        let mut set: libc::sigset_t = std::mem::zeroed();
        check(libc::sigemptyset(&mut set))?;
        for &signal in signals {
            check(libc::sigaddset(&mut set, signal))?;
        }
        let ret = libc::pthread_sigmask(libc::SIG_BLOCK, &set, std::ptr::null_mut());
        if ret != 0 {
            return Err(io::Error::from_raw_os_error(ret));
        }
        let fd = libc::signalfd(-1, &set, libc::SFD_CLOEXEC | libc::SFD_NONBLOCK);
        check(fd)?;
        Ok(OwnedFd::from_raw_fd(fd))
    }
}

/// Take one pending signal from a [`signal_fd`] descriptor, if there is one.
pub fn read_signal(fd: BorrowedFd<'_>) -> Option<i32> {
    // SAFETY: an all-zero signalfd_siginfo is a valid value; the kernel
    // writes at most `size_of` bytes into it.
    unsafe {
        let mut info: libc::signalfd_siginfo = std::mem::zeroed();
        let size = std::mem::size_of::<libc::signalfd_siginfo>();
        let n = libc::read(fd.as_raw_fd(), (&mut info as *mut libc::signalfd_siginfo).cast(), size);
        if n == size as isize {
            Some(info.ssi_signo as i32)
        } else {
            None
        }
    }
}

/// Ignore the given signals in this process.
pub fn ignore_signals(signals: &[i32]) {
    for &signal in signals {
        // SAFETY: SIG_IGN is a valid disposition for any catchable signal.
        unsafe {
            libc::signal(signal, libc::SIG_IGN);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::fd::AsFd;

    #[test]
    fn frame_buffer_structs_match_the_kernel_layout() {
        assert_eq!(std::mem::size_of::<FbVarScreeninfo>(), 160);
        assert_eq!(std::mem::size_of::<FbFixScreeninfo>(), 80);
        assert_eq!(std::mem::size_of::<VtStat>(), 6);
    }

    #[test]
    fn console_ioctls_fail_cleanly_on_a_file() {
        let file = std::fs::File::open("/dev/null").unwrap();
        assert!(kd_get_mode(file.as_fd()).is_err());
        assert!(kd_set_mode(file.as_fd(), KD_TEXT).is_err());
        assert!(kb_leds(file.as_fd()).is_err());
        assert!(vt_active(file.as_fd()).is_err());
        assert!(fb_var_screeninfo(file.as_fd()).is_err());
        assert!(fb_fix_screeninfo(file.as_fd()).is_err());
    }

    #[test]
    fn mapping_reads_and_writes_shared_memory() {
        let path = std::env::temp_dir().join(format!("boswas-bootsplash-map-{}", std::process::id()));
        let file = std::fs::OpenOptions::new().read(true).write(true).create(true).truncate(true).open(&path).unwrap();
        file.set_len(8192).unwrap();
        {
            let mut map = Mapping::new(file.as_fd(), 8192).unwrap();
            assert_eq!(map.len(), 8192);
            map.as_mut_slice()[4096] = 0x5a;
        }
        let bytes = std::fs::read(&path).unwrap();
        std::fs::remove_file(&path).unwrap();
        assert_eq!(bytes[4096], 0x5a);
        assert!(Mapping::new(file.as_fd(), 0).is_err());
    }
}
