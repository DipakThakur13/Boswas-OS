"""A whole fake computer for the widget tests: /proc, /sys, desktop entries, systemd and logind stand-ins."""

from __future__ import annotations

import errno
import signal
import sys
from pathlib import Path

import tm_fixtures as fx

from boswas_apps.task_manager import actions
from boswas_apps.task_manager.bus import BusError
from boswas_apps.task_manager.sampler import Sampler
from boswas_apps.task_manager.services import Session, Unit
from boswas_apps.task_manager.ui.backend import Backend

SESSION = "0::/user.slice/user-1000.slice/session-1.scope\n"
USER_SERVICE = "0::/user.slice/user-1000.slice/user@1000.service/session.slice/pipewire.service\n"
SYSTEM = "0::/system.slice/{}.service\n"
USERS = {0: "root", 1000: "alice", 1001: "bob"}
ME, OWN_PID = 1000, 500


def populate(system: fx.FakeSystem) -> None:
    add = system.add_process
    add(1, "systemd", ppid=0, uid=0, cgroup="0::/init.scope\n", cmdline=["/sbin/init"], exe="/usr/lib/systemd/systemd")
    add(2, "kthreadd", ppid=0, uid=0, kthread=True)
    add(11, "kworker/0:1-events", ppid=2, uid=0, kthread=True, state="I")
    add(12, "ksoftirqd/0", ppid=2, uid=0, kthread=True)
    add(300, "sshd", uid=0, cgroup=SYSTEM.format("ssh"), cmdline=["sshd: /usr/sbin/sshd -D [listener]"],
        exe="/usr/sbin/sshd", resident=1500)
    add(310, "cupsd", uid=0, cgroup=SYSTEM.format("cups"), exe="/usr/sbin/cupsd", resident=2500)
    add(400, "boswas-composit", cmdline=["/usr/bin/boswas-compositor"], exe="/usr/bin/boswas-compositor",
        resident=30000, shared=10000, tty_nr=(4 << 8) | 2)
    add(410, "boswas-shell", ppid=400, exe="/usr/bin/boswas-shell", resident=20000, shared=8000)
    add(OWN_PID, "boswas-task-man", ppid=410, cmdline=["/usr/bin/python3", "-IB", "/usr/bin/boswas-task-manager"],
        exe=sys.executable, resident=25000, shared=9000, tty_nr=(4 << 8) | 2, start=5000)
    add(600, "firefox-esr", ppid=410, cmdline=["/usr/lib/firefox-esr/firefox-esr"], exe="/usr/lib/firefox-esr/firefox-esr",
        resident=90000, shared=30000, threads=60, io=(1000, 2000), start=6000)
    add(601, "Web Content", ppid=600, cmdline=["/usr/lib/firefox-esr/firefox-esr", "-contentproc", "tab"],
        exe="/usr/lib/firefox-esr/firefox-esr", resident=50000, shared=20000, threads=20, start=6100)
    add(602, "Web Content", ppid=600, cmdline=["/usr/lib/firefox-esr/firefox-esr", "-contentproc", "tab"],
        exe="/usr/lib/firefox-esr/firefox-esr", resident=40000, shared=20000, threads=18, start=6200)
    add(700, "boswas-terminal", ppid=410, cmdline=["/usr/bin/python3", "-IB", "/usr/bin/boswas-terminal"],
        exe="/usr/bin/python3.13", resident=22000, shared=9000, start=7000)
    add(701, "bash", ppid=700, exe="/usr/bin/bash", resident=1200, shared=900, tty_nr=(136 << 8) | 0, start=7100)
    add(702, "vim", ppid=701, cmdline=["vim", "notes with spaces.txt"], exe="/usr/bin/vim.basic", resident=3000,
        shared=1500, tty_nr=(136 << 8) | 0, nice=5, start=7200)
    add(800, "pipewire", cgroup=USER_SERVICE, exe="/usr/bin/pipewire", resident=4000, shared=2000, start=4100)
    add(900, "stubborn", ppid=410, exe="/opt/stubborn/stubborn", resident=1000, shared=500, start=8000)
    add(950, "bash", uid=1001, cgroup="0::/user.slice/user-1001.slice/session-4.scope\n", exe="/usr/bin/bash",
        tty_nr=(136 << 8) | 4, start=9000)
    system.add_disk("nvme0n1", sectors=1000215216, model="Fast NVMe 512", partitions=("nvme0n1p1",), dev="259:0")
    system.write("proc/self/mountinfo", "20 1 259:1 / / rw - ext4 /dev/nvme0n1p1 rw\n")
    system.set_diskstats({"nvme0n1": (0, 0, 0, 0, 0, 0, 0)})
    system.add_interface("eth0")
    system.set_net({"eth0": (0, 0)})
    system.add_gpu(vendor=0x8086, device=0x9A49, driver="i915", busy=None, vram=None)
    system.add_battery()


