"""Boswas Clock: alarms, timers, a stopwatch, a world clock and focus sessions. See boswas-clock(1).

Modules without Qt
  model     the document ``clock.json`` and the arithmetic on it
  cities    the places the world clock knows
  settings  the user's settings

Modules with Qt
  store     the document in a running Clock: changes are saved off the GUI thread
  ui.*      the window and its pages
"""

APP_ID = "com.boswas.Clock"
