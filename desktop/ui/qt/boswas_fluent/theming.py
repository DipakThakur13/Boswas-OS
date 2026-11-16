"""Applying the theme to a Qt application and keeping it in step with the desktop.

``apply(app)`` gives an application the Boswas look: the Fusion style as a
neutral base, a palette, the generated style sheet, the interface font and
the icon theme, all from the tokens. It then follows the desktop: when the
``[appearance]`` table of the user's desktop configuration changes (see
``appearance.py``), the scheme is applied again to every open window and
``manager().changed`` is emitted.

``manager()`` is the one ``ThemeManager`` of the process; ``theme()`` its
current ``Theme``. Widgets that paint themselves ask ``theme()`` each time
they paint, so they need no code for a scheme change.
"""

from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import QEasingCurve, QFileSystemWatcher, QObject, QPointF, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QFontDatabase, QIcon, QPalette
from PySide6.QtWidgets import QApplication, QToolTip, QWidget

from . import appearance, assets, qss
from .tokens import SCHEMES, Theme, TokenError, default_tokens

ICON_THEMES = {"dark": "Boswas-Prism", "light": "Boswas-Prism-Light"}
FALLBACK_ICON_THEMES = {"dark": "breeze-dark", "light": "breeze"}
_WEIGHTS = (100, 200, 300, 400, 500, 600, 700, 800, 900)


def qcolor(name: str, theme: Theme | None = None) -> QColor:
    """A token colour or translucent layer (any name ``Theme.rgba`` accepts) as a QColor."""
    rgb, alpha = (theme or current_theme()).rgba(name)
    colour = QColor(rgb)
    if alpha < 1:
        colour.setAlphaF(alpha)
    return colour


def build_palette(theme: Theme) -> QPalette:
    """The application palette of ``theme`` (what unstyled and third-party widgets draw with)."""
    palette = QPalette()
    roles = QPalette.ColorRole
    normal = {
        roles.Window: "background", roles.WindowText: "text", roles.Base: "surface",
        roles.AlternateBase: "surface-raised", roles.Text: "text", roles.Button: "surface",
        roles.ButtonText: "text", roles.BrightText: "on-accent", roles.Highlight: "accent",
        roles.HighlightedText: "on-accent", roles.Link: "accent", roles.LinkVisited: "accent-pressed",
        roles.ToolTipBase: "tooltip", roles.ToolTipText: "text", roles.PlaceholderText: "text-muted",
        roles.Light: "surface-hover", roles.Midlight: "surface-raised", roles.Mid: "border",
        roles.Dark: "chrome", roles.Shadow: "black",
    }
    if hasattr(roles, "Accent"):                      # Qt 6.6 and later
        normal[roles.Accent] = "accent"
    for role, name in normal.items():
        palette.setColor(role, QColor(theme.color(name)))
    disabled_text = QColor(theme.over("text-disabled", "background"))
    for role in (roles.WindowText, roles.Text, roles.ButtonText, roles.PlaceholderText):
        palette.setColor(QPalette.ColorGroup.Disabled, role, disabled_text)
    palette.setColor(QPalette.ColorGroup.Disabled, roles.Highlight, QColor(theme.over("accent-disabled")))
    palette.setColor(QPalette.ColorGroup.Disabled, roles.HighlightedText, disabled_text)
    return palette


def build_font(theme: Theme, style: str = "body") -> QFont:
    """The font of one step of the type ramp (pixel sizes: logical pixels, scaled by Qt on dense screens)."""
    step = theme.type(style)
    font = QFont()
    font.setFamilies(list(theme.families))
    font.setPixelSize(step.size)
    font.setWeight(QFont.Weight(min(_WEIGHTS, key=lambda weight: abs(weight - step.weight))))
    return font


