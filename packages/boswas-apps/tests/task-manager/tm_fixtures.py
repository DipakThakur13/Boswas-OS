"""A fake /proc and /sys tree for the tests of Task Manager, built in a temporary directory.

``FakeSystem(root)`` writes the files the sampler reads, in the formats of
the kernel, and lets a test change them between two ticks: add a process,
let it use CPU time, remove it. Nothing outside ``root`` is touched.
"""

from __future__ import annotations

import os
import shutil
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

PF_KTHREAD = 0x00200000
HZ = 100
PAGE = 4096
BOOT = 1_700_000_000


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeSystem:
    def __init__(self, root: Path, cpus: int = 4) -> None:
        self.root = Path(root)
        self.proc = self.root / "proc"
        self.sys = self.root / "sys"
        self.proc.mkdir(parents=True, exist_ok=True)
        self.sys.mkdir(parents=True, exist_ok=True)
        self.cpus = cpus
        # user nice system idle iowait irq softirq steal, per CPU
        self.cpu_times = [[0, 0, 0, 0, 0, 0, 0, 0] for _ in range(cpus)]
        self.processes: dict[int, dict] = {}
        self.write_stat()
        self.write("proc/meminfo", MEMINFO)
        self.write("proc/loadavg", "0.52 0.48 0.40 2/345 4242\n")
        self.write("proc/uptime", "12345.67 40000.00\n")
        self.write("proc/cpuinfo", cpuinfo(cpus))
        self.write("proc/diskstats", "")
        self.write("proc/net/dev", NET_HEADER)
        self.write("proc/self/mountinfo", "")
        self.write("proc/swaps", "Filename\t\t\t\tType\t\tSize\t\tUsed\t\tPriority\n")
        for number in range(cpus if cpus <= 64 else 0):       # a big machine is described by cpuinfo alone
            self.write(f"sys/devices/system/cpu/cpu{number}/topology/physical_package_id", "0\n")
            self.write(f"sys/devices/system/cpu/cpu{number}/topology/core_id", f"{number // 2}\n")

    # --- files ---
    def path(self, relative: str) -> Path:
        return self.root / relative

    def write(self, relative: str, content: str | bytes) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, str):
            content = content.encode("utf-8")
        path.write_bytes(content)
        return path

    def remove(self, relative: str) -> None:
        path = self.root / relative
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        elif path.exists() or path.is_symlink():
            path.unlink()

    # --- /proc/stat ---
    def burn(self, busy: int, idle: int, cpu: int | None = None) -> None:
        """Add ``busy`` user ticks and ``idle`` idle ticks to one CPU (or spread over all)."""
        targets = list(range(self.cpus)) if cpu is None else [cpu]
        share = len(targets)
        for index, number in enumerate(targets):
            self.cpu_times[number][0] += busy // share + (1 if index < busy % share else 0)
            self.cpu_times[number][3] += idle // share + (1 if index < idle % share else 0)
        self.write_stat()

    def write_stat(self, columns: int = 10) -> None:
        total = [sum(times[i] for times in self.cpu_times) for i in range(8)]
        pad = [0] * (columns - 8) if columns > 8 else []
        lines = ["cpu  " + " ".join(str(v) for v in (total + pad)[:columns])]
        for number, times in enumerate(self.cpu_times):
            lines.append(f"cpu{number} " + " ".join(str(v) for v in (times + pad)[:columns]))
        lines += ["intr 12345 " + " ".join("0" for _ in range(64)), "ctxt 999", f"btime {BOOT}", "processes 4242",
                  "procs_running 2", "procs_blocked 0", "softirq 1 2 3"]
        self.write("proc/stat", "\n".join(lines) + "\n")

    # --- processes ---
    def add_process(self, pid: int, comm: str | bytes, *, ppid: int = 1, state: str = "S", uid: int = 1000,
                    utime: int = 0, stime: int = 0, nice: int = 0, threads: int = 1, start: int = 100,
                    resident: int = 1000, shared: int = 200, cmdline: list | None = None, exe: str | None = "",
                    cgroup: str = "0::/user.slice/user-1000.slice/session-1.scope\n", kthread: bool = False,
                    io: tuple[int, int] | None = (0, 0), session: int | None = None, tty_nr: int = 0,
                    pss_kb: int | None = 512) -> None:
        if isinstance(comm, str):
            comm = comm.encode("utf-8")
        if kthread:
            cmdline, exe, resident, shared, io, pss_kb = [], None, 0, 0, None, None
            cgroup = "0::/\n"
        self.processes[pid] = dict(
            comm=comm, ppid=ppid, state=state, uid=uid, utime=utime, stime=stime, nice=nice, threads=threads,
            start=start, resident=resident, shared=shared,
            cmdline=[comm.decode("utf-8", "replace")] if cmdline is None else cmdline, exe=exe, cgroup=cgroup,
            flags=PF_KTHREAD if kthread else 0x400000, io=io, session=pid if session is None else session,
            tty_nr=tty_nr, pss_kb=pss_kb)
        self.write_process(pid)

    def update(self, pid: int, **changes) -> None:
        self.processes[pid].update(changes)
        self.write_process(pid)

    def use_cpu(self, pid: int, ticks: int) -> None:
        self.processes[pid]["utime"] += ticks
        self.write_process(pid)

    def stat_line(self, pid: int) -> bytes:
        p = self.processes[pid]
        tail = (f" {p['state']} {p['ppid']} {pid} {p['session']} {p['tty_nr']} -1 {p['flags']} 10 0 0 0 "
                f"{p['utime']} {p['stime']} 0 0 20 {p['nice']} {p['threads']} 0 {p['start']} "
                f"{p['resident'] * PAGE * 4} {p['resident']} 18446744073709551615 1 1 0 0 0 0 0 0 0 0 0 0 17 "
                f"1 0 0 0 0 0 0 0 0 0 0 0 0 0\n")
        return str(pid).encode() + b" (" + p["comm"] + b")" + tail.encode()

    def write_process(self, pid: int) -> None:
        p = self.processes[pid]
        base = f"proc/{pid}"
        self.write(f"{base}/stat", self.stat_line(pid))
        self.write(f"{base}/statm", f"{p['resident'] * 4} {p['resident']} {p['shared']} 10 0 50 0\n")
        name = p["comm"].decode("utf-8", "replace")
        self.write(f"{base}/status", f"Name:\t{name}\nUmask:\t0022\nState:\t{p['state']} (x)\nTgid:\t{pid}\n"
                                     f"Pid:\t{pid}\nPPid:\t{p['ppid']}\nUid:\t{p['uid']}\t{p['uid']}\t{p['uid']}\t"
                                     f"{p['uid']}\nGid:\t{p['uid']}\t{p['uid']}\t{p['uid']}\t{p['uid']}\n"
                                     f"Threads:\t{p['threads']}\nVmSwap:\t       0 kB\n")
        parts = [part if isinstance(part, bytes) else part.encode("utf-8") for part in p["cmdline"]]
        self.write(f"{base}/cmdline", b"".join(part + b"\0" for part in parts))
        self.write(f"{base}/cgroup", p["cgroup"])
        link = self.root / base / "exe"
        if link.is_symlink() or link.exists():
            link.unlink()
        if p["exe"] is not None:
            target = p["exe"] or f"/usr/bin/{name}"
            os.symlink(target, link)
        cwd = self.root / base / "cwd"
        if not cwd.is_symlink():
            os.symlink("/home/alice", cwd)
        (self.root / base / "fd").mkdir(exist_ok=True)
        for number in range(3):
            (self.root / base / "fd" / str(number)).write_bytes(b"")
        if p["io"] is None:
            self.remove(f"{base}/io")
        else:
            self.write(f"{base}/io", f"rchar: 1\nwchar: 2\nsyscr: 3\nsyscw: 4\nread_bytes: {p['io'][0]}\n"
                                     f"write_bytes: {p['io'][1]}\ncancelled_write_bytes: 0\n")
        if p["pss_kb"] is None:
            self.remove(f"{base}/smaps_rollup")
        else:
            self.write(f"{base}/smaps_rollup",
                       "00400000-7ffd0000 ---p 00000000 00:00 0                          [rollup]\n"
                       f"Rss:            {p['resident'] * 4:8d} kB\nPss:            {p['pss_kb']:8d} kB\n"
                       "Pss_Dirty:            100 kB\nSwap:                   0 kB\n")
        self.write(f"{base}/limits", LIMITS)

    def remove_process(self, pid: int) -> None:
        self.processes.pop(pid, None)
        self.remove(f"proc/{pid}")

    # --- devices ---
    def add_disk(self, name: str, *, sectors: int = 1000000, rotational: int = 0, removable: int = 0,
                 model: str = "Fast Disk 500", vendor: str = "", partitions: tuple = (), dev: str = "8:0") -> None:
        base = f"sys/block/{name}"
        self.write(f"{base}/size", f"{sectors}\n")
        self.write(f"{base}/removable", f"{removable}\n")
        self.write(f"{base}/queue/rotational", f"{rotational}\n")
        self.write(f"{base}/dev", f"{dev}\n")
        self.write(f"{base}/device/model", f"{model}\n")
        if vendor:
            self.write(f"{base}/device/vendor", f"{vendor}\n")
        major, minor = dev.split(":")
        for index, partition in enumerate(partitions, 1):
            self.write(f"{base}/{partition}/partition", f"{index}\n")
            self.write(f"{base}/{partition}/dev", f"{major}:{int(minor) + index}\n")

    def set_diskstats(self, rows: dict[str, tuple]) -> None:
        """rows: name -> (reads, sectors_read, read_ms, writes, sectors_written, write_ms, busy_ms)."""
        lines = []
        for index, (name, row) in enumerate(rows.items()):
            reads, sectors_read, read_ms, writes, sectors_written, write_ms, busy_ms = row
            lines.append(f"   8 {index:7d} {name} {reads} 0 {sectors_read} {read_ms} {writes} 0 {sectors_written} "
                         f"{write_ms} 0 {busy_ms} {busy_ms} 0 0 0 0 0 0")
        self.write("proc/diskstats", "\n".join(lines) + "\n")

    def add_interface(self, name: str, *, state: str = "up", physical: bool = True, speed: int | None = 1000,
                      wireless: bool = False, arp_type: int = 1, mac: str = "02:00:00:00:00:01") -> None:
        base = f"sys/class/net/{name}"
        self.write(f"{base}/operstate", f"{state}\n")
        self.write(f"{base}/type", f"{arp_type}\n")
        self.write(f"{base}/address", f"{mac}\n")
        if speed is not None:
            self.write(f"{base}/speed", f"{speed}\n")
        if physical:
            (self.root / base / "device").mkdir(parents=True, exist_ok=True)
        if wireless:
            (self.root / base / "wireless").mkdir(parents=True, exist_ok=True)

    def set_net(self, rows: dict[str, tuple[int, int]]) -> None:
        """rows: name -> (rx_bytes, tx_bytes)."""
        lines = [NET_HEADER.rstrip("\n")]
        for name, (rx, tx) in rows.items():
            lines.append(f"{name:>6}: {rx} 10 0 0 0 0 0 0 {tx} 20 0 0 0 0 0 0")
        self.write("proc/net/dev", "\n".join(lines) + "\n")

    def add_gpu(self, card: str = "card0", *, vendor: int | None = 0x1002, device: int | None = 0x73BF,
                driver: str = "amdgpu", busy: int | None = 37, vram: tuple[int, int] | None = (2 << 30, 8 << 30),
                compatible: str = "") -> None:
        base = f"sys/class/drm/{card}/device"
        event = f"DRIVER={driver}\n"
        if vendor is not None:
            event += f"PCI_ID={vendor:04X}:{device:04X}\n"
            self.write(f"{base}/vendor", f"0x{vendor:04x}\n")
            self.write(f"{base}/device", f"0x{device:04x}\n")
        if compatible:
            event += f"OF_COMPATIBLE_0={compatible}\n"
        self.write(f"{base}/uevent", event)
        if busy is not None:
            self.write(f"{base}/gpu_busy_percent", f"{busy}\n")
        if vram is not None:
            self.write(f"{base}/mem_info_vram_used", f"{vram[0]}\n")
            self.write(f"{base}/mem_info_vram_total", f"{vram[1]}\n")
        (self.sys / "class/drm" / f"{card}-HDMI-A-1").mkdir(parents=True, exist_ok=True)

    def add_battery(self, name: str = "BAT0", **values) -> None:
        event = {"NAME": name, "TYPE": "Battery", "STATUS": "Discharging", "PRESENT": "1", "TECHNOLOGY": "Li-ion",
                 "CYCLE_COUNT": "123", "VOLTAGE_NOW": "11400000", "POWER_NOW": "9500000",
                 "ENERGY_FULL_DESIGN": "57000000", "ENERGY_FULL": "50000000", "ENERGY_NOW": "38000000",
                 "CAPACITY": "76", "MODEL_NAME": "X1", "MANUFACTURER": "ACME"}
        event.update({key: str(value) for key, value in values.items()})
        text = "".join(f"POWER_SUPPLY_{key}={value}\n" for key, value in event.items() if value != "")
        self.write(f"sys/class/power_supply/{name}/uevent", text)


