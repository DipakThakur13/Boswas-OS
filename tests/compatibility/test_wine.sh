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

# --- Boswas WinCompat (boswas-compat) ---------------------------------------------
check "boswas-compat installed" pkg_installed "$root" boswas-compat
check "boswas-winapp command present" test -x "$root/usr/bin/boswas-winapp"
check "in-sandbox runner present (AppArmor attachment point)" test -x "$root/usr/lib/boswas/compat/winapp-exec"
check "Wine AppArmor profile installed" test -s "$root/etc/apparmor.d/boswas-winapp"
check "local AppArmor override file created by the package (empty)" \
	bash -c "test -e '$root/etc/apparmor.d/local/boswas-winapp' && ! test -s '$root/etc/apparmor.d/local/boswas-winapp'"
if out="$(chroot "$root" apparmor_parser -Q -K -T --features-file /usr/share/apparmor-features/features \
	/etc/apparmor.d/boswas-winapp 2>&1)"; then
	pass "Wine AppArmor profile compiles with the image's apparmor_parser"
else
	fail "Wine AppArmor profile compile: $out"
fi
policy="$root/etc/boswas/compat/policy.conf"
contains "WinCompat policy requires AppArmor confinement" "$policy" 'REQUIRE_APPARMOR="yes"'
contains "WinCompat policy gives unlisted applications no network" "$policy" 'UNLISTED_NETWORK="no"'
catalog="$(chroot "$root" /usr/bin/boswas-winapp --json catalog 2>/dev/null)"
check "boswas-winapp runs in the image and reads policy and catalog" python3 -c '
import json, sys
d = json.loads(sys.argv[1])
assert d["schema"] == "boswas-winapp/1" and d["policy"]["require_apparmor"] is True and d["problems"] == []' "$catalog"
version="$(chroot "$root" /usr/bin/boswas-winapp --version 2>/dev/null)"
check "boswas-winapp version matches the release ($version)" test "$version" = "boswas-winapp $(. "$BOSWAS_REPO_ROOT/config/boswas/release.conf" && echo "$BOSWAS_VERSION_ID")"
out="$(chroot "$root" /usr/bin/boswas-winapp install /usr/share/boswas/compat/runtime.conf 2>&1)"
[ $? -eq 4 ] && grep -q root <<<"$out" && pass "boswas-winapp refuses to install as root (exit 4)" \
	|| fail "boswas-winapp install as root: $out"
check "\"Run with Boswas\" desktop entry is valid" chroot "$root" desktop-file-validate /usr/share/applications/boswas-winapp-install.desktop
check "\"Run with Boswas\" is the handler for Windows executables" \
	grep -qE '^application/x-msdownload=.*boswas-winapp-install.desktop' "$root/usr/share/applications/mimeinfo.cache"
check_not "no other handler runs Windows executables unconfined (wine.desktop)" \
	grep -qE '^application/(x-msdownload|x-ms-dos-executable)=.*wine' "$root/usr/share/applications/mimeinfo.cache"

finish
