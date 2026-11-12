"""Shared test fixtures: synthetic APKs (binary XML encoder, signing blocks), a fake runner, a fake root.

Synthetic APKs follow the real formats closely enough that the inspector
cannot tell them from APKs built by Android's tools: binary AndroidManifest.xml
with a string pool, resource map and typed attributes; APK Signing Blocks with
v2/v3 signer structures carrying real X.509 certificates; v1 signatures as
PKCS#7 made by openssl. Certificates are generated with openssl at test
time (tests that need them skip when openssl is missing).

The tests add their own import path, so they run without PYTHONPATH.
"""

from __future__ import annotations

import io
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve()
PACKAGE = HERE.parents[1]
if str(PACKAGE) not in sys.path:
    sys.path.insert(0, str(PACKAGE))

from boswas_android import runner as runner_mod  # noqa: E402
from boswas_android.runtime import Context  # noqa: E402
from boswas_android.system import Host, Root  # noqa: E402

ANDROID = "http://schemas.android.com/apk/res/android"
STRING, INT, BOOL, REF = 0x03, 0x10, 0x12, 0x01
RID = {"label": 0x01010001, "minSdkVersion": 0x0101020C, "versionCode": 0x0101021B, "versionName": 0x0101021C,
       "targetSdkVersion": 0x01010270, "versionCodeMajor": 0x01010576, "isSplitRequired": 0x01010591}


# --- binary XML encoder ------------------------------------------------------------------------------

@dataclass
class Attr:
    name: str
    value: object
    type: int = STRING
    ns: str | None = ANDROID
    rid: int | None = None        # defaults to RID[name] for android: attributes


@dataclass
class Node:
    name: str
    attrs: list = field(default_factory=list)
    children: list = field(default_factory=list)


def _pad4(data: bytes) -> bytes:
    return data + b"\0" * (-len(data) % 4)


