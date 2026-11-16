#!/usr/bin/env bash
# Boswas packages: build, lintian, contents, CLI unit tests.
set -uo pipefail
. "$(dirname "$0")/../lib.sh"
. "$BOSWAS_REPO_ROOT/build/scripts/lib.sh"
load_release

debs="${TESTWORK:?TESTWORK not set}/debs"
rm -rf "$debs"
if "$BOSWAS_REPO_ROOT/build/scripts/build-packages.sh" "$debs" > "$TESTWORK/build-packages.log" 2>&1; then
	pass "Boswas packages build with dpkg-buildpackage (incl. CLI unit tests)"
else
	fail "Boswas packages build (see $TESTWORK/build-packages.log)"
	finish
fi

for p in boswas-os boswas-cli boswas-branding boswas-security boswas-compat boswas-device-agent boswas-compat-manager \
	boswas-control-center boswas-apps; do
	check "${p}_${BOSWAS_VERSION_ID} .deb produced" test -s "$debs/${p}_${BOSWAS_VERSION_ID}_all.deb"
done
# The native desktop and the boot screen are compiled programs (amd64).
for p in boswas-desktop boswas-bootsplash; do
	check "${p}_${BOSWAS_VERSION_ID} .deb produced (amd64)" test -s "$debs/${p}_${BOSWAS_VERSION_ID}_amd64.deb"
done
server_deb="$debs/server/boswas-control-plane_${BOSWAS_VERSION_ID}_all.deb"
check "boswas-control-plane_${BOSWAS_VERSION_ID} .deb produced (server; not in the device package set)" \
	bash -c "test -s '$server_deb' && ! ls '$debs'/boswas-control-plane_*.deb >/dev/null 2>&1"

# Lintian: errors and warnings fail the suite.
if lintian_out="$(lintian --fail-on error,warning "$debs"/*.deb "$server_deb" 2>&1)"; then
	pass "lintian: no errors or warnings"
else
	fail "lintian: $(printf '%s' "$lintian_out" | grep -E '^[EW]:' | tr '\n' ';')"
fi

# Permissions that matter for security
listing="$(for d in "$debs"/*.deb "$server_deb"; do dpkg-deb -c "$d"; done)"
# Checks that run in a child shell read the listing from a file: it is longer
# than one command-line argument may be (128 KiB) since the icon packs grew.
listing_file="$(mktemp)"
printf '%s\n' "$listing" > "$listing_file"
check "sudoers drop-in is 0440 root:root" grep -qE '^-r--r----- root/root .* \./etc/sudoers\.d/boswas$' <<<"$listing"
check "CLI entry points are executable" grep -qE '^-rwxr-xr-x root/root .* \./usr/bin/boswas$' <<<"$listing"
check_not "no world-writable files" grep -qE '^-.......w' <<<"$listing"
check_not "no setuid/setgid files" grep -qE '^-..[sS]|^-.....[sS]' <<<"$listing"

# Dependencies wire the components together
os_depends="$(dpkg-deb -f "$debs/boswas-os_${BOSWAS_VERSION_ID}_all.deb" Depends 2>/dev/null)"
check "boswas-os depends on all Boswas device components" bash -c '
	for p in boswas-branding boswas-security boswas-cli "boswas-compat " boswas-device-agent boswas-compat-manager \
		boswas-control-center boswas-sysops boswas-android boswas-apps boswas-desktop boswas-bootsplash; do
		grep -q -- "$p" <<<"$1" || exit 1
	done' _ "$os_depends"
check_not "boswas-os does not pull in the Control Plane" grep -q boswas-control-plane <<<"$os_depends"

# Boswas OS identity (ADR-0005): diverted os-release and console banners, Debian's
# EFI directory kept for Secure Boot, live-session integration.
os_deb="$debs/boswas-os_${BOSWAS_VERSION_ID}_all.deb"
os_x="$(mktemp -d)" os_ctl="$(mktemp -d)"
dpkg-deb -x "$os_deb" "$os_x"
dpkg-deb -e "$os_deb" "$os_ctl"
check "identity: os-release names Boswas OS (ID=boswas, ID_LIKE=debian, VERSION_ID without '~')" \
	bash -c "grep -qx 'NAME=\"Boswas OS\"' '$os_x/usr/lib/os-release' && grep -qx 'ID=boswas' '$os_x/usr/lib/os-release' &&
		grep -qx 'ID_LIKE=debian' '$os_x/usr/lib/os-release' && grep -qE '^VERSION_ID=\"[a-z0-9._-]+\"$' '$os_x/usr/lib/os-release'"
