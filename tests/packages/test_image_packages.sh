#!/usr/bin/env bash
# Image contents: manifest packages, exclusions, Boswas components, branding,
# commands, systemd units, the native desktop.
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

for p in kdeconnect plasma-discover packagekit libpam-fprintd fprintd openssh-server cups-browsed plasma-welcome \
	plasma-workspace plasma-desktop kde-plasma-desktop kwin-wayland kwin-x11 sddm systemsettings plymouth; do
	check_not "excluded package '$p' absent" pkg_installed "$root" "$p"
done
check "live-build exclusion pins not left on the image" bash -c "! ls '$root'/etc/apt/preferences.d/boswas-exclude* 2>/dev/null"

# The native desktop: Boswas compositor, shell and session; greetd for login.
# No Plasma shell, KWin, Plasma session or SDDM.
for f in usr/bin/boswas-compositor usr/libexec/boswas/boswas-shell usr/bin/boswas-session \
	usr/libexec/boswas/boswas-greeter usr/libexec/boswas/boswas-bootsplashd usr/bin/boswas-bootsplash; do
	check "native desktop program /$f present and executable" test -x "$root/$f"
done
check "Boswas session available at login" grep -qx 'Exec=/usr/bin/boswas-session' "$root/usr/share/wayland-sessions/boswas.desktop"
check_not "no Plasma shell, KWin or Plasma session in the image" \
	bash -c "test -e '$root/usr/bin/plasmashell' || test -e '$root/usr/bin/kwin_wayland' || test -e '$root/usr/bin/startplasma-wayland' ||
		ls '$root'/usr/share/wayland-sessions/plasma*.desktop '$root'/usr/share/xsessions/plasma*.desktop >/dev/null 2>&1"
check_not "no Plymouth in the image" bash -c "test -e '$root/usr/sbin/plymouthd' || test -e '$root/usr/bin/plymouth'"
check "greetd is the display manager and reads the Boswas login configuration" \
	bash -c "readlink '$root/etc/systemd/system/display-manager.service' | grep -q greetd &&
		grep -qx 'ExecStart=/usr/sbin/greetd --config /etc/boswas/greetd.toml' '$root/usr/lib/systemd/system/greetd.service.d/10-boswas.conf' &&
		grep -qx 'command = \"/usr/libexec/boswas/boswas-greeter\"' '$root/etc/boswas/greetd.toml'"
check_not "the image does not sign a user in automatically (only the live session does, at boot)" \
	grep -q '^\[initial_session\]' "$root/etc/boswas/greetd.toml"
check "the compositor and the shell run inside the image" \
	bash -c "chroot '$root' /usr/bin/boswas-compositor --version | grep -q '^boswas-compositor ' &&
		chroot '$root' /usr/libexec/boswas/boswas-shell --version | grep -q '^boswas-shell '"

# Boswas commands
for c in boswas boswas-info boswas-status; do
	check "command /usr/bin/$c present and executable" test -x "$root/usr/bin/$c"
done
info="$(chroot "$root" /usr/bin/boswas --json info 2>/dev/null)"
check "boswas info runs inside the image" python3 -c 'import json,sys; d=json.loads(sys.argv[1]); assert d["os"]["name"]=="Boswas OS" and d["os"]["base"]["codename"]=="trixie" and d["os"]["desktop"].startswith("Boswas Desktop 1.")' "$info"
check "image build ID matches the build manifest" python3 -c '
import json, sys
info = json.loads(sys.argv[1]); latest = json.load(open(sys.argv[2]))
assert info["os"]["build"]["id"] == latest["build_id"]' "$info" "$BOSWAS_REPO_ROOT/build/manifest/latest.json"

# Branding
for f in \
	usr/lib/boswas/release usr/lib/boswas/image-info \
	usr/share/wallpapers/Boswas/metadata.json usr/share/wallpapers/Boswas/contents/images/3840x2160.png \
	usr/share/color-schemes/BoswasHorizon.colors usr/share/color-schemes/BoswasPrism.colors \
	usr/share/color-schemes/BoswasPrismLight.colors usr/share/boswas/ui/tokens.json \
	usr/share/icons/Boswas-Prism/index.theme usr/share/icons/Boswas-Prism-Light/index.theme \
	usr/share/icons/Boswas-Prism/scalable/apps/boswas-control-center.svg \
	usr/share/icons/Boswas-Prism/scalable/status/network-wired-symbolic.svg \
	usr/share/icons/Boswas/index.theme usr/share/icons/Boswas-Light/index.theme usr/bin/boswas-preset \
	usr/bin/boswas-control-center usr/share/boswas/compositor/compositor.toml usr/share/boswas/compositor/version \
	etc/boswas/greetd.toml etc/pam.d/boswas-lock usr/lib/systemd/user/boswas-session.target \
	usr/share/icons/hicolor/scalable/apps/boswas-logo.svg \
	usr/share/boswas/branding/boswas-about-logo.svg usr/share/boswas/branding/login-background.png \
	usr/share/boswas/branding/boswas-os-mark.svg usr/share/fonts/truetype/orbitron/Orbitron-SemiBold.ttf \
	usr/share/boswas/kde-settings/kdeglobals \
	etc/issue.d/boswas.issue; do
	check "branding/identity file /$f" test -s "$root/$f"
