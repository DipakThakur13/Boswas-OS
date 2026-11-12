"""System Overview rows of the About page (pure Python, no Qt, no I/O).

Only data already read is shown: the load (/proc), the network state
(nmcli), pending updates only if the Update Center already simulated them in
this window, the firmware report only if it was run in this window, and the
Android runtime's status if boswas-android is installed. Nothing here starts
an operation; unknown values say so.
"""

from __future__ import annotations

from . import android, sysinfo
from . import viewmodel as vm


def overview_rows(load: object, network, network_error: str | None, upgradable, firmware,
                  android_doc: object, android_unavailable: bool) -> list[tuple[str, str]]:
    rows = sysinfo.load_rows(load)
    if isinstance(network, sysinfo.NetworkState):
        rows.append(("Network", network.summary_line))
    else:
        rows.append(("Network", "Unknown" if network_error else "Checking…"))
    if upgradable is not None and hasattr(upgradable, "upgrades"):
        count = len(upgradable.upgrades)
        security = upgradable.security_count
        rows.append(("Pending updates", "None in the current package lists" if not count else
                     f"{count}" + (f" ({security} security)" if security else "")))
    else:
        rows.append(("Pending updates", "Not checked in this window (open Updates)"))
    if firmware is not None:
        _source, missing = firmware
        rows.append(("Firmware", "No missing firmware reported" if not missing else
                     f"{len(missing)} missing firmware file{'s' if len(missing) != 1 else ''} (see Hardware & "
                     "Drivers)"))
    else:
        rows.append(("Firmware", "Not checked (Hardware & Drivers)"))
    if android_unavailable:
        rows.append(("Android apps", "Not available (boswas-android is not installed)"))
    elif isinstance(android_doc, dict):
        view = android.android_view(android_doc)
        rows.append(("Android apps", view.summary.headline))
    else:
        rows.append(("Android apps", "Unknown"))
    return rows
