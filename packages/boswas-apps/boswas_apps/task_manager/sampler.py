"""The sampler: reads /proc and /sys and turns counters into rates (standard library only, no Qt).

``Sampler.tick(parts)`` reads only what ``parts`` asks for and returns a
``Sample``. It is meant to be called about once a second from one thread
and to cost little:

* ``/proc/PID/stat``, ``statm`` and ``io`` are kept open and read again
  with ``pread`` (one system call per file per tick) as long as the file
  descriptor limit allows; beyond that the files are opened and closed.
* A process whose ``stat`` line is byte for byte what it was at the last
  tick has not run since: nothing else is read for it and the previous
  ``Proc`` object is handed out again (with 0 % CPU). On a desktop that is
  most processes most of the time. Every ``FULL_PERIOD`` ticks each process
  is read in full all the same, so that memory that was reclaimed, or
  traffic of a process that used less than a clock tick of CPU time, shows
  after a few seconds at the latest.
* What rarely changes about a process (user, command line, executable,
  control group) is read when the process is first seen, when its name
  changes (it called ``exec``), and again every ``SLOW_PERIOD`` ticks.
* The proportional set size (``smaps_rollup``, which makes the kernel walk
  the page tables of the process) is read for at most ``PSS_BATCH``
  processes per tick, in turn.
* A part that is not asked for is not read. Rates are computed against the
  time and the counters of the last time that part *was* read, so a part
  that was left out for a while gives the average over the gap.

CPU of a process: the growth of ``utime + stime`` divided by the growth of
the sum of the ``cpu`` line of /proc/stat, times 100. Both are in clock
ticks and the ``cpu`` line counts every CPU, so 100 % means all cores busy
(``top`` in its default mode shows this value multiplied by the number of
CPUs).

Nothing here raises when a process disappears in the middle of a scan or
a file cannot be read: the process is left out, or the value is None.
"""

from __future__ import annotations

import os
import re
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, NamedTuple

from . import procfs, sysfs
from .textutil import decode

PARTS = frozenset({"cpu", "memory", "disks", "net", "gpu", "battery", "procs", "io", "pss"})
SLOW_PERIOD = 16             # ticks between two looks at the slow attributes of a process
FULL_PERIOD = 8              # ticks between two full reads of a process whose stat line does not change
PSS_BATCH = 8
IO_RETRY = 120               # ticks before an unreadable io file is tried again
DEVICE_PERIOD = 30           # ticks between two looks for disks, interfaces, cards and batteries
ADDRESS_PERIOD = 5           # ticks between two looks at interface state and addresses
MIN_INTERVAL = 0.2           # seconds; a rate over less time than this is noise, the previous one is kept
_FD_RESERVE = 256            # descriptors left to the rest of the application
_STAT_SIZE = 1024
_INTERPRETERS = re.compile(r"^(python[\d.]*|perl[\d.]*|ruby[\d.]*|node|nodejs|bash|sh|dash|zsh|ksh|lua[\d.]*"
                           r"|php[\d.]*|java|mono|electron[\d]*|wine(64)?(-preloader)?)$")


class Proc(NamedTuple):
    """One process as it was at a tick."""
    pid: int
    ppid: int
    name: str                # the program's name: longer than the kernel's 15 characters when argv tells it
    state: str               # one letter, see procfs.STATES
    uid: int | None
    user: str
    cpu: float               # percent of all cores since the previous scan
    cpu_ticks: int           # utime + stime
    mem: int                 # resident memory that is not shared with other processes, bytes
    rss: int
    pss: int | None          # None: not read yet, or not readable
    threads: int
    nice: int
    start: float             # seconds since the epoch
    start_ticks: int         # as in /proc/PID/stat: identifies the process together with the PID
    args: tuple[str, ...]
    exe: str | None          # None: not readable (another user's process) or a kernel thread
    disk: float | None       # bytes per second read from and written to storage; None: not readable
    kthread: bool
    sid: int                 # the kernel's session ID
    tty: int
    cgroup: str

    @property
    def cmdline(self) -> str:
        return " ".join(self.args) if self.args else (f"[{self.name}]" if self.kthread else self.name)


class CpuStatic(NamedTuple):
    model: str
    base_mhz: float | None
    max_mhz: float | None
    sockets: int
    cores: int
    logical: int
    caches: dict[str, int]
    virtualization: str      # "Virtual machine", "VT-x", "AMD-V" or ""


class CpuSample(NamedTuple):
    percent: float
    cores: tuple[tuple[int, float], ...]     # (CPU number, percent) for every CPU that is online
    mhz: float | None
    uptime: float | None
    load: procfs.LoadAvg | None
    processes: int
    threads: int | None
    static: CpuStatic


