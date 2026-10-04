"""Boswas device agent: contracts between a Boswas OS device and the Control Plane.

Milestone 1 provides the foundation only: data models, interfaces, device
identity handling, privacy guards and JSON schemas. The service
(boswas-device-agent.service), its Debian package and any network client
arrive in Milestone 2. See docs/device-management/README.md.
"""

__version__ = "1.0~alpha2"

# Control Plane API version the models describe (/api/v1/...).
API_VERSION = "v1"
