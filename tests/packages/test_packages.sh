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
	boswas-control-center; do
	check "${p}_${BOSWAS_VERSION_ID} .deb produced" test -s "$debs/${p}_${BOSWAS_VERSION_ID}_all.deb"
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
check "sudoers drop-in is 0440 root:root" grep -qE '^-r--r----- root/root .* \./etc/sudoers\.d/boswas$' <<<"$listing"
check "CLI entry points are executable" grep -qE '^-rwxr-xr-x root/root .* \./usr/bin/boswas$' <<<"$listing"
check_not "no world-writable files" grep -qE '^-.......w' <<<"$listing"
check_not "no setuid/setgid files" grep -qE '^-..[sS]|^-.....[sS]' <<<"$listing"

# Dependencies wire the components together
os_depends="$(dpkg-deb -f "$debs/boswas-os_${BOSWAS_VERSION_ID}_all.deb" Depends 2>/dev/null)"
check "boswas-os depends on all Boswas device components" bash -c '
	for p in boswas-branding boswas-security boswas-cli "boswas-compat " boswas-device-agent boswas-compat-manager \
		boswas-control-center; do
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
	bash -c "grep -qE '^-rwxr-xr-x root/root .* \./usr/bin/boswas-winapp$' <<<\"\$1\" && grep -qE '^-rwxr-xr-x root/root .* \./usr/lib/boswas/compat/winapp-exec$' <<<\"\$1\"" _ "$listing"
check "Wine AppArmor profile and WinCompat policy shipped (0644 conffiles)" \
	bash -c "grep -qE '^-rw-r--r-- root/root .* \./etc/apparmor.d/boswas-winapp$' <<<\"\$1\" && grep -qE '^-rw-r--r-- root/root .* \./etc/boswas/compat/policy.conf$' <<<\"\$1\"" _ "$listing"
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
	bash -c "for f in usr/bin/boswas-device usr/lib/boswas/agent/boswas-device-agent usr/lib/boswas/agent/boswas-session-agent usr/lib/boswas/agent/agent-update; do grep -qE \"^-rwxr-xr-x root/root .* \\./\$f\$\" <<<\"\$1\" || exit 1; done" _ "$listing"
check "device agent: system unit, update template and user unit shipped" \
	bash -c "grep -q '\./usr/lib/systemd/system/boswas-device-agent.service$' <<<\"\$1\" && grep -q '\./usr/lib/systemd/system/boswas-agent-update@.service$' <<<\"\$1\" && grep -q '\./usr/lib/systemd/user/boswas-session-agent.service$' <<<\"\$1\"" _ "$listing"
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
	bash -c "grep -qE '^-rwxr-xr-x root/root .* \./usr/bin/boswas-compat-manager$' <<<\"\$1\" && grep -q '\./usr/share/applications/com.boswas.CompatibilityManager.desktop$' <<<\"\$1\" && grep -q '\./usr/share/kio/servicemenus/boswas-compat-manager-install.desktop$' <<<\"\$1\"" _ "$listing"
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
	bash -c "grep -q '\./usr/share/boswas-control-plane/dashboard/app.js$' <<<\"\$1\" && grep -q '\./usr/lib/boswas-control-plane/python/boswas_agent/commands.py$' <<<\"\$1\" && ! grep -q '\./usr/lib/boswas/python/' <<<\"\$(dpkg-deb -c '$server_deb')\"" _ "$listing"
rm -rf "$ctl"

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
	bash -c "test \$(grep -cE '^-rw-r--r-- root/root .* \./usr/share/fonts/truetype/orbitron/Orbitron-(Regular|Medium|SemiBold|Bold|ExtraBold|Black)\.ttf$' <<<\"\$1\") -eq 6 &&
		grep -q 'SIL OPEN FONT LICENSE Version 1.1' '$extract/usr/share/doc/boswas-branding/copyright' &&
		grep -q 'Reserved Font Name: \"Orbitron\"' '$extract/usr/share/doc/boswas-branding/copyright'" _ "$listing"
check "boot splash: Boswas OS Plymouth theme (two-step, passphrase dialog images, 24-frame spinner)" \
	bash -c "grep -q 'ModuleName=two-step' '$extract/usr/share/plymouth/themes/boswas/boswas.plymouth' &&
		for f in watermark lock entry bullet capslock throbber-0024; do test -s '$extract/usr/share/plymouth/themes/boswas/'\$f.png || exit 1; done"
ctl="$(mktemp -d)"
dpkg-deb -e "$debs/boswas-branding_${BOSWAS_VERSION_ID}_all.deb" "$ctl"
check "boot splash: boswas-branding selects the theme and rebuilds the initramfs (trigger)" \
	bash -c "grep -q 'plymouth-set-default-theme boswas' '$ctl/postinst' && grep -qx 'activate-noawait update-initramfs' '$ctl/triggers'"
rm -rf "$ctl"
check "desktop: Boswas Launcher (Kickoff under the Boswas name) and the Boswas session splash" \
	bash -c "grep -q '\"X-Plasma-RootPath\": \"org.kde.plasma.kickoff\"' '$extract/usr/share/plasma/plasmoids/com.boswas.launcher/metadata.json' &&
		grep -q 'org.kde.plasma.launchermenu' '$extract/usr/share/plasma/plasmoids/com.boswas.launcher/metadata.json' &&
		test -s '$extract/usr/share/plasma/look-and-feel/com.boswas.splash/contents/splash/Splash.qml' &&
		! grep -q '<image' '$extract/usr/share/plasma/look-and-feel/com.boswas.splash/contents/splash/images/lockup.svg'"
check "desktop: KDE defaults select the Horizon preset, Boswas icons, splash and Konsole profile" \
	bash -c "d='$extract/usr/share/boswas/kde-settings'; grep -qx 'LookAndFeelPackage=com.boswas.horizon' \$d/kdeglobals &&
		grep -qx 'Theme=Boswas' \$d/kdeglobals && grep -qx 'Theme=com.boswas.splash' \$d/ksplashrc &&
		grep -qx 'DefaultProfile=Boswas Horizon.profile' \$d/konsolerc"
check "terminal: Boswas prompt and welcome (sources the user's ~/.bashrc first)" \
	bash -c "grep -q '\\. \"\$HOME/.bashrc\"' '$extract/usr/share/boswas/terminal/bashrc' && test -x '$extract/usr/bin/boswas-welcome'"
check "login screen: Breeze with the Boswas background and logo (self-contained)" \
	bash -c "grep -qx 'showlogo=shown' '$extract/usr/share/sddm/themes/breeze/theme.conf.user' &&
		test -s '$extract/usr/share/boswas/branding/boswas-login-logo.svg' &&
		! grep -q '<image' '$extract/usr/share/boswas/branding/boswas-login-logo.svg'"
check_not "branding: the retired teal accent and Boswas Group gear logo are gone" \
	bash -c "grep -rqiE '#17C6C0|#2ED3CD|23,198,192' '$extract/usr/share/boswas' '$extract/usr/share/color-schemes' '$extract/usr/share/icons' ||
		test -e '$extract/usr/share/boswas/branding/boswas-group-logo.svg'"
rm -rf "$extract"

# CLI unit tests (also run during the package build; repeated for a direct report)
if out="$(cd "$BOSWAS_REPO_ROOT/packages/boswas-cli" && python3 -B -m unittest discover -s tests 2>&1)"; then
	pass "boswas CLI unit tests ($(grep -oE '^Ran [0-9]+' <<<"$out" | grep -oE '[0-9]+') tests)"
else
	fail "boswas CLI unit tests"
fi

finish
