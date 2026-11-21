"""The window of the Snipping Tool: the bars, the canvas, the snip flow, saving and copying, the settings."""

from __future__ import annotations

import datetime as dt
import os
from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import QEvent, QSize, Qt, QTimer
from PySide6.QtGui import QGuiApplication, QImage, QKeySequence
from PySide6.QtWidgets import (QComboBox, QFileDialog, QFrame, QHBoxLayout, QScrollArea, QStackedWidget,
                               QVBoxLayout, QWidget)

import boswas_fluent as fluent
from boswas_fluent.shortcuts import key_sequences

from .. import APP_ID, DISPLAY_NAME, capture, cli, picture
from .. import settings as settings_module
from ..annotations import History, State
from .canvas import SnipCanvas
from .snip import SnipFlow
from .toolbar import DEFAULT_COLOUR, EditBar, ZoomBar, colour_value

VERSION = "1.0"
MODE_CHOICES = (("region", "Rectangle", "draw-rectangle-symbolic"), ("screen", "Full screen", "view-fullscreen-symbolic"))
AUTO_COPY_MS = 600
NOTICE_MS = 4000                # how long a passing notice ("Snip cancelled.") stays
COMPACT_BELOW = 820             # window width below which the mode and delay show only their icons
OPEN_FILTER = "Pictures (*.png *.jpg *.jpeg *.webp);;All files (*)"
SAVE_FILTERS = (("png", "PNG picture (*.png)"), ("jpeg", "JPEG picture (*.jpg *.jpeg)"))
PRIMARY = fluent.ContentDialog.Result.PRIMARY
SECONDARY = fluent.ContentDialog.Result.SECONDARY


def delay_text(seconds: int) -> str:
    if seconds <= 0:
        return "No delay"
    return f"{seconds} second" + ("" if seconds == 1 else "s")