@contextmanager
def deny(*suffixes: str):
    """Make ``os.open`` of paths that end with one of ``suffixes`` fail with EACCES, whoever runs the test."""
    real_open = os.open

    def guarded(path, flags, *args, **kwargs):
        if isinstance(path, (str, bytes, os.PathLike)) and os.fspath(path).endswith(suffixes):
            raise PermissionError(13, "Permission denied", os.fspath(path))
        return real_open(path, flags, *args, **kwargs)

    with mock.patch("os.open", guarded):
        yield


@contextmanager
def dead_descriptors():
    """Make ``os.pread`` on a file that was deleted fail with ESRCH, as /proc does for a process that is
    gone (an ordinary file system would go on serving the old content)."""
    real_pread = os.pread

    def guarded(fd, size, offset):
        if os.fstat(fd).st_nlink == 0:
            raise ProcessLookupError(3, "No such process")
        return real_pread(fd, size, offset)

    with mock.patch("os.pread", guarded):
        yield


def cpuinfo(cpus: int, model: str = "Example(R) Core(TM) i7-1234X CPU @ 2.80GHz", flags: str = "fpu vmx sse") -> str:
    stanzas = []
    for number in range(cpus):
        stanzas.append(f"processor\t: {number}\nvendor_id\t: GenuineExample\nmodel name\t: {model}\n"
                       f"cpu MHz\t\t: {1800 + 100 * (number % 4)}.000\nphysical id\t: 0\nsiblings\t: {cpus}\n"
                       f"core id\t\t: {number // 2}\ncpu cores\t: {max(1, cpus // 2)}\nflags\t\t: {flags}\n")
    return "\n".join(stanzas) + "\n"


