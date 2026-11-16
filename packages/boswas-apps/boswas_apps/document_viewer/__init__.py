"""Boswas Document Viewer: a PDF reader built on Qt PDF.

Modules without Qt (unit-tested on their own):
  layout      where pages sit at a zoom, a rotation and a page arrangement; fitting; page ranges
  textsearch  finding text in the text of pages: case, whole words, context lines
  store       recent files and the last view of each document, as a bounded JSON file

Qt modules:
  document    a QPdfDocument with what the application needs around it (sizes, links, text, metadata)
  render      rendering pages on a thread, with priorities and a cache that has a memory budget
  view        the page view: scrolling, zoom, selection, links, highlights
  sidebar     thumbnails, outline, search results
  tab         one document: view, sidebar, find bar, the password and error states
  window      the window: tabs, command bar, start page, printing, properties
  presentation  one page at a time on a black screen
"""

APP_ID = "com.boswas.DocumentViewer"
DISPLAY_NAME = "Document Viewer"
PROGRAM = "boswas-document-viewer"
