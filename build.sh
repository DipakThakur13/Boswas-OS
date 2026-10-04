#!/usr/bin/env bash
# Build the Boswas OS live/installer ISO.
#
#   ./build.sh                 build (container mode automatically on non-Debian-13 hosts)
#   ./build.sh --container     always build inside the boswas-os-builder container
#   ./build.sh --native        build on this Debian 13 host (requires root)
#
# Options:
#   --keep-work      keep the live-build working tree after the build
#   --clean-cache    drop the live-build package/bootstrap cache first
#   -h, --help       show this help
#
# Environment:
#   BOSWAS_MIRROR / BOSWAS_MIRROR_SECURITY   Debian mirrors used during the build
#   BOSWAS_WORK_DIR, BOSWAS_CACHE_DIR        native-mode work and cache locations
#   BOSWAS_CONTAINER_RUNTIME                 docker | podman (auto-detected)
#   BOSWAS_BUILDER_IMAGE                     builder image tag (boswas-os-builder:trixie)
#
# Output (build/output/):
#   Boswas-OS-v1-alpha-amd64.iso, .sha256, .manifest.txt
#   boswas-control-plane_<version>_all.deb (server package, not in the ISO)
# Build records: build/manifest/   Logs: build/logs/
set -Eeuo pipefail
# shellcheck source=build/scripts/lib.sh
. "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/build/scripts/lib.sh"
load_release

mode="auto" keep_work=false clean_cache=false
for arg in "$@"; do
	case "$arg" in
		--container) mode="container" ;;
		--native) mode="native" ;;
		--keep-work) keep_work=true ;;
		--clean-cache) clean_cache=true ;;
		-h|--help) sed -n '2,22p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
		*) die "unknown option: $arg (see ./build.sh --help)" ;;
	esac
done

if [ "$mode" = "auto" ]; then
	if [ -n "${BOSWAS_IN_CONTAINER:-}" ] || is_supported_build_host; then
		mode="native"
	else
		mode="container"
	fi
fi

OUT="$BOSWAS_REPO_ROOT/build/output"
LOGS="$BOSWAS_REPO_ROOT/build/logs"
MANIFESTS="$BOSWAS_REPO_ROOT/build/manifest"
mkdir -p "$OUT" "$LOGS" "$MANIFESTS"

# --- container mode: re-run this script inside the Debian 13 builder ---------
if [ "$mode" = "container" ]; then
	rt="$(container_runtime)"
	[ -n "$rt" ] || die "this host is not Debian 13 and no docker/podman was found; see docs/development/building.md"
	image="${BOSWAS_BUILDER_IMAGE:-boswas-os-builder:trixie}"
	log "building builder image $image ($rt)"
	"$rt" build -q -t "$image" -f "$BOSWAS_REPO_ROOT/build/container/Containerfile" "$BOSWAS_REPO_ROOT/build/container" >/dev/null
	image_id="$("$rt" image inspect -f '{{.Id}}' "$image")"
	pass=()
	$keep_work && pass+=(--keep-work)
	$clean_cache && pass+=(--clean-cache)
	log "starting containerised build (privileged: live-build needs mounts inside its chroot)"
	# Keep Git Bash on Windows from rewriting container paths.
	export MSYS_NO_PATHCONV=1
	exec "$rt" run --rm --privileged \
		-v "$(host_repo_path):/src" \
		-v boswas-os-build:/var/cache/boswas \
		-e BOSWAS_IN_CONTAINER=1 \
		-e BOSWAS_BUILDER_IMAGE="$image ($image_id)" \
		-e BOSWAS_HOST_UID="$(id -u)" -e BOSWAS_HOST_GID="$(id -g)" \
		-e BOSWAS_MIRROR -e BOSWAS_MIRROR_SECURITY -e NO_COLOR \
		-w /src "$image" ./build.sh --native "${pass[@]}"
fi

# --- native mode ----------------------------------------------------------------
[ "$(id -u)" -eq 0 ] || die "native builds need root (live-build creates a chroot). Use sudo, or ./build.sh --container"
if ! is_supported_build_host && [ -z "${BOSWAS_ALLOW_UNSUPPORTED_HOST:-}" ]; then
	die "native builds are supported on Debian 13 (trixie) only; use ./build.sh --container (or set BOSWAS_ALLOW_UNSUPPORTED_HOST=1 at your own risk)"
