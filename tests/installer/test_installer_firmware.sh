#!/usr/bin/env bash
# Firmware for the installer, tested with the installer's own code.
#
# The Debian Installer is a separate system on the medium (its own kernel and
# initrd). When a driver asks for firmware it does not have, hw-detect's
# check-missing-firmware looks the file up in /cdrom/firmware/Contents-firmware,
# unpacks the package with udpkg and reloads the driver; this happens before
# the network is configured, so network firmware must come from the medium.
#
# This test runs exactly that code, taken from the installer initrd of the ISO
# under test (not a copy in this repository), inside that initrd as a chroot,
# with the ISO's contents mounted at /cdrom as the installer mounts them:
#
#   * the kernel message parser (the sed expression of check-missing-firmware)
#     turns a Debian kernel's "firmware: failed to load" line into the request;
#   * check_for_firmware + install_firmware_pkg load rtl_nic/rtl8125b-2.fw (the
#     reported prompt) and one file of every pattern in
#     config/boswas/firmware-required.list into the installer's /lib/firmware,
#     and queue the packages for the installed system (/var/cache/firmware);
#   * a file that is not on the medium stays missing (the installer then asks
#     for removable media: an honest prompt, not a silent pass).
#
# Not covered here (needs the hardware): the driver reload itself. Run against
# the shipped 1.0~alpha3 image, the test reproduces the reported failure.
set -uo pipefail
. "$(dirname "$0")/../lib.sh"

iso="${ISO:?}"
isodir="${TESTWORK:?}/iso"
[ -d "$isodir/install" ] || { skip "installer firmware (image not extracted)"; finish; }
if [ "$(id -u)" -ne 0 ]; then
	skip "installer firmware (needs root for the chroot and the bind mount)"
	finish
fi

work="$TESTWORK/installer-firmware"
initrd="$work/initrd"
cleanup() {
	umount "$initrd/cdrom" 2>/dev/null || umount -l "$initrd/cdrom" 2>/dev/null || true
	rm -rf --one-file-system "$work"
}
trap cleanup EXIT
cleanup
mkdir -p "$initrd"

if ! (cd "$initrd" && zcat "$isodir/install/gtk/initrd.gz" | cpio -id --quiet 2>/dev/null); then
	fail "installer initrd (/install/gtk/initrd.gz) unpacks"
	finish
fi
cmf="$initrd/usr/bin/check-missing-firmware"
check "the installer contains hw-detect's check-missing-firmware" test -s "$cmf"
hwdetect="$(sed -n '/^Package: hw-detect$/,/^$/s/^Version: //p' "$initrd/var/lib/dpkg/status")"
pass "installer hw-detect version: ${hwdetect:-unknown} (informational)"

if [ ! -d "$isodir/firmware" ]; then
	fail "the medium has no /firmware pool: the installer cannot load rtl_nic/rtl8125b-2.fw (or any firmware) before the network exists (reported installer prompt reproduced)"
	finish
fi

# The medium, read-only at /cdrom, as cdrom-detect mounts it.
mkdir -p "$initrd/cdrom"
if ! mount --bind "$isodir" "$initrd/cdrom" || ! mount -o remount,bind,ro "$initrd/cdrom"; then
	fail "bind-mounting the medium at the installer's /cdrom"
	finish
fi

# 1. The kernel message parser: the installer's own sed expression.
sed_prog="$(python3 -c '
import re, sys
m = re.search(r"sed -rn \x27([^\x27]*firmware: failed to load[^\x27]*)\x27", open(sys.argv[1]).read())
print(m.group(1) if m else "")' "$cmf")"
check "the installer's firmware-request parser was found in check-missing-firmware" test -n "$sed_prog"
line='[    3.141592] r8169 0000:03:00.0: firmware: failed to load rtl_nic/rtl8125b-2.fw (-2)'
parsed="$(printf '%s\n' "$line" | chroot "$initrd" /bin/sed -rn "$sed_prog" 2>&1)"
check "the installer parses a Debian kernel's firmware request (r8169 0000:03:00.0 rtl_nic/rtl8125b-2.fw)" \
	test "$parsed" = "r8169 0000:03:00.0 rtl_nic/rtl8125b-2.fw"

# 2. The lookup and unpack functions, extracted verbatim from the installer.
awk '/^list_deb_firmware \(\) \{/ { on = 1 } /^# For those who don.t want to load any firmware/ { on = 0 } on' \
	"$cmf" > "$initrd/tmp/cmf-functions.sh"
