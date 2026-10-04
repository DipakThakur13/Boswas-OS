# shellcheck shell=bash
# Shared helpers for Boswas OS test suites (sourced by tests/*/*.sh).
#
# Each result is printed and appended to $BOSWAS_TEST_REPORT as
#   PASS|FAIL|SKIP <TAB> suite <TAB> description
# test.sh aggregates the report. A suite exits non-zero if anything FAILed.

: "${BOSWAS_TEST_REPORT:=/dev/null}"
: "${BOSWAS_REPO_ROOT:=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
SUITE="${SUITE:-$(basename "$(dirname "$0")")/$(basename "$0" .sh)}"
FAILED=0

_record() {
	printf '%s\t%s\t%s\n' "$1" "$SUITE" "$2" >> "$BOSWAS_TEST_REPORT"
	printf '  %-4s  %s\n' "$1" "$2"
}
pass() { _record PASS "$*"; }
fail() { _record FAIL "$*"; FAILED=1; }
skip() { _record SKIP "$*"; }

# check "description" command [args...]   - PASS if the command succeeds
check() {
	local desc="$1"; shift
	if "$@" >/dev/null 2>&1; then pass "$desc"; else fail "$desc"; fi
}

# check_not "description" command [args...] - PASS if the command fails
check_not() {
	local desc="$1"; shift
	if "$@" >/dev/null 2>&1; then fail "$desc"; else pass "$desc"; fi
}

# contains "description" FILE PATTERN       - fixed-string match in a file
contains() {
	check "$1" grep -qF -- "$3" "$2"
}

# Package state inside an extracted image root.
pkg_installed() {
	[ "$(dpkg-query --admindir="$1/var/lib/dpkg" -W -f='${db:Status-Status}' "$2" 2>/dev/null)" = "installed" ]
}

# Top-level packages named in the live-build package lists.
manifest_packages() {
	sed 's/#.*//' "$BOSWAS_REPO_ROOT"/config/live-build/config/package-lists/*.list.chroot | awk 'NF { print $1 }'
}

finish() {
	exit "$FAILED"
}
