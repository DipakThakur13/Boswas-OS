"""Boswas Photos: the image viewer and light editor of Boswas OS.

Modules without widgets (unit-tested without a display server):

  natsort     natural ("a2" before "a10") order of file names
  formats     file types by extension and by content
  exif        EXIF from JPEG APP1 and TIFF, read with strict bounds checking
  trash       the freedesktop.org Trash specification
  thumbcache  names and directories of the freedesktop.org thumbnail cache
  desktopcfg  the wallpaper key of the desktop's appearance.toml
  library     finding the pictures of the gallery, months, folders, search
  settings    what the user chose
  openwith    other applications that open a file type (desktop entries)

Modules that use QtGui only (QImage, no widgets):

  decode      reading pictures with size and memory limits
  editops     the edit pipeline as pure functions over QImage
  imageio     writing pictures atomically, EXIF kept for JPEG
  thumbs      thumbnails: making, caching on disk and in memory

Everything under ``ui`` is widgets.
"""

APP_ID = "com.boswas.Photos"
DISPLAY_NAME = "Photos"
PROGRAM = "boswas-photos"
