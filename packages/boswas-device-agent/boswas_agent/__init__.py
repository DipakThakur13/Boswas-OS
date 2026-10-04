"""Boswas device agent: device identity, state, inventory, local management
API and the conversation with the Boswas Control Plane.

Components (docs/device-management/README.md):
  boswas-device-agent   the system service (daemon.py)
  boswas-session-agent  the per-user service behind the Compatibility Manager
                        and remote application commands (session_agent.py)
  boswas-device         the command-line tool (cli.py)

Shared with the Control Plane: models.py, commands.py, policydoc.py,
privacy.py and the JSON schemas in packages/boswas-device-agent/schemas/.
"""

__version__ = "1.0~alpha3"

# Control Plane API version the models describe (/api/v1/...).
API_VERSION = "v1"