class ThemeManager(QObject):
    """The theme of this process. Use ``manager()``; do not create a second one."""

    changed = Signal(object)          # the new Theme, after it has been applied to the application

    def __init__(self) -> None:
        super().__init__()
        self._forced_scheme: str | None = None
        self._forced_reduced: bool | None = None
        self._appearance = appearance.read_appearance()
        self._theme = Theme(self._appearance.scheme)
        self._app: QApplication | None = None
        self._watcher: QFileSystemWatcher | None = None
        self._debounce: QTimer | None = None
        self._polisher: QObject | None = None

    # --- state ---
    @property
    def theme(self) -> Theme:
        return self._theme

    @property
    def scheme(self) -> str:
        return self._theme.scheme

    @property
    def follows_desktop(self) -> bool:
        """False after ``set_scheme("dark" or "light")``; True again after ``set_scheme(None)``."""
        return self._forced_scheme is None

    @property
    def reduced_motion(self) -> bool:
        if self._forced_reduced is not None:
            return self._forced_reduced
        return self._appearance.reduced_motion

    @property
    def applied(self) -> bool:
        """True once ``apply`` has styled the running QApplication."""
        return self._app is not None and self._app is QApplication.instance()

    def font_family(self) -> str:
        """The first family of the token list that is installed (the last one if none is)."""
        installed = set(QFontDatabase.families())
        for family in self._theme.families:
            if family in installed:
                return family
        return self._theme.families[-1]

    # --- changes ---
    def set_scheme(self, scheme: str | None) -> None:
        """Use ``scheme`` (``"dark"`` or ``"light"``) whatever the desktop says; ``None`` follows the desktop."""
        if scheme is not None and scheme not in SCHEMES:
            raise TokenError(f"unknown scheme {scheme!r}")
        self._forced_scheme = scheme
        self._update()

    def set_reduced_motion(self, reduced: bool | None) -> None:
        """Turn animations off (True) or on (False) whatever the desktop says; ``None`` follows the desktop."""
        self._forced_reduced = reduced

    def reload(self) -> None:
        """Read the desktop's appearance again and apply it if it changed."""
        self._appearance = appearance.read_appearance()
        self._update()
        self._rewatch()

    def _update(self) -> None:
        scheme = self._forced_scheme or self._appearance.scheme
        tokens = default_tokens()
        if scheme == self._theme.scheme and tokens is self._theme.tokens:
            return
        self._theme = Theme(scheme, tokens)
        if self.applied:
            self._restyle()
        self.changed.emit(self._theme)

    # --- the application ---
    def apply(self, app: QApplication, watch: bool = True) -> None:
        first = self._app is not app
        self._app = app
        if first:
            app.setStyle("Fusion")
            _add_icon_search_paths()
            from .polish import Polisher
            self._polisher = Polisher(app)
            app.aboutToQuit.connect(self._shutdown)
        self._restyle()
        if watch and self._watcher is None:
            self._watcher = QFileSystemWatcher(self)
            self._debounce = QTimer(self)
            self._debounce.setSingleShot(True)
            self._debounce.setInterval(60)
            self._debounce.timeout.connect(self.reload)
            self._watcher.directoryChanged.connect(lambda _path: self._debounce.start())
            self._watcher.fileChanged.connect(lambda _path: self._debounce.start())
            self._rewatch()

    def _restyle(self) -> None:
        app, theme = self._app, self._theme
        if app is None:
            return
        app.setPalette(build_palette(theme))
        QToolTip.setPalette(build_palette(theme))
        app.setFont(build_font(theme, "body"))
        app.setStyleSheet(qss.build(theme, str(assets.ensure(theme))))
        QIcon.setThemeName(ICON_THEMES[theme.scheme])
        QIcon.setFallbackThemeName(FALLBACK_ICON_THEMES[theme.scheme])
        from . import icons
        icons.clear_cache()
        for widget in app.allWidgets():
            # QWidget.update explicitly: on item views PySide6 6.8 resolves
            # widget.update() to QAbstractItemView.update(QModelIndex).
            QWidget.update(widget)

    def _rewatch(self) -> None:
        if self._watcher is None:
            return
        directory = appearance.user_dir()
        wanted = [directory / "appearance.toml", directory / "compositor.toml"]
        # Watch the directory to see files being created or replaced; until it exists, its parent.
        wanted.append(directory if os.path.isdir(directory) else appearance.config_home())
        watched = set(self._watcher.files()) | set(self._watcher.directories())
        present = {str(path) for path in wanted if os.path.exists(path)}
        if watched - present:
            self._watcher.removePaths(sorted(watched - present))
        if present - watched:
            self._watcher.addPaths(sorted(present - watched))

    def _shutdown(self) -> None:
        if self._watcher is not None:
            self._watcher.deleteLater()
            self._watcher = None
        if self._polisher is not None:
            self._polisher.remove()
            self._polisher = None
        self._app = None


