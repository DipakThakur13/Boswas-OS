"""Boswas Control Plane: device registry, typed commands, signed policies,
application catalog and audit trail for Boswas OS devices.

A modular monolith in the Python standard library with SQLite storage
(ADR-0017); every module talks to storage through store.Store, so another
database can replace SQLite behind the same interface.

  store.py     schema, migrations, repository
  service.py   domain rules (registry, heartbeat, inventory, commands,
               catalog, policies, artifacts, enrollment, events)
  auth.py      operator authentication (local API tokens; the attachment
               point for a future identity provider) and roles
  pki.py       the device CA, the server certificate, the policy key
  api.py       HTTP API /api/v1 and the dashboard
  server.py    TLS server (mutual TLS for devices)
  sweeper.py   offline detection and command expiry
  cli.py       boswas-cp administration command
"""

__version__ = "1.0~alpha3"
API_VERSION = "v1"
