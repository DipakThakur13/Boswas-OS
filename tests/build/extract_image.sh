#!/usr/bin/env bash
# Extract the ISO and its root filesystem once for the image-content suites.
#   $TESTWORK/iso     ISO 9660 contents
#   $TESTWORK/rootfs  unpacked /live/filesystem.squashfs
set -uo pipefail
. "$(dirname "$0")/../lib.sh"
. "$BOSWAS_REPO_ROOT/build/scripts/lib.sh"

iso="${ISO:?ISO not set}"
work="${TESTWORK:?TESTWORK not set}"
[ -s "$iso" ] || { skip "image extraction (no ISO at $iso)"; finish; }

safe_rm_tree "$work/iso"
safe_rm_tree "$work/rootfs"
mkdir -p "$work"

if xorriso -osirrox on -indev "$iso" -extract / "$work/iso" >/dev/null 2>&1; then
	chmod -R u+w "$work/iso"
	pass "ISO filesystem is readable (xorriso)"
else
	fail "ISO filesystem is readable (xorriso)"
	finish
fi

if unsquashfs -no-progress -quiet -d "$work/rootfs" "$work/iso/live/filesystem.squashfs" >/dev/null 2>&1; then
	pass "root filesystem image is readable (unsquashfs, $(du -sh "$work/rootfs" | cut -f1))"
else
	fail "root filesystem image is readable (unsquashfs)"
fi
finish
