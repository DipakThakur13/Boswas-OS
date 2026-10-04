#!/usr/bin/env bash
# Windows compatibility and application isolation runtimes in the image.
# (Wine actually running is verified in the live VM by tests/boot.)
set -uo pipefail
. "$(dirname "$0")/../lib.sh"

root="${TESTWORK:?}/rootfs"
[ -d "$root/usr" ] || { skip "compatibility (root filesystem not extracted)"; finish; }

check "Wine installed" pkg_installed "$root" wine
check "64-bit Wine runtime installed" pkg_installed "$root" wine64
version="$(dpkg-query --admindir="$root/var/lib/dpkg" -W -f='${Version}' wine64 2>/dev/null)"
case "$version" in
	10.*) pass "Wine 10 runtime ($version)" ;;
	*) fail "Wine 10 runtime (found: ${version:-none})" ;;
esac
# /usr/bin/wine is an alternatives symlink with an absolute target: resolve it
# inside the image root, not on the test host.
check "wine command resolves to the Debian wine-stable launcher" \
	bash -c "[ \"\$(chroot '$root' readlink -f /usr/bin/wine)\" = /usr/bin/wine-stable ] && test -x '$root/usr/bin/wine-stable'"
check "Wine 64-bit loader present" test -x "$root/usr/lib/wine/wine64"

check "Flatpak installed" pkg_installed "$root" flatpak
check "bubblewrap installed" pkg_installed "$root" bubblewrap
remotes="$(chroot "$root" flatpak remotes --system 2>/dev/null)"
[ -z "$remotes" ] && pass "no Flatpak remote configured (installs gated by policy)" || fail "unexpected Flatpak remotes: $remotes"

finish
