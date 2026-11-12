# Release readiness: cross-platform applications, installer firmware, functional Control Center (2026-11-12)

**Verdict: not ready for release to physical devices.** Everything below that
says "passed" ran and passed. Nothing has been tested on physical hardware.
Android was **not** demonstrated end to end (see Gate 5). Use this build for
VM-based acceptance and for the first physical-hardware trials.

Starting point and root-cause evidence:
[2026-11-12-baseline-audit.md](2026-11-12-baseline-audit.md).

Three kinds of evidence, kept apart throughout:

| Kind | Where it ran |
|------|--------------|
| **Automated** | Unit tests, package builds and lintian, static checks, tests on the extracted image, installer code run in a chroot. Builder container (Debian 13, Docker on Windows) |
| **VM** | QEMU with **software emulation (TCG)**, OVMF Secure Boot with Microsoft keys, virtio disk and network |
| **Physical** | **None.** No physical PC was available to this work |

## Release candidate

| | |
|--|--|
| Build ID | @BUILD_ID@ |
| ISO | `build/output/Boswas-OS-v1-alpha-amd64.iso`, @ISO_SIZE@ bytes, SHA-256 `@ISO_SHA@` |
| Packages | 2044 (1.0~alpha3 shipped 1753) |
| Source | Uncommitted working tree on branch `feature/m2-device-management` (manifest says `dirty: true`) |
| Previous images | Last known-good 1.0~alpha3 image (BOS-1.0~alpha3-20261004T201233Z) kept, checksum verified, in `build/known-good/`. The intermediate image of this work is in `build/previous/` |

**ISO size impact: +682 MiB** (2,883,624,960 → 3,598,5xx,xxx bytes, +24.8 %):

- root filesystem +412 MiB (2,249 → 2,661 MiB squashfs): mostly `wine32:i386`
  and its i386 libraries (291 more packages), plus five firmware packages and
  the new Boswas packages;
- installer firmware pool +266 MiB (`/firmware`: 39 Debian firmware packages).
  The images still fit 8 GB USB sticks.

## Root causes found

1. **Installer firmware prompt (`rtl_nic/rtl8125b-2.fw`).**
   `--firmware-binary false`: the ISO had no `/firmware` pool. The installer
   runs its own kernel/initrd, so it could not satisfy the r8169 driver's
   request, although the live system had `firmware-realtek`. The new
   installer test reproduces the failure against the shipped 1.0~alpha3 ISO
   ("the medium has no /firmware pool").
2. **Untested installation paths.** The VM install test always had internet
   and preseeded `hw-detect/load_firmware false`. The installer used a
   network mirror, so an unreachable mirror could stop an installation.
3. **Found by the new offline installation test (fixed, see Gate 2):** the
   first version of the new `finish-install` counted the installer's own
   still-active `deb cdrom:` entry as a configured source. The installed
   system ended with no active package source.
4. **Found by the new installer firmware test (inherited from Debian, not
   fixable in Boswas):** Debian Installer cannot load `firmware-sof-signed`
   (Intel SOF audio). Its preinst needs `dpkg-maintscript-helper`, which the
   installer lacks. Audio is not needed to install, and the installed system
   has the package.
5. **32-bit Windows:** refused by product decision (ADR-0014), and not
   possible with Debian 13's wine64 alone (no `i386-windows` modules).

## Gates

### Gate 1: Regression (Automated + VM)

@GATE1@

### Gate 2: Firmware and installation

| Check | Kind | Result |
|-------|------|--------|
| `/firmware` pool, `Contents-firmware`, every package file present | Automated (ISO) | Passed |
| Every required firmware pattern in the installer's index; build fails otherwise | Automated (build hook + ISO test) | Passed (46 patterns) |
| The installer's own `check-missing-firmware` code (from the ISO's initrd, in a chroot, medium at /cdrom) parses the r8169 request and loads `rtl_nic/rtl8125b-2.fw`, queues `firmware-realtek` for the target | Automated (real installer code, no hardware) | Passed |
| …loads one file of every required pattern | Automated | Passed for 36 files. **Not loadable:** the 2 Intel SOF audio files (inherited limitation above, reported as SKIP) |
| A file on no medium stays missing (honest prompt) | Automated | Passed |
| Required firmware in the image | Automated | Passed |
| Clean installation **with** network, Secure Boot, LUKS2, reboot, login | VM | Passed on 2026-10-05 (1.0~alpha3). **Not re-run** on this build: the offline run below covers the same steps |
| Clean installation **without internet** (DHCP-less LAN, no route out), reboot, signed sources, report, wine32, firmware | VM | @OFFLINE@ |
| Installation on physical hardware; RTL8125B on real hardware | Physical | **Not tested** |

### Gate 3: Linux applications

| Check | Kind | Result |
|-------|------|--------|
| boswas-sysops APT operations: install/remove/upgrade/refresh, signed repositories only, excluded and protected packages refused, LOCKED with holder, NETWORK/DISK_SPACE/NOT_FOUND/UNTRUSTED mapping, cancel during download, refused during dpkg | Automated (unit) and against real APT in a container | Passed (120 unit tests) |
| Control Center: search, install, remove through sysops, progress, cancel, retry, one job at a time, no false success | Automated (unit + offscreen widgets, fakes at the process boundary) | Passed (236 tests) |
| Flatpak (Flathub opt-in, user installs), AppImage (register/remove, unsandboxed warning), local .deb (unverified-origin warning) | Automated | Passed |
| Install, launch, update, uninstall a real Linux app in the VM through pkexec | VM | **Not done**: pkexec needs a polkit authentication agent, which the serial-console VM test has no way to answer |

