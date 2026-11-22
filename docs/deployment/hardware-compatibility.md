# Hardware Compatibility List (HCL)

Boswas OS supports **company-approved hardware**. This document defines the
requirements and how a model becomes "supported". Unknown hardware is not
blocked. It is simply unsupported until it has been tested.

## Requirements

| Requirement | Managed device (production) | v1 alpha |
|-------------|-----------------------------|----------|
| CPU architecture | x86_64 / amd64 | Required (only build target) |
| Firmware | UEFI, Secure Boot capable | UEFI recommended; BIOS boots but reports WARN |
| Secure Boot | Enabled, Microsoft UEFI CA trusted (for shim) | Reported, not enforced |
| TPM | TPM 2.0 enabled | Reported, not required |
| Memory | 16 GB recommended, 8 GB minimum | 4 GB boots the live system |
| Storage | SSD/NVMe, 128 GB+ | 32 GB minimum for installation |
| Graphics | Intel or AMD integrated/discrete (open drivers): a kernel mode-setting (KMS) driver with GBM and EGL / OpenGL ES 2 through Mesa, which the Boswas compositor needs | NVIDIA with the open `nouveau` driver only (untested with the compositor). Without a GPU driver the desktop is drawn in software with pixman, as in a VM (ADR-0045) |
| Wi-Fi / Bluetooth | Intel, Realtek, Qualcomm Atheros, MediaTek, Broadcom (brcmfmac) | Firmware included |
| Audio | Intel SOF / HDA, AMD | Firmware included |

## Firmware

Two places, both from Debian's `non-free-firmware` area (redistributable,
the same as on Debian's official media):

**1. The installer medium (`/firmware`)** carries **every** Debian firmware
package with the index the installer reads (`Contents-firmware`, DEP-11
modalias patterns). The installer is a separate system with its own kernel.
It loads missing firmware from there before any network exists, and installs
the packages it used on the target. This fixes the reported prompt for
`rtl_nic/rtl8125b-2.fw` (Realtek RTL8125B 2.5 GbE, package
`firmware-realtek`).

**2. The live and installed system** has these packages installed:

- **CPU:** `intel-microcode`, `amd64-microcode`
- **Graphics:** `firmware-intel-graphics`, `firmware-amd-graphics`
- **Wi-Fi / Bluetooth:** `firmware-iwlwifi`, `firmware-realtek`,
  `firmware-atheros`, `firmware-mediatek`, `firmware-brcm80211`,
  `bluez-firmware`, `firmware-libertas`, `firmware-zd1211`
- **Wired Ethernet:** `firmware-realtek` (RTL8125/8126/8168/8153),
  `firmware-misc-nonfree` (tg3), `firmware-bnx2`, `firmware-bnx2x`,
  `firmware-intel-misc` (e100, ice)
- **Audio:** `firmware-sof-signed`, `firmware-intel-sound`,
  `firmware-cirrus` (CS35L41 laptop speaker amplifiers)
- **Other:** `firmware-intel-misc`, `firmware-misc-nonfree`, and
  `firmware-linux-free`

