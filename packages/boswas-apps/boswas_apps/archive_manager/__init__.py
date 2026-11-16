"""Boswas Archive Manager: opens, extracts and creates ZIP and TAR archives.

Modules without Qt (unit-tested on their own):
  names     decoding entry names and making them safe to show and to extract
  model     Entry, Listing (the folder tree of an archive), sizes and types as text
  formats   which file is which kind of archive; what this system can read and write
  sevenzip  the optional 7z program: lookup, listing parser, streamed reading
  reader    the archive back ends (zipfile, tarfile, gzip/bz2/lzma streams, 7z)
  extract   extraction that cannot leave its destination
  create    creating archives and rewriting them (add, delete) through a temporary file

Qt modules:
  jobs      runs the engine on a thread; progress and questions cross to the GUI thread
  ui.*      the window, the list model, dialogs, the progress window
"""

APP_ID = "com.boswas.ArchiveManager"
DISPLAY_NAME = "Archive Manager"
PROGRAM = "boswas-archive-manager"
