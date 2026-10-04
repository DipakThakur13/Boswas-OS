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

for p in boswas-os boswas-cli boswas-branding boswas-security boswas-compat; do
	check "${p}_${BOSWAS_VERSION_ID} .deb produced" test -s "$debs/${p}_${BOSWAS_VERSION_ID}_all.deb"
done

# Lintian: errors and warnings fail the suite.
if lintian_out="$(lintian --fail-on error,warning "$debs"/*.deb 2>&1)"; then
	pass "lintian: no errors or warnings"
else
	fail "lintian: $(printf '%s' "$lintian_out" | grep -E '^[EW]:' | tr '\n' ';')"
fi

# Permissions that matter for security
listing="$(for d in "$debs"/*.deb; do dpkg-deb -c "$d"; done)"
check "sudoers drop-in is 0440 root:root" grep -qE '^-r--r----- root/root .* \./etc/sudoers\.d/boswas$' <<<"$listing"
check "CLI entry points are executable" grep -qE '^-rwxr-xr-x root/root .* \./usr/bin/boswas$' <<<"$listing"
check_not "no world-writable files" grep -qE '^-.......w' <<<"$listing"
check_not "no setuid/setgid files" grep -qE '^-..[sS]|^-.....[sS]' <<<"$listing"

# Dependencies wire the components together
check "boswas-os depends on all Boswas components" \
	bash -c "dpkg-deb -f '$debs/boswas-os_${BOSWAS_VERSION_ID}_all.deb' Depends | grep -q boswas-branding && dpkg-deb -f '$debs/boswas-os_${BOSWAS_VERSION_ID}_all.deb' Depends | grep -q boswas-security && dpkg-deb -f '$debs/boswas-os_${BOSWAS_VERSION_ID}_all.deb' Depends | grep -q boswas-cli && dpkg-deb -f '$debs/boswas-os_${BOSWAS_VERSION_ID}_all.deb' Depends | grep -q boswas-compat"

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

# Package contents never carry key material
extract="$(mktemp -d)"
for d in "$debs"/*.deb; do dpkg-deb -x "$d" "$extract"; done
check_not "no private keys in packages" grep -rqE -- '-----BEGIN ([A-Z]+ )?PRIVATE KEY-----' "$extract"
check "KDE icon is self-contained (no external <image> references)" \
	bash -c "! grep -q '<image' '$extract/usr/share/icons/hicolor/scalable/apps/boswas-logo.svg'"
rm -rf "$extract"

# CLI unit tests (also run during the package build; repeated for a direct report)
if (cd "$BOSWAS_REPO_ROOT/packages/boswas-cli" && python3 -B -m unittest discover -s tests >/dev/null 2>&1); then
	pass "boswas CLI unit tests"
else
	fail "boswas CLI unit tests"
fi

finish
