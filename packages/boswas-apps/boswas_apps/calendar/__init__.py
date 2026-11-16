"""Boswas Calendar: a local calendar with iCalendar import and export. See boswas-calendar(1).

Modules without Qt
  recurrence  recurrence rules (RRULE): parsing, writing, expanding within bounds
  model       calendars, events, occurrences and the edits on them
  ical        reading and writing iCalendar files (RFC 5545)
  layout      where events go in a month grid and in a day column
  settings    the user's settings

Modules with Qt
  store       the data in a running Calendar: saving off the GUI thread, undo
  ui.*        the window, the views, the dialogs, printing
"""

APP_ID = "com.boswas.Calendar"
