"""What an archive contains: Entry, Listing (the folder tree), and sizes, dates and types as text. No Qt."""

from __future__ import annotations

import stat
import time
from dataclasses import dataclass, field

from . import names

FILE, DIR, SYMLINK, HARDLINK, DEVICE, FIFO, OTHER = "file", "dir", "symlink", "hardlink", "device", "fifo", "other"


@dataclass(slots=True)
class Entry:
    """One member of an archive.

    ``name`` is the name as stored (decoded); ``path`` is where the entry sits
    in the tree the window shows: components joined by ``/``, without empty
    or ``.`` components and without a leading ``/`` (a ``..`` component is
    kept, so the user sees what the archive really holds).
    """

    index: int
    name: str
    path: str
    kind: str = FILE
    size: int = 0                  # bytes when extracted; -1 when the format does not say
    packed: int = -1               # bytes inside the archive; -1 when the format does not say
    mtime: float | None = None     # seconds since the epoch
    mode: int | None = None        # permission bits as stored (may hold set-uid and friends)
    link: str = ""                 # target of a symbolic or hard link
    crc: int | None = None
    encrypted: bool = False
    method: str = ""
    synthetic: bool = False        # a folder the archive does not list but its entries imply
    handle: object = None          # the back end's own reference (ZipInfo, member number, ...)

    @property
    def is_dir(self) -> bool:
        return self.kind == DIR

    @property
    def base(self) -> str:
        return self.path.rpartition("/")[2]

    @property
    def parent(self) -> str:
        return self.path.rpartition("/")[0]


@dataclass
class Totals:
    files: int = 0
    folders: int = 0
    size: int = 0                  # of entries whose size is known
    packed: int = 0
    unknown_sizes: int = 0
    encrypted: int = 0


@dataclass
class Listing:
    """The entries of an archive and the folder tree they form.

    ``children[folder_path]`` lists the entries directly inside a folder
    (``""`` is the root). Folders that no entry names are added as synthetic
    entries. Two entries with the same path both stay in the list.
    """

    entries: list[Entry] = field(default_factory=list)
    children: dict[str, list[Entry]] = field(default_factory=dict)
    totals: Totals = field(default_factory=Totals)

    @classmethod
    def build(cls, entries: list[Entry]) -> "Listing":
        listing = cls(entries=list(entries))
        children: dict[str, list[Entry]] = {"": []}
        folders: dict[str, Entry] = {}
        slots: dict[str, int] = {}          # where an implied folder sits in its parent's list
        totals = listing.totals
        for entry in entries:
            if entry.kind == DIR:
                totals.folders += 1
            else:
                totals.files += 1
                if entry.size >= 0:
                    totals.size += entry.size
                else:
                    totals.unknown_sizes += 1
                if entry.packed >= 0:
                    totals.packed += entry.packed
            if entry.encrypted:
                totals.encrypted += 1
            if not entry.path:
                continue                    # "./" and "/": the root itself, not something inside it
            if entry.kind == DIR:
                existing = folders.get(entry.path)
                if existing is not None:
                    if existing.synthetic:
                        # Named after its contents were listed: the real entry replaces the implied one.
                        children[existing.parent][slots.pop(entry.path)] = entry
                        folders[entry.path] = entry
                    continue                # a folder listed twice is shown once
                folders[entry.path] = entry
                children.setdefault(entry.path, [])
            parent = entry.parent
            if parent not in children:
                listing._imply(parent, children, folders, slots)
            children[parent].append(entry)
        listing.children = children
        return listing

    @staticmethod
    def _imply(path: str, children: dict[str, list[Entry]], folders: dict[str, Entry], slots: dict[str, int]) -> None:
        missing = []
        while path not in children:
            missing.append(path)
            path = path.rpartition("/")[0]
        for folder in reversed(missing):
            implied = Entry(index=-1, name=folder + "/", path=folder, kind=DIR, size=0, synthetic=True)
            folders[folder] = implied
            children[folder] = []
            siblings = children[implied.parent]
            slots[folder] = len(siblings)
            siblings.append(implied)

    def folder(self, path: str) -> list[Entry]:
        """The entries directly inside the folder ``path`` (empty for an unknown folder)."""
        return self.children.get(path, [])

    def has_folder(self, path: str) -> bool:
        return path in self.children

    def descendants(self, entry: Entry) -> list[Entry]:
        """``entry`` and, for a folder, every real entry below it."""
        if entry.kind != DIR:
            return [entry]
        found = [] if entry.synthetic else [entry]
        stack = [entry.path]
        while stack:
            for child in self.children.get(stack.pop(), []):
                if child.kind == DIR:
                    stack.append(child.path)
                    if child.synthetic:
                        continue
                found.append(child)
        return found

    def folder_size(self, path: str) -> int:
        """The known sizes of all files below a folder, added up."""
        total = 0
        stack = [path]
        while stack:
            for child in self.children.get(stack.pop(), []):
                if child.kind == DIR:
                    stack.append(child.path)
                elif child.size > 0:
                    total += child.size
        return total

    def search(self, needle: str, limit: int = 50_000) -> list[Entry]:
        """Entries whose own name contains ``needle`` (case-insensitive), in archive order."""
        needle = needle.casefold()
        if not needle:
            return []
        found = []
        for entries in self.children.values():
            for entry in entries:
                if needle in entry.base.casefold():
                    found.append(entry)
                    if len(found) >= limit:
                        return found
        return found


