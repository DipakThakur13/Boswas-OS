"""The troubleshooters of System > Troubleshoot: guided, read-only checks with plain-language results.

Each check takes what was read (no Qt; the readers are passed in, so the
tests give stand-ins) and returns a Report: one line per step, each Fine, a
Problem or a Note, with what helps.

  Internet connection  a network adapter that is up (ip -j addr), an address,
                       a default route (ip -j route), name servers
                       (/etc/resolv.conf); then whether a name is answered
                       (deb.debian.org through the system's resolver) and
                       whether an HTTPS request to https://deb.debian.org/
                       returns. That host, the Debian archive, is the only
                       place contacted, and the page says so before the check
                       runs.
  Sound                a sound server answers, an output device is there, it
                       is not muted, its volume (the services bridge)
  Bluetooth            the Bluetooth service runs (systemctl is-active), an
                       adapter is there and switched on (the services bridge)
  Display              the monitors the compositor reports (its outputs
                       command): which are on, at which mode

Nothing here changes a setting; the fixes are what the person can do, with
the page that does it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import servicebridge as sb

TEST_HOST = "deb.debian.org"
TEST_URL = f"https://{TEST_HOST}/"
CONTACTED = (f"This check contacts {TEST_HOST}, the server of the Debian archive, once: it looks up its name and "
             "opens its start page over HTTPS. Nothing about you or this device is sent.")
OK, PROBLEM, NOTE = "ok", "problem", "note"


@dataclass(frozen=True)
class Step:
    state: str                  # ok, problem or note
    title: str
    detail: str = ""
    fix: str = ""               # what helps, in words
    page: str = ""              # the place that does it, if Control Center has one


@dataclass(frozen=True)
class Report:
    check: str
    steps: tuple[Step, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        return not any(step.state == PROBLEM for step in self.steps)

    @property
    def headline(self) -> str:
        problems = [step for step in self.steps if step.state == PROBLEM]
        if not problems:
            return "No problem was found."
        return problems[0].title

    @property
    def fix(self) -> Step | None:
        return next((step for step in self.steps if step.state == PROBLEM and step.fix), None)


# --- internet --------------------------------------------------------------------------------------------

def internet(details, resolve=None, fetch=None) -> Report:
    """``details``: netinfo.Details. ``resolve(host)`` returns addresses or raises OSError; ``fetch(url)``
    returns the HTTP status or raises OSError. Without them the check stops after the local steps."""
    steps: list[Step] = []
    interfaces = list(details.interfaces) if details is not None else []
    up = [item for item in interfaces if item.up]
    if not interfaces:
        steps.append(Step(PROBLEM, "No network adapter was found",
                          "This device has no Wi-Fi or Ethernet adapter the system can use.",
                          "Look under Bluetooth & devices > Hardware & Drivers for a network device without a "
                          "driver, or connect a USB network adapter.", "hardware"))
        return Report("Internet connection", tuple(steps))
    if not up:
        steps.append(Step(PROBLEM, "No network adapter is connected",
                          ", ".join(f"{item.kind} ({item.name}): not connected" for item in interfaces),
                          "Connect to a Wi-Fi network, or plug in a network cable. Check that airplane mode is off.",
                          "wifi"))
        return Report("Internet connection", tuple(steps))
    names = ", ".join(f"{item.kind} ({item.name})" for item in up)
    steps.append(Step(OK, "A network adapter is connected", names))
    addressed = [item for item in up if item.ipv4 or item.ipv6]
    if not addressed:
        steps.append(Step(PROBLEM, "The connection has no address",
                          "The network did not give this device an address (DHCP).",
                          "Disconnect and connect again. If it keeps happening, restart the router.", "wifi"))
        return Report("Internet connection", tuple(steps))
    first = addressed[0]
    steps.append(Step(OK, "This device has an address", (first.ipv4 or first.ipv6)[0].split("/")[0]))
    gateways = [item for item in addressed if item.gateway4 or item.gateway6]
    if not gateways:
        steps.append(Step(PROBLEM, "There is no route to the internet",
                          "The network gave an address but no gateway (default route).",
                          "Connect to another network, or ask whoever runs this network.", "advanced-network"))
        return Report("Internet connection", tuple(steps))
    steps.append(Step(OK, "There is a route to the internet",
                      f"Gateway {gateways[0].gateway4 or gateways[0].gateway6}"))
    if not details.dns:
        steps.append(Step(PROBLEM, "No name server is set",
                          "Without a name server, addresses like deb.debian.org cannot be found.",
                          "Disconnect and connect again so that the network sends its name servers.",
                          "advanced-network"))
        return Report("Internet connection", tuple(steps))
    steps.append(Step(OK, "Name servers are set", ", ".join(details.dns[:3])))
    if resolve is None or fetch is None:
        return Report("Internet connection", tuple(steps))
    try:
        addresses = resolve(TEST_HOST)
    except OSError as exc:
        addresses = []
        reason = getattr(exc, "strerror", None) or str(exc)
    else:
        reason = ""
    if not addresses:
        steps.append(Step(PROBLEM, "Names on the internet are not found",
                          f"The name server did not answer for {TEST_HOST}" + (f" ({reason})." if reason else "."),
                          "The network's name server may be down. Disconnect and connect again, or restart the "
                          "router.", "advanced-network"))
        return Report("Internet connection", tuple(steps))
    steps.append(Step(OK, "Names on the internet are found", f"{TEST_HOST}: {addresses[0]}"))
    try:
        status = fetch(TEST_URL)
    except OSError as exc:
        text = getattr(exc, "reason", None) or getattr(exc, "strerror", None) or str(exc)
        steps.append(Step(PROBLEM, "Secure websites cannot be reached",
                          f"The request to {TEST_URL} failed: {str(text)[:200]}",
                          "The network may need a sign-in in the browser (hotel or café networks), or a proxy, or "
                          "a firewall blocks the connection.", "proxy"))
        return Report("Internet connection", tuple(steps))
    if not isinstance(status, int) or not 200 <= status < 400:
        steps.append(Step(PROBLEM, "A secure website did not answer as expected",
                          f"{TEST_URL} answered with status {status}.",
                          "The network may need a sign-in in the browser, or a proxy changes the answer.", "proxy"))
        return Report("Internet connection", tuple(steps))
    steps.append(Step(OK, "Secure websites can be reached", f"{TEST_URL} answered (status {status})."))
    return Report("Internet connection", tuple(steps))


# --- sound ----------------------------------------------------------------------------------------------

def sound(status: str, state) -> Report:
    """``status``: the bridge's status; ``state``: servicebridge.AudioState."""
    steps: list[Step] = []
    if status != sb.RUNNING or state is None or not state.available:
        steps.append(Step(PROBLEM, "The sound server does not answer",
                          "PipeWire, which plays sound on Boswas OS, did not answer.",
                          "Sign out and in again; the sound server starts with your session."))
        return Report("Sound", tuple(steps))
    steps.append(Step(OK, "The sound server runs"))
    output = state.output
    if output is None or not state.outputs:
        steps.append(Step(PROBLEM, "No speakers or headphones were found",
                          "The sound server has no output device.",
                          "Connect speakers or headphones. A USB or Bluetooth device appears as soon as it is "
                          "connected.", "sound"))
        return Report("Sound", tuple(steps))
    steps.append(Step(OK, "An output device is chosen", output.name or "Output"))
    if output.muted:
        steps.append(Step(PROBLEM, "The sound is muted", f"{output.name} is muted.", "Turn mute off on the Sound page "
                          "or in the quick settings (Super+A).", "sound"))
    elif output.volume < 0.05:
        steps.append(Step(PROBLEM, "The volume is at zero", f"{output.name}: {round(output.volume * 100)} %.",
                          "Turn the volume up on the Sound page or in the quick settings (Super+A).", "sound"))
    else:
        steps.append(Step(OK, "The volume is up", f"{round(output.volume * 100)} %"))
    if len(state.outputs) > 1:
        steps.append(Step(NOTE, f"{len(state.outputs)} output devices are connected",
                          "Sound plays on the chosen one; choose another on the Sound page if you hear nothing.",
                          page="sound"))
    return Report("Sound", tuple(steps))