def write_desktop_files(root: Path) -> dict[str, str]:
    """Applications and autostart entries under ``root``; returns the environment that points at them."""
    def put(relative: str, text: str) -> None:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    apps = "usr/share/applications"
    put(f"{apps}/com.boswas.TaskManager.desktop", fx.desktop_file("Task Manager", "boswas-task-manager",
                                                                 Icon="utilities-system-monitor"))
    put(f"{apps}/firefox-esr.desktop", fx.desktop_file("Firefox", "/usr/lib/firefox-esr/firefox-esr %u", Icon="firefox-esr"))
    put(f"{apps}/com.boswas.Terminal.desktop", fx.desktop_file("Terminal", "boswas-terminal", Icon="utilities-terminal"))
    put(f"{apps}/com.boswas.Files.desktop", fx.desktop_file("Files", "boswas-files %U", Icon="system-file-manager"))
    put(f"{apps}/org.kde.kate.desktop", fx.desktop_file("Kate", "kate -b %U", Icon="kate", Comment="Advanced text editor"))
    put(f"{apps}/vim.desktop", fx.desktop_file("Vim", "vim %F", Terminal="true"))
    put("etc/xdg/autostart/org.kde.kdeconnect.daemon.desktop", fx.desktop_file("KDE Connect", "kdeconnectd"))
    put("etc/xdg/autostart/boswas-agent.desktop", fx.desktop_file("Boswas Device Agent", "boswas-device-agent --session",
                                                                  **{"X__Publisher": "Boswas"}))
    put("etc/xdg/autostart/gnome-only.desktop", fx.desktop_file("GNOME Thing", "gnome-thing", OnlyShowIn="GNOME;"))
    put("home/.config/autostart/notes.desktop", fx.desktop_file("My Notes", "/home/alice/bin/notes --tray", Hidden="true"))
    return {
        "HOME": str(root / "home"), "XDG_CONFIG_HOME": str(root / "home/.config"),
        "XDG_CONFIG_DIRS": str(root / "etc/xdg"), "XDG_DATA_HOME": str(root / "home/.local/share"),
        "XDG_DATA_DIRS": str(root / "usr/share"), "PATH": "/usr/bin:/bin", "LANG": "C",
    }


def default_units() -> dict[str, list[Unit]]:
    path = "/org/freedesktop/systemd1/unit/x"
    return {
        "system": [
            Unit("cups.service", "CUPS Scheduler", "loaded", "active", "running", path, "system", "enabled", 310),
            Unit("ssh.service", "OpenBSD Secure Shell server", "loaded", "active", "running", path, "system", "enabled", 300),
            Unit("backup.service", "Nightly backup", "loaded", "failed", "failed", path, "system", "static", 0),
            Unit("bluetooth.service", "Bluetooth service", "", "inactive", "dead", "", "system", "disabled", 0),
        ],
        "user": [
            Unit("pipewire.service", "PipeWire Multimedia Service", "loaded", "active", "running", path, "user", "enabled", 800),
            Unit("tracker.service", "File indexer", "loaded", "inactive", "dead", path, "user", "disabled", 0),
        ],
    }


def default_sessions() -> list[Session]:
    return [Session("1", 1000, "alice", "seat0", "tty2", "active", fx.BOOT + 40.0, "wayland", "user", 400),
            Session("4", 1001, "bob", "", "pts/4", "online", fx.BOOT + 90.0, "tty", "user", 950, remote="192.0.2.9"),
            Session("c1", 110, "greeter", "seat0", "tty1", "closing", fx.BOOT + 5.0, "wayland", "greeter", 77)]


