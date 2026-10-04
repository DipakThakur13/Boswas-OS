#!/usr/bin/env bash
# Build the Boswas .deb packages from packages/*/debian.
#
# Usage: build-packages.sh OUTPUT_DIR
#
# Packages reference shared sources elsewhere in the monorepo (security/,
# desktop/, config/boswas/, build/scripts/), so the relevant parts of the
# repository are staged into a scratch tree with the same layout and each
# package is built there with dpkg-buildpackage. The checkout is never
# modified, and the build does not depend on its absolute path.
set -euo pipefail
# shellcheck source=build/scripts/lib.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
require_cmds dpkg-buildpackage dh rsync

out="${1:?usage: build-packages.sh OUTPUT_DIR}"
mkdir -p "$out"
out="$(cd "$out" && pwd)"

stage="$(mktemp -d)"
trap 'rm -rf "$stage"' EXIT
rsync -a --exclude '__pycache__' \
	--include '/build/' --include '/build/scripts/***' --exclude '/build/*' \
	--include '/config/' --include '/config/boswas/***' --exclude '/config/*' \
	--include '/desktop/***' --include '/installer/***' --include '/packages/***' --include '/security/***' \
	--exclude '/*' \
	"$BOSWAS_REPO_ROOT/" "$stage/"

# Sources may come from a filesystem without POSIX permissions (e.g. a
# Windows checkout). Normalise so nothing depends on host file modes; the
# packages themselves install every file with an explicit mode.
find "$stage" -type d -exec chmod 0755 {} +
find "$stage" -type f -exec chmod 0644 {} +
chmod 0755 "$stage"/build/scripts/*.sh "$stage"/build/scripts/*.py "$stage"/packages/*/debian/rules

# Reproducible package timestamps.
export SOURCE_DATE_EPOCH="${SOURCE_DATE_EPOCH:-$(git -c safe.directory='*' -C "$BOSWAS_REPO_ROOT" log -1 --format=%ct 2>/dev/null || date +%s)}"

for dir in "$stage"/packages/*/; do
	pkg="$(basename "$dir")"
	[ -f "$dir/debian/control" ] || continue
	log "building package $pkg"
	(cd "$dir" && dpkg-buildpackage --build=binary --no-sign --check-builddeps) \
		> "$out/$pkg.build.log" 2>&1 || { tail -40 "$out/$pkg.build.log" >&2; die "package $pkg failed (log: $out/$pkg.build.log)"; }
done

mv "$stage"/packages/*.deb "$out/"
rm -f "$stage"/packages/*.buildinfo "$stage"/packages/*.changes
ok "built $(find "$out" -maxdepth 1 -name '*.deb' | wc -l) package(s) into $out"