# --- Bluetooth --------------------------------------------------------------------------------------------

def bluetooth(service: str | None, status: str, state) -> Report:
    """``service``: what `systemctl is-active bluetooth.service` printed (None: it could not be asked);
    ``state``: servicebridge.BluetoothState."""
    steps: list[Step] = []
    word = (service or "").strip()
    if word and word != "active":
        steps.append(Step(PROBLEM, "The Bluetooth service is not running",
                          f"bluetooth.service is {word}.",
                          "Bluetooth needs the package bluez; an administrator starts the service with "
                          "“systemctl enable --now bluetooth”."))
        return Report("Bluetooth", tuple(steps))
    if word == "active":
        steps.append(Step(OK, "The Bluetooth service runs"))
    if status != sb.RUNNING or state is None or not state.available:
        steps.append(Step(PROBLEM, "No Bluetooth adapter was found",
                          "This device has no Bluetooth adapter, or its driver or firmware is missing.",
                          "Look under Hardware & Drivers for missing firmware.", "hardware"))
        return Report("Bluetooth", tuple(steps))
    steps.append(Step(OK, "A Bluetooth adapter is there"))
    if not state.enabled:
        steps.append(Step(PROBLEM, "Bluetooth is turned off", "The adapter is switched off.",
                          "Turn Bluetooth on on the Bluetooth page or in the quick settings. Check that airplane "
                          "mode is off.", "bluetooth"))
        return Report("Bluetooth", tuple(steps))
    steps.append(Step(OK, "Bluetooth is on"))
    paired = [device for device in state.devices if device.paired]
    connected = [device for device in paired if device.connected]
    steps.append(Step(NOTE, f"{len(paired)} paired device{'s' if len(paired) != 1 else ''}, "
                            f"{len(connected)} connected",
                      "A device that does not connect: switch it off and on, then remove it and pair it again.",
                      page="bluetooth"))
    return Report("Bluetooth", tuple(steps))