check_not "identity: console banners and message of the day do not name Debian" \
	grep -qi debian "$os_x/usr/share/boswas/issue" "$os_x/usr/share/boswas/issue.net" "$os_x/usr/share/boswas/motd"
check "identity: base-files' os-release is diverted on install and restored on removal" \
	bash -c "grep -q -- '--add --rename --divert /usr/lib/os-release.debian /usr/lib/os-release' '$os_ctl/preinst' &&
		grep -q -- '--remove --rename --divert /usr/lib/os-release.debian /usr/lib/os-release' '$os_ctl/postrm'"
check_not "identity: no conffile is diverted (Debian Policy: dpkg does not handle it well)" \
	bash -c "grep -E 'dpkg-divert' '$os_ctl/preinst' | grep -qE '/etc/'"
check "identity: GRUB keeps Debian's distributor (EFI directory, Secure Boot) and hides its menu" \
	bash -c "grep -qx 'GRUB_DISTRIBUTOR=\"Debian\"' '$os_x/etc/default/grub.d/10-boswas.cfg' &&
		grep -qx 'GRUB_TIMEOUT_STYLE=hidden' '$os_x/etc/default/grub.d/10-boswas.cfg'"
check "live session: live user name, Install Boswas OS entry and Live marker (live-config component)" \
	bash -c "grep -q 'LIVE_USER_FULLNAME=\"Boswas OS Live\"' '$os_x/etc/live/config.conf.d/boswas.conf' &&
		test -x '$os_x/usr/lib/live/config/9000-boswas' &&
		grep -q 'Exec=boswas-control-center --page install' '$os_x/usr/lib/live/config/9000-boswas'"
check_not "live session: the live-config component never starts an installer or a browser" \
	grep -qE '^[^#]*(debian-installer|calamares|ubiquity|firefox|xdg-open|kioclient)' "$os_x/usr/lib/live/config/9000-boswas"
rm -rf "$os_x" "$os_ctl"

# WinCompat package
compat_deb="$debs/boswas-compat_${BOSWAS_VERSION_ID}_all.deb"
compat_depends="$(dpkg-deb -f "$compat_deb" Depends 2>/dev/null)"
check "boswas-compat depends on Wine 10, bubblewrap and AppArmor" \
	bash -c "grep -q 'wine64 (>= 10' <<<'$compat_depends' && grep -q bubblewrap <<<'$compat_depends' && grep -q apparmor <<<'$compat_depends'"
check "boswas-winapp command and in-sandbox runner are executable" \
	bash -c "grep -qE '^-rwxr-xr-x root/root .* \./usr/bin/boswas-winapp$' <\"\$1\" && grep -qE '^-rwxr-xr-x root/root .* \./usr/lib/boswas/compat/winapp-exec$' <\"\$1\"" _ "$listing_file"
check "Wine AppArmor profile and WinCompat policy shipped (0644 conffiles)" \
	bash -c "grep -qE '^-rw-r--r-- root/root .* \./etc/apparmor.d/boswas-winapp$' <\"\$1\" && grep -qE '^-rw-r--r-- root/root .* \./etc/boswas/compat/policy.conf$' <\"\$1\"" _ "$listing_file"
