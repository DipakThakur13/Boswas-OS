#!/usr/bin/env bash
# Python unit tests of the Boswas components: boswas-compat, the device agent,
# the Compatibility Manager (Qt tests run offscreen when PySide6 is
# installed) and the Control Plane (including the end-to-end agent <->
# Control Plane tests over mutual TLS). These also run during the package
# builds (dh_auto_test); repeating them here gives one report line each with
# the number of test cases. The CLI unit tests run in tests/packages/test_packages.sh.
set -uo pipefail
. "$(dirname "$0")/../lib.sh"

export QT_QPA_PLATFORM=offscreen
for dir in packages/boswas-compat packages/boswas-device-agent packages/boswas-compat-manager control-plane; do
	name="$(basename "$dir")"
	out="$(cd "$BOSWAS_REPO_ROOT/$dir" && python3 -B -m unittest discover -s tests 2>&1)"
	count="$(grep -oE '^Ran [0-9]+ tests?' <<<"$out" | grep -oE '[0-9]+')"
	skipped="$(grep -oE 'skipped=[0-9]+' <<<"$out" | grep -oE '[0-9]+' || true)"
	if grep -qE '^OK' <<<"$out"; then
		pass "$name unit tests (${count:-?} tests${skipped:+, $skipped skipped})"
	else
		fail "$name unit tests: $(grep -E '^(FAIL|ERROR):' <<<"$out" | head -5 | tr '\n' ';')"
	fi
done
finish
