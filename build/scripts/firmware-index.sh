#!/usr/bin/env bash
# Generate the firmware index of the image: which Debian package ships which
# firmware file.
#
# Usage: firmware-index.sh OUTPUT_FILE [CACHE_DIR]
#
# Reads the Contents indices of Debian's non-free-firmware area (all and the
# image architecture) for the release's suite from $BOSWAS_MIRROR and writes
# one line per file: "<path below /usr/lib/firmware>\t<package>". The image
# installs it as /usr/share/boswas/firmware/index.tsv; boswas-sysops
# firmware-report uses it to name the package that provides a firmware file a
# driver could not load (installed files are resolved with dpkg -S instead).
# The downloads are cached in CACHE_DIR (default: a temporary directory).
set -euo pipefail
# shellcheck source=build/scripts/lib.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
load_release
require_cmds wget gzip awk sort

out="${1:?usage: firmware-index.sh OUTPUT_FILE [CACHE_DIR]}"
cache="${2:-}"
tmp=""
if [ -z "$cache" ]; then
	tmp="$(mktemp -d)"
	cache="$tmp"
	trap 'rm -rf "$tmp"' EXIT
fi
mkdir -p "$cache"
mirror="${BOSWAS_MIRROR:-http://deb.debian.org/debian/}"
mirror="${mirror%/}"

files=()
for arch in all "$BOSWAS_ARCH"; do
	# Small files (tens of kB): fetched fresh every build; the cached copy is
	# only a fallback when the mirror cannot be reached.
	f="$cache/Contents-non-free-firmware-${BOSWAS_BASE_CODENAME}-${arch}.gz"
	if wget -q "$mirror/dists/$BOSWAS_BASE_CODENAME/non-free-firmware/Contents-${arch}.gz" -O "$f.new" && [ -s "$f.new" ]; then
		mv "$f.new" "$f"
	else
		rm -f "$f.new"
		[ -s "$f" ] || die "cannot download Contents-${arch}.gz from $mirror"
		warn "using the cached Contents-${arch}.gz (mirror unreachable)"
	fi
	files+=("$f")
done

# Contents lines: "<path><spaces><section/package>[,<section/package>...]"
gzip -dc "${files[@]}" \
	| awk '$1 ~ /^(usr\/)?lib\/firmware\// {
		path = $1; sub(/^(usr\/)?lib\/firmware\//, "", path)
		n = split($NF, owners, ",")
		for (i = 1; i <= n; i++) { pkg = owners[i]; sub(/.*\//, "", pkg); print path "\t" pkg }
	}' \
	| LC_ALL=C sort -u > "$out.tmp"
[ -s "$out.tmp" ] || die "the firmware index is empty"
grep -q $'^rtl_nic/rtl8125b-2.fw\tfirmware-realtek$' "$out.tmp" || die "the firmware index lacks rtl_nic/rtl8125b-2.fw (Contents format changed?)"
mv "$out.tmp" "$out"
ok "firmware index: $(wc -l < "$out") files ($out)"
