"""Boswas Files: the file manager of Boswas OS.

The engine modules have no Qt imports and are tested on their own:

  text        names, natural ordering, sizes
  listing     reading and sorting a folder
  places      locations (folders, trash:///, computer:///), user folders
  trash       the freedesktop.org Trash specification
  fileops     copy, move, delete, trash, restore, with progress and undo
  archives    ZIP creation, ZIP and tar extraction
  clipformats clipboard and drag-and-drop formats shared with other file managers
  desktopapps desktop entries, the Exec key, mimeapps.list
  volumes     /proc/self/mountinfo, UDisks2 data, the list of volumes
  thumbs      the freedesktop.org thumbnail cache: names, validity
  search      searching a folder tree by name
  settings    what the user chose

Everything else is the PySide6 user interface on the boswas_fluent toolkit.
"""

APP_ID = "com.boswas.Files"
PROGRAM = "boswas-files"
DISPLAY_NAME = "Files"
