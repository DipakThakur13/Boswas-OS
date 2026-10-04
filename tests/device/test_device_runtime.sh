#!/usr/bin/env bash
# Device management end to end in the built image: the device agent service,
# a user's session agent, the Compatibility Manager's backend operations
# with real Wine 10 and bubblewrap, enrollment, signed policies and remote
# typed commands against a Control Plane started from the repository sources.
#
# Like the WinCompat runtime test, the image root is used through a throw-away
# overlay entered with pivot_root in a private mount namespace (bubblewrap
# needs user namespaces, which the kernel refuses inside a chroot). The
# scenario itself is tests/device/device_scenario.py.
set -uo pipefail
. "$(dirname "$0")/../lib.sh"
. "$BOSWAS_REPO_ROOT/build/scripts/lib.sh"

root="${TESTWORK:?}/rootfs"
fixtures="$TESTWORK/fixtures"
[ -d "$root/usr" ] || { skip "device management runtime (root filesystem not extracted)"; finish; }
[ -s "$fixtures/boswas-testapp.exe" ] || { skip "device management runtime (test fixtures not built)"; finish; }
[ "$(id -u)" -eq 0 ] || { skip "device management runtime (needs root for the mount namespace)"; finish; }

work="$TESTWORK/device"
cleanup() {
	umount -R "$work/merged" 2>/dev/null || umount -l "$work/merged" 2>/dev/null || true
	safe_rm_tree "$work" 2>/dev/null || rm -rf "$work"
}
cleanup
mkdir -p "$work/upper" "$work/work" "$work/merged"
trap cleanup EXIT
if ! mount -t overlay overlay -o "lowerdir=$root,upperdir=$work/upper,workdir=$work/work" "$work/merged"; then
	fail "device management runtime: overlay of the image root could not be mounted"
	finish
fi
m="$work/merged"
t="$m/opt/boswas-test"
mkdir -p "$t/control-plane"
cp -r "$fixtures" "$t/fixtures"
cp -r "$BOSWAS_REPO_ROOT/control-plane/boswas_cp" "$BOSWAS_REPO_ROOT/control-plane/dashboard" "$t/control-plane/"
cp "$BOSWAS_REPO_ROOT"/tests/device/*.py "$t/"
find "$t" -type d -exec chmod 0755 {} +
find "$t" -type f -exec chmod 0644 {} +

log="$BOSWAS_REPO_ROOT/build/logs/device-runtime.log"
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
	exec python3 -B /opt/boswas-test/device_scenario.py
' device-scenario "$m" > "$work/results.txt" 2> "$log"
rc=$?
# Service logs of the scenario (agent, session agent, Control Plane) for diagnosis.
cp -r "$m/root/device-logs" "$BOSWAS_REPO_ROOT/build/logs/device-runtime-services" 2>/dev/null || true

while IFS=$'\t' read -r status text; do
	case "$status" in
		PASS) pass "$text" ;;
		FAIL) fail "$text" ;;
		SKIP) skip "$text" ;;
		INFO) printf '  INFO  %s\n' "$text" ;;
	esac
done < "$work/results.txt"
if [ "$rc" -ne 0 ] && ! grep -q '^FAIL' "$work/results.txt"; then
	fail "device scenario exited with $rc (see build/logs/$(basename "$log"))"
fi
grep -q . "$work/results.txt" || fail "device scenario produced no results (see build/logs/$(basename "$log"))"
finish
