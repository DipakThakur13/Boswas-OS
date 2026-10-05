#!/usr/bin/env python3
"""QEMU boot tests for the Boswas OS ISO.

Scenario "uefi-secureboot"
    OVMF with Secure Boot enforced and the Microsoft keys enrolled boots the
    ISO through the normal path: shim -> signed GRUB (Boswas menu, live entry
    auto-selected after the timeout) -> signed kernel -> Boswas boot splash ->
    live system -> KDE Plasma. Verified from screenshots (Boswas navy and gold
    on screen, the desktop panel with the gold launcher, screen differs from
    the boot menu); screenshots are kept for review.

Scenario "usb" (release blocker)
    The ISO attached as a USB stick, UEFI with Secure Boot, plus an internal
    NVMe disk with known content: boots to the Boswas boot menu, the default
    entry starts the live session (boot splash), the desktop opens directly
    with no installer, wizard or browser started; applications, terminal,
    system settings, Boswas Control Center, Security Center and the
    Compatibility Manager open; all ten presets apply; lock and login screens;
    clean shutdown; the internal disk is bit-for-bit unchanged. A second boot
    chooses "Install Boswas OS" and reaches the Boswas-branded installer.

Scenario "serial"
    Boots the ISO's live kernel directly with a serial console, logs in as the
    live user and runs functional checks: boswas commands, firewall, AppArmor,
    audit, KDE, Wine, network. With --fixtures-iso (built by
    tests/compatibility/build_fixtures.sh) it also installs and launches the
    Windows test application with boswas-winapp under the real kernel's
    AppArmor (the boswas-winapp profile enforcing). Finally powers the VM off.

Uses KVM when /dev/kvm is available, TCG otherwise (much slower).

Results are printed as "PASS|FAIL|SKIP<TAB>description" and appended to
$BOSWAS_TEST_REPORT. Exit status is non-zero if anything failed.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

OVMF_DIR = Path("/usr/share/OVMF")
ANSI = re.compile(rb"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[()][0-9A-Za-z]|\x1b[=>]")
FAILED = False


# --- reporting -------------------------------------------------------------------

def record(status: str, description: str) -> None:
    global FAILED
    if status == "FAIL":
        FAILED = True
    line = f"{status}\tboot/qemu\t{description}\n"
    print(f"  {status:<4}  {description}", flush=True)
    report = os.environ.get("BOSWAS_TEST_REPORT")
    if report:
        with open(report, "a", encoding="utf-8") as fh:
            fh.write(line)


def check(condition: bool, description: str) -> bool:
    record("PASS" if condition else "FAIL", description)
    return condition


def log(msg: str) -> None:
    print(f"[boot-test {time.strftime('%H:%M:%S')}] {msg}", flush=True)


# --- images ------------------------------------------------------------------------

def read_ppm(path: Path) -> tuple[int, int, bytes]:
    data = path.read_bytes()
    fields, pos = [], 0
    while len(fields) < 4:
        while data[pos:pos + 1].isspace():
            pos += 1
        if data[pos:pos + 1] == b"#":
            pos = data.index(b"\n", pos)
            continue
        start = pos
        while not data[pos:pos + 1].isspace():
            pos += 1
        fields.append(data[start:pos])
    if fields[0] != b"P6":
        raise ValueError("expected binary PPM")
    w, h = int(fields[1]), int(fields[2])
    return w, h, data[pos + 1:pos + 1 + w * h * 3]


def is_navy(r: int, g: int, b: int) -> bool:
    """Boswas navy family (backgrounds): dark and clearly bluish."""
    return (299 * r + 587 * g + 114 * b) // 1000 < 70 and 4 <= b - r <= 60


def is_gold(r: int, g: int, b: int) -> bool:
    """Boswas gold family (the mark, accents, selections)."""
    return r >= 120 and r - b >= 40 and g - b >= 20


def analyse(path: Path, region: tuple[int, int, int, int] | None = None) -> dict:
    """Palette statistics on a sample of pixels (optionally of a region x0, y0, x1, y1)."""
    w, h, px = read_ppm(path)
    x0, y0, x1, y1 = region or (0, 0, w, h)
    total = black = navy = gold = 0
    luma_sum = 0
    step = 7 if region is None else 1
    for y in range(max(0, y0), min(h, y1)):
        for x in range(max(0, x0) + (y % step), min(w, x1), step):
            i = y * w + x
            r, g, b = px[3 * i], px[3 * i + 1], px[3 * i + 2]
            total += 1
            luma = (299 * r + 587 * g + 114 * b) // 1000
            luma_sum += luma
            black += luma < 8
            navy += is_navy(r, g, b)
            gold += is_gold(r, g, b)
    total = total or 1
    return {"w": w, "h": h, "black": black / total, "navy": navy / total, "gold": gold / total,
            "luma": luma_sum / total}


def boswas_screen(stats: dict) -> bool:
    """A Boswas-branded screen: navy dominates and gold accents are present."""
    return stats["navy"] > 0.45 and stats["gold"] > 0.0008 and stats["black"] < 0.5


def passphrase_prompt(path: Path) -> bool:
    """The Plymouth disk-unlock prompt: the passphrase field's gold border is a
    long horizontal gold line below the mark (while booting, only the small
    spinner is gold there)."""
    w, h, px = read_ppm(path)
    for y in range(int(h * 0.6), int(h * 0.95)):
        row = y * w
        if sum(is_gold(px[3 * i], px[3 * i + 1], px[3 * i + 2]) for i in range(row, row + w)) >= 150:
            return True
    return False


def desktop_screen(path: Path) -> bool:
    """The Plasma desktop: a Boswas background plus the panel along the bottom,
    whose launcher shows the gold Boswas mark (boot and start-up splashes have
    an empty bottom edge)."""
    stats = analyse(path)
    panel = analyse(path, (0, stats["h"] - 64, stats["w"], stats["h"]))
    return stats["black"] < 0.6 and stats["navy"] > 0.3 and panel["gold"] > 0.0015


def difference(a: Path, b: Path) -> float:
    wa, ha, pa = read_ppm(a)
    wb, hb, pb = read_ppm(b)
    if (wa, ha) != (wb, hb):
        return 255.0
    diff = n = 0
    for i in range(0, len(pa), 3 * 11):
        diff += abs(pa[i] - pb[i]) + abs(pa[i + 1] - pb[i + 1]) + abs(pa[i + 2] - pb[i + 2])
        n += 3
    return diff / n


def to_png(ppm: Path, png: Path) -> None:
    if shutil.which("pnmtopng"):
        with png.open("wb") as fh:
            subprocess.run(["pnmtopng", str(ppm)], stdout=fh, stderr=subprocess.DEVNULL, check=False)


# --- QEMU control ------------------------------------------------------------------

class VM:
    def __init__(self, name: str, workdir: Path, outdir: Path, qemu_args: list[str]):
        self.name, self.workdir, self.outdir = name, workdir, outdir
        self.mon_path = workdir / f"{name}.mon"
        self.ser_path = workdir / f"{name}.ser"
        self.serial_log = (outdir / f"{name}-serial.log").open("wb")
        self.buffer = b""
        if os.access("/dev/kvm", os.R_OK | os.W_OK):
            accel = ["-accel", "kvm", "-cpu", "host"]
        else:
            accel = ["-accel", "tcg,thread=multi", "-cpu", "max"]
        cmd = ["qemu-system-x86_64", *accel, "-m", "4096", "-smp", "4",
               "-display", "none", "-vga", "std",
               "-nic", "user,model=virtio-net-pci",
               "-monitor", f"unix:{self.mon_path},server=on,wait=off",
               "-chardev", f"socket,id=ser0,path={self.ser_path},server=on,wait=off",
               "-serial", "chardev:ser0", *qemu_args]
        log(f"{name}: starting QEMU ({accel[1]})")
        self.proc = subprocess.Popen(cmd, stdout=(outdir / f"{name}-qemu.log").open("wb"), stderr=subprocess.STDOUT)
        self.mon = self._connect(self.mon_path)
        self.ser = self._connect(self.ser_path)
        self.ser.setblocking(False)
        self._hmp_read()

    def _connect(self, path: Path) -> socket.socket:
        deadline = time.time() + 30
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(f"QEMU exited early (see {self.name}-qemu.log)")
            try:
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.connect(str(path))
                return s
            except OSError:
                time.sleep(0.2)
        raise RuntimeError(f"cannot connect to {path}")

    def _hmp_read(self) -> str:
        """Read monitor output up to the next prompt (best effort under slow emulation)."""
        self.mon.settimeout(60)
        data = b""
        try:
            while not data.endswith(b"(qemu) "):
                chunk = self.mon.recv(4096)
                if not chunk:
                    break
                data += chunk
        except socket.timeout:
            log(f"{self.name}: monitor did not answer within 60 s")
        return data.decode(errors="replace")

    def screendump(self, label: str) -> Path:
        ppm = self.workdir / f"{self.name}-{label}.ppm"
        self.mon.sendall(f"screendump {ppm}\n".encode())
        self._hmp_read()
        for _ in range(50):
            if ppm.exists() and ppm.stat().st_size > 0:
                break
            time.sleep(0.1)
        to_png(ppm, self.outdir / f"{self.name}-{label}.png")
        return ppm

    KEYS = {" ": "spc", ",": "comma", "=": "equal", "-": "minus", ".": "dot", "/": "slash", "_": "shift-minus",
            ":": "shift-semicolon"}

    def key(self, name: str) -> None:
        """Press a key on the VM keyboard (QEMU sendkey names, e.g. "e", "end", "ctrl-x")."""
        self.mon.sendall(f"sendkey {name}\n".encode())
        self._hmp_read()
        time.sleep(0.15)

    def nudge(self) -> None:
        """A small mouse movement: real user input for the guest, so the enforced
        idle screen lock (10 minutes) does not start while the test works over
        the serial console."""
        for move in ("mouse_move 6 4", "mouse_move -6 -4"):
            self.mon.sendall(f"{move}\n".encode())
            self._hmp_read()

    def type_text(self, text: str) -> None:
        for ch in text:
            if ch.isupper():
                self.key(f"shift-{ch.lower()}")
            else:
                self.key(self.KEYS.get(ch, ch))

    def pump(self, seconds: float = 0.2) -> None:
        end = time.time() + seconds
        while time.time() < end:
            try:
                chunk = self.ser.recv(65536)
                if chunk:
                    self.serial_log.write(chunk)
                    self.serial_log.flush()
                    self.buffer += ANSI.sub(b"", chunk).replace(b"\r", b"")
                    continue
            except BlockingIOError:
                pass
            time.sleep(0.05)

    def expect(self, pattern: bytes, timeout: float) -> re.Match | None:
        rx = re.compile(pattern)
        deadline = time.time() + timeout
        while time.time() < deadline:
            m = rx.search(self.buffer)
            if m:
                self.buffer = self.buffer[m.end():]
                return m
            if self.proc.poll() is not None:
                return None
            self.pump(0.5)
        return None

    def send(self, text: str) -> None:
        self.ser.setblocking(True)
        self.ser.sendall(text.encode())
        self.ser.setblocking(False)

    def alive(self) -> bool:
        return self.proc.poll() is None

    def stop(self, timeout: float = 120) -> None:
        if self.alive():
            try:
                self.mon.sendall(b"quit\n")
            except OSError:
                pass
            try:
                self.proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.serial_log.close()


class Shell:
    """Command runner over the VM serial console."""

    def __init__(self, vm: VM):
        self.vm, self.n = vm, 0

    def run(self, cmd: str, timeout: float = 180) -> tuple[int | None, str]:
        self.n += 1
        tag = f"{self.n}"
        # The markers are assembled by the shell, so the echoed command line
        # never matches the patterns.
        self.vm.send(f"echo \"@@BEGIN-\"\"{tag}@@\"; {cmd}; echo \"@@END-\"\"{tag}@@ $?\"\n")
        m = self.vm.expect(rf"@@END-{tag}@@ (\d+)".encode(), timeout)
        return (int(m.group(1)) if m else None), self._between(tag)

    def _between(self, tag: str) -> str:
        """Command output: the text between the markers in the serial log."""
        raw = Path(self.vm.serial_log.name).read_bytes()
        clean = ANSI.sub(b"", raw).replace(b"\r", b"").decode(errors="replace")
        begin = clean.rfind(f"@@BEGIN-{tag}@@\n")
        end = clean.find(f"@@END-{tag}@@", begin)
        return clean[begin + len(f"@@BEGIN-{tag}@@\n"):end].strip("\n") if begin >= 0 and end >= 0 else ""


# --- scenarios ------------------------------------------------------------------

def iso_extract(iso: Path, dest: Path, pattern: str) -> Path | None:
    listing = subprocess.run(["xorriso", "-indev", str(iso), "-find", "/live", "-name", pattern],
                             capture_output=True, text=True).stdout.split()
    files = sorted(f.strip("'") for f in listing if f.strip("'").startswith("/live/"))
    if not files:
        return None
    target = dest / Path(files[-1]).name
    subprocess.run(["xorriso", "-osirrox", "on", "-indev", str(iso), "-extract", files[-1], str(target)],
                   capture_output=True, check=True)
    return target


def scenario_uefi(iso: Path, workdir: Path, outdir: Path, timeout: float) -> None:
    code = OVMF_DIR / "OVMF_CODE_4M.secboot.fd"
    vars_src = OVMF_DIR / "OVMF_VARS_4M.ms.fd"
    if not (code.exists() and vars_src.exists()):
        record("SKIP", "UEFI Secure Boot boot (OVMF Secure Boot firmware not installed)")
        return
    vars_copy = workdir / "uefi-vars.fd"
    shutil.copyfile(vars_src, vars_copy)
    vm = VM("uefi-secureboot", workdir, outdir, [
        "-machine", "q35,smm=on",
        "-global", "driver=cfi.pflash01,property=secure,value=on",
        "-drive", f"if=pflash,format=raw,unit=0,readonly=on,file={code}",
        "-drive", f"if=pflash,format=raw,unit=1,file={vars_copy}",
        "-drive", f"file={iso},media=cdrom,readonly=on,if=ide",
    ])
    try:
        start = time.time()
        menu = None
        log("uefi-secureboot: waiting for the Boswas boot menu")
        # The Boswas boot menu: navy with gold accents while GRUB counts down.
        while time.time() - start < min(timeout, 600) and vm.alive():
            time.sleep(10)
            shot = vm.screendump(f"t{int(time.time() - start):04d}")
            stats = analyse(shot)
            if boswas_screen(stats):
                menu = shot
                shutil.copyfile(shot, workdir / "menu.ppm")
                to_png(shot, outdir / "uefi-secureboot-boot-menu.png")
                break
        if not check(menu is not None, "UEFI + Secure Boot: Boswas boot menu displayed (shim -> signed GRUB)"):
            return
        # Live entry is selected automatically after the menu timeout.
        desktop = splash = None
        stable = 0
        previous = None
        while time.time() - start < timeout and vm.alive():
            time.sleep(30)
            shot = vm.screendump(f"t{int(time.time() - start):04d}")
            stats = analyse(shot)
            changed_from_menu = difference(shot, workdir / "menu.ppm") > 12
            if changed_from_menu and splash is None and boswas_screen(stats) and not desktop_screen(shot):
                splash = shot
                to_png(shot, outdir / "uefi-secureboot-splash.png")
            if changed_from_menu and desktop_screen(shot):
                stable = stable + 1 if previous is not None and difference(shot, previous) < 4 else 1
                previous = shot
                if stable >= 2:
                    desktop = shot
                    break
            else:
                stable, previous = 0, None
        if desktop is not None:
            to_png(desktop, outdir / "uefi-secureboot-desktop.png")
        check(splash is not None, "UEFI + Secure Boot: a Boswas splash is shown between the boot menu and the desktop "
                                  "(screenshot uefi-secureboot-splash.png)")
        check(desktop is not None,
              f"UEFI + Secure Boot: live system reached the Boswas desktop ({int(time.time() - start)} s)")
    finally:
        vm.stop(timeout=30)


# Installers, installation wizards and browsers that must not start on their own
# (matched on the full command line: process names are truncated to 15 characters).
INSTALLER_PROCS = "(^|/)(calamares|ubiquity|debian-installer|anaconda|plasma-welcome|firefox|firefox-esr|chromium|konqueror)( |$)"
PRESET_IDS = ("horizon", "midnight", "aurora", "slate", "carbon", "pearl", "ocean", "ember", "nebula", "classic")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def internal_disk(workdir: Path) -> Path:
    """A 256 MiB stand-in for the computer's internal disk, with recognisable content."""
    disk = workdir / "internal-disk.img"
    block = hashlib.sha256(b"boswas-internal-disk-sentinel").digest() * 32768      # 1 MiB
    with disk.open("wb") as fh:
        for _ in range(16):
            fh.write(block)
        fh.truncate(256 * 1024 * 1024)
    return disk


