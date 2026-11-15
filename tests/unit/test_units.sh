#!/usr/bin/env bash
# Python unit tests of the Boswas components: boswas-compat, the device agent,
# the Compatibility Manager and Boswas Control Center (Qt tests run offscreen
# when PySide6 is installed), the privileged helper (boswas-sysops), the
# Android runtime manager (boswas-android), the Control Plane (including the end-to-end
# agent <-> Control Plane tests over mutual TLS), the presets (boswas-preset,
# generated themes), the Boswas UI tokens and the icon themes, and the Rust
# test suites of the native desktop (compositor, shell, design system) and
# the boot screen. These also run during the package builds (dh_auto_test);
# repeating them here gives one report line each with the number of test
# cases. The CLI unit tests run in tests/packages/test_packages.sh.
set -uo pipefail
. "$(dirname "$0")/../lib.sh"

export QT_QPA_PLATFORM=offscreen
for dir in packages/boswas-compat packages/boswas-device-agent packages/boswas-compat-manager \
	packages/boswas-control-center packages/boswas-sysops packages/boswas-android control-plane \
	desktop/ui desktop/presets desktop/icons; do
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