class MemorySample(NamedTuple):
    total: int
    available: int
    in_use: int              # total - available: what cannot be given to a program that asks now
    free: int
    cached: int              # page cache and reclaimable kernel memory
    modified: int            # the part of the cache that still has to be written out
    standby: int             # cached - modified
    used: int                # total - free - cached: the first segment of the composition bar
    committed: int
    commit_limit: int
    swap_total: int
    swap_used: int
    shared: int
    kernel: int              # slab, kernel stacks, page tables

    @property
    def percent(self) -> float:
        return 100 * self.in_use / self.total if self.total else 0.0


def memory_from_meminfo(info: dict[str, int]) -> MemorySample:
    """The memory page's numbers from /proc/meminfo. The four composition segments add up to the total."""
    total = info.get("MemTotal", 0)
    free = min(info.get("MemFree", 0), total)
    shared = info.get("Shmem", 0)
    cached = max(0, info.get("Buffers", 0) + info.get("Cached", 0) + info.get("SReclaimable", 0) - shared)
    cached = min(cached, total - free)
    modified = min(info.get("Dirty", 0) + info.get("Writeback", 0), cached)
    available = info.get("MemAvailable")
    if available is None:                        # kernels before 3.14
        available = free + cached
    available = min(available, total)
    swap_total = info.get("SwapTotal", 0)
    return MemorySample(
        total, available, total - available, free, cached, modified, cached - modified, total - free - cached,
        info.get("Committed_AS", 0), info.get("CommitLimit", 0), swap_total,
        max(0, swap_total - info.get("SwapFree", 0)), shared,
        info.get("Slab", 0) + info.get("KernelStack", 0) + info.get("PageTables", 0))


class DiskSample(NamedTuple):
    info: sysfs.DiskInfo
    active: float            # percent of the time with a request in flight
    read_bps: float
    write_bps: float
    response_ms: float | None        # mean time per request in this interval; None: no request finished
    mounts: tuple[str, ...]
    system: bool             # holds the root file system
    swap: bool


class NetSample(NamedTuple):
    info: sysfs.NetInfo
    rx_bps: float
    tx_bps: float
    rx_total: int
    tx_total: int
    ipv4: tuple[str, ...]
    ipv6: tuple[str, ...]


@dataclass
class Sample:
    time: float                      # the sampler's clock (monotonic)
    wall: float                      # seconds since the epoch
    interval: float | None           # seconds since the previous tick; None for the first
    ncpu: int = 1
    total_cpu: float | None = None   # percent, from every tick (it costs one small file)
    cpu: CpuSample | None = None
    memory: MemorySample | None = None
    disks: list[DiskSample] | None = None
    nets: list[NetSample] | None = None
    gpus: list[sysfs.GpuInfo] | None = None
    batteries: list[sysfs.Battery] | None = None
    procs: dict[int, Proc] | None = None
    errors: list[str] = field(default_factory=list)


class _State:
    """What the sampler remembers about one process between ticks."""
    __slots__ = ("dir", "stat_fd", "statm_fd", "io_fd", "start_ticks", "comm", "name", "uid", "user", "args",
                 "exe", "cgroup", "ticks", "io_total", "io_time", "io_retry", "pss", "pss_blocked", "slow_tick",
                 "seen", "start", "cpu", "disk", "raw", "proc")

    def __init__(self, directory: str) -> None:
        self.dir = directory
        self.stat_fd = self.statm_fd = self.io_fd = -1
        self.start_ticks = -1
        self.comm: bytes | None = None
        self.name = ""
        self.uid: int | None = None
        self.user = ""
        self.args: tuple[str, ...] = ()
        self.exe: str | None = None
        self.cgroup = ""
        self.ticks: int | None = None
        self.io_total: int | None = None
        self.io_time = 0.0
        self.io_retry = 0
        self.pss: int | None = None
        self.pss_blocked = False
        self.slow_tick = 0
        self.seen = 0
        self.start = 0.0
        self.cpu = 0.0
        self.disk: float | None = None
        self.raw: bytes | None = None             # the stat line as it was read last
        self.proc: Proc | None = None             # what was handed out for it


def raise_fd_limit(wanted: int = 16384) -> int:
    """Raise the soft limit of open files towards the hard limit (at most ``wanted``); returns the soft limit.

    Qt and Python wait for events with poll, not select, so descriptors above 1023 are safe.
    """
    try:
        import resource
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        target = wanted if hard == resource.RLIM_INFINITY else min(wanted, hard)
        if soft != resource.RLIM_INFINITY and soft < target:
            resource.setrlimit(resource.RLIMIT_NOFILE, (target, hard))
            soft = target
        return 1 << 20 if soft == resource.RLIM_INFINITY else int(soft)
    except (ImportError, ValueError, OSError):
        return 1024