def usb_vm(name: str, iso: Path, workdir: Path, outdir: Path, disk: Path) -> "VM":
    """UEFI with Secure Boot enforced; the ISO written to a USB stick; an internal NVMe disk."""
    vars_copy = workdir / f"{name}-vars.fd"
    shutil.copyfile(OVMF_DIR / "OVMF_VARS_4M.ms.fd", vars_copy)
    return VM(name, workdir, outdir, [
        "-machine", "q35,smm=on",
        "-global", "driver=cfi.pflash01,property=secure,value=on",
        "-drive", f"if=pflash,format=raw,unit=0,readonly=on,file={OVMF_DIR / 'OVMF_CODE_4M.secboot.fd'}",
        "-drive", f"if=pflash,format=raw,unit=1,file={vars_copy}",
        "-device", "qemu-xhci,id=xhci",
        "-drive", f"if=none,id=stick,format=raw,readonly=on,file={iso}",
        "-device", "usb-storage,bus=xhci.0,drive=stick,removable=on,bootindex=0",
        "-drive", f"if=none,id=internal,format=raw,file={disk}",
        "-device", "nvme,serial=BOSWAS-INTERNAL,drive=internal,bootindex=1",
    ])


def wait_boot_menu(vm: "VM", start: float, limit: float, label: str, outdir: Path, workdir: Path) -> Path | None:
    # Poll quickly: the menu counts down 10 s, and the test must press a key in time.
    while time.time() - start < limit and vm.alive():
        time.sleep(2)
        shot = vm.screendump(f"t{int(time.time() - start):04d}")
        if boswas_screen(analyse(shot)):
            shutil.copyfile(shot, workdir / f"{label}-menu.ppm")
            to_png(shot, outdir / f"{label}-boot-menu.png")
            return workdir / f"{label}-menu.ppm"
    return None


