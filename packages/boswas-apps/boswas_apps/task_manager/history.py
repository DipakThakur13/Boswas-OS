"""What the Performance page shows: one item per resource with its last 60 samples and its numbers as text.

Standard library only. ``History.push(sample)`` takes a ``sampler.Sample``
and keeps, for the CPU, the memory, every disk, every network interface,
every GPU and every battery found in it, a ``PerfItem``: titles, the
series of its graphs, the big figures under the graph and the pairs of
facts beside them. The page only draws what is here.

A series holds ``LENGTH`` values, the oldest first; None is a gap: no
sample was taken then (the page was not visible, the window was hidden,
sampling was paused).
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field

from . import textutil as fmt
from .sampler import CpuSample, DiskSample, MemorySample, NetSample, Sample
from .sysfs import Battery, GpuInfo

LENGTH = 60
MAX_CORE_GRAPHS = 64         # above this many logical processors the grid shows tiles, not little graphs
KINDS = ("cpu", "memory", "disk", "net", "gpu", "battery")


@dataclass
class Series:
    name: str
    values: deque
    dashed: bool = False


@dataclass
class Graph:
    title: str                       # what is measured, above the graph on the left
    series: list[Series]
    maximum: float | None = 100.0    # the top of the scale; None: the next round number above the data
    unit: str = "percent"            # percent, speed (bytes per second), bitrate, bytes
    floor: float = 0.0               # the smallest top of an automatic scale

    def top(self) -> float:
        if self.maximum is not None:
            return self.maximum
        peak = max((value for series in self.series for value in series.values if value is not None), default=0.0)
        return nice_ceiling(max(peak, self.floor), self.unit)

    def top_label(self) -> str:
        return format_value(self.unit, self.top())


@dataclass
class PerfItem:
    key: str
    kind: str
    title: str
    summary: list[str] = field(default_factory=list)       # the lines under the title in the list
    heading: str = ""                                       # the model, at the right of the page's title
    graphs: list[Graph] = field(default_factory=list)
    stats: list[tuple[str, str]] = field(default_factory=list)
    info: list[tuple[str, str]] = field(default_factory=list)
    composition: list[tuple[str, int]] = field(default_factory=list)   # memory: the segments of the bar
    cores: list[tuple[int, deque]] = field(default_factory=list)      # CPU: a series per logical processor
    core_values: list[tuple[int, float]] = field(default_factory=list)
    note: str = ""                                          # shown instead of a graph when there is none
    last_time: float = 0.0
    replaced: bool = False                                  # the newest sample took the place of the one before

    def current(self) -> float | None:
        """The newest value of the first series (what the little graph in the list ends with)."""
        if self.graphs and self.graphs[0].series and self.graphs[0].series[0].values:
            return self.graphs[0].series[0].values[-1]
        return None


def format_value(unit: str, value: float | None) -> str:
    if value is None:
        return "—"
    if unit == "percent":
        return fmt.percent(value, 0)
    if unit == "bitrate":
        return fmt.bitrate(value)
    if unit == "speed":
        return fmt.speed(value)
    return fmt.size(value)


def nice_ceiling(value: float, unit: str = "speed") -> float:
    """The next round number (1, 2 or 5 times a power of ten of the unit shown) at or above ``value``."""
    if unit == "bitrate":
        scale, base = 1 / 8, 1000.0          # value is bytes per second, the labels are decimal bits
    else:
        scale, base = 1.0, 1024.0
    shown = max(value / scale, 1e-9)
    factor = 1.0
    while shown >= base:
        shown /= base
        factor *= base
    magnitude = 10 ** math.floor(math.log10(shown)) if shown >= 1 else 1.0
    for step in (1, 2, 5, 10):
        if shown <= step * magnitude and step * magnitude < 1000:
            return step * magnitude * factor * scale
    return base * factor * scale             # the next unit: 1 MB after 500 KB


class History:
    """See the module text. ``interval`` is the expected time between samples (for finding gaps)."""

    def __init__(self, length: int = LENGTH) -> None:
        self.length = length
        self.interval = 1.0
        self._items: dict[str, PerfItem] = {}

    def items(self) -> list[PerfItem]:
        """The items in the order of the list: CPU, memory, disks, network, GPUs, batteries."""
        return sorted(self._items.values(), key=lambda item: (KINDS.index(item.kind), _natural(item.key)))

    def item(self, key: str) -> PerfItem | None:
        return self._items.get(key)

    def clear(self) -> None:
        self._items.clear()

    def _series(self, name: str, dashed: bool = False) -> Series:
        return Series(name, deque([None] * self.length, maxlen=self.length), dashed)

    def _add(self, item: PerfItem, time: float, values: list[float | None]) -> None:
        """Append one value to each series of the item, after a gap mark when samples were missed."""
        gap = item.last_time and time - item.last_time > 2.5 * self.interval
        # A sample taken right after another (the page was switched) replaces it: the samples of a
        # graph stay one interval apart.
        again = bool(item.last_time) and time - item.last_time < 0.4 * self.interval
        index = 0
        for graph in item.graphs:
            for series in graph.series:
                if gap:
                    series.values.append(None)
                value = values[index] if index < len(values) else None
                if again and series.values:
                    series.values[-1] = value
                else:
                    series.values.append(value)
                index += 1
        if gap:
            for _number, core in item.cores:
                core.append(None)
        item.replaced = again
        if not again:
            item.last_time = time

    def push(self, sample: Sample) -> None:
        time = sample.time
        if sample.cpu is not None:
            self._cpu(sample.cpu, time)
        elif sample.total_cpu is not None and "cpu" in self._items:
            # Another page is shown: the total, which every sample has, keeps the graph going.
            item = self._items["cpu"]
            self._add(item, time, [sample.total_cpu])
            if not item.replaced:
                for _number, series in item.cores:
                    series.append(None)
        if sample.memory is not None:
            self._memory(sample.memory, time)
        if sample.disks is not None:
            self._keep("disk", {f"disk:{disk.info.name}" for disk in sample.disks})
            for number, disk in enumerate(sample.disks):
                self._disk(number, disk, time)
        if sample.nets is not None:
            self._keep("net", {f"net:{net.info.name}" for net in sample.nets})
            for net in sample.nets:
                self._net(net, time)
        if sample.gpus is not None:
            self._keep("gpu", {f"gpu:{gpu.card}" for gpu in sample.gpus})
            for number, gpu in enumerate(sample.gpus):
                self._gpu(number, gpu, time)
        if sample.batteries is not None:
            self._keep("battery", {f"battery:{battery.name}" for battery in sample.batteries})
            for battery in sample.batteries:
                self._battery(battery, time, len(sample.batteries))

    def _keep(self, kind: str, keys: set[str]) -> None:
        for key in [key for key, item in self._items.items() if item.kind == kind and key not in keys]:
            del self._items[key]

    # --- the items ---
    def _cpu(self, cpu: CpuSample, time: float) -> None:
        item = self._items.get("cpu")
        if item is None:
            item = self._items["cpu"] = PerfItem("cpu", "cpu", "CPU", graphs=[
                Graph("% Utilisation", [self._series("Utilisation")])])
        static = cpu.static
        with_graphs = len(cpu.cores) <= MAX_CORE_GRAPHS
        if with_graphs and [number for number, _values in item.cores] != [number for number, _pct in cpu.cores]:
            known = dict(item.cores)
            item.cores = [(number, known.get(number) or deque([None] * self.length, maxlen=self.length))
                          for number, _pct in cpu.cores]
        elif not with_graphs:
            item.cores = []
        self._add(item, time, [cpu.percent])
        for (_number, series), (_n, percent) in zip(item.cores, cpu.cores):
            if item.replaced and series:
                series[-1] = percent
            else:
                series.append(percent)
        item.core_values = list(cpu.cores)
        item.heading = static.model
        item.summary = [f"{fmt.percent(cpu.percent, 0)}  {fmt.frequency(cpu.mhz)}" if cpu.mhz
                        else fmt.percent(cpu.percent, 0)]
        item.stats = [("Utilisation", fmt.percent(cpu.percent, 0)), ("Speed", fmt.frequency(cpu.mhz)),
                      ("Processes", fmt.count(cpu.processes)), ("Threads", fmt.count(cpu.threads)),
                      ("Up time", fmt.uptime(cpu.uptime))]
        if cpu.load is not None:
            item.stats.append(("Load average", f"{cpu.load.one:.2f}  {cpu.load.five:.2f}  {cpu.load.fifteen:.2f}"))
        info = []
        if static.base_mhz:
            info.append(("Base speed:", fmt.frequency(static.base_mhz)))
        elif static.max_mhz:
            info.append(("Maximum speed:", fmt.frequency(static.max_mhz)))
        info += [("Sockets:", fmt.count(static.sockets)), ("Cores:", fmt.count(static.cores)),
                 ("Logical processors:", fmt.count(static.logical))]
        if static.virtualization == "Virtual machine":
            info.append(("Virtual machine:", "Yes"))
        elif static.virtualization:
            info.append(("Virtualisation:", static.virtualization))
        l1 = static.caches.get("L1d", 0) + static.caches.get("L1i", 0) + static.caches.get("L1", 0)
        for label, value in (("L1 cache:", l1), ("L2 cache:", static.caches.get("L2", 0)),
                             ("L3 cache:", static.caches.get("L3", 0))):
            if value:
                info.append((label, fmt.size(value)))
        item.info = info

    def _memory(self, memory: MemorySample, time: float) -> None:
        item = self._items.get("memory")
        if item is None:
            item = self._items["memory"] = PerfItem("memory", "memory", "Memory", graphs=[
                Graph("Memory usage", [self._series("In use")], maximum=1.0, unit="bytes")])
        item.graphs[0].maximum = float(memory.total or 1)
        self._add(item, time, [float(memory.in_use)])
        item.heading = fmt.size(memory.total)
        item.summary = [f"{_gb(memory.in_use)}/{_gb(memory.total)} GB ({fmt.percent(memory.percent, 0)})"]
        item.stats = [("In use", fmt.size(memory.in_use)), ("Available", fmt.size(memory.available)),
                      ("Committed", f"{_gb(memory.committed)}/{_gb(memory.commit_limit)} GB"),
                      ("Cached", fmt.size(memory.cached)),
                      ("Swap in use", fmt.size(memory.swap_used) if memory.swap_total else "No swap")]
        item.info = [("Total:", fmt.size(memory.total)), ("Free:", fmt.size(memory.free)),
                     ("Shared:", fmt.size(memory.shared)), ("Kernel:", fmt.size(memory.kernel)),
                     ("Waiting to be written:", fmt.size(memory.modified))]
        if memory.swap_total:
            item.info.append(("Swap space:", fmt.size(memory.swap_total)))
        item.composition = [("In use", memory.used), ("Modified", memory.modified), ("Standby", memory.standby),
                            ("Free", memory.free)]

    def _disk(self, number: int, disk: DiskSample, time: float) -> None:
        key = f"disk:{disk.info.name}"
        item = self._items.get(key)
        if item is None:
            item = self._items[key] = PerfItem(key, "disk", "", graphs=[
                Graph("Active time", [self._series("Active time")]),
                Graph("Disk transfer rate", [self._series("Read"), self._series("Write", dashed=True)],
                      maximum=None, unit="speed", floor=100 * 1024)])
        self._add(item, time, [disk.active, disk.read_bps, disk.write_bps])
        item.title = f"Disk {number} ({disk.info.name})"
        item.heading = disk.info.model or disk.info.kind
        item.summary = [disk.info.kind, fmt.percent(disk.active, 0)]
        item.stats = [("Active time", fmt.percent(disk.active, 0)),
                      ("Average response time", f"{disk.response_ms:.1f} ms" if disk.response_ms is not None
                       else "0 ms"),
                      ("Read speed", fmt.speed(disk.read_bps)), ("Write speed", fmt.speed(disk.write_bps))]
        item.info = [("Capacity:", fmt.size(disk.info.capacity)), ("Type:", disk.info.kind),
                     ("System disk:", "Yes" if disk.system else "No"), ("Swap space:", "Yes" if disk.swap else "No")]
        if disk.mounts:
            item.info.append(("Mounted at:", ", ".join(disk.mounts)))

    def _net(self, net: NetSample, time: float) -> None:
        key = f"net:{net.info.name}"
        item = self._items.get(key)
        if item is None:
            item = self._items[key] = PerfItem(key, "net", "", graphs=[
                Graph("Throughput", [self._series("Receive"), self._series("Send", dashed=True)],
                      maximum=None, unit="bitrate", floor=100_000 / 8)])
        self._add(item, time, [net.rx_bps, net.tx_bps])
        item.title = f"{net.info.kind} ({net.info.name})"
        item.heading = net.info.name
        item.summary = [f"S: {fmt.bitrate(net.tx_bps)}", f"R: {fmt.bitrate(net.rx_bps)}"]
        item.stats = [("Send", fmt.bitrate(net.tx_bps)), ("Receive", fmt.bitrate(net.rx_bps))]
        speed = net.info.speed_mbps
        item.info = [("Adapter name:", net.info.name), ("Connection type:", net.info.kind),
                     ("State:", net.info.state.capitalize()),
                     ("Link speed:", "—" if not speed else (f"{speed / 1000:g} Gbps" if speed >= 1000
                                                            else f"{speed} Mbps")),
                     ("IPv4 address:", ", ".join(net.ipv4) or "—"), ("IPv6 address:", ", ".join(net.ipv6) or "—"),
                     ("Sent:", fmt.size(net.tx_total)), ("Received:", fmt.size(net.rx_total))]

    def _gpu(self, number: int, gpu: GpuInfo, time: float) -> None:
        key = f"gpu:{gpu.card}"
        item = self._items.get(key)
        if item is None:
            item = self._items[key] = PerfItem(key, "gpu", "")
            if gpu.busy_percent is not None:
                item.graphs = [Graph("% Utilisation", [self._series("Utilisation")])]
        name = " ".join(part for part in (gpu.vendor, gpu.model) if part)
        item.title = f"GPU {number}"
        item.heading = name
        if gpu.busy_percent is not None and item.graphs:
            self._add(item, time, [float(gpu.busy_percent)])
            item.summary = [name, fmt.percent(gpu.busy_percent, 0)]
            item.stats = [("Utilisation", fmt.percent(gpu.busy_percent, 0))]
            item.note = ""
        else:
            item.graphs = []
            item.summary = [name]
            item.stats = []
            item.note = ("The driver of this graphics adapter does not report its utilisation to the system, "
                         "so there is no graph.")
        if gpu.memory_total:
            item.stats.append(("Video memory", f"{fmt.size(gpu.memory_used or 0)} of {fmt.size(gpu.memory_total)}"))
        item.info = [("Vendor:", gpu.vendor or "—"), ("Model:", gpu.model or "—"), ("Driver:", gpu.driver or "—"),
                     ("Device ID:", gpu.bus_id or "—"), ("Card:", gpu.card)]

    def _battery(self, battery: Battery, time: float, count: int) -> None:
        key = f"battery:{battery.name}"
        item = self._items.get(key)
        if item is None:
            item = self._items[key] = PerfItem(key, "battery", "", graphs=[
                Graph("% Charge", [self._series("Charge")])])
        self._add(item, time, [battery.percent])
        item.title = "Battery" if count == 1 else f"Battery ({battery.name})"
        item.heading = battery.model
        item.summary = [f"{fmt.percent(battery.percent, 0)}  {battery.status}"]
        rate = f"{battery.power_w:.1f} W" if battery.power_w is not None else "—"
        left_label = "Time to full" if battery.status == "Charging" else "Time remaining"
        item.stats = [("Charge", fmt.percent(battery.percent, 0)), ("Status", battery.status),
                      ("Charging rate" if battery.status == "Charging" else "Discharge rate", rate),
                      (left_label, fmt.span(battery.seconds_left) if battery.seconds_left is not None else "—")]
        info = []
        if battery.full_wh is not None:
            info.append(("Full charge:", f"{battery.full_wh:.1f} Wh"))
        if battery.design_wh:
            info.append(("Design capacity:", f"{battery.design_wh:.1f} Wh"))
            if battery.full_wh is not None:
                info.append(("Health:", fmt.percent(min(100.0, 100 * battery.full_wh / battery.design_wh), 0)))
        if battery.cycles:
            info.append(("Charge cycles:", fmt.count(battery.cycles)))
        if battery.technology:
            info.append(("Technology:", battery.technology))
        item.info = info


def _gb(value: int) -> str:
    return f"{value / 1073741824:.1f}"


def _natural(key: str) -> tuple:
    """Sort ``disk:sda10`` after ``disk:sda9``."""
    parts: list[object] = []
    number = ""
    for char in key:
        if char.isdigit():
            number += char
        else:
            if number:
                parts.append((1, int(number), ""))
                number = ""
            parts.append((0, 0, char))
    if number:
        parts.append((1, int(number), ""))
    return tuple(parts)