ctl="$(mktemp -d)"
dpkg-deb -e "$compat_deb" "$ctl" 2>/dev/null
check "boswas-compat postinst loads the AppArmor profile (dh_apparmor)" grep -q 'apparmor_parser -r -T -W "$APP_PROFILE"' "$ctl/postinst"
check "AppArmor profile and policy are conffiles" bash -c "grep -qx /etc/apparmor.d/boswas-winapp '$ctl/conffiles' && grep -qx /etc/boswas/compat/policy.conf '$ctl/conffiles'"
rm -rf "$ctl"
if command -v apparmor_parser >/dev/null 2>&1 && [ -e /usr/share/apparmor-features/features ]; then
	pextract="$(mktemp -d)"
	dpkg-deb -x "$compat_deb" "$pextract"
	if out="$(apparmor_parser -Q -K -T --base /etc/apparmor.d --features-file /usr/share/apparmor-features/features \
		"$pextract/etc/apparmor.d/boswas-winapp" 2>&1)"; then
		pass "Wine AppArmor profile compiles (apparmor_parser, Debian 13 feature set)"
	else
		fail "Wine AppArmor profile does not compile: $out"
	fi
	rm -rf "$pextract"
else
	skip "Wine AppArmor profile compile check (apparmor_parser not installed; rebuild the builder image)"
fi

# Device agent package
agent_deb="$debs/boswas-device-agent_${BOSWAS_VERSION_ID}_all.deb"
check "device agent: boswas-device, the agent, the session agent and the update helper are executable" \
	bash -c "for f in usr/bin/boswas-device usr/lib/boswas/agent/boswas-device-agent usr/lib/boswas/agent/boswas-session-agent usr/lib/boswas/agent/agent-update; do grep -qE \"^-rwxr-xr-x root/root .* \\./\$f\$\" <\"\$1\" || exit 1; done" _ "$listing_file"
check "device agent: system unit, update template and user unit shipped" \
	bash -c "grep -q '\./usr/lib/systemd/system/boswas-device-agent.service$' <\"\$1\" && grep -q '\./usr/lib/systemd/system/boswas-agent-update@.service$' <\"\$1\" && grep -q '\./usr/lib/systemd/user/boswas-session-agent.service$' <\"\$1\"" _ "$listing_file"
ctl="$(mktemp -d)"
dpkg-deb -e "$agent_deb" "$ctl" 2>/dev/null
check "device agent: postinst enables the agent and, for every user, the session agent" \
	bash -c "grep -q \"deb-systemd-helper enable 'boswas-device-agent.service'\" '$ctl/postinst' && grep -q \"deb-systemd-helper --user enable 'boswas-session-agent.service'\" '$ctl/postinst'"
check "device agent depends on boswas-compat and openssl" \
	bash -c "dpkg-deb -f '$agent_deb' Depends | grep -q 'boswas-compat (>= ' && dpkg-deb -f '$agent_deb' Depends | grep -q openssl"
rm -rf "$ctl"

# Compatibility Manager package
manager_deb="$debs/boswas-compat-manager_${BOSWAS_VERSION_ID}_all.deb"
check "Compatibility Manager: command, desktop entry and service menu shipped" \
	bash -c "grep -qE '^-rwxr-xr-x root/root .* \./usr/bin/boswas-compat-manager$' <\"\$1\" && grep -q '\./usr/share/applications/com.boswas.CompatibilityManager.desktop$' <\"\$1\" && grep -q '\./usr/share/kio/servicemenus/boswas-compat-manager-install.desktop$' <\"\$1\"" _ "$listing_file"
check "Compatibility Manager depends on PySide6 and the device agent (its backend)" \
	bash -c "dpkg-deb -f '$manager_deb' Depends | grep -q python3-pyside6.qtwidgets && dpkg-deb -f '$manager_deb' Depends | grep -q boswas-device-agent"

# Control Plane package (server)
ctl="$(mktemp -d)"
dpkg-deb -e "$server_deb" "$ctl" 2>/dev/null
check "Control Plane: configuration is a conffile" grep -qx /etc/boswas-control-plane/control-plane.conf "$ctl/conffiles"
# --no-enable --no-start: a new installation neither enables nor starts it; the
# re-enable runs only for an administrator who enabled it (debian-installed).
check "Control Plane: the service is not enabled or started at installation (needs boswas-cp init first)" \
	bash -c "grep -q \"debian-installed 'boswas-control-plane.service'\" '$ctl/postinst' &&
		! grep -q 'was-enabled defaults to true' '$ctl/postinst' &&
		! grep -qE \"deb-systemd-invoke (start|restart) 'boswas-control-plane\" '$ctl/postinst'"
