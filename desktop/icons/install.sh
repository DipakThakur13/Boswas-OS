#!/bin/sh
# Install the Boswas icon themes under DESTDIR.
#
#   legacy (default)  $DESTDIR/usr/share/icons/Boswas/        (dark panels; inherits Breeze Dark)
#                     $DESTDIR/usr/share/icons/Boswas-Light/  (light panels; inherits Breeze)
#   prism             $DESTDIR/usr/share/icons/Boswas-Prism/        (dark panels)
#                     $DESTDIR/usr/share/icons/Boswas-Prism-Light/  (light panels)
#   all               both packs
#
# The two themes of a pack get the same coloured icons, as real files (no
# symlinks): the legacy pack from Boswas/, Prism from Boswas-Prism/. The
# symbolic icons of Prism (status, actions) differ per theme and come from
# the theme's own directory. Files are installed 0644 and directories 0755.
# Nothing else is touched: no icon cache is built (dh_icons or the packaging
# does that).
#
# Usage: desktop/icons/install.sh DESTDIR [legacy|prism|all]
set -eu

usage() {
	echo "usage: $0 DESTDIR [legacy|prism|all]" >&2
	exit 2
}

if [ "$#" -lt 1 ] || [ "$#" -gt 2 ] || [ -z "$1" ]; then
	usage
fi
destdir=$1
pack=${2:-legacy}
case "$pack" in
	legacy|prism|all) ;;
	*) usage ;;
esac
src=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
umask 022

# copy_icons SOURCE_THEME TARGET_THEME CONTEXT: every SVG of one context.
copy_icons() {
	install -d -m 0755 "$destdir/usr/share/icons/$2/scalable/$3"
	for svg in "$src/$1/scalable/$3"/*.svg; do
		[ -f "$svg" ] || { echo "install.sh: no icons in $src/$1/scalable/$3" >&2; exit 1; }
		install -m 0644 "$svg" "$destdir/usr/share/icons/$2/scalable/$3/"
	done
}

if [ "$pack" = legacy ] || [ "$pack" = all ]; then
	for theme in Boswas Boswas-Light; do
		install -D -m 0644 "$src/$theme/index.theme" "$destdir/usr/share/icons/$theme/index.theme"
		for context in apps mimetypes; do
			copy_icons Boswas "$theme" "$context"
		done
	done
fi

if [ "$pack" = prism ] || [ "$pack" = all ]; then
	for theme in Boswas-Prism Boswas-Prism-Light; do
		install -D -m 0644 "$src/$theme/index.theme" "$destdir/usr/share/icons/$theme/index.theme"
		for context in apps places devices mimetypes; do
			copy_icons Boswas-Prism "$theme" "$context"
		done
		for context in status actions; do
			copy_icons "$theme" "$theme" "$context"
		done
	done
fi