# --- text --------------------------------------------------------------------------------------------------

_UNITS = ("bytes", "KB", "MB", "GB", "TB", "PB")


def human_size(size: int | None) -> str:
    """``1536`` → ``1.5 KB`` (binary steps, as the Files application shows sizes); unknown → an em dash."""
    if size is None or size < 0:
        return "—"
    if size < 1024:
        return "1 byte" if size == 1 else f"{size} bytes"
    value = float(size)
    for unit in _UNITS[1:]:
        value /= 1024
        if value < 1024 or unit == _UNITS[-1]:
            return f"{value:.0f} {unit}" if value >= 100 else f"{value:.1f} {unit}"
    return f"{size} bytes"


def ratio_text(size: int, packed: int) -> str:
    """How much smaller the packed data is, as a percentage of the original size."""
    if size <= 0 or packed < 0:
        return "—"
    return f"{max(0.0, min(100.0, 100.0 * packed / size)):.0f} %"


def time_text(mtime: float | None) -> str:
    if mtime is None:
        return ""
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(mtime))
    except (OverflowError, OSError, ValueError):
        return ""


def mode_text(entry: Entry) -> str:
    """``drwxr-xr-x`` for an entry whose permission bits are known."""
    if entry.mode is None:
        return ""
    kind = {DIR: stat.S_IFDIR, SYMLINK: stat.S_IFLNK, FIFO: stat.S_IFIFO, DEVICE: stat.S_IFCHR}.get(entry.kind,
                                                                                                 stat.S_IFREG)
    return stat.filemode(kind | (entry.mode & 0o7777))


def crc_text(entry: Entry) -> str:
    return f"{entry.crc:08X}" if entry.crc is not None and entry.kind == FILE else ""


