#!/usr/bin/env bash
# Build artifacts: ISO, checksum and manifests exist and are consistent.
set -uo pipefail
. "$(dirname "$0")/../lib.sh"

iso="${ISO:?ISO not set}"
base="${iso%.iso}"

if [ ! -s "$iso" ]; then
	fail "ISO exists ($iso)"
	finish
fi
pass "ISO exists ($(du -h "$iso" | cut -f1))"
check "ISO is a bootable hybrid ISO 9660 image" bash -c "file -b '$iso' | grep -q 'ISO 9660' && file -b '$iso' | grep -q 'bootable'"
check "ISO volume label is BOSWAS_OS_*" bash -c "file -b '$iso' | grep -q \"'BOSWAS_OS_\""

check "SHA-256 checksum file exists" test -s "$base.sha256"
check "SHA-256 checksum verifies" bash -c "cd '$(dirname "$iso")' && sha256sum --quiet -c '$(basename "$base").sha256'"

manifest="$base.manifest.txt"
check "build manifest exists" test -s "$manifest"
for field in "Build ID:" "Git commit:" "Config hash:" "live-build:" "Debian suite:        trixie" "Architecture:        amd64" "Kernel:"; do
	contains "manifest records '${field%%:*}'" "$manifest" "$field"
done
count="$(awk -F'\t' 'NF == 2' "$manifest" | wc -l)"
[ "$count" -gt 1000 ] && pass "manifest lists $count installed packages" || fail "manifest package list too short ($count)"

latest="$BOSWAS_REPO_ROOT/build/manifest/latest.json"
check "machine-readable build record exists" test -s "$latest"
check "build record ISO hash matches the checksum file" python3 - "$latest" "$base.sha256" <<'EOF'
import json, sys
record = json.load(open(sys.argv[1]))
expected = open(sys.argv[2]).read().split()[0]
sys.exit(0 if record["iso"]["sha256"] == expected and record["debian"]["suite"] == "trixie" else 1)
EOF

finish
