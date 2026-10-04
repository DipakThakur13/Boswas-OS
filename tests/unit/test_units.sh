#!/usr/bin/env bash
# Python unit tests of the Boswas components that are not covered by the
# package build: boswas-compat (also run by dh_auto_test) and the device agent
# foundations (not packaged until Milestone 2). The CLI unit tests run in
# tests/packages/test_packages.sh.
set -uo pipefail
. "$(dirname "$0")/../lib.sh"

for pkg in boswas-compat boswas-device-agent; do
	dir="$BOSWAS_REPO_ROOT/packages/$pkg"
	out="$(cd "$dir" && python3 -B -m unittest discover -s tests 2>&1)"
	count="$(grep -oE '^Ran [0-9]+ tests' <<<"$out" | grep -oE '[0-9]+')"
	if grep -q '^OK' <<<"$out"; then
		pass "$pkg unit tests (${count:-?} tests)"
	else
		fail "$pkg unit tests: $(grep -E '^(FAIL|ERROR):' <<<"$out" | head -5 | tr '\n' ';')"
	fi
done
finish
