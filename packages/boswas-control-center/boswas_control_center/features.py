"""Apps > Optional features: optional Debian packages useful on a desktop (data and pure functions, no Qt).

Each feature is a few packages of Debian 13's main archive. The page shows
whether they are installed (`dpkg-query -W`, read-only) and installs or
removes them through the system helper (sysops apt-install / apt-remove,
which simulate first and refuse what Boswas policy excludes or protects).
Language support is offered for the languages whose locales are generated
on this device (`locale -a`): Firefox in that language. Printing is
installed without what its packages only recommend (sysops apt-install
--no-recommends): cups-daemon recommends cups-browsed, which Boswas policy
excludes, and APT would otherwise make the whole install fail.

Names checked against docs/architecture/package-manifest.md where it names
them (7zip, fonts-noto-color-emoji, flatpak, firefox-esr); the others are
Debian 13 package names that could not be checked against the archive from
the build environment (see the change notes).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Feature:
    key: str
    title: str
    description: str
    packages: tuple[str, ...]
    icon: str = "package-x-generic"
    recommends: bool = True               # False: installed with --no-recommends (see the module text)

    def install_words(self, missing: tuple[str, ...]) -> tuple[str, ...]:
        """The sysops arguments that install the ``missing`` packages of this feature."""
        return ("apt-install", *(() if self.recommends else ("--no-recommends",)), *missing)


FEATURES = (
    Feature("codecs", "Extra media codecs", "More audio and video formats for applications that play media through "
            "GStreamer (MP3 encoders, older and less common formats)",
            ("gstreamer1.0-plugins-ugly", "gstreamer1.0-libav"), "applications-multimedia"),
    Feature("printing", "Printing support", "CUPS, the printing system, with driverless printing for most network "
            "and USB printers", ("cups",), "printer", recommends=False),
    Feature("pdf-printer", "Print to PDF", "A printer that saves what you print as a PDF file (needs printing "
            "support)", ("printer-driver-cups-pdf",), "application-pdf", recommends=False),
    Feature("7zip", "7-Zip archives", "Open and create .7z archives in Archive Manager", ("7zip",),
            "application-x-7z-compressed"),
    Feature("cjk-fonts", "Chinese, Japanese and Korean fonts", "Noto fonts for text in Chinese, Japanese and Korean",
            ("fonts-noto-cjk",), "font-x-generic"),
    Feature("emoji", "Colour emoji", "Emoji in colour in every application", ("fonts-noto-color-emoji",),
            "preferences-desktop-emoticons"),
    Feature("office-fonts", "Fonts for office documents", "Carlito and Caladea: documents made elsewhere keep their "
            "layout", ("fonts-crosextra-carlito", "fonts-crosextra-caladea"), "x-office-document"),
    Feature("flatpak", "Flatpak", "Install sandboxed applications from Flatpak remotes your administrator allows",
            ("flatpak",), "boswas-software-center"),
    Feature("developer", "Developer tools", "The compiler, make and the other tools to build software from source",
            ("build-essential",), "utilities-terminal"),
)

# Firefox's language packs: glibc language (or language_TERRITORY) -> the suffix of firefox-esr-l10n-<suffix>.
FIREFOX_LANGUAGES = {
    "ar": "ar", "cs": "cs", "da": "da", "de": "de", "el": "el", "es": "es-es", "fi": "fi", "fr": "fr", "he": "he",
    "hi": "hi-in", "hu": "hu", "it": "it", "ja": "ja", "ko": "ko", "nb": "nb-no", "nl": "nl", "pl": "pl",
    "pt": "pt-pt", "pt_BR": "pt-br", "ro": "ro", "ru": "ru", "sv": "sv-se", "tr": "tr", "uk": "uk",
    "zh_CN": "zh-cn", "zh_TW": "zh-tw",
}
LANGUAGE_NAMES = {
    "ar": "Arabic", "cs": "Czech", "da": "Danish", "de": "German", "el": "Greek", "es": "Spanish", "fi": "Finnish",
    "fr": "French", "he": "Hebrew", "hi": "Hindi", "hu": "Hungarian", "it": "Italian", "ja": "Japanese",
    "ko": "Korean", "nb": "Norwegian Bokmål", "nl": "Dutch", "pl": "Polish", "pt": "Portuguese",
    "pt_BR": "Portuguese (Brazil)", "ro": "Romanian", "ru": "Russian", "sv": "Swedish", "tr": "Turkish",
    "uk": "Ukrainian", "zh_CN": "Chinese (simplified)", "zh_TW": "Chinese (traditional)",
}


def language_features(locales: list[str]) -> list[Feature]:
    """Firefox in each language whose locale is generated (locale -a names such as de_DE.utf8)."""
    found: dict[str, Feature] = {}
    for locale in locales:
        base = locale.split(".")[0].split("@")[0]
        language = base.split("_")[0]
        key = base if base in FIREFOX_LANGUAGES else language
        if key not in FIREFOX_LANGUAGES or key in found:
            continue
        found[key] = Feature(f"firefox-{FIREFOX_LANGUAGES[key]}", f"{LANGUAGE_NAMES[key]} for Firefox",
                             f"Firefox's menus and dialogs in {LANGUAGE_NAMES[key]}",
                             (f"firefox-esr-l10n-{FIREFOX_LANGUAGES[key]}",), "preferences-desktop-locale")
    return sorted(found.values(), key=lambda f: f.title)


def parse_status(text: str) -> dict[str, tuple[str, str, int | None]]:
    """{package: (status abbreviation, version, size in bytes)} from dpkg-query -W with commands.DPKG_STATUS_FORMAT."""
    found = {}
    for line in (text or "").splitlines():
        parts = line.split("\t")
        if len(parts) != 4:
            continue
        package, status, size, version = (part.strip() for part in parts)
        package = package.split(":")[0]
        found[package] = (status, version[:60], int(size) * 1024 if size.isdigit() else None)
    return found


def installed(status: dict, package: str) -> bool:
    """Whether dpkg has the package installed ("ii": wanted and installed; "hi": held and installed)."""
    entry = status.get(package)
    return entry is not None and entry[0][:2] in ("ii", "hi")


def feature_state(feature: Feature, status: dict) -> str:
    """installed, partly or missing."""
    present = [installed(status, package) for package in feature.packages]
    return "installed" if all(present) else "partly" if any(present) else "missing"