def _default_user_name(uid: int) -> str:
    try:
        import pwd
        return pwd.getpwuid(uid).pw_name
    except (ImportError, KeyError, OSError):
        return str(uid)


def ipv4_addresses(name: str) -> tuple[str, ...]:
    """The primary IPv4 address of an interface with its prefix length (``192.0.2.7/24``), asked from the
    kernel with the SIOCGIFADDR and SIOCGIFNETMASK ioctls. Empty when it has none."""
    import fcntl
    import socket
    import struct
    try:
        request = struct.pack("256s", name.encode("utf-8", "replace")[:15])
    except struct.error:
        return ()
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM | socket.SOCK_CLOEXEC) as probe:
            address = socket.inet_ntoa(fcntl.ioctl(probe.fileno(), 0x8915, request)[20:24])
            try:
                mask = fcntl.ioctl(probe.fileno(), 0x891B, request)[20:24]
                prefix = bin(int.from_bytes(mask, "big")).count("1")
                return (f"{address}/{prefix}",)
            except OSError:
                return (address,)
    except OSError:
        return ()


def display_name(comm: str, args: tuple[str, ...] | list[str], kthread: bool = False) -> str:
    """The name of a process for people.

    The kernel keeps 15 bytes of the name. When the name is that long and
    one of the first arguments is a path whose last part starts with it,
    that last part is the full name (``boswas-task-man`` becomes
    ``boswas-task-manager``).
    """
    if kthread or len(comm) < 15 or not args:
        return comm
    for argument in args[:3]:
        base = os.path.basename(argument)
        if base.startswith(comm) and len(base) <= 64 and " " not in base:
            return base
    return comm


def program_names(proc: Proc) -> list[str]:
    """The names a process can be known by, most telling first: its executable, the script it runs when
    the executable is an interpreter, the kernel's name. Lower case, without directory."""
    names: list[str] = []

    def add(name: str) -> None:
        name = os.path.basename(name).lower()
        if name.endswith(" (deleted)"):
            name = name[:-10]
        if name and name not in names:
            names.append(name)

    exe = os.path.basename(proc.exe) if proc.exe else (os.path.basename(proc.args[0]) if proc.args else "")
    if exe.endswith(" (deleted)"):
        exe = exe[:-10]
    if _INTERPRETERS.match(exe.lower()):
        for argument in proc.args[1:6]:
            if argument and not argument.startswith("-"):
                script = os.path.basename(argument)
                add(script)
                if script.lower().endswith((".py", ".pl", ".rb", ".js", ".sh")):
                    add(script.rsplit(".", 1)[0])
                break
    if exe:
        add(exe)
    add(proc.name)
    return names