def _add_icon_search_paths() -> None:
    paths = list(QIcon.themeSearchPaths())
    wanted = []
    checkout = Path(__file__).resolve().parents[3] / "icons"
    if os.path.isfile(checkout / "Boswas-Prism" / "index.theme"):
        wanted.append(str(checkout))
    data_home = os.environ.get("XDG_DATA_HOME") or ""
    wanted.append(str((Path(data_home) if data_home.startswith("/") else Path.home() / ".local/share") / "icons"))
    for directory in (os.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share").split(":"):
        if directory.startswith("/"):
            wanted.append(str(Path(directory) / "icons"))
    for path in wanted:
        if path not in paths:
            paths.append(path)
    QIcon.setThemeSearchPaths(paths)


_manager: ThemeManager | None = None


def manager() -> ThemeManager:
    """The theme manager of this process (created on first use)."""
    global _manager
    if _manager is None:
        _manager = ThemeManager()
    return _manager


def current_theme() -> Theme:
    """The theme in use now."""
    return manager().theme


theme = current_theme            # public name: fluent.theme()


def apply(app: QApplication | None = None, *, scheme: str | None = None, watch: bool = True) -> ThemeManager:
    """Style ``app`` (default: the running QApplication) and follow the desktop's scheme.

    ``scheme`` fixes the scheme instead of following the desktop. ``watch=False``
    does not watch the configuration files (tests that switch by hand).
    Calling it again is harmless.
    """
    app = app or QApplication.instance()
    if app is None:
        raise RuntimeError("fluent.apply() needs a QApplication")
    themes = manager()
    if scheme is not None:
        themes.set_scheme(scheme)
    themes.apply(app, watch)
    return themes


def ensure_applied() -> None:
    """Apply the theme if a QApplication exists and has not been styled yet (windows call this)."""
    app = QApplication.instance()
    if app is not None and not manager().applied:
        manager().apply(app)


def font(style: str = "body") -> QFont:
    """The font of a step of the type ramp: caption, body, body-strong, body-large, subtitle, title, title-large."""
    return build_font(current_theme(), style)


def duration(name: str = "normal") -> int:
    """A motion duration in milliseconds (fast, normal, slow); 0 when the desktop asks for reduced motion."""
    themes = manager()
    return 0 if themes.reduced_motion else themes.theme.duration(name)


def easing(name: str = "decelerate") -> QEasingCurve:
    """A token easing curve (decelerate, accelerate, standard) for Qt animations."""
    x1, y1, x2, y2 = current_theme().curve(name)
    curve = QEasingCurve(QEasingCurve.Type.BezierSpline)
    curve.addCubicBezierSegment(QPointF(x1, y1), QPointF(x2, y2), QPointF(1.0, 1.0))
    return curve


def repolish(widget: QWidget) -> None:
    """Make the style sheet look at ``widget`` again after one of its dynamic properties changed."""
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)
    QWidget.update(widget)          # not widget.update(): item views shadow it (see _restyle)
