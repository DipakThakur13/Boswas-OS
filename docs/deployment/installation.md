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

## Notes

- Manual partitioning is intentionally not offered on the default path:
  full-disk encryption is policy.
- APT sources point to Debian (deb.debian.org) until the Boswas repository
  exists. Debian security updates install automatically.