check "Control Plane: creates its unprivileged service user (sysusers)" grep -q systemd-sysusers "$ctl/postinst"
check "Control Plane: dashboard and protocol modules shipped in its own module directory" \
	bash -c "grep -q '\./usr/share/boswas-control-plane/dashboard/app.js$' <\"\$1\" && grep -q '\./usr/lib/boswas-control-plane/python/boswas_agent/commands.py$' <\"\$1\" && ! grep -q '\./usr/lib/boswas/python/' <<<\"\$(dpkg-deb -c '$server_deb')\"" _ "$listing_file"
rm -rf "$ctl"

# Boswas Control Center (settings hub; Security, Software and Update Centers)
cc_deb="$debs/boswas-control-center_${BOSWAS_VERSION_ID}_all.deb"
check "Control Center: command and its four desktop entries shipped (no Install entry: live sessions only)" \
	bash -c "grep -qE '^-rwxr-xr-x root/root .* \./usr/bin/boswas-control-center$' <\"\$1\" &&
		for e in ControlCenter SecurityCenter SoftwareCenter UpdateCenter; do
			grep -q \"\\./usr/share/applications/com.boswas.\$e.desktop$\" <\"\$1\" || exit 1; done &&
		! grep -q 'InstallBoswasOS' <\"\$1\"" _ "$listing_file"
check "Control Center depends on PySide6, KDE's module launcher, the CLI, the device agent and the branding (presets)" \
	bash -c "d=\$(dpkg-deb -f '$cc_deb' Depends); for p in python3-pyside6.qtwidgets libkf6kcmutils-bin boswas-cli boswas-device-agent boswas-branding; do
		grep -q \"\$p\" <<<\"\$d\" || exit 1; done"

# boswas-os: the installer's last step, the sources it writes, the required firmware list
check "boswas-os ships the installer's finish step, the signed Debian sources template and the required firmware list" \
	bash -c "grep -qE '^-rwxr-xr-x root/root .* \./usr/lib/boswas/installer/finish-install$' <\"\$1\" &&
		grep -qE '^-rw-r--r-- root/root .* \./usr/share/boswas/apt/debian.sources$' <\"\$1\" &&
		grep -qE '^-rw-r--r-- root/root .* \./usr/share/boswas/firmware/required.list$' <\"\$1\"" _ "$listing_file"

# Privileged helper: started only by pkexec, one polkit action per operation
sysops_deb="$debs/boswas-sysops_${BOSWAS_VERSION_ID}_all.deb"
check "boswas-sysops: helper in /usr/libexec (0755), polkit actions (0644), nothing on the PATH" \
	bash -c "grep -qE '^-rwxr-xr-x root/root .* \./usr/libexec/boswas/sysops$' <\"\$1\" &&
		grep -qE '^-rw-r--r-- root/root .* \./usr/share/polkit-1/actions/com.boswas.sysops.policy$' <\"\$1\" &&
		! grep -qE '\./usr/(s?bin|local/bin)/[^ ]*sysops' <\"\$1\"" _ "$listing_file"
ctl="$(mktemp -d)"
dpkg-deb -e "$sysops_deb" "$ctl" 2>/dev/null
check "boswas-sysops: /etc/boswas/apps.conf is a conffile; depends on apt, pkexec and polkitd" \
	bash -c "grep -qx /etc/boswas/apps.conf '$ctl/conffiles' && d=\$(dpkg-deb -f '$sysops_deb' Depends) &&
		grep -q apt <<<\"\$d\" && grep -q pkexec <<<\"\$d\" && grep -q polkitd <<<\"\$d\""
rm -rf "$ctl"
sx="$(mktemp -d)"
dpkg-deb -x "$sysops_deb" "$sx"
check "boswas-sysops: every operation is a polkit action; state changes need administrator authentication" python3 - \
	"$sx/usr/share/polkit-1/actions/com.boswas.sysops.policy" <<'PYEOF'