fi
require_cmds lb debootstrap dpkg-buildpackage dh rsvg-convert python3 xorriso mksquashfs sha256sum rsync git

if [ -n "${BOSWAS_IN_CONTAINER:-}" ]; then
	WORK="${BOSWAS_WORK_DIR:-/var/cache/boswas/work}"
	CACHE="${BOSWAS_CACHE_DIR:-/var/cache/boswas/lb-cache}"
else
	WORK="${BOSWAS_WORK_DIR:-/var/tmp/boswas-os-build}"
	CACHE="${BOSWAS_CACHE_DIR:-$BOSWAS_REPO_ROOT/build/cache}"
fi
mkdir -p "$WORK" "$CACHE"

free_gb=$(( $(df -Pk "$WORK" | awk 'NR==2 {print $4}') / 1024 / 1024 ))
[ "$free_gb" -ge 25 ] || die "need at least 25 GB free in $WORK (have ${free_gb} GB)"

git_meta
if [ "$BOSWAS_GIT_COMMIT" != "none" ] && [ "$BOSWAS_GIT_DIRTY" = "false" ] && [ -n "$BOSWAS_GIT_COMMIT_EPOCH" ]; then
	SOURCE_DATE_EPOCH="$BOSWAS_GIT_COMMIT_EPOCH"   # reproducible: tied to the commit
else
	SOURCE_DATE_EPOCH="$(date +%s)"                 # uncommitted tree: wall clock
fi
export SOURCE_DATE_EPOCH
stamp="$(date -u -d "@$SOURCE_DATE_EPOCH" +%Y%m%dT%H%M%SZ)"
BUILD_ID="BOS-${BOSWAS_VERSION_ID}-${stamp}"
LOG="$LOGS/build-${stamp}.log"
exec > >(tee -a "$LOG") 2>&1

on_error() {
	local code=$?
	warn "build FAILED (exit $code). Full log: build/logs/$(basename "$LOG")"
	[ -f "$WORK/lb/build.log" ] && tail -n 30 "$WORK/lb/build.log" >&2 || true
	exit "$code"
}
trap on_error ERR

lb_version="$(dpkg-query -W -f='${Version}' live-build 2>/dev/null || lb --version)"
log "$BOSWAS_NAME $BOSWAS_VERSION - build $BUILD_ID"
log "base: $BOSWAS_BASE_NAME $BOSWAS_BASE_VERSION ($BOSWAS_BASE_CODENAME) $BOSWAS_ARCH, live-build $lb_version"
log "git: $BOSWAS_GIT_COMMIT (dirty: $BOSWAS_GIT_DIRTY); work dir: $WORK"