class ChoiceBox(QComboBox):
    """A combo box that can show only the icon of its choice and its arrow (``set_compact``), in a narrow window."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._compact = False

    def set_compact(self, compact: bool) -> None:
        if compact != self._compact:
            self._compact = compact
            self.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon if compact
                                     else QComboBox.SizeAdjustPolicy.AdjustToContents)
            self.setMinimumContentsLength(0)
            self.updateGeometry()

    def is_compact(self) -> bool:
        return self._compact

    def sizeHint(self) -> QSize:
        hint = super().sizeHint()
        if self._compact:                      # Qt leaves no room for the icon without any text
            hint.setWidth(hint.width() + self.iconSize().width() + 4)
        return hint

    def minimumSizeHint(self) -> QSize:
        return super().minimumSizeHint().boundedTo(self.sizeHint())


class SnipWindow(fluent.FluentWindow):
    """The Snipping Tool. Everything that reaches outside (the compositor, the clipboard, file choosers,
    questions, other programs) is an argument or an attribute that tests replace."""

    def __init__(self, arguments: list[str] | None = None, *, client=None, clipboard=None,
                 settings_path: str | os.PathLike | None = None, environ=None, folder=None) -> None:
        super().__init__(APP_ID, DISPLAY_NAME, icon="boswas-snipping-tool", size=(1100, 720),
                         minimum_size=(520, 420))
        self.environ = environ
        self.client = client if client is not None else capture.CaptureClient(environ)
        self.clipboard = clipboard if clipboard is not None else QGuiApplication.clipboard()
        self.settings_path = Path(settings_path) if settings_path is not None else \
            fluent.config_dir(APP_ID) / settings_module.FILE_NAME
        self.settings = settings_module.load(self.settings_path)
        self.open_path = fluent.open_path
        self._find_folder = folder if folder is not None else (lambda: capture.find_screenshots_dir(environ))
        self.worker = fluent.Worker(self, max_threads=2)
        self.settings_worker = fluent.Worker(self, max_threads=1)       # one write after the other, in order
        self.flow = SnipFlow(self.client, self, folder=self._find_folder)
        self.screenshots_dir: Path | None = None
        self.desktop_available: bool | None = None
        # the edit
        self.image: QImage | None = None
        self.file_path: str | None = None        # the file that holds what is shown while nothing is changed
        self.source_stem: str | None = None      # the name the picture came with, for suggestions
        self.saved_path: str | None = None       # where it was saved last
        self.history = History()
        self.mode = self.settings.mode
        self.delay = self.settings.delay
        self.tool = ""
        self.colour_token = DEFAULT_COLOUR
        self.size_index = 1
        self._saving = False
        self._closing = False
        self._copy_again = False
        self._auto_copy_timer = QTimer(self)
        self._auto_copy_timer.setSingleShot(True)
        self._auto_copy_timer.setInterval(AUTO_COPY_MS)
        self._auto_copy_timer.timeout.connect(self._auto_copy)
        self._notice_timer = QTimer(self)
        self._notice_timer.setSingleShot(True)
        self._notice_timer.setInterval(NOTICE_MS)
        self._notice_timer.timeout.connect(self._dismiss_notice)
        self._notice = False
        self._build()
        self._wire()
        self._add_shortcuts()
        self.set_mode(self.mode, save=False)
        self.set_delay(self.delay, save=False)
        parsed = cli.parse(list(arguments or []))
        if parsed.mode is not None:
            self.set_mode(parsed.mode, save=False)
        if parsed.delay is not None:
            self.set_delay(parsed.delay, save=False)
        self._sync()
        QTimer.singleShot(0, self, lambda: self._start(parsed))

    # --- building ---
    def _build(self) -> None:
        self.pages = QStackedWidget(self)
        self.set_content(self.pages)
        self.main_page = QWidget()
        outer = QVBoxLayout(self.main_page)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        header = QWidget(self.main_page)
        row = QHBoxLayout(header)
        row.setContentsMargins(16, 12, 16, 8)
        row.setSpacing(8)
        self.new_button = fluent.Button("New", header, icon="screenshot-symbolic", accent=True)
        self.new_button.setToolTip("New snip (Ctrl+N)")
        self.new_button.setAccessibleName("New snip")
        self.mode_box = self._mode_combo(header)
        self.delay_box = self._delay_combo(header)
        self.commands = fluent.CommandBar(header)
        self.open_action = self.commands.add_command("Open", "document-open-symbolic", self.open_file,
                                                     shortcut="Ctrl+O", tooltip="Open a picture to mark up")
        self.save_action = self.commands.add_command("Save as", "document-save-as-symbolic", self.save_as,
                                                     shortcut="Ctrl+S")
        self.copy_action = self.commands.add_command("Copy", "edit-copy-symbolic", self.copy, shortcut="Ctrl+C",
                                                     tooltip="Copy the snip as edited")
        self.photos_action = self.commands.add_command("Open in Photos", "image-x-generic-symbolic",
                                                       self.open_in_photos)
        self.paste_action = self.commands.add_command("Paste a picture", "edit-paste-symbolic", self.paste,
                                                      shortcut="Ctrl+V", primary=False,
                                                      tooltip="Mark up the picture on the clipboard")
        self.settings_button = fluent.IconButton("preferences-system-symbolic", "Settings (Ctrl+,)", header)
        self.settings_button.setAccessibleName("Settings")
        self.commands.add_widget(self.settings_button)
        row.addWidget(self.new_button)
        row.addWidget(self.mode_box)
        row.addWidget(self.delay_box)
        row.addSpacing(8)
        row.addWidget(self.commands, 1)
        outer.addWidget(header)

        self.edit_holder = QWidget(self.main_page)
        edit_row = QHBoxLayout(self.edit_holder)
        edit_row.setContentsMargins(16, 0, 16, 8)
        self.edit_bar = EditBar(self.edit_holder)
        edit_row.addWidget(self.edit_bar)
        outer.addWidget(self.edit_holder)

        banners = QWidget(self.main_page)
        banner_column = QVBoxLayout(banners)
        banner_column.setContentsMargins(16, 0, 16, 0)
        banner_column.setSpacing(8)
        self.desktop_bar = fluent.InfoBar("warning", "", "", banners)
        self.desktop_bar.hide()
        self.status = fluent.InfoBar("info", "", "", banners)
        self.status.hide()
        banner_column.addWidget(self.desktop_bar)
        banner_column.addWidget(self.status)
        outer.addWidget(banners)

        self.stack = QStackedWidget(self.main_page)
        self.empty_page = fluent.EmptyState(
            "Snip the screen", "Select New (Ctrl+N) to snip an area or the whole screen, or open a picture "
            "to mark it up.", "screenshot-symbolic")
        self.canvas = SnipCanvas()
        self.countdown_page = self._countdown_page()
        self.waiting_page = self._waiting_page()
        for page in (self.empty_page, self.canvas, self.countdown_page, self.waiting_page):
            self.stack.addWidget(page)
        outer.addWidget(self.stack, 1)

        self.zoom_holder = QWidget(self.main_page)
        zoom_row = QHBoxLayout(self.zoom_holder)
        zoom_row.setContentsMargins(16, 4, 16, 8)
        self.zoom_bar = ZoomBar(self.zoom_holder)
        zoom_row.addWidget(self.zoom_bar)
        outer.addWidget(self.zoom_holder)

        self.pages.addWidget(self.main_page)
        self.settings_page = self._settings_page()
        self.pages.addWidget(self.settings_page)
        self.canvas.colour = colour_value(self.colour_token)
        self.canvas.size_index = self.size_index
        self._show_page(self.empty_page)

    def _mode_combo(self, parent: QWidget) -> QComboBox:
        box = ChoiceBox(parent)
        for key, text, icon in MODE_CHOICES:
            box.addItem(fluent.icon(icon), text, key)
        box.setAccessibleName("Snipping mode")
        return box

    def _delay_combo(self, parent: QWidget) -> QComboBox:
        box = ChoiceBox(parent)
        for seconds in settings_module.DELAYS:
            box.addItem(fluent.icon("timer-symbolic"), delay_text(seconds), seconds)
        box.setAccessibleName("Snip delay")
        return box

    def _centred_column(self) -> tuple[QWidget, QVBoxLayout]:
        page = QWidget()
        column = QVBoxLayout(page)
        column.setContentsMargins(32, 32, 32, 32)
        column.setSpacing(8)
        column.addStretch(1)
        return page, column

    def _countdown_page(self) -> QWidget:
        page, column = self._centred_column()
        self.count_label = fluent.Label("", "title-large", page)
        self.count_label.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self.count_text = fluent.Label("", "body", page, tone="secondary", wrap=True)
        self.count_text.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self.count_cancel = fluent.Button("Cancel", page)
        self.count_cancel.setAutoDefault(True)
        self.count_cancel.setToolTip("Do not take the snip (Esc)")
        column.addWidget(self.count_label)
        column.addWidget(self.count_text)
        column.addSpacing(8)
        column.addWidget(self.count_cancel, 0, Qt.AlignmentFlag.AlignHCenter)
        column.addStretch(1)
        page.setAccessibleName("Countdown")
        return page

    def _waiting_page(self) -> QWidget:
        page, column = self._centred_column()
        self.waiting_ring = fluent.ProgressRing(page, size=48)
        column.addWidget(self.waiting_ring, 0, Qt.AlignmentFlag.AlignHCenter)
        column.addSpacing(8)
        title = fluent.Label("Waiting for your snip", "subtitle", page, wrap=True)
        title.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self.waiting_text = fluent.Label("", "body", page, tone="secondary", wrap=True)
        self.waiting_text.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self.waiting_cancel = fluent.Button("Cancel", page)
        self.waiting_cancel.setAutoDefault(True)
        self.waiting_cancel.setToolTip("Stop waiting (Esc)")
        column.addWidget(title)
        column.addWidget(self.waiting_text)
        column.addSpacing(8)
        column.addWidget(self.waiting_cancel, 0, Qt.AlignmentFlag.AlignHCenter)
        column.addStretch(1)
        page.setAccessibleName("Waiting for the snip")
        return page

    def _settings_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        head = QHBoxLayout()
        head.setContentsMargins(24, 16, 32, 12)
        head.setSpacing(8)
        self.back_button = fluent.IconButton("go-previous-symbolic", "Back (Esc)", page)
        self.back_button.setAccessibleName("Back")
        head.addWidget(self.back_button, 0, Qt.AlignmentFlag.AlignVCenter)
        head.addWidget(fluent.Label("Settings", "title", page), 1)
        layout.addLayout(head)
        content = QWidget()
        column = QVBoxLayout(content)
        column.setContentsMargins(32, 0, 32, 32)
        column.setSpacing(4)
        self.settings_mode_box = self._mode_combo(content)
        column.addWidget(fluent.SettingsCard("Snipping mode", "What New snips: an area you drag or the whole screen",
                                             "screenshot-symbolic", self.settings_mode_box))
        self.settings_delay_box = self._delay_combo(content)
        column.addWidget(fluent.SettingsCard("Snip delay", "How long to wait before the snip, to open a menu first",
                                             "timer-symbolic", self.settings_delay_box))
        self.auto_copy_switch = fluent.ToggleSwitch(checked=self.settings.auto_copy)
        column.addWidget(fluent.SettingsCard(
            "Copy changes automatically", "After every change the edited snip is on the clipboard. The desktop "
            "copies every new snip itself.", "edit-copy-symbolic", self.auto_copy_switch))
        self.ask_switch = fluent.ToggleSwitch(checked=self.settings.ask_to_save)
        column.addWidget(fluent.SettingsCard(
            "Ask to save changes", "Before marks that are not saved are thrown away by closing, a new snip or "
            "opening another picture", "document-save-as-symbolic", self.ask_switch))
        self.folder_button = fluent.Button("Open folder", content)
        self.folder_card = fluent.SettingsCard("Screenshots folder", "Where the desktop saves every snip",
                                               "document-open-symbolic", self.folder_button)
        column.addWidget(self.folder_card)
        column.addWidget(fluent.SettingsCard(
            DISPLAY_NAME, f"Version {VERSION}. Snips are taken by the Boswas desktop, saved to the Screenshots "
            "folder and copied to the clipboard; here you mark them up.", "help-about-symbolic"))
        column.addStretch(1)
        scroll = QScrollArea(page)
        scroll.setWidget(content)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        layout.addWidget(scroll, 1)
        return page

    def _wire(self) -> None:
        self.new_button.clicked.connect(self.new_snip)
        for box in (self.mode_box, self.settings_mode_box):
            box.activated.connect(lambda index, b=box: self.set_mode(b.itemData(index)))
        for box in (self.delay_box, self.settings_delay_box):
            box.activated.connect(lambda index, b=box: self.set_delay(b.itemData(index)))
        self.settings_button.clicked.connect(self.show_settings)
        self.back_button.clicked.connect(self.hide_settings)
        self.auto_copy_switch.toggled.connect(self._auto_copy_toggled)
        self.ask_switch.toggled.connect(self._ask_toggled)
        self.folder_button.clicked.connect(self.open_screenshots_folder)
        bar = self.edit_bar
        bar.tool_chosen.connect(self.set_tool)
        bar.colour_chosen.connect(self.set_colour)
        bar.size_chosen.connect(self.set_size)
        bar.undo_button.clicked.connect(self.undo)
        bar.redo_button.clicked.connect(self.redo)
        bar.clear_button.clicked.connect(self.clear_marks)
        bar.apply_crop_button.clicked.connect(self.apply_crop)
        bar.cancel_crop_button.clicked.connect(self.cancel_crop)
        zoom = self.zoom_bar
        zoom.zoom_in_button.clicked.connect(self.canvas.zoom_in)
        zoom.zoom_out_button.clicked.connect(self.canvas.zoom_out)
        zoom.fit_button.clicked.connect(self.canvas.zoom_fit)
        zoom.actual_button.clicked.connect(self.canvas.zoom_actual)
        zoom.percent_button.clicked.connect(self.canvas.toggle_zoom)
        self.canvas.zoom_changed.connect(self._show_zoom)
        self.canvas.item_added.connect(self._item_added)
        self.canvas.items_erased.connect(self._items_erased)
        self.desktop_bar.action_triggered.connect(self._probe)
        self.count_cancel.clicked.connect(self.flow.cancel)
        self.waiting_cancel.clicked.connect(self.flow.cancel)
        flow = self.flow
        flow.countdown.connect(self._flow_countdown)
        flow.hide_requested.connect(self._flow_hide)
        flow.show_requested.connect(self._flow_show)
        flow.waiting.connect(self._flow_waiting)
        flow.finished.connect(self._flow_finished)
        flow.failed.connect(self._flow_failed)
        flow.ended.connect(self._flow_ended)

    def _add_shortcuts(self) -> None:
        self.add_shortcut("new", self.new_snip)
        self.add_shortcut("save_as", self.save_as)
        self.add_shortcut("undo", self.undo)
        self.add_shortcut("redo", self.redo)
        if not any(sequence.matches(QKeySequence("Ctrl+Y")) == QKeySequence.SequenceMatch.ExactMatch
                   for sequence in key_sequences("redo")):
            self.add_shortcut("Ctrl+Y", self.redo)
        self.add_shortcut("delete", self.clear_marks)
        self.add_shortcut("zoom_in", self.canvas.zoom_in)
        self.add_shortcut("Ctrl+=", self.canvas.zoom_in)
        self.add_shortcut("zoom_out", self.canvas.zoom_out)
        self.add_shortcut("zoom_reset", self.canvas.zoom_fit)
        self.add_shortcut("Ctrl+1", self.canvas.zoom_actual)
        self.add_shortcut("preferences", self.show_settings)
        self.add_shortcut("close", self.close)
        self.add_shortcut("quit", self.close)

    # --- hooks: what asks the user or the system (tests replace these) ---
    def run_dialog(self, dialog: fluent.ContentDialog) -> fluent.ContentDialog.Result:
        return dialog.exec()

    def choose_open_path(self, start: str) -> str | None:
        path, _filter = QFileDialog.getOpenFileName(self, "Open a picture", start, OPEN_FILTER)
        return path or None

    def choose_save_path(self, suggested: str) -> str | None:
        fmt = picture.format_for_path(suggested) or "png"
        selected = next(text for key, text in SAVE_FILTERS if key == fmt)
        path, chosen = QFileDialog.getSaveFileName(self, "Save as", suggested,
                                                   ";;".join(text for _key, text in SAVE_FILTERS), selected)
        if not path:
            return None
        if not Path(path).suffix:
            path += ".jpg" if chosen.startswith("JPEG") else ".png"
        return path

    # --- start-up and arguments ---
    def _start(self, parsed: cli.Arguments) -> None:
        self._probe()
        self._apply_arguments(parsed)

    def handle_arguments(self, arguments: list[str]) -> None:
        """A later start of the program: its options and file are handed here."""
        parsed = cli.parse(list(arguments))
        if parsed.mode is not None:
            self.set_mode(parsed.mode, save=False)
        if parsed.delay is not None:
            self.set_delay(parsed.delay, save=False)
        self._apply_arguments(parsed)

    def _apply_arguments(self, parsed: cli.Arguments) -> None:
        if parsed.new:
            self.new_snip()
        elif parsed.path is not None:
            path = parsed.path
            self._confirm_discard(lambda: self.load_path(path))

    def _probe(self) -> None:
        """Find out (on the worker) whether the desktop's capture is there and where the Screenshots folder is."""
        client, find = self.client, self._find_folder
        self.worker.submit(lambda: (client.available(), find()), on_done=self._probed, key="probe",
                           on_error=lambda exc: self._probed((False, None)))

    def _probed(self, result: tuple[bool, Path | None]) -> None:
        available, folder = result
        self.desktop_available = bool(available)
        if folder is not None:
            self.screenshots_dir = Path(folder)
            self.folder_card.set_description(str(folder))
        if available:
            self.desktop_bar.hide()
        else:
            self.desktop_bar.set_action("Try again")
            self.desktop_bar.show_message(
                "warning", "Snips are taken by the Boswas desktop, and its screen capture cannot be reached in this "
                "session. You can still open a picture and mark it up.", "New snips are not available")
        self._sync()

    # --- settings ---
    def show_settings(self) -> None:
        if self.flow.is_active():
            return
        self.canvas.finish_text(commit=True)
        self.pages.setCurrentWidget(self.settings_page)
        self.back_button.setFocus(Qt.FocusReason.OtherFocusReason)

    def hide_settings(self) -> None:
        self.pages.setCurrentWidget(self.main_page)
        self.settings_button.setFocus(Qt.FocusReason.OtherFocusReason)

    def _save_settings(self) -> None:
        values, path = replace(self.settings), self.settings_path
        self.settings_worker.submit(settings_module.save, values, path, on_error=lambda exc: self.report(
            "warning", f"Your settings could not be saved: {getattr(exc, 'strerror', None) or exc}."))

    def set_mode(self, mode: str, save: bool = True) -> None:
        if mode not in settings_module.MODES:
            return
        self.mode = mode
        for box in (self.mode_box, self.settings_mode_box):
            box.setCurrentIndex(max(0, box.findData(mode)))
        self.mode_box.setToolTip(f"Snipping mode: {self.mode_box.currentText()}")
        if save and self.settings.mode != mode:
            self.settings.mode = mode
            self._save_settings()
        self.waiting_text.setText(self._waiting_hint())

    def set_delay(self, delay: int, save: bool = True) -> None:
        if not isinstance(delay, int) or not 0 <= delay <= cli.MAX_DELAY:
            return
        self.delay = delay
        for box in (self.delay_box, self.settings_delay_box):
            if box.findData(delay) < 0:
                box.addItem(fluent.icon("timer-symbolic"), delay_text(delay), delay)
            box.setCurrentIndex(box.findData(delay))
        self.delay_box.setToolTip(f"Snip delay: {self.delay_box.currentText()}")
        if save and delay in settings_module.DELAYS and self.settings.delay != delay:
            self.settings.delay = delay
            self._save_settings()

    def _auto_copy_toggled(self, on: bool) -> None:
        self.settings.auto_copy = bool(on)
        self._save_settings()

    def _ask_toggled(self, on: bool) -> None:
        self.settings.ask_to_save = bool(on)
        self._save_settings()

    def open_screenshots_folder(self) -> None:
        folder = self.screenshots_dir

        def opened(exists: bool) -> None:
            if not exists:
                self.hide_settings()
                self.report("info", "The Screenshots folder does not exist yet. The desktop makes it with the first "
                            "snip.")
            elif not self.open_path(str(folder)):
                self.hide_settings()
                self.report("warning", "The Screenshots folder could not be opened.")

        if folder is not None:
            self.worker.submit(os.path.isdir, folder, on_done=opened)

    # --- messages ---
    def report(self, severity: str, message: str, title: str = "") -> None:
        self._notice = False
        self._notice_timer.stop()
        self.status.set_action(None)
        self.status.show_message(severity, message, title)

    def notice(self, message: str) -> None:
        """A passing message: it closes by itself after a moment, or with the next action."""
        self.report("info", message)
        self._notice = True
        self._notice_timer.start()

    def _dismiss_notice(self) -> None:
        if self._notice:
            self._notice = False
            self._notice_timer.stop()
            self.status.hide()

    # --- the state of the window ---
    def _editing(self) -> bool:
        """Whether a picture is in the editor and nothing else is going on (a snip, the settings)."""
        return (self.image is not None and not self.flow.is_active() and self.pages.currentWidget() is self.main_page
                and self.stack.currentWidget() is self.canvas)

    def _show_page(self, page: QWidget) -> None:
        self.stack.setCurrentWidget(page)
        editor = page is self.canvas
        self.edit_holder.setVisible(editor)
        self.zoom_holder.setVisible(editor)
        self._update_title()

    def _restore_view(self) -> None:
        self._show_page(self.canvas if self.image is not None else self.empty_page)
        self._sync()

    def _sync(self) -> None:
        busy = self.flow.is_active()
        has = self.image is not None and not busy
        self.new_button.setEnabled(not busy and self.desktop_available is not False)
        for box in (self.mode_box, self.delay_box):
            box.setEnabled(not busy)
        self.open_action.setEnabled(not busy)
        self.paste_action.setEnabled(not busy)
        for action in (self.save_action, self.copy_action, self.photos_action):
            action.setEnabled(has and not self._saving)
        state = self.history.state
        bar = self.edit_bar
        bar.undo_button.setEnabled(has and self.history.can_undo())
        bar.redo_button.setEnabled(has and self.history.can_redo())
        bar.clear_button.setEnabled(has and bool(state.items))
        bar.show_tool(self.tool)
        if self.image is not None:
            box = state.visible(self.image.width(), self.image.height())
            self.zoom_bar.show_size(int(box.width), int(box.height))
        self._show_zoom()
        self._update_title()

    def _show_zoom(self) -> None:
        self.zoom_bar.show_zoom(self.canvas.zoom_percent(), self.canvas.is_fitted())

    def _name(self) -> str:
        if self.file_path:
            return Path(self.file_path).name
        return self.source_stem or "Snip"

    def _update_title(self) -> None:
        if self.image is None or self.stack.currentWidget() in (self.countdown_page, self.waiting_page):
            self.setWindowTitle(DISPLAY_NAME)
            return
        modified = "*" if self.history.is_modified() else ""
        self.setWindowTitle(f"{modified}{self._name()} - {DISPLAY_NAME}")

    # --- the picture ---
    def _set_document(self, image: QImage, path: str | None, stem: str | None = None) -> None:
        self.canvas.finish_text(commit=False)
        self.image = image
        self.file_path = path
        self.source_stem = Path(path).stem if path else stem
        self.saved_path = None
        self.history.reset()
        self.set_tool("")
        self.canvas.set_picture(image)
        self.canvas.set_state(self.history.state)
        self.status.hide()
        self._show_page(self.canvas)
        self._sync()
        self.canvas.setFocus(Qt.FocusReason.OtherFocusReason)

    def load_path(self, path: str) -> None:
        """Open a picture file (decoded on the worker)."""
        def failed(exc: Exception) -> None:
            if isinstance(exc, picture.PictureError):
                self.report("error", exc.detail or exc.title, exc.title)
            else:
                self.report("error", f"{Path(path).name} could not be opened: {exc}")

        self.worker.submit(picture.load, path, on_done=lambda image: self._set_document(image, path),
                           on_error=failed)

    def open_file(self) -> None:
        if self.flow.is_active():
            return
        self.canvas.finish_text(commit=True)
        self._confirm_discard(self._choose_and_open)

    def _choose_and_open(self) -> None:
        start = self.file_path and str(Path(self.file_path).parent) or str(self.screenshots_dir or
                                                                         capture.home_dir(self.environ))
        path = self.choose_open_path(start)
        if path:
            self.load_path(path)

    def paste(self) -> None:
        """Mark up the picture on the clipboard."""
        if self.flow.is_active():
            return
        image = self.clipboard.image()
        if image is None or image.isNull():
            self.report("info", "There is no picture on the clipboard.")
            return
        try:
            image = picture.accept_image(image)
        except picture.PictureError as exc:
            self.report("error", exc.detail, exc.title)
            return
        stem = "Snip " + dt.datetime.now().strftime("%Y-%m-%d %H%M%S")
        self.canvas.finish_text(commit=True)
        self._confirm_discard(lambda: self._set_document(image, None, stem))

    # --- editing ---
    def set_tool(self, tool: str) -> None:
        if tool == "crop" and self.image is None:
            tool = ""
        if tool:
            self._dismiss_notice()
        self.tool = tool
        self.canvas.set_tool(tool)
        self.edit_bar.show_tool(tool)
        if tool:
            self.canvas.setFocus(Qt.FocusReason.OtherFocusReason)

    def set_colour(self, token: str) -> None:
        self.colour_token = token
        self.canvas.colour = colour_value(token)
        self.edit_bar.show_value(token, self.size_index)
        if self.tool in ("", "eraser", "crop"):
            self.set_tool("pen")

    def set_size(self, index: int) -> None:
        self.size_index = index
        self.canvas.size_index = index
        self.edit_bar.show_value(self.colour_token, index)
        if self.tool in ("", "eraser", "crop"):
            self.set_tool("pen")

    def _push(self, state: State) -> None:
        if self.image is None or state == self.history.state:
            return
        self.history.push(state)
        self._changed()

    def _changed(self) -> None:
        self._dismiss_notice()
        self.canvas.set_state(self.history.state)
        self._sync()
        if self.settings.auto_copy and self.image is not None:
            self._auto_copy_timer.start()

    def _item_added(self, item) -> None:
        if self.history.state.is_full():
            self.report("warning", "This snip has as many marks as it can take. Clear some to add more.")
            return
        self._push(self.history.state.adding(item))

    def _items_erased(self, indexes) -> None:
        self._push(self.history.state.erasing(indexes))

    def undo(self) -> None:
        if self._editing():
            self.canvas.finish_text(commit=False)
            if self.history.undo():
                self._changed()

    def redo(self) -> None:
        if self._editing() and self.history.redo():
            self._changed()

    def clear_marks(self) -> None:
        if self._editing() and self.history.state.items:
            self._push(self.history.state.cleared())

    def apply_crop(self) -> None:
        if not self._editing() or self.tool != "crop":
            return
        rect = self.canvas.chosen_crop()
        self.set_tool("")
        if rect is not None:
            self._push(self.history.state.cropped(rect))

    def cancel_crop(self) -> None:
        if self.tool == "crop":
            self.set_tool("")

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._fit_header()

    def _fit_header(self) -> None:
        """Below ``COMPACT_BELOW`` pixels the two choices show only their icons, so commands keep their room."""
        compact = self.width() < COMPACT_BELOW
        for box in (self.mode_box, self.delay_box):
            box.set_compact(compact)

    def keyPressEvent(self, event) -> None:
        key = event.key()
        if key == Qt.Key.Key_Escape:
            if self.pages.currentWidget() is self.settings_page:
                self.hide_settings()
            elif self.flow.is_active():
                self.flow.cancel()
            elif self.tool:
                self.set_tool("")
            event.accept()
            return
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and self.tool == "crop" and self._editing():
            self.apply_crop()
            event.accept()
            return
        super().keyPressEvent(event)

    # --- the snip ---
    def new_snip(self) -> None:
        if self.flow.is_active():
            return
        if self.desktop_available is False:
            self.desktop_bar.show()
            self.report("error", "Snips need the Boswas desktop, which cannot be reached in this session.",
                        "No new snip")
            return
        self.canvas.finish_text(commit=True)
        self._confirm_discard(self._begin_snip)

    def _begin_snip(self) -> None:
        self.hide_settings()
        self._dismiss_notice()
        self.status.hide()
        self.set_tool("")
        self.waiting_text.setText(self._waiting_hint())
        if self.flow.start(self.mode, self.delay):
            self._sync()

    def _waiting_hint(self) -> str:
        if self.mode == "screen":
            how = "Click or press Enter to snip the whole screen"
        else:
            how = "Drag over the area you want, then press Enter"
        return f"{how}, or press Esc there to cancel. The snip is saved to the Screenshots folder and opens here."

    def _flow_countdown(self, seconds: int) -> None:
        self.count_label.setText(str(seconds))
        self.count_label.setAccessibleName(f"{seconds} seconds until the snip")
        self.count_text.setText(f"The snip starts in {delay_text(seconds)}. Get the screen ready.")
        self._show_page(self.countdown_page)
        self._sync()

    def _flow_hide(self) -> None:
        self._show_page(self.waiting_page)
        self._sync()
        self.hide()

    def _flow_show(self) -> None:
        self._show_page(self.waiting_page)
        self._reveal()

    def _flow_waiting(self) -> None:
        self._show_page(self.waiting_page)

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if event.type() == QEvent.Type.ActivationChange and self.isActiveWindow():
            self.window_activated()

    def window_activated(self) -> None:
        """The window became active. While a snip is awaited that means the desktop's capture screen closed:
        the flow gives the snip a moment to arrive and otherwise takes it as cancelled."""
        if self.flow.phase == "waiting":
            self.flow.activated()

    def _reveal(self) -> None:
        if self._closing:
            return
        if not self.isVisible():
            self.show()
        self.raise_()
        self.activateWindow()

    def _flow_finished(self, path: str) -> None:
        def loaded(image: QImage) -> None:
            self._reveal()
            self._set_document(image, path)

        def failed(exc: Exception) -> None:
            self._reveal()
            self._restore_view()
            detail = exc.detail if isinstance(exc, picture.PictureError) else str(exc)
            self.report("error", f"{Path(path).name}: {detail}", "The snip could not be opened")

        self.worker.submit(picture.load, path, on_done=loaded, on_error=failed)

    def _flow_failed(self, title: str, message: str) -> None:
        self._reveal()
        self._restore_view()
        self.report("error", message, title)

    def _flow_ended(self, reason: str) -> None:
        self._reveal()
        self._restore_view()
        if reason == "dismissed":
            self.notice("Snip cancelled.")
        elif reason == "timeout":
            self.report("info", "No new snip arrived in the Screenshots folder. If the desktop could not save it, "
                        "it is on the clipboard: paste it here with Ctrl+V.", "No snip")

    # --- saving, copying, other programs ---
    def _render(self, on_done) -> None:
        image, state, family = self.image, self.history.state, self.canvas.family
        self.worker.submit(picture.render, image, state, family, on_done=on_done,
                           on_error=lambda exc: self.report("error", f"The snip could not be prepared: {exc}"))

    def copy(self) -> None:
        if not self._editing():
            return
        self.canvas.finish_text(commit=True)

        def copied(image: QImage) -> None:
            self.clipboard.setImage(image)
            self.report("success", "Copied to the clipboard.")

        self._render(copied)

    def _auto_copy(self) -> None:
        if self.image is None or not self.settings.auto_copy:
            return
        if self.worker.busy("auto-copy"):
            self._copy_again = True
            return
        image, state, family = self.image, self.history.state, self.canvas.family

        def copied(result: QImage) -> None:
            if image is self.image:
                self.clipboard.setImage(result)
            if self._copy_again:
                self._copy_again = False
                self._auto_copy_timer.start()

        self.worker.submit(picture.render, image, state, family, key="auto-copy", on_done=copied,
                           on_error=lambda _exc: None)

    def _suggested_path(self) -> str:
        if self.saved_path:
            return self.saved_path
        folder = self.screenshots_dir or capture.home_dir(self.environ)
        stem = self.source_stem or "Snip " + dt.datetime.now().strftime("%Y-%m-%d %H%M%S")
        extension = ".png"
        if self.file_path and picture.format_for_path(self.file_path) == "jpeg":
            extension = Path(self.file_path).suffix.lower()
        if self.file_path and (self.history.state.items or self.history.state.crop is not None):
            stem += " (edited)"
        return str(folder / f"{stem}{extension}")

    def save_as(self, then=None) -> None:
        """Ask where, then write the picture as edited (PNG or JPEG); ``then()`` after it was written."""
        if self.image is None or self.flow.is_active() or self._saving:
            return
        self.canvas.finish_text(commit=True)
        path = self.choose_save_path(self._suggested_path())
        if not path:
            return
        fmt = picture.format_for_path(path)
        if fmt is None:
            self.report("error", "Choose a name that ends in .png or .jpg.", "Not saved")
            return
        image, state, family = self.image, self.history.state, self.canvas.family
        self._saving = True
        self._sync()

        def saved(written: str) -> None:
            self._saving = False
            self.file_path = self.saved_path = written
            if self.history.state is state:
                self.history.mark_saved()
            self._sync()
            self.report("success", f"Saved as {Path(written).name}.")
            if then is not None:
                then()

        def failed(exc: Exception) -> None:
            self._saving = False
            self._closing = False
            self._sync()
            detail = exc.detail if isinstance(exc, picture.PictureError) else str(exc)
            self.report("error", detail, "Not saved")

        self.worker.submit(picture.save, image, state, path, fmt, family, on_done=saved, on_error=failed)

    def open_in_photos(self) -> None:
        """Show the snip in Photos (the program that opens pictures); changes are saved first."""
        if not self._editing():
            return
        self.canvas.finish_text(commit=True)
        if self.file_path and not self.history.is_modified():
            self._hand_over(self.file_path)
        else:
            self.save_as(then=lambda: self._hand_over(self.file_path))

    def _hand_over(self, path: str | None) -> None:
        if path and not self.open_path(path):
            self.report("warning", "No program to show pictures could be started.")

    # --- unsaved changes ---
    def _needs_question(self) -> bool:
        return self.image is not None and self.history.is_modified() and self.settings.ask_to_save

    def _confirm_discard(self, then) -> None:
        """Call ``then()`` now, or after the user saved or chose not to save the changes; not at all on Cancel."""
        if not self._needs_question():
            then()
            return
        dialog = fluent.ContentDialog(self, "Save the changes to this snip?",
                                      f"Your marks on {self._name()} are not saved yet.", primary="Save",
                                      secondary="Don't save", close="Cancel", default="primary")
        try:
            result = self.run_dialog(dialog)
        finally:
            dialog.deleteLater()
        if result == PRIMARY:
            self.save_as(then=then)
        elif result == SECONDARY:
            then()

    def _close_now(self) -> None:
        self._closing = True
        self.close()

    def closeEvent(self, event) -> None:
        if not self._closing:
            self.canvas.finish_text(commit=True)
            if self.flow.is_active():
                self.flow.cancel()
            if self._needs_question():
                event.ignore()
                self._confirm_discard(lambda: QTimer.singleShot(0, self, self._close_now))
                return
        self._closing = True
        super().closeEvent(event)
        if event.isAccepted():
            self._auto_copy_timer.stop()
            self.flow.shutdown()
            self.worker.shutdown()
            self.settings_worker.wait(2)                 # the last change of a setting is written
            self.settings_worker.shutdown()
