# Live USB validation (release blocker)

The automated boot test (`./test.sh`, `tests/boot/qemu_boot_test.py`
scenario `usb`) boots the ISO as a USB stick in a virtual machine with UEFI
and Secure Boot enforced, next to an internal disk whose checksum must not
change. This checklist is the same test on **real hardware**. It must pass
on at least one UEFI laptop with Secure Boot on before a release.

**If an installer, installation wizard or installation web page opens by
itself at any point, the release is FAILED.**

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
| 2 | Wait (do nothing) | After the countdown: Boswas OS boot splash (mark, gold spinner) | [ ] |
| 3 | Keep waiting | The Boswas OS desktop opens directly, signed in as "Boswas OS Live" | [ ] |
| 4 | Look at the screen for one minute | No installer, wizard, "Try or Install" dialog or browser page opens | [ ] |
| 5 | Open the Boswas Launcher (Meta key) | Search, favourites and applications; "Install Boswas OS" is listed but not opened | [ ] |
| 6 | Start Terminal, Files, Calculator | They open; the terminal shows "Welcome to Boswas OS … Live session" | [ ] |
| 7 | Open Boswas Control Center > About Boswas OS | Boswas OS v1 Alpha, x86_64, "Live session" | [ ] |
| 8 | Control Center > Security | The Security Center lists AppArmor, sandbox, firewall, Secure Boot (enabled), etc. | [ ] |
| 9 | Control Center > Network | Connect to Wi-Fi or Ethernet works | [ ] |
| 10 | Personalization: apply two presets (e.g. Pearl, then Horizon) | The desktop changes and returns | [ ] |
| 11 | Compatibility Manager | Opens; states "x86_64 / 64-bit Windows applications only" | [ ] |
| 12 | Lock the screen (Meta+L), unlock with password `live` | Boswas lock screen | [ ] |
| 13 | Open "Install Boswas OS" from the launcher | It explains installation and offers "Restart to Install…" and nothing else starts | [ ] |
| 14 | Shut down from the launcher | Boswas shutdown splash; the computer powers off | [ ] |
| 15 | Remove the stick and boot normally | The existing OS on the internal disk boots unchanged; partitions and EFI boot entries are as noted | [ ] |
| 16 | Boot the stick again, choose "Install Boswas OS" in the boot menu | The installer starts (Boswas OS banner) only now; quit without installing (power off) | [ ] |

Record the hardware model, firmware version, ISO checksum and the result
of each step in the release notes.
