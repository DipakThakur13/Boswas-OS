"""Everything Control Center can read or do, as blocking calls.

Sources, all read-only except the five actions at the end:

  boswas --json status        posture checks of boswas-cli (exit 1 = a check FAILed)
  device agent socket         agent.status, device.config, runtime.status
                              (operations open to any local user)
  session agent socket        apps.list, system.status (the user's own service)
  boswas-preset --json list   the desktop presets and the current one
  apt-config shell            APT::Periodic settings (read-only)
  probes.py                   public files (release, hardware, storage, power,
                              APT stamps, desktop entries, KDE module plugins)

Actions: open a KDE settings module (kcmshell6 <module>), start a program
from the catalog, open an application (kstart --application <id>), apply a
preset (boswas-preset apply <id> [--layout]) and, in a live session only,
restart (systemctl reboot). Every variable argument is validated here before
commands.argv() builds the argument list.

All methods block; the widgets call them from worker threads.
"""

from __future__ import annotations

import json
import os

from boswas_agent.localapi import ApiClient, ApiError

from . import commands, probes
from . import viewmodel as vm
from .catalog import KCM, KCM_MODULES, PROGRAM_MODULES, PROGRAMS, Module
from .commands import CommandRunner, argv
from .errors import CommandFailed, CommandUnavailable, ServiceRefused, ServiceUnavailable, Unavailable

AGENT_SOCKET = "/run/boswas-agent/agent.sock"
AGENT_TIMEOUT = 10.0
RUNTIME_TIMEOUT = 75.0       # the agent may run the WinCompat runtime check (up to 60 s) on first request
SESSION_TIMEOUT = 30.0
SESSION_SYSTEM_TIMEOUT = 120.0
STATUS_TIMEOUT = 60.0
PRESET_LIST_TIMEOUT = 20.0
PRESET_APPLY_TIMEOUT = 180.0
APT_CONFIG_TIMEOUT = 10.0
PLASMA_TIMEOUT = 5.0
REBOOT_TIMEOUT = 30.0

SESSION, AGENT = "session", "agent"
# The only operations this client sends. Both lists are read-only on purpose.
SESSION_OPERATIONS = frozenset({"apps.list", "system.status"})
AGENT_OPERATIONS = frozenset({"agent.status", "device.config", "runtime.status"})


def session_socket_path(environ=None) -> str:
    environ = os.environ if environ is None else environ
    runtime = environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    return os.path.join(runtime, "boswas", "session.sock")