def session_env(sh: "Shell") -> str:
    """The environment of the live user's Plasma session, to start applications in it."""
    rc, env = sh.run("p=$(pgrep -u boswas -x plasmashell | head -1); [ -n \"$p\" ] && "
                     "tr '\\0' '\\n' < /proc/$p/environ | grep -E '^(WAYLAND_DISPLAY|DISPLAY|XAUTHORITY|"
                     "XDG_RUNTIME_DIR|DBUS_SESSION_BUS_ADDRESS|XDG_SESSION_TYPE|XDG_CURRENT_DESKTOP)=' | tr '\\n' ' '")
    return env.strip()


def in_session(cmd: str, background: bool = False) -> str:
    """A shell snippet that runs CMD (plain words, no quotes) with the live user's
    complete Plasma session environment, as if started from the desktop
    (XDG_CONFIG_DIRS brings the Boswas defaults: Konsole profile, icons, colours)."""
    run = f"xargs -0 -a /proc/$p/environ sh -c 'exec env -i \"$@\" {cmd}' _"
    if background:
        run = f"( {run} >/dev/null 2>&1 & )"
    return f"p=$(pgrep -u boswas -x plasmashell | head -1); {run}"


def installer_banner(path: Path) -> bool:
    """The Debian Installer screen with the Boswas OS banner: a navy band above the gold rule."""
    w, h, _ = read_ppm(path)
    for y in range(20, min(h, 200)):
        if analyse(path, (0, y, w, y + 1))["gold"] >= 0.4:
            band = analyse(path, (0, max(0, y - 55), w, max(1, y - 8)))
            if band["navy"] >= 0.4:
                return True
    return False


