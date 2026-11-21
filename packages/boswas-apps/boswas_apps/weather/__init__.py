"""Boswas Weather: the forecast for the places you choose, from Open-Meteo. See boswas-weather(1).

Modules without Qt
  model     the answers of the weather service as dataclasses, the WMO codes, units and formatting
  places    the user's places (``places.json``)
  settings  the user's settings (``settings.json``)
  service   addresses, the HTTP call, the forecast cache
  storage   reading and atomically writing small JSON files

Modules with Qt
  ui.*      the window and its pages
"""

APP_ID = "com.boswas.Weather"
