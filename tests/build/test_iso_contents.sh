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
check "installer needs no network: no network mirror and no security/updates services during installation" \
	bash -c "grep -qx 'd-i apt-setup/use_mirror boolean false' <<<\"\$1\" && grep -qx 'd-i apt-setup/services-select multiselect' <<<\"\$1\"" _ "$preseed"
check "installer's last step writes signed package sources and the installation report (finish-install)" \
	grep -q 'in-target /usr/lib/boswas/installer/finish-install' <<<"$preseed"
check_not "installer never disables firmware lookup or answers destructive questions" \
	grep -qE 'firmware-lookup string never|partman-auto/disk|partman/confirm boolean true|passwd/user-password ' <<<"$preseed"

# Firmware for the installer (loaded before any network exists)
fw="$isodir/firmware"
check "installer firmware pool present on the medium (/firmware)" test -d "$fw"
check "installer firmware index present (/firmware/Contents-firmware, read by hw-detect)" test -s "$fw/Contents-firmware"
# DEP-11 modalias patterns are a second, optional mechanism (queue firmware
# packages for devices that never asked for firmware during installation);
# live-build only writes them when the build chroot has DEP-11 metadata.
# Loading requested firmware (Contents-firmware, above and below) does not
# depend on them.
dep11="$(find "$fw/dep11" -name '*.patterns' 2>/dev/null | wc -l)"
if [ "$dep11" -gt 0 ]; then
	pass "firmware modalias patterns present for $dep11 packages (/firmware/dep11)"
else
	skip "firmware modalias patterns (/firmware/dep11): none generated (no DEP-11 metadata in the build chroot); requested firmware still loads through Contents-firmware"
fi
check "every package named in Contents-firmware is on the medium" \
	bash -c "awk '{ print \$2 }' '$fw/Contents-firmware' | sort -u | while read -r d; do test -f '$fw'/\"\$d\" || exit 1; done"
check "the reported Realtek RTL8125B firmware (rtl_nic/rtl8125b-2.fw) is in the installer's index (firmware-realtek)" \
	grep -qE '^/?(usr/)?lib/firmware/rtl_nic/rtl8125b-2\.fw[[:space:]]+firmware-realtek_[^ ]+\.deb[[:space:]]+non-free-firmware$' \
	"$fw/Contents-firmware"
required="$BOSWAS_REPO_ROOT/config/boswas/firmware-required.list"
if out="$(python3 - "$required" "$fw/Contents-firmware" <<'PYEOF'
import fnmatch, re, sys
patterns = [l.split("#")[0].strip() for l in open(sys.argv[1])]
patterns = [p for p in patterns if p]
files = [re.sub(r"^/?(usr/)?lib/firmware/", "", l.split()[0]) for l in open(sys.argv[2]) if l.strip()]
missing = [p for p in patterns if not any(fnmatch.fnmatchcase(f, p) for f in files)]
print(f"{len(patterns)} patterns, {len(files)} files")
sys.exit(f"missing: {' '.join(missing)}" if missing else 0)
PYEOF
)"; then
	pass "every required firmware file is in the installer's index ($out)"
else
	fail "required firmware missing from the installer's index: $out"
fi
pool_mb="$(du -smL "$fw" 2>/dev/null | cut -f1)"
pool_n="$(find -L "$fw" -maxdepth 1 -name '*.deb' | wc -l)"
pass "installer firmware pool: $pool_n packages, ${pool_mb:-?} MiB (informational)"

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