def encode_axml(root: Node, utf8: bool = False, hide_names: bool = False) -> bytes:
    """Binary XML for the tree; hide_names drops attribute names (resource IDs only, like obfuscated APKs)."""
    attr_nodes: list[Attr] = []

    def walk(node):
        attr_nodes.extend(node.attrs)
        for child in node.children:
            walk(child)
    walk(root)
    with_rid, rid_list = [], []
    for attr in attr_nodes:
        rid = attr.rid if attr.rid is not None else (RID.get(attr.name) if attr.ns == ANDROID else None)
        key = (attr.name, rid)
        if rid is not None and key not in with_rid:
            with_rid.append(key)
            rid_list.append(rid)
    strings: list[str] = []
    index: dict[str, int] = {}

    def s(text: str) -> int:
        if text not in index:
            index[text] = len(strings)
            strings.append(text)
        return index[text]

    rid_index = {}
    for i, (name, rid) in enumerate(with_rid):
        # The first strings correspond 1:1 to the resource map.
        placeholder = f"_{i}" if hide_names else name
        rid_index[(name, rid)] = s(placeholder)
    s(ANDROID)
    s("android")

    def attr_name_index(attr: Attr) -> int:
        rid = attr.rid if attr.rid is not None else (RID.get(attr.name) if attr.ns == ANDROID else None)
        if rid is not None:
            return rid_index[(attr.name, rid)]
        return s(attr.name)

    def collect(node):
        s(node.name)
        for attr in node.attrs:
            attr_name_index(attr)
            if attr.type == STRING:
                s(str(attr.value))
        for child in node.children:
            collect(child)
    collect(root)

    # string pool
    offsets, blob = [], b""
    for text in strings:
        offsets.append(len(blob))
        if utf8:
            raw = text.encode("utf-8")
            assert len(text) < 128 and len(raw) < 128
            blob += bytes([len(text), len(raw)]) + raw + b"\0"
        else:
            raw = text.encode("utf-16-le")
            blob += struct.pack("<H", len(raw) // 2) + raw + b"\0\0"
    blob = _pad4(blob)
    header_size = 28
    strings_start = header_size + 4 * len(strings)
    pool_size = strings_start + len(blob)
    pool = struct.pack("<HHIIIIII", 0x0001, header_size, pool_size, len(strings), 0, 0x100 if utf8 else 0,
                       strings_start, 0) + b"".join(struct.pack("<I", o) for o in offsets) + blob
    resmap = struct.pack("<HHI", 0x0180, 8, 8 + 4 * len(rid_list)) + b"".join(struct.pack("<I", r) for r in rid_list)
    body = struct.pack("<HHIIIII", 0x0100, 16, 24, 1, 0xFFFFFFFF, s("android"), s(ANDROID))

    def element(node):
        nonlocal body
        attrs = b""
        for attr in node.attrs:
            ns = s(attr.ns) if attr.ns else 0xFFFFFFFF
            if attr.type == STRING:
                raw = s(str(attr.value))
                data = raw
            elif attr.type == BOOL:
                raw, data = 0xFFFFFFFF, 0xFFFFFFFF if attr.value else 0
            else:
                raw, data = 0xFFFFFFFF, int(attr.value) & 0xFFFFFFFF
            attrs += struct.pack("<IIIHBBI", ns, attr_name_index(attr), raw, 8, 0, attr.type, data)
        size = 16 + 20 + len(attrs)
        body += struct.pack("<HHIII", 0x0102, 16, size, 1, 0xFFFFFFFF)
        body += struct.pack("<IIHHHHHH", 0xFFFFFFFF, s(node.name), 20, 20, len(node.attrs), 0, 0, 0) + attrs
        for child in node.children:
            element(child)
        body += struct.pack("<HHIIIII", 0x0103, 16, 24, 1, 0xFFFFFFFF, 0xFFFFFFFF, s(node.name))
    element(root)
    body += struct.pack("<HHIIIII", 0x0101, 16, 24, 1, 0xFFFFFFFF, s("android"), s(ANDROID))
    content = pool + resmap + body
    return struct.pack("<HHI", 0x0003, 8, 8 + len(content)) + content


def manifest(package="com.boswas.testapp", version_code=42, version_name="1.2.3", label="Boswas Test",
             min_sdk=24, target_sdk=33, split=None, split_required=False, required_split_types=None,
             label_ref=False, version_code_major=None, extra_manifest_attrs=(), utf8=False,
             hide_names=False) -> bytes:
    attrs = [Attr("versionCode", version_code, INT), Attr("versionName", version_name)]
    if package is not None:
        attrs.insert(0, Attr("package", package, ns=None))
    if version_code_major is not None:
        attrs.append(Attr("versionCodeMajor", version_code_major, INT))
    if split:
        attrs.append(Attr("split", split, ns=None))
    if required_split_types:
        attrs.append(Attr("requiredSplitTypes", required_split_types))
    attrs.extend(extra_manifest_attrs)
    app_attrs = [Attr("label", 0x7F0B0001, REF) if label_ref else Attr("label", label)]
    if split_required:
        app_attrs.append(Attr("isSplitRequired", True, BOOL))
    root = Node("manifest", attrs, [
        Node("uses-sdk", [Attr("minSdkVersion", min_sdk, INT), Attr("targetSdkVersion", target_sdk, INT)]),
        Node("application", app_attrs, [Node("activity", [Attr("name", ".Main")])]),
    ])
    return encode_axml(root, utf8=utf8, hide_names=hide_names)


# --- APK builder ----------------------------------------------------------------------------------------

def _lp(data: bytes) -> bytes:
    return struct.pack("<I", len(data)) + data


def v2_value(certs: list[bytes]) -> bytes:
    signed_data = _lp(b"") + _lp(b"".join(_lp(c) for c in certs)) + _lp(b"")
    signer = _lp(signed_data) + _lp(b"") + _lp(b"")
    return _lp(_lp(signer))


def v3_value(certs: list[bytes]) -> bytes:
    signed_data = _lp(b"") + _lp(b"".join(_lp(c) for c in certs)) + struct.pack("<II", 24, 0x7FFFFFFF) + _lp(b"")
    signer = _lp(signed_data) + struct.pack("<II", 24, 0x7FFFFFFF) + _lp(b"") + _lp(b"")
    return _lp(_lp(signer))


def signing_block(pairs: dict[int, bytes]) -> bytes:
    body = b"".join(struct.pack("<QI", len(v) + 4, k) + v for k, v in pairs.items())
    size = len(body) + 8 + 16
    return struct.pack("<Q", size) + body + struct.pack("<Q", size) + b"APK Sig Block 42"


def build_apk(path, manifest_bytes: bytes | None, abis=(), entries=None, pairs=None, compress=zipfile.ZIP_DEFLATED):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compress) as archive:
        if manifest_bytes is not None:
            archive.writestr("AndroidManifest.xml", manifest_bytes)
        archive.writestr("classes.dex", b"dex\n035\0" + b"\0" * 64)
        for abi in abis:
            archive.writestr(f"lib/{abi}/libnative.so", b"\x7fELF" + b"\0" * 60)
        for name, data in (entries or {}).items():
            archive.writestr(name, data)
    data = buffer.getvalue()
    if pairs:
        eocd = data.rfind(b"PK\x05\x06")
        (cd_offset,) = struct.unpack_from("<I", data, eocd + 16)
        block = signing_block(pairs)
        data = data[:cd_offset] + block + data[cd_offset:]
        eocd += len(block)
        data = data[:eocd + 16] + struct.pack("<I", cd_offset + len(block)) + data[eocd + 20:]
    Path(path).write_bytes(data)
    return path