class FakeBackend(Backend):
    """The Backend on a fake tree; what would change the real system is recorded instead."""

    def __init__(self, system: fx.FakeSystem, env: dict[str, str], settings_path: Path, threaded: bool = False) -> None:
        super().__init__(str(system.root), env, threaded=threaded, settings_path=settings_path)
        self.system = system
        self.uid, self.pid = ME, OWN_PID
        self.clock = fx.FakeClock()
        self.signals: list[tuple[int, int]] = []
        self.priorities: list[tuple[int, int]] = []
        self.started: list[tuple[list[str], str]] = []
        self.shown: list[str] = []
        self.clipboard = ""
        self.ignores_term = {900}
        self.refuses: set[int] = {1, 300, 310}                # other people's processes
        self.units: dict[str, list[Unit] | Exception] = default_units()
        self.sessions: list[Session] | Exception = default_sessions()
        self.service_calls: list[tuple[str, str, str]] = []
        self.service_error: Exception | None = None
        self.list_calls: list[tuple[str, bool]] = []

    def make_sampler(self) -> Sampler:
        return Sampler(self.root, clock=self.clock, wall_clock=lambda: fx.BOOT + 3600.0, hz=fx.HZ, page_size=fx.PAGE,
                       user_name=lambda uid: USERS.get(uid, str(uid)), ipv4=lambda name: ("192.0.2.7/24",),
                       fd_limit=0)

    def user_name(self) -> str:
        return "alice"

    def send_signal(self, pid, signum, start_ticks, name) -> None:
        if actions.condition(pid, start_ticks, self.proc) == "gone":
            raise actions.ActionError(f"{name} (PID {pid}) has already ended.")
        if pid in self.refuses:
            actions.send_signal(pid, signum, start_ticks, name, proc=self.proc, kill=self._refuse)
        self.signals.append((pid, int(signum)))
        if signum == signal.SIGKILL or (signum == signal.SIGTERM and pid not in self.ignores_term):
            self.system.remove_process(pid)
        elif signum == signal.SIGSTOP:
            self.system.update(pid, state="T")
        elif signum == signal.SIGCONT:
            self.system.update(pid, state="S")

    @staticmethod
    def _refuse(_pid, _signum) -> None:
        raise PermissionError(errno.EPERM, "Operation not permitted")

    def set_priority(self, pid, nice, start_ticks, name) -> None:
        def setpriority(_which, _pid, value):
            if value < self.system.processes[pid]["nice"]:
                raise PermissionError(errno.EACCES, "Permission denied")
            self.priorities.append((pid, value))
            self.system.update(pid, nice=value)

        actions.set_priority(pid, nice, start_ticks, name, proc=self.proc, setpriority=setpriority)

    def start_program(self, arguments, directory) -> None:
        if arguments[0] == "no-such-program":
            raise actions.ActionError("No program named “no-such-program” was found. Check the spelling, or give "
                                      "the full path.")
        self.started.append((list(arguments), directory))

    def show_in_files(self, path) -> None:
        self.shown.append(path)

    def list_services(self, scope, full=False):
        self.list_calls.append((scope, full))
        units = self.units[scope]
        if isinstance(units, Exception):
            raise units
        return list(units)

    def service_action(self, scope, action, name) -> str:
        self.service_calls.append((scope, action, name))
        if self.service_error is not None:
            raise self.service_error
        units = self.units[scope]
        from dataclasses import replace
        for index, unit in enumerate(units):
            if unit.name == name:
                running = action != "stop"
                units[index] = replace(unit, active="active" if running else "inactive",
                                       sub="running" if running else "dead", pid=4321 if running else 0)
        return f"{name} was {'stopped' if action == 'stop' else 'started and is running'}."

    def list_sessions(self):
        if isinstance(self.sessions, Exception):
            raise self.sessions
        return list(self.sessions)

    def copy_text(self, text: str) -> None:
        self.clipboard = text


NO_BUS = BusError("The system message bus cannot be reached: no such file or directory.")