### Gate 4: Windows applications

| Check | Kind | Result |
|-------|------|--------|
| PE detection: PE32/x86, PE32+/x86_64, arm64, inconsistent headers, DLLs, non-executable images, scripts | Automated (unit + real MinGW binaries) | Passed |
| A real 32-bit program built from source installs and launches, runs as a 32-bit WoW64 process, confined (AppArmor enforce, home unreachable) | VM (real kernel AppArmor) | Passed |
| The same for the 64-bit program (native 64-bit process) | VM | Passed |
| 32-bit install through the session agent and remotely through the Control Plane (real wine32) | Automated (extracted image) | Passed |
| Refusal when wine32 is missing (message + package name) | Automated (unit) | Passed |
| Real-world 32-bit and 64-bit applications (beyond the test program) | VM / Physical | **Not tested** |

### Gate 5: Android applications

@GATE5@

| Check | Kind | Result |
|-------|------|--------|
| Google Play absent by default (no package, no images, no repository, `boswas-android google-play` unavailable) | Automated (build guard rails + image tests) | Passed |
| Waydroid repository key fingerprint pinned and verified, APT pin to four packages, binder module config, autostart off, Waydroid's own launcher and APK handler hidden, GAPPS refused | Automated (unit) | Passed (sysops 120, boswas-android 109 tests) |
| APK inspection (AXML, ABIs, split, v1/v2/v3 signer) | Automated (synthetic APKs + the real F-Droid APK) | Passed |
| Declining Google Play leaves F-Droid and local APKs usable | Automated (Control Center) | Passed |

### Gate 6: Control Center

| Check | Kind | Result |
|-------|------|--------|
| Every page's controls work or are disabled with the reason; real system interfaces only | Automated | Passed (236 tests) |
| pkexec only ever with sysops and one of 10 operations; no arbitrary root command | Automated (safety tests) | Passed |
| Opening Updates, Android, Hardware & Drivers, Software Center starts no privileged helper (Live USB) | VM | Passed |
| Privileged actions require authentication | Automated (polkit policy: `auth_admin_keep` for every state change) | Passed. **Not exercised** with a live polkit agent in the VM |

### Gate 7: Release build

@GATE7@

## Configuration delivered

- **Firmware:** installer pool = every Debian firmware package (39). The image
  adds firmware-cirrus, firmware-bnx2, firmware-bnx2x, firmware-libertas and
  firmware-zd1211. The required list (46 patterns) is the build gate.
- **Installer:** no network mirror or services during installation;
  `finish-install` writes signed `debian.sources` (main, non-free-firmware,
  updates, security), disables the installation-medium entry, removes the
  live pool and writes `/var/log/boswas/install-report.txt`.
- **Windows:** Wine 10.0 (Debian), `wine32:i386` (i386 the only foreign
  architecture), win64 prefixes, 32-bit through `/usr/lib/wine/wine`.
  AppArmor adds one rule.
- **Android:** `boswas-android` in the image. Waydroid (repo.waydro.id,
  suite trixie, key `7CE0331F71E0A238BB1002D70E406D181DCEE19C`) installed on
  request; VANILLA images; no autostart.
- **Google Play:** not offered (no GMS licence; Waydroid is not certified;
  GAPPS images redistribute Google apps without a licence). Alternatives:
  F-Droid (signer `43238d51…9ccab` pinned) and local APKs. **Needs the
  product owner's confirmation** (legal/product decision).
- **Privileged operations:** `boswas-sysops` only (ADR-0024).

## Known limitations and remaining work

- No physical hardware tested. Next step: the certification checklist in
  `docs/deployment/hardware-compatibility.md` on at least one Intel and one
  AMD machine with RTL8125B Ethernet and Intel/MediaTek Wi-Fi.
- Polkit authorization was verified as policy text and unit tests, not with
  a running polkit agent in the VM.
- Android: Waydroid is outside Debian security support; ARM-only apps cannot
  run; NVIDIA proprietary drivers and VMs get software rendering only.
- The installer cannot load Intel SOF audio firmware (Debian limitation).
- DEP-11 modalias patterns are not generated (no AppStream metadata in the
  build chroot). Requested firmware loads anyway.
- Version: the build still identifies as 1.0~alpha3. Bump to 1.0~alpha4
  before publishing (`config/boswas/release.conf` and the package
  changelogs).
- Nothing is committed: the work is an uncommitted tree on
  `feature/m2-device-management`.

## Upgrade and rollback

- **Existing 1.0~alpha3 installations** get the new packages through APT, as
  soon as the Boswas repository exists (M7). `boswas-os` now depends on
  `boswas-sysops` and `boswas-android`. `wine32:i386` needs `dpkg
  --add-architecture i386` on existing systems: an upgrade step that does not
  exist yet. Until then, 32-bit support on upgraded systems stays unavailable
  and is reported as "not installed" (by design: x86 is only offered while
  the 32-bit loader exists).
- **Android** is opt-in. `android-runtime-remove [--purge-data]` removes
  packages, the repository, the pin, module configuration and the hidden
  entries. `boswas-android cleanup` removes a user's data.
- **Rollback of the image:** the last known-good ISO is in
  `build/known-good/`. `build.sh` now keeps the previous image in
  `build/previous/` instead of deleting it.
