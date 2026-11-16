"""What Document Viewer remembers: recent files and how each document was last looked at. No Qt.

One JSON file in the state directory. It is bounded (a fixed number of
recent files and of remembered documents, the oldest go first) and
everything read from it is checked, because it is a file like any other.
"""

from __future__ import annotations

import json
import os
import tempfile
import time

MAX_RECENT = 24
MAX_DOCUMENTS = 200
_MAX_FILE = 2 * 1024 * 1024
_MODES = ("fit-width", "fit-page", "custom")


def _clean_view(raw: object) -> dict | None:
    if not isinstance(raw, dict):
        return None
    view: dict = {}
    page = raw.get("page")
    view["page"] = page if isinstance(page, int) and not isinstance(page, bool) and 0 <= page < 10_000_000 else 0
    zoom = raw.get("zoom")
    view["zoom"] = float(zoom) if isinstance(zoom, (int, float)) and not isinstance(zoom, bool) and 0.05 <= zoom <= 20 \
        else 1.0
    view["mode"] = raw.get("mode") if raw.get("mode") in _MODES else "fit-width"
    rotation = raw.get("rotation")
    view["rotation"] = rotation % 4 if isinstance(rotation, int) and not isinstance(rotation, bool) else 0
    view["continuous"] = raw.get("continuous") if isinstance(raw.get("continuous"), bool) else True
    view["columns"] = 2 if raw.get("columns") == 2 else 1
    view["cover"] = bool(raw.get("cover")) if isinstance(raw.get("cover"), bool) else False
    offset = raw.get("offset")
    view["offset"] = float(offset) if isinstance(offset, (int, float)) and not isinstance(offset, bool) \
        and 0 <= offset <= 1 else 0.0
    return view


def _clean_path(raw: object) -> str | None:
    if isinstance(raw, str) and raw.startswith("/") and "\0" not in raw and len(raw) < 4096:
        return raw
    return None


class Store:
    def __init__(self, directory: str | os.PathLike | None) -> None:
        self.path = os.path.join(os.fspath(directory), "documents.json") if directory is not None else None
        self.recent: list[dict] = []            # {"path", "title", "pages", "opened"}; newest first
        self.views: dict[str, dict] = {}        # path → view (insertion order: oldest first)
        self.sidebar = True
        self.sidebar_tab = "thumbnails"
        self.load()

    def load(self) -> None:
        if self.path is None:
            return
        try:
            with open(self.path, "rb") as handle:
                raw = handle.read(_MAX_FILE + 1)
            data = json.loads(raw.decode("utf-8")) if len(raw) <= _MAX_FILE else None
        except (OSError, ValueError, UnicodeDecodeError):
            return
        if not isinstance(data, dict):
            return
        recent = data.get("recent")
        if isinstance(recent, list):
            for item in recent[:MAX_RECENT * 2]:
                path = _clean_path(item.get("path")) if isinstance(item, dict) else None
                if path is None or any(entry["path"] == path for entry in self.recent):
                    continue
                title = item.get("title")
                pages = item.get("pages")
                opened = item.get("opened")
                self.recent.append({
                    "path": path,
                    "title": title[:300] if isinstance(title, str) else "",
                    "pages": pages if isinstance(pages, int) and not isinstance(pages, bool) and 0 <= pages < 10 ** 7
                    else 0,
                    "opened": float(opened) if isinstance(opened, (int, float)) and not isinstance(opened, bool)
                    else 0.0})
            del self.recent[MAX_RECENT:]
        views = data.get("views")
        if isinstance(views, dict):
            for path, raw_view in list(views.items())[-MAX_DOCUMENTS:]:
                view = _clean_view(raw_view)
                if _clean_path(path) is not None and view is not None:
                    self.views[path] = view
        if isinstance(data.get("sidebar"), bool):
            self.sidebar = data["sidebar"]
        if data.get("sidebar_tab") in ("thumbnails", "outline", "results"):
            self.sidebar_tab = data["sidebar_tab"]

    def save(self) -> None:
        if self.path is None:
            return
        data = {"recent": self.recent[:MAX_RECENT], "views": self.views, "sidebar": self.sidebar,
                "sidebar_tab": self.sidebar_tab}
        try:
            descriptor, temporary = tempfile.mkstemp(prefix=".documents-", dir=os.path.dirname(self.path))
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False)
            os.replace(temporary, self.path)
        except (OSError, ValueError):
            pass

    def add_recent(self, path: str, title: str = "", pages: int = 0) -> None:
        path = os.path.abspath(path)
        self.recent = [entry for entry in self.recent if entry["path"] != path]
        self.recent.insert(0, {"path": path, "title": title[:300], "pages": int(pages), "opened": time.time()})
        del self.recent[MAX_RECENT:]

    def forget(self, path: str) -> None:
        self.recent = [entry for entry in self.recent if entry["path"] != path]
        self.views.pop(path, None)

    def view(self, path: str) -> dict | None:
        found = self.views.get(os.path.abspath(path))
        return dict(found) if found is not None else None

    def set_view(self, path: str, view: dict) -> None:
        cleaned = _clean_view(view)
        if cleaned is None:
            return
        path = os.path.abspath(path)
        self.views.pop(path, None)                  # moves it to the end: the most recently used
        self.views[path] = cleaned
        while len(self.views) > MAX_DOCUMENTS:
            del self.views[next(iter(self.views))]
