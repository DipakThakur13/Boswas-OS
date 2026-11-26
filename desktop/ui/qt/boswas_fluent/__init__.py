"""boswas_fluent: the Fluent look and the shared widgets of Boswas OS applications (PySide6).

    import boswas_fluent as fluent

Everything public is an attribute of this package and is loaded when first
used, so ``import boswas_fluent`` is cheap and the token and style sheet
modules work without Qt. ``desktop/ui/qt/README.md`` is the reference.

Modules
  tokens      the design tokens as a Theme: colours, radii, type, motion (no Qt)
  appearance  the desktop's scheme and reduced-motion setting (no Qt)
  qss, assets the generated style sheet and its generated images
  theming     apply(), theme(), the ThemeManager, fonts, durations
  animation   Animation: values that move at the refresh rate of the screen
  polish      popups with round corners, the keyboard focus ring
  icons       icon(): Boswas Prism icons, symbolic ones in the text colour
  paint       painting helpers for custom widgets
  widgets.*   the widgets
  app         run(), SingleInstance
  worker      Worker, run_in_thread
  desktop     directories, open_url, open_path, notify
  shortcuts   standard shortcuts
  testing     helpers for unittest
  gallery     python3 -m boswas_fluent.gallery
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

__version__ = "1.0"

_EXPORTS = {
    # tokens (no Qt)
    "Theme": "tokens", "TokenError": "tokens", "TypeStyle": "tokens", "contrast": "tokens", "blend": "tokens",
    "load_tokens": "tokens", "tokens_path": "tokens",
    "Appearance": "appearance", "read_appearance": "appearance", "appearance_path": "appearance",
    # theme
    "apply": "theming", "manager": "theming", "ThemeManager": "theming", "font": "theming", "duration": "theming",
    "easing": "theming", "qcolor": "theming", "repolish": "theming", "ensure_applied": "theming",
    "theme": "theming",
    # motion
    "Animation": "animation", "refresh_rate": "animation",
    # icons
    "icon": "icons", "glyph_icon": "icons", "icon_path": "icons", "has_icon": "icons", "pixmap": "icons",
    "missing_icons": "icons",
    # widgets
    "Label": "widgets.basic", "IconWidget": "widgets.basic", "StatusIcon": "widgets.basic",
    "Separator": "widgets.basic", "Button": "widgets.basic", "IconButton": "widgets.basic",
    "HyperlinkButton": "widgets.basic", "ToggleSwitch": "widgets.basic", "ProgressRing": "widgets.basic",
    "Badge": "widgets.basic", "InfoBadge": "widgets.basic", "TagChip": "widgets.basic", "Avatar": "widgets.basic",
    "Skeleton": "widgets.basic",
    "SearchBox": "widgets.inputs", "PasswordBox": "widgets.inputs", "NumberBox": "widgets.inputs",
    "Card": "widgets.cards", "SettingsCard": "widgets.cards", "Expander": "widgets.cards",
    "SettingsExpander": "widgets.cards", "InfoBar": "widgets.cards", "ListItem": "widgets.cards",
    "EmptyState": "widgets.cards", "RowButton": "widgets.cards",
    "NavigationView": "widgets.navigation", "NavigationList": "widgets.navigation",
    "Pivot": "widgets.selectors", "SegmentedControl": "widgets.selectors", "BreadcrumbBar": "widgets.selectors",
    "CommandBar": "widgets.bars",
    "ContentDialog": "widgets.dialogs", "Flyout": "widgets.dialogs", "MenuFlyout": "widgets.dialogs",
    "TeachingTip": "widgets.dialogs", "ask": "widgets.dialogs", "message": "widgets.dialogs",
    "FluentWindow": "widgets.window",
    # helpers
    "run": "app", "SingleInstance": "app", "activate_window": "app", "install_signal_handlers": "app",
    "NEW_WINDOW": "app", "split_new_window": "app",
    "Worker": "worker", "run_in_thread": "worker",
    "notify": "desktop", "open_path": "desktop", "open_url": "desktop", "config_dir": "desktop",
    "state_dir": "desktop", "cache_dir": "desktop", "data_dir": "desktop", "runtime_dir": "desktop",
    "add_shortcut": "shortcuts", "add_shortcuts": "shortcuts",
}
_MODULES = ("tokens", "appearance", "qss", "assets", "theming", "animation", "polish", "icons", "paint", "app",
            "worker", "desktop", "shortcuts", "testing", "gallery", "widgets")

__all__ = sorted([*_EXPORTS, "__version__"])


def __getattr__(name: str):
    module = _EXPORTS.get(name)
    if module is not None:
        value = getattr(importlib.import_module(f"{__name__}.{module}"), name)
        globals()[name] = value
        return value
    if name in _MODULES:
        return importlib.import_module(f"{__name__}.{name}")
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__) | set(_MODULES))


if TYPE_CHECKING:                 # for editors and type checkers; nothing is imported at run time
    from .animation import Animation, refresh_rate
    from .app import NEW_WINDOW, SingleInstance, activate_window, install_signal_handlers, run, split_new_window
    from .appearance import Appearance, appearance_path, read_appearance
    from .desktop import cache_dir, config_dir, data_dir, notify, open_path, open_url, runtime_dir, state_dir
    from .icons import glyph_icon, has_icon, icon, icon_path, missing_icons, pixmap
    from .shortcuts import add_shortcut, add_shortcuts
    from .theming import ThemeManager, apply, duration, easing, ensure_applied, font, manager, qcolor, repolish
    from .tokens import Theme, TokenError, TypeStyle, blend, contrast, load_tokens, tokens_path
    from .widgets.bars import CommandBar
    from .widgets.basic import (Avatar, Badge, Button, HyperlinkButton, IconButton, IconWidget, InfoBadge, Label,
                                ProgressRing, Separator, Skeleton, StatusIcon, TagChip, ToggleSwitch)
    from .widgets.cards import (Card, EmptyState, Expander, InfoBar, ListItem, RowButton, SettingsCard,
                                SettingsExpander)
    from .widgets.dialogs import ContentDialog, Flyout, MenuFlyout, TeachingTip, ask, message
    from .widgets.inputs import NumberBox, PasswordBox, SearchBox
    from .widgets.navigation import NavigationList, NavigationView
    from .widgets.selectors import BreadcrumbBar, Pivot, SegmentedControl
    from .widgets.window import FluentWindow
    from .worker import Worker, run_in_thread
