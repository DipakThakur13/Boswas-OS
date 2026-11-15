#!/bin/sh
# Build Debian's disk-unlock helper for the integration test (tests/askpass.rs).
#
#   tests/build-askpass.sh /path/to/cryptsetup-source/debian/askpass.c [OUTDIR]
#
# askpass.c is part of Debian's cryptsetup source package (it becomes
# /lib/cryptsetup/askpass in cryptsetup-initramfs). It is not copied into
# this repository, and it is compiled here exactly as it is: the test runs
# it inside a scratch root directory laid out like the initramfs, so the
# paths it hard-codes (/bin/plymouth, /lib/cryptsetup/passfifo) need no
# changes.
#
# OUTDIR defaults to target/tmp/askpass, where the test looks for the
# binary; a different place is passed to the test in
# BOSWAS_BOOTSPLASH_ASKPASS (the path of the built binary).
set -eu

if [ "$#" -lt 1 ] || [ ! -f "$1" ]; then
	echo "usage: $0 ASKPASS_C [OUTDIR]" >&2
	exit 2
fi
src=$1
here=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
out=${2:-$here/target/tmp/askpass}
mkdir -p "$out"
${CC:-cc} -O2 -o "$out/askpass" "$src"
echo "$out/askpass"
