"""Text for people: cleaning what comes from outside, and sizes, rates, times as Task Manager shows them.

Names of processes, command lines, desktop entries and unit descriptions
are written by whoever runs or installs a program. ``clean`` makes such a
string safe to show on one line: control characters become spaces and the
Unicode characters that reorder text (bidirectional overrides and
isolates) or hide it (zero-width characters) are replaced, so a name
cannot pose as another one.

Sizes are binary (1 KB = 1024 bytes), as in the Task Manager people know;
network rates are decimal bits per second.
"""

from __future__ import annotations

import time

_REPLACEMENT = "�"
# Bidirectional embedding, override and isolate controls, zero-width characters, line and paragraph
# separators, the byte order mark: none of them belongs in a one-line label.
_HOSTILE = {code: _REPLACEMENT for code in (
    0x061C, 0x200B, 0x200C, 0x200D, 0x200E, 0x200F, 0x2028, 0x2029, 0x202A, 0x202B, 0x202C, 0x202D, 0x202E,
    0x2060, 0x2066, 0x2067, 0x2068, 0x2069, 0xFEFF)}
_CONTROL = {code: " " for code in (*range(0x00, 0x20), 0x7F, *range(0x80, 0xA0))}
_TABLE = {**_CONTROL, **_HOSTILE}


def clean(text: str, limit: int = 4096) -> str:
    """``text`` on one line, without control or text-reordering characters, at most ``limit`` characters."""
    if not isinstance(text, str):
        return ""
    if len(text) > limit:
        text = text[:limit - 1] + "…"
    return text.translate(_TABLE).encode("utf-8", "replace").decode("utf-8")


def decode(data: bytes, limit: int = 4096) -> str:
    """Bytes from the kernel as clean text; bytes that are not UTF-8 show as U+FFFD."""
    return clean(data.decode("utf-8", "replace"), limit)


# --- numbers -----------------------------------------------------------------------------------------

_SIZE_UNITS = ("KB", "MB", "GB", "TB", "PB")


def size(value: float | None, decimals: int = 1) -> str:
    """Bytes as ``512 B``, ``1.5 MB``, ``12.3 GB`` (binary multiples). None: an em dash."""
    if value is None:
        return "—"
    value = max(0.0, float(value))
    if value < 1024:
        return f"{int(value)} B"
    for unit in _SIZE_UNITS:
        value /= 1024
        if value < 1024 or unit == _SIZE_UNITS[-1]:
            break
    if value >= 100 or decimals <= 0:
        return f"{value:,.0f} {unit}"
    return f"{value:.{decimals}f} {unit}"


def megabytes(value: float | None) -> str:
    """Bytes as megabytes with one decimal, the way the memory column shows them: ``1,234.5 MB``."""
    if value is None:
        return "—"
    return f"{max(0.0, float(value)) / 1048576:,.1f} MB"


def rate(value: float | None) -> str:
    """Bytes per second as ``0 MB/s``, ``0.1 MB/s``, ``12.3 MB/s`` (the disk column)."""
    if value is None:
        return "—"
    value = max(0.0, float(value)) / 1048576
    if value < 0.05:
        return "0 MB/s"
    return f"{value:,.1f} MB/s"


def speed(value: float | None) -> str:
    """Bytes per second with a fitting unit: ``312 KB/s``, ``1.2 MB/s``."""
    if value is None:
        return "—"
    value = max(0.0, float(value))
    if value < 1024:
        return f"{value:.0f} B/s"
    return size(value) + "/s"


def bitrate(value: float | None) -> str:
    """Bytes per second as decimal bits per second: ``0 Kbps``, ``56.3 Kbps``, ``1.2 Mbps``, ``2.5 Gbps``."""
    if value is None:
        return "—"
    bits = max(0.0, float(value)) * 8
    if bits < 1000:
        return f"{bits / 1000:.1f} Kbps" if bits >= 50 else "0 Kbps"
    for unit, factor in (("Kbps", 1e3), ("Mbps", 1e6), ("Gbps", 1e9)):
        if bits < factor * 1000 or unit == "Gbps":
            scaled = bits / factor
            return f"{scaled:.0f} {unit}" if scaled >= 100 else f"{scaled:.1f} {unit}"
    return "—"


def percent(value: float | None, decimals: int = 1) -> str:
    """A percentage: ``0%``, ``0.4%``, ``12.5%``, ``100%``."""
    if value is None:
        return "—"
    value = max(0.0, float(value))
    text = f"{value:.{decimals}f}"
    if decimals and text.endswith("0" * decimals):
        text = text[:-decimals - 1]
    return text + "%"


def frequency(mhz: float | None) -> str:
    """A clock frequency: ``800 MHz``, ``2.40 GHz``."""
    if mhz is None or mhz <= 0:
        return "—"
    return f"{mhz / 1000:.2f} GHz" if mhz >= 1000 else f"{mhz:.0f} MHz"


def uptime(seconds: float | None) -> str:
    """A running time as ``d:hh:mm:ss``, the way the CPU page shows the up time."""
    if seconds is None or seconds < 0:
        return "—"
    total = int(seconds)
    days, rest = divmod(total, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{days}:{hours:02d}:{minutes:02d}:{secs:02d}"


def span(seconds: float | None) -> str:
    """A time span in words: ``45 s``, ``12 min``, ``3 h 05 min``, ``2 d 4 h``."""
    if seconds is None or seconds < 0:
        return "—"
    total = int(seconds)
    if total < 60:
        return f"{total} s"
    if total < 3600:
        return f"{total // 60} min"
    if total < 86400:
        return f"{total // 3600} h {total % 3600 // 60:02d} min"
    return f"{total // 86400} d {total % 86400 // 3600} h"


def cpu_time(seconds: float | None) -> str:
    """CPU time used, as ``h:mm:ss``."""
    if seconds is None or seconds < 0:
        return "—"
    total = int(seconds)
    return f"{total // 3600}:{total % 3600 // 60:02d}:{total % 60:02d}"


def moment(epoch: float | None, now: float | None = None) -> str:
    """A point in time in local time: the clock time for today, date and time otherwise."""
    if epoch is None or epoch <= 0:
        return "—"
    try:
        then = time.localtime(epoch)
        today = time.localtime(time.time() if now is None else now)
    except (OverflowError, OSError, ValueError):
        return "—"
    if (then.tm_year, then.tm_yday) == (today.tm_year, today.tm_yday):
        return time.strftime("%H:%M:%S", then)
    return time.strftime("%Y-%m-%d %H:%M", then)


def count(value: int | None) -> str:
    """A whole number with thousands separators."""
    return "—" if value is None else f"{int(value):,}"
