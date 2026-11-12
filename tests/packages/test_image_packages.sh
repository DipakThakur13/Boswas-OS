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
	usr/share/plasma/look-and-feel/com.boswas.horizon/metadata.json \
	usr/share/plasma/look-and-feel/com.boswas.horizon/contents/layouts/org.kde.plasma.desktop-layout.js \
	usr/share/color-schemes/BoswasHorizon.colors usr/share/plymouth/themes/boswas/boswas.plymouth \
	usr/share/icons/Boswas/index.theme usr/share/icons/Boswas-Light/index.theme usr/bin/boswas-preset \
	usr/share/plasma/plasmoids/com.boswas.launcher/metadata.json usr/bin/boswas-control-center \
	usr/share/icons/hicolor/scalable/apps/boswas-logo.svg \
	usr/share/boswas/branding/boswas-about-logo.svg usr/share/boswas/branding/login-background.png \
	usr/share/boswas/branding/boswas-os-mark.svg usr/share/fonts/truetype/orbitron/Orbitron-SemiBold.ttf \
	usr/share/boswas/kde-settings/kdeglobals usr/share/boswas/kde-settings/kcm-about-distrorc \
	etc/xdg/plasma-workspace/env/boswas-kde-settings.sh etc/sddm.conf.d/10-boswas.conf \
	usr/share/sddm/themes/breeze/theme.conf.user etc/issue.d/boswas.issue; do
	check "branding/identity file /$f" test -s "$root/$f"
done
contains "default Plasma global theme is Boswas Horizon" "$root/usr/share/boswas/kde-settings/kdeglobals" "LookAndFeelPackage=com.boswas.horizon"
contains "About page shows Boswas OS" "$root/usr/share/boswas/kde-settings/kcm-about-distrorc" "Name=Boswas OS"
contains "os-release names Boswas OS" "$root/etc/os-release" 'NAME="Boswas OS"'
contains "os-release keeps ID_LIKE=debian for Debian tooling" "$root/etc/os-release" 'ID_LIKE=debian'
check "ten Boswas OS presets installed (Global Themes com.boswas.<preset>)" \
	bash -c "[ \$(ls -d '$root'/usr/share/plasma/look-and-feel/com.boswas.* | grep -vc 'com.boswas.splash') -eq 10 ]"
check_not "no Debian branding in the console banners and message of the day" \
	grep -qi debian "$root/etc/issue" "$root/etc/issue.net" "$root/etc/motd"
check "live session: the live user is Boswas OS Live (not Debian Live user)" \
	grep -q 'LIVE_USER_FULLNAME="Boswas OS Live"' "$root/etc/live/config.conf.d/boswas.conf"

# systemd units
for u in nftables.service auditd.service apparmor.service sddm.service NetworkManager.service; do
	check "unit $u exists" bash -c "test -e '$root/usr/lib/systemd/system/$u' || test -e '$root/lib/systemd/system/$u'"
done
for u in nftables.service auditd.service apparmor.service NetworkManager.service; do
	check "unit $u enabled" bash -c "[ \"\$(chroot '$root' systemctl is-enabled $u 2>/dev/null)\" = enabled ]"
done
check "Boswas nftables drop-in installed" test -s "$root/usr/lib/systemd/system/nftables.service.d/boswas.conf"

# Firmware in the live/installed system (the installer medium: tests/build and tests/installer)
missing_fw=""
for pattern in $(sed -e 's/#.*//' -e '/^[[:space:]]*$/d' "$BOSWAS_REPO_ROOT/config/boswas/firmware-required.list"); do
	compgen -G "$root/usr/lib/firmware/$pattern" >/dev/null || missing_fw="$missing_fw $pattern"
done
[ -z "$missing_fw" ] && pass "every required firmware file is in the image (/usr/lib/firmware)" \
	|| fail "required firmware missing from the image:$missing_fw"
check "firmware index maps rtl_nic/rtl8125b-2.fw to firmware-realtek (/usr/share/boswas/firmware/index.tsv)" \
	grep -qx "$(printf 'rtl_nic/rtl8125b-2.fw\tfirmware-realtek')" "$root/usr/share/boswas/firmware/index.tsv"
# Installation: the installer's last step (writes signed sources offline) and its template
check "installer finish step present and executable (/usr/lib/boswas/installer/finish-install)" \
	test -x "$root/usr/lib/boswas/installer/finish-install"
check "signed Debian sources template present (Signed-By the Debian archive keyring, security updates)" \
	bash -c "grep -q 'Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg' '$root/usr/share/boswas/apt/debian.sources' &&
		grep -q 'trixie-security' '$root/usr/share/boswas/apt/debian.sources' && test -s '$root/usr/share/keyrings/debian-archive-keyring.gpg'"

# Applications: privileged helper and Android manager installed; no Android runtime,
# no Android images and no Google components preinstalled
check "boswas-sysops installed (helper only reachable through pkexec)" \
	bash -c "test -x '$root/usr/libexec/boswas/sysops' && test -s '$root/usr/share/polkit-1/actions/com.boswas.sysops.policy'"
check "boswas-android installed" test -x "$root/usr/bin/boswas-android"
for p in waydroid lxc python3-gbinder; do
	check_not "the Android runtime is not preinstalled ($p absent)" pkg_installed "$root" "$p"
done
check_not "no Android images in the image (/var/lib/waydroid)" test -e "$root/var/lib/waydroid"
check_not "no Google Play / Google Mobile Services package preinstalled" \
	bash -c "dpkg-query --admindir='$root/var/lib/dpkg' -W -f='\${Package}\n' | grep -qiE '(^|-)(gapps|google-play|play-services|gms)(-|$)'"
check_not "no Waydroid or other third-party APT source preconfigured (the Android repository is opt-in)" \
	bash -c "grep -rqsE 'waydro' '$root/etc/apt/sources.list' '$root/etc/apt/sources.list.d/' '$root/etc/apt/preferences.d/'"
check_not "no Flatpak remote preconfigured (an administrator enables an approved one)" \
	bash -c "grep -qs '^\[remote' '$root/var/lib/flatpak/repo/config'"

finish
