# Baseline audit: firmware, installer, applications, Control Center (2026-11-12)

Starting point for the cross-platform application platform work (workstreams
A–J). Every statement below was checked against the repository, the last
build (`BOS-1.0~alpha3-20261004T201233Z`, ISO SHA-256 `57c9e5b9…72f5`,
2 883 624 960 bytes, 1753 packages) or the Debian 13 archive indices of
2026-11-12. Nothing here is taken from earlier reports without re-checking.

## Entry points

| Area | Entry point |
|------|-------------|
| Build | `build.sh` → `build/scripts/build-packages.sh` → `build/scripts/prepare-live-config.sh` → `lb config` (`config/live-build/auto/config`) → `lb build` |
| Image content | `config/live-build/config/package-lists/*.list.chroot`, `archives/boswas-exclude.pref.chroot`, guard rails `hooks/live/0100-boswas-verify.hook.chroot` |
| Installer | Debian Installer, live mode (`--debian-installer live`), GTK; policy preseed `installer/configuration/preseed.cfg` (staged into the installer initrd) |
| Windows apps | `packages/boswas-compat` (`boswas-winapp`), runtime `compatibility/wine/runtime.conf`, AppArmor `compatibility/policies/apparmor/boswas-winapp` |
| GUI | `packages/boswas-control-center` (PySide6), `packages/boswas-compat-manager` |
| Tests | `test.sh` (static, packages, unit, image, runtime, QEMU boot: `serial`, `uefi`, `usb`, optional `install`) |

Last full run (2026-10-05): 525 passed, 0 failed, 2 skipped (AppArmor
enforcement inside the Docker kernel; covered by the QEMU test); the
`install` scenario (UEFI Secure Boot, encrypted LVM) passed separately.

## Workstream A: firmware

**Root cause of the `rtl_nic/rtl8125b-2.fw` prompt (reproduced from the
shipped ISO, not guessed):**

1. `config/live-build/auto/config` sets `--firmware-binary false`. The ISO
   has no `/firmware` directory (checked with xorriso), so the installer has
   no firmware packages and no `Contents-firmware` index.
2. The installer boots its own kernel (`/install/gtk/vmlinuz`, 6.12.107) and
   initrd, separate from the live system (6.12.111). Its `hw-detect`
   (1.171) `check-missing-firmware` parses `firmware: failed to load …` from
   dmesg and looks in `/firmware` and `/cdrom/firmware`; with neither, it
   asks for removable media: the reported prompt.
3. The file exists in Debian 13: `usr/lib/firmware/rtl_nic/rtl8125b-2.fw`
   is shipped by `firmware-realtek` (non-free-firmware, 20250410-2;
   `Contents-all.gz`). The live image already installs `firmware-realtek`,
   so the **live session** has it; only the installer does not.
4. The installer's fallback without an index greps debs for `./lib/firmware/`
   while trixie firmware packages ship `./usr/lib/firmware/`: the
   `Contents-firmware` index is required, loose debs alone would not work.

Not causes (checked): the installer's kernel-module udebs on the ISO match
its kernel (6.12.107); signed GRUB and shim (`grub-efi-amd64-signed`,
`shim-signed`) are in the ISO pool, so the bootloader step does not need the
network.

**Why tests did not catch it:** the QEMU install scenario always has user-mode
networking with internet, uses virtio-net (no firmware) and preseeds
`hw-detect/load_firmware false`. No test ran an installation without internet.

**Offline-install risk (to verify):** with no network the installer cannot
reach `deb.debian.org`; the installed system's APT sources may end up without
an active Debian entry, which would block firmware and security updates
later. Nothing in the repository writes canonical sources independently of
the installer's network state.

Firmware size in Debian 13 (all packages with files under `lib/firmware`,
live-build's architecture blocklist applied): 39 packages, about 260 MB.
`firmware-marvell-prestera` (58 MB, network-switch ASICs) and
`firmware-nvidia-tesla-535-gsp` (37 MB, only used by the proprietary
datacenter driver) are irrelevant to a desktop installer.

## Workstream B: installer

The installer is Debian Installer in live mode with a policy preseed (full-disk
encryption, root locked, no destructive answers preseeded). Failure handling,
logs (`/var/log/installer/` on the target, "Save debug logs" in the menu) and
retry are the Debian Installer's own. Not tested: no network, missing firmware,
BIOS install, low disk space. No physical-hardware results exist in the
repository; `docs/deployment/hardware-compatibility.md` has no tested entries.

## Workstream C/G: Linux applications

The Software Center page lists installed applications (desktop entries) and
opens them; it cannot install, update or remove anything. Plasma Discover and
PackageKit are excluded by policy (`boswas-exclude.pref.chroot`). Flatpak is
installed with no remote. `libfuse2t64` (needed by older AppImages) is not
installed; `fuse3` is. No privileged helper exists: Control Center runs as the
user and its only state-changing system action is `systemctl reboot` in a
live session.

## Workstream D: Windows applications

`boswas-winapp` detects the architecture from the PE header (machine field,
optional-header magic), never the file name; MSI platform from the summary
stream. 32-bit programs are refused by product decision (ADR-0014,
`ARCHITECTURES="x86_64"`), and the guard rails fail the build if `wine32` or
any foreign dpkg architecture is present.

Debian 13 Wine (10.0~repack-6): `libwine:amd64` contains only
`x86_64-windows` PE modules (no `i386-windows`), so the 64-bit package cannot
run 32-bit programs (no new-style WoW64). 32-bit support requires `wine32:i386`
(loader `/usr/lib/wine/wine`, 32-bit PE modules in `libwine:i386`,
623 MB installed). live-build supports `package:arch` entries in package lists
and enables the foreign architecture itself.

## Workstream E: Android

Nothing exists. Facts: Debian's signed kernel has `CONFIG_ANDROID_BINDER_IPC=m`
(`binder_linux.ko`, signed with the kernel, loads under Secure Boot lockdown)
and no binderfs; `devices=binder,hwbinder,vndbinder` creates the legacy device
nodes Waydroid supports. Waydroid, `libgbinder` and `python3-gbinder` are
**not** in Debian 13; the maintained source is the Waydroid project's APT
repository (`repo.waydro.id`). `lxc` is in Debian 13.

## Workstream F: Control Center

A real application, not a mockup: pages read public system files and the
device agent, actions open KDE settings modules through an allowlisted runner
(fixed absolute paths, no shell, timeouts; safety tests enforce it). Gaps:
Update Center is read-only; Storage shows usage only; no Hardware & Drivers
page; Network and Bluetooth only open KDE modules (no live interface status);
no Android page; no application installation.

## Plan (ordered by dependency)

1. Firmware on the installer medium (`--firmware-binary true` + documented
   prune + build-time verification of required firmware), canonical APT
   sources for offline installs, installer firmware test against the real
   installer code, offline VM install scenario.
2. 32-bit Windows: `wine32:i386`, runtime and AppArmor updates, guard rails,
   a real 32-bit test program, ADR update.
3. `boswas-sysops`: one narrowly scoped privileged helper (pkexec + one polkit
   action per fixed operation) for APT, cleanup, firmware report, Flatpak
   remotes and the Android runtime.
4. `boswas-android`: runtime manager (Waydroid, opt-in), APK inspection
   (manifest, ABIs, splits, signer), install/launch/remove; Google Play
   feasibility assessed honestly.
5. Control Center: functional Updates, Storage, Hardware & Drivers, Network,
   Android and application hub pages on top of 3 and 4.
6. Image integration, full build, all test stages, boot scenarios, report.