import sys
import xml.etree.ElementTree as ET
root = ET.parse(sys.argv[1]).getroot()
read_only = {"firmware-report", "android-service"}
seen = set()
for action in root.findall("action"):
    ann = {a.get("key"): a.text for a in action.findall("annotate")}
    op = ann["org.freedesktop.policykit.exec.argv1"]
    assert ann["org.freedesktop.policykit.exec.path"] == "/usr/libexec/boswas/sysops", op
    assert action.get("id") == f"com.boswas.sysops.{op}", op
    d = action.find("defaults")
    assert d.findtext("allow_any") == "no" and d.findtext("allow_inactive") == "no", op
    assert d.findtext("allow_active") == ("yes" if op in read_only else "auth_admin_keep"), op
    seen.add(op)
assert {"apt-install", "apt-remove", "apt-upgrade", "android-runtime-install", "android-init"} <= seen, seen
PYEOF
rm -rf "$sx"

# Android runtime manager: no runtime, no autostart, the pinned repository key
android_deb="$debs/boswas-android_${BOSWAS_VERSION_ID}_all.deb"
check "boswas-android: command (0755), runtime.conf and the Waydroid repository keyring (0644)" \
	bash -c "grep -qE '^-rwxr-xr-x root/root .* \./usr/bin/boswas-android$' <\"\$1\" &&
		grep -qE '^-rw-r--r-- root/root .* \./usr/share/boswas/android/runtime.conf$' <\"\$1\" &&
		grep -qE '^-rw-r--r-- root/root .* \./usr/share/boswas/keyrings/waydroid.gpg$' <\"\$1\"" _ "$listing_file"
check_not "boswas-android: does not depend on or recommend the Android runtime (opt-in after installation)" \
	bash -c "dpkg-deb -f '$android_deb' Depends Recommends | grep -qiE 'waydroid|lxc|gbinder'"
check_not "boswas-android: ships no desktop entry, autostart or service (nothing starts Android by itself)" \
	bash -c "dpkg-deb -c '$android_deb' | grep -qE '\.(desktop|service|timer)$|/autostart/'"
ax="$(mktemp -d)"
dpkg-deb -x "$android_deb" "$ax"
check "boswas-android: Android images are VANILLA only (never GAPPS)" \
	bash -c "grep -qx 'IMAGE_TYPE=\"VANILLA\"' '$ax/usr/share/boswas/android/runtime.conf' &&
		! grep -v '^[[:space:]]*#' '$ax/usr/share/boswas/android/runtime.conf' | grep -qi 'gapps'"
rm -rf "$ax"