# --- certificates (openssl) ----------------------------------------------------------------------------

OPENSSL = shutil.which("openssl")


class Certs:
    """A throw-away key and self-signed certificate, and helpers that sign with them."""

    def __init__(self, directory: str, cn: str = "Boswas Test"):
        self.dir = directory
        self.key = os.path.join(directory, f"{cn.replace(' ', '_')}.key")
        self.pem = os.path.join(directory, f"{cn.replace(' ', '_')}.pem")
        subprocess.run([OPENSSL, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", self.key, "-out", self.pem,
                        "-subj", f"/CN={cn}", "-days", "2"], check=True, capture_output=True)
        self.der = subprocess.run([OPENSSL, "x509", "-in", self.pem, "-outform", "DER"], check=True,
                                  capture_output=True).stdout

    def pkcs7(self, content: bytes) -> bytes:
        src = os.path.join(self.dir, "content.sf")
        Path(src).write_bytes(content)
        return subprocess.run([OPENSSL, "smime", "-sign", "-binary", "-noattr", "-outform", "DER", "-md", "sha256",
                               "-signer", self.pem, "-inkey", self.key, "-in", src], check=True,
                              capture_output=True).stdout


# --- fake runner, root and clock --------------------------------------------------------------------------

class FakeRunner:
    """Records every command (after the real allowlist check) and answers from a table."""

    def __init__(self):
        self.calls: list[list[str]] = []
        self.started: list[list[str]] = []
        self.streams: list[list[str]] = []
        self.answers: dict[tuple, object] = {}
        self.stream_lines: list[str] = []
        self.stream_code = 0
        self.start_error: Exception | None = None
        self.cancel_events = []

    def available(self, name):
        return True

    def run(self, command, timeout=60.0):
        runner_mod.check(command)
        self.calls.append(list(command))
        answer = self.answers.get(tuple(command[1:]))
        if callable(answer):
            answer = answer()
        if isinstance(answer, Exception):
            raise answer
        return answer or runner_mod.Completed(0, "", "")

    def start(self, command, log_path=None, settle=2.0):
        runner_mod.check(command)
        self.started.append(list(command))
        if self.start_error:
            raise self.start_error

    def stream(self, command, on_line, cancel_event=None, timeout=None):
        runner_mod.check(command)
        self.streams.append(list(command))
        self.cancel_events.append(cancel_event)
        for line in self.stream_lines:
            on_line(line)
        return self.stream_code


class Clock:
    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def done(stdout="", code=0, stderr=""):
    return runner_mod.Completed(code, stdout, stderr)


CPUINFO = "processor\t: 0\nflags\t\t: fpu sse sse2 ssse3 sse4_1 sse4_2 popcnt avx\n"
DPKG_WAYDROID = ("Package: waydroid\nStatus: install ok installed\nPriority: optional\nVersion: 1.6.3\n"
                 "Architecture: all\n\nPackage: lxc\nStatus: install ok installed\nVersion: 1:6.0.4-4\n")
