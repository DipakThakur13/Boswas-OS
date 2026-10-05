#!/bin/sh
# Install the Boswas icon themes under DESTDIR:
#   $DESTDIR/usr/share/icons/Boswas/        (dark panels; inherits Breeze Dark)
#   $DESTDIR/usr/share/icons/Boswas-Light/  (light panels; inherits Breeze)
# Both themes get the same icons, as real files (no symlinks). Files are
# installed 0644 and directories 0755. Nothing else is touched: no icon
# cache is built (dh_icons or the packaging does that).
#
# Usage: desktop/icons/install.sh DESTDIR
set -eu

if [ "$#" -ne 1 ] || [ -z "$1" ]; then
	echo "usage: $0 DESTDIR" >&2
	exit 2
fi
destdir=$1
src=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
umask 022

for theme in Boswas Boswas-Light; do
	target="$destdir/usr/share/icons/$theme"
	install -D -m 0644 "$src/$theme/index.theme" "$target/index.theme"
	for context in apps mimetypes; do
		install -d -m 0755 "$target/scalable/$context"
		for svg in "$src/Boswas/scalable/$context"/*.svg; do
			[ -f "$svg" ] || { echo "install.sh: no icons in $src/Boswas/scalable/$context" >&2; exit 1; }
			install -m 0644 "$svg" "$target/scalable/$context/"
		done
	done
done