def scenario_usb(iso: Path, workdir: Path, outdir: Path, timeout: float) -> None:
    """Release blocker: the ISO on a USB stick boots (UEFI, Secure Boot) straight into
    the usable Boswas OS live desktop; nothing starts an installer or touches the
    internal disk; the installer runs only when the user chooses it."""
    if not ((OVMF_DIR / "OVMF_CODE_4M.secboot.fd").exists() and (OVMF_DIR / "OVMF_VARS_4M.ms.fd").exists()):
        record("SKIP", "Live USB boot (OVMF Secure Boot firmware not installed)")
        return
    disk = internal_disk(workdir)
    disk_before = sha256_file(disk)
    vm = usb_vm("usb-live", iso, workdir, outdir, disk)
    try:
        start = time.time()
        menu = wait_boot_menu(vm, start, min(timeout, 600), "usb-live", outdir, workdir)
        if not check(menu is not None, "Live USB: a USB stick boots under UEFI with Secure Boot to the Boswas boot menu"):
            return
        # The default entry (Live session) gets a serial console for the test;
        # everything else on its command line, including the splash, stays as shipped.
        # GRUB's editor shows "setparams '<title>'", an empty line, then the
        # entry: two lines down is the linux line.
        vm.key("e")
        time.sleep(3)
        vm.key("down")
        vm.key("down")
        vm.key("end")
        # (Plymouth shows only text when a serial console is configured, unless told
        # to ignore it; without the test's console the splash is shown anyway.)
        vm.type_text(" console=ttyS0,115200n8 console=tty0 plymouth.ignore-serial-consoles")
        time.sleep(1)
        to_png(vm.screendump("boot-edit"), outdir / "usb-live-boot-edit.png")
        vm.key("ctrl-x")
        splash = None
        found = None
        while time.time() - start < timeout and vm.alive():
            found = vm.expect(rb"(login: )", 15)
            if found:
                break
            shot = vm.screendump(f"t{int(time.time() - start):04d}")
            if splash is None and difference(shot, menu) > 12 and boswas_screen(analyse(shot)) and \
                    not desktop_screen(shot):
                splash = shot
                to_png(shot, outdir / "usb-live-splash.png")
        check(splash is not None, "Live USB: the Boswas OS boot splash is shown (screenshot usb-live-splash.png)")
        if not check(found is not None, f"Live USB: the live system boots ({int(time.time() - start)} s)"):
            return
        vm.send("boswas\n")
        if vm.expect(rb"Password: ", 60):
            vm.send("live\n")
        if not check(vm.expect(rb"\$ ", 120) is not None, "Live USB: the live user is signed in"):
            return
        vm.send("export TERM=dumb PAGER=cat SYSTEMD_PAGER= SYSTEMD_COLORS=0 PS1='$ '; stty -echo cols 250; "
                "sudo -n dmesg -n 1\n")
        vm.pump(3)
        sh = Shell(vm)

        rc, out = sh.run("cat /proc/cmdline")
        check("boot=live" in out and "splash" in out and "/install" not in out,
              "Live USB: the default boot entry starts the live session (not the installer)")
        rc, out = sh.run("/usr/sbin/plymouth-set-default-theme; lsinitramfs "
                         "/run/live/medium/live/initrd.img* 2>/dev/null | grep -c 'plymouth/themes/boswas/'")
        check(out.split()[:1] == ["boswas"] and out.split()[-1:] != ["0"],
              "boot splash: the Boswas OS Plymouth theme is the default and is in the live initramfs")

        # Identity
        rc, out = sh.run(". /etc/os-release; echo \"$NAME|$ID|$ID_LIKE|$PRETTY_NAME|$VERSION_CODENAME\"")
        fields = out.strip().split("|")
        check(fields[:3] == ["Boswas OS", "boswas", "debian"] and fields[3].startswith("Boswas OS"),
              f"identity: os-release names Boswas OS (ID=boswas, ID_LIKE=debian) ({out.strip()})")
        rc, out = sh.run("cat /etc/issue /etc/issue.net /etc/motd; hostnamectl | grep -i 'operating system'")
        check("Boswas OS" in out and "debian" not in out.lower(),
              "identity: console banners, message of the day and hostnamectl say Boswas OS (no Debian)")
        rc, out = sh.run("getent passwd boswas | cut -d: -f5; cat /run/boswas/session; "
                         "grep '^Variant=' /usr/share/boswas/kde-settings/kcm-about-distrorc; "
                         "grep -c 'Exec=boswas-control-center --page install' "
                         "/usr/local/share/applications/com.boswas.InstallBoswasOS.desktop")
        lines = out.splitlines()
        check(bool(lines) and lines[0].startswith("Boswas OS Live"), f"live session: user shown as Boswas OS Live ({lines[:1]})")
        check("live" in lines and "Variant=Live session" in lines,
              "live session: marked as Live session (Control Center, About this System, terminal)")
        check(lines[-1:] == ["1"], "live session: \"Install Boswas OS\" is offered in the application menu (optional)")

        # The desktop, directly
        plasma = False
        deadline = time.time() + min(900, timeout)
        while time.time() < deadline:
            rc, out = sh.run("pgrep -u boswas -x plasmashell >/dev/null && echo running")
            if "running" in out:
                plasma = True
                break
            vm.pump(20)
        if not check(plasma, "Live USB: the Plasma desktop starts for the live user (no sign-in needed)"):
            return
        vm.pump(60)
        vm.nudge()
        shot = vm.screendump("desktop")
        to_png(shot, outdir / "usb-live-desktop.png")
        check(desktop_screen(shot), "Live USB: the Boswas OS desktop is on screen (screenshot usb-live-desktop.png)")
        rc, out = sh.run(f"pgrep -a -i -f '{INSTALLER_PROCS}' || echo none")
        check(out.strip() == "none", f"Live USB: no installer, installation wizard or browser started on its own ({out.strip()[:120]})")
        rc, out = sh.run("grep -l -i -E '^Exec=.*(calamares|debian-installer|ubiquity|install)' "
                         "/etc/xdg/autostart/*.desktop ~/.config/autostart/*.desktop 2>/dev/null || echo none")
        check(out.strip() == "none", "Live USB: nothing installer-related is set to start automatically")
        rc, out = sh.run("findmnt -rno SOURCE | grep -c nvme0n1; lsblk -dno NAME /dev/nvme0n1")
        check(out.split()[:1] == ["0"] and "nvme0n1" in out,
              "Live USB: the internal disk is present but not mounted by the live session")

        # Using the system: applications, settings, terminal, Control Center
        env = session_env(sh)
        if not check("DBUS_SESSION_BUS_ADDRESS=" in env, "Live USB: the Plasma session environment is reachable"):
            return

        def start_app(argv: str, pattern: str, label: str, description: str, extra: str = "") -> None:
            """Start an application as the desktop would (full session environment)
            and check it is still running after 45 s; EXTRA is a further shell
            check that must print "ok"."""
            vm.nudge()
            sh.run(in_session(argv, background=True))
            vm.pump(45)
            vm.nudge()
            to_png(vm.screendump(label), outdir / f"usb-live-{label}.png")
            rc, out = sh.run(f"pgrep -u boswas -f '{pattern}' >/dev/null && echo running" +
                             (f"; {extra}" if extra else ""))
            check("running" in out and (not extra or "ok" in out.split()),
                  f"{description} (screenshot usb-live-{label}.png)")
            sh.run(f"pkill -u boswas -f '{pattern}'")
            vm.pump(5)

        start_app("konsole", "^konsole", "terminal", "Live USB: the terminal opens with the Boswas profile (welcome and prompt)",
                  extra="pgrep -u boswas -f 'rcfile /usr/share/boswas/terminal/bashrc' >/dev/null && echo ok")
        start_app("boswas-control-center --page about", "boswas-control-center", "control-center",
                  "Live USB: Boswas Control Center opens (About Boswas OS)")
        start_app("boswas-control-center --page security", "boswas-control-center", "security-center",
                  "Live USB: Boswas Security Center opens")
        start_app("systemsettings", "^systemsettings", "system-settings", "Live USB: system settings open")
        start_app("kcalc", "^kcalc", "application", "Live USB: applications start (calculator)")
        start_app("boswas-compat-manager", "boswas-compat-manager", "compat-manager",
                  "Live USB: the Boswas Compatibility Manager opens")
        start_app("boswas-control-center --page install", "boswas-control-center", "install-page",
                  "Live USB: the installer is reached by explicit choice (Install Boswas OS page)")
        rc, out = sh.run(f"pgrep -a -i -f '{INSTALLER_PROCS}' || echo none")
        check(out.strip() == "none", "Live USB: opening the Install page starts nothing by itself")

        # The ten presets in the running session: each applies (exit 0, recorded),
        # switches the user's colour scheme to its own, and changes the screen
        # while the desktop panel (gold Boswas launcher) stays.
        rc, out = sh.run(in_session("boswas-preset --json list"))
        presets = {p.get("id"): p for p in _json(out).get("presets", [])}
        check(sorted(presets) == sorted(PRESET_IDS), f"presets: ten Boswas OS presets ({len(presets)})")
        previous = workdir / "usb-live-preset-previous.ppm"
        shutil.copyfile(shot, previous)
        # Horizon is the default (already on screen), so it goes last: then every
        # preset, Horizon included, must visibly change the screen.
        for preset in PRESET_IDS[1:] + PRESET_IDS[:1]:
            vm.nudge()
            rc, out = sh.run(in_session(f"boswas-preset apply {preset}") + " >/dev/null 2>&1; echo rc=$?; " +
                             in_session("kreadconfig6 --file kdeglobals --group General --key ColorScheme") + "; " +
                             in_session("boswas-preset current"), timeout=300)
            vm.pump(25)
            vm.nudge()
            vm.pump(3)
            shot = vm.screendump(f"preset-{preset}")
            to_png(shot, outdir / f"usb-live-preset-{preset}.png")
            expected = (presets.get(preset, {}).get("name") or "").replace(" ", "")
            words = out.split()
            w, h, _ = read_ppm(shot)
            panel = analyse(shot, (0, h - 64, w, h))
            changed = difference(shot, previous) > 2
            check("rc=0" in words and expected in words and words[-1:] == [preset] and changed and panel["gold"] > 0.001,
                  f"presets: {preset} applies in the live session (colour scheme {expected if expected in words else words[1:2]}; "
                  f"screen changed: {changed}; screenshot usb-live-preset-{preset}.png)")
            shutil.copyfile(shot, previous)
        sh.run(in_session("boswas-preset apply horizon") + " >/dev/null 2>&1", timeout=300)
        vm.pump(20)

        # Lock and login screens
        rc, sid = sh.run("loginctl show-user boswas -p Display --value")
        sid = sid.strip()
        vm.nudge()
        sh.run(f"loginctl lock-session {sid}")
        vm.pump(30)
        vm.nudge()
        vm.pump(5)
        shot = vm.screendump("lock-screen")
        to_png(shot, outdir / "usb-live-lock-screen.png")
        stats = analyse(shot)
        rc, out = sh.run("pgrep -f kscreenlocker_greet >/dev/null && echo locked")
        check("locked" in out and stats["navy"] > 0.3 and stats["black"] < 0.5,
              "lock screen: the Boswas lock screen (screenshot usb-live-lock-screen.png)")
        # The login screen, as "Switch user" opens it (terminating the autologin
        # session instead makes SDDM treat it as crashed and start no greeter).
        sh.run("qdbus6 --system org.freedesktop.DisplayManager /org/freedesktop/DisplayManager/Seat0 "
               "org.freedesktop.DisplayManager.Seat.SwitchToGreeter")
        for _ in range(6):
            vm.pump(15)
            vm.nudge()
        shot = vm.screendump("login-screen")
        to_png(shot, outdir / "usb-live-login-screen.png")
        stats = analyse(shot)
        rc, out = sh.run("pgrep -f sddm-greeter-qt6 >/dev/null && echo greeter")
        check("greeter" in out and stats["black"] < 0.5,
              "login screen: the Boswas login screen opens (Switch user; screenshot usb-live-login-screen.png)")

        vm.send("sudo -n systemctl poweroff\n")
        shutdown_splash = None
        try:
            for i in range(48):
                if vm.proc.poll() is not None:
                    break
                time.sleep(5)
                try:
                    shot = vm.screendump(f"shutdown-{i:02d}")
                    shutting_down = boswas_screen(analyse(shot)) and not desktop_screen(shot)
                except OSError:
                    break           # QEMU exited while capturing: the system has powered off
                if shutdown_splash is None and shutting_down:
                    shutdown_splash = shot
                    to_png(shot, outdir / "usb-live-shutdown.png")
            vm.proc.wait(timeout=120)
            check(vm.proc.returncode == 0, "Live USB: the live system shuts down cleanly")
        except subprocess.TimeoutExpired:
            check(False, "Live USB: the live system shuts down cleanly")
        record("PASS" if shutdown_splash is not None else "SKIP",
               "shutdown splash: Boswas OS (screenshot usb-live-shutdown.png)" if shutdown_splash is not None
               else "shutdown splash (shutdown was faster than the screenshot interval)")
    finally:
        vm.stop(timeout=30)
    check(sha256_file(disk) == disk_before,
          "Live USB: the internal disk is unchanged after the live session (SHA-256 before = after)")

    # Explicit choice: the installer entry of the same boot menu
    vm = usb_vm("usb-installer", iso, workdir, outdir, disk)
    try:
        start = time.time()
        menu = wait_boot_menu(vm, start, min(timeout, 600), "usb-installer", outdir, workdir)
        if not check(menu is not None, "installer: the boot menu offers it (second USB boot)"):
            return
        vm.key("i")                       # "Install Boswas OS" (hotkey i)
        banner = None
        while time.time() - start < min(timeout, 1500) and vm.alive():
            time.sleep(15)
            shot = vm.screendump(f"t{int(time.time() - start):04d}")
            if difference(shot, menu) > 12 and installer_banner(shot):
                banner = shot
                to_png(shot, outdir / "usb-installer.png")
                break
        check(banner is not None, "installer: starts only when \"Install Boswas OS\" is chosen, with the Boswas OS "
                                  "banner (screenshot usb-installer.png)")
    finally:
        vm.stop(timeout=10)
    check(sha256_file(disk) == disk_before, "installer: starting it (without installing) leaves the internal disk unchanged")


