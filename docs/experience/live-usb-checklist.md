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
   - Linux: `sudo dd if=Boswas-OS-v3-alpha-amd64.iso of=/dev/sdX bs=4M conv=fsync status=progress`
   - Windows/macOS: a raw image writer (e.g. "dd mode" in Rufus, or balenaEtcher).
2. Note the internal disk's state: existing OS, partition layout
   (`lsblk` / Disk Management) and EFI boot entries (`efibootmgr` / firmware setup).
3. In firmware setup: UEFI mode, Secure Boot **on**.

## Check

| # | Step | Expected | Pass |
|---|------|----------|------|
| 1 | Boot from the USB stick (firmware boot menu) | Boswas OS boot menu (mark, "BOSWAS OS", "Live session" highlighted); Secure Boot accepted | [ ] |
| 2 | Wait (do nothing) | After the countdown: the Boswas OS boot screen (the mark assembles itself on a dark background lit softly in blue, then a progress ring turns under it; no text; the motion is smooth and the mark sharp at the display's resolution); Escape shows the text console and returns | [ ] |
| 3 | Keep waiting | The Boswas OS desktop opens directly, signed in as "Boswas OS Live": background with the Trash icon at the top left, taskbar along the bottom with the widgets button at its left end, Start (the Boswas mark) and the pinned applications in the centre, the status icons and the clock at the right | [ ] |
| 4 | Look at the screen for one minute | No installer, wizard, "Try or Install" dialog or browser page opens | [ ] |
| 5 | Open Start (tap the Super key) | Search field and the pinned applications; typing "install" finds "Install Boswas OS", which is listed but not opened | [ ] |
| 6 | Start Terminal, Files, Calculator (the Boswas applications) | They open with a Boswas window frame (title bar with icon, three buttons, rounded corners all round) and the blurred, tinted background behind their content (Boswas glass), with readable text; the terminal shows "Welcome to Boswas OS … Live session". "New window of Files" in Files' taskbar menu, and the middle button on its taskbar button, open a second Files window | [ ] |
| 7 | Move, resize and snap a window (drag the title bar to a side and to the top; Super+Left, Super+Up, Super+Down; rest on the maximise button), switch with Alt+Tab, open Task View (Super+Tab) | The window follows; the snap preview and the snap layouts appear; the window picker and Task View show live previews; animations run without tearing or a stale picture | [ ] |
| 8 | Keyboard, touchpad and mouse: type in the terminal, tap to click, two-finger scroll, three fingers sideways and up, an external mouse; a touch screen or tablet if the device has one | All work; the pointer is drawn and follows smoothly | [ ] |
| 9 | Open Boswas Control Center > System > About | Boswas OS v3 Alpha, x86_64, "Live session"; the desktop row says "Boswas Desktop" with a version | [ ] |
| 10 | Control Center > Privacy & security > Boswas Security Center | The Security Center lists AppArmor, sandbox, firewall, Secure Boot (enabled), screen lock (10 min, enforced), etc. | [ ] |
| 11 | Network: Ethernet connects on its own; for Wi-Fi open quick settings (Super+A), choose a network and type its passphrase, then look at Control Center > Network & internet > Wi-Fi | The connection is up; the taskbar's network icon, quick settings and the Wi-Fi page agree. Bluetooth, volume and brightness in quick settings work too, and the volume and brightness keys show their display | [ ] |
| 11a | Control Center: change something that asks for a password (Boswas Update > Check for Updates) | The Boswas password dialog appears over the desktop; the right password is accepted, a wrong one refused | [ ] |
| 11b | Notifications: start a download in the browser or set a one-minute timer in Clock | A toast appears above the taskbar and stays in the notification centre (Super+N) | [ ] |
| 11c | Screen capture: press Print; then, if a capture tool is at hand, let it ask for the screen | The capture screen opens and saves to Pictures/Screenshots. For the tool: "Allow screen capture?" appears, nothing is captured before "Allow", and a red mark on the taskbar shows while it may | [ ] |
| 11d | Copy two different texts in Text Editor, then in another window press Super+V, choose the older one and press Enter; press Super+. and insert an emoji; press Super+W | The clipboard history lists both, newest first, and pastes the chosen one; the emoji appears in colour and the clipboard still holds the copied text; the widgets board opens with the system card's live readings and a click on a card opens its application | [ ] |
| 11e | Desktop: right-click the background > New > Folder, type a name and press Enter; drag a file from Files onto the desktop; select the folder and press Delete; open the Trash icon | The folder appears and is renamed; the file is copied to the desktop; the folder goes to the Trash, which Files shows | [ ] |
| 11f | With a Files window open, open Start and then quick settings over it; right-click the Start button; press Ctrl+Alt+Del, then Escape; with an external monitor connected, press Super+P | Start, quick settings and the menu are glass: on a computer with a GPU the Files window shows through, blurred, behind them (on the software renderer of a virtual machine the blurred wallpaper), and the text stays readable. The Start button's menu lists Installed apps, Power options, Terminal, Settings and the others. Ctrl+Alt+Del shows Lock, Sign out, Change a password, Task Manager and Cancel. Super+P offers PC screen only, Duplicate, Extend and Second screen only and switches the monitors (ADR-0062, ADR-0063) | [ ] |
| 12 | Personalization: apply two presets (e.g. Pearl, then Horizon) | The wallpaper changes, and the shell, the window frames and the Boswas applications turn light or dark with the preset and return | [ ] |
| 13 | Compatibility Manager | Opens; states "x86_64 (64-bit) and x86 (32-bit) Windows applications" | [ ] |
| 14 | Lock the screen (Super+L), press a volume key, type a wrong password, then `live` | Boswas lock screen (clock, user, password field); the volume level shows on the lock screen; the wrong password is refused and the desktop is never visible; `live` unlocks | [ ] |
| 14a | With a network, add a place in Weather, wait for its forecast, then lock the screen; unlock, turn off Settings > Privacy & security > Lock screen > "Show weather on the lock screen" and lock again | The first lock shows a weather card in the lower left corner (the place, the temperature, the next hours), not in the password view and not on a second monitor; the password is accepted with the card shown; the second lock shows no card | [ ] |
| 15 | Second display, if available: connect and disconnect an external monitor; arrange it and change its scale in Control Center > System > Display; switch night light on | The desktop extends to it with its own taskbar; "Keep these display settings?" appears and reverts when not answered; the screens turn warmer; windows come back when the monitor is removed | [ ] |
| 16 | Close the lid or suspend (`systemctl suspend`), then wake | The lock screen is shown on wake, not the desktop | [ ] |
| 17 | Open "Install Boswas OS" from Start | It explains installation and offers "Restart to Install…" and nothing else starts | [ ] |
| 18 | Shut down from Start's power menu (it asks first when windows are open) | Boswas shutdown screen (the mark, the ring and "Shutting down", fading in); the computer powers off | [ ] |
| 19 | Remove the stick and boot normally | The existing OS on the internal disk boots unchanged; partitions and EFI boot entries are as noted | [ ] |
| 20 | Boot the stick again, choose "Install Boswas OS" in the boot menu | The installer starts (Boswas OS banner) only now; quit without installing (power off) | [ ] |

After an installation on test hardware (not part of the live check), also
confirm: the disk-unlock prompt of the boot screen accepts the passphrase,
the Boswas login screen appears, lists the account and signs in, and
signing out from Start's user menu returns to the login screen.

Record the hardware model, firmware version, ISO checksum and the result
of each step in the release notes.