WAYDROID_CFG = ("[waydroid]\narch = x86_64\nimages_path = /var/lib/waydroid/images\nvendor_type = MAINLINE\n"
                "system_datetime = 1790000000\nvendor_datetime = 1790000500\nsuspend_action = freeze\n"
                "system_ota = https://ota.waydro.id/system/lineage/waydroid_x86_64/VANILLA.json\n"
                "vendor_ota = https://ota.waydro.id/vendor/waydroid_x86_64/MAINLINE.json\n")
STATUS_RUNNING = ("Session:\tRUNNING\nContainer:\tRUNNING\nVendor type:\tMAINLINE\nIP address:\t192.168.240.112\n"
                  "Session user:\ttester(1000)\nWayland display:\twayland-0\n")
STATUS_STOPPED = "Session:\tSTOPPED\nVendor type:\tMAINLINE\n"


def write(root: str, path: str, data, mode=None):
    target = Path(root) / path.lstrip("/")
    target.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, bytes):
        target.write_bytes(data)
    else:
        target.write_text(data, encoding="utf-8")
    if mode is not None:
        os.chmod(target, mode)
    return target


def make_root(tmp: str, *, installed=False, initialized=False, binder="module", cpuinfo=CPUINFO,
              gpu_driver="i915", secure_boot=1, conf_overrides=None) -> str:
    root = os.path.join(tmp, "root")
    os.makedirs(root, exist_ok=True)
    write(root, "/proc/cpuinfo", cpuinfo)
    write(root, "/proc/filesystems", "nodev\tsysfs\nnodev\tproc\n")
    write(root, "/sys/fs/cgroup/cgroup.controllers", "cpu memory pids\n")
    if binder == "module":
        write(root, "/lib/modules/6.12.111+deb13-amd64/kernel/drivers/android/binder_linux.ko.xz", b"\0")
    elif binder == "node":
        write(root, "/dev/binder", b"")
    if gpu_driver:
        drm = Path(root) / "sys/class/drm/renderD128/device"
        drm.mkdir(parents=True, exist_ok=True)
        os.symlink(f"../../../bus/pci/drivers/{gpu_driver}", drm / "driver")
    else:
        (Path(root) / "sys/class/drm").mkdir(parents=True, exist_ok=True)
    if secure_boot is not None:
        write(root, "/sys/firmware/efi/efivars/SecureBoot-8be4df61-93ca-11d2-aa0d-00e098032b8c",
              bytes([6, 0, 0, 0, secure_boot]))
    conf = (PACKAGE / "data" / "runtime.conf").read_text(encoding="utf-8")
    for key, value in (conf_overrides or {}).items():
        conf += f'\n{key}="{value}"\n'
    write(root, "/usr/share/boswas/android/runtime.conf", conf)
    status = "Package: base-files\nStatus: install ok installed\nVersion: 13.8\n"
    if installed:
        status += "\n" + DPKG_WAYDROID
        write(root, "/usr/bin/waydroid", "#!/bin/sh\n", 0o755)
    write(root, "/var/lib/dpkg/status", status)
    if initialized:
        write(root, "/var/lib/waydroid/waydroid.cfg", WAYDROID_CFG)
        (Path(root) / "var/lib/waydroid/rootfs").mkdir(parents=True, exist_ok=True)
        write(root, "/var/lib/waydroid/images/system.img", b"\0" * 3000)
        write(root, "/var/lib/waydroid/images/vendor.img", b"\0" * 1000)
        write(root, "/var/lib/waydroid/waydroid.log", "line one\nline two\n")
    return root


def make_ctx(tmp: str, root: str, runner=None, environ=None, machine="x86_64", opener=None):
    home = os.path.join(tmp, "home")
    os.makedirs(home, exist_ok=True)
    env = {"HOME": home, "XDG_SESSION_TYPE": "wayland", "WAYLAND_DISPLAY": "wayland-0", "PATH": "/usr/bin"}
    env.update(environ or {})
    clock = Clock()
    ctx = Context(root=Root(root), host=Host(env, release="6.12.111+deb13-amd64", machine=machine),
                  runner=runner or FakeRunner(), sleep=clock.sleep, monotonic=clock.monotonic, opener=opener)
    return ctx


def tempdir(testcase) -> str:
    directory = tempfile.mkdtemp(prefix="boswas-android-test-")
    testcase.addCleanup(shutil.rmtree, directory, True)
    return directory
