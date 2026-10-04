"""Boswas Compatibility Manager: the desktop front end for Windows applications.

The application runs as the user and talks only to two local services:

  boswas-session-agent  $XDG_RUNTIME_DIR/boswas/session.sock  every application operation
  boswas-device-agent   /run/boswas-agent/agent.sock          read-only device status

It never starts processes and never touches application, policy or Wine
files itself; installing, launching, confinement and policy stay entirely in
the trusted backend (docs/device-management/README.md).

Modules:
  backend.py    the two socket clients (the only way out of the GUI)
  viewmodel.py  presentation rules without Qt (filters, actions, install flow)
  ui/           Qt widgets
  app.py        entry point (boswas-compat-manager)
"""

__version__ = "1.0~alpha3"
