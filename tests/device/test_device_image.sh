#!/usr/bin/env bash
# Device management as shipped in the image: device agent, session agent,
# Compatibility Manager; no Control Plane; 64-bit only; no identity baked in.
set -uo pipefail
. "$(dirname "$0")/../lib.sh"

root="${TESTWORK:?}/rootfs"
[ -d "$root/usr" ] || { skip "device management image checks (root filesystem not extracted)"; finish; }

check "boswas-device-agent installed" pkg_installed "$root" boswas-device-agent
check "boswas-compat-manager installed" pkg_installed "$root" boswas-compat-manager
check_not "the Control Plane is not part of the device image" pkg_installed "$root" boswas-control-plane
check_not "no 32-bit Wine (wine32) in the image" pkg_installed "$root" wine32
check "no foreign dpkg architectures (no i386 multiarch)" \
	bash -c "[ -z \"\$(chroot '$root' dpkg --print-foreign-architectures)\" ]"

check "boswas-device-agent.service enabled" \
	bash -c "[ \"\$(chroot '$root' systemctl is-enabled boswas-device-agent.service 2>/dev/null)\" = enabled ]"
check "boswas-session-agent.service enabled for every user's graphical session" \
	test -L "$root/etc/systemd/user/graphical-session.target.wants/boswas-session-agent.service"
unit="$root/usr/lib/systemd/system/boswas-device-agent.service"
check "agent unit is sandboxed (ProtectSystem=strict, one capability, no new privileges)" \
	bash -c "grep -qx 'ProtectSystem=strict' '$unit' && grep -qx 'CapabilityBoundingSet=CAP_DAC_READ_SEARCH' '$unit' && grep -qx 'NoNewPrivileges=yes' '$unit'"
if out="$(chroot "$root" systemd-analyze verify /usr/lib/systemd/system/boswas-device-agent.service 2>&1)"; then
	pass "systemd-analyze verify accepts the agent unit"
elif grep -qiE 'failed to (connect|create)|no such file or directory.*(bus|runtime)|Transport endpoint' <<<"$out"; then
	skip "systemd-analyze verify (no systemd runtime in this environment)"
else
	fail "systemd-analyze verify: $(head -3 <<<"$out" | tr '\n' ' ')"
fi

# Every device gets its own identity at first start: nothing may be baked in.
check_not "no device identity baked into the image" test -e "$root/var/lib/boswas/agent/identity.json"
check_not "no agent credentials baked into the image" test -e "$root/var/lib/boswas/agent/credentials"
check "shipped device.conf is a standalone configuration (no Control Plane)" \
	grep -qx 'CONTROL_PLANE_URL=""' "$root/etc/boswas/device.conf"
out="$(chroot "$root" boswas-device --json config validate 2>&1)"
check "boswas-device validates the shipped device.conf in the image" python3 -c '
import json, sys
assert json.loads(sys.argv[1])["config"]["valid"] is True' "$out"
out="$(chroot "$root" boswas-device status 2>&1)"
[ $? -eq 5 ] && grep -q "never run" <<<"$out" && pass "boswas-device reports a never-started agent (exit 5) in the image" \
	|| fail "boswas-device status in the image: $out"

check "Compatibility Manager desktop entry is valid" \
	chroot "$root" desktop-file-validate /usr/share/applications/com.boswas.CompatibilityManager.desktop
check "Compatibility Manager icon installed" test -s "$root/usr/share/icons/hicolor/scalable/apps/boswas-compat-manager.svg"
check "Compatibility Manager imports with the image's PySide6" \
	chroot "$root" python3 -IB -c 'import sys; sys.path.insert(0, "/usr/lib/boswas/python"); import boswas_manager.app'
out="$(chroot "$root" /usr/bin/boswas-compat-manager --version 2>&1)"
check "boswas-compat-manager --version ($out)" grep -q "1.0~alpha3" <<<"$out"
check "\"Run with Boswas\" stays the default handler for Windows executables" \
	grep -qE '^application/x-msdownload=boswas-winapp-install.desktop' "$root/usr/share/applications/mimeinfo.cache"
finish
