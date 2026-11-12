"""Software sources as Control Center reads them (pure Python, no Qt, no I/O).

  apt-get -s -o Debug::NoLocking=1 --with-new-pkgs upgrade
        the pending updates: a simulation as the desktop user, from the package
        lists already on the device (refreshing them is boswas-sysops apt-refresh)
  apt-cache search --names-only / show / policy
        Debian packages: search, details, installed and candidate versions
  flatpak remotes / list / search (tab-separated --columns output)

Every value from these programs is untrusted text and goes through
viewmodel.one_line() before it is shown.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import viewmodel as vm

# --- pending updates (apt-get -s upgrade) ----------------------------------------------------------------

# Inst openssl [3.5.1-1] (3.5.2-1~deb13u1 Debian-Security:13/stable-security [amd64])
# Inst newpkg (1.0-1 Debian:13.1/stable [all])
_INST_RE = re.compile(r"^Inst (\S+) (?:\[([^\]]*)\] )?\((\S+) (.*?) ?\[([^\]]+)\]\)")
_SUMMARY_RE = re.compile(r"^(\d+) upgraded, (\d+) newly installed, (\d+) to remove and (\d+) not upgraded\.")
_NAME_RE = re.compile(r"[a-z0-9][a-z0-9+.-]{0,99}(:[a-z0-9]+)?")


@dataclass(frozen=True)
class Upgrade:
    name: str
    old: str
    new: str
    origins: str
    arch: str
    security: bool
    new_install: bool


@dataclass(frozen=True)
class UpgradePlan:
    upgrades: tuple[Upgrade, ...]
    upgraded: int | None = None
    newly_installed: int | None = None
    to_remove: int | None = None
    held_back: int | None = None

    @property
    def security_count(self) -> int:
        return sum(1 for u in self.upgrades if u.security)


def is_security_origin(origins: str) -> bool:
    text = origins.casefold()
    return "-security" in text or "debian-security" in text


def parse_simulated_upgrade(text: object) -> UpgradePlan:
    """The packages `apt-get -s ... upgrade` would install (Inst lines) and its summary line."""
    upgrades: list[Upgrade] = []
    counts: tuple[int, ...] | None = None
    seen: set[tuple[str, str]] = set()
    for line in text.splitlines() if isinstance(text, str) else []:
        match = _INST_RE.match(line.strip())
        if match:
            name, old, new, origins, arch = match.groups()
            if not _NAME_RE.fullmatch(name) or (name, arch) in seen:
                continue
            seen.add((name, arch))
            upgrades.append(Upgrade(vm.one_line(name, 100), vm.one_line(old or "", 60), vm.one_line(new, 60),
                                    vm.one_line(origins, 200), vm.one_line(arch, 20), is_security_origin(origins),
                                    old is None))
            continue
        summary = _SUMMARY_RE.match(line.strip())
        if summary:
            counts = tuple(int(g) for g in summary.groups())
    upgrades.sort(key=lambda u: (not u.security, u.name))
    if counts is None:
        return UpgradePlan(tuple(upgrades))
    return UpgradePlan(tuple(upgrades), *counts)


# --- apt-cache ------------------------------------------------------------------------------------------------

SEARCH_LIMIT = 60
SEARCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9+._ -]{0,63}$")


def valid_search(term: object) -> bool:
    """A search term passed to apt-cache/flatpak: letters, digits, + . _ - and spaces (no regex syntax)."""
    return isinstance(term, str) and bool(SEARCH_RE.fullmatch(term.strip()))


@dataclass(frozen=True)
class PackageHit:
    name: str
    summary: str
    installed: str = ""            # installed version, "" when not installed
    candidate: str = ""            # version that would be installed
    source: str = "Debian (signed repository)"

    @property
    def is_installed(self) -> bool:
        return bool(self.installed)


def parse_apt_search(text: object, limit: int = SEARCH_LIMIT) -> list[tuple[str, str]]:
    """`apt-cache search --names-only` lines "name - summary" (first `limit`)."""
    hits: list[tuple[str, str]] = []
    for line in text.splitlines() if isinstance(text, str) else []:
        name, sep, summary = line.partition(" - ")
        name = name.strip()
        if not sep or not re.fullmatch(r"[a-z0-9][a-z0-9+.-]{0,99}", name):
            continue
        hits.append((name, vm.one_line(summary, 200)))
        if len(hits) >= limit:
            break
    return hits


def parse_apt_policy(text: object) -> dict[str, dict]:
    """`apt-cache policy PKG...` -> {name: {"installed": str, "candidate": str, "sources": [str]}}."""
    result: dict[str, dict] = {}
    current: dict | None = None
    in_table = False
    for raw in text.splitlines() if isinstance(text, str) else []:
        if raw and not raw[0].isspace() and raw.rstrip().endswith(":"):
            name = raw.rstrip()[:-1]
            current = {"installed": "", "candidate": "", "sources": []}
            if re.fullmatch(r"[a-z0-9][a-z0-9+.-]{0,99}(:[a-z0-9]+)?", name):
                result[name] = current
            in_table = False
            continue
        if current is None:
            continue
        line = raw.strip()
        if line.startswith("Installed:"):
            value = line.partition(":")[2].strip()
            current["installed"] = "" if value == "(none)" else vm.one_line(value, 60)
        elif line.startswith("Candidate:"):
            value = line.partition(":")[2].strip()
            current["candidate"] = "" if value == "(none)" else vm.one_line(value, 60)
        elif line.startswith("Version table:"):
            in_table = True
        elif in_table and re.match(r"^\d+ \S+", line):
            source = line.split(None, 1)[1] if " " in line else ""
            if source and source not in current["sources"] and len(current["sources"]) < 8:
                current["sources"].append(vm.one_line(source, 160))
    return result


def source_label(sources: list[str]) -> str:
    """Where a package comes from, in words (the sources APT verifies by signature)."""
    names = " ".join(sources).casefold()
    if not sources:
        return "Unknown source"
    if "/var/lib/dpkg/status" in names and len(sources) == 1:
        return "Installed locally (no repository offers it)"
    if "deb.debian.org" in names or "debian" in names:
        return "Debian (signed repository)"
    return "Configured repository: " + vm.one_line(sources[0], 80)


def hits_with_policy(hits: list[tuple[str, str]], policy: dict[str, dict]) -> list[PackageHit]:
    result = []
    for name, summary in hits:
        info = policy.get(name) or {}
        sources = info.get("sources") or []
        result.append(PackageHit(name, summary, info.get("installed", ""), info.get("candidate", ""),
                                 source_label([s for s in sources if "/var/lib/dpkg/status" not in s] or sources)))
    return result


SHOW_FIELDS = ("Package", "Version", "Section", "Installed-Size", "Size", "Homepage", "Maintainer", "Depends")


def parse_apt_show(text: object) -> dict[str, str]:
    """The first stanza of `apt-cache show PKG`: selected fields plus the description."""
    fields: dict[str, str] = {}
    description: list[str] = []
    key = None
    for raw in text.splitlines() if isinstance(text, str) else []:
        if not raw.strip():
            if fields:
                break
            continue
        if raw[0].isspace():
            if key in ("Description", "Description-en"):
                line = raw.strip()
                description.append("" if line == "." else line)
            continue
        key, _, value = raw.partition(":")
        key = key.strip()
        value = value.strip()
        if key in ("Description", "Description-en"):
            description = [value]
        elif key in SHOW_FIELDS and key not in fields:
            fields[key] = vm.one_line(value, 300)
    summary = vm.one_line(description[0], 200) if description else ""
    long = vm.sanitize("\n".join(description[1:])).strip()[:3000]
    fields["Summary"] = summary
    fields["Description"] = long
    return fields


def size_text(fields: dict[str, str]) -> str:
    """Download and installed size from apt-cache show (Installed-Size is in KiB)."""
    parts = []
    size = fields.get("Size", "")
    if size.isdigit():
        parts.append(f"download {vm.format_bytes(int(size))}")
    installed = fields.get("Installed-Size", "")
    if installed.isdigit():
        parts.append(f"installed {vm.format_bytes(int(installed) * 1024)}")
    return ", ".join(parts) or "Unknown"


# --- flatpak ----------------------------------------------------------------------------------------------

FLATPAK_APP_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*(\.[A-Za-z0-9_-]+){2,}$")


def valid_flatpak_app(value: object) -> bool:
    return isinstance(value, str) and len(value) <= 255 and bool(FLATPAK_APP_RE.fullmatch(value))


@dataclass(frozen=True)
class FlatpakRemote:
    name: str
    url: str
    installation: str              # system | user
    disabled: bool = False


@dataclass(frozen=True)
class FlatpakApp:
    app_id: str
    name: str
    version: str = ""
    remote: str = ""
    description: str = ""
    installation: str = ""


def _columns(line: str, count: int) -> list[str]:
    parts = line.split("\t")
    return (parts + [""] * count)[:count]


def parse_flatpak_remotes(text: object, installation: str) -> list[FlatpakRemote]:
    """`flatpak remotes --<installation> --columns=name,url,options`."""
    remotes = []
    for line in text.splitlines() if isinstance(text, str) else []:
        name, url, options = _columns(line, 3)
        name = name.strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", name):
            continue
        remotes.append(FlatpakRemote(name, vm.one_line(url, 200), installation, "disabled" in options))
    return remotes


def parse_flatpak_list(text: object, installation: str = "user") -> list[FlatpakApp]:
    """`flatpak list --<installation> --app --columns=application,name,version,origin`."""
    apps = []
    for line in text.splitlines() if isinstance(text, str) else []:
        app_id, name, version, origin = _columns(line, 4)
        app_id = app_id.strip()
        if not valid_flatpak_app(app_id):
            continue
        apps.append(FlatpakApp(app_id, vm.one_line(name, 120) or app_id, vm.one_line(version, 40),
                               vm.one_line(origin, 60), "", installation))
    return apps


def parse_flatpak_search(text: object, limit: int = SEARCH_LIMIT) -> list[FlatpakApp]:
    """`flatpak search --columns=application,name,description,version,remotes TERM`."""
    apps = []
    for line in text.splitlines() if isinstance(text, str) else []:
        app_id, name, description, version, remotes = _columns(line, 5)
        app_id = app_id.strip()
        if not valid_flatpak_app(app_id):
            continue
        remote = remotes.split(",")[0].strip()
        apps.append(FlatpakApp(app_id, vm.one_line(name, 120) or app_id, vm.one_line(version, 40),
                               vm.one_line(remote, 60), vm.one_line(description, 200)))
        if len(apps) >= limit:
            break
    return apps


@dataclass(frozen=True)
class FlatpakState:
    remotes: tuple[FlatpakRemote, ...] = ()
    apps: tuple[FlatpakApp, ...] = ()
    shipped: tuple[str, ...] = ()          # remotes Boswas ships a pinned .flatpakrepo file for
    problems: tuple[str, ...] = field(default=())

    def enabled(self, installation: str | None = None) -> list[FlatpakRemote]:
        return [r for r in self.remotes if not r.disabled and (installation is None or r.installation == installation)]
