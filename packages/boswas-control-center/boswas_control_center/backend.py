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

import time

from boswas_agent.localapi import ApiClient, ApiError

from . import android, commands, filetypes, hardware, jobs, probes, software, sysinfo, userfiles
from . import viewmodel as vm
from .catalog import KCM, KCM_MODULES, PROGRAM_MODULES, PROGRAMS, Module
from .commands import CommandRunner, android_argv, argv, sysops_argv
from .errors import CommandFailed, CommandUnavailable, Failed, ServiceRefused, ServiceUnavailable, Unavailable

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
APT_SIMULATE_TIMEOUT = 120.0
APT_CACHE_TIMEOUT = 60.0
FLATPAK_QUERY_TIMEOUT = 90.0
FLATPAK_CHANGE_TIMEOUT = 3600.0
ANDROID_STATUS_TIMEOUT = 90.0
ANDROID_QUERY_TIMEOUT = 120.0
NMCLI_TIMEOUT = 20.0
LSBLK_TIMEOUT = 20.0
CPU_SAMPLE = 0.5
FLATPAK_REMOTE_FILES = "/usr/share/boswas/sysops/flatpak-remotes"

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
        for name in ("kstart", "kioclient", "boswas-preset", "systemctl", "boswas-android", "nmcli", "lsblk",
                     "flatpak", "apt-cache"):
            result[name] = self.commands.available(commands.EXECUTABLES[name])
        result["admin"] = self.commands.available(commands.EXECUTABLES[commands.ADMIN_TOOL])
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

    # --- long operations (boswas-sysops with administrator authorization, boswas-android) ------------------
    @staticmethod
    def job_command(spec: jobs.JobSpec) -> list[str]:
        if spec.kind == jobs.SYSOPS:
            return sysops_argv(*spec.words)
        if spec.kind == jobs.ANDROID:
            return android_argv(*spec.words)
        raise ValueError(f"unknown job kind {spec.kind!r}")

    def run_job(self, spec: jobs.JobSpec, on_event, handle: commands.StreamHandle) -> jobs.Outcome:
        """Run one long operation; on_event(Event) is called on this (worker) thread for every line."""
        command = self.job_command(spec)
        if not self.commands.available(command[0]):
            if spec.kind == jobs.ANDROID:
                raise CommandUnavailable("boswas-android", android.NOT_INSTALLED_MANAGER)
            raise CommandUnavailable(commands.ADMIN_TOOL, commands.NO_ADMIN)
        state = jobs.JobState()

        def line(text: str) -> None:
            event = jobs.parse_line(text)
            state.feed(event)
            on_event(event)

        try:
            code = self.commands.stream(command, line, handle, spec.timeout)
        except CommandFailed as exc:
            return jobs.Outcome(False, jobs.TIMEOUT, exc.message, detail=vm.one_line(" ".join(state.text[-3:])))
        if handle.killed and state.result is None:
            return jobs.Outcome(False, "CANCELLED", jobs.CODE_HINTS["CANCELLED"])
        return jobs.outcome(state, code, spec.kind)

    # --- updates (read-only simulation) ------------------------------------------------------------------------
    def upgradable(self) -> software.UpgradePlan:
        """What installing updates would change, simulated by APT from the package lists on the device."""
        command = argv("apt-get", "-s", "-o", "Debug::NoLocking=1", "--with-new-pkgs", "upgrade")
        result = self._run(command, APT_SIMULATE_TIMEOUT)
        if result.code != 0:
            raise commands.failure(command, result, "Checking for pending updates")
        return software.parse_simulated_upgrade(result.stdout)

    # --- Debian packages (read-only) ------------------------------------------------------------------------------
    def apt_policy(self, names: list[str]) -> dict[str, dict]:
        names = [n for n in names if commands.valid_package(n)][:software.SEARCH_LIMIT]
        if not names:
            return {}
        command = argv("apt-cache", "policy", *names)
        result = self._run(command, APT_CACHE_TIMEOUT)
        if result.code != 0:
            raise commands.failure(command, result, "Reading package versions")
        return software.parse_apt_policy(result.stdout)

    def apt_search(self, term: str) -> list[software.PackageHit]:
        if not software.valid_search(term):
            raise ValueError("Search with letters, digits, spaces and + . _ - only.")
        command = argv("apt-cache", "search", "--names-only", term.strip())
        result = self._run(command, APT_CACHE_TIMEOUT)
        if result.code != 0:
            raise commands.failure(command, result, "Searching packages")
        hits = software.parse_apt_search(result.stdout)
        return software.hits_with_policy(hits, self.apt_policy([name for name, _s in hits]))

    def apt_details(self, name: str) -> dict:
        if not commands.valid_package(name):
            raise ValueError(f"invalid package name {name!r}")
        command = argv("apt-cache", "show", name)
        result = self._run(command, APT_CACHE_TIMEOUT)
        if result.code != 0:
            raise commands.failure(command, result, f"Reading details of {name}")
        fields = software.parse_apt_show(result.stdout)
        policy = self.apt_policy([name]).get(name, {})
        fields["Installed"] = policy.get("installed", "")
        fields["Candidate"] = policy.get("candidate", "")
        fields["Source"] = software.source_label([s for s in policy.get("sources", [])
                                                  if "/var/lib/dpkg/status" not in s] or policy.get("sources", []))
        return fields

    # --- Flatpak (user installation; remotes are enabled by the administrator) ----------------------------------
    def flatpak(self) -> software.FlatpakState:
        remotes: list[software.FlatpakRemote] = []
        problems: list[str] = []
        for installation in ("system", "user"):
            command = argv("flatpak", "remotes", f"--{installation}", "--columns=name,url,options")
            result = self._run(command, FLATPAK_QUERY_TIMEOUT)
            if result.code == 0:
                remotes.extend(software.parse_flatpak_remotes(result.stdout, installation))
            else:
                problems.append(f"The {installation} Flatpak remotes could not be listed.")
        apps: list[software.FlatpakApp] = []
        for installation in ("user", "system"):
            command = argv("flatpak", "list", f"--{installation}", "--app",
                           "--columns=application,name,version,origin")
            result = self._run(command, FLATPAK_QUERY_TIMEOUT)
            if result.code == 0:
                apps.extend(software.parse_flatpak_list(result.stdout, installation))
        shipped = tuple(sorted(name[:-len(".flatpakrepo")] for name in self.root.listdir(FLATPAK_REMOTE_FILES)
                               if name.endswith(".flatpakrepo") and commands.REMOTE_RE.fullmatch(
                                   name[:-len(".flatpakrepo")])))
        return software.FlatpakState(tuple(remotes), tuple(apps), shipped, tuple(problems))

    def flatpak_search(self, term: str) -> list[software.FlatpakApp]:
        if not software.valid_search(term):
            raise ValueError("Search with letters, digits, spaces and + . _ - only.")
        command = argv("flatpak", "search", "--columns=application,name,description,version,remotes", term.strip())
        result = self._run(command, FLATPAK_QUERY_TIMEOUT)
        if result.code != 0:
            raise commands.failure(command, result, "Searching Flatpak applications")
        return software.parse_flatpak_search(result.stdout)

    def flatpak_install(self, remote: str, app_id: str, state: software.FlatpakState) -> None:
        """Install for this user. A remote the administrator enabled system-wide is first added to the user's
        installation from the same pinned .flatpakrepo file Boswas ships (with its signing key)."""
        if not commands.REMOTE_RE.fullmatch(remote or "") or not software.valid_flatpak_app(app_id):
            raise ValueError("invalid Flatpak remote or application ID")
        user_remotes = {r.name for r in state.enabled("user")}
        if remote not in user_remotes:
            if remote not in {r.name for r in state.enabled("system")} or remote not in state.shipped:
                raise Failed(f"The remote {remote} is not enabled by your administrator.")
            command = argv("flatpak", "remote-add", "--user", "--if-not-exists", remote,
                           f"{FLATPAK_REMOTE_FILES}/{remote}.flatpakrepo")
            result = self._run(command, FLATPAK_QUERY_TIMEOUT)
            if result.code != 0:
                raise commands.failure(command, result, f"Adding the {remote} remote for your account")
        command = argv("flatpak", "install", "--user", "--noninteractive", "-y", remote, app_id)
        result = self._run(command, FLATPAK_CHANGE_TIMEOUT)
        if result.code != 0:
            raise commands.failure(command, result, f"Installing {app_id}")

    def flatpak_uninstall(self, app_id: str) -> None:
        if not software.valid_flatpak_app(app_id):
            raise ValueError("invalid Flatpak application ID")
        command = argv("flatpak", "uninstall", "--user", "--noninteractive", "-y", app_id)
        result = self._run(command, FLATPAK_CHANGE_TIMEOUT)
        if result.code != 0:
            raise commands.failure(command, result, f"Removing {app_id}")

    def flatpak_update(self) -> None:
        command = argv("flatpak", "update", "--user", "--noninteractive", "-y")
        result = self._run(command, FLATPAK_CHANGE_TIMEOUT)
        if result.code != 0:
            raise commands.failure(command, result, "Updating Flatpak applications")

    # --- AppImages and files chosen by the user ---------------------------------------------------------------
    def appimages(self) -> list[userfiles.AppImage]:
        return userfiles.list_appimages(self.environ)

    def add_appimage(self, path: str) -> userfiles.AppImage:
        kind = filetypes.detect(path)
        if kind.kind != filetypes.APPIMAGE:
            raise Failed(f"This file is not an AppImage ({kind.description}).")
        try:
            return userfiles.add_appimage(self.environ, path)
        except userfiles.UserFileError as exc:
            raise Failed(str(exc)) from None

    def remove_appimage(self, ident: str) -> None:
        try:
            userfiles.remove_appimage(self.environ, ident)
        except userfiles.UserFileError as exc:
            raise Failed(str(exc)) from None

    @staticmethod
    def detect_file(path: str) -> filetypes.FileKind:
        try:
            return filetypes.detect(path)
        except ValueError as exc:
            raise Failed(str(exc)) from None

    def open_windows_installer(self, path: str) -> None:
        """Open the Compatibility Manager's install flow for a Windows program or MSI package."""
        kind = self.detect_file(path)
        if kind.kind not in (filetypes.WINDOWS, filetypes.MSI):
            raise Failed(f"This file is not a Windows program ({kind.description}).")
        self._start(argv("boswas-compat-manager", "--install", kind.path))

    # --- Android (boswas-android) ---------------------------------------------------------------------------
    def _android(self, words: tuple[str, ...], timeout: float) -> dict:
        command = android_argv(*words)
        if not self.commands.available(command[0]):
            raise CommandUnavailable("boswas-android", android.NOT_INSTALLED_MANAGER)
        result = self.commands.run(command, timeout)
        doc = None
        for line in result.stdout.splitlines():
            line = line.strip()
            if line.startswith("{"):
                try:
                    candidate = json.loads(line)
                except ValueError:
                    continue
                if isinstance(candidate, dict) and candidate.get("schema") == android.SCHEMA:
                    doc = candidate
        if doc is None:
            try:
                candidate = json.loads(result.stdout)
                doc = candidate if isinstance(candidate, dict) and candidate.get("schema") == android.SCHEMA else None
            except ValueError:
                doc = None
        error = jobs.android_error(doc) if doc is not None else None
        if error is not None or result.code != 0:
            code, message, problems = error or ("", "", "")
            text = message + ("" if not problems else (" " if message.endswith((".", "!", "?")) or not message
                                                     else ". ") + problems)
            raise CommandFailed("boswas-android", text or commands.failure(command, result,
                                                                           "The Android runtime manager").message,
                                result.code)
        if doc is None:
            raise CommandFailed("boswas-android", "The Android runtime manager returned an unexpected answer.")
        return doc

    def android_status(self) -> dict:
        return self._android(("status",), ANDROID_STATUS_TIMEOUT)

    def android_apps(self) -> list[android.AndroidApp]:
        return android.android_apps(self._android(("app", "list"), ANDROID_QUERY_TIMEOUT))

    def android_inspect(self, path: str) -> android.ApkView:
        kind = self.detect_file(path)
        if kind.kind != filetypes.APK:
            raise Failed(f"This file is not an Android app ({kind.description}).")
        return android.apk_view(self._android(("apk", "inspect", kind.path), ANDROID_QUERY_TIMEOUT))

    def android_launch(self, package: str) -> None:
        self._android(("app", "launch", package), ANDROID_QUERY_TIMEOUT)

    # --- hardware ------------------------------------------------------------------------------------------------
    def pci(self) -> list[hardware.Device]:
        raw = probes.pci_devices(self.root)
        text = probes.ids_text(self.root, probes.PCI_IDS)
        names = hardware.parse_ids(text, hardware.wanted_ids(raw)) if text else None
        return hardware.pci_rows(raw, names)

    def usb(self) -> list[hardware.Device]:
        raw = probes.usb_devices(self.root)
        text = probes.ids_text(self.root, probes.USB_IDS)
        names = hardware.parse_ids(text, hardware.wanted_ids(raw, "product_id")) if text else None
        return hardware.usb_rows(raw, names)

    # --- storage ------------------------------------------------------------------------------------------------
    def disks(self) -> list[sysinfo.Disk]:
        command = argv("lsblk", "-J", "-b", "-o", "NAME,TYPE,SIZE,FSTYPE,MOUNTPOINTS,MODEL,TRAN,RM,RO")
        result = self._run(command, LSBLK_TIMEOUT)
        if result.code != 0:
            raise commands.failure(command, result, "Listing disks")
        try:
            return sysinfo.parse_lsblk(result.stdout)
        except ValueError as exc:
            raise CommandFailed("lsblk", str(exc)) from None

    def thumbnails(self) -> tuple[int, int]:
        try:
            return userfiles.thumbnail_usage(self.environ)
        except userfiles.UserFileError as exc:
            raise Failed(str(exc)) from None

    def clean_thumbnails(self) -> int:
        try:
            return userfiles.clean_thumbnails(self.environ)
        except userfiles.UserFileError as exc:
            raise Failed(str(exc)) from None

    # --- network (NetworkManager decides what the user may change) ----------------------------------------------
    def network(self) -> sysinfo.NetworkState:
        command = argv("nmcli", "-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "device")
        result = self._run(command, NMCLI_TIMEOUT)
        if result.code != 0:
            raise commands.failure(command, result, "Reading network devices")
        devices = []
        for device in sysinfo.parse_nmcli_devices(result.stdout):
            if device.connected:
                show = self._run(argv("nmcli", "-t", "-f", "IP4.ADDRESS,IP4.GATEWAY,IP6.ADDRESS", "device", "show",
                                      device.device), NMCLI_TIMEOUT)
                if show.code == 0:
                    addresses, gateway = sysinfo.parse_nmcli_show(show.stdout)
                    device = sysinfo.NetDevice(device.device, device.kind, device.state, device.connection,
                                               addresses, gateway)
            devices.append(device)
        radio = self._run(argv("nmcli", "-t", "radio", "wifi"), NMCLI_TIMEOUT)
        wifi = sysinfo.parse_radio(radio.stdout) if radio.code == 0 else None
        return sysinfo.NetworkState(tuple(devices), wifi)

    def set_wifi(self, enabled: bool) -> None:
        command = argv("nmcli", "radio", "wifi", "on" if enabled else "off")
        result = self._run(command, NMCLI_TIMEOUT)
        if result.code != 0:
            raise commands.failure(command, result, "Switching Wi-Fi " + ("on" if enabled else "off"))

    def device_connection(self, device: str, connect: bool) -> None:
        if not sysinfo.valid_device(device):
            raise ValueError(f"invalid network device {device!r}")
        command = argv("nmcli", "device", "connect" if connect else "disconnect", device)
        result = self._run(command, NMCLI_TIMEOUT * 3)
        if result.code != 0:
            raise commands.failure(command, result, ("Connecting " if connect else "Disconnecting ") + device)

    # --- load ------------------------------------------------------------------------------------------------------
    def load(self) -> dict:
        first = sysinfo.parse_proc_stat(probes.proc_stat(self.root))
        time.sleep(CPU_SAMPLE)
        second = sysinfo.parse_proc_stat(probes.proc_stat(self.root))
        return {"cpu_percent": sysinfo.cpu_percent(first, second),
                "memory": sysinfo.parse_memory(probes.meminfo(self.root))}
