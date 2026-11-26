#!/usr/bin/env bash
# Python unit tests of the Boswas components: boswas-compat, the device agent,
# the Compatibility Manager and Boswas Control Center (Qt tests run offscreen
# when PySide6 is installed), the privileged helper (boswas-sysops), the
# Android runtime manager (boswas-android), the system applications
# (boswas-apps), the Control Plane (including the end-to-end
# agent <-> Control Plane tests over mutual TLS), the presets (boswas-preset,
# the generated colour schemes and artwork), the Boswas UI tokens, the
# icon themes, the pointer theme and the brand assets (the mark and wordmark
# generators), and the Rust test suites of the native desktop (compositor,
# shell, design system) and the boot screen. These also run during the
# package builds (dh_auto_test);
# repeating them here gives one report line each with the number of test
# cases. The CLI unit tests run in tests/packages/test_packages.sh.
set -uo pipefail
. "$(dirname "$0")/../lib.sh"

export QT_QPA_PLATFORM=offscreen
for dir in packages/boswas-compat packages/boswas-device-agent packages/boswas-compat-manager \
	packages/boswas-control-center packages/boswas-sysops packages/boswas-android control-plane \
	desktop/ui desktop/ui/qt desktop/presets desktop/icons desktop/cursors desktop/branding; do
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

# The generator of the shell's emoji data (packages/boswas-desktop/tools).
out="$(cd "$BOSWAS_REPO_ROOT/packages/boswas-desktop" && python3 -B -m unittest discover -s tools/tests 2>&1)"
count="$(grep -oE '^Ran [0-9]+ tests?' <<<"$out" | grep -oE '[0-9]+')"
skipped="$(grep -oE 'skipped=[0-9]+' <<<"$out" | grep -oE '[0-9]+' || true)"
if grep -qE '^OK' <<<"$out"; then
	pass "boswas-desktop tools unit tests (${count:-?} tests${skipped:+, $skipped skipped})"
else
	fail "boswas-desktop tools unit tests: $(grep -E '^(FAIL|ERROR):' <<<"$out" | head -5 | tr '\n' ';')"
fi

# The system applications (boswas-apps): one suite per application, on the
# toolkit of this checkout.
apps="$BOSWAS_REPO_ROOT/packages/boswas-apps"
for suite in "$apps"/tests/*/; do
	[ -n "$(find "$suite" -name 'test_*.py' -print -quit 2>/dev/null)" ] || continue
	name="boswas-apps/$(basename "$suite")"
	out="$(cd "$apps" && PYTHONPATH="$apps:$BOSWAS_REPO_ROOT/desktop/ui/qt" \
		python3 -B -m unittest discover -s "$suite" -t "$suite" 2>&1)"
	count="$(grep -oE '^Ran [0-9]+ tests?' <<<"$out" | grep -oE '[0-9]+')"
	skipped="$(grep -oE 'skipped=[0-9]+' <<<"$out" | grep -oE '[0-9]+' || true)"
	if grep -qE '^OK' <<<"$out"; then
		pass "$name unit tests (${count:-?} tests${skipped:+, $skipped skipped})"
	else
		fail "$name unit tests: $(grep -E '^(FAIL|ERROR):' <<<"$out" | head -5 | tr '\n' ';')"
	fi
done

# Rust: the native desktop and the boot screen. cargo uses the committed
# Cargo.lock; the crates come from crates.io (or the builder's cargo cache).
# The build trees go to the test work directory, never into the checkout.
rust_suite() {
	local name="$1" dir="$2"; shift 2
	local out passed
	if ! command -v cargo >/dev/null 2>&1; then
		skip "$name Rust tests (cargo is not installed; rebuild the builder image)"
		return
	fi
	out="$(cd "$BOSWAS_REPO_ROOT/$dir" && CARGO_TARGET_DIR="${TESTWORK:-/var/tmp/boswas-os-test}/cargo-$name" \
		cargo test --locked "$@" 2>&1)"
	passed="$(grep -oE '^test result: ok\. [0-9]+ passed' <<<"$out" | grep -oE '[0-9]+' | awk '{ s += $1 } END { print s }')"
	if grep -qE '^test result: FAILED|^error' <<<"$out" || ! grep -q '^test result: ok' <<<"$out"; then
		fail "$name Rust tests: $(grep -E '^(test .* FAILED|error)' <<<"$out" | head -5 | tr '\n' ';')"
	else
		pass "$name Rust tests (${passed:-?} tests)"
	fi
}
rust_suite boswas-desktop packages/boswas-desktop --workspace --features boswas-ui/text,boswas-ui/icons,boswas-ui/images
rust_suite boswas-bootsplash packages/boswas-bootsplash
finish
