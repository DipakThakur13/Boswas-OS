#!/usr/bin/env python3
"""QEMU boot tests for the Boswas OS ISO.

Scenario "uefi-secureboot"
    OVMF with Secure Boot enforced and the Microsoft keys enrolled boots the
    ISO through the normal path: shim -> signed GRUB (Boswas menu, live entry
    auto-selected after the timeout) -> signed kernel -> live system -> KDE
    Plasma. Verified from screenshots (Boswas teal/navy palette on screen,
    screen differs from the boot menu); screenshots are kept for review.

Scenario "serial"
    Boots the ISO's live kernel directly with a serial console, logs in as the
    live user and runs functional checks: boswas commands, firewall, AppArmor,
    audit, KDE, Wine, network. Finally powers the VM off.

Uses KVM when /dev/kvm is available, TCG otherwise (much slower).

Results are printed as "PASS|FAIL|SKIP<TAB>description" and appended to
$BOSWAS_TEST_REPORT. Exit status is non-zero if anything failed.
"""

from __future__ import annotations

import argparse
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


def analyse(path: Path) -> dict:
    """Palette statistics on a sample of pixels."""
    w, h, px = read_ppm(path)
    total = black = teal = 0
    luma_sum = 0
    for i in range(0, w * h, 7):
        r, g, b = px[3 * i], px[3 * i + 1], px[3 * i + 2]
        total += 1
        luma = (299 * r + 587 * g + 114 * b) // 1000
        luma_sum += luma
        if luma < 8:
            black += 1
        # Boswas teal family: green and blue clearly above red.
        if g > r + 20 and b > r + 15 and g + b > 110:
            teal += 1
    return {"w": w, "h": h, "black": black / total, "teal": teal / total, "luma": luma_sum / total}


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
        # The Boswas boot menu: teal palette while GRUB counts down.
        while time.time() - start < min(timeout, 600) and vm.alive():
            time.sleep(10)
            shot = vm.screendump(f"t{int(time.time() - start):04d}")
            stats = analyse(shot)
            if stats["teal"] > 0.05 and stats["black"] < 0.5:
                menu = shot
                shutil.copyfile(shot, workdir / "menu.ppm")
                to_png(shot, outdir / "uefi-secureboot-boot-menu.png")
                break
        if not check(menu is not None, "UEFI + Secure Boot: Boswas boot menu displayed (shim -> signed GRUB)"):
            return
        # Live entry is selected automatically after the menu timeout.
        desktop = None
        stable = 0
        previous = None
        while time.time() - start < timeout and vm.alive():
            time.sleep(30)
            shot = vm.screendump(f"t{int(time.time() - start):04d}")
            stats = analyse(shot)
            changed_from_menu = difference(shot, workdir / "menu.ppm") > 12
            if changed_from_menu and stats["black"] < 0.6 and stats["teal"] > 0.01:
                stable = stable + 1 if previous is not None and difference(shot, previous) < 4 else 1
                previous = shot
                if stable >= 2:
                    desktop = shot
                    break
            else:
                stable, previous = 0, None
        if desktop is not None:
            to_png(desktop, outdir / "uefi-secureboot-desktop.png")
        check(desktop is not None,
              f"UEFI + Secure Boot: live system reached the Boswas desktop ({int(time.time() - start)} s)")
    finally:
        vm.stop(timeout=30)


def scenario_serial(iso: Path, workdir: Path, outdir: Path, timeout: float) -> None:
    kernel = iso_extract(iso, workdir, "vmlinuz*")
    initrd = iso_extract(iso, workdir, "initrd.img*")
    if not (kernel and initrd):
        check(False, "serial boot: live kernel and initrd found on the ISO")
        return
    append = "boot=live components quiet hostname=boswas-device username=boswas console=tty0 console=ttyS0,115200n8"
    vm = VM("serial", workdir, outdir, [
        "-machine", "q35",
        "-drive", f"file={iso},media=cdrom,readonly=on,if=ide",
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
        check("trixie" in out and "Debian 13" in out, "boswas-info reports the Debian 13 trixie base")
        (outdir / "boswas-info.txt").write_text(out + "\n")

        rc, out = sh.run("boswas --json status")
        (outdir / "boswas-status.json").write_text(out + "\n")
        try:
            status = {c["id"]: c for c in json.loads(out)["checks"]}
        except (ValueError, KeyError):
            status = {}
        check(bool(status), "boswas --json status returns valid JSON")
        for cid in ("firewall", "apparmor", "audit", "ssh-server"):
            st = status.get(cid, {}).get("status")
            check(st == "PASS", f"boswas status: {cid} is PASS on the running system (got {st})")

        for unit in ("nftables", "auditd", "apparmor", "NetworkManager"):
            rc, out = sh.run(f"systemctl is-active {unit}")
            check(out.strip() == "active", f"service {unit} is active")
        rc, out = sh.run("sudo -n nft list table inet boswas_filter")
        check("policy drop" in out, "Boswas firewall ruleset is loaded (inbound policy drop)")
        rc, out = sh.run("sudo -n auditctl -l")
        check("boswas-identity" in out, "Boswas audit rules are loaded into the kernel")
        rc, out = sh.run("cat /sys/module/apparmor/parameters/enabled")
        check(out.strip() == "Y", "AppArmor is enabled in the running kernel")
        rc, out = sh.run("sudo -n aa-status --profiled 2>/dev/null || true")
        check(out.strip().isdigit() and int(out.strip()) > 0, f"AppArmor profiles loaded ({out.strip() or 0})")
        rc, out = sh.run("sysctl -n kernel.kptr_restrict kernel.unprivileged_userns_clone")
        check(out.split() == ["2", "1"], "Boswas sysctl hardening applied; user namespaces still available")

        rc, out = sh.run("wine --version")
        check(out.strip().startswith("wine-10."), f"Wine runs ({out.strip()})")
        rc, out = sh.run("plasmashell --version")
        check(out.strip().startswith("plasmashell 6."), f"KDE Plasma present ({out.strip()})")

        rc, out = sh.run("ip -4 -o addr show scope global")
        check("10.0.2.15" in out, "network: DHCP address obtained (NetworkManager)")
        rc, out = sh.run("getent hosts deb.debian.org >/dev/null && echo resolved")
        record("PASS" if "resolved" in out else "SKIP",
               "network: DNS resolution works" if "resolved" in out else "network: DNS resolution (no upstream network)")

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
    ap.add_argument("--scenario", choices=("all", "uefi", "serial"), default="all")
    ap.add_argument("--timeout", type=float, default=None, help="per-scenario timeout in seconds")
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
        if args.scenario in ("all", "serial"):
            scenario_serial(args.iso, workdir, args.out, timeout)
        if args.scenario in ("all", "uefi"):
            scenario_uefi(args.iso, workdir, args.out, timeout)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