INSTALL_PRESEED = """# Test-only answers for an unattended installation (boot test "install").
# The Boswas policy itself (encrypted LVM, root locked, hostname) comes from
# the preseed inside the installer initrd, as on real devices.
d-i debian-installer/locale string en_US.UTF-8
d-i keyboard-configuration/xkb-keymap select us
d-i netcfg/choose_interface select auto
d-i netcfg/get_hostname string boswas-device
d-i netcfg/get_hostname seen true
d-i netcfg/get_domain string
d-i netcfg/get_domain seen true
d-i hw-detect/load_firmware boolean false
d-i passwd/user-fullname string Boswas Tester
d-i passwd/username string tester
d-i passwd/user-password password Boswas-Test-1234
d-i passwd/user-password-again password Boswas-Test-1234
d-i time/zone string UTC
d-i clock-setup/ntp boolean false
d-i partman-auto/disk string /dev/vda
d-i partman-crypto/passphrase password boswas-disk-passphrase
d-i partman-crypto/passphrase-again password boswas-disk-passphrase
d-i partman-crypto/weak_passphrase boolean true
d-i partman-auto-crypto/erase_disks boolean false
d-i partman-lvm/device_remove_lvm boolean true
d-i partman-md/device_remove_md boolean true
d-i partman-lvm/confirm boolean true
d-i partman-lvm/confirm_nooverwrite boolean true
d-i partman-partitioning/confirm_write_new_label boolean true
d-i partman/choose_partition select finish
d-i partman/confirm boolean true
d-i partman/confirm_nooverwrite boolean true
d-i grub-installer/bootdev string default
d-i finish-install/reboot_in_progress note
d-i debian-installer/exit/poweroff boolean true
# A serial console on the installed system, for the test only (Plymouth would
# fall back to text mode on a serial console without the last option).
d-i debian-installer/add-kernel-opts string console=ttyS0,115200n8 console=tty0 plymouth.ignore-serial-consoles
"""
DISK_PASSPHRASE = "boswas-disk-passphrase"


