"""Boswas Terminal: the terminal emulator of Boswas OS.

Modules without Qt (unit-testable on their own):
  wcwidth    how many cells a character takes, from the Unicode database
  emulator   the terminal: escape sequence parser, screen, scrollback, modes
  keys       key and mouse events as the bytes an application expects
  grid       text of lines, selections, words, addresses in the text, search
  ptyhost    the child process on a pseudo-terminal
  schemes    colour schemes: Boswas Prism from the UI tokens, classic ones from JSON
  settings   profiles and settings (JSON in the configuration directory)
  cli        the command line

Qt modules:
  session    a pseudo-terminal child wired to the Qt event loop (and the fake one of the tests)
  view       the widget that draws the cell grid and takes keyboard and mouse
  boxdraw    box drawing and block characters drawn as shapes
  tabstrip   the row of tabs
  findbar    search in the scrollback
  pane       one tab: view, search bar, messages
  settings_page  the settings tab
  window     the window: tabs, shortcuts, menus
  app        windows of one process, single instance
"""

APP_ID = "com.boswas.Terminal"
DISPLAY_NAME = "Terminal"
PROGRAM = "boswas-terminal"
VERSION = "1.0"
