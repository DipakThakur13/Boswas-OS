"""Can this computer and session run the Android runtime (Waydroid)?

Each check reads public files only (through Root) and returns PASS, WARN,
FAIL or UNKNOWN with a sentence for the user. FAIL means the runtime cannot
work here; WARN means it works with a limitation. Facts behind the checks
(from Waydroid 1.6 source and Debian 13's kernel): Waydroid needs the binder
IPC driver (Debian ships it as the signed module binder_linux), a Wayland
session, an x86_64 CPU with SSSE3 (without SSE4.2 Waydroid falls back to
32-bit x86 images), and a GPU driver other than NVIDIA's proprietary one for
hardware-accelerated graphics.
"""

from __future__ import annotations

import re

from . import config, system
from .system import Host, Root

PASS, WARN, FAIL, UNKNOWN = "PASS", "WARN", "FAIL", "UNKNOWN"
IDS = ("binder", "wayland", "cpu", "kernel", "gpu", "disk", "secure-boot")
TITLES = {"binder": "Android binder driver", "wayland": "Wayland session", "cpu": "Processor",
          "kernel": "Kernel", "gpu": "Graphics acceleration", "disk": "Free disk space",
          "secure-boot": "Secure Boot"}
BINDER_NODES = ("/dev/binder", "/dev/anbox-binder", "/dev/binderfs/binder", "/dev/binderfs/anbox-binder")
SECURE_BOOT_VAR = "/sys/firmware/efi/efivars/SecureBoot-8be4df61-93ca-11d2-aa0d-00e098032b8c"
IMAGES_NEED = 4 * 1024 ** 3        # system + vendor images (~2.5 GB) plus the downloaded archives
HOME_NEED = 1024 ** 3
UNSUPPORTED_GPU_DRIVERS = frozenset({"nvidia"})


def _check(check_id: str, status: str, detail: str) -> dict:
    return {"id": check_id, "status": status, "title": TITLES[check_id], "detail": detail}


def _gib(n: int) -> str:
    return f"{n / 1024 ** 3:.1f} GB"


def cpu_flags(root: Root) -> set[str]:
    text = root.read("/proc/cpuinfo", 4 * 1024 * 1024) or ""
    for line in text.splitlines():
        if line.startswith("flags"):
            return set(line.partition(":")[2].split())
    return set()


def runtime_abis(root: Root, cfg: dict | None = None) -> tuple[str, ...]:
    """ABIs the Android runtime runs here: Waydroid's configured arch, else what the CPU allows."""
    arch = (cfg or {}).get("arch")
    if arch == "x86":
        return ("x86",)
    if arch == "x86_64":
        return ("x86_64", "x86")
    flags = cpu_flags(root)
    if flags and "sse4_2" not in flags:
        return ("x86",)
    return ("x86_64", "x86")


def gpu(root: Root) -> dict:
    """{"acceleration", "driver", "render_node"} from /sys/class/drm (no device identifiers)."""
    if not root.isdir("/sys/class/drm"):
        return {"acceleration": "unknown", "driver": None, "render_node": None}
    nodes = [n for n in root.listdir("/sys/class/drm") if re.fullmatch(r"renderD\d+", n)]
    unsupported = None
    for node in nodes:
        driver = root.readlink_name(f"/sys/class/drm/{node}/device/driver")
        if driver is None:
            uevent = root.read(f"/sys/class/drm/{node}/device/uevent", 65536) or ""
            match = re.search(r"^DRIVER=(\S+)$", uevent, re.MULTILINE)
            driver = match.group(1) if match else None
        if driver and driver not in UNSUPPORTED_GPU_DRIVERS:
            return {"acceleration": "hardware", "driver": driver, "render_node": f"/dev/dri/{node}"}
        if driver:
            unsupported = driver
    return {"acceleration": "software", "driver": unsupported, "render_node": None}


def binder_check(root: Root, host: Host) -> dict:
    for node in BINDER_NODES:
        if root.exists(node):
            return _check("binder", PASS, f"Binder device present ({node}).")
    module_dir = f"/lib/modules/{host.release}/kernel/drivers/android"
    for name in root.listdir(module_dir):
        if re.fullmatch(r"binder_linux\.ko(\.xz|\.zst|\.gz)?", name):
            return _check("binder", PASS, "The kernel's signed binder_linux module is available; it is loaded "
                                          "when the Android runtime starts.")
    if re.search(r"^\S*\s+binder$", root.read("/proc/filesystems", 65536) or "", re.MULTILINE):
        return _check("binder", PASS, "The kernel provides binderfs.")
    return _check("binder", FAIL, f"The running kernel ({host.release}) has no Android binder support, which the "
                                  "Android runtime requires.")


def wayland_check(host: Host) -> dict:
    session = (host.environ.get("XDG_SESSION_TYPE") or "").lower()
    if session == "wayland" or host.environ.get("WAYLAND_DISPLAY"):
        return _check("wayland", PASS, "This is a Wayland session (Plasma on Wayland).")
    if session in ("x11", "tty", "mir"):
        return _check("wayland", FAIL, f"Android apps need a Wayland session; this session is {session}. Choose "
                                       "\"Plasma (Wayland)\" on the login screen.")
    return _check("wayland", UNKNOWN, "Not run from a desktop session; Android apps need Plasma on Wayland.")


