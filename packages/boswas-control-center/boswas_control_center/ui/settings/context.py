"""Where the sections of this package find the system: the environment and the command runner.

A section gets nothing but its parent, so it asks here for what it needs
when a test did not pass it explicitly:

    context.environ()   the environment (the home and XDG directories come from it)
    context.runner()    a commands.CommandRunner (timedatectl, localectl, ip, locale, ...)
    context.root        the file system root ("/")
    context.resolve     looks a host name up (the internet troubleshooter)
    context.fetch       asks an HTTPS address for its status (the internet troubleshooter)
    context.compositor  the compositor's settings and control socket (compositorconfig.Settings)

The tests of the whole window point these at their fake root, environment
and recording runner (cc_fixtures), so that showing a page never reads the
home directory or runs a program of whoever runs the tests.
"""

from __future__ import annotations

import os

from ... import commands, troubleshoot

root = "/"
resolve = troubleshoot.resolve_host
fetch = troubleshoot.fetch_status


def environ():
    return os.environ


def runner():
    return commands.CommandRunner()


def compositor():
    from ... import compositorconfig
    return compositorconfig.default()
