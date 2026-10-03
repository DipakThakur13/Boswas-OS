# shellcheck shell=bash
# Common helpers for Boswas OS build and test scripts. Source, do not execute.

# Repository root, independent of the caller's working directory.
BOSWAS_REPO_ROOT="${BOSWAS_REPO_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
export BOSWAS_REPO_ROOT

if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
	_c_info=$'\033[1;36m' _c_ok=$'\033[1;32m' _c_warn=$'\033[1;33m' _c_err=$'\033[1;31m' _c_off=$'\033[0m'
else
	_c_info="" _c_ok="" _c_warn="" _c_err="" _c_off=""
fi

log()  { printf '%s[boswas]%s %s\n' "$_c_info" "$_c_off" "$*"; }
ok()   { printf '%s[ ok ]%s %s\n' "$_c_ok" "$_c_off" "$*"; }
warn() { printf '%s[warn]%s %s\n' "$_c_warn" "$_c_off" "$*" >&2; }
die()  { printf '%s[fail]%s %s\n' "$_c_err" "$_c_off" "$*" >&2; exit 1; }

# Load config/boswas/release.conf into the environment.
load_release() {
	local conf="$BOSWAS_REPO_ROOT/config/boswas/release.conf"
	[ -r "$conf" ] || die "missing $conf"
	# shellcheck source=/dev/null
	. "$conf"
	BOSWAS_IMAGE_BASENAME="Boswas-OS-${BOSWAS_IMAGE_TAG}-${BOSWAS_ARCH}"
	export BOSWAS_NAME BOSWAS_VERSION BOSWAS_VERSION_ID BOSWAS_IMAGE_TAG BOSWAS_VENDOR \
		BOSWAS_CHANNEL BOSWAS_BASE_NAME BOSWAS_BASE_VERSION BOSWAS_BASE_CODENAME \
		BOSWAS_ARCH BOSWAS_IMAGE_BASENAME
}

# Git metadata for manifests. Works when the repository is bind-mounted into a
# container owned by a different uid (safe.directory).
git_meta() {
	local g=(git -c safe.directory='*' -C "$BOSWAS_REPO_ROOT")
	if command -v git >/dev/null 2>&1 && "${g[@]}" rev-parse --git-dir >/dev/null 2>&1; then
		BOSWAS_GIT_COMMIT="$("${g[@]}" rev-parse HEAD 2>/dev/null || echo none)"
		if [ "$BOSWAS_GIT_COMMIT" = "HEAD" ] || [ -z "$BOSWAS_GIT_COMMIT" ]; then
			BOSWAS_GIT_COMMIT="none"
		fi
		if [ -n "$("${g[@]}" status --porcelain 2>/dev/null)" ]; then
			BOSWAS_GIT_DIRTY="true"
		else
			BOSWAS_GIT_DIRTY="false"
		fi
		BOSWAS_GIT_COMMIT_EPOCH="$("${g[@]}" log -1 --format=%ct 2>/dev/null || true)"
	else
		BOSWAS_GIT_COMMIT="none" BOSWAS_GIT_DIRTY="unknown" BOSWAS_GIT_COMMIT_EPOCH=""
	fi
	export BOSWAS_GIT_COMMIT BOSWAS_GIT_DIRTY BOSWAS_GIT_COMMIT_EPOCH
}

# Hash of every input that determines the image (relative paths + content), so
# two checkouts of the same tree at different absolute paths hash identically.
config_hash() {
	(
		cd "$BOSWAS_REPO_ROOT" &&
		find build.sh build/scripts build/container config desktop installer packages security \
			-type f ! -name '*.pyc' ! -path '*/__pycache__/*' -print0 2>/dev/null |
		LC_ALL=C sort -z | xargs -0 sha256sum | sha256sum | cut -d' ' -f1
	)
}

# Detect an OCI container runtime for --container mode.
container_runtime() {
	if [ -n "${BOSWAS_CONTAINER_RUNTIME:-}" ]; then
		echo "$BOSWAS_CONTAINER_RUNTIME"
	elif command -v podman >/dev/null 2>&1; then
		echo podman
	elif command -v docker >/dev/null 2>&1; then
		echo docker
	fi
}

# Path of the repository as the container runtime needs it for a bind mount.
# Git Bash / MSYS on Windows needs a native Windows path.
host_repo_path() {
	if command -v cygpath >/dev/null 2>&1; then
		cygpath -w "$BOSWAS_REPO_ROOT"
	else
		echo "$BOSWAS_REPO_ROOT"
	fi
}

is_supported_build_host() {
	[ -r /etc/os-release ] || return 1
	# shellcheck source=/dev/null
	( . /etc/os-release && [ "${ID:-}" = "debian" ] && [ "${VERSION_CODENAME:-}" = "trixie" ] )
}

require_cmds() {
	local missing=() c
	for c in "$@"; do
		command -v "$c" >/dev/null 2>&1 || missing+=("$c")
	done
	[ "${#missing[@]}" -eq 0 ] || die "missing required tools: ${missing[*]} (see docs/development/building.md)"
}
