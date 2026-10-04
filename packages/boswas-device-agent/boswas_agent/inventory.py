"""Inventory collectors.

Milestone 1 provides the Windows application collector, the interface
between WinCompat (boswas-winapp) and the agent. It runs
`boswas-winapp --json list --all-users` as root and aggregates per
application: which applications and versions exist on the device and how
many users installed them. User names are not reported.
"""

from __future__ import annotations

import json
import subprocess
from collections import Counter

from .interfaces import InventoryCollector
from .models import ApplicationInventoryItem

WINAPP_COMMAND = ("/usr/bin/boswas-winapp", "--json", "list", "--all-users")


class WindowsApplicationsCollector(InventoryCollector):
    name = "windows-applications"

    def __init__(self, run=subprocess.run, command=WINAPP_COMMAND):
        self._run = run
        self._command = list(command)

    def collect(self) -> dict:
        try:
            proc = self._run(self._command, capture_output=True, text=True, timeout=60, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            return {"available": False, "error": type(exc).__name__, "applications": []}
        if proc.returncode != 0:
            return {"available": False, "error": f"exit {proc.returncode}", "applications": []}
        try:
            doc = json.loads(proc.stdout)
            users = doc["users"]
        except (ValueError, KeyError, TypeError):
            return {"available": False, "error": "unreadable output", "applications": []}
        counts: Counter = Counter()
        for user in users:
            for app in user.get("applications", []):
                if app.get("state") == "installed" and isinstance(app.get("id"), str):
                    counts[(app["id"], app.get("version"), app.get("status"))] += 1
        items = [ApplicationInventoryItem(kind="winapp", id=i, version=v, status=s, installations=n)
                 for (i, v, s), n in sorted(counts.items(), key=lambda kv: kv[0][0])]
        return {"available": True, "applications": [item.to_dict() for item in items]}