def cpu_check(root: Root, host: Host) -> dict:
    if host.machine != "x86_64":
        return _check("cpu", FAIL, f"The Android runtime of Boswas OS supports x86_64 computers ({host.machine}).")
    flags = cpu_flags(root)
    if not flags:
        return _check("cpu", UNKNOWN, "The processor's features could not be read.")
    if "ssse3" not in flags:
        return _check("cpu", FAIL, "The processor lacks SSSE3, which Android on x86 requires.")
    if "sse4_2" not in flags:
        return _check("cpu", WARN, "The processor lacks SSE4.2: Android runs as 32-bit x86, and apps built only "
                                   "for x86_64 will not run.")
    return _check("cpu", PASS, "x86_64 processor with SSSE3 and SSE4.2 (64-bit Android, x86_64 and x86 apps).")


def kernel_check(root: Root, host: Host) -> dict:
    match = re.match(r"(\d+)\.(\d+)", host.release)
    if not match:
        return _check("kernel", UNKNOWN, f"Unrecognised kernel release {host.release}.")
    version = (int(match.group(1)), int(match.group(2)))
    if not root.exists("/sys/fs/cgroup/cgroup.controllers"):
        return _check("kernel", WARN, "No unified cgroup hierarchy found; the Android container (LXC) may not start.")
    if version < (5, 4):
        return _check("kernel", WARN, f"Kernel {host.release} is older than the kernels the runtime is tested with.")
    return _check("kernel", PASS, f"Kernel {host.release} with cgroup v2 (container support).")


def gpu_check(root: Root) -> dict:
    info = gpu(root)
    if info["acceleration"] == "hardware":
        return _check("gpu", PASS, f"Hardware-accelerated graphics through the {info['driver']} driver.")
    if info["acceleration"] == "software":
        if info["driver"] in UNSUPPORTED_GPU_DRIVERS:
            return _check("gpu", WARN, "NVIDIA's proprietary driver is not supported by the Android runtime: "
                                       "Android uses software rendering (slower graphics).")
        return _check("gpu", WARN, "No supported graphics render device: Android uses software rendering "
                                   "(slower graphics).")
    return _check("gpu", UNKNOWN, "Graphics devices could not be read.")


def disk_check(root: Root, host: Host, cfg: dict | None, initialized: bool) -> dict:
    images_path = (cfg or {}).get("images_path") or config.DEFAULT_IMAGES
    home = host.home()
    home_free = system.free_bytes(home) if home else None
    home_note = (f" Only {_gib(home_free)} free in your home folder for Android app data."
                 if home_free is not None and home_free < HOME_NEED else "")
    if initialized and root.isfile(f"{images_path}/system.img"):
        status = WARN if home_note else PASS
        return _check("disk", status, "The Android system images are installed." + home_note)
    free = root.free_bytes(images_path)
    if free is None:
        return _check("disk", UNKNOWN, "Free space for the Android images could not be determined.")
    if free < IMAGES_NEED:
        return _check("disk", FAIL, f"The Android images need about {_gib(IMAGES_NEED)} free in /var/lib; "
                                    f"{_gib(free)} is free.")
    return _check("disk", WARN if home_note else PASS,
                  f"{_gib(free)} free for the Android images (about {_gib(IMAGES_NEED)} needed)." + home_note)


def secure_boot_check(root: Root) -> dict:
    data = root.read_bytes(SECURE_BOOT_VAR, 64)
    if data is None:
        if root.isdir("/sys/firmware/efi"):
            return _check("secure-boot", UNKNOWN, "The Secure Boot state could not be read. It does not affect the "
                                                  "Android runtime: binder_linux is part of Debian's signed kernel.")
        return _check("secure-boot", PASS, "Legacy BIOS boot. Secure Boot does not affect the Android runtime.")
    enabled = len(data) >= 5 and data[4] == 1
    if enabled:
        return _check("secure-boot", PASS, "Secure Boot is enabled. binder_linux is part of Debian's signed kernel "
                                           "and loads under Secure Boot; no unsigned module is needed.")
    return _check("secure-boot", PASS, "Secure Boot is off. It is not needed for the Android runtime (and would not "
                                       "block it: binder_linux is signed).")


def compatibility(root: Root, host: Host, cfg: dict | None = None, initialized: bool = False) -> dict:
    checks = [binder_check(root, host), wayland_check(host), cpu_check(root, host), kernel_check(root, host),
              gpu_check(root), disk_check(root, host, cfg, initialized), secure_boot_check(root)]
    return {"supported": not any(c["status"] == FAIL for c in checks), "checks": checks}


def blocking_failures(compat: dict, ids: tuple[str, ...] = ("binder", "cpu")) -> list[dict]:
    """FAILed checks that make installing the runtime pointless (hardware/kernel, not the session)."""
    return [c for c in compat["checks"] if c["id"] in ids and c["status"] == FAIL]
