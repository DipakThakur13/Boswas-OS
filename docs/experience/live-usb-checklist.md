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
| 2 | Wait (do nothing) | After the countdown: the Boswas OS boot screen (the mark and a ring of dots that turns; no text); Escape shows the text console and returns | [ ] |
| 3 | Keep waiting | The Boswas OS desktop opens directly, signed in as "Boswas OS Live": background, taskbar along the bottom with Start (the Boswas mark) and the pinned applications in the centre, the status icons and the clock at the right | [ ] |
| 4 | Look at the screen for one minute | No installer, wizard, "Try or Install" dialog or browser page opens | [ ] |
| 5 | Open Start (tap the Super key) | Search field and the pinned applications; typing "install" finds "Install Boswas OS", which is listed but not opened | [ ] |
| 6 | Start Terminal, Files, Calculator (the Boswas applications) | They open with a Boswas window frame (title bar with icon, three buttons, rounded corners all round); the terminal shows "Welcome to Boswas OS … Live session" | [ ] |
| 7 | Move, resize and snap a window (drag the title bar to a side and to the top; Super+Left, Super+Up, Super+Down; rest on the maximise button), switch with Alt+Tab, open Task View (Super+Tab) | The window follows; the snap preview and the snap layouts appear; the window picker and Task View show live previews; animations run without tearing or a stale picture | [ ] |
| 8 | Keyboard, touchpad and mouse: type in the terminal, tap to click, two-finger scroll, three fingers sideways and up, an external mouse; a touch screen or tablet if the device has one | All work; the pointer is drawn and follows smoothly | [ ] |
| 9 | Open Boswas Control Center > About Boswas OS | Boswas OS v1 Alpha, x86_64, "Live session"; the desktop row says "Boswas Desktop" with a version | [ ] |
| 10 | Control Center > Security | The Security Center lists AppArmor, sandbox, firewall, Secure Boot (enabled), screen lock (10 min, enforced), etc. | [ ] |
| 11 | Network: Ethernet connects on its own; for Wi-Fi open quick settings (Super+A), choose a network and type its passphrase, then look at Control Center > Network | The connection is up; the taskbar's network icon, quick settings and the Network page agree. Bluetooth, volume and brightness in quick settings work too, and the volume and brightness keys show their display | [ ] |
| 11a | Control Center: change something that asks for a password (Updates > Check for Updates) | The Boswas password dialog appears over the desktop; the right password is accepted, a wrong one refused | [ ] |
| 11b | Notifications: start a download in the browser or set a one-minute timer in Clock | A toast appears above the taskbar and stays in the notification centre (Super+N) | [ ] |
| 11c | Screen capture: press Print; then, if a capture tool is at hand, let it ask for the screen | The capture screen opens and saves to Pictures/Screenshots. For the tool: "Allow screen capture?" appears, nothing is captured before "Allow", and a red mark on the taskbar shows while it may | [ ] |
| 12 | Personalization: apply two presets (e.g. Pearl, then Horizon) | The wallpaper changes, and the shell, the window frames and the Boswas applications turn light or dark with the preset and return | [ ] |
| 13 | Compatibility Manager | Opens; states "x86_64 (64-bit) and x86 (32-bit) Windows applications" | [ ] |
| 14 | Lock the screen (Super+L), type a wrong password, then `live` | Boswas lock screen (clock, user, password field); the wrong password is refused and the desktop is never visible; `live` unlocks | [ ] |
| 15 | Second display, if available: connect and disconnect an external monitor; arrange it and change its scale in Control Center > Display; switch night light on | The desktop extends to it with its own taskbar; "Keep these display settings?" appears and reverts when not answered; the screens turn warmer; windows come back when the monitor is removed | [ ] |
| 16 | Close the lid or suspend (`systemctl suspend`), then wake | The lock screen is shown on wake, not the desktop | [ ] |
| 17 | Open "Install Boswas OS" from Start | It explains installation and offers "Restart to Install…" and nothing else starts | [ ] |
| 18 | Shut down from Start's power menu (it asks first when windows are open) | Boswas shutdown screen (the ring and "Shutting down"); the computer powers off | [ ] |
| 19 | Remove the stick and boot normally | The existing OS on the internal disk boots unchanged; partitions and EFI boot entries are as noted | [ ] |
| 20 | Boot the stick again, choose "Install Boswas OS" in the boot menu | The installer starts (Boswas OS banner) only now; quit without installing (power off) | [ ] |

After an installation on test hardware (not part of the live check), also
confirm: the disk-unlock prompt of the boot screen accepts the passphrase,
the Boswas login screen appears, lists the account and signs in, and
signing out from Start's user menu returns to the login screen.

Record the hardware model, firmware version, ISO checksum and the result
of each step in the release notes.
