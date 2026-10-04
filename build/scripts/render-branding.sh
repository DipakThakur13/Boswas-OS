#!/usr/bin/env bash
# Render the boot-menu and installer branding (SVG -> PNG) for the live ISO.
# Desktop assets (wallpapers, icons, login screen) are rendered by the
# boswas-branding package itself (packages/boswas-branding/debian/rules).
#
# Usage: render-branding.sh OUTPUT_DIR
#   OUTPUT_DIR/splash.png            800x600  GRUB menu background
#   OUTPUT_DIR/installer-banner.png  800x75   Debian Installer banner
set -euo pipefail
# shellcheck source=build/scripts/lib.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
require_cmds rsvg-convert python3

out="${1:?usage: render-branding.sh OUTPUT_DIR}"
mkdir -p "$out"
scripts="$BOSWAS_REPO_ROOT/build/scripts"

stage="$(mktemp -d)"
trap 'rm -rf "$stage"' EXIT

# Inline the traced Boswas Group artwork, then fill release placeholders.
python3 "$scripts/inline-svg.py" "$BOSWAS_REPO_ROOT/desktop/branding/boot-splash.svg" "$stage/splash.inlined.svg"
"$scripts/subst-release.sh" < "$stage/splash.inlined.svg" > "$stage/splash.svg"
python3 "$scripts/inline-svg.py" "$BOSWAS_REPO_ROOT/installer/branding/installer-banner.svg" "$stage/banner.svg"

rsvg-convert --width 800 --height 600 "$stage/splash.svg" -o "$out/splash.png"
rsvg-convert --width 800 --height 75 "$stage/banner.svg" -o "$out/installer-banner.png"
