"""The Android page's rules (pure Python, no Qt, no I/O).

Source: boswas-android (docs/architecture/platform-contracts.md, section 2):
``boswas-android --json status``, ``app list`` and ``apk inspect FILE``.
Control Center shows what the runtime manager reports and never claims the
runtime is installed, initialised or running unless the status says so.
Google Play is shown exactly as the runtime manager assesses it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from . import viewmodel as vm

SCHEMA = "boswas-android/1"
CHECK_STATES = {"PASS": vm.SECURE, "WARN": vm.WARNING, "FAIL": vm.ATTENTION, "UNKNOWN": vm.UNAVAILABLE}
GPU_LABELS = {"hardware": "Hardware accelerated", "software": "Software rendering (slower)", "unknown": "Unknown"}
ALTERNATIVE_LABELS = {"fdroid": "F-Droid (open-source app store, not Google Play)",
                      "local-apk": "Install an APK file you trust"}
INSPECTION_NOTE = "Inspection does not prove an app is safe. Install only apps from sources you trust."
NOT_INSTALLED_MANAGER = ("The Android runtime manager (boswas-android) is not installed on this system, so Android "
                         "applications are not available.")
PACKAGE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z][A-Za-z0-9_]*)+$")
SHA256_RE = re.compile(r"^[0-9A-Fa-f]{64}$")


def _dict(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def _bool(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


@dataclass(frozen=True)
class Check:
    id: str
    title: str
    state: str
    detail: str


@dataclass(frozen=True)
class AndroidView:
    summary: vm.Summary
    installed: bool | None
    initialized: bool | None
    service_active: bool | None
    session_running: bool | None
    supported: bool | None
    checks: tuple[Check, ...]
    rows: tuple[tuple[str, str], ...]
    blockers: tuple[str, ...]              # why installing the runtime is not offered
    google_play_reason: str
    alternatives: tuple[str, ...]


def checks_of(doc: object) -> list[Check]:
    checks = []
    for item in _dict(_dict(doc).get("compatibility")).get("checks") or []:
        item = _dict(item)
        check_id = vm.one_line(item.get("id"), 30)
        if not check_id:
            continue
        checks.append(Check(check_id, vm.one_line(item.get("title"), 80) or check_id,
                            CHECK_STATES.get(item.get("status"), vm.UNAVAILABLE),
                            vm.sentence(item.get("detail")) or "No details reported."))
    return checks


def android_view(doc: object, error: str | None = None, unavailable: bool = False,
                 loading: bool = False) -> AndroidView:
    """The Android page from `boswas-android --json status` (or why it is not available)."""
    if not isinstance(doc, dict):
        if loading and not error:
            summary = vm.Summary(vm.CHECKING, "Checking the Android runtime…", "")
        elif unavailable:
            summary = vm.Summary(vm.UNAVAILABLE, "Android applications are not available", NOT_INSTALLED_MANAGER)
        else:
            summary = vm.Summary(vm.ERROR, "The Android runtime status could not be read",
                                 vm.one_line(error, 300) if error else "")
        return AndroidView(summary, None, None, None, None, None, (), (), (summary.detail,) if not loading else (),
                           "", ())
    runtime, compat = _dict(doc.get("runtime")), _dict(doc.get("compatibility"))
    installed = _bool(runtime.get("installed"))
    initialized = _bool(doc.get("initialized"))
    service = _bool(_dict(doc.get("service")).get("active"))
    session = _bool(_dict(doc.get("session")).get("running"))
    supported = _bool(compat.get("supported"))
    checks = checks_of(doc)
    failing = [c for c in checks if c.state == vm.ATTENTION]
    blockers: list[str] = []
    if supported is False:
        blockers = [f"{c.title}: {c.detail}" for c in failing] or ["This device does not meet the Android runtime's "
                                                                  "requirements."]
    version = vm.one_line(runtime.get("version"), 40)
    if supported is False and not installed:
        summary = vm.Summary(vm.ATTENTION, "This device cannot run Android applications",
                             "The checks below explain what is missing.")
    elif installed is not True:
        summary = vm.Summary(vm.INFO, "The Android runtime is not installed",
                             "Android applications run in Waydroid, a container with its own Android system. "
                             "Installing it is optional and needs a network connection.")
    elif initialized is not True:
        summary = vm.Summary(vm.WARNING, "The Android runtime is installed but not set up",
                             "Initialize it to download the Android system images (about 1 GB).")
    elif session:
        summary = vm.Summary(vm.SECURE, "Android is running", "Android applications can be started now.")
    else:
        summary = vm.Summary(vm.INFO, "Android is ready", "Android starts when you open an Android application or "
                                                          "choose Start.")
    images = _dict(doc.get("images"))
    storage = _dict(doc.get("storage"))
    network = _dict(doc.get("network"))
    gpu = _dict(doc.get("gpu"))

    def yes_no(value: bool | None, yes: str, no: str) -> str:
        return yes if value is True else (no if value is False else "Unknown")

    image_text = "Not downloaded"
    if images.get("system") or images.get("vendor"):
        image_text = ", ".join(p for p in (
            f"{vm.one_line(images.get('type'), 20) or 'Android'} system {vm.one_line(images.get('system'), 40)}"
            if images.get("system") else "",
            f"vendor {vm.one_line(images.get('vendor'), 40)}" if images.get("vendor") else "") if p)
    gpu_text = GPU_LABELS.get(gpu.get("acceleration"), "Unknown")
    driver = vm.one_line(gpu.get("driver"), 40)
    if driver:
        gpu_text += f" ({driver})"
    net_text = "Unknown"
    if network.get("present") is True:
        address = vm.one_line(network.get("address"), 60)
        net_text = f"{vm.one_line(network.get('interface'), 20) or 'waydroid0'} up" + (f", {address}" if address else "")
    elif network.get("present") is False:
        net_text = "Not connected (starts with Android)"
    rows = (
        ("Runtime", (f"Waydroid {version}" if version else "Waydroid") if installed else
         yes_no(installed, "Installed", "Not installed")),
        ("Android system", image_text if installed else "Not downloaded"),
        ("Container service", yes_no(service, "Running", "Stopped")),
        ("Android session", yes_no(session, "Running", "Stopped")),
        ("Graphics", gpu_text),
        ("Network", net_text),
        ("Storage used", ", ".join(p for p in (
            f"system images {vm.format_bytes(storage.get('images_bytes'))}"
            if _int(storage.get("images_bytes")) is not None else "",
            f"your Android data {vm.format_bytes(storage.get('user_bytes'))}"
            if _int(storage.get("user_bytes")) is not None else "") if p) or "Unknown"),
    )
    play = _dict(doc.get("google_play"))
    reason = vm.sentence(play.get("reason"), 1200) or "The runtime manager did not report whether Google Play " \
                                                      "can be installed."
    alternatives = tuple(a for a in play.get("alternatives") or [] if a in ALTERNATIVE_LABELS)
    if play.get("available") is True:
        # The contract has no supported Google Play path; never present one the window cannot verify.
        reason = "The runtime manager reports a Google Play option, but this version of Control Center does not " \
                 "offer it."
    return AndroidView(summary, installed, initialized, service, session, supported, tuple(checks), rows,
                       tuple(blockers), reason, alternatives)


# --- installed Android apps -------------------------------------------------------------------------------

@dataclass(frozen=True)
class AndroidApp:
    package: str
    name: str
    version: str


def android_apps(doc: object) -> list[AndroidApp]:
    apps = []
    for item in _dict(doc).get("apps") or []:
        item = _dict(item)
        package = item.get("package")
        if not isinstance(package, str) or not PACKAGE_RE.fullmatch(package) or len(package) > 255:
            continue
        apps.append(AndroidApp(package, vm.one_line(item.get("name"), 120) or package,
                               vm.one_line(item.get("version"), 40)))
    apps.sort(key=lambda a: (a.name.casefold(), a.package))
    return apps


# --- APK inspection ------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class ApkView:
    title: str
    rows: tuple[tuple[str, str], ...]
    problems: tuple[str, ...]
    compatible: bool
    package: str


def apk_view(doc: object) -> ApkView:
    """`boswas-android --json apk inspect FILE` -> what the confirmation dialog shows."""
    apk = _dict(_dict(doc).get("apk"))
    package = apk.get("package") if isinstance(apk.get("package"), str) and PACKAGE_RE.fullmatch(apk["package"]) \
        else ""
    label = vm.one_line(apk.get("label"), 120)
    version = vm.one_line(apk.get("version_name"), 40)
    code = _int(apk.get("version_code"))
    abis = [vm.one_line(a, 20) for a in apk.get("abis") or [] if isinstance(a, str)]
    split = _dict(apk.get("split"))
    signing = _dict(apk.get("signing"))
    certificates = [c.get("sha256") for c in signing.get("certificates") or []
                    if isinstance(c, dict) and isinstance(c.get("sha256"), str) and SHA256_RE.fullmatch(c["sha256"])]
    schemes = [vm.one_line(s, 4) for s in signing.get("schemes") or [] if isinstance(s, str)]
    problems = tuple(vm.sentence(p, 400) for p in apk.get("problems") or [] if isinstance(p, str) and p.strip())
    compatible = apk.get("compatible") is True and not problems
    split_text = "No"
    if split.get("is_split") is True:
        split_text = f"Yes, part “{vm.one_line(split.get('name'), 60)}” of a split app" if split.get("name") \
            else "Yes, part of a split app"
    elif split.get("requires_splits") is True:
        split_text = "Needs additional split parts"
    sdk = ", ".join(p for p in (f"minimum Android API {apk['min_sdk']}" if _int(apk.get("min_sdk")) else "",
                                f"targets API {apk['target_sdk']}" if _int(apk.get("target_sdk")) else "") if p)
    rows = (
        ("Application", label or package or "Unknown"),
        ("Package", package or "Unknown"),
        ("Version", f"{version} ({code})" if version and code is not None else (version or
                                                                                  (str(code) if code is not None
                                                                                   else "Unknown"))),
        ("Native code", ", ".join(abis) if abis else "None (runs on any architecture)"),
        ("Split app", split_text),
        ("Android versions", sdk or "Unknown"),
        ("Signature", ", ".join(schemes) if schemes else "Not signed"),
        ("Signer certificate (SHA-256)", "\n".join(c.lower() for c in certificates) or "None found"),
        ("File size", vm.format_bytes(apk.get("size"))),
        ("File SHA-256", vm.one_line(apk.get("sha256"), 64) or "Unknown"),
    )
    if apk.get("compatible") is False and not problems:
        problems = ("The runtime manager reports that this app cannot run on this device.",)
    return ApkView(label or package or "Android app", rows, problems, compatible, package)


def apk_confirmation(view: ApkView) -> str:
    lines = [f"{label}: {value}" for label, value in view.rows]
    return "\n".join(lines) + "\n\n" + INSPECTION_NOTE