MEMINFO = """MemTotal:       16000000 kB
MemFree:         4000000 kB
MemAvailable:   10000000 kB
Buffers:          500000 kB
Cached:          5000000 kB
SwapCached:            0 kB
Dirty:              2000 kB
Writeback:             0 kB
Shmem:            400000 kB
Slab:             800000 kB
SReclaimable:     600000 kB
KernelStack:       20000 kB
PageTables:        60000 kB
SwapTotal:       8000000 kB
SwapFree:        7000000 kB
CommitLimit:    16000000 kB
Committed_AS:    9000000 kB
HugePages_Total:       0
Hugepagesize:       2048 kB
"""

NET_HEADER = ("Inter-|   Receive                                                |  Transmit\n"
              " face |bytes    packets errs drop fifo frame compressed multicast|bytes    packets errs drop fifo "
              "colls carrier compressed\n")

LIMITS = """Limit                     Soft Limit           Hard Limit           Units
Max cpu time              unlimited            unlimited            seconds
Max file size             unlimited            unlimited            bytes
Max stack size            8388608              unlimited            bytes
Max processes             63432                63432                processes
Max open files            1024                 524288               files
Max nice priority         0                    0
Max realtime timeout      unlimited            unlimited            us
"""


def desktop_file(name: str, exec_line: str, **keys: str) -> str:
    lines = ["[Desktop Entry]", "Type=Application", f"Name={name}", f"Exec={exec_line}"]
    lines += [f"{key.replace('__', '-')}={value}" for key, value in keys.items()]
    return "\n".join(lines) + "\n"
