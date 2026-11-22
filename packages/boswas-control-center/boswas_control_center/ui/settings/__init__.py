"""The native settings sections of Control Center, and which page shows which.

A section is a QWidget subclass with four class attributes and two methods:

    class NightLightSection(QWidget):
        page = "display"                 # key of the catalog page it appears on (catalog.PAGES)
        title = "Night light"            # heading above the section's cards
        order = 20                       # position on the page, ascending
        keywords = ("night", "blue light", "warm")   # for the search box
        also = ()                        # optional: more pages that show it too (each its own instance)
        def __init__(self, parent=None): ...          # cheap: build widgets only, no I/O
        def activate(self) -> None: ...   # the page became visible: load state, start watching
        def deactivate(self) -> None: ... # the page was hidden / the window closes: stop timers, child processes

Each module of MODULES exports ``SECTIONS``, a tuple of such classes. The
modules are imported one by one the first time the registry is asked; one
that is missing or fails to import is logged and left out, and the others
still load. A class that does not follow the contract is left out with a
log line as well.

    sections_for("display")  -> the section classes of that page, by order
    all_keywords()           -> {page key: (words the search finds the page by)}
    find("night light")      -> the sections matching every word, best first
    section_id(cls)          -> "night-light" (boswas-control-center --page display --section night-light)
    search_entries()         -> (title, page, command-line name, keywords) of every section, for the search

The modules of the first versions hold the settings that were there before
the window was laid out in categories; the others hold the pages added with
the categories (maintenance: device name, troubleshooters, recovery;
cleanup: storage; peripherals: printers, cameras, USB; netadmin: VPN and
adapters; fonts; apps: installed apps and optional features; permissions:
app permissions; updates: update history and options).
"""

from __future__ import annotations

import logging
import re

from PySide6.QtWidgets import QWidget

from ... import catalog

log = logging.getLogger("boswas_control_center.settings")

# Imported in this order; the order on a page comes from each section's ``order``.
MODULES = ("connectivity", "sound_power", "accounts", "system", "display", "input", "personalize", "maintenance",
           "cleanup", "peripherals", "netadmin", "fonts", "apps", "permissions", "updates", "colors", "taskbar",
           "multitasking", "notifications", "accessibility")

_sections: list[type] | None = None
_problems: dict[str, str] = {}


def _import(name: str):
    """The module ``name`` of this package (each import is written out: nothing is imported by a computed name)."""
    if name == "connectivity":
        from . import connectivity as module
    elif name == "sound_power":
        from . import sound_power as module
    elif name == "accounts":
        from . import accounts as module
    elif name == "system":
        from . import system as module
    elif name == "display":
        from . import display as module
    elif name == "input":
        from . import input as module
    elif name == "personalize":
        from . import personalize as module
    elif name == "maintenance":
        from . import maintenance as module
    elif name == "cleanup":
        from . import cleanup as module
    elif name == "peripherals":
        from . import peripherals as module
    elif name == "netadmin":
        from . import netadmin as module
    elif name == "fonts":
        from . import fonts as module
    elif name == "apps":
        from . import apps as module
    elif name == "permissions":
        from . import permissions as module
    elif name == "updates":
        from . import updates as module
    elif name == "colors":
        from . import colors as module
    elif name == "taskbar":
        from . import taskbar as module
    elif name == "multitasking":
        from . import multitasking as module
    elif name == "notifications":
        from . import notifications as module
    elif name == "accessibility":
        from . import accessibility as module
    else:
        raise ImportError(f"{name} is not a settings module")
    return module


def valid_section(cls: object) -> str | None:
    """None when ``cls`` follows the section contract, else what is wrong with it."""
    if not isinstance(cls, type) or not issubclass(cls, QWidget):
        return "not a QWidget subclass"
    if getattr(cls, "page", None) not in catalog.BY_KEY:
        return f"page {getattr(cls, 'page', None)!r} is not a page of the catalog"
    title = getattr(cls, "title", None)
    if not isinstance(title, str) or not title.strip():
        return "no title"
    order = getattr(cls, "order", None)
    if isinstance(order, bool) or not isinstance(order, (int, float)):
        return "order is not a number"
    keywords = getattr(cls, "keywords", ())
    if isinstance(keywords, str) or not all(isinstance(word, str) for word in keywords):
        return "keywords is not a tuple of words"
    also = getattr(cls, "also", ())
    if isinstance(also, str) or not all(key in catalog.BY_KEY for key in also):
        return "also is not a tuple of pages of the catalog"
    for method in ("activate", "deactivate"):
        if not callable(getattr(cls, method, None)):
            return f"no {method}()"
    return None