class Backend:
    def __init__(self, *, root: str = "/", environ=None, commands_runner=None, client_factory=ApiClient,
                 session_socket: str | None = None, agent_socket: str = AGENT_SOCKET):
        self.root = probes.Root(root)
        self.environ = os.environ if environ is None else environ
        self.commands = commands_runner or CommandRunner(self.environ)
        self.session_socket = session_socket or session_socket_path(self.environ)
        self._session = client_factory(self.session_socket, timeout=SESSION_TIMEOUT)
        self._agent = client_factory(agent_socket, timeout=AGENT_TIMEOUT)

    # --- transport ------------------------------------------------------------------------------------
    @staticmethod
    def _call(client, service: str, op: str, timeout: float | None) -> dict:
        name = "session service" if service == SESSION else "device agent"
        try:
            result = client.call(op, timeout=timeout)
        except ApiError as exc:
            raise ServiceRefused(str(exc.code), f"The {name} refused the request: {vm.one_line(exc.message, 300)}") \
                from None
        except OSError as exc:              # ConnectionError included: the service is not running
            raise ServiceUnavailable(service, f"The Boswas {name} is not running ({vm.one_line(exc, 200)}).") \
                from None
        if not isinstance(result, dict):
            raise ServiceRefused("BAD_RESPONSE", f"The {name} returned an unexpected answer to {op}.")
        return result

    def _session_call(self, op: str, timeout: float | None = None) -> dict:
        if op not in SESSION_OPERATIONS:
            raise ValueError(f"{op} is not an allowed session operation")
        return self._call(self._session, SESSION, op, timeout)

    def _agent_call(self, op: str, timeout: float | None = None) -> dict:
        if op not in AGENT_OPERATIONS:
            raise ValueError(f"{op} is not an allowed device agent operation")
        return self._call(self._agent, AGENT, op, timeout)

    def _run(self, command: list[str], timeout: float) -> commands.Completed:
        if not self.commands.available(command[0]):
            raise CommandUnavailable(commands.program_name(command))
        return self.commands.run(command, timeout)

    def _start(self, command: list[str]) -> None:
        if not self.commands.available(command[0]):
            raise CommandUnavailable(commands.program_name(command))
        self.commands.start(command)

    # --- device agent and session agent (read-only) ---------------------------------------------------------
    def agent_status(self) -> dict:
        return self._agent_call("agent.status")

    def agent_runtime(self) -> dict:
        return self._agent_call("runtime.status", RUNTIME_TIMEOUT)

    def agent_config(self) -> dict:
        """The device's privacy and update settings: from the agent, else from /etc/boswas/device.conf."""
        try:
            return self._agent_call("device.config")
        except ServiceUnavailable:
            return probes.device_settings_file(self.root)

    def session_system(self) -> dict:
        return self._session_call("system.status", SESSION_SYSTEM_TIMEOUT)

    def windows_apps(self) -> list[dict]:
        apps = self._session_call("apps.list").get("applications")
        return [a for a in apps if isinstance(a, dict)] if isinstance(apps, list) else []

    # --- commands that read ------------------------------------------------------------------------------
    def security_status(self) -> dict:
        command = argv("boswas", "--json", "status")
        result = self._run(command, STATUS_TIMEOUT)
        if result.code not in (0, 1):            # 1 = at least one check FAILed; the document is complete
            raise commands.failure(command, result, "The security check")
        try:
            doc = json.loads(result.stdout)
        except ValueError:
            raise CommandFailed("boswas", "The boswas tool returned output that is not JSON.") from None
        if not isinstance(doc, dict) or not isinstance(doc.get("checks"), list):
            raise CommandFailed("boswas", "The boswas tool returned an unexpected document.")
        return doc

    def presets(self) -> vm.PresetList:
        command = argv("boswas-preset", "--json", "list")
        try:
            result = self._run(command, PRESET_LIST_TIMEOUT)
        except CommandUnavailable:
            raise CommandUnavailable("boswas-preset", "Boswas presets are not available on this system "
                                                      "(boswas-preset is not installed).") from None
        if result.code != 0:
            raise commands.failure(command, result, "Listing presets")
        try:
            return vm.parse_presets(json.loads(result.stdout))
        except ValueError:
            raise CommandFailed("boswas-preset", "boswas-preset returned an unexpected list of presets.") from None

    def plasma_version(self) -> str | None:
        version = probes.plasma_metainfo_version(self.root)
        if version:
            return version
        command = argv("plasmashell", "--version")
        try:
            result = self._run(command, PLASMA_TIMEOUT)
        except (Unavailable, CommandFailed):
            return None
        return vm.plasma_version_from_output(result.stdout) if result.code == 0 else None

    def updates(self) -> dict:
        periodic, source = {}, None
        names = [part for var, name in vm.APT_SHELL_VARS for part in (var, f"APT::Periodic::{name}")]
        command = argv("apt-config", "shell", *names)
        try:
            result = self._run(command, APT_CONFIG_TIMEOUT)
            if result.code == 0:
                periodic, source = vm.parse_apt_shell(result.stdout), "apt-config"
        except (Unavailable, CommandFailed):
            pass
        if source is None:
            periodic = vm.parse_apt_conf(probes.apt_conf_texts(self.root))
            source = "files" if periodic else None
        release = probes.release(self.root)
        info = vm.release_info(release)
        return {"periodic": periodic, "source": source, "stamps": probes.apt_stamps(self.root),
                "log": probes.unattended_log(self.root), "channel": info.channel,
                "repository": release["update"].get("REPOSITORY", "")}

    # --- files ---------------------------------------------------------------------------------------------
    def release(self) -> dict:
        return probes.release(self.root)

    def hardware(self) -> dict:
        return probes.hardware(self.root)

    def storage(self) -> list[dict]:
        return probes.storage(self.root)

    def power(self) -> list[dict]:
        return probes.power_supplies(self.root)

    def live_session(self) -> bool:
        return probes.live_session(self.root)

    def desktop_entries(self) -> list[vm.DesktopEntry]:
        locale = self.environ.get("LC_ALL") or self.environ.get("LC_MESSAGES") or self.environ.get("LANG")
        return probes.desktop_entries(self.environ, locale)

    def kcm_availability(self) -> dict[str, bool]:
        return {module: probes.kcm_installed(self.root, module) for module in sorted(KCM_MODULES)}

    def program_availability(self) -> dict[str, bool]:
        result = {key: self.commands.available(commands.EXECUTABLES[PROGRAMS[key][0]]) for key in PROGRAM_MODULES}
        for name in ("kstart", "kioclient", "boswas-preset", "systemctl"):
            result[name] = self.commands.available(commands.EXECUTABLES[name])
        return result

    # --- actions -----------------------------------------------------------------------------------------------
    @staticmethod
    def module_command(module: Module) -> list[str]:
        if module.kind == KCM:
            if module.id not in KCM_MODULES:
                raise ValueError(f"{module.id!r} is not a module of the catalog")
            return argv("kcmshell6", module.id)
        if module.id not in PROGRAMS:
            raise ValueError(f"{module.id!r} is not a program of the catalog")
        name, args = PROGRAMS[module.id]
        return argv(name, *args)

    def open_module(self, module: Module) -> None:
        self._start(self.module_command(module))

    def launch_command(self, item: vm.AppItem) -> list[str]:
        if not item.launchable or not vm.valid_desktop_id(item.desktop_id):
            raise ValueError("this application cannot be opened from Control Center")
        if self.commands.available(commands.EXECUTABLES["kstart"]):
            return argv("kstart", "--application", item.desktop_id)
        if item.path.startswith("/") and item.path.endswith(".desktop"):
            return argv("kioclient", "exec", item.path)
        raise CommandUnavailable("kstart", "Applications cannot be opened: kstart and kioclient are not installed.")

    def launch(self, item: vm.AppItem) -> None:
        self._start(self.launch_command(item))

    @staticmethod
    def preset_command(preset_id: str, layout: bool = False) -> list[str]:
        if not vm.valid_preset_id(preset_id):
            raise ValueError(f"invalid preset ID {preset_id!r}")
        return argv("boswas-preset", "apply", preset_id, *(["--layout"] if layout else []))

    def apply_preset(self, preset_id: str, layout: bool = False) -> None:
        command = self.preset_command(preset_id, layout)
        result = self._run(command, PRESET_APPLY_TIMEOUT)
        if result.code != 0:
            raise commands.failure(command, result, "Applying the preset")

    def restart(self) -> None:
        """Restart the computer (offered in live sessions, to reach the installer boot entry)."""
        command = argv("systemctl", "reboot")
        result = self._run(command, REBOOT_TIMEOUT)
        if result.code != 0:
            raise commands.failure(command, result, "Restarting")
