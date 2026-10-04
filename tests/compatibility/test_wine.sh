#!/usr/bin/env bash
# Windows compatibility and application isolation runtimes in the image.
set -uo pipefail
. "$(dirname "$0")/../lib.sh"

root="${TESTWORK:?}/rootfs"
[ -d "$root/usr" ] || { skip "compatibility (root filesystem not extracted)"; finish; }

check "Wine installed" pkg_installed "$root" wine
check "64-bit Wine runtime installed" pkg_installed "$root" wine64
check "wine command present" test -x "$root/usr/bin/wine"
version="$(chroot "$root" /usr/bin/wine --version 2>/dev/null)"
case "$version" in
	wine-10.*) pass "wine --version reports $version" ;;
	*) fail "wine --version (got: ${version:-nothing})" ;;
esac

check "Flatpak installed" pkg_installed "$root" flatpak
check "bubblewrap installed" pkg_installed "$root" bubblewrap
remotes="$(chroot "$root" flatpak remotes --system 2>/dev/null)"
[ -z "$remotes" ] && pass "no Flatpak remote configured (installs gated by policy)" || fail "unexpected Flatpak remotes: $remotes"

finish