**Build gate:** `config/boswas/firmware-required.list` names the firmware
files that must be in **both** places: common Ethernet, Wi-Fi, Bluetooth, GPU
and audio. The build fails if one is missing. `tests/installer/` runs the
installer's own firmware-loading code against the built medium. One
inherited limitation: the installer cannot load Intel SOF audio firmware
(`firmware-sof-signed`'s preinst needs a dpkg tool the installer lacks).
Audio is not needed to install, and the installed system has it.

**After installation:** Control Center → Hardware & Drivers → *Check
firmware* lists firmware the running kernel could not load and the Debian
package that provides it (`/usr/share/boswas/firmware/index.tsv`). It
installs that package through the signed Debian repository with
administrator authentication. Firmware updates arrive as normal package
updates.

In Debian 13 `firmware-misc-nonfree` pulls in `firmware-nvidia-graphics`
(GPU System Processor firmware). That firmware is present, but **proprietary
NVIDIA drivers are not part of Boswas OS v1**. Prefer Intel/AMD graphics for
managed devices.

## Graphics and the native desktop

Since the native desktop (ADR-0026) the display is driven by Boswas
software, so graphics support is no longer inherited from KWin and
Plymouth:

- **Desktop (`boswas-compositor`):** opens the display through DRM/KMS and
  renders with OpenGL ES 2 through GBM and EGL (Mesa). Outputs of the
  primary GPU are used; displays connected to a second GPU are not driven
  yet. Each display has its own mode, scale, position and rotation
  (Control Center > System > Display). A monitor runs at the fastest
  refresh rate it offers at its native size (120, 144, 165 Hz...) unless
  another mode is chosen; variable refresh rate (adaptive sync) can be
  turned on per monitor where the monitor and driver support it
  (ADR-0048). No HDR. The proprietary NVIDIA driver is not supported.
  Night light needs a driver with gamma tables. X11 applications run
  through Xwayland and are drawn at scale 1.
- **Software rendering:** where Mesa has no hardware driver (virtual
  machines without 3D acceleration, the firmware frame buffer) the
  compositor draws with pixman instead of Mesa's software OpenGL
  (llvmpipe), which was too slow to move a window smoothly (ADR-0045).
  Windows then have square corners, and the colour filters of
  Accessibility are not available (the magnifier is; ADR-0049).
  `BOSWAS_RENDERER=gles` or `pixman` in
  the session's environment overrides the choice. Under `nomodeset` there
  is no KMS device at all: the compositor cannot start and the text login
  is shown. In a virtual machine, give it 3D acceleration where the
  hypervisor offers it (virtio-gpu with virgl, VMware/VirtualBox 3D) and
  the compositor uses OpenGL ES on the host's GPU.
- **Boot screen (`boswas-bootsplash`):** needs only a KMS device with dumb
  buffers, or a frame buffer (`/dev/fb0`). It follows the switch from the
  firmware frame buffer to the real driver. Without either, boot messages
  and the passphrase prompt appear on the text console. A 24-bit legacy
  frame buffer is not drawn on (the DRM device is used where there is
  one). It draws on the processor, from vector outlines at the display's
  size, one frame per refresh up to 240 a second (ADR-0050); on the build
  machine the first frame took 20 ms at 1920x1080 and 82 ms at 3840x2160,
  and the 1.6 second intro 0.45 s of processor time at 3840x2160 and 240
  frames a second. A slow processor with a large, fast display has not
  been tried. On a 16-bit (RGB565) frame buffer the background is not
  dithered and may show bands.
- **When the desktop cannot start** on a machine (no usable graphics
  device, a driver that fails), the boot ends at a text login, not at an
  empty screen (ADR-0044).
- **Input:** keyboards, mice, touchpads (with gestures), touch screens and
  drawing tablets through libinput. Only keyboard and pointer input has
  been exercised, and only as injected events in tests.
- **Other devices the desktop now talks to:** Wi-Fi and Bluetooth adapters
  (through NetworkManager and BlueZ), sound devices (PipeWire), the
  battery and the backlight, cameras (the Camera application, through Qt
  Multimedia) and removable drives (Files, through UDisks2). None of
  these paths has run on hardware. Printers are not supported in this
  image: no print service (CUPS) is in the manifest, so the applications'
  Print dialogs can print to a PDF file only.
- **If the graphical login cannot start** (no usable graphics device),
  the login falls back to a text login on the first virtual terminal.

None of this has run on physical hardware or in the boot test yet; the
matrix below says what has.

## Certification process for a model

A model is added to the list below only after the reference checks pass on
real hardware:

1. **Boot:** the ISO boots in UEFI mode with Secure Boot **enabled**.
2. **Install:** guided encrypted install completes, and the LUKS2 passphrase
   prompt of the Boswas boot screen works on the built-in keyboard.
3. **Status:** `boswas status` reports PASS for Secure Boot, TPM, disk
   encryption, firewall, AppArmor and audit.
4. **Devices:** graphics (internal and external display), Wi-Fi, Bluetooth,
   audio (speakers, microphone), webcam, touchpad and suspend/resume all
   work, from the desktop: Wi-Fi and Bluetooth from quick settings, sound
   and brightness from their keys, the webcam in Camera, an external
   display arranged in Control Center, night light. The desktop renders
   with the hardware driver, not in software (the compositor's log says
   "drawing with OpenGL ES"), window corners are round, and the session is
   locked when the device resumes. Moving a window and opening Start are
   smooth: with `BOSWAS_LOG=info,boswas_compositor::stats=debug` the log
   shows about one frame per refresh and a few milliseconds each.
5. **Firmware:** `fwupdmgr get-devices` lists the system firmware where the
   vendor publishes to LVFS.
6. **Battery:** life is within 20% of the vendor's Linux or Windows figure.

## Supported models

| Vendor | Model | CPU / GPU | Status | Tested build | Notes |
|--------|-------|-----------|--------|--------------|-------|
| (QEMU, TCG) | q35 + OVMF (Secure Boot, MS keys), virtio disk and network | virtual | Reference VM | see `build/logs/boot-test/` and `docs/reports/` | Used by `./test.sh`; installation tested with and without internet access. **A VM proves no physical hardware compatibility** (no real NIC, Wi-Fi, GPU or firmware request) |
| none | none | none | No physical model certified yet | none | Add rows as models pass the checklist |

## Validation matrix

What has been validated where. "VM" means QEMU under software emulation
(TCG) on the build host; "Physical" means a real PC.

| Area | VM | Physical |
|------|----|----------|
| UEFI boot, Secure Boot enforced (shim → signed GRUB → signed kernel) | Tested | Not tested |
| Legacy BIOS boot | Boots (reported WARN) | Not tested |
| Live USB to desktop, no installer started | Tested with the Plasma desktop (1.0~alpha3). **Not re-tested with the native desktop** | Not tested |
| Encrypted installation, reboot into the installed system | Tested with internet (1.0~alpha3) and without internet (2026-11-12 build), with the Plasma desktop and the text passphrase prompt. Not re-tested with the native desktop and boot screen | Not tested |
| Installer firmware loading (rtl8125b and the required list) | The installer's own code against the medium (no hardware); Intel SOF audio cannot be loaded by the installer | Not tested |
| SATA / NVMe storage | virtio only | Not tested |
| Wired Ethernet / Wi-Fi / Bluetooth drivers | virtio-net only | Not tested |
| Intel / AMD / NVIDIA graphics | QEMU standard VGA only (Plasma desktop, 1.0~alpha3) | Not tested |
| Native desktop on a display (`boswas-compositor`, DRM/KMS, libinput, libseat) | Booted from the built packages on another distribution's base system (Ubuntu 24.04), not from the image: QEMU standard VGA and the firmware frame buffer, software rendering, keyboard only (ADR-0044). **Not tested on Debian 13 or from the ISO** | Not tested |
| Boot screen and LUKS2 passphrase prompt (`boswas-bootsplash`) | The boot screen: booted as above, from the initramfs to the hand-over, with a driver that loads late or replaces the firmware frame buffer, with the previous picture; the picture drawn from vectors (ADR-0050) has not been booted. The passphrase prompt: not on a boot (Debian's unlock helper in a scratch root only) | Not tested |
| Login screen (greetd, Boswas greeter), lock on suspend | Login screen, sign-in and the text-login fallbacks: booted as above. Lock on suspend: not tested | Not tested |
| Quick settings and Settings against the real services (NetworkManager, BlueZ, PipeWire, UPower, polkit) | Not tested (stand-ins only) | Not tested |
| System applications with devices (camera, removable drives, sound) | Not tested (stand-ins only) | Not tested |
| Low disk space / low memory | Not tested | Not tested |
