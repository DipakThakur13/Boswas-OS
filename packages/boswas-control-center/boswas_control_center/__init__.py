"""Boswas Control Center: the settings hub of Boswas OS.

Control Center is the user's entry point to the system's settings. It shows
the state of the device (security posture, updates, Windows compatibility,
device management, storage, power) and opens the matching KDE settings
modules for everything that is configured there, instead of
re-implementing them.

It runs as the desktop user and never as root. It reads:

  boswas --json status                 posture checks (boswas-cli)
  /run/boswas-agent/agent.sock         device agent, read-only operations
  $XDG_RUNTIME_DIR/boswas/session.sock session agent, read-only operations
  boswas-preset --json list            desktop presets
  public files under /usr/lib/boswas, /proc, /sys, /etc and /var/lib/apt

and starts only the programs listed in commands.EXECUTABLES, each with a
fixed argument list and a timeout, never through a shell.

Modules:
  catalog.py    pages and the KDE modules and programs each page offers (data)
  commands.py   the program allowlist and the only code that starts processes
  errors.py     the errors the window explains to the user
  probes.py     read-only file probes (release, hardware, storage, updates)
  backend.py    everything the window can read or do, as blocking calls
  viewmodel.py  presentation rules without Qt (status mapping, parsing)
  ui/           Qt widgets
  app.py        entry point (boswas-control-center)
"""

__version__ = "1.0~alpha3"
