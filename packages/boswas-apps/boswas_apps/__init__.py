"""The Boswas OS system applications.

One Python package per application (boswas_apps.files, boswas_apps.terminal,
...), each built on the shared Fluent toolkit boswas_fluent and started by
its own program in /usr/bin. The applications run as the desktop user,
never as root, and share nothing at run time but the toolkit.
"""

__version__ = "3.0~alpha1"
