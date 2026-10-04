#!/usr/bin/env bash
# Assemble the live-build working tree for one build.
#
# Usage: prepare-live-config.sh WORK_DIR DEB_DIR IMAGE_INFO_FILE
#
# Copies config/live-build into WORK_DIR and adds everything generated per
# build: the Boswas .debs, rendered boot/installer branding, the installer
# preseed and /usr/lib/boswas/image-info. The repository is never modified.
set -euo pipefail
# shellcheck source=build/scripts/lib.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

work="${1:?usage: prepare-live-config.sh WORK_DIR DEB_DIR IMAGE_INFO_FILE}"
debs="${2:?missing DEB_DIR}"
image_info="${3:?missing IMAGE_INFO_FILE}"
scripts="$BOSWAS_REPO_ROOT/build/scripts"

safe_rm_tree "$work"
mkdir -p "$work"
cp -a "$BOSWAS_REPO_ROOT/config/live-build/." "$work/"

# Never inherit host file modes (e.g. 0777 on a Windows checkout): files that
# end up in the image get explicit permissions.
find "$work" -type d -exec chmod 0755 {} +
find "$work" -type f -exec chmod 0644 {} +
chmod 0755 "$work"/auto/*
find "$work/config/hooks" -type f -name '*.hook.*' -exec chmod 0755 {} +

# Boswas packages (installed via config/package-lists/boswas-components.list.chroot)
mkdir -p "$work/config/packages.chroot"
cp "$debs"/*.deb "$work/config/packages.chroot/"

# Boot menu: release placeholders and the rendered Boswas splash
grub="$work/config/bootloaders/grub-pc"
for cfg in "$grub"/*.cfg; do
	"$scripts/subst-release.sh" < "$cfg" > "$cfg.tmp"
	mv "$cfg.tmp" "$cfg"
	chmod 0644 "$cfg"
done
art="$(mktemp -d)"
trap 'rm -rf "$art"' EXIT
"$scripts/render-branding.sh" "$art"
install -m 0644 "$art/splash.png" "$grub/splash.png"

# Debian Installer: Boswas preseed and banner (light and dark variants)
inst="$work/config/includes.installer"
install -D -m 0644 "$BOSWAS_REPO_ROOT/installer/configuration/preseed.cfg" "$inst/preseed.cfg"
for name in logo_debian logo_debian_dark logo_installer logo_installer_dark; do
	install -D -m 0644 "$art/installer-banner.png" "$inst/usr/share/graphics/$name.png"
done

# Image build metadata, readable on every device (boswas info)
install -D -m 0644 "$image_info" "$work/config/includes.chroot_after_packages/usr/lib/boswas/image-info"

ok "live-build tree prepared in $work"
