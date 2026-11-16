"""Parsers of the files under /proc (standard library only, no Qt).

Every parser takes the bytes of one file and returns plain data, or None
(or an empty result) when the content is not what the kernel writes; none
of them raises on odd input. ``read_file`` is the only function that
touches the file system; callers pass paths under a root directory so
tests can feed a tree built in a temporary directory.

Formats: proc(5), Documentation/filesystems/proc.rst and
Documentation/admin-guide/iostats.rst of the Linux sources.
"""

from __future__ import annotations

import os
import re
from typing import NamedTuple

from .textutil import decode

PF_KTHREAD = 0x00200000           # include/linux/sched.h: the task is a kernel thread
MAX_FILE = 8 * 1024 * 1024        # /proc/stat of a 4096-CPU machine is well below this
_CHUNK = 65536

STATES = {
    "R": "Running", "S": "Sleeping", "D": "Waiting", "Z": "Zombie", "T": "Stopped", "t": "Traced",
    "I": "Idle", "X": "Dead", "x": "Dead", "K": "Wakekill", "W": "Waking", "P": "Parked",
}


def read_file(path: str, limit: int = MAX_FILE) -> bytes:
    """The whole content of ``path`` (at most ``limit`` bytes). Raises OSError like ``open``."""
    fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
    try:
        chunks = []
        total = 0
        while total < limit:
            chunk = os.read(fd, min(_CHUNK, limit - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        return chunks[0] if len(chunks) == 1 else b"".join(chunks)
    finally:
        os.close(fd)


def read_text(path: str, limit: int = 65536) -> str | None:
    """A small file as stripped text, or None when it cannot be read."""
    try:
        return read_file(path, limit).decode("utf-8", "replace").strip()
    except OSError:
        return None


def read_int(path: str) -> int | None:
    """A file that holds one integer (decimal, or hexadecimal with ``0x``), or None."""
    text = read_text(path, 64)
    if not text:
        return None
    try:
        return int(text, 0)
    except ValueError:
        return None


# --- /proc/PID/stat ------------------------------------------------------------------------------------

class ProcStat(NamedTuple):
    pid: int
    comm: bytes              # as the kernel has it: at most 15 bytes, any bytes but NUL
    state: str
    ppid: int
    pgrp: int
    session: int
    tty_nr: int
    flags: int
    utime: int               # clock ticks, the whole thread group
    stime: int
    priority: int
    nice: int
    threads: int
    starttime: int           # clock ticks after boot
    vsize: int               # bytes
    rss: int                 # pages

    @property
    def kernel_thread(self) -> bool:
        return bool(self.flags & PF_KTHREAD)


def parse_stat(data: bytes) -> ProcStat | None:
    """One line of /proc/PID/stat.

    The command name stands between the first ``(`` and the *last* ``)``:
    it may itself contain spaces, parentheses and line breaks.
    """
    left = data.find(b"(")
    right = data.rfind(b")")
    if left < 1 or right <= left:
        return None
    try:
        pid = int(data[:left])
        fields = data[right + 1:].split()
        return ProcStat(pid, data[left + 1:right], chr(fields[0][0]), int(fields[1]), int(fields[2]),
                        int(fields[3]), int(fields[4]), int(fields[6]), int(fields[11]), int(fields[12]),
                        int(fields[15]), int(fields[16]), int(fields[17]), int(fields[19]), int(fields[20]),
                        int(fields[21]))
    except (ValueError, IndexError):
        return None


def parse_statm(data: bytes) -> tuple[int, int, int] | None:
    """/proc/PID/statm: (size, resident, shared) in pages."""
    fields = data.split()
    try:
        return int(fields[0]), int(fields[1]), int(fields[2])
    except (ValueError, IndexError):
        return None


class ProcIo(NamedTuple):
    read_bytes: int          # bytes this process caused to be fetched from storage
    write_bytes: int         # bytes it caused to be sent to storage


def parse_io(data: bytes) -> ProcIo | None:
    """/proc/PID/io: the two counters of real storage traffic."""
    read = write = None
    for line in data.split(b"\n"):
        if line.startswith(b"read_bytes:"):
            read = _int(line[11:])
        elif line.startswith(b"write_bytes:"):
            write = _int(line[12:])
    if read is None or write is None:
        return None
    return ProcIo(read, write)


def _int(data: bytes) -> int | None:
    try:
        return int(data)
    except ValueError:
        return None


def parse_status(data: bytes) -> dict[str, str]:
    """/proc/PID/status as a dictionary of its fields (values as text, white space trimmed)."""
    result: dict[str, str] = {}
    for line in data.split(b"\n"):
        key, colon, value = line.partition(b":")
        if colon and key and len(key) < 64:
            result[key.decode("ascii", "replace")] = value.strip().decode("utf-8", "replace")
    return result


def status_uid(data: bytes) -> int | None:
    """The effective user ID from /proc/PID/status (the second number of the ``Uid:`` line)."""
    start = data.find(b"\nUid:")
    if start < 0:
        if not data.startswith(b"Uid:"):
            return None
        start = -1
    end = data.find(b"\n", start + 1)
    fields = data[start + 5:end if end >= 0 else len(data)].split()
    try:
        return int(fields[1]) if len(fields) > 1 else int(fields[0])
    except (ValueError, IndexError):
        return None


def parse_cmdline(data: bytes, limit: int = 4096) -> list[str]:
    """/proc/PID/cmdline as its arguments. Bytes that are not UTF-8 show as U+FFFD.

    The arguments are separated by NUL. A program that rewrote its title
    (``sshd: user@pts/0``) leaves one argument with spaces in it; that is
    returned as it is.
    """
    if not data:
        return []
    truncated = len(data) > limit
    data = data[:limit].rstrip(b"\0")
    if not data:
        return []
    arguments = [decode(part, limit) for part in data.split(b"\0")]
    if truncated:
        arguments[-1] += "…"
    return arguments


def command_line(arguments: list[str]) -> str:
    """The arguments as one line for display (not for a shell: nothing is quoted)."""
    return " ".join(arguments)


def parse_smaps_rollup(data: bytes) -> dict[str, int]:
    """/proc/PID/smaps_rollup: the sums in bytes (``Pss``, ``Rss``, ``Swap``, ``Private_Dirty``, ...)."""
    result: dict[str, int] = {}
    for line in data.split(b"\n"):
        key, colon, value = line.partition(b":")
        if not colon or b" " in key:
            continue
        fields = value.split()
        if len(fields) == 2 and fields[1] == b"kB":
            number = _int(fields[0])
            if number is not None:
                result[key.decode("ascii", "replace")] = number * 1024
    return result


class Limit(NamedTuple):
    name: str
    soft: str
    hard: str
    units: str


def parse_limits(data: bytes) -> list[Limit]:
    """/proc/PID/limits: the table of resource limits."""
    lines = data.decode("utf-8", "replace").split("\n")
    result = []
    for line in lines[1:]:
        parts = re.split(r"\s{2,}", line.strip())
        if len(parts) >= 3 and parts[0].startswith("Max "):
            result.append(Limit(parts[0], parts[1], parts[2], parts[3] if len(parts) > 3 else ""))
    return result


def parse_cgroup(data: bytes) -> str:
    """The control group of a process from /proc/PID/cgroup.

    The unified hierarchy (``0::/path``) wins; on a system with the old
    layout the hierarchy named ``systemd`` is used. Empty when neither is
    there.
    """
    legacy = ""
    for line in data.split(b"\n"):
        parts = line.split(b":", 2)
        if len(parts) != 3:
            continue
        if parts[0] == b"0" and not parts[1]:
            return parts[2].decode("utf-8", "replace")
        if parts[1] == b"name=systemd":
            legacy = parts[2].decode("utf-8", "replace")
    return legacy


# --- /proc/stat ----------------------------------------------------------------------------------------

class CpuTimes(NamedTuple):
    """One ``cpu`` line of /proc/stat, in clock ticks. Guest time is part of user and nice."""
    user: int = 0
    nice: int = 0
    system: int = 0
    idle: int = 0
    iowait: int = 0
    irq: int = 0
    softirq: int = 0
    steal: int = 0

    @property
    def total(self) -> int:
        return (self.user + self.nice + self.system + self.idle + self.iowait + self.irq + self.softirq
                + self.steal)

    @property
    def busy(self) -> int:
        return self.user + self.nice + self.system + self.irq + self.softirq + self.steal


class SystemStat(NamedTuple):
    total: CpuTimes
    cpus: dict[int, tuple[int, int]]     # CPU number -> (all ticks, busy ticks); offline CPUs have no line
    count: int                           # how many CPUs have a line
    boot_time: int                       # seconds since the epoch
    running: int
    blocked: int


def _cpu_times(fields: list[bytes]) -> CpuTimes | None:
    try:
        numbers = [int(field) for field in fields[:8]]
    except ValueError:
        return None
    if len(numbers) < 4:               # user, nice, system, idle are in every kernel
        return None
    return CpuTimes(*numbers)


def parse_proc_stat(data: bytes, per_cpu: bool = True) -> SystemStat | None:
    """/proc/stat: the CPU lines (old kernels write fewer columns), boot time, running and blocked tasks.

    With ``per_cpu=False`` the lines of the single CPUs are only counted, not parsed: on a machine with
    thousands of CPUs that is most of the file.
    """
    total = None
    cpus: dict[int, tuple[int, int]] = {}
    count = 0
    boot = running = blocked = 0
    if per_cpu:
        sums = [0] * 8
        for line in data.split(b"\n"):
            if line.startswith(b"cpu"):
                fields = line.split()
                if fields[0] == b"cpu":
                    total = _cpu_times(fields[1:]) or total
                    continue
                number = _int(fields[0][3:])
                if number is None or not 0 <= number < 1 << 20:
                    continue
                try:
                    numbers = [int(field) for field in fields[1:9]]
                except ValueError:
                    continue
                if len(numbers) < 4:
                    continue
                everything = sum(numbers)
                cpus[number] = (everything, everything - numbers[3] - (numbers[4] if len(numbers) > 4 else 0))
                for index, value in enumerate(numbers):
                    sums[index] += value
            elif line.startswith(b"btime "):
                boot = _int(line[6:]) or 0
            elif line.startswith(b"procs_running "):
                running = _int(line[14:]) or 0
            elif line.startswith(b"procs_blocked "):
                blocked = _int(line[14:]) or 0
        count = len(cpus)
        if total is None and cpus:
            total = CpuTimes(*sums)
    else:
        end = data.find(b"\n")
        first = data[:end if end >= 0 else len(data)].split()
        if first and first[0] == b"cpu":
            total = _cpu_times(first[1:])
        if total is None:
            return parse_proc_stat(data, True)
        count = data.count(b"\ncpu")
        for key in (b"\nbtime ", b"\nprocs_running ", b"\nprocs_blocked "):
            start = data.find(key)
            value = 0
            if start >= 0:
                stop = data.find(b"\n", start + 1)
                value = _int(data[start + len(key):stop if stop >= 0 else len(data)]) or 0
            if key == b"\nbtime ":
                boot = value
            elif key == b"\nprocs_running ":
                running = value
            else:
                blocked = value
    if total is None:
        return None
    return SystemStat(total, cpus, count, boot, running, blocked)


# --- memory, load, uptime ------------------------------------------------------------------------------

def parse_meminfo(data: bytes) -> dict[str, int]:
    """/proc/meminfo: sizes in bytes; the lines without a unit (huge page counts) as they are."""
    result: dict[str, int] = {}
    for line in data.split(b"\n"):
        key, colon, value = line.partition(b":")
        if not colon:
            continue
        fields = value.split()
        if not fields:
            continue
        number = _int(fields[0])
        if number is None:
            continue
        if len(fields) > 1 and fields[1] == b"kB":
            number *= 1024
        result[key.strip().decode("ascii", "replace")] = number
    return result


class LoadAvg(NamedTuple):
    one: float
    five: float
    fifteen: float
    running: int             # scheduling entities that are runnable now
    entities: int            # all scheduling entities: every thread of every process
    last_pid: int


def parse_loadavg(data: bytes) -> LoadAvg | None:
    fields = data.split()
    try:
        running, _, entities = fields[3].partition(b"/")
        return LoadAvg(float(fields[0]), float(fields[1]), float(fields[2]), int(running), int(entities),
                       int(fields[4]) if len(fields) > 4 else 0)
    except (ValueError, IndexError):
        return None


def parse_uptime(data: bytes) -> float | None:
    fields = data.split()
    try:
        return float(fields[0])
    except (ValueError, IndexError):
        return None


# --- /proc/cpuinfo -------------------------------------------------------------------------------------

class CpuInfo(NamedTuple):
    model: str               # the name of the processor, "" when the architecture does not say
    vendor: str
    logical: int             # "processor" stanzas
    sockets: int             # 0 when the file does not tell
    cores: int               # 0 when the file does not tell
    mhz: tuple[float, ...]   # "cpu MHz" per logical CPU (x86), else empty
    flags: frozenset[str]


_MODEL_KEYS = ("model name", "Model Name", "cpu model", "Processor", "cpu", "Hardware", "uarch", "isa")
_FLAG_KEYS = ("flags", "Features")


def parse_cpuinfo(data: bytes) -> CpuInfo:
    """/proc/cpuinfo: model, counts and flags. The keys differ between architectures; x86, ARM, MIPS,
    PowerPC and RISC-V spellings of the model are known."""
    text = data.decode("utf-8", "replace")
    logical = 0
    models: dict[str, str] = {}
    vendor = ""
    mhz: list[float] = []
    packages: set[str] = set()
    cores: set[tuple[str, str]] = set()
    flags: frozenset[str] = frozenset()
    package = "0"
    for line in text.split("\n"):
        key, colon, value = line.partition(":")
        if not colon:
            continue
        key, value = key.strip(), value.strip()
        if key == "processor" and value.isdigit():
            logical += 1
            package = "0"
        elif key in _MODEL_KEYS:
            models.setdefault(key, value)
        elif key == "vendor_id" and not vendor:
            vendor = value
        elif key == "cpu MHz":
            try:
                mhz.append(float(value))
            except ValueError:
                pass
        elif key == "physical id":
            package = value
            packages.add(value)
        elif key == "core id":
            cores.add((package, value))
        elif key in _FLAG_KEYS and not flags:
            flags = frozenset(value.split())
    model = ""
    for key in _MODEL_KEYS:
        if models.get(key):
            model = models[key]
            break
    return CpuInfo(decode(model.encode("utf-8", "replace"), 200), decode(vendor.encode("utf-8", "replace"), 64),
                   logical, len(packages), len(cores), tuple(mhz), flags)


# --- disks ---------------------------------------------------------------------------------------------

class DiskCounters(NamedTuple):
    reads: int               # requests completed
    sectors_read: int        # 512-byte sectors, whatever the device's block size
    read_ms: int
    writes: int
    sectors_written: int
    write_ms: int
    busy_ms: int             # time with at least one request in flight (a 32-bit counter in the kernel)


def parse_diskstats(data: bytes) -> dict[str, DiskCounters]:
    """/proc/diskstats by device name. Lines of 14, 18 or 20 fields; the 7-field partition lines of
    Linux 2.6 before .25 carry no times and are skipped."""
    result: dict[str, DiskCounters] = {}
    for line in data.split(b"\n"):
        fields = line.split()
        if len(fields) < 14:
            continue
        try:
            numbers = [int(field) for field in fields[3:13]]
        except ValueError:
            continue
        result[fields[2].decode("utf-8", "replace")] = DiskCounters(
            numbers[0], numbers[2], numbers[3], numbers[4], numbers[6], numbers[7], numbers[9])
    return result


class Mount(NamedTuple):
    device: str              # "major:minor"
    root: str
    mount_point: str
    fstype: str
    source: str


_OCTAL = re.compile(r"\\([0-7]{3})")


def _unescape(text: str) -> str:
    return _OCTAL.sub(lambda match: chr(int(match.group(1), 8)), text)


def parse_mountinfo(data: bytes) -> list[Mount]:
    """/proc/self/mountinfo. Paths with spaces, tabs, line breaks and backslashes are written with octal
    escapes and are decoded here; the optional fields before `` - `` are skipped."""
    result = []
    for line in data.decode("utf-8", "replace").split("\n"):
        left, dash, right = line.partition(" - ")
        fields = left.split(" ")
        tail = right.split(" ")
        if not dash or len(fields) < 6 or len(tail) < 2:
            continue
        result.append(Mount(fields[2], _unescape(fields[3]), _unescape(fields[4]), tail[0], _unescape(tail[1])))
    return result


def parse_swaps(data: bytes) -> list[str]:
    """/proc/swaps: the files and devices in use as swap space."""
    lines = data.decode("utf-8", "replace").split("\n")[1:]
    return [_unescape(line.split()[0]) for line in lines if line.split()]


# --- network -------------------------------------------------------------------------------------------

class NetCounters(NamedTuple):
    rx_bytes: int
    rx_packets: int
    tx_bytes: int
    tx_packets: int


def parse_net_dev(data: bytes) -> dict[str, NetCounters]:
    """/proc/net/dev by interface name."""
    result: dict[str, NetCounters] = {}
    for line in data.split(b"\n"):
        name, colon, rest = line.rpartition(b":")
        if not colon:
            continue
        fields = rest.split()
        try:
            counters = NetCounters(int(fields[0]), int(fields[1]), int(fields[8]), int(fields[9]))
        except (ValueError, IndexError):
            continue
        name = name.strip()
        if name:
            result[name.decode("utf-8", "replace")] = counters
    return result


def parse_if_inet6(data: bytes) -> dict[str, list[tuple[str, int, int]]]:
    """/proc/net/if_inet6: per interface, its IPv6 addresses as (address, prefix length, scope).

    The scope is the kernel's number: 0x00 global, 0x20 link-local, 0x10 loopback, 0x40 site-local.
    """
    import ipaddress
    result: dict[str, list[tuple[str, int, int]]] = {}
    for line in data.split(b"\n"):
        fields = line.split()
        if len(fields) < 6 or len(fields[0]) != 32:
            continue
        try:
            address = str(ipaddress.IPv6Address(int(fields[0], 16)))
            prefix, scope = int(fields[2], 16), int(fields[3], 16)
        except ValueError:
            continue
        result.setdefault(fields[5].decode("utf-8", "replace"), []).append((address, prefix, scope))
    return result


# --- counters ------------------------------------------------------------------------------------------

def delta(now: int, before: int, width: int = 64) -> int:
    """How far a kernel counter moved from ``before`` to ``now``.

    A counter that went backwards has either wrapped around (a ``width``-bit
    counter that was close to its end: the distance over the wrap is
    returned) or was reset (a device that went away and came back: 0).
    """
    if now >= before:
        return now - before
    modulus = 1 << width
    if before < modulus and modulus - before < modulus >> 2 and now < modulus >> 2:
        return now + modulus - before
    if width == 64 and before < 1 << 32 and (1 << 32) - before < 1 << 30 and now < 1 << 30:
        return now + (1 << 32) - before       # a 32-bit kernel counts in 32 bits
    return 0


def tty_name(tty_nr: int) -> str:
    """The controlling terminal of /proc/PID/stat as a name: ``pts/3``, ``tty2``, ``ttyS0``; "" for none."""
    if tty_nr <= 0:
        return ""
    major = (tty_nr >> 8) & 0xFFF
    minor = (tty_nr & 0xFF) | ((tty_nr >> 12) & 0xFFF00)
    if 136 <= major <= 143:
        return f"pts/{minor + (major - 136) * 256}"
    if major == 4:
        return f"tty{minor}" if minor < 64 else f"ttyS{minor - 64}"
    if major == 5 and minor == 1:
        return "console"
    return f"{major}:{minor}"