def scenario_install(iso: Path, workdir: Path, outdir: Path, timeout: float) -> None:
    """Installed OS: the real installer (chosen in the boot menu, UEFI with Secure Boot)
    installs Boswas OS with the shipped policy (encrypted LVM) onto an empty disk;
    the installed system then boots through Secure Boot, shows the Boswas OS
    passphrase screen and login screen, and identifies itself as Boswas OS."""
    import http.server
    import threading
    if not ((OVMF_DIR / "OVMF_CODE_4M.secboot.fd").exists() and shutil.which("qemu-img")):
        record("SKIP", "installed-system test (OVMF Secure Boot firmware or qemu-img missing)")
        return
    serve_dir = workdir / "preseed"
    serve_dir.mkdir()
    (serve_dir / "boswas-test.cfg").write_text(INSTALL_PRESEED)
    handler = lambda *a, **k: http.server.SimpleHTTPRequestHandler(*a, directory=str(serve_dir), **k)  # noqa: E731
    httpd = http.server.ThreadingHTTPServer(("0.0.0.0", 8088), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    disk = workdir / "installed.qcow2"
    subprocess.run(["qemu-img", "create", "-q", "-f", "qcow2", str(disk), "24G"], check=True)
    vars_copy = workdir / "install-vars.fd"
    shutil.copyfile(OVMF_DIR / "OVMF_VARS_4M.ms.fd", vars_copy)
    firmware = ["-machine", "q35,smm=on", "-global", "driver=cfi.pflash01,property=secure,value=on",
                "-drive", f"if=pflash,format=raw,unit=0,readonly=on,file={OVMF_DIR / 'OVMF_CODE_4M.secboot.fd'}",
                "-drive", f"if=pflash,format=raw,unit=1,file={vars_copy}",
                "-drive", f"file={disk},format=qcow2,if=virtio"]
    vm = VM("install", workdir, outdir, [*firmware, "-drive", f"file={iso},media=cdrom,readonly=on,if=ide"])
    try:
        start = time.time()
        menu = wait_boot_menu(vm, start, min(timeout, 600), "install", outdir, workdir)
        if not check(menu is not None, "installed system: the boot menu appears (installation boot)"):
            return
        # Third entry, "Install Boswas OS": add the unattended-installation answers
        # before the "---" that separates installer and installed-system options.
        for key in ("down", "down", "e"):
            vm.key(key)
            time.sleep(1)
        # Editor: "setparams '<title>'", an empty line, then the linux line.
        time.sleep(2)
        vm.key("down")
        vm.key("down")
        vm.key("end")
        for _ in range(len(" --- quiet")):
            vm.key("left")
        vm.type_text(" auto=true priority=critical url=http://10.0.2.2:8088/boswas-test.cfg")
        time.sleep(1)
        to_png(vm.screendump("boot-edit"), outdir / "install-boot-edit.png")
        vm.key("ctrl-x")
        log("install: unattended installation running (encrypted LVM, live image copy)")
        # Copying and encrypting the image under TCG takes hours, not minutes.
        deadline = start + max(timeout, 14400)
        last = time.time()
        while vm.alive() and time.time() < deadline:
            time.sleep(60)
            if time.time() - last > 600:
                to_png(vm.screendump(f"t{int(time.time() - start):05d}"), outdir / "install-progress.png")
                last = time.time()
        finished = not vm.alive() and vm.proc.returncode == 0
        if not check(finished, f"installed system: the installer completes and powers off "
                               f"({int(time.time() - start)} s; screenshot install-progress.png if not)"):
            return
    finally:
        vm.stop(timeout=10)
        httpd.shutdown()

    vm = VM("installed", workdir, outdir, firmware)
    try:
        start = time.time()
        passphrase = shot = None
        while time.time() - start < min(timeout, 1800) and vm.alive():
            time.sleep(10)
            shot = vm.screendump(f"t{int(time.time() - start):04d}")
            if boswas_screen(analyse(shot)) and passphrase_prompt(shot):
                passphrase = shot
                break
        if shot is not None:
            to_png(shot, outdir / "installed-passphrase.png")
        check(passphrase is not None, "installed system: boots through shim and GRUB under Secure Boot to the Boswas OS "
                                      "disk-unlock screen (screenshot installed-passphrase.png)")
        time.sleep(3)
        vm.type_text(DISK_PASSPHRASE)
        vm.key("ret")
        m = vm.expect(rb"(login: )", min(timeout, 2400))
        if not check(m is not None, f"installed system: unlocks the disk and boots ({int(time.time() - start)} s)"):
            return
        vm.pump(60)
        shot = vm.screendump("login")
        to_png(shot, outdir / "installed-login-screen.png")
        stats = analyse(shot)
        check(stats["navy"] > 0.4 and stats["black"] < 0.5,
              "installed system: the Boswas OS login screen (no automatic sign-in; screenshot installed-login-screen.png)")
        vm.send("tester\n")
        if vm.expect(rb"Password: ", 60):
            vm.send("Boswas-Test-1234\n")
        if not check(vm.expect(rb"\$ ", 120) is not None, "installed system: the administrator created at installation signs in"):
            return
        vm.send("export TERM=dumb PAGER=cat SYSTEMD_PAGER= SYSTEMD_COLORS=0 PS1='$ '; stty -echo cols 250\n")
        vm.pump(3)
        sh = Shell(vm)
        rc, out = sh.run(". /etc/os-release; echo \"$NAME|$ID|$ID_LIKE\"; cat /etc/issue; hostnamectl | grep -i 'operating system'")
        check(out.startswith("Boswas OS|boswas|debian") and "debian" not in out.split("\n", 1)[-1].lower(),
              "installed system: identifies as Boswas OS (os-release, console banner, hostnamectl)")
        rc, out = sh.run("echo Boswas-Test-1234 | sudo -S -p '' sh -c 'mokutil --sb-state; efibootmgr -v; "
                         "cryptsetup status $(ls /dev/mapper | grep -m1 _crypt) | grep -i type; aa-enabled; "
                         "plymouth-set-default-theme; test -e /usr/local/share/applications/com.boswas.InstallBoswasOS.desktop "
                         "&& echo install-entry-present; dpkg -s live-boot 2>/dev/null | "
                         "grep -qx \"Status: install ok installed\" && echo live-boot-installed; echo'",
                         timeout=300)
        (outdir / "installed-system.txt").write_text(out + "\n")
        check("SecureBoot enabled" in out, "installed system: Secure Boot is enforced")
        check("\\EFI\\debian\\shimx64.efi" in out or "\\EFI\\DEBIAN\\SHIMX64.EFI" in out.upper(),
              "installed system: the firmware boots Debian's signed shim from \\EFI\\debian (Secure Boot path kept)")
        check("LUKS2" in out, "installed system: disk encryption is LUKS2 (installation policy)")
        check("Yes" in out, "installed system: AppArmor is enabled")
        check("boswas" in out.split(), "installed system: the Boswas OS boot splash is configured")
        check("install-entry-present" not in out and "live-boot-installed" not in out,
              "installed system: no live-session leftovers (no Install entry, live-boot removed)")
        vm.send("echo Boswas-Test-1234 | sudo -S -p '' systemctl poweroff\n")
        try:
            vm.proc.wait(timeout=300)
            check(True, "installed system: shuts down cleanly")
        except subprocess.TimeoutExpired:
            check(False, "installed system: shuts down cleanly")
    finally:
        vm.stop(timeout=10)


WINAPP = "com.boswas.testapp"
WINAPP_PORT = 47011


def probe_results(output: str) -> dict:
    """{'read Z:/x': 'BLOCKED', ...} from the Windows test application's output."""
    found = {}
    for line in output.splitlines():
        parts = line.split()
        if len(parts) >= 4 and parts[0] == "PROBE":
            found[f"{parts[1]} {parts[2]}"] = parts[3]
    return found


def winapp_checks(sh: "Shell", outdir: Path, fixtures: bool, timeout: float) -> None:
    """WinCompat on the running system: confinement by the real kernel's AppArmor."""
    rc, out = sh.run("boswas-winapp --version")
    check(rc == 0 and out.strip().startswith("boswas-winapp "), f"boswas-winapp runs ({out.strip()})")
    rc, out = sh.run("xdg-mime query default application/x-msdownload")
    check("boswas-winapp-install.desktop" in out, "Windows executables open with \"Run with Boswas\"")
    # Debian does not load AppArmor profiles on live media; boswas-compat's
    # postinst loaded it during the image build only into the build kernel.
    # Load it like apparmor.service does on an installed system.
    rc, out = sh.run("sudo -n apparmor_parser -r -W /etc/apparmor.d/boswas-winapp && "
                     "sudo -n grep '^boswas-winapp ' /sys/kernel/security/apparmor/profiles")
    if not check(rc == 0 and "boswas-winapp (enforce)" in out,
                 "AppArmor profile boswas-winapp loads into the running kernel in enforce mode"):
        return
    if not fixtures:
        record("SKIP", "WinCompat install/launch in the VM (no --fixtures-iso)")
        return
    rc, _ = sh.run("sudo -n mount -o ro /dev/sr1 /mnt && sudo -n install -m 0644 /mnt/manifests/*.json "
                   "/etc/boswas/compat/manifests/")
    if not check(rc == 0, "WinCompat fixtures available (second CD) and manifests installed"):
        return
    rc, out = sh.run(f"sudo -n boswas-winapp install /mnt/boswas-testapp.exe --id {WINAPP}; echo rc=$?")
    check("rc=4" in out and "root" in out, "boswas-winapp refuses to install Windows software as root")

    # Negative: without the enforcing profile no Windows code runs at all.
    rc, out = sh.run(f"sudo -n apparmor_parser -R /etc/apparmor.d/boswas-winapp; "
                     f"boswas-winapp install /mnt/boswas-testapp.exe --id {WINAPP}; echo rc=$?; "
                     f"boswas-winapp remove {WINAPP} >/dev/null; "
                     f"sudo -n apparmor_parser -r -W /etc/apparmor.d/boswas-winapp", timeout=600)
    check("rc=5" in out and "not enforcing" in out,
          "without the AppArmor profile Windows code is refused (REQUIRE_APPARMOR)")

    start = time.time()
    rc, out = sh.run(f"boswas-winapp install /mnt/boswas-testapp.exe --id {WINAPP}; echo rc=$?", timeout=timeout)
    (outdir / "winapp-install.txt").write_text(out + "\n")
    if not check("rc=0" in out, f"boswas-winapp installs the Windows test application ({int(time.time() - start)} s)"):
        rc, install_log = sh.run(f"boswas-winapp logs {WINAPP} --install --lines 40")
        (outdir / "winapp-install.log").write_text(install_log + "\n")
        return
    rc, out = sh.run(f"stat -c '%U %a' ~/.local/share/boswas/wine/{WINAPP} && "
                     f"test -f ~/.local/share/boswas/wine/{WINAPP}/sandbox/prefix/system.reg && echo prefix-ok")
    check("boswas 700" in out and "prefix-ok" in out, "isolated per-application prefix owned by the user (0700)")
    rc, out = sh.run("boswas-winapp --json list")
    check(f'"id": "{WINAPP}"' in out, "boswas-winapp list shows the application")

    sh.run(f"python3 -c 'import socket;s=socket.socket();s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);"
           f"s.bind((\"127.0.0.1\",{WINAPP_PORT}));s.listen(9)\nwhile 1: s.accept()[0].close()' "
           f">/dev/null 2>&1 & sleep 1")
    rc, out = sh.run(f"boswas-winapp launch {WINAPP} -- read Z:/home/boswas/.bashrc write Z:/home/boswas/pwned.txt "
                     f"read Z:/etc/boswas/device.conf connect 127.0.0.1 {WINAPP_PORT} "
                     f"write C:/users/boswas/AppData/Roaming/probe.txt; echo rc=$?", timeout=timeout)
    (outdir / "winapp-launch.txt").write_text(out + "\n")
    check("BOSWAS-TESTAPP OK" in out and "rc=0" in out, "boswas-winapp launch runs the Windows application")
    probes = probe_results(out)
    for key, expected, text in (
            ("read Z:/home/boswas/.bashrc", "BLOCKED", "the user's home is not reachable"),
            ("write Z:/home/boswas/pwned.txt", "BLOCKED", "cannot write into the user's home"),
            ("read Z:/etc/boswas/device.conf", "BLOCKED", "AppArmor denies files outside the profile"),
            (f"connect 127.0.0.1:{WINAPP_PORT}", "BLOCKED", "no network unless the manifest grants it"),
            ("write C:/users/boswas/AppData/Roaming/probe.txt", "ALLOWED", "positive control: own prefix writable")):
        check(probes.get(key) == expected, f"WinCompat isolation: {text} ({key}: {probes.get(key)})")
    rc, out = sh.run(f"boswas-winapp --json status {WINAPP}")
    check('"confinement": "boswas-winapp (enforce)"' in out,
          "the Windows application ran confined by AppArmor (boswas-winapp, enforce)")
    rc, out = sh.run("sudo -n grep -h 'apparmor=\"DENIED\"' /var/log/audit/audit.log | grep -c 'profile=\"boswas-winapp\"'")
    sh_out = sh.run("sudo -n grep -h 'apparmor=\"DENIED\"' /var/log/audit/audit.log | grep 'profile=\"boswas-winapp\"' "
                    "| tail -50")[1]
    (outdir / "winapp-apparmor-denials.txt").write_text(sh_out + "\n")
    log(f"boswas-winapp AppArmor denials recorded: {out.strip()} (build/logs/boot-test/winapp-apparmor-denials.txt)")
    rc, out = sh.run(f"boswas-winapp remove {WINAPP} && test ! -e ~/.local/share/boswas/wine/{WINAPP} && echo removed")
    check("removed" in out, "boswas-winapp remove deletes the application and its prefix")


def winapp_gui_check(vm: "VM", sh: "Shell", outdir: Path, timeout: float) -> None:
    """A Windows GUI program on the live Plasma session's (Xwayland) display, confined."""
    rc, env = sh.run("p=$(pgrep -u boswas -x plasmashell | head -1); [ -n \"$p\" ] && "
                     "tr '\\0' '\\n' < /proc/$p/environ | grep -E '^(DISPLAY|XAUTHORITY)=' | tr '\\n' ' '")
    if "DISPLAY=" not in env:
        record("SKIP", "WinCompat GUI check (no X display in the Plasma session)")
        return
    rc, out = sh.run("boswas-winapp install /mnt/boswas-testapp-unlisted.exe --id local.gui-test -- /S; echo rc=$?",
                     timeout=timeout)
    if not check("rc=0" in out, "an unlisted Windows application installs (policy: no network; display and GPU)"):
        return
    # Background it in a subshell: Shell.run appends "; echo <marker>", and "&;"
    # would be a syntax error.
    sh.run(f"( env {env.strip()} boswas-winapp launch local.gui-test --quiet --timeout 90 "
           f"--exe 'C:\\windows\\notepad.exe' >/dev/null 2>&1 & )")
    vm.pump(70)
    to_png(vm.screendump("winapp-gui"), outdir / "winapp-gui.png")
    vm.pump(45)
    rc, out = sh.run("boswas-winapp --json status local.gui-test")
    # Without a working display Notepad exits at once; running until the
    # 90 s timeout means its window loop ran.
    check('"confinement": "boswas-winapp (enforce)"' in out and '"timed_out": true' in out,
          "a Windows GUI program (Notepad) runs on the session's display, confined (screenshot winapp-gui.png)")
    sh.run("boswas-winapp remove local.gui-test")


MESSAGE_32 = "This application requires 32-bit Windows compatibility, which is not supported by Boswas OS."
CP_PORT = 18443


def _json(out: str) -> dict:
    """The JSON document in a command's serial output (ignores echoed noise)."""
    start, end = out.find("{"), out.rfind("}")
    try:
        return json.loads(out[start:end + 1]) if start >= 0 else {}
    except ValueError:
        return {}


def agent_checks(sh: "Shell") -> None:
    """The device agent on the live system (no Control Plane yet)."""
    rc, out = sh.run("systemctl is-active boswas-device-agent.service")
    check(out.strip() == "active", "boswas-device-agent.service is active (started at boot)")
    status = {}
    for _ in range(30):
        status = _json(sh.run("boswas-device --json status")[1]).get("agent", {})
        if status.get("state") in ("READY", "DEGRADED"):
            break
        time.sleep(5)
    check(status.get("state") in ("READY", "DEGRADED") and status.get("connection") == "STANDALONE",
          f"device agent: state {status.get('state')}, connection {status.get('connection')} (standalone device)")
    ident = _json(sh.run("boswas-device --json identity")[1]).get("identity", {})
    check(bool(ident.get("device_id")) and ident.get("ephemeral") is True,
          "device identity created at first start, marked ephemeral in the live session")
    rc, out = sh.run("boswas-device --json inventory")
    inventory = _json(out).get("inventory", {})
    check(inventory.get("compatibility", {}).get("architectures") == ["x86_64"] and "boswas" not in
          json.dumps(inventory.get("windows_applications", [])),
          "local inventory: x86_64-only Windows runtime, no user names")


def device_management_checks(vm: "VM", sh: "Shell", outdir: Path, workdir: Path, fixtures: Path | None,
                             timeout: float) -> None:
    """Session agent, Compatibility Manager and the Control Plane with the real kernel's AppArmor."""
    rc, out = sh.run("systemctl --user is-active boswas-session-agent.service")
    check(out.strip() == "active", "the user's session agent runs in the Plasma session (systemd user unit)")
    connected = False
    for _ in range(12):
        if _json(sh.run("boswas-device --json status")[1]).get("agent", {}).get("sessions", {}).get("connected"):
            connected = True
            break
        time.sleep(5)
    check(connected, "the session agent is registered with the device agent")

    rc, env = sh.run("p=$(pgrep -u boswas -x plasmashell | head -1); [ -n \"$p\" ] && "
                     "tr '\\0' '\\n' < /proc/$p/environ | grep -E '^(DISPLAY|XAUTHORITY|WAYLAND_DISPLAY|"
                     "XDG_RUNTIME_DIR)=' | tr '\\n' ' '")
    sh.run(f"( env {env.strip()} boswas-compat-manager >/tmp/compat-manager.log 2>&1 & )")
    vm.pump(60)
    to_png(vm.screendump("compat-manager"), outdir / "compat-manager.png")
    rc, out = sh.run("pgrep -u boswas -f boswas-compat-manager >/dev/null && echo running; "
                     "tail -5 /tmp/compat-manager.log")
    check("running" in out, "the Compatibility Manager starts on the Plasma session (screenshot compat-manager.png)")
    sh.run("pkill -u boswas -f boswas-compat-manager")

    if fixtures is None:
        record("SKIP", "Control Plane round trip in the VM (no --fixtures-iso)")
        return
    rc, out = sh.run("boswas-winapp install /mnt/boswas-testapp-x86.exe; echo rc=$?", timeout=600)
    check("rc=4" in out and MESSAGE_32 in out, "a 32-bit Windows installer is refused with the product message")

    # A Control Plane in the builder container, reachable from the VM at 10.0.2.2.
    repo = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(repo / "tests/device"))
    from cp_harness import ControlPlane
    cp = ControlPlane(python_path=[repo / "control-plane", repo / "packages/boswas-device-agent",
                                   repo / "packages/boswas-compat"], data_dir=workdir / "cp", port=CP_PORT,
                      listen="0.0.0.0", public_url=f"https://10.0.2.2:{CP_PORT}", log=outdir / "control-plane.log")
    try:
        cp.init(["10.0.2.2", "127.0.0.1"])
        cp.start()
    except RuntimeError as exc:
        check(False, f"Control Plane starts for the VM ({exc})")
        return
    try:
        fixture_dir = fixtures.parent / "fixtures"
        status, art = cp.request("POST", "/api/v1/artifacts", raw=(fixture_dir / "boswas-testapp.exe").read_bytes(),
                                 name="boswas-testapp.exe")
        manifest = json.loads((fixture_dir / "manifests" / f"{WINAPP}.json").read_text())
        status2, _ = cp.request("POST", "/api/v1/applications", {"manifest": manifest})
        check(status == 201 and status2 == 201, "Control Plane catalog prepared (64-bit test application)")
        token = cp.enrollment_token("--allow-ephemeral")
        ca = base64.b64encode(cp.ca_pem.encode()).decode()
        sh.run(f"echo {ca} | base64 -d | sudo -n tee /etc/boswas/control-plane-ca.pem >/dev/null")
        sh.run(f"sudo -n sed -i 's|^CONTROL_PLANE_URL=\"\"|CONTROL_PLANE_URL=\"https://10.0.2.2:{CP_PORT}\"|; "
               "s|^CONTROL_PLANE_CA=\"\"|CONTROL_PLANE_CA=\"/etc/boswas/control-plane-ca.pem\"|; "
               "s|^HEARTBEAT_INTERVAL=\"300\"|HEARTBEAT_INTERVAL=\"30\"|' /etc/boswas/device.conf")
        rc, out = sh.run(f"echo {token} | sudo -n boswas-device --json enroll", timeout=300)
        enrolled = _json(out)
        check(enrolled.get("enrolled") is True, "the VM enrolls with the Control Plane (token on stdin, mutual TLS)")
        device_id = enrolled.get("device_id")

        def agent() -> dict:
            return _json(sh.run("boswas-device --json status")[1]).get("agent", {})

        def wait_agent(predicate, seconds: float) -> dict:
            deadline, current = time.time() + seconds, {}
            while time.time() < deadline:
                current = agent()
                if predicate(current):
                    break
                sh.run("sudo -n boswas-device sync >/dev/null 2>&1")
                time.sleep(10)
            return current

        st = wait_agent(lambda s: s.get("connection") == "CONNECTED" and (s.get("policy") or {}).get("version"), 600)
        check(st.get("connection") == "CONNECTED", "heartbeats reach the Control Plane (connection CONNECTED)")
        check((st.get("policy") or {}).get("version") == "default-1", "the signed default policy is verified and applied")
        status, dev = cp.request("GET", f"/api/v1/devices/{device_id}")
        check(status == 200 and dev.get("connection") == "online" and dev.get("ephemeral") is True,
              "the Control Plane shows the VM online (ephemeral live device)")

        def run_command(body: dict, seconds: float) -> dict:
            status, cmd = cp.command(device_id, body)
            if status not in (200, 201):
                return {"status": f"HTTP {status}", "error": cmd.get("error")}
            deadline, result = time.time() + seconds, {}
            while time.time() < deadline:
                result = cp.command_status(device_id, cmd["command_id"])
                if result.get("status") in ("SUCCEEDED", "FAILED", "EXPIRED", "CANCELLED"):
                    break
                sh.run("sudo -n boswas-device sync >/dev/null 2>&1")
                time.sleep(15)
            return result

        started = time.time()
        result = run_command({"type": "INSTALL_APPLICATION", "application_id": WINAPP}, timeout)
        check(result.get("status") == "SUCCEEDED",
              f"remote INSTALL_APPLICATION under AppArmor: downloaded over mutual TLS, installed for the live user "
              f"({int(time.time() - started)} s; {result.get('status')}"
              f"{' ' + str(result['error']) if result.get('error') else ''})")
        result = run_command({"type": "LAUNCH_APPLICATION", "application_id": WINAPP}, 900)
        check(result.get("status") == "SUCCEEDED", f"remote LAUNCH_APPLICATION ({result.get('status')})")
        confinement = None
        for _ in range(20):
            confinement = (_json(sh.run(f"boswas-winapp --json status {WINAPP}")[1]).get("last_launch") or {}) \
                .get("confinement")
            if confinement:
                break
            time.sleep(10)
        check(confinement == "boswas-winapp (enforce)",
              f"the remotely launched application ran confined (boswas-winapp, enforce; got {confinement})")
        status, body = cp.command(device_id, {"type": "EXECUTE_SHELL_COMMAND", "command": "id"})
        check(status == 400, "the Control Plane refuses untyped (shell) commands")

        cp.stop()
        st = wait_agent(lambda s: s.get("connection") == "OFFLINE", 600)
        check(st.get("connection") == "OFFLINE" and st.get("state") == "OFFLINE",
              "with the Control Plane gone the agent is OFFLINE")
        rc, out = sh.run(f"boswas-winapp launch {WINAPP} --quiet; echo rc=$?", timeout=900)
        check("rc=0" in out, "Windows applications still launch, confined, while the Control Plane is unreachable")
        sh.run(f"boswas-winapp remove {WINAPP} >/dev/null 2>&1")
    finally:
        cp.stop()


