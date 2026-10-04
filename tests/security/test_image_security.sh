#!/usr/bin/env bash
# Security baseline as shipped in the image.
set -uo pipefail
. "$(dirname "$0")/../lib.sh"

root="${TESTWORK:?}/rootfs"
[ -d "$root/usr" ] || { skip "image security (root filesystem not extracted)"; finish; }

# AppArmor
check "AppArmor package installed" pkg_installed "$root" apparmor
kconfig="$(ls "$root"/boot/config-* 2>/dev/null | sort -V | tail -1)"
check "kernel built with AppArmor as an active LSM" bash -c "grep -q '^CONFIG_SECURITY_APPARMOR=y' '$kconfig' && grep -E '^CONFIG_LSM=' '$kconfig' | grep -q apparmor"
check "AppArmor profiles shipped" bash -c "ls '$root'/etc/apparmor.d/ | grep -q ."

# Firewall
fw="$root/etc/boswas/firewall/nftables.conf"
check "firewall ruleset present" test -s "$fw"
contains "firewall: inbound default drop" "$fw" "hook input priority filter; policy drop;"
contains "firewall: forward default drop" "$fw" "hook forward priority filter; policy drop;"
contains "nftables.service loads the Boswas ruleset" "$root/usr/lib/systemd/system/nftables.service.d/boswas.conf" "/etc/boswas/firewall/nftables.conf"
nft_out="$(chroot "$root" nft -c -f /etc/boswas/firewall/nftables.conf 2>&1)"
if [ $? -eq 0 ]; then
	pass "firewall ruleset validates with the image's nft"
elif grep -qiE 'netlink|permission|operation not permitted|protocol not supported' <<<"$nft_out"; then
	skip "firewall ruleset validation (no netfilter access in this environment)"
else
	fail "firewall ruleset validation: $nft_out"
fi

# Audit
rules="$root/etc/audit/rules.d/50-boswas.rules"
check "audit rules present (0640)" bash -c "[ \"\$(stat -c %a '$rules')\" = 640 ]"
if chroot "$root" augenrules >/dev/null 2>&1 || [ -s "$root/etc/audit/audit.rules" ]; then
	contains "augenrules compiles the Boswas audit rules" "$root/etc/audit/audit.rules" "boswas-identity"
else
	fail "augenrules compiles the audit rules"
fi
check_not "audit rules do not log user command lines (execve)" grep -q 'execve' "$rules"

# Privilege and authentication
check "sudoers configuration validates" chroot "$root" visudo -c -q
check "sudoers drop-in mode 0440" bash -c "[ \"\$(stat -c %a '$root/etc/sudoers.d/boswas')\" = 440 ]"
check_not "no NOPASSWD rules baked into the image" grep -rqsE '^[^#]*NOPASSWD' "$root/etc/sudoers" "$root/etc/sudoers.d"
check "password quality policy present" test -s "$root/etc/security/pwquality.conf.d/50-boswas.conf"
check "pam_pwquality enabled in PAM" grep -q 'pam_pwquality' "$root/etc/pam.d/common-password"

# Service enable policy
preset="$root/usr/lib/systemd/system-preset/80-boswas.preset"
contains "service preset enables the firewall" "$preset" "enable nftables.service"
contains "service preset keeps SSH disabled unless provisioned" "$preset" "disable ssh.service"

# Remote access
check_not "SSH server not installed" test -e "$root/usr/sbin/sshd"
contains "SSH policy forbids root login (if provisioned)" "$root/etc/ssh/sshd_config.d/50-boswas.conf" "PermitRootLogin no"

# Kernel hardening, without breaking sandboxes
contains "kernel hardening sysctls installed" "$root/usr/lib/sysctl.d/60-boswas-hardening.conf" "kernel.kptr_restrict = 2"
check_not "user namespaces not disabled (Flatpak/browser sandboxes)" \
	grep -rqsE '^\s*(user\.max_user_namespaces\s*=\s*0|kernel\.unprivileged_userns_clone\s*=\s*0)' "$root/etc/sysctl.d" "$root/usr/lib/sysctl.d"

# Logging
contains "journal retention bounded" "$root/usr/lib/systemd/journald.conf.d/50-boswas.conf" "MaxRetentionSec=90day"

# Updates and repository trust
uu="$(chroot "$root" apt-config dump 2>/dev/null)"
check "automatic security updates enabled" grep -q 'APT::Periodic::Unattended-Upgrade "1"' <<<"$uu"
origins="$(grep 'Unattended-Upgrade::Origins-Pattern::' <<<"$uu")"
if [ -n "$origins" ] && ! grep -v 'label=Debian-Security' <<<"$origins" | grep -q .; then
	pass "unattended-upgrades limited to Debian-Security"
else
	fail "unattended-upgrades origins: $(tr '\n' ' ' <<<"$origins")"
fi
# live-build adds one unsigned source for the boot medium's pool (live session
# only); anything else is a failure, and the installer must remove it.
unsigned="$(grep -rhsE '^[^#]*trusted=yes' "$root/etc/apt" | grep -v 'file:/run/live/medium' || true)"
[ -z "$unsigned" ] && pass "no unauthenticated APT sources besides the live boot medium" \
	|| fail "unauthenticated APT sources: $unsigned"
preseed_late="$(zcat "$TESTWORK/iso/install/gtk/initrd.gz" 2>/dev/null | cpio -i --quiet --to-stdout preseed.cfg 2>/dev/null | grep 'preseed/late_command' || true)"
check "installer removes the live-medium source from installed systems" grep -q 'file:/run/live/medium' <<<"$preseed_late"
check "Debian archive keyring present" bash -c "ls '$root'/usr/share/keyrings/debian-archive-keyring.* >/dev/null"

# Screen lock policy
kscr="$root/usr/share/boswas/kde-settings/kscreenlockerrc"
contains "screen lock: automatic lock enforced" "$kscr" 'Autolock[$i]=true'
contains "screen lock: lock on resume enforced" "$kscr" 'LockOnResume[$i]=true'

# Secrets
check_not "no private keys in Boswas paths" grep -rqsE -- '-----BEGIN ([A-Z]+ )?PRIVATE KEY-----' "$root/etc/boswas" "$root/usr/lib/boswas" "$root/usr/share/boswas"
check_not "device.conf contains no credentials" grep -qiE '(token|secret|password|key)\s*=' "$root/etc/boswas/device.conf"

finish
