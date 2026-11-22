"""Personalization > Fonts: the installed font families, a preview, adding and removing the user's fonts.

  Add fonts      a .ttf, .otf or .ttc file the person chooses is checked by
                 its header and size, copied into the user's font folder
                 (userfiles.install_font) and fontconfig's cache of that
                 folder is refreshed (`fc-cache -f <folder>`); it is for this
                 account only
  Fonts          the families of `fc-list` with a search box; the chosen one
                 is previewed at a size the person picks, with its styles;
                 a family the user installed can be removed after a question
                 (its files in the user's font folder are deleted); fonts of
                 the system come with packages and are not removed here

A font installed while Control Center runs is previewed by loading its file
into this window only (QFontDatabase.addApplicationFont); nothing else is
written.
"""

from __future__ import annotations

import boswas_fluent as fluent
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import QComboBox, QListWidget, QListWidgetItem, QWidget

from ... import commands, fonts, userfiles
from ...errors import Failed
from . import context
from .system import CHANGE_TIMEOUT, READ_TIMEOUT, FilesSection, tool_failure
from .widgets import ask, window_of


class FontsSection(FilesSection):
    page = "fonts"
    title = "Fonts"
    order = 10
    keywords = ("font", "fonts", "typeface", "install font", "add font", "ttf", "otf", "preview")

    def __init__(self, parent: QWidget | None = None, *, environ=None, runner=None):
        super().__init__(parent, environ=environ)
        self.runner = runner or context.runner()
        self.install_button = fluent.Button("Install a Font…", self, accent=True)
        self.install_button.setAccessibleName("Install a font from a file")
        self.install_button.clicked.connect(self.choose)
        self.add(fluent.SettingsCard("Add fonts", "TrueType and OpenType files (.ttf, .otf, .ttc), for your account",
                                     fluent.icon("preferences-desktop-font", "font-x-generic"), self.install_button,
                                     self))
        self.search = fluent.SearchBox(self, placeholder="Search fonts")
        self.search.setAccessibleName("Search fonts")
        self.search.textChanged.connect(self.populate)
        self.add(self.search)
        self.count = self.add(fluent.Label("", "caption", self, tone="secondary"))
        self.list = QListWidget(self)
        self.list.setAccessibleName("Font families")
        self.list.setMinimumHeight(220)
        self.list.currentItemChanged.connect(lambda *_a: self.preview())
        self.add(self.list)

        self.preview_card = self.add(fluent.Card(self, title="Preview", padding=16, spacing=10))
        self.size = QComboBox(self.preview_card)
        for size in fonts.SIZES:
            self.size.addItem(f"{size} pt", size)
        self.size.setCurrentIndex(fonts.SIZES.index(36))
        self.size.setAccessibleName("Preview size")
        self.size.currentIndexChanged.connect(lambda _i: self.preview())
        self.sample = fluent.Label(fonts.SAMPLE, "body", self.preview_card, wrap=True)
        self.sample.setAccessibleName("Preview text")
        self.styles = fluent.Label("", "caption", self.preview_card, tone="secondary", wrap=True)
        self.remove_button = fluent.Button("Uninstall", self.preview_card)
        self.remove_button.clicked.connect(self.remove)
        size_row = fluent.SettingsCard("Size", "", None, self.size, self.preview_card)
        self.preview_card.add(size_row)
        self.preview_card.add(self.sample)
        self.preview_card.add(self.styles)
        self.preview_card.add(self.remove_button)
        self.preview_card.body.setAlignment(self.remove_button, Qt.AlignmentFlag.AlignLeft)
        self.families: list[fonts.Family] = []
        self.loaded: set[str] = set()
        self.install_button.setEnabled(False)

    def watched(self) -> list[str]:
        return []

    def activate(self) -> None:
        super().activate()
        self._poll.stop()

    # --- reading ---
    def read(self) -> list[fonts.Family]:
        if not self.runner.available(commands.EXECUTABLES["fc-list"]):
            raise Failed("fontconfig's fc-list is not installed (package fontconfig).")
        result = self.runner.run(commands.argv("fc-list", commands.FONT_LIST_FORMAT), READ_TIMEOUT)
        if result.code != 0:
            raise tool_failure("Listing the fonts", result)
        return fonts.parse_fc_list(result.stdout, userfiles.font_dir(self.environ))

    def show_state(self, families: list[fonts.Family]) -> None:
        self.families = families
        self.show_notice("")
        self.populate()
        self.install_button.setEnabled(not self.busy and window_of(self) is not None)

    def load_failed(self, exc: Exception) -> None:
        self.show_notice(f"The fonts cannot be listed: {exc}", "warning")
        self.install_button.setEnabled(False)

    def set_enabled(self, enabled: bool) -> None:
        self.install_button.setEnabled(enabled and window_of(self) is not None)
        self.remove_button.setEnabled(enabled)

    def populate(self, _text: str = "") -> None:
        chosen = self.chosen()
        shown = fonts.search(self.families, self.search.text())
        self.list.blockSignals(True)
        self.list.clear()
        for family in shown:
            item = QListWidgetItem(family.name)
            item.setData(Qt.ItemDataRole.UserRole, family.name)
            item.setToolTip(family.summary)
            self.list.addItem(item)
            if chosen is not None and family.name == chosen.name:
                self.list.setCurrentItem(item)
        if self.list.currentItem() is None and self.list.count():
            self.list.setCurrentRow(0)
        self.list.blockSignals(False)
        total = len(self.families)
        self.count.setText(f"{len(shown)} of {total} font families" if self.search.text() else
                           f"{total} font families")
        self.preview()

    def chosen(self) -> fonts.Family | None:
        item = self.list.currentItem()
        name = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        return next((f for f in self.families if f.name == name), None)

    def preview(self) -> None:
        family = self.chosen()
        self.preview_card.setVisible(family is not None)
        if family is None:
            return
        if family.name not in QFontDatabase.families() and family.files and family.files[0].path not in self.loaded:
            self.loaded.add(family.files[0].path)
            QFontDatabase.addApplicationFont(family.files[0].path)
        font = QFont(family.name)
        font.setPointSize(int(self.size.currentData() or 36))
        self.sample.setFont(font)
        self.preview_card.title_label.setText(family.name)
        shown = family.name in QFontDatabase.families()
        self.styles.setText(f"{family.summary} · Styles: {', '.join(family.styles) or 'Regular'}"
                            + ("" if shown else " · This window cannot load this font: the preview uses a similar "
                                                "one."))
        self.remove_button.setVisible(family.user)
        self.remove_button.setAccessibleName(f"Uninstall {family.name}")
        self.remove_button.setEnabled(not self.busy)

    # --- changes ---
    def choose(self) -> None:
        window = window_of(self)
        if window is None:
            return
        path = window.choose_file("Choose a font", "Fonts (*.ttf *.otf *.ttc *.TTF *.OTF *.TTC);;All files (*)")
        if path:
            self.install(path)

    def _refresh_cache(self) -> None:
        if self.runner.available(commands.EXECUTABLES["fc-cache"]):
            result = self.runner.run(commands.fc_cache_argv(userfiles.font_dir(self.environ)), CHANGE_TIMEOUT)
            if result.code != 0:
                raise tool_failure("Refreshing the font cache", result)

    def install(self, path: str) -> None:
        environ = self.environ
        installed = []

        def apply() -> None:
            try:
                installed.append(userfiles.install_font(environ, path))
            except userfiles.UserFileError as exc:
                raise Failed(str(exc)) from None
            self._refresh_cache()

        self.change(apply, "The font was installed for your account. Programs that are open may need a restart "
                           "to offer it.")

    def remove(self) -> None:
        family = self.chosen()
        if family is None or not family.user:
            return
        files = [f.path for f in family.files]
        if not ask(self, f"Uninstall {family.name}?", f"{len(files)} font file{'s' if len(files) != 1 else ''} of "
                   "this family are deleted from your font folder. Documents that use it show another font.",
                   "Uninstall"):
            return
        environ = self.environ

        def apply() -> None:
            try:
                for path in files:
                    userfiles.remove_font(environ, path)
            except userfiles.UserFileError as exc:
                raise Failed(str(exc)) from None
            self._refresh_cache()

        self.change(apply, f"{family.name} was uninstalled.")


SECTIONS = (FontsSection,)
