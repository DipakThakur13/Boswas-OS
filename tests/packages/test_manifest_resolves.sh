#!/usr/bin/env bash
# The Debian 13 package manifest resolves (apt simulation against the live
# Debian archive) with the Boswas exclusion pins applied. Needs network.
set -uo pipefail
. "$(dirname "$0")/../lib.sh"

if ! getent hosts deb.debian.org >/dev/null 2>&1; then
	skip "package manifest resolution (no network)"
	finish
fi

root="$(mktemp -d)"
mkdir -p "$root/etc/apt/preferences.d" "$root/etc/apt/sources.list.d" "$root/var/lib/apt/lists/partial" \
	"$root/var/cache/apt/archives/partial" "$root/var/lib/dpkg"
touch "$root/var/lib/dpkg/status"
keyring=/usr/share/keyrings/debian-archive-keyring.pgp
[ -e "$keyring" ] || keyring=/usr/share/keyrings/debian-archive-keyring.gpg
cat > "$root/etc/apt/sources.list.d/debian.sources" <<EOF
Types: deb
URIs: http://deb.debian.org/debian
Suites: trixie trixie-updates
Components: main non-free-firmware
Signed-By: $keyring

Types: deb
URIs: http://deb.debian.org/debian-security
Suites: trixie-security
Components: main non-free-firmware
Signed-By: $keyring
EOF
cp "$BOSWAS_REPO_ROOT/config/live-build/config/archives/boswas-exclude.pref.chroot" \
	"$root/etc/apt/preferences.d/boswas-exclude.pref"
apt_opts=(-o "Dir=$root" -o "Dir::State::status=$root/var/lib/dpkg/status" -o APT::Architecture=amd64 -o Debug::NoLocking=1)

if ! apt-get "${apt_opts[@]}" update -qq >/dev/null 2>&1; then
	skip "package manifest resolution (Debian archive unreachable)"
	rm -rf "$root"
	finish
fi

pkgs="$(manifest_packages | grep -v '^boswas-' | tr '\n' ' ')"
# shellcheck disable=SC2086
if out="$(apt-get "${apt_opts[@]}" install -s -qq linux-image-amd64 live-boot live-config live-config-systemd $pkgs 2>&1)"; then
	pass "manifest resolves on Debian 13 ($(grep -c '^Inst ' <<<"$out") packages incl. dependencies)"
else
	fail "manifest does not resolve: $(grep -E '^E:|Depends:' <<<"$out" | head -5 | tr '\n' ';')"
fi
for p in kdeconnect plasma-discover packagekit libpam-fprintd openssh-server cups-browsed; do
	check_not "excluded package '$p' is not pulled in" grep -q "^Inst $p " <<<"$out"
done
rm -rf "$root"
finish
