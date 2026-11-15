"""The gallery: every widget of the toolkit and every standard widget the style sheet covers.

    python3 -m boswas_fluent.gallery                  open the gallery
    python3 -m boswas_fluent.gallery --scheme light   start in the light scheme
    python3 -m boswas_fluent.gallery --grab DIR       render every page in both schemes to DIR/*.png
                                                      (no display needed) and exit

It is also the longest example of how to use the toolkit: each page is a
plain function that builds widgets.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from PySide6.QtCore import QDate, QSize, Qt, QTimer
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (QApplication, QCalendarWidget, QCheckBox, QComboBox, QDateEdit, QDockWidget,
                               QFormLayout, QGridLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QListWidget,
                               QListWidgetItem, QMainWindow, QMenu, QMenuBar, QPlainTextEdit, QProgressBar,
                               QPushButton, QRadioButton, QScrollArea, QSizePolicy, QSlider, QSpinBox, QSplitter,
                               QStatusBar, QTableWidget, QTableWidgetItem, QTabWidget, QTextEdit, QTimeEdit,
                               QToolBar, QToolButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

import boswas_fluent as fluent

APP_ID = "com.boswas.FluentGallery"


# --- small helpers of the gallery ------------------------------------------------------------------------

def page(*sections: QWidget) -> QScrollArea:
    """A scrolling page: the sections under each other with the page margins of a NavigationView."""
    area = QScrollArea()
    area.setWidgetResizable(True)
    area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    content = QWidget()
    layout = QVBoxLayout(content)
    layout.setContentsMargins(32, 4, 32, 32)
    layout.setSpacing(4)
    for section in sections:
        layout.addWidget(section)
    layout.addStretch(1)
    area.setWidget(content)
    return area


def section(title: str, *widgets: QWidget, description: str = "") -> QWidget:
    """A titled group of rows."""
    holder = QWidget()
    layout = QVBoxLayout(holder)
    layout.setContentsMargins(0, 16, 0, 0)
    layout.setSpacing(4)
    layout.addWidget(fluent.Label(title, "body-strong"))
    if description:
        layout.addWidget(fluent.Label(description, "caption", tone="secondary", wrap=True))
    layout.addSpacing(4)
    for widget in widgets:
        layout.addWidget(widget)
    return holder


def row(*widgets: QWidget, spacing: int = 12) -> QWidget:
    """Widgets beside each other, left aligned."""
    holder = QWidget()
    layout = QHBoxLayout(holder)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(spacing)
    for widget in widgets:
        layout.addWidget(widget, 0, Qt.AlignmentFlag.AlignVCenter)
    layout.addStretch(1)
    return holder


def card(*widgets: QWidget, title: str = "") -> fluent.Card:
    surface = fluent.Card(title=title)
    for widget in widgets:
        surface.add(widget)
    return surface


def show_state(widget: QWidget, *, hover: bool = False, down: bool = False) -> QWidget:
    """Make a widget draw as hovered or pressed without a pointer (for the rendered pages)."""
    if hover:
        widget.setAttribute(Qt.WidgetAttribute.WA_UnderMouse, True)
    if down and hasattr(widget, "setDown"):
        widget.setDown(True)
    return widget


# --- pages ---------------------------------------------------------------------------------------------------

def typography_page() -> QWidget:
    theme = fluent.theme()
    ramp = QWidget()
    grid = QGridLayout(ramp)
    grid.setContentsMargins(0, 0, 0, 0)
    grid.setHorizontalSpacing(24)
    for index, name in enumerate(("title-large", "title", "subtitle", "body-large", "body-strong", "body",
                                  "caption")):
        style = theme.type(name)
        grid.addWidget(fluent.Label(name, "caption", tone="secondary"), index, 0)
        grid.addWidget(fluent.Label("The quick brown fox", name), index, 1)
        grid.addWidget(fluent.Label(f"{style.size}/{style.line_height} px, weight {style.weight}", "caption",
                                    tone="muted"), index, 2)
    grid.setColumnStretch(3, 1)
    tones = row(*[fluent.Label(tone, "body", tone=tone) for tone in ("secondary", "muted", "accent", "success",
                                                                    "warning", "danger", "info")],
                fluent.Label("default"))
    swatches = QWidget()
    flow = QGridLayout(swatches)
    flow.setContentsMargins(0, 0, 0, 0)
    flow.setSpacing(8)
    for index, name in enumerate(("background", "layer", "surface", "surface-raised", "surface-hover", "chrome",
                                  "accent", "accent-hover", "accent-pressed", "success", "warning", "danger",
                                  "info", "success-tint", "warning-tint", "danger-tint")):
        flow.addWidget(Swatch(name), index // 6, index % 6)
    flow.setColumnStretch(6, 1)
    icon_names = ("go-previous", "go-next", "go-up", "go-down", "view-refresh", "system-search", "list-add",
                  "list-remove", "edit-delete", "document-open", "preferences-system", "open-menu", "window-close",
                  "emblem-ok", "dialog-information", "dialog-warning", "dialog-error", "security-high",
                  "notification", "network-wireless-signal-good", "audio-volume-high", "battery-good",
                  "bluetooth-active", "display-brightness", "night-light", "airplane-mode")
    symbolic = row(*[fluent.IconWidget(fluent.icon(name, symbolic=True), 20) for name in icon_names], spacing=14)
    coloured = row(*[fluent.IconWidget(name, 32) for name in (
        "boswas-control-center", "boswas-files", "boswas-terminal", "accessories-calculator",
        "accessories-text-editor", "utilities-system-monitor", "folder", "folder-documents", "user-trash",
        "drive-harddisk", "computer", "text-plain", "application-pdf", "image-x-generic")], spacing=14)
    glyphs = row(*[fluent.IconWidget(fluent.glyph_icon(name), 16) for name in (
        "chevron-down", "chevron-up", "chevron-left", "chevron-right", "check", "dash", "close", "more", "restore",
        "grip")], spacing=14)
    return page(section("Type ramp", ramp), section("Tones", tones),
                section("Surfaces and status colours", swatches,
                        description="Every colour is a token of desktop/ui/tokens.json."),
                section("Symbolic icons (Boswas Prism), in the text colour", symbolic),
                section("Coloured icons (Boswas Prism)", coloured),
                section("Glyphs the toolkit draws itself", glyphs))


class Swatch(QWidget):
    """A colour sample with its token name."""

    def __init__(self, name: str) -> None:
        super().__init__()
        self._name = name
        self.setFixedSize(108, 64)
        self.setToolTip(name)
        self.setAccessibleName(name)

    def paintEvent(self, _event) -> None:
        from PySide6.QtGui import QPainter
        painter = QPainter(self)
        theme = fluent.theme()
        fluent.paint.rounded_rect(painter, self.rect().adjusted(0, 0, 0, -20), theme.radius("card"), self._name,
                                  "stroke-surface")
        fluent.paint.elided(painter, self.rect().adjusted(0, 46, 0, 0), self._name, fluent.font("caption"),
                            "text-secondary")


def buttons_page(states: bool) -> QWidget:
    standard = fluent.Button("Standard")
    accent = fluent.Button("Accent", accent=True)
    subtle = fluent.Button("Subtle", subtle=True)
    with_icon = fluent.Button("With icon", icon=fluent.icon("document-open", symbolic=True))
    disabled = fluent.Button("Disabled")
    disabled.setEnabled(False)
    accent_disabled = fluent.Button("Disabled accent", accent=True)
    accent_disabled.setEnabled(False)
    toggle = QPushButton("Toggle")
    toggle.setCheckable(True)
    toggle.setChecked(True)
    menu_button = QPushButton("With menu")
    menu = fluent.MenuFlyout(menu_button)
    menu.add_item("First")
    menu.add_item("Second")
    menu_button.setMenu(menu)
    link = fluent.HyperlinkButton("Hyperlink", "https://example.org/")
    link_disabled = fluent.HyperlinkButton("Disabled link")
    link_disabled.setEnabled(False)

    hover = show_state(fluent.Button("Hover"), hover=states)
    pressed = show_state(fluent.Button("Pressed"), hover=states, down=states)
    accent_hover = show_state(fluent.Button("Accent hover", accent=True), hover=states)
    accent_pressed = show_state(fluent.Button("Accent pressed", accent=True), hover=states, down=states)
    subtle_hover = show_state(fluent.Button("Subtle hover", subtle=True), hover=states)

    icon_buttons = [fluent.IconButton(fluent.icon(name, symbolic=True), tip) for name, tip in (
        ("view-refresh", "Refresh"), ("list-add", "Add"), ("edit-delete", "Delete"), ("open-menu", "Menu"))]
    icon_hover = show_state(fluent.IconButton(fluent.icon("system-search", symbolic=True), "Search"), hover=states)
    checked = fluent.IconButton(fluent.icon("preferences-system", symbolic=True), "Checked", checkable=True)
    checked.setChecked(True)
    icon_disabled = fluent.IconButton(fluent.icon("edit-delete", symbolic=True), "Disabled")
    icon_disabled.setEnabled(False)
    tool = QToolButton()
    tool.setText("Tool button")
    tool.setIcon(fluent.icon("document-open", symbolic=True))
    tool.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
    split = QToolButton()
    split.setText("Split")
    split.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
    split.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)
    split.setMenu(menu)

    chips = [fluent.TagChip("Tag"), fluent.TagChip("Filter on", checkable=True), fluent.TagChip("Removable",
                                                                                             closable=True)]
    chips[1].setChecked(True)
    disabled_chip = fluent.TagChip("Disabled")
    disabled_chip.setEnabled(False)
    return page(
        section("Button", row(standard, accent, subtle, with_icon, toggle, menu_button),
                row(hover, pressed, accent_hover, accent_pressed, subtle_hover), row(disabled, accent_disabled)),
        section("HyperlinkButton", row(link, link_disabled)),
        section("IconButton and QToolButton", row(*icon_buttons, icon_hover, checked, icon_disabled, tool, split)),
        section("TagChip", row(*chips, disabled_chip)),
    )


def input_page(states: bool) -> QWidget:
    plain = QLineEdit()
    plain.setPlaceholderText("Placeholder")
    filled = QLineEdit("Text box")
    hovered = show_state(QLineEdit("Hover"), hover=states)
    off = QLineEdit("Disabled")
    off.setEnabled(False)
    read_only = QLineEdit("Read only")
    read_only.setReadOnly(True)
    for widget in (plain, filled, hovered, off, read_only):
        widget.setFixedWidth(180)
    search = fluent.SearchBox()
    search.setFixedWidth(240)
    password = fluent.PasswordBox(placeholder="Password")
    password.setText("secret")
    password.setFixedWidth(200)
    number = fluent.NumberBox(value=42, maximum=1000)
    spin = QSpinBox()
    spin.setValue(7)
    spin_off = QSpinBox()
    spin_off.setEnabled(False)
    combo = QComboBox()
    combo.addItems(["Balanced", "Power saver", "Performance"])
    combo.setMinimumWidth(160)
    editable = QComboBox()
    editable.setEditable(True)
    editable.addItems(["Inter", "Noto Sans", "Lato"])
    editable.setMinimumWidth(160)
    combo_off = QComboBox()
    combo_off.addItems(["Disabled"])
    combo_off.setEnabled(False)
    date = QDateEdit(QDate(2026, 11, 15))
    date.setCalendarPopup(True)
    clock = QTimeEdit()
    text = QTextEdit()
    text.setPlainText("A multi-line text box.\nThe accent line appears when it has the focus.")
    text.setFixedHeight(88)
    code = QPlainTextEdit("Plain text\nsecond line")
    code.setFixedHeight(88)
    code.setEnabled(False)
    return page(
        section("TextBox (QLineEdit)", row(plain, filled, hovered), row(off, read_only)),
        section("SearchBox, PasswordBox", row(search, password)),
        section("NumberBox, QSpinBox", row(number, spin, spin_off)),
        section("ComboBox", row(combo, editable, combo_off)),
        section("Date and time", row(date, clock)),
        section("Multi-line (QTextEdit, QPlainTextEdit disabled)", row(text, code)),
    )


def selection_page(states: bool) -> QWidget:
    boxes = [QCheckBox("Unchecked"), QCheckBox("Checked"), QCheckBox("Indeterminate"), QCheckBox("Disabled"),
             QCheckBox("Disabled checked")]
    boxes[1].setChecked(True)
    boxes[2].setTristate(True)
    boxes[2].setCheckState(Qt.CheckState.PartiallyChecked)
    boxes[3].setEnabled(False)
    boxes[4].setChecked(True)
    boxes[4].setEnabled(False)
    radios_holder = QWidget()
    radios = QHBoxLayout(radios_holder)
    radios.setContentsMargins(0, 0, 0, 0)
    radios.setSpacing(16)
    for index, text in enumerate(("Option one", "Option two", "Disabled")):
        button = QRadioButton(text)
        button.setChecked(index == 0)
        button.setEnabled(index != 2)
        radios.addWidget(button)
    radios.addStretch(1)
    switches = [fluent.ToggleSwitch("Off"), fluent.ToggleSwitch("On", checked=True),
                fluent.ToggleSwitch("Disabled off"), fluent.ToggleSwitch("Disabled on", checked=True),
                fluent.ToggleSwitch("No label", checked=True, show_state=False)]
    switches[2].setEnabled(False)
    switches[3].setEnabled(False)
    slider = QSlider(Qt.Orientation.Horizontal)
    slider.setValue(40)
    slider.setFixedWidth(240)
    slider_off = QSlider(Qt.Orientation.Horizontal)
    slider_off.setValue(70)
    slider_off.setFixedWidth(160)
    slider_off.setEnabled(False)
    vertical = QSlider(Qt.Orientation.Vertical)
    vertical.setValue(60)
    vertical.setFixedHeight(96)
    pivot = fluent.Pivot()
    for key, text in (("all", "All"), ("installed", "Installed"), ("updates", "Updates (3)")):
        pivot.add_item(key, text)
    segmented = fluent.SegmentedControl()
    for key, text in (("light", "Light"), ("dark", "Dark"), ("auto", "Automatic")):
        segmented.add_item(key, text)
    segmented.set_current("dark")
    segmented.setFixedWidth(320)
    return page(
        section("CheckBox", row(*boxes, spacing=20)),
        section("RadioButton", radios_holder),
        section("ToggleSwitch", row(*switches, spacing=28)),
        section("Slider", row(slider, slider_off, vertical, spacing=28)),
        section("Pivot", pivot),
        section("SegmentedControl", row(segmented)),
    )


def status_page() -> QWidget:
    bars = []
    for value in (0, 35, 100):
        bar = QProgressBar()
        bar.setRange(0, 100)
        bar.setValue(value)
        bar.setTextVisible(False)
        bars.append(bar)
    busy = QProgressBar()
    busy.setRange(0, 0)
    busy.setTextVisible(False)
    paused = QProgressBar()
    paused.setValue(50)
    paused.setTextVisible(False)
    paused.setProperty("status", "paused")
    failed = QProgressBar()
    failed.setValue(70)
    failed.setTextVisible(False)
    failed.setProperty("status", "error")
    progress = QWidget()
    form = QFormLayout(progress)
    form.setContentsMargins(0, 0, 0, 0)
    form.setVerticalSpacing(12)
    form.setHorizontalSpacing(24)
    form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
    form.setFormAlignment(Qt.AlignmentFlag.AlignVCenter)
    for label, bar in (("0 %", bars[0]), ("35 %", bars[1]), ("100 %", bars[2]), ("Indeterminate", busy),
                       ("Paused", paused), ("Error", failed)):
        holder = QWidget()
        centred = QVBoxLayout(holder)
        centred.setContentsMargins(0, 8, 0, 8)
        centred.addWidget(bar)
        form.addRow(fluent.Label(label, tone="secondary"), holder)
    ring = fluent.ProgressRing()
    small = fluent.ProgressRing(size=16)
    half = fluent.ProgressRing(size=32, indeterminate=False)
    half.set_value(60)
    full = fluent.ProgressRing(size=48, indeterminate=False)
    full.set_value(100)
    info = fluent.InfoBar("info", "Update available", "Version 1.1 can be installed.", action_text="Install")
    success = fluent.InfoBar("success", "Saved", "Your changes were applied.")
    warning = fluent.InfoBar("warning", "Low disk space", "Less than 2 GB are free on the system volume.")
    error = fluent.InfoBar("error", "Connection lost", "The session service is not running.", closable=False)
    badges = row(fluent.Badge("Neutral"), fluent.Badge("Current", kind="accent"), fluent.Badge("Info", kind="info"),
                 fluent.Badge("Stable", kind="success"), fluent.Badge("Beta", kind="warning"),
                 fluent.Badge("Blocked", kind="danger"), fluent.InfoBadge(), fluent.InfoBadge(3),
                 fluent.InfoBadge(128), fluent.InfoBadge(1, kind="danger"), fluent.InfoBadge(5, kind="success"))
    icons = row(*[fluent.StatusIcon(kind, 20) for kind in ("info", "success", "warning", "danger", "attention",
                                                           "neutral", "busy")], spacing=16)
    skeleton = QWidget()
    lines = QVBoxLayout(skeleton)
    lines.setContentsMargins(0, 0, 0, 0)
    lines.setSpacing(8)
    lines.addWidget(fluent.Skeleton(width=220, height=20))
    lines.addWidget(fluent.Skeleton(width=420))
    lines.addWidget(fluent.Skeleton(width=360))
    return page(
        section("InfoBar", info, success, warning, error),
        section("ProgressBar", progress),
        section("ProgressRing", row(ring, small, half, full, spacing=24)),
        section("Badge, InfoBadge", badges),
        section("StatusIcon", icons, description="Each state has its own shape as well as its own colour."),
        section("Skeleton", skeleton),
    )


def lists_page() -> QWidget:
    plain = QListWidget()
    for name, icon in (("Documents", "folder-documents"), ("Downloads", "folder-download"),
                       ("Pictures", "folder-pictures"), ("Music", "folder-music"), ("Videos", "folder-videos")):
        plain.addItem(QListWidgetItem(fluent.icon(icon), name))
    plain.setCurrentRow(1)
    plain.item(4).setFlags(Qt.ItemFlag.NoItemFlags)
    plain.setFixedSize(240, 196)
    tree = QTreeWidget()
    tree.setHeaderLabels(["Name", "Size", "Kind"])
    for name, children in (("Projects", ("report.odt", "budget.ods")), ("Archive", ("2025.tar", "2024.tar")),
                           ("notes.txt", ())):
        top = QTreeWidgetItem([name, "" if children else "4 KB", "Folder" if children else "Text"])
        top.setIcon(0, fluent.icon("folder" if children else "text-plain"))
        for child in children:
            leaf = QTreeWidgetItem([child, "120 KB", "Document"])
            leaf.setIcon(0, fluent.icon("x-office-document"))
            top.addChild(leaf)
        tree.addTopLevelItem(top)
    tree.topLevelItem(0).setExpanded(True)
    tree.setCurrentItem(tree.topLevelItem(0).child(0))
    tree.setColumnWidth(0, 220)
    tree.setFixedHeight(232)
    table = QTableWidget(4, 4)
    table.setHorizontalHeaderLabels(["Process", "CPU", "Memory", "Status"])
    for r, values in enumerate((("boswas-shell", "1.2 %", "148 MB", "Running"),
                                ("boswas-compositor", "2.8 %", "212 MB", "Running"),
                                ("files", "0.0 %", "64 MB", "Sleeping"), ("terminal", "0.4 %", "38 MB", "Running"))):
        for c, value in enumerate(values):
            table.setItem(r, c, QTableWidgetItem(value))
    table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
    table.setShowGrid(False)
    table.verticalHeader().hide()
    table.horizontalHeader().setStretchLastSection(True)
    table.setSortingEnabled(True)
    table.selectRow(1)
    table.setColumnWidth(0, 220)
    table.setFixedHeight(196)
    rows = QWidget()
    stack = QVBoxLayout(rows)
    stack.setContentsMargins(0, 0, 0, 0)
    stack.setSpacing(2)
    first = fluent.ListItem("Ada Lovelace", "ada@example.org", trailing=fluent.Badge("Admin", kind="accent"))
    first.set_leading(fluent.Avatar("Ada Lovelace"))
    second = fluent.ListItem("Grace Hopper", "grace@example.org")
    second.set_leading(fluent.Avatar("Grace Hopper"))
    second.set_selected(True)
    third = fluent.ListItem("Guest", icon=fluent.icon("avatar-default", symbolic=True))
    for item in (first, second, third):
        stack.addWidget(item)
    rows.setFixedWidth(420)
    return page(
        section("QListView", row(plain, rows, spacing=32),
                description="Left: a standard list. Right: ListItem rows with Avatar and Badge."),
        section("QTreeView", tree),
        section("QTableView", table),
    )


def settings_page() -> QWidget:
    wifi = fluent.SettingsCard("Wi-Fi", "Connected to Boswas-Guest", fluent.icon("network-wireless-signal-good",
                                                                               symbolic=True),
                               fluent.ToggleSwitch(checked=True))
    night = fluent.SettingsCard("Night light", "Warmer colours in the evening",
                                fluent.icon("night-light", symbolic=True), fluent.ToggleSwitch())
    combo = QComboBox()
    combo.addItems(["Balanced", "Best power efficiency", "Best performance"])
    power = fluent.SettingsCard("Power mode", "Optimise the device for power use or performance",
                                fluent.icon("battery-good", symbolic=True), combo)
    display = fluent.SettingsCard("Display", "Monitors, brightness, scale",
                                  fluent.icon("preferences-desktop-display"), clickable=True)
    plain = fluent.SettingsCard("Device name", control=fluent.Button("Rename"))
    disabled = fluent.SettingsCard("Bluetooth", "Not available on this device",
                                   fluent.icon("bluetooth-disabled", symbolic=True), fluent.ToggleSwitch())
    disabled.setEnabled(False)
    expander = fluent.SettingsExpander("Volume", "Output devices and levels",
                                       fluent.icon("audio-volume-high", symbolic=True), expanded=True)
    slider = QSlider(Qt.Orientation.Horizontal)
    slider.setValue(65)
    slider.setFixedWidth(200)
    expander.add_row("Speakers", control=slider)
    expander.add_row("Mono audio", "Combine left and right channels", fluent.ToggleSwitch())
    expander.add_row("More sound settings", clickable=True)
    closed = fluent.SettingsExpander("Advanced", "Rarely needed options")
    closed.add_row("Reset", control=fluent.Button("Reset"))
    generic = fluent.Expander("Expander", description="Any content", expanded=True)
    generic.add_widget(fluent.Label("An Expander holds arbitrary widgets: this label, a form, a list.", wrap=True))
    return page(section("SettingsCard", wifi, night, power, display, plain, disabled),
                section("SettingsExpander", expander, closed), section("Expander", generic))


def commands_page() -> QWidget:
    bar = fluent.CommandBar()
    bar.add_command("New", fluent.icon("list-add", symbolic=True), shortcut="Ctrl+N")
    bar.add_command("Open", fluent.icon("document-open", symbolic=True), shortcut="Ctrl+O")
    bar.add_separator()
    bar.add_command("Delete", fluent.icon("edit-delete", symbolic=True)).setEnabled(False)
    bar.add_command("Refresh", fluent.icon("view-refresh", symbolic=True), shortcut="F5")
    bar.add_command("Properties", primary=False)
    bar.add_command("Select all", primary=False, shortcut="Ctrl+A")
    search = fluent.SearchBox()
    search.setFixedWidth(200)
    bar.add_widget(search)
    crumbs = fluent.BreadcrumbBar(items=["Home", "Documents", "Projects", "Report"])
    title_crumbs = fluent.BreadcrumbBar(items=["System", "Display"], style="title")
    tabs = QTabWidget()
    for name in ("Overview", "Permissions", "Logs"):
        inner = QWidget()
        inner_layout = QVBoxLayout(inner)
        inner_layout.addWidget(fluent.Label(f"{name} content", tone="secondary"))
        inner_layout.addStretch(1)
        tabs.addTab(inner, name)
    tabs.setTabEnabled(2, False)
    tabs.setFixedHeight(120)
    menu_bar = QMenuBar()
    file_menu = menu_bar.addMenu("File")
    file_menu.addAction("New")
    menu_bar.addMenu("Edit")
    menu_bar.addMenu("View")
    tool_bar = QToolBar()
    tool_bar.addAction(fluent.icon("go-previous", symbolic=True), "Back")
    tool_bar.addAction(fluent.icon("go-next", symbolic=True), "Forward")
    tool_bar.addSeparator()
    tool_bar.addAction(fluent.icon("view-refresh", symbolic=True), "Reload")
    status = QStatusBar()
    status.showMessage("3 items selected")
    status.setSizeGripEnabled(False)
    return page(section("CommandBar", card(bar)), section("BreadcrumbBar", crumbs, title_crumbs),
                section("QTabWidget", tabs), section("QMenuBar, QToolBar, QStatusBar", card(menu_bar, tool_bar, status)))


def build_menu(parent: QWidget) -> fluent.MenuFlyout:
    menu = fluent.MenuFlyout(parent)
    menu.add_item("Open", icon=fluent.icon("document-open", symbolic=True), shortcut="Ctrl+O")
    menu.add_item("Refresh", icon=fluent.icon("view-refresh", symbolic=True), shortcut="F5")
    menu.addSeparator()
    menu.add_item("Show hidden files", checkable=True, checked=True)
    menu.add_item("Disabled", enabled=False)
    sort = menu.add_submenu("Sort by")
    sort.add_item("Name")
    sort.add_item("Date")
    menu.addSeparator()
    menu.add_item("Delete", icon=fluent.icon("edit-delete", symbolic=True), shortcut="Del")
    return menu


class DialogsPage(QScrollArea):
    """Buttons that open the dialog and the flyouts (kept as attributes so the gallery can render them)."""

    def __init__(self) -> None:
        super().__init__()
        self.setWidgetResizable(True)
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(32, 4, 32, 32)
        self.dialog_button = fluent.Button("Show ContentDialog")
        self.dialog_button.clicked.connect(self.show_dialog)
        self.flyout_button = fluent.Button("Show Flyout")
        self.flyout_button.clicked.connect(self.show_flyout)
        self.menu_button = fluent.Button("Show MenuFlyout")
        self.menu_button.clicked.connect(lambda: self.menu.show_at(self.menu_button))
        self.tip_button = fluent.Button("Show TeachingTip")
        self.tip_button.clicked.connect(self.show_tip)
        self.answer = fluent.Label("", tone="secondary")
        self.menu = build_menu(self)
        layout.addWidget(section("ContentDialog", row(self.dialog_button, self.answer),
                                 description="Modal inside the window: the window is dimmed, focus stays in the "
                                             "dialog, Escape closes it."))
        layout.addWidget(section("Flyout, MenuFlyout, TeachingTip", row(self.flyout_button, self.menu_button,
                                                                        self.tip_button)))
        tooltip = fluent.Button("Hover for a tool tip")
        tooltip.setToolTip("A tool tip")
        layout.addWidget(section("QToolTip", row(tooltip)))
        layout.addStretch(1)
        self.setWidget(content)

    def make_dialog(self) -> fluent.ContentDialog:
        return fluent.ContentDialog(self, "Delete 3 files?", "The files are moved to the bin. You can restore them "
                                    "from there until the bin is emptied.", primary="Delete", secondary="Keep copies",
                                    close="Cancel")

    def show_dialog(self) -> None:
        dialog = self.make_dialog()
        result = dialog.exec()
        self.answer.setText(f"Result: {result.name.lower()}")
        dialog.deleteLater()

    def make_flyout(self) -> fluent.Flyout:
        flyout = fluent.Flyout(self.window())
        flyout.add_widget(fluent.Label("Empty the bin?", "body-strong"))
        flyout.add_widget(fluent.Label("All items will be deleted for good.", tone="secondary"))
        confirm = fluent.Button("Empty bin", accent=True)
        confirm.clicked.connect(flyout.close)
        flyout.add_widget(confirm)
        return flyout

    def show_flyout(self) -> None:
        self.make_flyout().show_at(self.flyout_button)

    def make_tip(self) -> fluent.TeachingTip:
        return fluent.TeachingTip("Pin what you use most", "Pinned applications stay on the taskbar.", self.window(),
                                  action_text="Got it")

    def show_tip(self) -> None:
        tip = self.make_tip()
        tip.action_triggered.connect(tip.close)
        tip.show_at(self.tip_button)


def containers_page() -> QWidget:
    plain = card(fluent.Label("A Card groups content on the card surface.", wrap=True),
                 row(fluent.Button("Action"), fluent.HyperlinkButton("Learn more")), title="Card")
    group = QGroupBox("QGroupBox")
    form = QFormLayout(group)
    form.addRow("Name", QLineEdit("boswas"))
    form.addRow("Start at login", QCheckBox())
    checkable = QGroupBox("Checkable group")
    checkable.setCheckable(True)
    inner = QVBoxLayout(checkable)
    inner.addWidget(QRadioButton("Option"))
    splitter = QSplitter()
    left = QListWidget()
    left.addItems(["One", "Two", "Three"])
    right = QPlainTextEdit("Drag the divider between the two panes.")
    splitter.addWidget(left)
    splitter.addWidget(right)
    splitter.setFixedHeight(120)
    calendar = QCalendarWidget()
    calendar.setSelectedDate(QDate(2026, 11, 15))
    calendar.setFixedSize(320, 260)
    calendar.setVerticalHeaderFormat(QCalendarWidget.VerticalHeaderFormat.NoVerticalHeader)
    empty = fluent.EmptyState("No results", "Nothing matches your search. Try another word.",
                              fluent.icon("system-search", symbolic=True), "Clear search")
    empty.setFixedHeight(240)
    people = row(fluent.Avatar("Ada Lovelace", size=48), fluent.Avatar("Grace Hopper", size=40),
                 fluent.Avatar("linus", size=32), fluent.Avatar("", size=32), spacing=16)
    dock_host = QMainWindow()
    dock_host.setWindowFlags(Qt.WindowType.Widget)
    dock = QDockWidget("QDockWidget")
    dock.setWidget(fluent.Label("  Docked content", tone="secondary"))
    dock_host.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, dock)
    dock_host.setCentralWidget(fluent.Label("  Central widget", tone="secondary"))
    dock_host.setFixedHeight(96)
    return page(section("Card", plain), section("QGroupBox", row(group, checkable, spacing=16)),
                section("QSplitter", splitter), section("QCalendarWidget, EmptyState", row(calendar, card(empty))),
                section("Avatar", people), section("QDockWidget", dock_host))


# --- the window ---------------------------------------------------------------------------------------------

PAGES = (
    ("typography", "Typography & colour", "preferences-desktop-font"),
    ("buttons", "Buttons", "preferences-desktop-mouse"),
    ("input", "Text input", "preferences-desktop-keyboard"),
    ("selection", "Selection", "preferences-desktop-accessibility"),
    ("status", "Status & progress", "preferences-desktop-notification-bell"),
    ("lists", "Lists & tables", "system-file-manager"),
    ("settings", "Settings rows", "preferences-system"),
    ("commands", "Commands & navigation", "utilities-terminal"),
    ("dialogs", "Dialogs & flyouts", "help-browser"),
    ("containers", "Containers", "boswas-storage"),
)


class GalleryWindow(fluent.FluentWindow):
    def __init__(self, states: bool = False) -> None:
        super().__init__(APP_ID, "Boswas Fluent Gallery", navigation={"search": True, "back_button": True,
                                                                       "pane_toggle": True},
                         icon="preferences-desktop-theme-global", size=(1180, 820), remember_geometry=not states)
        nav = self.navigation
        builders = {
            "typography": typography_page, "buttons": lambda: buttons_page(states),
            "input": lambda: input_page(states), "selection": lambda: selection_page(states),
            "status": status_page, "lists": lists_page, "settings": settings_page, "commands": commands_page,
            "dialogs": DialogsPage, "containers": containers_page,
        }
        nav.add_section("Basics")
        for index, (key, title, icon) in enumerate(PAGES):
            if index == 4:
                nav.add_section("Collections and layout")
            nav.add_page(key, title, icon=icon, factory=builders[key])
        nav.set_item_badge("status", 4)
        nav.add_action("scheme", "Switch light and dark", icon=fluent.icon("night-light", symbolic=True))
        nav.add_page("about", "About", fluent.EmptyState(
            "Boswas Fluent", f"Toolkit version {fluent.__version__}. Typeface: {fluent.manager().font_family()}.",
            "boswas-logo"), icon=fluent.icon("dialog-information", symbolic=True), footer=True)
        nav.item_invoked.connect(self._invoked)
        self.scheme_switch = fluent.SegmentedControl()
        self.scheme_switch.add_item("dark", "Dark")
        self.scheme_switch.add_item("light", "Light")
        self.scheme_switch.set_current(fluent.manager().scheme)
        self.scheme_switch.setFixedWidth(160)
        self.scheme_switch.current_changed.connect(fluent.manager().set_scheme)
        nav.add_header_widget(self.scheme_switch)
        # A method of the window, not a lambda: Qt drops the connection when the window is destroyed.
        fluent.manager().changed.connect(self._scheme_changed)
        self.add_shortcut("find", nav.focus_search)
        self.add_shortcut("quit", self.close)

    def _scheme_changed(self, theme: fluent.Theme) -> None:
        self.scheme_switch.set_current(theme.scheme)

    def _invoked(self, key: str) -> None:
        if key == "scheme":
            fluent.manager().set_scheme("light" if fluent.manager().scheme == "dark" else "dark")


# --- rendering to files -------------------------------------------------------------------------------------

def grab_all(directory: Path, size: QSize) -> list[Path]:
    """Render every page in both schemes, plus the dialog, a menu, the compact pane and the focus ring."""
    from boswas_fluent import testing

    written: list[Path] = []
    testing.app()
    fluent.manager().set_reduced_motion(True)        # still pictures: nothing mid-animation
    for scheme in ("dark", "light"):
        testing.set_scheme(scheme)
        window = GalleryWindow(states=True)
        window.resize(size)
        window.show()
        testing.process(50)
        nav = window.navigation
        for key, _title, _icon in PAGES:
            nav.set_current(key)
            testing.process(30)
            written.append(testing.grab(window, directory / f"{scheme}-{key}.png"))
        # The dialog, open over the dialogs page.
        nav.set_current("dialogs")
        testing.process(20)
        dialogs_page = nav.page("dialogs")
        dialog = dialogs_page.make_dialog()
        dialog.open()
        testing.process(30)
        written.append(testing.grab(window, directory / f"{scheme}-dialog.png"))
        dialog.reject()
        dialog.deleteLater()
        # Popups are windows of their own.
        menu = dialogs_page.menu
        menu.popup(window.mapToGlobal(window.rect().center()))
        testing.process(30)
        menu.setActiveAction(menu.actions()[1])
        written.append(testing.grab(menu, directory / f"{scheme}-menu.png"))
        menu.hide()
        for name, popup in (("flyout", dialogs_page.make_flyout()), ("teachingtip", dialogs_page.make_tip())):
            popup.show_at(dialogs_page.flyout_button)
            testing.process(30)
            written.append(testing.grab(popup, directory / f"{scheme}-{name}.png"))
            popup.close()
            popup.deleteLater()
        combo = QComboBox(window)
        combo.addItems(["Balanced", "Power saver", "Performance"])
        combo.setFixedWidth(200)
        combo.show()
        combo.showPopup()
        testing.process(30)
        written.append(testing.grab(combo.view().window(), directory / f"{scheme}-combo-popup.png"))
        combo.hidePopup()
        combo.hide()
        combo.setParent(None)
        combo.deleteLater()
        # The keyboard focus ring.
        nav.set_current("buttons")
        testing.process(20)
        window.activateWindow()
        buttons = nav.page("buttons").findChildren(QPushButton)
        if buttons:
            polisher = fluent.manager()._polisher
            polisher.keyboard = True
            buttons[1].setFocus(Qt.FocusReason.TabFocusReason)
            polisher.ring.attach(buttons[1])
            testing.process(30)
            written.append(testing.grab(window, directory / f"{scheme}-focus.png"))
            polisher.keyboard = False
            polisher.ring.detach()
        # The pane collapsed to icons.
        nav.set_current("settings")
        window.resize(700, size.height())
        testing.process(50)
        written.append(testing.grab(window, directory / f"{scheme}-compact.png"))
        window.close()
        window.deleteLater()
        testing.process(20)
    fluent.manager().set_reduced_motion(None)
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m boswas_fluent.gallery",
                                     description="Every widget of boswas_fluent, in both schemes.")
    parser.add_argument("--scheme", choices=("dark", "light"), help="start in this scheme instead of the desktop's")
    parser.add_argument("--grab", metavar="DIR", help="render every page to PNG files in DIR and exit")
    parser.add_argument("--size", metavar="WxH", default="1180x820", help="window size for --grab (default 1180x820)")
    args, qt_args = parser.parse_known_args((sys.argv if argv is None else argv)[1:])
    if args.grab:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        try:
            width, height = (int(part) for part in args.size.lower().split("x", 1))
        except ValueError:
            parser.error("--size must look like 1180x820")
        for path in grab_all(Path(args.grab), QSize(width, height)):
            print(path)
        return 0

    def factory(_arguments: list[str]) -> QWidget:
        if args.scheme:
            fluent.manager().set_scheme(args.scheme)
        return GalleryWindow()

    return fluent.run(APP_ID, factory, argv=[(sys.argv if argv is None else argv)[0], *qt_args],
                      display_name="Boswas Fluent Gallery", icon="preferences-desktop-theme-global",
                      single_instance=False)


if __name__ == "__main__":
    sys.exit(main())
