# Live USB validation (release blocker)

The automated boot test (`./test.sh`, `tests/boot/qemu_boot_test.py`
scenario `usb`) boots the ISO as a USB stick in a virtual machine with UEFI
and Secure Boot enforced, next to an internal disk whose checksum must not
change. This checklist is the same test on **real hardware**. It must pass
on at least one UEFI laptop with Secure Boot on before a release.

**If an installer, installation wizard or installation web page opens by
itself at any point, the release is FAILED.**

The checklist below is for the native Boswas desktop (`boswas-compositor`,
`boswas-shell`, `boswas-bootsplash`). That desktop has not yet run on real
hardware, so on its first run record the graphics hardware (GPU, driver)
and input devices as well, and for any step that fails attach the boot's
log (`journalctl -b`; the compositor and the shell log to standard error,
and `BOSWAS_LOG=debug` in their environment gives more detail).

## Prepare

1. Write the ISO to a USB stick (4 GB or larger):
   - Linux: `sudo dd if=Boswas-OS-v1-alpha-amd64.iso of=/dev/sdX bs=4M conv=fsync status=progress`
   - Windows/macOS: a raw image writer (e.g. "dd mode" in Rufus, or balenaEtcher).
2. Note the internal disk's state: existing OS, partition layout
   (`lsblk` / Disk Management) and EFI boot entries (`efibootmgr` / firmware setup).
3. In firmware setup: UEFI mode, Secure Boot **on**.

## Check

| # | Step | Expected | Pass |
|---|------|----------|------|
| 1 | Boot from the USB stick (firmware boot menu) | Boswas OS boot menu (mark, "BOSWAS OS", "Live session" highlighted); Secure Boot accepted | [ ] |
| 2 | Wait (do nothing) | After the countdown: the Boswas OS boot screen (mark, BOSWAS OS wordmark, progress arc); Escape shows the text console and returns | [ ] |
| 3 | Keep waiting | The Boswas OS desktop opens directly, signed in as "Boswas OS Live": background, panel along the bottom with the Boswas mark, pinned applications and the clock | [ ] |
| 4 | Look at the screen for one minute | No installer, wizard, "Try or Install" dialog or browser page opens | [ ] |
| 5 | Open the launcher (tap the Super key) | Search field and the application grid; typing "install" finds "Install Boswas OS", which is listed but not opened | [ ] |
| 6 | Start Terminal, Files, Calculator | They open with a Boswas window frame (title bar, three buttons); the terminal shows "Welcome to Boswas OS … Live session" | [ ] |
| 7 | Move, resize and snap a window (drag the title bar to a side and to the top; Super+Left, Super+Up, Super+Down), switch with Alt+Tab | The window follows; the snap preview appears; no tearing or stale picture | [ ] |
| 8 | Keyboard, touchpad and mouse: type in the terminal, tap to click, two-finger scroll, an external mouse | All work; the pointer is drawn and follows smoothly | [ ] |
| 9 | Open Boswas Control Center > About Boswas OS | Boswas OS v1 Alpha, x86_64, "Live session"; the desktop row says "Boswas Desktop" with a version | [ ] |
| 10 | Control Center > Security | The Security Center lists AppArmor, sandbox, firewall, Secure Boot (enabled), screen lock (10 min, enforced), etc. | [ ] |
| 11 | Network: Ethernet connects on its own; for Wi-Fi run `nmtui` in the terminal, then look at Control Center > Network | The connection is up; the panel's network indicator and the Network page show it | [ ] |
| 12 | Personalization: apply two presets (e.g. Pearl, then Horizon) | Wallpaper, the light or dark shell and window frames, and the colours of Files change and return | [ ] |
| 13 | Compatibility Manager | Opens; states "x86_64 (64-bit) and x86 (32-bit) Windows applications" | [ ] |
| 14 | Lock the screen (Super+L), type a wrong password, then `live` | Boswas lock screen (clock, user, password field); the wrong password is refused and the desktop is never visible; `live` unlocks | [ ] |
| 15 | Second display, if available: connect and disconnect an external monitor | The desktop extends to it with its own panel; windows come back when it is removed | [ ] |
| 16 | Close the lid or suspend (`systemctl suspend`), then wake | The lock screen is shown on wake, not the desktop | [ ] |
| 17 | Open "Install Boswas OS" from the launcher | It explains installation and offers "Restart to Install…" and nothing else starts | [ ] |
| 18 | Shut down from the launcher (Shut down, click again to confirm) | Boswas shutdown screen; the computer powers off | [ ] |
| 19 | Remove the stick and boot normally | The existing OS on the internal disk boots unchanged; partitions and EFI boot entries are as noted | [ ] |
| 20 | Boot the stick again, choose "Install Boswas OS" in the boot menu | The installer starts (Boswas OS banner) only now; quit without installing (power off) | [ ] |

After an installation on test hardware (not part of the live check), also
confirm: the disk-unlock prompt of the boot screen accepts the passphrase,
the Boswas login screen appears and signs in, and logging out from the
launcher returns to the login screen.

Record the hardware model, firmware version, ISO checksum and the result
of each step in the release notes.