done
contains "KDE applications default to the Boswas Prism icons" "$root/usr/share/boswas/kde-settings/kdeglobals" "Theme=Boswas-Prism"
contains "KDE applications default to the Prism colour scheme" "$root/usr/share/boswas/kde-settings/kdeglobals" "ColorScheme=BoswasPrism"
# The pointer theme is Boswas's own and the system's default (Debian's x-cursor-theme alternative).
check "the pointer theme Boswas-Cursors is installed" test -s "$root/usr/share/icons/Boswas-Cursors/cursors/default"
check "the default cursor theme of the system is Boswas-Cursors" \
	bash -c "[ \"\$(readlink '$root/etc/alternatives/x-cursor-theme')\" = /usr/share/icons/Boswas-Cursors/cursor.theme ]"
contains "the compositor's pointer theme is Boswas-Cursors" "$root/usr/share/boswas/compositor/compositor.toml" 'cursor_theme = "Boswas-Cursors"'
# Nothing of Boswas is installed for programs the image does not have (ADR-0042): Plasma (Global
# Themes), Konsole (profiles, colour schemes, a default profile), System Settings (the About
# module's file), Dolphin (service menus). The lists are dpkg's, so files of Debian packages do
# not count.
check "the Boswas packages' file lists are present (dpkg)" bash -c "ls '$root'/var/lib/dpkg/info/boswas-branding.list '$root'/var/lib/dpkg/info/boswas-compat-manager.list"
check_not "no Boswas package installs anything under /usr/share/plasma, /usr/share/konsole or /usr/share/kio" \
	bash -c "grep -qE '^/usr/share/(plasma|konsole|kio)(/|\$)' '$root'/var/lib/dpkg/info/boswas-*.list"
check_not "no Konsole default profile and no System Settings module file among the KDE application defaults" \
	bash -c "test -e '$root/usr/share/boswas/kde-settings/konsolerc' || test -e '$root/usr/share/boswas/kde-settings/kcm-about-distrorc'"
check "\"Install with Boswas Compatibility Manager\" is offered for Windows installers (a hidden entry with --install %f)" \
	bash -c "e='$root/usr/share/applications/com.boswas.CompatibilityManager.Install.desktop'
		grep -qx 'NoDisplay=true' \$e && grep -qx 'Exec=boswas-compat-manager --install %f' \$e &&
		grep -q '^MimeType=.*application/x-msi;' \$e"
check "the Boswas desktop's defaults name \"Run with Boswas\" for Windows programs and installer packages" \
	bash -c "for t in application/x-msdownload application/x-ms-dos-executable application/vnd.microsoft.portable-executable application/x-msi; do
			grep -qx \"\$t=boswas-winapp-install.desktop\" '$root/usr/share/applications/boswas-mimeapps.list' || exit 1
		done"
check_not "boswas-preset names no KDE program" \
	grep -rqiE 'kwriteconfig|kreadconfig|plasma|konsole|kscreenlocker|qdbus|lookandfeel' \
	"$root/usr/lib/boswas/python/boswas_preset" "$root/usr/bin/boswas-preset"
contains "os-release names Boswas OS" "$root/etc/os-release" 'NAME="Boswas OS"'
contains "os-release keeps ID_LIKE=debian for Debian tooling" "$root/etc/os-release" 'ID_LIKE=debian'
check "ten Boswas OS presets installed (colour schemes and wallpapers)" \
	bash -c "[ \$(ls '$root'/usr/share/color-schemes/Boswas*.colors | grep -vc 'BoswasPrism') -eq 10 ] &&
		[ \$(ls -d '$root'/usr/share/wallpapers/Boswas-* | wc -l) -eq 10 ]"
check_not "no Debian branding in the console banners and message of the day" \
	grep -qi debian "$root/etc/issue" "$root/etc/issue.net" "$root/etc/motd"
check "live session: the live user is Boswas OS Live (not Debian Live user)" \
	grep -q 'LIVE_USER_FULLNAME="Boswas OS Live"' "$root/etc/live/config.conf.d/boswas.conf"

# systemd units
for u in nftables.service auditd.service apparmor.service greetd.service NetworkManager.service \
	boswas-bootsplash-quit.service boswas-bootsplash-poweroff.service; do
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
