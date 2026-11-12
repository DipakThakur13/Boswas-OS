"""Shipped runtime facts (/usr/share/boswas/android/runtime.conf) and Waydroid's own files.

KEY="value" files are parsed without evaluation, like WinCompat's
runtime.conf. Waydroid's waydroid.cfg is an INI file read with configparser
(no interpolation). Every read goes through a Root, so the tests point the
code at a temporary directory.
"""

from __future__ import annotations

import configparser
import re
import shlex
from dataclasses import dataclass

from .system import Root

RUNTIME_CONF = "/usr/share/boswas/android/runtime.conf"
KEYRING = "/usr/share/boswas/keyrings/waydroid.gpg"
WAYDROID_WORK = "/var/lib/waydroid"
WAYDROID_CFG = f"{WAYDROID_WORK}/waydroid.cfg"
WAYDROID_LOG = f"{WAYDROID_WORK}/waydroid.log"
WAYDROID_ROOTFS = f"{WAYDROID_WORK}/rootfs"
DEFAULT_IMAGES = f"{WAYDROID_WORK}/images"

_KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_FINGERPRINT_RE = re.compile(r"[0-9A-F]{40}")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_PACKAGE_RE = re.compile(r"[a-z0-9][a-z0-9+.-]{0,99}")


def parse_env(text: str | None) -> dict[str, str]:
    result: dict[str, str] = {}
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not _KEY_RE.fullmatch(key):
            continue
        try:
            parts = shlex.split(value, comments=True)
        except ValueError:
            continue
        result[key] = " ".join(parts)
    return result


@dataclass(frozen=True)
class RuntimeConf:
    repo_url: str
    repo_suite: str
    repo_components: tuple[str, ...]
    key_fingerprint: str
    allowed_packages: tuple[str, ...]
    image_type: str
    fdroid_url: str
    fdroid_cert_sha256: str

    @property
    def valid(self) -> bool:
        return not self.problems()

    def problems(self) -> list[str]:
        problems = []
        if not self.repo_url.startswith("https://"):
            problems.append("REPO_URL must be an https:// URL")
        if not self.repo_suite or not _KEY_RE.fullmatch(self.repo_suite.replace("-", "_")):
            problems.append("REPO_SUITE is missing or invalid")
        if not self.repo_components:
            problems.append("REPO_COMPONENTS is empty")
        if not _FINGERPRINT_RE.fullmatch(self.key_fingerprint):
            problems.append("KEY_FINGERPRINT must be 40 upper-case hexadecimal digits")
        if not self.allowed_packages or not all(_PACKAGE_RE.fullmatch(p) for p in self.allowed_packages):
            problems.append("ALLOWED_PACKAGES is empty or names an invalid package")
        if self.image_type != "VANILLA":
            problems.append("IMAGE_TYPE must be VANILLA (Boswas OS never requests Google applications)")
        if not self.fdroid_url.startswith("https://"):
            problems.append("FDROID_URL must be an https:// URL")
        if not _SHA256_RE.fullmatch(self.fdroid_cert_sha256):
            problems.append("FDROID_CERT_SHA256 must be 64 lower-case hexadecimal digits")
        return problems


def runtime_conf(root: Root) -> RuntimeConf:
    values = parse_env(root.read(RUNTIME_CONF, 65536))
    return RuntimeConf(
        repo_url=values.get("REPO_URL", ""),
        repo_suite=values.get("REPO_SUITE", ""),
        repo_components=tuple(values.get("REPO_COMPONENTS", "").split()),
        key_fingerprint=values.get("KEY_FINGERPRINT", "").replace(" ", "").upper(),
        allowed_packages=tuple(values.get("ALLOWED_PACKAGES", "").split()),
        image_type=values.get("IMAGE_TYPE", ""),
        fdroid_url=values.get("FDROID_URL", ""),
        fdroid_cert_sha256=values.get("FDROID_CERT_SHA256", "").lower(),
    )


def waydroid_cfg(root: Root) -> dict[str, str] | None:
    """The [waydroid] section of /var/lib/waydroid/waydroid.cfg, or None if absent/unreadable."""
    text = root.read(WAYDROID_CFG, 256 * 1024)
    if text is None:
        return None
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    try:
        parser.read_string(text)
    except configparser.Error:
        return None
    if not parser.has_section("waydroid"):
        return None
    return {key: value for key, value in parser.items("waydroid")}


def image_type_from_ota(url: str | None) -> str | None:
    """VANILLA / GAPPS / FOSS from a system OTA URL (…/waydroid_x86_64/VANILLA.json)."""
    if not url or url == "None":
        return None
    match = re.search(r"/([A-Za-z]+)\.json$", url)
    return match.group(1).upper() if match else None