_TYPES = {
    "txt": "Text document", "md": "Markdown document", "log": "Log file", "csv": "CSV table",
    "json": "JSON file", "xml": "XML file", "yaml": "YAML file", "yml": "YAML file", "toml": "TOML file",
    "ini": "Settings file", "conf": "Settings file", "cfg": "Settings file",
    "html": "Web page", "htm": "Web page", "css": "Style sheet", "js": "JavaScript file",
    "py": "Python script", "sh": "Shell script", "c": "C source", "h": "C header", "cpp": "C++ source",
    "rs": "Rust source", "go": "Go source", "java": "Java source", "class": "Java class",
    "png": "PNG image", "jpg": "JPEG image", "jpeg": "JPEG image", "gif": "GIF image", "bmp": "Bitmap image",
    "svg": "SVG image", "webp": "WebP image", "ico": "Icon", "tif": "TIFF image", "tiff": "TIFF image",
    "mp3": "MP3 audio", "flac": "FLAC audio", "ogg": "Ogg audio", "wav": "WAV audio", "m4a": "AAC audio",
    "mp4": "MP4 video", "mkv": "Matroska video", "webm": "WebM video", "avi": "AVI video", "mov": "QuickTime video",
    "pdf": "PDF document", "epub": "E-book", "odt": "Text document (ODF)", "ods": "Spreadsheet (ODF)",
    "odp": "Presentation (ODF)", "docx": "Word document", "xlsx": "Excel workbook", "pptx": "PowerPoint presentation",
    "doc": "Word document", "xls": "Excel workbook", "ppt": "PowerPoint presentation", "rtf": "Rich text document",
    "zip": "ZIP archive", "tar": "TAR archive", "gz": "Gzip archive", "bz2": "Bzip2 archive", "xz": "XZ archive",
    "zst": "Zstandard archive", "7z": "7z archive", "rar": "RAR archive", "jar": "Java archive",
    "apk": "Android package", "deb": "Debian package", "rpm": "RPM package", "iso": "Disc image",
    "exe": "Windows program", "dll": "Windows library", "msi": "Windows installer", "so": "Shared library",
    "ttf": "TrueType font", "otf": "OpenType font", "woff": "Web font", "woff2": "Web font",
    "desktop": "Desktop entry", "dex": "Android code", "sql": "SQL script", "db": "Database",
}
_KIND_TYPES = {DIR: "Folder", SYMLINK: "Symbolic link", HARDLINK: "Hard link", DEVICE: "Device", FIFO: "Pipe",
               OTHER: "Special file"}

_ICON_GROUPS = (
    ("text-plain", "txt md log csv ini conf cfg toml yaml yml"),
    ("application-json", "json"),
    ("text-html", "html htm xml css"),
    ("text-x-script", "py sh js c h cpp rs go java sql pl rb php ts"),
    ("image-x-generic", "png jpg jpeg gif bmp svg webp ico tif tiff heic avif"),
    ("audio-x-generic", "mp3 flac ogg wav m4a opus aac wma"),
    ("video-x-generic", "mp4 mkv webm avi mov wmv m4v"),
    ("application-pdf", "pdf"),
    ("x-office-document", "odt doc docx rtf epub"),
    ("x-office-spreadsheet", "ods xls xlsx"),
    ("x-office-presentation", "odp ppt pptx"),
    ("application-zip", "zip jar"),
    ("application-x-7z-compressed", "7z rar"),
    ("application-x-compressed-tar", "gz bz2 xz zst tgz txz tbz2"),
    ("application-x-tar", "tar"),
    ("application-vnd.android.package-archive", "apk"),
    ("application-x-deb", "deb"),
    ("package-x-generic", "rpm iso"),
    ("application-x-msdownload", "exe dll"),
    ("application-x-msi", "msi"),
    ("application-x-sharedlib", "so"),
    ("font-x-generic", "ttf otf woff woff2"),
)
_ICONS = {extension: icon for icon, extensions in _ICON_GROUPS for extension in extensions.split()}


def extension(name: str) -> str:
    base = name.rpartition("/")[2]
    dot = base.rfind(".")
    return base[dot + 1:].lower() if dot > 0 else ""


def type_text(entry: Entry) -> str:
    """What kind of thing an entry is, in words, from its kind and its extension."""
    if entry.kind != FILE:
        return _KIND_TYPES.get(entry.kind, "File")
    suffix = extension(entry.path)
    if suffix in _TYPES:
        return _TYPES[suffix]
    if suffix and len(suffix) <= 8 and suffix.isalnum():
        return f"{suffix.upper()} file"
    return "File"


def icon_name(entry: Entry) -> str:
    """The freedesktop icon name for an entry (all names exist in the Boswas Prism pack)."""
    if entry.kind == DIR:
        return "folder"
    if entry.kind != FILE:
        return "unknown"
    suffix = extension(entry.path)
    if suffix in _ICONS:
        return _ICONS[suffix]
    if entry.mode is not None and entry.mode & 0o111:
        return "application-x-executable"
    return "unknown" if suffix else "text-x-generic"


def make_entry(index: int, name: str, kind: str, **fields) -> Entry:
    """An Entry whose tree path is derived from its stored name."""
    return Entry(index=index, name=name, path="/".join(names.split(name)), kind=kind, **fields)