if $clean_cache; then
	log "dropping live-build cache $CACHE"
	rm -rf "${CACHE:?}"/*
fi

# 1. Boswas packages -----------------------------------------------------------------
rm -rf "$WORK/debs"
"$BOSWAS_REPO_ROOT/build/scripts/build-packages.sh" "$WORK/debs"

# 2. Image metadata ------------------------------------------------------------------
image_info="$WORK/image-info"
cat > "$image_info" <<EOF
# Boswas OS image build metadata (generated by build.sh)
BOSWAS_NAME="$BOSWAS_NAME"
BOSWAS_VERSION="$BOSWAS_VERSION"
BOSWAS_VERSION_ID="$BOSWAS_VERSION_ID"
BOSWAS_CHANNEL="$BOSWAS_CHANNEL"
BOSWAS_ARCH="$BOSWAS_ARCH"
BOSWAS_BUILD_ID="$BUILD_ID"
BOSWAS_BUILD_DATE="$(date -u -d "@$SOURCE_DATE_EPOCH" +%Y-%m-%dT%H:%M:%SZ)"
BOSWAS_SOURCE_DATE_EPOCH="$SOURCE_DATE_EPOCH"
BOSWAS_DEBIAN_SUITE="$BOSWAS_BASE_CODENAME"
BOSWAS_GIT_COMMIT="$BOSWAS_GIT_COMMIT"
BOSWAS_GIT_DIRTY="$BOSWAS_GIT_DIRTY"
BOSWAS_CONFIG_HASH="$(config_hash)"
BOSWAS_LIVE_BUILD_VERSION="$lb_version"
EOF

# 3. live-build tree -----------------------------------------------------------------
lbdir="$WORK/lb"
if [ -d "$lbdir/chroot" ]; then
	log "cleaning previous live-build state"
	(cd "$lbdir" && lb clean noauto >/dev/null 2>&1) || true
fi
"$BOSWAS_REPO_ROOT/build/scripts/prepare-live-config.sh" "$lbdir" "$WORK/debs" "$image_info"
ln -sfn "$CACHE" "$lbdir/cache"

# 4. live-build ---------------------------------------------------------------------------
export BOSWAS_BASE_CODENAME BOSWAS_ARCH BOSWAS_IMAGE_TAG
export BOSWAS_ISO_VOLUME="BOSWAS_OS_$(echo "$BOSWAS_IMAGE_TAG" | tr 'a-z-' 'A-Z_')"
log "lb config"
(cd "$lbdir" && lb config)
log "lb build (this takes a while: bootstrap, ~1400 packages, squashfs, ISO)"
(cd "$lbdir" && lb build)

iso_src="$lbdir/Boswas-OS-${BOSWAS_IMAGE_TAG}-${BOSWAS_ARCH}.hybrid.iso"
[ -s "$iso_src" ] || die "live-build finished but produced no ISO ($iso_src)"

# 5. Artifacts ------------------------------------------------------------------------------
iso="$OUT/${BOSWAS_IMAGE_BASENAME}.iso"
log "collecting artifacts into build/output/"
rm -f "$OUT/${BOSWAS_IMAGE_BASENAME}".* "$OUT"/boswas-control-plane_*.deb
cp "$iso_src" "$iso"
(cd "$OUT" && sha256sum "$(basename "$iso")" > "${BOSWAS_IMAGE_BASENAME}.sha256")
# The Control Plane is a server package: delivered next to the ISO, never in it.
cp "$WORK"/debs/server/boswas-control-plane_*.deb "$OUT/"

kernel="$(basename "$(ls "$lbdir"/chroot/boot/vmlinuz-* | sort -V | tail -1)" | sed 's/^vmlinuz-//')"
python3 "$BOSWAS_REPO_ROOT/build/scripts/generate-manifest.py" \
	--image-info "$image_info" \
	--iso "$iso" \
	--packages "$lbdir/Boswas-OS-${BOSWAS_IMAGE_TAG}-${BOSWAS_ARCH}.packages" \
	--output-dir "$OUT" \
	--manifest-dir "$MANIFESTS" \
	--set "debian_version=$(cat "$lbdir/chroot/etc/debian_version")" \
	--set "kernel=$kernel" \
	--set "debootstrap=$(dpkg-query -W -f='${Version}' debootstrap)" \
	--set "builder=${BOSWAS_BUILDER_IMAGE:-native $(. /etc/os-release && echo "$PRETTY_NAME")}"

# 6. Clean temporary state -------------------------------------------------------------
if $keep_work; then
	log "keeping work tree in $lbdir (--keep-work)"
else
	log "removing live-build chroot and binary trees (package cache kept in $CACHE)"
	(cd "$lbdir" && lb clean noauto >/dev/null 2>&1) || true
	safe_rm_tree "$lbdir"
	rm -rf "$WORK/debs" "$image_info"
fi

if [ -n "${BOSWAS_HOST_UID:-}" ]; then
	chown -R "$BOSWAS_HOST_UID:${BOSWAS_HOST_GID:-$BOSWAS_HOST_UID}" "$OUT" "$LOGS" "$MANIFESTS" 2>/dev/null || true
fi

ok "ISO:       build/output/$(basename "$iso") ($(du -h "$iso" | cut -f1))"
ok "SHA-256:   build/output/${BOSWAS_IMAGE_BASENAME}.sha256"
ok "Manifest:  build/output/${BOSWAS_IMAGE_BASENAME}.manifest.txt, build/manifest/${BUILD_ID}.json"
ok "Server:    build/output/$(basename "$(ls "$OUT"/boswas-control-plane_*.deb)") (Control Plane, not in the image)"
ok "Next:      ./test.sh"
