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

## Firmware in the image

From Debian's `non-free-firmware` area (redistributable, the same as on
Debian's official media):

- **CPU:** `intel-microcode`, `amd64-microcode`
- **Graphics:** `firmware-intel-graphics`, `firmware-amd-graphics`
- **Wi-Fi / Bluetooth:** `firmware-iwlwifi`, `firmware-realtek`,
  `firmware-atheros`, `firmware-mediatek`, `firmware-brcm80211`,
  `bluez-firmware`
- **Audio:** `firmware-sof-signed`, `firmware-intel-sound`
- **Other:** `firmware-intel-misc`, `firmware-misc-nonfree`, and
  `firmware-linux-free`

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
| (QEMU/KVM) | q35 + OVMF (Secure Boot, MS keys) | virtual | Reference VM | see `build/logs/boot-test/` | Used by `./test.sh` |
| none | none | none | No physical model certified yet | none | Add rows as models pass the checklist |
