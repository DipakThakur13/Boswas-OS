"""SettingsPage: the settings tab.

Every change takes effect at once in all open tabs and is saved a moment
later; there is no Save button. The page lists the settings of the
application first and then one expander for each profile.
"""

from __future__ import annotations

from dataclasses import replace

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import QComboBox, QFrame, QHBoxLayout, QLineEdit, QScrollArea, QVBoxLayout, QWidget

import boswas_fluent as fluent

from . import schemes
from . import settings as settings_module
from .emulator import MAX_SCROLLBACK
from .view import default_font_family

_CURSORS = (("bar", "Bar"), ("block", "Block"), ("underline", "Underline"))
_BELLS = (("visual", "Flash the terminal"), ("none", "Nothing"))
_CLOSING = (("graceful", "Close the tab when it ended without an error"), ("always", "Always close the tab"),
            ("never", "Keep the tab open"))
_AUTOMATIC = "Automatic"


def _combo(items, current: str, name: str) -> QComboBox:
    box = QComboBox()
    for key, text in items:
        box.addItem(text, key)
    index = box.findData(current)
    box.setCurrentIndex(max(0, index))
    box.setAccessibleName(name)
    box.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
    box.wheelEvent = lambda event: event.ignore()        # scrolling the page must not change a setting
    return box


def monospace_families() -> list[str]:
    """Installed fixed-pitch font families, the ones of the default list first."""
    families = [family for family in QFontDatabase.families()
                if QFontDatabase.isFixedPitch(family) and not QFontDatabase.isPrivateFamily(family)]
    preferred = [family for family in settings_module.FONT_CHOICES if family in families]
    return preferred + sorted(family for family in families if family not in preferred)


