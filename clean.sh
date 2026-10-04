#!/usr/bin/env bash
# Remove Boswas OS build state.
#
#   ./clean.sh            remove build outputs, logs, manifests and work trees
#   ./clean.sh --cache    also drop the live-build package/bootstrap cache
#   ./clean.sh --all      everything above plus the builder container image
#
# In container mode the work tree and cache live in the Docker/Podman volume
# "boswas-os-build"; --cache removes that volume.
set -euo pipefail
# shellcheck source=build/scripts/lib.sh
. "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/build/scripts/lib.sh"

cache=false all=false
for arg in "$@"; do
	case "$arg" in
		--cache) cache=true ;;
		--all) cache=true all=true ;;
		-h|--help) sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
		*) die "unknown option: $arg" ;;
	esac
done

cd "$BOSWAS_REPO_ROOT"
log "removing build/output, build/logs, build/manifest"
rm -rf build/output build/logs build/manifest
rm -rf packages/*/build packages/*/debian/.debhelper packages/*/debian/boswas-*/ \
	packages/*/debian/files packages/*/debian/*.substvars packages/*/debian/debhelper-build-stamp
find . -name __pycache__ -type d -prune -exec rm -rf {} +

# Native-mode work tree (needs root if a build ran as root)
work="${BOSWAS_WORK_DIR:-/var/tmp/boswas-os-build}"
if [ -d "$work" ]; then
	if [ "$(id -u)" -eq 0 ]; then
		log "removing native work tree $work"
		safe_rm_tree "$work"
	else
		warn "native work tree $work exists; re-run as root to remove it"
	fi
fi

if $cache; then
	log "removing build/cache"
	if [ -d build/cache ] && [ "$(id -u)" -ne 0 ] && [ -n "$(find build/cache -mindepth 1 -maxdepth 1 2>/dev/null)" ]; then
		warn "build/cache may contain root-owned files; re-run as root if removal fails"
	fi
	rm -rf build/cache || true
	rt="$(container_runtime)"
	if [ -n "$rt" ] && "$rt" volume inspect boswas-os-build >/dev/null 2>&1; then
		log "removing container volume boswas-os-build"
		"$rt" volume rm boswas-os-build >/dev/null
	fi
fi

if $all; then
	rt="$(container_runtime)"
	if [ -n "$rt" ] && "$rt" image inspect "${BOSWAS_BUILDER_IMAGE:-boswas-os-builder:trixie}" >/dev/null 2>&1; then
		log "removing builder image"
		"$rt" image rm "${BOSWAS_BUILDER_IMAGE:-boswas-os-builder:trixie}" >/dev/null
	fi
fi
ok "clean"
