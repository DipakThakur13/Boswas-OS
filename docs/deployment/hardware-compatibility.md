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
| Graphics | Intel or AMD integrated/discrete (open drivers) | NVIDIA works with the open `nouveau` driver only |
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

## Certification process for a model

A model is added to the list below only after the reference checks pass on
real hardware:

1. **Boot:** the ISO boots in UEFI mode with Secure Boot **enabled**.
2. **Install:** guided encrypted install completes, and the LUKS2 passphrase
   prompt works on the built-in keyboard.
3. **Status:** `boswas status` reports PASS for Secure Boot, TPM, disk
   encryption, firewall, AppArmor and audit.
4. **Devices:** graphics (internal and external display), Wi-Fi, Bluetooth,
   audio (speakers, microphone), webcam, touchpad and suspend/resume all
   work.
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
| Live USB to desktop, no installer started | Tested | Not tested |
| Encrypted installation, reboot into the installed system | Tested (with network; without internet: see the release report) | Not tested |
| Installer firmware loading (rtl8125b and the required list) | The installer's own code against the medium (no hardware) | Not tested |
| SATA / NVMe storage | virtio only | Not tested |
| Wired Ethernet / Wi-Fi / Bluetooth drivers | virtio-net only | Not tested |
| Intel / AMD / NVIDIA graphics | QEMU standard VGA only | Not tested |
| Low disk space / low memory | Not tested | Not tested |
