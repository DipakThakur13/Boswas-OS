#!/bin/sh
# Install the Boswas pointer cursor theme into DESTDIR.
#
# Renders the committed SVG sources (Boswas-Cursors/src, written by
# tools/build_cursors.py) with rsvg-convert at the nominal sizes 24, 32, 48,
# 64 and 96 and writes
#   /usr/share/icons/Boswas-Cursors/index.theme
#   /usr/share/icons/Boswas-Cursors/cursor.theme       for the x-cursor-theme alternative
#   /usr/share/icons/Boswas-Cursors/cursors/<shape>       XCursor files
#   /usr/share/icons/Boswas-Cursors/cursors/<other name>  symbolic links to a shape
# The XCursor files are written by tools/xcursor.py; xcursorgen is not used.
# The result depends only on the sources and the version of librsvg: there
# are no timestamps in it, and running the script twice gives the same bytes.
# Files are installed 0644 and directories 0755. Nothing else is touched.
#
# Needs python3 and rsvg-convert (librsvg2-bin).
#
# Usage: install.sh DESTDIR
set -eu
umask 022

if [ $# -ne 1 ] || [ -z "$1" ]; then
	echo "usage: install.sh DESTDIR" >&2
	exit 2
fi
dest=$1
here=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
source="$here/Boswas-Cursors"
tool="$here/tools/xcursor.py"

for program in python3 rsvg-convert; do
	if ! command -v "$program" >/dev/null 2>&1; then
		echo "install.sh: $program is required" >&2
		exit 1
	fi
done

# Never ship sources that do not match the generator.
python3 -B "$here/tools/build_cursors.py" --check

stage=$(mktemp -d)
trap 'rm -rf "$stage"' EXIT
trap 'exit 1' HUP INT TERM

# One PNG per frame and size: "PIXELS SOURCE.svg OUTPUT.png" per line.
python3 -B "$tool" plan "$source/cursors.json" > "$stage/plan.txt"
while read -r pixels svg png; do
	rsvg-convert --width "$pixels" --height "$pixels" "$source/src/$svg" -o "$stage/$png"
done < "$stage/plan.txt"

theme="$dest/usr/share/icons/Boswas-Cursors"
install -d -m 0755 "$theme/cursors"
python3 -B "$tool" assemble "$source/cursors.json" "$stage" "$theme/cursors"
install -m 0644 "$source/index.theme" "$theme/index.theme"

# What Debian's x-cursor-theme alternative points /usr/share/icons/default/index.theme
# at, for the programs that load the theme called "default" (GTK and X11
# clients that do not read XCURSOR_THEME). The package registers it.
printf '[Icon Theme]\nInherits=Boswas-Cursors\n' > "$stage/cursor.theme"
install -m 0644 "$stage/cursor.theme" "$theme/cursor.theme"