# Package contents never carry key material
extract="$(mktemp -d)"
for d in "$debs"/*.deb "$server_deb"; do dpkg-deb -x "$d" "$extract"; done
check_not "no private keys in packages" grep -rqE -- '-----BEGIN ([A-Z]+ )?PRIVATE KEY-----' "$extract"
check "KDE icon is self-contained (no external <image> references)" \
	bash -c "! grep -q '<image' '$extract/usr/share/icons/hicolor/scalable/apps/boswas-logo.svg'"
check "branding: the Boswas OS mark (gold and silver gradients) is the launcher icon and About logo" \
	bash -c "for f in usr/share/icons/hicolor/scalable/apps/boswas-logo.svg usr/share/boswas/branding/boswas-about-logo.svg \
		usr/share/boswas/branding/boswas-os-mark.svg; do grep -q 'boswas-mark-gold' '$extract/'\$f && grep -q 'boswas-mark-silver' '$extract/'\$f || exit 1; done"
check "branding: Orbitron brand typeface shipped (6 static weights, 0644) with its SIL OFL 1.1 licence" \
	bash -c "test \$(grep -cE '^-rw-r--r-- root/root .* \./usr/share/fonts/truetype/orbitron/Orbitron-(Regular|Medium|SemiBold|Bold|ExtraBold|Black)\.ttf$' <\"\$1\") -eq 6 &&
		grep -q 'SIL OPEN FONT LICENSE Version 1.1' '$extract/usr/share/doc/boswas-branding/copyright' &&
		grep -q 'Reserved Font Name: \"Orbitron\"' '$extract/usr/share/doc/boswas-branding/copyright'" _ "$listing_file"
# The boot screen (boswas-bootsplash): the daemon and its client, the
# initramfs integration, and no file named plymouth on the root file system.
splash_deb="$debs/boswas-bootsplash_${BOSWAS_VERSION_ID}_amd64.deb"
splash_listing="$(dpkg-deb -c "$splash_deb")"
check "boot screen: daemon, client, initramfs hook and scripts are shipped executable" \
	bash -c "for f in usr/libexec/boswas/boswas-bootsplashd usr/bin/boswas-bootsplash \
			usr/share/initramfs-tools/hooks/boswas-bootsplash \
			usr/share/initramfs-tools/scripts/init-premount/boswas-bootsplash \
			usr/share/initramfs-tools/scripts/init-bottom/boswas-bootsplash; do
			grep -qE \"^-rwxr-xr-x root/root .* \\./\$f\$\" <<<\"\$1\" || exit 1; done" _ "$splash_listing"
check "boot screen: conflicts with plymouth and installs nothing under that name" \
	bash -c "dpkg-deb -f '$splash_deb' Conflicts | grep -qw plymouth && ! grep -qE '/plymouth[^/]*\$' <<<\"\$1\"" _ "$splash_listing"
check "boot screen: hand-over to the login screen, and shutdown, restart and halt screens (systemd units)" \
	bash -c "for u in quit poweroff reboot halt; do
			grep -q \"\\./usr/lib/systemd/system/boswas-bootsplash-\$u.service\$\" <<<\"\$1\" || exit 1; done" _ "$splash_listing"
check "boot screen: the daemon reports its version and checks its own sandbox" \
	bash -c "x=\$(mktemp -d) && dpkg-deb -x '$splash_deb' \"\$x\" &&
		\"\$x/usr/bin/boswas-bootsplash\" --version | grep -q '^boswas-bootsplash ${BOSWAS_VERSION_ID}\$'; rc=\$?; rm -rf \"\$x\"; exit \$rc"
check_not "branding no longer depends on Plymouth or ships a Plymouth theme" \
	bash -c "dpkg-deb -f '$debs/boswas-branding_${BOSWAS_VERSION_ID}_all.deb' Depends | grep -q plymouth ||
		test -e '$extract/usr/share/plymouth/themes/boswas'"

# The native desktop (boswas-desktop): compositor, shell, session, login.
desktop_deb="$debs/boswas-desktop_${BOSWAS_VERSION_ID}_amd64.deb"
desktop_listing="$(dpkg-deb -c "$desktop_deb")"
check "desktop: compositor, shell, session and login screen programs are shipped executable" \
	bash -c "for f in usr/bin/boswas-compositor usr/bin/boswas-session usr/libexec/boswas/boswas-shell \
			usr/libexec/boswas/boswas-greeter usr/libexec/boswas/boswas-session-ready; do
			grep -qE \"^-rwxr-xr-x root/root .* \\./\$f\$\" <<<\"\$1\" || exit 1; done" _ "$desktop_listing"
check "desktop: the programs run and report the release version" \
	bash -c "'$extract/usr/bin/boswas-compositor' --version | grep -q '^boswas-compositor ' &&
		'$extract/usr/libexec/boswas/boswas-shell' --version | grep -q '^boswas-shell ' &&
		grep -qx '${BOSWAS_VERSION_ID}' '$extract/usr/share/boswas/compositor/version'"
check "desktop: session entry, session target, lock-screen PAM service and compositor defaults" \
	bash -c "grep -qx 'Exec=/usr/bin/boswas-session' '$extract/usr/share/wayland-sessions/boswas.desktop' &&
		grep -qx 'BindsTo=graphical-session.target' '$extract/usr/lib/systemd/user/boswas-session.target' &&
		grep -qx '@include common-auth' '$extract/etc/pam.d/boswas-lock' &&
		test -x '$extract/usr/lib/systemd/system-sleep/boswas-lock' &&
		test -s '$extract/usr/share/boswas/compositor/compositor.toml' && test -s '$extract/usr/share/boswas/compositor/greeter.toml'"
check "desktop: greetd is configured by a drop-in and /etc/boswas/greetd.toml (a conffile); greetd's own file is not shipped" \
	bash -c "grep -qx 'ExecStart=/usr/sbin/greetd --config /etc/boswas/greetd.toml' '$extract/usr/lib/systemd/system/greetd.service.d/10-boswas.conf' &&
		grep -q '\\./etc/boswas/greetd.toml\$' <<<\"\$1\" && ! grep -q '\\./etc/greetd/' <<<\"\$1\" &&
		! grep -q initial_session '$extract/etc/boswas/greetd.toml'" _ "$desktop_listing"
check "desktop: the Settings bridge is shipped, the shell is the polkit agent, and screen sharing goes to the wlroots portal" \
	bash -c "grep -qE '^-rwxr-xr-x root/root .* \\./usr/libexec/boswas/boswas-services-bridge\$' <<<\"\$1\" &&
		'$extract/usr/libexec/boswas/boswas-services-bridge' --version | grep -q '^boswas-services-bridge ' &&
		! grep -q polkit '$extract/usr/lib/systemd/user/boswas-session.target' &&
		grep -qx 'org.freedesktop.impl.portal.ScreenCast=wlr' '$extract/usr/share/xdg-desktop-portal/boswas-portals.conf' &&
		grep -qx 'chooser_type=none' '$extract/etc/xdg/xdg-desktop-portal-wlr/Boswas'" _ "$desktop_listing"
check "desktop: depends on greetd, a session bus and the Mesa drivers; no Plasma, KWin or SDDM anywhere in its relations" \
	bash -c "d=\$(dpkg-deb -f '$desktop_deb' Depends Recommends Suggests);
		for p in greetd dbus-user-session libegl1 libgl1-mesa-dri libseat1 libinput10; do grep -q \"\$p\" <<<\"\$d\" || exit 1; done;
		! grep -qE 'plasma-workspace|plasma-desktop|kwin|sddm' <<<\"\$d\""
check "desktop: KDE application defaults select the Prism colours and the Boswas Prism icons" \
	bash -c "d='$extract/usr/share/boswas/kde-settings'; grep -qx 'Theme=Boswas-Prism' \$d/kdeglobals &&
		grep -qx 'ColorScheme=BoswasPrism' \$d/kdeglobals && ! grep -q LookAndFeelPackage \$d/kdeglobals &&
		grep -qx 'DefaultProfile=Boswas Prism.profile' \$d/konsolerc &&
		grep -qx 'ColorScheme=BoswasPrism' '$extract/usr/share/konsole/Boswas Prism.profile' &&
		test -s '$extract/usr/share/konsole/BoswasPrism.colorscheme' &&
		test -s '$extract/usr/share/color-schemes/BoswasPrism.colors' && test -s '$extract/usr/share/boswas/ui/tokens.json'"
check "icons: Boswas Prism and Boswas Prism Light are installed with the launcher, lock and panel status icons" \
	bash -c "for t in Boswas-Prism Boswas-Prism-Light; do
			for f in index.theme scalable/apps/boswas-logo.svg scalable/apps/boswas-control-center.svg scalable/places/folder.svg \
				scalable/status/network-wired-symbolic.svg scalable/status/battery-full-symbolic.svg \
				scalable/actions/system-search-symbolic.svg scalable/actions/system-shutdown-symbolic.svg; do
				test -s '$extract/usr/share/icons/'\$t/\$f || exit 1; done; done"
check_not "desktop: no Plasma shell, KSplash, SDDM or Plasma session file is shipped by a Boswas package" \
	bash -c "test -e '$extract/usr/share/plasma/plasmoids' || test -e '$extract/usr/share/plasma/look-and-feel/com.boswas.splash' ||
		test -e '$extract/etc/sddm.conf.d' || test -e '$extract/usr/share/sddm' || test -e '$extract/etc/xdg/plasma-workspace'"
# The system applications (boswas-apps) and the toolkit they are built on.
apps_deb="$debs/boswas-apps_${BOSWAS_VERSION_ID}_all.deb"
apps_listing="$(dpkg-deb -c "$apps_deb")"
check "apps: every application has its launcher (0755), desktop entry, manual page and module" \
	bash -c "for a in files:Files:files terminal:Terminal:terminal text-editor:TextEditor:text_editor \
			calculator:Calculator:calculator task-manager:TaskManager:task_manager photos:Photos:photos \
			media-player:MediaPlayer:media_player camera:Camera:camera document-viewer:DocumentViewer:document_viewer \
			archive-manager:ArchiveManager:archive_manager clock:Clock:clock calendar:Calendar:calendar; do
			IFS=: read -r bin id mod <<<\"\$a\"
			grep -qE \"^-rwxr-xr-x root/root .* \\./usr/bin/boswas-\$bin\$\" <<<\"\$1\" || { echo \"launcher \$bin\"; exit 1; }
			grep -qE \"^Exec=boswas-\$bin( |\$)\" '$extract/usr/share/applications/com.boswas.'\$id.desktop || { echo \"entry \$id\"; exit 1; }
			test -s '$extract/usr/share/man/man1/boswas-'\$bin.1.gz || { echo \"man \$bin\"; exit 1; }
			test -s '$extract/usr/lib/boswas/python/boswas_apps/'\$mod/__init__.py || { echo \"module \$mod\"; exit 1; }
		done" _ "$apps_listing"
check "apps: desktop entries name icons of the Boswas Prism pack" \
	bash -c "for f in '$extract'/usr/share/applications/com.boswas.*.desktop; do
			icon=\$(sed -n 's/^Icon=//p' \"\$f\" | head -1); [ -n \"\$icon\" ] || continue
			test -s '$extract/usr/share/icons/Boswas-Prism/scalable/apps/'\$icon.svg || { echo \"\$f: \$icon\"; exit 1; }
		done"
check "apps: the toolkit is installed by boswas-branding next to the applications' modules" \
	bash -c "test -s '$extract/usr/lib/boswas/python/boswas_fluent/__init__.py' &&
		test -s '$extract/usr/lib/boswas/python/boswas_fluent/widgets/navigation.py' &&
		dpkg-deb -f '$apps_deb' Depends | grep -q 'boswas-branding'"
check "apps: Boswas Terminal is an x-terminal-emulator alternative; no KDE application in the relations" \
	bash -c "dpkg-deb -f '$apps_deb' Provides | grep -qx 'x-terminal-emulator' &&
		c=\$(mktemp -d) && dpkg-deb -e '$apps_deb' \"\$c\" && grep -q 'update-alternatives --install /usr/bin/x-terminal-emulator' \"\$c/postinst\" &&
		grep -q 'update-alternatives --remove x-terminal-emulator' \"\$c/prerm\" && rm -rf \"\$c\" &&
		! dpkg-deb -f '$apps_deb' Depends Recommends Suggests | grep -qE 'konsole|dolphin|kwrite|okular|gwenview|kcalc|(^|[ ,])ark([ ,]|\$)'"
check_not "apps: no world-writable or setuid file" grep -qE '^-.......w|^-..[sS]|^-.....[sS]' <<<"$apps_listing"
check "terminal: Boswas prompt and welcome (sources the user's ~/.bashrc first)" \
	bash -c "grep -q '\\. \"\$HOME/.bashrc\"' '$extract/usr/share/boswas/terminal/bashrc' && test -x '$extract/usr/bin/boswas-welcome'"
check_not "branding: the retired teal accent and Boswas Group gear logo are gone" \
	bash -c "grep -rqiE '#17C6C0|#2ED3CD|23,198,192' '$extract/usr/share/boswas' '$extract/usr/share/color-schemes' '$extract/usr/share/icons' ||
		test -e '$extract/usr/share/boswas/branding/boswas-group-logo.svg'"
rm -rf "$extract"
rm -f "$listing_file"

# CLI unit tests (also run during the package build; repeated for a direct report)
if out="$(cd "$BOSWAS_REPO_ROOT/packages/boswas-cli" && python3 -B -m unittest discover -s tests 2>&1)"; then
	pass "boswas CLI unit tests ($(grep -oE '^Ran [0-9]+' <<<"$out" | grep -oE '[0-9]+') tests)"
else
	fail "boswas CLI unit tests"
fi

finish
