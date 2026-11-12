#!/usr/bin/env bash
# Build the WinCompat test fixtures from source (no binaries are committed).
#
#   $TESTWORK/fixtures/boswas-testapp.exe           the test application
#   $TESTWORK/fixtures/boswas-testapp-blocked.exe   byte-different variant, blocked by a manifest
#   $TESTWORK/fixtures/boswas-testapp-unlisted.exe  byte-different variant, in no manifest
#   $TESTWORK/fixtures/boswas-testapp-x86.exe       the same program built as a 32-bit (x86, PE32) program
#                                                   (i686 MinGW-w64): installs and runs with wine32:i386
#   $TESTWORK/fixtures/boswas-testapp-badpe.exe     a PE32+ image whose machine field was changed to i386:
#                                                   an inconsistent header, refused before anything runs
#   $TESTWORK/fixtures/manifests/*.json             manifests pinned to the built hashes
#   $TESTWORK/fixtures.iso                          all of the above, for the QEMU boot test
#
# Needs the MinGW-w64 cross-compilers (x86_64 and i686) of the builder image.
set -uo pipefail
. "$(dirname "$0")/../lib.sh"
. "$BOSWAS_REPO_ROOT/build/scripts/lib.sh"

src="$BOSWAS_REPO_ROOT/tests/compatibility/fixtures"
out="${TESTWORK:?TESTWORK not set}/fixtures"
cc=x86_64-w64-mingw32-gcc
cc32=i686-w64-mingw32-gcc

if ! command -v "$cc" >/dev/null 2>&1; then
	skip "WinCompat test fixtures ($cc not installed; rebuild the builder image)"
	finish
fi
rm -rf "$out" "$TESTWORK/fixtures.iso"
mkdir -p "$out/manifests"

build() {
	"$cc" -O2 -s -Wall -Werror -std=c11 -D"BOSWAS_VARIANT=$2" -o "$out/$1" "$src/testapp/testapp.c" -lws2_32
}
if build boswas-testapp.exe 1 && build boswas-testapp-blocked.exe 2 && build boswas-testapp-unlisted.exe 3; then
	pass "Windows test application builds from source (MinGW-w64, x86_64 PE)"
else
	fail "Windows test application build"
	finish
fi
check "test application is a PE32+ x86-64 console program" \
	bash -c "file -b '$out/boswas-testapp.exe' | grep -q '^PE32+ executable.*(console), x86-64'"
# The real 32-bit build of the same program (variant 4: its own hash).
if command -v "$cc32" >/dev/null 2>&1; then
	if "$cc32" -O2 -s -Wall -Werror -std=c11 -DBOSWAS_VARIANT=4 -o "$out/boswas-testapp-x86.exe" \
		"$src/testapp/testapp.c" -lws2_32; then
		pass "32-bit Windows test application builds from source (MinGW-w64 i686, x86 PE)"
		check "32-bit test application is a PE32 Intel 80386 console program" \
			bash -c "file -b '$out/boswas-testapp-x86.exe' | grep -qE '^PE32 executable.*\(console\).*Intel (80386|i386)'"
	else
		fail "32-bit Windows test application build"
	fi
else
	fail "32-bit Windows test application ($cc32 not installed; rebuild the builder image)"
fi
# A damaged copy: only the PE machine field of the 64-bit build changes
# (0x8664 -> 0x014c), leaving a PE32+ optional header. Boswas OS must refuse
# it from the header alone (inconsistent); it is never executed.
if python3 - "$out/boswas-testapp-unlisted.exe" "$out/boswas-testapp-badpe.exe" <<'PYEOF'
import struct, sys
data = bytearray(open(sys.argv[1], "rb").read())
pe = struct.unpack_from("<I", data, 0x3C)[0]
assert data[pe:pe + 4] == b"PE\0\0" and struct.unpack_from("<H", data, pe + 4)[0] == 0x8664
struct.pack_into("<H", data, pe + 4, 0x014C)
open(sys.argv[2], "wb").write(bytes(data))
PYEOF
then
	pass "inconsistent PE header variant prepared (PE32+ image marked i386; refusal test)"
else
	fail "inconsistent PE header variant"
fi
# boswas-winapp's own inspection of the real fixtures (same code as on devices).
inspect_out="$(cd "$BOSWAS_REPO_ROOT/packages/boswas-compat" && python3 -B -c '
import sys
sys.path.insert(0, ".")
from boswas_compat import installer
from boswas_compat.errors import Refused
from pathlib import Path
for name in sys.argv[1:]:
    try:
        i = installer.inspect(Path(name))
        print(Path(name).name, i.machine, i.pe_format)
    except Refused as exc:
        print(Path(name).name, "REFUSED", exc.reason)' "$out/boswas-testapp.exe" "$out/boswas-testapp-x86.exe" \
	"$out/boswas-testapp-badpe.exe" 2>&1)"
check "PE detection on the real binaries: 64-bit = x86_64 PE32+, 32-bit = x86 PE32, altered header refused" \
	bash -c "grep -qx 'boswas-testapp.exe x86_64 PE32+' <<<\"\$1\" && grep -qx 'boswas-testapp-x86.exe x86 PE32' <<<\"\$1\" &&
		grep -qx 'boswas-testapp-badpe.exe REFUSED bad-installer' <<<\"\$1\"" _ "$inspect_out"
main_sha="$(sha256sum "$out/boswas-testapp.exe" | cut -d' ' -f1)"
blocked_sha="$(sha256sum "$out/boswas-testapp-blocked.exe" | cut -d' ' -f1)"
unlisted_sha="$(sha256sum "$out/boswas-testapp-unlisted.exe" | cut -d' ' -f1)"
if [ "$main_sha" != "$blocked_sha" ] && [ "$main_sha" != "$unlisted_sha" ] && [ "$blocked_sha" != "$unlisted_sha" ]; then
	pass "fixture variants have distinct SHA-256 hashes"
else
	fail "fixture variants have distinct SHA-256 hashes"
fi

for tpl in "$src"/manifests/*.json.in; do
	sed -e "s/@SHA256_MAIN@/$main_sha/" -e "s/@SHA256_BLOCKED@/$blocked_sha/" "$tpl" \
		> "$out/manifests/$(basename "$tpl" .in)"
done
validate="$(cd "$BOSWAS_REPO_ROOT/packages/boswas-compat" && python3 -B -c '
import sys
sys.path.insert(0, ".")
from boswas_compat.cli import main
sys.exit(main(["manifest", "validate", *sys.argv[1:]]))' "$out"/manifests/*.json 2>&1)"
if [ $? -eq 0 ]; then
	pass "fixture manifests are valid ($(ls "$out"/manifests | wc -l) manifests)"
else
	fail "fixture manifests: $validate"
fi

if xorriso -as mkisofs -quiet -o "$TESTWORK/fixtures.iso" -V BOSWAS_FIXTURES -J -R "$out" >/dev/null 2>&1; then
	pass "fixture ISO for the QEMU boot test built"
else
	fail "fixture ISO for the QEMU boot test"
fi
finish