def scenario_serial(iso: Path, workdir: Path, outdir: Path, timeout: float, fixtures: Path | None = None) -> None:
    kernel = iso_extract(iso, workdir, "vmlinuz*")
    initrd = iso_extract(iso, workdir, "initrd.img*")
    if not (kernel and initrd):
        check(False, "serial boot: live kernel and initrd found on the ISO")
        return
    append = "boot=live components quiet hostname=boswas-device username=boswas console=tty0 console=ttyS0,115200n8"
    extra = ["-drive", f"file={fixtures},media=cdrom,readonly=on,if=ide"] if fixtures else []
    vm = VM("serial", workdir, outdir, [
        "-machine", "q35",
        "-drive", f"file={iso},media=cdrom,readonly=on,if=ide",
        *extra,
        "-kernel", str(kernel), "-initrd", str(initrd), "-append", append,
    ])
    try:
        start = time.time()
        m = vm.expect(rb"(login: |\$ $|\$ )", timeout)
        if not check(m is not None, f"serial boot: live system reached a login/shell on ttyS0 ({int(time.time() - start)} s)"):
            return
        if m.group(1) == b"login: ":
            vm.send("boswas\n")
            if vm.expect(rb"Password: ", 60):
                vm.send("live\n")
            if not check(vm.expect(rb"\$ ", 120) is not None, "serial boot: live user can log in"):
                return
        # Quiet console: keep kernel messages (audit, firewall log) from
        # interleaving with command output on ttyS0.
        vm.send("export TERM=dumb PAGER=cat SYSTEMD_PAGER= SYSTEMD_COLORS=0 PS1='$ '; stty -echo cols 250; "
                "sudo -n dmesg -n 1\n")
        vm.pump(3)
        sh = Shell(vm)

        rc, out = sh.run("boswas-info")
        check(rc == 0 and "Boswas OS" in out and "v1 Alpha" in out, "boswas-info runs and identifies Boswas OS v1 Alpha")
        check("trixie" in out and "Debian 13" in out, "boswas-info reports the package base in its technical details (Debian 13, trixie)")
        (outdir / "boswas-info.txt").write_text(out + "\n")

        rc, out = sh.run("boswas --json status")
        (outdir / "boswas-status.json").write_text(out + "\n")
        try:
            status = {c["id"]: c for c in json.loads(out)["checks"]}
        except (ValueError, KeyError):
            status = {}
        check(bool(status), "boswas --json status returns valid JSON")
        for cid in ("firewall", "audit", "ssh-server"):
            st = status.get(cid, {}).get("status")
            check(st == "PASS", f"boswas status: {cid} is PASS on the running system (got {st})")
        # Debian does not load AppArmor profiles on live media; the CLI must say so
        # (WARN) instead of claiming PASS from the kernel flag alone.
        aa = status.get("apparmor", {})
        check(aa.get("status") == "WARN" and "live session" in aa.get("detail", ""),
              f"boswas status: apparmor reports live-media state honestly (got {aa.get('status')})")
        trust = status.get("apt-trust", {})
        check(trust.get("status") in ("PASS", "INFO"), f"boswas status: repository trust (got {trust.get('status')})")

        for unit in ("nftables", "auditd", "NetworkManager"):
            rc, out = sh.run(f"systemctl is-active {unit}")
            check(out.strip() == "active", f"service {unit} is active")
        rc, out = sh.run("sudo -n nft list table inet boswas_filter")
        check("policy drop" in out, "Boswas firewall ruleset is loaded (inbound policy drop)")
        rc, out = sh.run("sudo -n auditctl -l")
        check("boswas-identity" in out, "Boswas audit rules are loaded into the kernel")
        rc, out = sh.run("cat /sys/module/apparmor/parameters/enabled /sys/kernel/security/lsm")
        check(out.split()[:1] == ["Y"] and "apparmor" in out, "AppArmor is enabled in the running kernel (active LSM)")
        rc, out = sh.run("systemctl is-enabled apparmor.service; "
                         "systemctl show -p ConditionResult apparmor.service; "
                         "systemctl status apparmor.service --no-pager 2>&1 | grep -o 'ConditionPathExists=[^ ]*'")
        check("enabled" in out and "ConditionPathExists=!/run/live/overlay/work" in out,
              "apparmor.service enabled; skipped only by Debian's live-media condition (profiles load once installed)")
        rc, out = sh.run("cat /proc/sys/kernel/kptr_restrict /proc/sys/kernel/kexec_load_disabled "
                         "/proc/sys/kernel/sysrq /proc/sys/kernel/unprivileged_userns_clone")
        check(out.split() == ["2", "1", "176", "1"],
              f"Boswas sysctl hardening applied, user namespaces still available ({' '.join(out.split())})")

        rc, out = sh.run("wine --version 2>/dev/null")
        version = next((line for line in out.splitlines() if line.startswith("wine-")), "")
        check(version.startswith("wine-10."), f"Wine runs ({version or 'no version output'})")
        # plasmashell cannot run outside a graphical session (even --version), so
        # check the installed version; the running session is verified below.
        rc, out = sh.run("dpkg-query -W -f='${Version}\\n' plasma-workspace")
        check(out.strip().split(":")[-1].startswith("6."), f"KDE Plasma 6 installed (plasma-workspace {out.strip()})")

        rc, out = sh.run("ip -4 -o addr show scope global")
        check("10.0.2.15" in out, "network: DHCP address obtained (NetworkManager)")
        rc, out = sh.run("getent hosts deb.debian.org >/dev/null && echo resolved")
        record("PASS" if "resolved" in out else "SKIP",
               "network: DNS resolution works" if "resolved" in out else "network: DNS resolution (no upstream network)")

        winapp_checks(sh, outdir, fixtures is not None, timeout)
        agent_checks(sh)

        # The graphical session (SDDM autologin -> Plasma) under emulation may
        # take a while; give it time, then capture the screen.
        plasma = False
        deadline = time.time() + min(900, timeout)
        while time.time() < deadline:
            rc, out = sh.run("pgrep -u boswas -x plasmashell >/dev/null && echo running")
            if "running" in out:
                plasma = True
                break
            vm.pump(20)
        check(plasma, "KDE Plasma session started for the live user (SDDM autologin)")
        if plasma:
            vm.pump(60)
            to_png(vm.screendump("desktop"), outdir / "serial-desktop.png")
            if fixtures is not None:
                winapp_gui_check(vm, sh, outdir, timeout)
            device_management_checks(vm, sh, outdir, workdir, fixtures, timeout)
        else:
            record("SKIP", "session agent, Compatibility Manager and Control Plane checks (no Plasma session)")
        rc, out = sh.run("cat /proc/cmdline; uname -r")
        (outdir / "kernel.txt").write_text(out + "\n")

        vm.send("sudo -n systemctl poweroff\n")
        try:
            vm.proc.wait(timeout=240)
            check(True, "live system shuts down cleanly")
        except subprocess.TimeoutExpired:
            check(False, "live system shuts down cleanly")
    finally:
        vm.stop(timeout=10)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--iso", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True, help="directory for screenshots and logs")
    ap.add_argument("--scenario", choices=("all", "uefi", "serial", "usb", "install", "full"), default="all",
                    help="all = serial, uefi and usb; full = all plus the installed-system test (slow)")
    ap.add_argument("--timeout", type=float, default=None, help="per-scenario timeout in seconds")
    ap.add_argument("--fixtures-iso", type=Path, default=None,
                    help="WinCompat test fixtures (tests/compatibility/build_fixtures.sh) attached as a second CD")
    args = ap.parse_args()

    if not shutil.which("qemu-system-x86_64"):
        record("SKIP", "QEMU boot test (qemu-system-x86_64 not installed)")
        return 0
    if not args.iso.is_file():
        record("FAIL", f"QEMU boot test: ISO not found ({args.iso})")
        return 1
    kvm = os.access("/dev/kvm", os.R_OK | os.W_OK)
    timeout = args.timeout or (900 if kvm else 2700)
    log(f"acceleration: {'KVM' if kvm else 'TCG (software emulation; slow)'}; timeout {int(timeout)} s per scenario")
    args.out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="boswas-boot-") as tmp:
        workdir = Path(tmp)
        if args.scenario in ("all", "full", "serial"):
            fixtures = args.fixtures_iso if args.fixtures_iso and args.fixtures_iso.is_file() else None
            scenario_serial(args.iso, workdir, args.out, timeout, fixtures)
        if args.scenario in ("all", "full", "uefi"):
            scenario_uefi(args.iso, workdir, args.out, timeout)
        if args.scenario in ("all", "full", "usb"):
            scenario_usb(args.iso, workdir, args.out, timeout)
        if args.scenario in ("full", "install"):
            scenario_install(args.iso, workdir, args.out, timeout)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
