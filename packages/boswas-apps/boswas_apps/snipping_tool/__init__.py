"""Boswas Snipping Tool: snip the screen, mark the snip up, save or copy it. See boswas-snipping-tool(1).

The screen is never read here: the desktop shell does the capture when the
compositor is asked to start one, saves the snip to the Screenshots folder
and copies it to the clipboard. The Snipping Tool watches that folder for
the new file and opens it in its editor.

Modules without Qt
  cli          the command line
  capture      the compositor's control socket, the Screenshots folder and waiting for a new snip
  annotations  marks in picture pixels, the eraser's hit test, crop arithmetic, the undo history
  settings     the user's settings, kept in ``settings.json``

Modules with QtGui only (no widgets)
  picture      reading pictures with limits, drawing the marks, writing PNG and JPEG atomically

Modules with widgets
  ui.*         the window, the canvas, the tool bar and the snip flow
"""

APP_ID = "com.boswas.SnippingTool"
DISPLAY_NAME = "Snipping Tool"
PROGRAM = "boswas-snipping-tool"