class Sampler:
    """See the module text. One instance, one thread; ``close`` releases the file descriptors."""

    def __init__(self, root: str = "/", *, clock: Callable[[], float] = time.monotonic,
                 wall_clock: Callable[[], float] = time.time, hz: int | None = None, page_size: int | None = None,
                 user_name: Callable[[int], str] = _default_user_name,
                 ipv4: Callable[[str], tuple[str, ...]] = ipv4_addresses, fd_limit: int | None = None) -> None:
        self.root = root
        self.proc = os.path.join(root, "proc")
        self.sys = os.path.join(root, "sys")
        self._clock = clock
        self._wall = wall_clock
        self.hz = hz or _sysconf("SC_CLK_TCK", 100)
        self.page = page_size or _sysconf("SC_PAGE_SIZE", 4096)
        self._user_name = user_name
        self._ipv4 = ipv4
        self._users: dict[int, str] = {}
        self._fd_limit = max(0, (raise_fd_limit() if fd_limit is None else fd_limit) - _FD_RESERVE)
        self._fds = 0
        self._tick = 0
        self._last_time: float | None = None
        self._states: dict[int, _State] = {}
        self._scans = 0
        self._procs_total: int | None = None         # the cpu line's sum at the previous process scan
        self._procs_time = 0.0
        self._stat_prev: procfs.SystemStat | None = None      # for total_cpu, every tick
        self._stat_time = 0.0
        self._total_cpu: float | None = None
        self._cpu_time = 0.0
        self._cpu_last: tuple[float, tuple] | None = None
        self._disk_last: list[DiskSample] = []
        self._net_last: dict[str, tuple[float, float]] = {}
        self._cpu_prev: procfs.SystemStat | None = None       # for the cpu part
        self._cpu_static: CpuStatic | None = None
        self._cpuinfo_mhz: float | None = None
        self._boot = 0
        self._pss_queue: deque[int] = deque()
        self._disk_prev: dict[str, procfs.DiskCounters] = {}
        self._disk_time = 0.0
        self._disk_infos: list[sysfs.DiskInfo] = []
        self._disk_extra: dict[str, tuple[set[str], set[str]]] = {}
        self._disk_scan = -DEVICE_PERIOD
        self._mounts: list[procfs.Mount] = []
        self._swaps: list[str] = []
        self._net_prev: dict[str, procfs.NetCounters] = {}
        self._net_time = 0.0
        self._net_infos: dict[str, sysfs.NetInfo] = {}
        self._net_addresses: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {}
        self._net_scan = -ADDRESS_PERIOD
        self._gpu_static: dict[str, sysfs.GpuInfo] = {}
        self._gpu_scan = -DEVICE_PERIOD
        self._battery_names: list[str] = []
        self._battery_scan = -DEVICE_PERIOD

    # --- housekeeping ---
    def close(self) -> None:
        for state in self._states.values():
            self._release(state)
        self._states.clear()

    def _release(self, state: _State) -> None:
        for name in ("stat_fd", "statm_fd", "io_fd"):
            fd = getattr(state, name)
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
                self._fds -= 1
                setattr(state, name, -1)

    def open_descriptors(self) -> int:
        """How many /proc files are kept open now."""
        return self._fds

    def user_name(self, uid: int | None) -> str:
        if uid is None:
            return ""
        name = self._users.get(uid)
        if name is None:
            name = self._users[uid] = decode(str(self._user_name(uid)).encode("utf-8", "replace"), 64)
        return name

    def _read(self, state: _State, attribute: str, name: str, size: int) -> bytes:
        """A small file of a process: with ``pread`` on the descriptor kept for it, else open, read, close
        (and keep the descriptor when the limit allows). Raises OSError."""
        fd = getattr(state, attribute)
        if fd >= 0:
            return os.pread(fd, size, 0)
        fd = os.open(f"{state.dir}/{name}", os.O_RDONLY | os.O_CLOEXEC)
        try:
            data = os.read(fd, size)
        except OSError:
            os.close(fd)
            raise
        if self._fds < self._fd_limit:
            setattr(state, attribute, fd)
            self._fds += 1
        else:
            os.close(fd)
        return data

    # --- one tick ---
    def tick(self, parts: frozenset[str] | set[str] = PARTS) -> Sample:
        now = self._clock()
        sample = Sample(now, self._wall(), None if self._last_time is None else now - self._last_time)
        self._last_time = now
        self._tick += 1
        try:
            stat = procfs.parse_proc_stat(procfs.read_file(os.path.join(self.proc, "stat")), "cpu" in parts)
        except OSError as error:
            stat = None
            sample.errors.append(f"/proc/stat: {error.strerror or error}")
        if stat is not None:
            self._boot = stat.boot_time
            sample.ncpu = max(1, stat.count)
            if self._stat_prev is None or now - self._stat_time >= MIN_INTERVAL:
                if self._stat_prev is not None:
                    self._total_cpu = _busy_percent(stat.total, self._stat_prev.total)
                self._stat_prev, self._stat_time = stat, now
            sample.total_cpu = self._total_cpu
            if "procs" in parts:
                sample.procs = self._processes(stat, now, "io" in parts, "pss" in parts)
            if "cpu" in parts:
                sample.cpu = self._cpu(stat, now, None if sample.procs is None else len(sample.procs))
        if "memory" in parts:
            sample.memory = self._memory()
        if "disks" in parts:
            sample.disks = self._disks(now)
        if "net" in parts:
            sample.nets = self._nets(now)
        if "gpu" in parts:
            sample.gpus = self._gpus()
        if "battery" in parts:
            sample.batteries = self._batteries()
        return sample

    # --- CPU ---
    def _static(self, stat: procfs.SystemStat) -> CpuStatic:
        try:
            info = procfs.parse_cpuinfo(procfs.read_file(os.path.join(self.proc, "cpuinfo")))
        except OSError:
            info = procfs.CpuInfo("", "", 0, 0, 0, (), frozenset())
        topology = sysfs.cpu_topology(self.sys)
        base, maximum, _minimum = sysfs.cpu_frequency_limits(self.sys)
        if base is None:
            match = re.search(r"@\s*([\d.]+)\s*([GM])Hz", info.model)
            if match:
                try:
                    base = float(match.group(1)) * (1000 if match.group(2) == "G" else 1)
                except ValueError:
                    base = None
        if info.mhz:
            self._cpuinfo_mhz = sum(info.mhz) / len(info.mhz)
        logical = topology.logical or info.logical or stat.count or 1
        if "hypervisor" in info.flags:
            virtualization = "Virtual machine"
        elif "vmx" in info.flags:
            virtualization = "VT-x"
        elif "svm" in info.flags:
            virtualization = "AMD-V"
        else:
            virtualization = ""
        return CpuStatic(info.model, base, maximum, topology.sockets or info.sockets or 1,
                         topology.cores or info.cores or logical, logical, sysfs.cpu_caches(self.sys),
                         virtualization)

    def _cpu(self, stat: procfs.SystemStat, now: float, processes: int | None = None) -> CpuSample:
        if self._cpu_static is None:
            self._cpu_static = self._static(stat)
        previous = self._cpu_prev
        if previous is None:
            percent = 0.0
            cores = tuple((number, 0.0) for number in sorted(stat.cpus))
        elif now - self._cpu_time < MIN_INTERVAL and self._cpu_last is not None:
            percent, cores = self._cpu_last
        else:
            percent = _busy_percent(stat.total, previous.total)
            before = previous.cpus
            cores = tuple((number, _core_percent(times, before.get(number)))
                          for number, times in sorted(stat.cpus.items()))
        if previous is None or now - self._cpu_time >= MIN_INTERVAL:
            self._cpu_prev, self._cpu_time = stat, now
        self._cpu_last = (percent, cores)
        mhz = sysfs.cpu_current_mhz(self.sys)
        if mhz is None:
            if self._tick % ADDRESS_PERIOD == 1 and stat.count <= 256:
                try:
                    info = procfs.parse_cpuinfo(procfs.read_file(os.path.join(self.proc, "cpuinfo")))
                    self._cpuinfo_mhz = sum(info.mhz) / len(info.mhz) if info.mhz else None
                except OSError:
                    pass
            mhz = self._cpuinfo_mhz
        load = uptime = None
        try:
            load = procfs.parse_loadavg(procfs.read_file(os.path.join(self.proc, "loadavg"), 256))
            uptime = procfs.parse_uptime(procfs.read_file(os.path.join(self.proc, "uptime"), 256))
        except OSError:
            pass
        if processes is None:
            try:
                processes = sum(1 for name in os.listdir(self.proc) if name.isdigit())
            except OSError:
                processes = 0
        return CpuSample(percent, cores, mhz, uptime, load, processes, load.entities if load else None,
                         self._cpu_static)

    # --- memory ---
    def _memory(self) -> MemorySample | None:
        try:
            return memory_from_meminfo(procfs.parse_meminfo(procfs.read_file(os.path.join(self.proc, "meminfo"))))
        except OSError:
            return None

    # --- processes ---
    def _processes(self, stat: procfs.SystemStat, now: float, want_io: bool, want_pss: bool) -> dict[int, Proc]:
        total = stat.total.total
        total_delta = total - self._procs_total if self._procs_total is not None else 0
        elapsed = now - self._procs_time if self._scans else 0.0
        first_scan = self._scans == 0
        # A scan right after another one (the page was switched): too little time for a rate. The CPU
        # figures of the scan before are shown again and the counters are left for the next real one.
        hold = not first_scan and elapsed < MIN_INTERVAL
        if not hold:
            self._procs_total, self._procs_time = total, now
        self._scans += 1
        tick = self._tick
        if total_delta > 0:
            scale = 100.0 / total_delta
        elif elapsed > 0:
            scale = 100.0 / (elapsed * self.hz * max(1, stat.count))
        else:
            scale = 0.0
        try:
            names = os.listdir(self.proc)
        except OSError:
            names = []
        states = self._states
        hz, page, boot = self.hz, self.page, stat.boot_time
        result: dict[int, Proc] = {}
        parse_stat = procfs.parse_stat
        for name in names:
            if not name.isdigit():
                continue
            pid = int(name)
            state = states.get(pid)
            fresh = state is None
            if fresh:
                state = _State(f"{self.proc}/{name}")
            try:
                raw = self._read(state, "stat_fd", "stat", _STAT_SIZE)
                if (raw == state.raw and (tick + pid) % FULL_PERIOD and state.proc is not None
                        and (state.proc.kthread or tick - state.slow_tick < SLOW_PERIOD)):
                    # Not a byte of the line has changed: the process has not run.
                    proc = state.proc
                    if not hold:
                        if proc.cpu or proc.disk:
                            state.cpu = 0.0
                            state.disk = None if proc.disk is None else 0.0
                            proc = state.proc = proc._replace(cpu=0.0, disk=state.disk)
                        state.io_time = now
                    state.seen = tick
                    result[pid] = proc
                    continue
                parsed = parse_stat(raw)
            except OSError:
                raw = parsed = None
                if not fresh:
                    # The descriptor belongs to a process that is gone; the number may be in use again.
                    self._release(state)
                    del states[pid]
                    state = _State(f"{self.proc}/{name}")
                    fresh = True
                    try:
                        raw = self._read(state, "stat_fd", "stat", _STAT_SIZE)
                        parsed = parse_stat(raw)
                    except OSError:
                        parsed = None
            if parsed is None or parsed.pid != pid:
                self._release(state)
                states.pop(pid, None)
                continue
            if not fresh and parsed.starttime != state.start_ticks:
                # Without a kept descriptor a reused PID shows as a new start time.
                self._release(state)
                state = _State(f"{self.proc}/{name}")
                fresh = True
            kthread = bool(parsed.flags & procfs.PF_KTHREAD)
            ticks = parsed.utime + parsed.stime
            if fresh:
                states[pid] = state
                state.start_ticks = parsed.starttime
                state.start = boot + parsed.starttime / hz
                # A process born since the previous scan spent all its time in this interval; one that
                # was already running when the first scan was made starts at nought.
                cpu = 0.0 if first_scan or hold else min(100.0, ticks * scale)
                state.ticks = ticks if first_scan or not hold else 0
                state.cpu = cpu
            elif hold:
                cpu = state.cpu
            else:
                cpu = state.cpu = min(100.0, max(0, ticks - state.ticks) * scale)
                state.ticks = ticks
            state.seen = tick
            if parsed.comm != state.comm or (tick - state.slow_tick >= SLOW_PERIOD and not kthread):
                self._slow(state, parsed, kthread, tick + (pid & (SLOW_PERIOD - 1) if fresh else 0))
            rss = parsed.rss * page
            mem = rss
            disk = None
            if not kthread:
                try:
                    statm = procfs.parse_statm(self._read(state, "statm_fd", "statm", 256))
                    if statm is not None:
                        rss = statm[1] * page
                        mem = max(0, statm[1] - statm[2]) * page
                except OSError:
                    pass
                if want_io and tick >= state.io_retry:
                    try:
                        counters = procfs.parse_io(self._read(state, "io_fd", "io", 512))
                    except PermissionError:
                        counters = None
                        state.io_retry = tick + IO_RETRY
                    except OSError:
                        counters = None
                    if counters is not None:
                        io_total = counters.read_bytes + counters.write_bytes
                        if state.io_total is None:
                            disk = io_total / elapsed if fresh and not first_scan and elapsed >= MIN_INTERVAL else 0.0
                            state.io_total, state.io_time = io_total, now
                        elif now - state.io_time >= MIN_INTERVAL:
                            disk = max(0, io_total - state.io_total) / (now - state.io_time)
                            state.io_total, state.io_time = io_total, now
                        else:
                            disk = state.disk if state.disk is not None else 0.0
                        state.disk = disk
            state.raw = raw
            proc = Proc(
                pid, parsed.ppid, state.name, parsed.state, state.uid, state.user, cpu, ticks, mem, rss, state.pss,
                parsed.threads, parsed.nice, state.start, parsed.starttime, state.args, state.exe, disk, kthread,
                parsed.session, parsed.tty_nr, state.cgroup)
            if proc == state.proc:               # a full read that found nothing new: the old object stays
                proc = state.proc
            state.proc = result[pid] = proc
        if len(states) != len(result):
            for pid in [pid for pid, state in states.items() if state.seen != tick]:
                self._release(states.pop(pid))
        if want_pss:
            self._pss(result)
        return result

    def _slow(self, state: _State, parsed: procfs.ProcStat, kthread: bool, tick: int) -> None:
        """Read what rarely changes: the user, the command line, the executable, the control group."""
        state.comm = parsed.comm
        state.slow_tick = tick
        comm = decode(parsed.comm, 64)
        directory = state.dir
        try:
            uid = procfs.status_uid(procfs.read_file(f"{directory}/status", 8192))
        except OSError:
            uid = state.uid
        if uid != state.uid or not state.user:
            state.uid = uid
            state.user = self.user_name(uid)
        if not kthread:
            try:
                state.args = tuple(procfs.parse_cmdline(procfs.read_file(f"{directory}/cmdline", 4097)))
            except OSError:
                pass
            try:
                state.exe = decode(os.fsencode(os.readlink(f"{directory}/exe")), 4096)
            except OSError:
                state.exe = None
        try:
            state.cgroup = decode(procfs.parse_cgroup(procfs.read_file(f"{directory}/cgroup", 8192)).encode(
                "utf-8", "replace"), 1024)
        except OSError:
            pass
        state.name = display_name(comm, state.args, kthread)

    def _pss(self, result: dict[int, Proc]) -> None:
        queue = self._pss_queue
        if not queue:
            queue.extend(pid for pid, proc in sorted(result.items())
                         if not proc.kthread and not self._states[pid].pss_blocked)
        done = 0
        while queue and done < PSS_BATCH:
            pid = queue.popleft()
            state = self._states.get(pid)
            if state is None or pid not in result:
                continue
            done += 1
            try:
                rollup = procfs.parse_smaps_rollup(procfs.read_file(f"{state.dir}/smaps_rollup", 8192))
            except PermissionError:
                state.pss_blocked = True
                continue
            except OSError:
                continue
            if "Pss" in rollup and rollup["Pss"] != state.pss:
                state.pss = rollup["Pss"]
                state.proc = result[pid] = result[pid]._replace(pss=state.pss)

    # --- disks ---
    def _scan_disks(self) -> None:
        self._disk_infos = [sysfs.disk_info(self.sys, name) for name in sysfs.disk_names(self.sys)]
        try:
            self._mounts = procfs.parse_mountinfo(procfs.read_file(os.path.join(self.proc, "self/mountinfo")))
        except OSError:
            self._mounts = []
        try:
            self._swaps = procfs.parse_swaps(procfs.read_file(os.path.join(self.proc, "swaps"), 65536))
        except OSError:
            self._swaps = []
        # Device mapper and RAID devices on top of a disk: their mounts belong to the disk underneath.
        owner: dict[str, str] = {}
        for info in self._disk_infos:
            owner[info.name] = info.name
            for partition in info.partitions:
                owner[partition] = info.name
        self._disk_extra = {info.name: (set(info.devices), {info.name, *info.partitions})
                            for info in self._disk_infos}
        block = os.path.join(self.sys, "block")
        try:
            stacked = [name for name in os.listdir(block) if name.startswith(("dm-", "md"))]
        except OSError:
            stacked = []

        def disks_under(name: str, depth: int = 0) -> set[str]:
            if name in owner:
                return {owner[name]}
            found: set[str] = set()
            if depth < 8:
                try:
                    slaves = os.listdir(os.path.join(block, name, "slaves"))
                except OSError:
                    slaves = []
                for slave in slaves:
                    found |= disks_under(slave, depth + 1)
            return found

        for name in stacked:
            number = procfs.read_text(os.path.join(block, name, "dev"), 32)
            alias = procfs.read_text(os.path.join(block, name, "dm/name"), 256)
            for disk in disks_under(name):
                devices, names = self._disk_extra[disk]
                if number:
                    devices.add(number)
                names.add(name)
                if alias:
                    names.add(alias)

    def _disks(self, now: float) -> list[DiskSample]:
        if self._tick - self._disk_scan >= DEVICE_PERIOD:
            self._disk_scan = self._tick
            self._scan_disks()
        try:
            counters = procfs.parse_diskstats(procfs.read_file(os.path.join(self.proc, "diskstats")))
        except OSError:
            counters = {}
        elapsed = now - self._disk_time if self._disk_prev else 0.0
        if self._disk_prev and elapsed < MIN_INTERVAL and self._disk_last:
            return self._disk_last
        previous, self._disk_prev, self._disk_time = self._disk_prev, counters, now
        result = []
        for info in self._disk_infos:
            current, before = counters.get(info.name), previous.get(info.name)
            active = read_bps = write_bps = 0.0
            response = None
            if current is not None and before is not None and elapsed > 0:
                delta = procfs.delta
                active = min(100.0, delta(current.busy_ms, before.busy_ms, 32) / (elapsed * 10))
                read_bps = delta(current.sectors_read, before.sectors_read) * 512 / elapsed
                write_bps = delta(current.sectors_written, before.sectors_written) * 512 / elapsed
                requests = delta(current.reads, before.reads) + delta(current.writes, before.writes)
                if requests:
                    spent = delta(current.read_ms, before.read_ms, 32) + delta(current.write_ms, before.write_ms, 32)
                    response = spent / requests
            devices, names = self._disk_extra.get(info.name, (set(), set()))
            mounts = []
            for mount in self._mounts:
                if mount.device in devices or os.path.basename(mount.source) in names:
                    if mount.mount_point not in mounts:
                        mounts.append(mount.mount_point)
            mounts.sort(key=lambda point: (len(point), point))
            swap = any(os.path.basename(path) in names for path in self._swaps)
            result.append(DiskSample(info, active, read_bps, write_bps, response, tuple(mounts[:8]),
                                     "/" in mounts, swap))
        self._disk_last = result
        return result

    # --- network ---
    def _nets(self, now: float) -> list[NetSample]:
        try:
            counters = procfs.parse_net_dev(procfs.read_file(os.path.join(self.proc, "net/dev")))
        except OSError:
            counters = {}
        if self._tick - self._net_scan >= ADDRESS_PERIOD or set(counters) != set(self._net_infos):
            self._net_scan = self._tick
            self._net_infos = {name: sysfs.net_info(self.sys, name) for name in counters}
            try:
                inet6 = procfs.parse_if_inet6(procfs.read_file(os.path.join(self.proc, "net/if_inet6"), 1 << 20))
            except OSError:
                inet6 = {}
            self._net_addresses = {}
            for name, info in self._net_infos.items():
                if info.kind == "Loopback":
                    continue
                # Global addresses first, link-local ones last.
                six = sorted(inet6.get(name, []), key=lambda entry: (entry[2] != 0, entry[0]))
                self._net_addresses[name] = (tuple(self._ipv4(name)),
                                             tuple(f"{address}/{prefix}" for address, prefix, _scope in six[:4]))
        elapsed = now - self._net_time if self._net_prev else 0.0
        hold = bool(self._net_prev) and elapsed < MIN_INTERVAL
        previous = self._net_prev
        if not hold:
            self._net_prev, self._net_time = counters, now
        result = []
        for name in sorted(counters):
            info = self._net_infos.get(name)
            if info is None or info.kind == "Loopback":
                continue
            current, before = counters[name], previous.get(name)
            ipv4, ipv6 = self._net_addresses.get(name, ((), ()))
            connected = info.state in ("up", "unknown", "dormant")
            if not info.physical and not (connected and (current.rx_bytes or current.tx_bytes or ipv4 or ipv6)):
                continue                 # a virtual interface that carries nothing
            rx = tx = 0.0
            if hold:
                rx, tx = self._net_last.get(name, (0.0, 0.0))
            elif before is not None and elapsed > 0:
                rx = procfs.delta(current.rx_bytes, before.rx_bytes) / elapsed
                tx = procfs.delta(current.tx_bytes, before.tx_bytes) / elapsed
            self._net_last[name] = (rx, tx)
            result.append(NetSample(info, rx, tx, current.rx_bytes, current.tx_bytes, ipv4, ipv6))
        result.sort(key=lambda net: (not net.info.physical, net.info.name))
        return result

    # --- GPU, battery ---
    def _gpus(self) -> list[sysfs.GpuInfo]:
        if self._tick - self._gpu_scan >= DEVICE_PERIOD:
            self._gpu_scan = self._tick
            cards = sysfs.gpu_cards(self.sys)
            self._gpu_static = {card: self._gpu_static.get(card) or sysfs.gpu_info(self.sys, card, self.root)
                                for card in cards}
        result = []
        for card, info in self._gpu_static.items():
            device = os.path.join(self.sys, "class/drm", card, "device")
            used = procfs.read_int(os.path.join(device, "mem_info_vram_used")) if info.memory_total else None
            result.append(info._replace(busy_percent=sysfs.gpu_busy(self.sys, card) if info.busy_percent is not None
                                        else None, memory_used=used))
        return result

    def _batteries(self) -> list[sysfs.Battery]:
        if self._tick - self._battery_scan >= DEVICE_PERIOD:
            self._battery_scan = self._tick
            self._battery_names = sysfs.battery_names(self.sys)
        result = []
        for name in self._battery_names:
            battery = sysfs.battery(self.sys, name)
            if battery is not None:
                result.append(battery)
        return result


def _busy_percent(now: procfs.CpuTimes, before: procfs.CpuTimes | None) -> float:
    if before is None:
        return 0.0
    total = now.total - before.total
    if total <= 0:
        return 0.0
    return max(0.0, min(100.0, 100.0 * (now.busy - before.busy) / total))


def _core_percent(now: tuple[int, int], before: tuple[int, int] | None) -> float:
    if before is None:
        return 0.0
    total = now[0] - before[0]
    if total <= 0:
        return 0.0
    return max(0.0, min(100.0, 100.0 * (now[1] - before[1]) / total))


def _sysconf(name: str, default: int) -> int:
    try:
        value = os.sysconf(name)
        return int(value) if value > 0 else default
    except (ValueError, OSError, AttributeError):
        return default
