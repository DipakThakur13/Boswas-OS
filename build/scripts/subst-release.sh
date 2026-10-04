#!/usr/bin/env bash
# Replace @BOSWAS_*@ placeholders on stdin with the values from
# config/boswas/release.conf and write the result to stdout. Fails if any
# placeholder is left unresolved.
set -euo pipefail
# shellcheck source=build/scripts/lib.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
load_release

args=()
for var in BOSWAS_NAME BOSWAS_VERSION BOSWAS_VERSION_ID BOSWAS_IMAGE_TAG BOSWAS_VENDOR \
	BOSWAS_CHANNEL BOSWAS_BASE_NAME BOSWAS_BASE_VERSION BOSWAS_BASE_CODENAME BOSWAS_ARCH; do
	value="${!var}"
	value="${value//\\/\\\\}"
	value="${value//|/\\|}"
	value="${value//&/\\&}"
	args+=(-e "s|@${var}@|${value}|g")
done

out="$(sed "${args[@]}")"
if grep -q '@BOSWAS_[A-Z_]*@' <<<"$out"; then
	die "unresolved placeholder: $(grep -o '@BOSWAS_[A-Z_]*@' <<<"$out" | sort -u | tr '\n' ' ')"
fi
printf '%s\n' "$out"