# --- display ----------------------------------------------------------------------------------------------

def display(outputs, error: str = "") -> Report:
    """``outputs``: compositorconfig.Output list from the compositor, None when it could not be asked."""
    steps: list[Step] = []
    if outputs is None:
        steps.append(Step(PROBLEM, "The desktop did not report its monitors",
                          error or "No Boswas desktop answers in this session.",
                          "Monitors are listed while the Boswas desktop runs. Sign in to a Boswas session."))
        return Report("Display", tuple(steps))
    if not outputs:
        steps.append(Step(PROBLEM, "No monitor is connected", "The desktop reports no monitor.",
                          "Check the cable and that the monitor is switched on and set to the right input."))
        return Report("Display", tuple(steps))
    enabled = [output for output in outputs if output.enabled]
    for output in outputs:
        mode = output.mode
        if not output.enabled:
            text = "Turned off"
        elif mode is not None:
            text = f"{mode.width} × {mode.height} at {round(mode.refresh / 1000)} Hz, scale {round(output.scale * 100)} %"
        else:
            text = "On"
        title = output.label if output.label == output.name else f"{output.label} ({output.name})"
        steps.append(Step(OK if output.enabled else NOTE, title, text))
    if not enabled:
        steps.append(Step(PROBLEM, "Every monitor is turned off", "The desktop has no monitor in use.",
                          "Turn a monitor on on the Display page.", "display"))
    return Report("Display", tuple(steps))


# --- the two network probes the internet check uses (tests pass stand-ins) ------------------------------------

def resolve_host(host: str) -> list[str]:
    """The addresses the system's resolver gives for ``host`` (raises OSError when there is no answer)."""
    import socket
    answers = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(answer[4][0] for answer in answers))


def fetch_status(url: str, timeout: float = 15.0) -> int:
    """The HTTP status of a HEAD request to ``url`` (the session's proxy is used, as a browser would)."""
    import urllib.error
    import urllib.request
    if url != TEST_URL:
        raise ValueError("only the test address is requested")
    request = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "Boswas-Troubleshooter/1"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:      # noqa: S310 - fixed https address
            return int(response.status)
    except urllib.error.HTTPError as exc:
        return int(exc.code)
    except urllib.error.URLError as exc:
        raise OSError(str(exc.reason)) from None