if ! grep -q '^check_for_firmware()' "$initrd/tmp/cmf-functions.sh" || ! grep -q '^install_firmware_pkg ()' "$initrd/tmp/cmf-functions.sh"; then
	fail "check_for_firmware / install_firmware_pkg found in the installer's check-missing-firmware"
	finish
fi
cat > "$initrd/tmp/fw-harness.sh" <<'EOF'
#!/bin/sh
# Runs inside the installer initrd. $@: firmware files requested by drivers.
log() { echo "LOG $*"; }
log_output() { "$@"; }
. /tmp/cmf-functions.sh
files="$*"
check_for_firmware /cdrom/firmware
for f in $files; do
	if [ -e "/lib/firmware/$f" ]; then echo "LOADED $f"; else echo "MISSING $f"; fi
done
sed 's/^/COMPONENT /' /var/cache/firmware/components 2>/dev/null
for d in /var/cache/firmware/*.deb; do [ -f "$d" ] && echo "QUEUED ${d##*/}"; done
exit 0
EOF

# One concrete file per required pattern (resolved against the medium's index),
# plus a file that exists nowhere.
# "image-only" entries (see the list) are requested too, but reported separately.
resolve() {
	python3 - "$BOSWAS_REPO_ROOT/config/boswas/firmware-required.list" "$isodir/firmware/Contents-firmware" "$1" <<'PYEOF'
import fnmatch, re, sys
want_image_only = sys.argv[3] == "image-only"
entries = []
for line in open(sys.argv[1]):
    pattern, _, comment = line.partition("#")
    if pattern.strip():
        entries.append((pattern.strip(), "image-only" in comment))
files = sorted(re.sub(r"^/?(usr/)?lib/firmware/", "", l.split()[0]) for l in open(sys.argv[2]) if l.strip())
out = [] if want_image_only else ["rtl_nic/rtl8125b-2.fw"]
for p, image_only in entries:
    if image_only != want_image_only:
        continue
    match = next((f for f in files if fnmatch.fnmatchcase(f, p)), None)
    out.append(match or f"UNRESOLVED:{p}")
print(" ".join(dict.fromkeys(out)))
PYEOF
}
requests="$(resolve installer)"
image_only="$(resolve image-only)"
missing_probe="boswas-test/not-on-any-medium.bin"
result="$(chroot "$initrd" /bin/sh /tmp/fw-harness.sh $requests $image_only "$missing_probe" 2>&1)"
printf '%s\n' "$result" > "$BOSWAS_REPO_ROOT/build/logs/installer-firmware.txt" 2>/dev/null || true

check "the installer loads rtl_nic/rtl8125b-2.fw from the medium (no prompt, no network)" \
	grep -qx 'LOADED rtl_nic/rtl8125b-2.fw' <<<"$result"
check "the installer queues firmware-realtek for the installed system (non-free-firmware component)" \
	bash -c "grep -qE '^QUEUED firmware-realtek_[^ ]+\.deb$' <<<\"\$1\" && grep -qx 'COMPONENT non-free-firmware' <<<\"\$1\"" _ "$result"
unresolved="$(tr ' ' '\n' <<<"$requests $image_only" | grep '^UNRESOLVED:' | tr '\n' ' ')"
not_loaded=""
for f in $requests; do
	grep -qx "MISSING $f" <<<"$result" && not_loaded="$not_loaded $f"
done
total="$(wc -w <<<"$requests")"
if [ -z "$unresolved" ] && [ -z "$not_loaded" ]; then
	pass "the installer loads every required installer firmware file from the medium ($total files; build/logs/installer-firmware.txt)"
else
	fail "required firmware the installer cannot load from the medium:${unresolved:+ $unresolved}${not_loaded}"
fi
# Image-only firmware: what the installer actually did with it, stated as a limitation.
for f in $image_only; do
	if grep -qx "LOADED $f" <<<"$result"; then
		pass "image-only firmware $f is loadable by the installer too (the list can drop its image-only mark)"
	else
		skip "installer cannot load image-only firmware $f (package preinst needs dpkg-maintscript-helper, absent from Debian Installer); the installed system has it"
	fi
done
check "firmware that is on no medium stays missing (the installer asks for it instead of pretending)" \
	grep -qx "MISSING $missing_probe" <<<"$result"

finish
