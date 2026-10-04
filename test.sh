#!/usr/bin/env bash
# Boswas OS test suite.
#
#   ./test.sh                  every stage the environment supports
#   ./test.sh --no-boot        skip the QEMU boot test
#   ./test.sh --boot-only      only the QEMU boot test
#   ./test.sh --packages-only  only static checks and the Boswas packages
#   ./test.sh --iso PATH       test a specific ISO
#                              (default: build/output/Boswas-OS-<tag>-amd64.iso)
#
# Like build.sh, runs inside the boswas-os-builder container unless the host
# is Debian 13 and the script runs as root.
#
# Stages: static, packages, manifest (network), artifacts, image extraction,
# ISO contents, image packages, security, compatibility, boot (QEMU).
# Report: build/logs/test-report-<timestamp>.txt; screenshots and serial logs
# of the boot test: build/logs/boot-test/.
set -Eeuo pipefail
# shellcheck source=build/scripts/lib.sh
. "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/build/scripts/lib.sh"
load_release

stages_mode="all" iso=""
args=("$@")
while [ $# -gt 0 ]; do
	case "$1" in
		--no-boot) stages_mode="no-boot" ;;
		--boot-only) stages_mode="boot-only" ;;
		--packages-only) stages_mode="packages-only" ;;
		--iso) iso="${2:?--iso needs a path}"; shift ;;
		-h|--help) sed -n '2,18p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
		*) die "unknown option: $1" ;;
	esac
	shift
done

# Re-run inside the builder container when this host cannot run the tests.
if [ -z "${BOSWAS_IN_CONTAINER:-}" ] && { ! is_supported_build_host || [ "$(id -u)" -ne 0 ]; }; then
	rt="$(container_runtime)"
	[ -n "$rt" ] || die "tests need Debian 13 + root, or docker/podman for the builder container"
	image="${BOSWAS_BUILDER_IMAGE:-boswas-os-builder:trixie}"
	"$rt" build -q -t "$image" -f "$BOSWAS_REPO_ROOT/build/container/Containerfile" "$BOSWAS_REPO_ROOT/build/container" >/dev/null
	kvm=()
	[ -e /dev/kvm ] && kvm=(--device /dev/kvm)
	export MSYS_NO_PATHCONV=1
	exec "$rt" run --rm --privileged "${kvm[@]}" \
		-v "$(host_repo_path):/src" \
		-v boswas-os-build:/var/cache/boswas \
		-e BOSWAS_IN_CONTAINER=1 -e NO_COLOR \
		-w /src "$image" ./test.sh "${args[@]}"
fi

[ -n "$iso" ] || iso="$BOSWAS_REPO_ROOT/build/output/${BOSWAS_IMAGE_BASENAME}.iso"
case "$iso" in /*) ;; *) iso="$BOSWAS_REPO_ROOT/$iso" ;; esac
export ISO="$iso"
if [ -n "${BOSWAS_IN_CONTAINER:-}" ]; then
	export TESTWORK="${BOSWAS_TEST_WORK:-/var/cache/boswas/test}"
else
	export TESTWORK="${BOSWAS_TEST_WORK:-/var/tmp/boswas-os-test}"
fi
mkdir -p "$TESTWORK" "$BOSWAS_REPO_ROOT/build/logs"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
export BOSWAS_TEST_REPORT="$BOSWAS_REPO_ROOT/build/logs/test-report-${stamp}.txt"
: > "$BOSWAS_TEST_REPORT"

run_stage() {
	local name="$1"; shift
	log "stage: $name"
	"$@" || true   # failures are recorded in the report; keep running other stages
}

t="$BOSWAS_REPO_ROOT/tests"
has_iso=false
[ -s "$ISO" ] && has_iso=true

if [ "$stages_mode" != "boot-only" ]; then
	run_stage "static checks" bash "$t/static/test_sources.sh"
	run_stage "Boswas packages" bash "$t/packages/test_packages.sh"
fi
if [ "$stages_mode" = "all" ] || [ "$stages_mode" = "no-boot" ]; then
	run_stage "package manifest resolves" bash "$t/packages/test_manifest_resolves.sh"
	if $has_iso; then
		run_stage "build artifacts" bash "$t/build/test_artifacts.sh"
		run_stage "image extraction" bash "$t/build/extract_image.sh"
		run_stage "ISO contents" bash "$t/build/test_iso_contents.sh"
		run_stage "image packages" bash "$t/packages/test_image_packages.sh"
		run_stage "security baseline" bash "$t/security/test_image_security.sh"
		run_stage "compatibility" bash "$t/compatibility/test_wine.sh"
	else
		printf 'SKIP\tbuild\timage tests (no ISO at %s; run ./build.sh)\n' "$ISO" >> "$BOSWAS_TEST_REPORT"
		warn "no ISO at $ISO - image tests skipped (run ./build.sh first)"
	fi
fi
if [ "$stages_mode" = "all" ] || [ "$stages_mode" = "boot-only" ]; then
	if $has_iso; then
		run_stage "QEMU boot test" python3 "$t/boot/qemu_boot_test.py" --iso "$ISO" --out "$BOSWAS_REPO_ROOT/build/logs/boot-test"
	else
		printf 'SKIP\tboot/qemu\tboot test (no ISO)\n' >> "$BOSWAS_TEST_REPORT"
	fi
fi

# Extracted image trees are large; drop them.
safe_rm_tree "$TESTWORK/iso" 2>/dev/null || true
safe_rm_tree "$TESTWORK/rootfs" 2>/dev/null || true

passed="$(grep -c '^PASS' "$BOSWAS_TEST_REPORT" || true)"
failed="$(grep -c '^FAIL' "$BOSWAS_TEST_REPORT" || true)"
skipped="$(grep -c '^SKIP' "$BOSWAS_TEST_REPORT" || true)"
echo
log "results: ${passed} passed, ${failed} failed, ${skipped} skipped"
log "report:  build/logs/$(basename "$BOSWAS_TEST_REPORT")"
if [ "$failed" -gt 0 ]; then
	grep '^FAIL' "$BOSWAS_TEST_REPORT" | cut -f2- | sed 's/^/  FAIL  /'
	exit 1
fi
ok "all executed tests passed"
