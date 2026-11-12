# Installing Boswas OS v1 alpha

> Alpha software for pilot devices only. Installing **erases the selected disk**.

## 1. Write the ISO to a USB stick

Verify the checksum first:

```sh
cd build/output
sha256sum -c Boswas-OS-v1-alpha-amd64.sha256
```

Write it with any raw image writer. On Linux:

```sh
sudo dd if=Boswas-OS-v1-alpha-amd64.iso of=/dev/sdX bs=4M conv=fsync status=progress
```

On Windows, use a raw writer such as Rufus in "DD image" mode.

## 2. Firmware settings

- Boot in **UEFI** mode. Leave **Secure Boot enabled**: the ISO's shim, GRUB
  and kernel are signed.
- Enable the TPM 2.0 if the firmware has a switch for it.

## 3. Boot menu

| Entry | Purpose |
|-------|---------|
| Boswas OS v1 Alpha - Live session | Try the desktop without installing (starts automatically after 10 s) |
| ... (safe graphics) | Live session with `nomodeset` for problematic GPUs |
| Install Boswas OS | Graphical Debian Installer (live mode) |
| Advanced install options | Text and expert installers, rescue mode |
| Utilities | UEFI firmware settings, verify the boot medium |

Live session login: user `boswas`, password `live`. Nothing is saved.

## 4. Installer flow

The installer copies the tested Boswas OS image to disk. Boswas policy is
preseeded. **You** always provide:

1. Language, location, keyboard.
2. Hostname: defaults to `boswas-device`; use the asset tag where required.
3. The **local administrator** account (full name, user name, password). The
   root account is locked. This account administers the device through
   `sudo`. Use a strong password (12+ characters).
4. The **disk**. It is partitioned automatically as an EFI system partition,
   an unencrypted `/boot`, and **LVM inside LUKS2** for root and swap.
5. Confirmation that the disk will be erased, then the **encryption
   passphrase**. It is needed at every boot, so store it according to the
   Boswas recovery procedure.
6. Final confirmation before changes are written.

After the reboot, unlock the disk, then log in at the Boswas login screen.

## 5. Verify

```sh
boswas-info
boswas-status        # expect PASS for disk encryption, firewall, AppArmor, audit
```

## Network and firmware during installation

- **No internet connection is needed.** The medium contains the complete
  system. The installer uses no network mirror and downloads nothing, so a
  missing network driver, missing firmware, a captive portal or an
  unreachable mirror cannot stop the installation.
- **Firmware comes from the medium.** When a device asks for firmware the
  installer does not have (for example `rtl_nic/rtl8125b-2.fw` for Realtek
  2.5 GbE Ethernet), the installer loads it from the medium's `/firmware`
  pool without asking, and the installed system gets the same firmware
  package. The installer asks for removable media **only** for firmware that
  is not in Debian at all; you can answer **No** and continue, and the
  device will not work until its firmware is provided.
- **After installation** the system has signature-verified Debian package
  sources (`/etc/apt/sources.list.d/debian.sources`: main, non-free-firmware,
  updates and security), whether or not the network was available. Debian
  security updates install automatically once the device is online.
- **Installation report:** `/var/log/boswas/install-report.txt` (build,
  firmware the installer loaded, firmware installed, package sources).
  The installer's own logs are in `/var/log/installer/`.

## If the installation fails

The installer stops at the failing step and shows its reason. Nothing
partitions or formats a disk without your confirmation, and a failure
before the partitioning confirmation leaves the disk unchanged.

| What you see | What it means | What to do |
|--------------|---------------|------------|
| "Some of your hardware needs non-free firmware files …" naming a file | That firmware is not in Debian (Boswas OS ships all Debian firmware) | Choose **No** and continue; report the file and the device (see *Collect logs*) |
| "Network autoconfiguration failed" | No cable, no DHCP, or a network adapter without a driver | Choose **Do not configure the network at this time**. Installing does not need it |
| "No disk drive was detected" / no disk in the list | The storage controller is not supported, or set to RAID/Intel RST mode | In the firmware settings, set SATA mode to **AHCI**; NVMe disks need no setting |
| "Failed to partition the selected disk" / "insufficient space" | The disk is smaller than the minimum (32 GB) | Use a larger disk |
| "Unable to install GRUB" / "grub-install failed" | The EFI system partition could not be written (firmware in legacy/CSM mode, or a write-protected EFI variable store) | Boot the medium in **UEFI** mode (disable CSM), retry **Install the GRUB boot loader** from the installer menu |
| "Installation step failed" (any step) | The step's program returned an error | Press Alt+F4 to read the installer log, go back to the main menu and **retry** the step, or collect the logs |

**Collect logs:** in the installer's main menu (Go Back until it appears)
choose **Save debug logs**. Save to a USB stick, or serve them over the
network to another computer. On a successfully installed system, the logs
are in `/var/log/installer/` and the report in
`/var/log/boswas/install-report.txt`.

**Retry:** a failed step can be retried from the installer's main menu.
Starting over (reboot from the medium) is always safe before the
partitioning confirmation. After it, start over and confirm the same disk:
it is partitioned again from scratch.

## Notes

- Manual partitioning is intentionally not offered on the default path:
  full-disk encryption is policy.
- APT sources point to Debian (deb.debian.org) until the Boswas repository
  exists. Debian security updates install automatically.
- Requirements and tested hardware:
  [hardware-compatibility.md](hardware-compatibility.md).
