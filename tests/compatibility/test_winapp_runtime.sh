#!/usr/bin/env bash
# WinCompat end to end in the built image: boswas-winapp installs, isolates,
# launches, repairs and removes the Windows test application with the real
# Wine 10, bubblewrap and boswas-compat of the image.
#
# The image root is used through a throw-away overlay and entered with
# pivot_root in a private mount namespace (not chroot: the kernel refuses to
# create the user namespaces bubblewrap needs inside a chroot). The scenario
# itself is tests/compatibility/winapp_scenario.py.
#
# AppArmor enforcement needs an AppArmor kernel; under Docker Desktop (WSL2
# kernel) it is reported as SKIP here and verified by the QEMU boot test.
set -uo pipefail
. "$(dirname "$0")/../lib.sh"
. "$BOSWAS_REPO_ROOT/build/scripts/lib.sh"

root="${TESTWORK:?}/rootfs"
fixtures="$TESTWORK/fixtures"
[ -d "$root/usr" ] || { skip "WinCompat runtime (root filesystem not extracted)"; finish; }
[ -s "$fixtures/boswas-testapp.exe" ] || { skip "WinCompat runtime (test fixtures not built)"; finish; }
[ "$(id -u)" -eq 0 ] || { skip "WinCompat runtime (needs root for the mount namespace)"; finish; }

work="$TESTWORK/winapp"
cleanup() {
	umount -R "$work/merged" 2>/dev/null || umount -l "$work/merged" 2>/dev/null || true
	safe_rm_tree "$work" 2>/dev/null || rm -rf "$work"
}
cleanup
mkdir -p "$work/upper" "$work/work" "$work/merged"
trap cleanup EXIT
if ! mount -t overlay overlay -o "lowerdir=$root,upperdir=$work/upper,workdir=$work/work" "$work/merged"; then
	fail "WinCompat runtime: overlay of the image root could not be mounted"
	finish
fi
m="$work/merged"
mkdir -p "$m/root/boswas-test"
cp -r "$fixtures" "$m/root/boswas-test/fixtures"
cp "$BOSWAS_REPO_ROOT/tests/compatibility/winapp_scenario.py" "$m/root/boswas-test/"

log="$BOSWAS_REPO_ROOT/build/logs/winapp-runtime.log"
mkdir -p "$(dirname "$log")"
# shellcheck disable=SC2016
unshare --mount --propagation private bash -c '
	set -e
	m="$1"
	mount --bind "$m" "$m"
	mount --rbind /dev "$m/dev"
	mount --rbind /sys "$m/sys"
	cd "$m"
	mkdir -p .oldroot
	pivot_root . .oldroot
	cd /
	mount -t proc proc /proc
	umount -l /.oldroot
	rmdir /.oldroot
	mount -t tmpfs -o mode=1777 tmpfs /tmp
	mount -t tmpfs -o mode=0755 tmpfs /run
	exec python3 -B /root/boswas-test/winapp_scenario.py
' winapp-scenario "$m" > "$work/results.txt" 2> "$log"
rc=$?

while IFS=$'\t' read -r status text; do
	case "$status" in
		PASS) pass "$text" ;;
		FAIL) fail "$text" ;;
		SKIP) skip "$text" ;;
		INFO) printf '  INFO  %s\n' "$text" ;;
	esac
done < "$work/results.txt"
if [ "$rc" -ne 0 ] && ! grep -q '^FAIL' "$work/results.txt"; then
	fail "WinCompat scenario exited with $rc (see build/logs/$(basename "$log"))"
fi
grep -q . "$work/results.txt" || fail "WinCompat scenario produced no results (see build/logs/$(basename "$log"))"
finish