def load(force: bool = False) -> list[type]:
    """Every valid section class, in module order (imports the modules on first use)."""
    global _sections
    if _sections is not None and not force:
        return list(_sections)
    found: list[type] = []
    _problems.clear()
    for name in MODULES:
        try:
            module = _import(name)
            declared = tuple(getattr(module, "SECTIONS"))
        except ImportError as exc:
            _problems[name] = f"not available ({exc})"
            log.info("settings module %s is not available: %s", name, exc)
            continue
        except Exception as exc:              # a broken module must not take the window down
            _problems[name] = f"failed to load ({type(exc).__name__}: {exc})"
            log.warning("settings module %s failed to load: %s: %s", name, type(exc).__name__, exc)
            continue
        for cls in declared:
            problem = valid_section(cls)
            if problem is not None:
                _problems[f"{name}.{getattr(cls, '__name__', cls)}"] = problem
                log.warning("settings section %s.%s is left out: %s", name, getattr(cls, "__name__", cls), problem)
            elif cls not in found:
                found.append(cls)
    _sections = found
    return list(found)


def problems() -> dict[str, str]:
    """What load() left out, and why: {module or module.Class: reason}."""
    load()
    return dict(_problems)


def pages_of(cls: type) -> tuple[str, ...]:
    """The pages that show a section: its page, then the pages of ``also``."""
    return tuple(dict.fromkeys((cls.page, *getattr(cls, "also", ()))))


def sections_for(page_key: str) -> list[type]:
    """The section classes of a page, sorted by ``order`` (then by title)."""
    return sorted((cls for cls in load() if page_key in pages_of(cls)), key=lambda cls: (cls.order, cls.title))


def all_keywords() -> dict[str, tuple[str, ...]]:
    """For the search box: per page, the titles and keywords of its sections."""
    words: dict[str, list[str]] = {}
    for cls in load():
        for page in pages_of(cls):
            bucket = words.setdefault(page, [])
            for word in (cls.title, *cls.keywords):
                if word and word not in bucket:
                    bucket.append(word)
    return {page: tuple(found) for page, found in words.items()}


def search_entries() -> list[tuple[str, str, str, tuple[str, ...]]]:
    """(title, page, command-line name, keywords) of every section on each page that shows it."""
    return [(cls.title, page, section_id(cls), tuple(cls.keywords)) for cls in load() for page in pages_of(cls)]


def section_id(cls: type) -> str:
    """A section's name on the command line: its title in lower case with dashes ("Night light" -> "night-light")."""
    return re.sub(r"[^a-z0-9]+", "-", cls.title.casefold()).strip("-")


def find(query: str, page_key: str | None = None) -> list[type]:
    """The sections whose title or keywords contain every word of ``query``, best match first.

    A section whose title matches comes before one that only matches by a
    keyword; within that, the order of the pages in the sidebar and of the
    sections on their page. With ``page_key`` only that page is searched.
    """
    words = query.casefold().split()
    if not words:
        return []
    ranked = []
    for cls in load():
        if page_key is not None and page_key not in pages_of(cls):
            continue
        title = cls.title.casefold()
        haystack = " ".join((title, section_id(cls), *(word.casefold() for word in cls.keywords)))
        if not all(word in haystack for word in words):
            continue
        in_title = all(word in title or word in section_id(cls) for word in words)
        exact = " ".join(words) in (title, section_id(cls))
        ranked.append(((0 if exact else 1 if in_title else 2), catalog.PAGE_KEYS.index(cls.page), cls.order,
                       cls.title, cls))
    ranked.sort(key=lambda entry: entry[:4])
    return [entry[4] for entry in ranked]