class SettingsPage(QWidget):
    def __init__(self, store, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.store = store
        self.profile_cards: dict[str, fluent.SettingsExpander] = {}
        self.controls: dict[str, QWidget] = {}
        self._expanded: set[str] = set()
        self._building = False
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea(self)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll = scroll
        outer.addWidget(scroll)
        content = QWidget()
        self.column = QVBoxLayout(content)
        self.column.setContentsMargins(32, 20, 32, 32)
        self.column.setSpacing(4)
        scroll.setWidget(content)
        self.setFocusProxy(scroll)
        self.column.addWidget(fluent.Label("Settings", "title", content))
        self.message = fluent.InfoBar("warning", parent=content)
        self.message.hide()
        self.column.addWidget(self.message)
        self.general = QWidget(content)
        self.general_layout = QVBoxLayout(self.general)
        self.general_layout.setContentsMargins(0, 0, 0, 0)
        self.general_layout.setSpacing(4)
        self.column.addWidget(self.general)
        self.profiles_box = QWidget(content)
        self.profiles_layout = QVBoxLayout(self.profiles_box)
        self.profiles_layout.setContentsMargins(0, 0, 0, 0)
        self.profiles_layout.setSpacing(4)
        self.column.addWidget(self.profiles_box)
        self.column.addStretch(1)
        self._build_general()
        self._build_profiles()
        if store.error:
            self.message.show_message("warning", store.error)
        store.save_failed.connect(lambda text: self.message.show_message("error", text))

    # --- helpers ---
    def _section(self, layout: QVBoxLayout, text: str) -> None:
        label = fluent.Label(text, "body-strong")
        label.setContentsMargins(0, 20, 0, 4)
        layout.addWidget(label)

    def _changed(self) -> None:
        if not self._building:
            self.store.update()

    def _toggle(self, key: str, title: str, description: str, icon: str | None = None) -> None:
        switch = fluent.ToggleSwitch(checked=bool(getattr(self.store.settings, key)))

        def set_value(on: bool) -> None:
            setattr(self.store.settings, key, bool(on))
            self._changed()

        switch.toggled.connect(set_value)
        self.controls[key] = switch
        self.general_layout.addWidget(fluent.SettingsCard(title, description, icon, switch))

    # --- the application ---
    def _build_general(self) -> None:
        settings = self.store.settings
        self._section(self.general_layout, "Startup")
        self.default_box = _combo([(profile.id, profile.name) for profile in settings.profiles],
                                  settings.default_profile, "Default profile")
        self.default_box.currentIndexChanged.connect(self._default_changed)
        self.controls["default_profile"] = self.default_box
        self.general_layout.addWidget(fluent.SettingsCard(
            "Default profile", "The profile of a new tab and of a new window", "tab-new-symbolic", self.default_box))
        self._section(self.general_layout, "Interaction")
        self._toggle("copy_on_select", "Copy on select", "Selected text goes to the clipboard at once",
                     "edit-copy-symbolic")
        self._toggle("ctrl_c_copies", "Ctrl+C copies selected text",
                     "With nothing selected, Ctrl+C interrupts the program as usual", "input-keyboard-symbolic")
        self._toggle("paste_warning", "Ask before pasting several lines",
                     "In a shell every line break in pasted text runs a command", "edit-paste-symbolic")
        self._toggle("clipboard_write", "Let programs write to the clipboard",
                     "Programs in the terminal, also on other machines, may then replace what you copied (OSC 52). "
                     "They can never read the clipboard.", "changes-allow-symbolic")
        self._toggle("confirm_close", "Ask before closing a tab in which a program runs",
                     "A shell that waits for a command is closed without asking", "window-close-symbolic")

    def _default_changed(self, index: int) -> None:
        key = self.default_box.itemData(index)
        if isinstance(key, str) and not self._building:
            self.store.settings.default_profile = key
            self._changed()

    def _sync_default_box(self) -> None:
        self._building = True
        try:
            settings = self.store.settings
            self.default_box.clear()
            for profile in settings.profiles:
                self.default_box.addItem(profile.name, profile.id)
            self.default_box.setCurrentIndex(max(0, self.default_box.findData(settings.default_profile)))
        finally:
            self._building = False

    # --- profiles ---
    def _build_profiles(self) -> None:
        self._building = True
        try:
            for card in self.profile_cards.values():
                if card.is_expanded():
                    self._expanded.add(card.property("profileId"))
            while self.profiles_layout.count():
                item = self.profiles_layout.takeAt(0)
                if item.widget() is not None:
                    item.widget().deleteLater()
            self.profile_cards = {}
            self._section(self.profiles_layout, "Profiles")
            for profile in self.store.settings.profiles:
                self.profiles_layout.addWidget(self._profile_card(profile))
            row = QWidget()
            buttons = QHBoxLayout(row)
            buttons.setContentsMargins(0, 8, 0, 0)
            self.add_button = fluent.Button("Add a profile", row, icon="list-add-symbolic")
            self.add_button.clicked.connect(self.add_profile)
            self.add_button.setEnabled(len(self.store.settings.profiles) < settings_module.MAX_PROFILES)
            buttons.addWidget(self.add_button)
            buttons.addStretch(1)
            self.profiles_layout.addWidget(row)
        finally:
            self._building = False

    def _describe(self, profile: settings_module.Profile) -> str:
        return profile.command or "The login shell"

    def _profile_card(self, profile: settings_module.Profile) -> fluent.SettingsExpander:
        key = profile.id
        card = fluent.SettingsExpander(profile.name, self._describe(profile), "utilities-terminal",
                                       expanded=key in self._expanded)
        card.setProperty("profileId", key)
        self.profile_cards[key] = card

        def current() -> settings_module.Profile | None:
            for item in self.store.settings.profiles:
                if item.id == key:
                    return item
            return None

        def setter(field: str, convert=lambda value: value):
            def apply(value) -> None:
                target = current()
                if target is None or self._building:
                    return
                setattr(target, field, convert(value))
                card.header.set_title(target.name)
                card.header.set_description(self._describe(target))
                if field == "name":
                    self._sync_default_box()
                self._changed()
            return apply

        def line(field: str, placeholder: str, limit: int) -> QLineEdit:
            edit = QLineEdit(getattr(profile, field))
            edit.setPlaceholderText(placeholder)
            edit.setMaxLength(limit)
            edit.setMinimumWidth(280)
            edit.editingFinished.connect(lambda: setter(field, lambda text: text.strip())(edit.text()))
            self.controls[f"{key}.{field}"] = edit
            return edit

        def name_text(text: str) -> str:
            return text.strip() or "Profile"

        name = QLineEdit(profile.name)
        name.setMaxLength(settings_module.MAX_NAME)
        name.setMinimumWidth(280)
        name.editingFinished.connect(lambda: setter("name", name_text)(name.text()))
        self.controls[f"{key}.name"] = name
        card.add_row("Name", "", name)
        card.add_row("Command", "The program of a tab, with its arguments; empty for your login shell",
                     line("command", "Login shell", settings_module.MAX_COMMAND))
        card.add_row("Starting directory", "Empty: where the terminal was started, usually your home folder",
                     line("directory", "~", 4096))

        families = monospace_families()
        font_box = QComboBox()
        font_box.addItem(f"{_AUTOMATIC} ({default_font_family()})", "")
        for family in families:
            font_box.addItem(family, family)
        if profile.font_family and profile.font_family not in families:
            font_box.addItem(profile.font_family, profile.font_family)
        font_box.setCurrentIndex(max(0, font_box.findData(profile.font_family)))
        font_box.setAccessibleName("Font")
        font_box.wheelEvent = lambda event: event.ignore()
        font_box.currentIndexChanged.connect(lambda index: setter("font_family")(font_box.itemData(index) or ""))
        self.controls[f"{key}.font_family"] = font_box
        card.add_row("Font", "A font in which every character has the same width", font_box)

        size = fluent.NumberBox(value=profile.font_size, minimum=settings_module.FONT_SIZE_MIN,
                                maximum=settings_module.FONT_SIZE_MAX, step=1, decimals=1, suffix=" pt")
        size.valueChanged.connect(setter("font_size", lambda value: round(float(value) * 2) / 2))
        self.controls[f"{key}.font_size"] = size
        card.add_row("Font size", "", size)

        scheme = _combo(schemes.names(), profile.scheme, "Colour scheme")
        scheme.currentIndexChanged.connect(lambda index: setter("scheme")(scheme.itemData(index)))
        self.controls[f"{key}.scheme"] = scheme
        card.add_row("Colour scheme", "Boswas Prism follows the dark or light scheme of the desktop", scheme)

        cursor = _combo(_CURSORS, profile.cursor_shape, "Cursor shape")
        cursor.currentIndexChanged.connect(lambda index: setter("cursor_shape")(cursor.itemData(index)))
        self.controls[f"{key}.cursor_shape"] = cursor
        card.add_row("Cursor shape", "A program can ask for another shape while it runs", cursor)

        blink = fluent.ToggleSwitch(checked=profile.cursor_blink)
        blink.toggled.connect(setter("cursor_blink", bool))
        self.controls[f"{key}.cursor_blink"] = blink
        card.add_row("Cursor blinks", "", blink)

        scrollback = fluent.NumberBox(value=profile.scrollback, minimum=0, maximum=MAX_SCROLLBACK, step=1000,
                                      suffix=" lines")
        scrollback.valueChanged.connect(setter("scrollback", lambda value: int(round(float(value)))))
        self.controls[f"{key}.scrollback"] = scrollback
        card.add_row("Scrollback", "Lines kept above the screen; 0 keeps none", scrollback)

        bell = _combo(_BELLS, profile.bell, "Bell")
        bell.currentIndexChanged.connect(lambda index: setter("bell")(bell.itemData(index)))
        self.controls[f"{key}.bell"] = bell
        card.add_row("Bell", "A tab in the background is marked in every case", bell)

        closing = _combo(_CLOSING, profile.close_on_exit, "When the program ends")
        closing.currentIndexChanged.connect(lambda index: setter("close_on_exit")(closing.itemData(index)))
        self.controls[f"{key}.close_on_exit"] = closing
        card.add_row("When the program ends", "", closing)

        actions = QWidget()
        row = QHBoxLayout(actions)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        duplicate = fluent.Button("Duplicate", actions)
        duplicate.clicked.connect(lambda: self.duplicate_profile(key))
        delete = fluent.Button("Delete", actions)
        delete.setEnabled(len(self.store.settings.profiles) > 1)
        delete.clicked.connect(lambda: self.delete_profile(key))
        row.addWidget(duplicate)
        row.addWidget(delete)
        self.controls[f"{key}.duplicate"] = duplicate
        self.controls[f"{key}.delete"] = delete
        card.add_row("This profile", "", actions)
        return card

    def _unique_name(self, base: str) -> str:
        names = {profile.name for profile in self.store.settings.profiles}
        if base not in names:
            return base
        number = 2
        while f"{base} {number}" in names:
            number += 1
        return f"{base} {number}"[:settings_module.MAX_NAME]

    def _profiles_changed(self, expand: str | None = None) -> None:
        if expand:
            self._expanded.add(expand)
        self._sync_default_box()
        self._build_profiles()
        self.store.update()
        if expand and expand in self.profile_cards:
            QTimer.singleShot(0, lambda: expand in self.profile_cards and self.scroll.ensureWidgetVisible(
                self.profile_cards[expand]))

    def add_profile(self) -> None:
        settings = self.store.settings
        if len(settings.profiles) >= settings_module.MAX_PROFILES:
            return
        profile = settings_module.Profile(settings_module.new_id(), self._unique_name("New profile"))
        settings.profiles.append(profile)
        self._profiles_changed(profile.id)

    def duplicate_profile(self, key: str) -> None:
        settings = self.store.settings
        source = next((profile for profile in settings.profiles if profile.id == key), None)
        if source is None or len(settings.profiles) >= settings_module.MAX_PROFILES:
            return
        copy = replace(source, id=settings_module.new_id(), name=self._unique_name(source.name))
        settings.profiles.insert(settings.profiles.index(source) + 1, copy)
        self._profiles_changed(copy.id)

    def delete_profile(self, key: str) -> None:
        settings = self.store.settings
        target = next((profile for profile in settings.profiles if profile.id == key), None)
        if target is None or len(settings.profiles) <= 1:
            return
        if not fluent.ask(self, "Delete this profile?", f"“{target.name}” will be removed. Tabs that use "
                          "it take the default profile.", "Delete", "Cancel"):
            return
        settings.profiles.remove(target)
        if settings.default_profile == key:
            settings.default_profile = settings.profiles[0].id
        self._expanded.discard(key)
        self._profiles_changed()
