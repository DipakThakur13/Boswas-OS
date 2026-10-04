#!/usr/bin/env bash
# Build the WinCompat test fixtures from source (no binaries are committed).
#
#   $TESTWORK/fixtures/boswas-testapp.exe           the test application
#   $TESTWORK/fixtures/boswas-testapp-blocked.exe   byte-different variant, blocked by a manifest
#   $TESTWORK/fixtures/boswas-testapp-unlisted.exe  byte-different variant, in no manifest
#   $TESTWORK/fixtures/boswas-testapp-x86.exe       the same program marked as a 32-bit (i386) PE file:
#                                                   refused before anything runs (64-bit only)
#   $TESTWORK/fixtures/manifests/*.json             manifests pinned to the built hashes
#   $TESTWORK/fixtures.iso                          all of the above, for the QEMU boot test
#
# Needs the MinGW-w64 cross-compiler of the builder image.
set -uo pipefail
. "$(dirname "$0")/../lib.sh"
. "$BOSWAS_REPO_ROOT/build/scripts/lib.sh"

src="$BOSWAS_REPO_ROOT/tests/compatibility/fixtures"
out="${TESTWORK:?TESTWORK not set}/fixtures"
cc=x86_64-w64-mingw32-gcc

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
# A 32-bit-marked copy: only the PE machine field changes (0x8664 -> 0x014c).
# Boswas OS must refuse it from the header alone; it is never executed.
if python3 - "$out/boswas-testapp-unlisted.exe" "$out/boswas-testapp-x86.exe" <<'PYEOF'
import struct, sys
data = bytearray(open(sys.argv[1], "rb").read())
pe = struct.unpack_from("<I", data, 0x3C)[0]
assert data[pe:pe + 4] == b"PE\0\0" and struct.unpack_from("<H", data, pe + 4)[0] == 0x8664
struct.pack_into("<H", data, pe + 4, 0x014C)
open(sys.argv[2], "wb").write(bytes(data))
PYEOF
then
	pass "32-bit (i386) variant of the test application prepared (refusal tests)"
else
	fail "32-bit variant of the test application"
fi
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
