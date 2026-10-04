#!/usr/bin/env bash
# ISO structure: live system, installer, BIOS + UEFI (Secure Boot) boot paths,
# Boswas boot and installer branding.
set -uo pipefail
. "$(dirname "$0")/../lib.sh"

iso="${ISO:?}"
isodir="${TESTWORK:?}/iso"
[ -d "$isodir/live" ] || { skip "ISO contents (image not extracted)"; finish; }

# Live system
check "live root filesystem (squashfs) present" test -s "$isodir/live/filesystem.squashfs"
check "live kernel present" bash -c "ls '$isodir'/live/vmlinuz* >/dev/null"
check "live initrd present" bash -c "ls '$isodir'/live/initrd.img* >/dev/null"

# Installer (Debian Installer, graphical + text)
for f in install/gtk/vmlinuz install/gtk/initrd.gz install/vmlinuz install/initrd.gz; do
	check "installer file /$f present" test -s "$isodir/$f"
done
initrd_list="$(zcat "$isodir/install/gtk/initrd.gz" 2>/dev/null | cpio -t 2>/dev/null)"
check "Boswas preseed is inside the installer initrd" grep -qx 'preseed.cfg' <<<"$initrd_list"
check "Boswas installer banner is inside the installer initrd" grep -qx 'usr/share/graphics/logo_installer.png' <<<"$initrd_list"
preseed="$(zcat "$isodir/install/gtk/initrd.gz" | cpio -i --quiet --to-stdout preseed.cfg 2>/dev/null)"
check "installer defaults to encrypted LVM (LUKS)" grep -q 'partman-auto/method string crypto' <<<"$preseed"
check "installer disables root login (sudo administrator)" grep -q 'passwd/root-login boolean false' <<<"$preseed"
check "installer default hostname is boswas-device" grep -q 'get_hostname string boswas-device' <<<"$preseed"

# Boot paths
eltorito="$(xorriso -indev "$iso" -report_el_torito plain 2>/dev/null)"
check "El Torito BIOS boot entry" grep -q 'BIOS' <<<"$eltorito"
check "El Torito UEFI boot entry" grep -q 'UEFI' <<<"$eltorito"
check "UEFI loader present (/EFI/boot/bootx64.efi)" test -s "$isodir/EFI/boot/bootx64.efi"
check "Secure Boot chain: shim signed by the Microsoft UEFI CA" grep -aq 'Microsoft Corporation UEFI CA' "$isodir/EFI/boot/bootx64.efi"
check "Secure Boot chain: signed GRUB next to shim" test -s "$isodir/EFI/boot/grubx64.efi"
check "BIOS GRUB modules present" test -d "$isodir/boot/grub/i386-pc"

# Boot menu branding
grubcfg="$isodir/boot/grub/grub.cfg"
contains "boot menu: Boswas live entry" "$grubcfg" "Boswas OS v1 Alpha - Live session"
contains "boot menu: Boswas installer entry" "$isodir/boot/grub/install_start.cfg" "Install Boswas OS"
contains "boot menu: live session sets hostname boswas-device" "$grubcfg" "hostname=boswas-device"
check_not "boot menu: AppArmor not disabled on the kernel command line" grep -q 'apparmor=0' "$grubcfg"
check "boot menu: Boswas splash (800x600 PNG)" bash -c "file -b '$isodir/boot/grub/splash.png' | grep -q '800 x 600'"
contains "boot menu: Boswas theme colours (logo gold accent)" "$isodir/boot/grub/live-theme/theme.txt" "#EBC786"

# Live USB behaviour (release blocker): the default entry is the live session
# with the boot splash; the installer is a separate, explicitly chosen entry.
check "boot menu: the first (default) entry is the live session, not the installer" \
	bash -c "grep -m1 '^menuentry' '$grubcfg' | grep -q 'Live session'"
check "boot menu: the live session shows the Boswas boot splash (splash on the kernel command line)" \
	bash -c "grep -A2 'Live session\" --hotkey=l' '$grubcfg' | grep -q ' splash'"
check_not "boot menu: no automatic installer on the live entry (no auto/priority=critical/preseed)" \
	bash -c "grep -A2 'Live session\" --hotkey=l' '$grubcfg' | grep -qE 'auto=true|priority=critical|preseed/file|install'"
check_not "boot menu: no Debian branding in the menu entries (comments excluded)" \
	bash -c "cat '$grubcfg' '$isodir/boot/grub/install_start.cfg' '$isodir/boot/grub/install.cfg' 2>/dev/null |
		grep -v '^[[:space:]]*#' | grep -E '^[[:space:]]*(menuentry|submenu)' | grep -qi debian"
contains "boot medium: named Boswas OS (.disk/info)" "$isodir/.disk/info" "Boswas OS"
check_not "boot medium: .disk/info does not name Debian" grep -qi debian "$isodir/.disk/info"

finish
