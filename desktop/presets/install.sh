#!/bin/sh
# Install the Boswas OS visual presets into DESTDIR.
#
# Renders the artwork from the generated SVG sources (inlining the brand
# assets with build/scripts/inline-svg.py, which Qt and the renderers need)
# and installs, for every preset in presets.json:
#   /usr/share/color-schemes/Boswas<Label>.colors
#   /usr/share/plasma/look-and-feel/com.boswas.<id>/       Global Theme
#   /usr/share/wallpapers/Boswas-<Label>/                  wallpaper package
#   /usr/share/konsole/Boswas<Label>.colorscheme, "Boswas <Label>.profile"
#   /usr/share/boswas/presets/lock/<id>.png                lock screen
#   /usr/share/boswas/presets/previews/<id>.png            Control Center preview
# plus /usr/share/boswas/presets/presets.json, the default wallpaper package
# "Boswas" (a copy of the default preset's), the login background
# /usr/share/boswas/branding/login-background.png and the boswas-preset tool.
#
# Needs python3 and rsvg-convert (librsvg2-bin). With netpbm (pngtopnm,
# pnmtojpeg) it also writes the Global Themes' full-screen previews.
#
# Usage: install.sh DESTDIR
set -eu
umask 022

if [ $# -ne 1 ] || [ -z "$1" ]; then
	echo "usage: install.sh DESTDIR" >&2
	exit 2
fi
dest=$1
here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/../.." && pwd)
inline="$repo/build/scripts/inline-svg.py"

for tool in python3 rsvg-convert; do
	if ! command -v "$tool" >/dev/null 2>&1; then
		echo "install.sh: $tool is required" >&2
		exit 1
	fi
done
jpeg=yes
for tool in pngtopnm pnmtojpeg; do
	command -v "$tool" >/dev/null 2>&1 || jpeg=no
done
[ "$jpeg" = yes ] || echo "install.sh: netpbm not found, skipping fullscreenpreview.jpg" >&2

# Never ship generated sources that do not match presets.json.
python3 "$here/tools/build_presets.py" --check

stage=$(mktemp -d)
trap 'rm -rf "$stage"' EXIT
trap 'exit 1' HUP INT TERM

share="$dest/usr/share"
data="$share/boswas/presets"

inst() {
	install -D -m 0644 "$1" "$2"
}

# render SOURCE.svg OUTPUT.png WIDTH HEIGHT
render() {
	python3 "$inline" "$1" "$stage/inlined.svg"
	rsvg-convert --width "$3" --height "$4" "$stage/inlined.svg" -o "$stage/rendered.png"
	python3 "$here/tools/repack_png.py" "$stage/rendered.png" "$2"
}

# Second name for an installed file: a hard link (stored once in the
# package), or a copy where links are not possible.
alias_file() {
	mkdir -p "$(dirname "$2")"
	ln -f "$1" "$2" 2>/dev/null || install -m 0644 "$1" "$2"
}

python3 -c '
import json, sys
data = json.load(open(sys.argv[1], encoding="utf-8"))
for p in data["presets"]:
    print(p["id"], p["label"], p["variant"], "default" if p["id"] == data["default"] else "-")
' "$here/presets.json" > "$stage/presets.txt"

default_id=
default_label=
while read -r id label variant default; do
	echo "install.sh: $id" >&2
	scheme="Boswas$label"
	wall="Boswas-$label"
	primary=dark
	[ "$variant" = light ] && primary=light
	[ "$default" = default ] && default_id=$id && default_label=$label

	# Colour scheme and Konsole
	inst "$here/color-schemes/$scheme.colors" "$share/color-schemes/$scheme.colors"
	inst "$here/konsole/$scheme.colorscheme" "$share/konsole/$scheme.colorscheme"
	inst "$here/konsole/Boswas $label.profile" "$share/konsole/Boswas $label.profile"

	# Wallpaper package: Plasma shows images_dark/ with a dark colour scheme.
	w="$share/wallpapers/$wall"
	inst "$here/wallpapers/$wall/metadata.json" "$w/metadata.json"
	render "$here/wallpapers/$wall/light.svg" "$stage/light.png" 3840 2160
	inst "$stage/light.png" "$w/contents/images/3840x2160.png"
	render "$here/wallpapers/$wall/dark.svg" "$stage/dark.png" 3840 2160
	inst "$stage/dark.png" "$w/contents/images_dark/3840x2160.png"
	render "$here/wallpapers/$wall/$primary.svg" "$stage/screenshot.png" 480 270
	inst "$stage/screenshot.png" "$w/contents/screenshot.png"

	# Lock screen background
	render "$here/wallpapers/$wall/lock.svg" "$stage/lock.png" 3840 2160
	inst "$stage/lock.png" "$data/lock/$id.png"

	# Global Theme
	l="$share/plasma/look-and-feel/com.boswas.$id"
	inst "$here/look-and-feel/com.boswas.$id/metadata.json" "$l/metadata.json"
	inst "$here/look-and-feel/com.boswas.$id/contents/defaults" "$l/contents/defaults"
	inst "$here/look-and-feel/com.boswas.$id/contents/layouts/org.kde.plasma.desktop-layout.js" \
		"$l/contents/layouts/org.kde.plasma.desktop-layout.js"
	render "$here/previews/$id.svg" "$stage/preview.png" 600 338
	inst "$stage/preview.png" "$l/contents/previews/preview.png"
	if [ "$jpeg" = yes ]; then
		render "$here/previews/$id.svg" "$stage/full.png" 1920 1080
		pngtopnm "$stage/full.png" | pnmtojpeg --quality=90 > "$stage/full.jpg"
		inst "$stage/full.jpg" "$l/contents/previews/fullscreenpreview.jpg"
	fi

	# Control Center preview
	render "$here/previews/$id.svg" "$stage/cc.png" 480 270
	inst "$stage/cc.png" "$data/previews/$id.png"
done < "$stage/presets.txt"

if [ -z "$default_id" ]; then
	echo "install.sh: presets.json names no default preset" >&2
	exit 1
fi

# "Boswas", the wallpaper the system defaults name, is the default preset's.
src="$share/wallpapers/Boswas-$default_label"
inst "$here/wallpapers/Boswas/metadata.json" "$share/wallpapers/Boswas/metadata.json"
for file in contents/images/3840x2160.png contents/images_dark/3840x2160.png contents/screenshot.png; do
	alias_file "$src/$file" "$share/wallpapers/Boswas/$file"
done
# Login screen (SDDM) background: the default preset's lock screen art.
alias_file "$data/lock/$default_id.png" "$share/boswas/branding/login-background.png"

# Preset list and the boswas-preset tool
inst "$here/presets.json" "$data/presets.json"
inst "$here/boswas_preset/__init__.py" "$dest/usr/lib/boswas/python/boswas_preset/__init__.py"
inst "$here/boswas_preset/cli.py" "$dest/usr/lib/boswas/python/boswas_preset/cli.py"
install -D -m 0755 "$here/bin/boswas-preset" "$dest/usr/bin/boswas-preset"
