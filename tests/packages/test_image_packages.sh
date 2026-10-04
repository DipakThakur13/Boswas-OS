#!/usr/bin/env bash
# Image contents: manifest packages, exclusions, Boswas components, branding,
# commands, systemd units, KDE.
set -uo pipefail
. "$(dirname "$0")/../lib.sh"

root="${TESTWORK:?}/rootfs"
[ -d "$root/usr" ] || { skip "image packages (root filesystem not extracted)"; finish; }

# Every package named in the manifest is installed
missing=""
for p in $(manifest_packages); do
	pkg_installed "$root" "$p" || missing="$missing $p"
done
[ -z "$missing" ] && pass "all $(manifest_packages | wc -l) manifest packages installed" || fail "manifest packages missing:$missing"

for p in kdeconnect plasma-discover packagekit libpam-fprintd fprintd openssh-server cups-browsed plasma-welcome; do
	check_not "excluded package '$p' absent" pkg_installed "$root" "$p"
done
check "live-build exclusion pins not left on the image" bash -c "! ls '$root'/etc/apt/preferences.d/boswas-exclude* 2>/dev/null"

# KDE Plasma
check "KDE Plasma installed (plasmashell)" test -x "$root/usr/bin/plasmashell"
check "Plasma Wayland session available" test -e "$root/usr/share/wayland-sessions/plasma.desktop"
check "SDDM is the display manager" bash -c "readlink '$root/etc/systemd/system/display-manager.service' | grep -q sddm"

# Boswas commands
for c in boswas boswas-info boswas-status; do
	check "command /usr/bin/$c present and executable" test -x "$root/usr/bin/$c"
done
info="$(chroot "$root" /usr/bin/boswas --json info 2>/dev/null)"
check "boswas info runs inside the image" python3 -c 'import json,sys; d=json.loads(sys.argv[1]); assert d["os"]["name"]=="Boswas OS" and d["os"]["base"]["codename"]=="trixie" and d["os"]["desktop"].startswith("KDE Plasma 6")' "$info"
check "image build ID matches the build manifest" python3 -c '
import json, sys
info = json.loads(sys.argv[1]); latest = json.load(open(sys.argv[2]))
assert info["os"]["build"]["id"] == latest["build_id"]' "$info" "$BOSWAS_REPO_ROOT/build/manifest/latest.json"

# Branding
for f in \
	usr/lib/boswas/release usr/lib/boswas/image-info \
	usr/share/wallpapers/Boswas/metadata.json usr/share/wallpapers/Boswas/contents/images/3840x2160.png \
	usr/share/plasma/look-and-feel/com.boswas.desktop/metadata.json \
	usr/share/plasma/look-and-feel/com.boswas.desktop/contents/layouts/org.kde.plasma.desktop-layout.js \
	usr/share/color-schemes/BoswasDark.colors \
	usr/share/icons/hicolor/scalable/apps/boswas-logo.svg \
	usr/share/boswas/branding/boswas-about-logo.svg usr/share/boswas/branding/login-background.png \
	usr/share/boswas/kde-settings/kdeglobals usr/share/boswas/kde-settings/kcm-about-distrorc \
	etc/xdg/plasma-workspace/env/boswas-kde-settings.sh etc/sddm.conf.d/10-boswas.conf \
	usr/share/sddm/themes/breeze/theme.conf.user etc/issue.d/boswas.issue; do
	check "branding/identity file /$f" test -s "$root/$f"
done
contains "default Plasma global theme is Boswas" "$root/usr/share/boswas/kde-settings/kdeglobals" "LookAndFeelPackage=com.boswas.desktop"
contains "About page shows Boswas OS" "$root/usr/share/boswas/kde-settings/kcm-about-distrorc" "Name=Boswas OS"
contains "Debian identity preserved in /etc/os-release" "$root/etc/os-release" 'ID=debian'

# systemd units
for u in nftables.service auditd.service apparmor.service sddm.service NetworkManager.service; do
	check "unit $u exists" bash -c "test -e '$root/usr/lib/systemd/system/$u' || test -e '$root/lib/systemd/system/$u'"
done
for u in nftables.service auditd.service apparmor.service NetworkManager.service; do
	check "unit $u enabled" bash -c "[ \"\$(chroot '$root' systemctl is-enabled $u 2>/dev/null)\" = enabled ]"
done
check "Boswas nftables drop-in installed" test -s "$root/usr/lib/systemd/system/nftables.service.d/boswas.conf"

finish
