"""Home: the device at a glance and the settings used most.

At the top the device: a picture of its kind (laptop, desktop computer,
virtual machine), its name with Rename, its maker and model; beside it the
person signed in, the network connection and Boswas Update, each a tile
that opens its place. Below, cards in two columns (one when the window is
narrow): Recommended settings (dark mode and night light as switches that
apply at once, and links to Display and Sound), Bluetooth devices, the
background and theme, and how full the storage is.

Everything is read in the background and nothing privileged runs: the
device and the account from public files (device.py), the network and
Bluetooth from the services bridge the quick settings use, storage and the
update settings from the window's store, the background from the
compositor's configuration. The two switches write the user's appearance
and compositor files, as the Colours and Display pages do.
"""

from __future__ import annotations

import os

import boswas_fluent as fluent
from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import QBoxLayout, QGridLayout, QHBoxLayout, QLabel, QProgressBar, QVBoxLayout, QWidget
from shiboken6 import isValid

from .. import compositorconfig as cc
from .. import servicebridge as sb
from .. import viewmodel as vm
from .. import wallpapers
from . import bridgeclient
from .base import Page
from .common import icon
from .settings.connectivity import BLUETOOTH_ICONS, BLUETOOTH_KINDS
from .settings.display import ConfigSection
from .settings.personalize import load_thumbnail

TWO_COLUMNS = 700           # content width from which the cards stand in two columns
NARROW_HERO = 620           # content width under which the tiles go below the device
DEVICE_PICTURE = 88
MAX_BLUETOOTH = 4


def go(widget: QWidget, key: str) -> None:
    """Show a place in the window that holds ``widget``."""
    window = widget.window()
    if hasattr(window, "show_page"):
        window.show_page(key)


class Tile(fluent.SettingsCard):
    """A small card of the top row that opens a place."""

    def __init__(self, title: str, icon_name: str, key: str, parent: QWidget | None = None):
        super().__init__(title, "", fluent.icon(icon_name), None, parent, clickable=True)
        self.key = key
        self.clicked.connect(lambda: go(self, self.key))


class RecommendedSection(ConfigSection):
    """Dark mode and night light as switches, and the pages used most."""

    title = "Recommended settings"

    def build(self) -> None:
        self.dark = fluent.ToggleSwitch()
        self.card("Dark mode", "Dark windows, taskbar and applications", "weather-clear-night-symbolic", self.dark)
        self.dark.clicked.connect(lambda on: self.change({("appearance", "scheme"): "dark" if on else "light"}))
        self.night = self.toggle("Night light", "Warmer colours in the evening", "night-light-symbolic",
                                 "night_light", "enabled")
        for title, description, icon_name, key in (
                ("Display", "Brightness, resolution, scale", "video-display-symbolic", "display"),
                ("Sound", "Volume, output and input", "audio-volume-high-symbolic", "sound")):
            row = self.add(fluent.SettingsCard(title, description, fluent.icon(icon_name), None, self, clickable=True))
            row.clicked.connect(lambda key=key: go(self, key))

    def perform(self, changes: dict) -> object:
        scheme = changes.get(("appearance", "scheme"))
        if scheme is not None:
            # A Boswas wallpaper comes as light and dark artwork: it follows the mode (as on Colours).
            current = self.config.snapshot().get("appearance", "wallpaper")
            other = wallpapers.counterpart(current, scheme)
            if other and other != current:
                changes = {**changes, ("appearance", "wallpaper"): other}
        return self.config.apply(changes)

    def show_state(self, snapshot: cc.Snapshot) -> None:
        self.dark.setChecked(snapshot.get("appearance", "scheme") == "dark")
        self.night.setChecked(bool(snapshot.get("night_light", "enabled")))


class HomeCard(fluent.Card):
    """A card of the grid: a title, its content and a link at the bottom."""

    def __init__(self, title: str, parent: QWidget | None = None):
        super().__init__(parent, title=title, padding=16, spacing=10)
        self.link: fluent.SettingsCard | None = None

    def add_link(self, text: str, key: str) -> fluent.SettingsCard:
        self.link = fluent.SettingsCard(text, "", None, None, self, clickable=True)
        self.link.clicked.connect(lambda: go(self, key))
        self.add(self.link)
        return self.link


class Grid(QWidget):
    """Cards in two columns, or in one when there is not enough room."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.grid = QGridLayout(self)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setHorizontalSpacing(12)
        self.grid.setVerticalSpacing(12)
        self.cards: list[QWidget] = []
        self.columns = 0

    def add(self, card: QWidget) -> QWidget:
        self.cards.append(card)
        self.columns = 0
        self._place()
        return card

    def _place(self) -> None:
        columns = 2 if self.width() >= TWO_COLUMNS else 1
        if columns == self.columns:
            return
        self.columns = columns
        for card in self.cards:
            self.grid.removeWidget(card)
        for index, card in enumerate(self.cards):
            self.grid.addWidget(card, index // columns, index % columns, Qt.AlignmentFlag.AlignTop)
        for column in range(2):
            self.grid.setColumnStretch(column, 1 if column < columns else 0)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._place()


class Hero(QWidget):
    """The device on the left; the account, network and update tiles on the right (below when narrow)."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.box = QBoxLayout(QBoxLayout.Direction.LeftToRight, self)
        self.box.setContentsMargins(0, 0, 0, 0)
        self.box.setSpacing(12)
        self.device = fluent.Card(self, padding=20, spacing=6)
        row = QHBoxLayout()
        row.setSpacing(20)
        self.picture = fluent.IconWidget(fluent.icon("computer"), DEVICE_PICTURE, self.device)
        self.picture.setAccessibleName("Picture of this device")
        text = QVBoxLayout()
        text.setSpacing(2)
        self.name = fluent.Label("", "subtitle", self.device, wrap=True, selectable=True)
        self.name.setAccessibleName("Device name")
        self.model = fluent.Label("", "body", self.device, tone="secondary", wrap=True)
        self.rename_button = fluent.Button("Rename", self.device)
        self.rename_button.setAccessibleName("Rename this device")
        text.addStretch(1)
        text.addWidget(self.name)
        text.addWidget(self.model)
        text.addSpacing(8)
        text.addWidget(self.rename_button, 0, Qt.AlignmentFlag.AlignLeft)
        text.addStretch(1)
        row.addWidget(self.picture, 0, Qt.AlignmentFlag.AlignVCenter)
        row.addLayout(text, 1)
        self.device.add_layout(row)
        self.tiles = QWidget(self)
        column = QVBoxLayout(self.tiles)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(4)
        self.account = Tile("", "avatar-default-symbolic", "users", self.tiles)
        self.network = Tile("Network", "network-wired-symbolic", "network", self.tiles)
        self.updates = Tile("Boswas Update", "software-update-available-symbolic", "updates", self.tiles)
        for tile in (self.account, self.network, self.updates):
            column.addWidget(tile)
        column.addStretch(1)
        self.box.addWidget(self.device, 3)
        self.box.addWidget(self.tiles, 2)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        direction = QBoxLayout.Direction.TopToBottom if self.width() < NARROW_HERO else \
            QBoxLayout.Direction.LeftToRight
        if self.box.direction() != direction:
            self.box.setDirection(direction)


class HomePage(Page):
    data_keys = ("device", "account", "storage", "updates", "agent_config", "live", "presets")
    passive_keys = ("upgradable",)

    def build(self) -> None:
        self.description.hide()
        self.hero = Hero(self.content)
        self.hero.rename_button.clicked.connect(self.rename_device)
        self.body.addWidget(self.hero)
        self.grid = Grid(self.content)
        self.body.addWidget(self.grid)

        self.recommended_card = HomeCard("Recommended settings")
        self.recommended = RecommendedSection(self.recommended_card)
        self.recommended_card.add(self.recommended)
        self.grid.add(self.recommended_card)

        self.bluetooth_card = HomeCard("Bluetooth devices")
        self.bluetooth_list = QWidget(self.bluetooth_card)
        self.bluetooth_rows = QVBoxLayout(self.bluetooth_list)
        self.bluetooth_rows.setContentsMargins(0, 0, 0, 0)
        self.bluetooth_rows.setSpacing(4)
        self.bluetooth_text = fluent.Label("Reading…", "body", self.bluetooth_card, tone="secondary", wrap=True)
        self.bluetooth_card.add(self.bluetooth_text)
        self.bluetooth_card.add(self.bluetooth_list)
        self.bluetooth_card.add_link("View all devices", "bluetooth")
        self.grid.add(self.bluetooth_card)

        self.look_card = HomeCard("Personalize your device")
        self.background = QLabel(self.look_card)
        self.background.setFixedSize(QSize(240, 135))
        self.background.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.background.setAccessibleName("Your background")
        self.look_text = fluent.Label("", "body", self.look_card, tone="secondary", wrap=True)
        self.look_card.add(self.background)
        self.look_card.add(self.look_text)
        self.look_card.add_link("Background", "background")
        theme = fluent.SettingsCard("Themes", "", None, None, self.look_card, clickable=True)
        theme.clicked.connect(lambda: go(self, "themes"))
        self.look_card.add(theme)
        self.grid.add(self.look_card)

        self.storage_card = HomeCard("Storage")
        self.storage_text = fluent.Label("Reading disk usage…", "body", self.storage_card, wrap=True)
        self.storage_bar = QProgressBar(self.storage_card)
        self.storage_bar.setRange(0, 100)
        self.storage_bar.setTextVisible(False)
        self.storage_bar.setFixedHeight(10)
        self.storage_bar.setAccessibleName("Space used on the system disk")
        self.storage_card.add(self.storage_text)
        self.storage_card.add(self.storage_bar)
        self.storage_card.add_link("Storage settings", "storage")
        self.grid.add(self.storage_card)

        self.bridge = None
        self.wallpaper: str | None = None
        self.theme_name = ""
        self._generation = 0

    # --- life cycle ---
    def activate(self, force: bool = False) -> None:
        super().activate(force)
        if self.bridge is None:
            self.bridge = bridgeclient.for_widget(self)
            self.bridge.network_changed.connect(self._bridge_changed)
            self.bridge.bluetooth_changed.connect(self._bridge_changed)
            self.bridge.status_changed.connect(self._bridge_changed)
        self.bridge.start()
        self.recommended.activate()
        self._render_bridge()
        self._load_background()

    def deactivate(self) -> None:
        super().deactivate()
        self.recommended.deactivate()
        self._generation += 1

    def _changed(self, key: str) -> None:
        if key in self.passive_keys:
            self._render_all()
        else:
            super()._changed(key)

    # --- the store ---
    def render(self) -> None:
        device = self.store.value("device")
        if device is not None:
            self.hero.name.setText(device.name)
            self.hero.model.setText(device.model_text if device.model_text == device.kind_label
                                    else f"{device.model_text} · {device.kind_label}")
            self.hero.picture.set_icon(icon(*device.icons))
        account = self.store.value("account")
        if account is not None:
            self.hero.account.set_title(account.display_name)
            self.hero.account.set_description("Your account" + (f" · {account.user}" if account.real_name else ""))
            picture = QIcon(account.picture) if account.picture else QIcon()
            self.hero.account.set_icon(picture if not picture.isNull() else fluent.icon("avatar-default-symbolic"),
                                       32 if not picture.isNull() else 20)
        self._render_updates()
        self._render_storage()
        presets = self.store.value("presets")
        current = None
        if presets is not None and presets.current:
            current = next((p.name for p in presets.presets if p.id == presets.current), None)
        self.theme_name = current or "the Boswas default look"
        self._look_text()

    def _render_updates(self) -> None:
        result = self.store.get("updates")
        tile = self.hero.updates
        if result is None:
            tile.set_description("Reading the update settings…")
            return
        settings = vm.device_settings(self.store.value("agent_config"))
        view = vm.updates_view(result.value if result.ok else None, settings.get("UPDATE_POLICY"),
                               self.store.value("live") is True)
        text = view.summary.headline
        plan = self.store.value("upgradable")
        if plan is not None and plan.upgrades:
            count = len(plan.upgrades)
            text = f"{count} update{'s' if count != 1 else ''} pending · {text}"
        last = dict(view.rows).get("Last refresh")
        tile.set_description(text + (f" · Last checked: {last}" if last else ""))

    def _render_storage(self) -> None:
        result = self.store.get("storage")
        rows = vm.storage_rows(result.value if result is not None and result.ok else [])
        if not rows:
            self.storage_text.setText("Reading disk usage…" if result is None else "Disk usage is not available.")
            self.storage_bar.hide()
            return
        row = rows[0]
        self.storage_text.setText(f"{row.title}: {row.text}")
        self.storage_bar.setValue(row.percent_used)
        self.storage_bar.setAccessibleDescription(f"{row.percent_used}% used")
        self.storage_bar.show()

    # --- the services bridge ---
    def _bridge_changed(self, _state=None) -> None:
        if self.active:
            self._render_bridge()

    def _render_bridge(self) -> None:
        if self.bridge is None:
            return
        running = self.bridge.status() == sb.RUNNING
        network: sb.NetworkState = self.bridge.state(sb.NETWORK)
        tile = self.hero.network
        if not running or not network.available:
            tile.set_title("Network")
            tile.set_description("Not available" if self.bridge.status() == sb.UNAVAILABLE or running else
                                 "Reading…")
            tile.set_icon(fluent.icon("network-offline-symbolic"), 20)
        elif network.wifi_connected:
            tile.set_title(network.wifi_connected)
            tile.set_description("Connected over Wi-Fi" + (" · no internet" if network.connectivity in
                                                            ("none", "limited") else ""))
            tile.set_icon(fluent.icon("network-wireless-symbolic"), 20)
        elif network.wired:
            tile.set_title("Ethernet")
            tile.set_description("Connected")
            tile.set_icon(fluent.icon("network-wired-symbolic"), 20)
        else:
            tile.set_title("Not connected")
            tile.set_description("Wi-Fi is off" if network.wifi_present and not network.wifi_enabled else
                                 "Choose a network")
            tile.set_icon(fluent.icon("network-offline-symbolic"), 20)
        tile.key = "wifi" if network.wifi_present and not network.wired else "network"
        bluetooth: sb.BluetoothState = self.bridge.state(sb.BLUETOOTH)
        while self.bluetooth_rows.count():
            old = self.bluetooth_rows.takeAt(0).widget()
            if old is not None:
                old.hide()
                old.deleteLater()
        if not running or not bluetooth.available:
            self.bluetooth_text.setText("Bluetooth is not available on this device." if running or
                                        self.bridge.status() == sb.UNAVAILABLE else "Reading…")
            self.bluetooth_text.show()
            return
        paired = sorted((d for d in bluetooth.devices if d.paired), key=lambda d: (not d.connected, d.name.casefold()))
        if not bluetooth.enabled:
            self.bluetooth_text.setText("Bluetooth is off.")
        elif not paired:
            self.bluetooth_text.setText("No device is paired yet.")
        self.bluetooth_text.setVisible(not bluetooth.enabled or not paired)
        for device in paired[:MAX_BLUETOOTH]:
            state = "Connected" if device.connected else "Paired"
            if device.connected and device.battery is not None:
                state += f" · battery {device.battery} %"
            kind = BLUETOOTH_KINDS.get(device.icon, "")
            row = fluent.SettingsCard(device.name or device.address, f"{kind} · {state}" if kind else state,
                                      fluent.icon(BLUETOOTH_ICONS.get(device.icon, "bluetooth-active-symbolic")),
                                      None, self.bluetooth_list)
            self.bluetooth_rows.addWidget(row)

    # --- the background ---
    def _load_background(self) -> None:
        self._generation += 1
        generation = self._generation

        def read() -> tuple[str, object]:
            path = cc.default().snapshot().get("appearance", "wallpaper") or ""
            image = None
            if path and os.path.isfile(path):
                try:
                    image = load_thumbnail(path)
                except (OSError, ValueError):
                    image = None
            return path, image

        def done(result) -> None:
            if isValid(self) and generation == self._generation:
                self._show_background(*result)

        fluent.run_in_thread(read, on_done=done, on_error=lambda _exc: done(("", None)))

    def _look_text(self) -> None:
        background = "the default background"
        if self.wallpaper:
            inside = os.path.relpath(self.wallpaper, wallpapers.SYSTEM_DIR)
            background = inside.split(os.sep)[0].replace("-", " ") if not inside.startswith("..") else \
                os.path.basename(self.wallpaper)
        lines = ([f"Theme: {self.theme_name}"] if self.theme_name else []) + \
            ([f"Background: {background}"] if self.wallpaper is not None else [])
        self.look_text.setText("\n".join(lines))

    def _show_background(self, path: str, image) -> None:
        self.wallpaper = path
        self._look_text()
        if image is not None and not image.isNull():
            pixmap = QPixmap.fromImage(image).scaled(self.background.size(),
                                                     Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                                                     Qt.TransformationMode.SmoothTransformation)
            self.background.setPixmap(pixmap)
            self.background.setToolTip(path)
        else:
            self.background.setPixmap(fluent.pixmap(fluent.icon("preferences-desktop-wallpaper"), 64))
            self.background.setToolTip("The default background")

    # --- actions ---
    def rename_device(self) -> None:
        self.window_.show_page("about", "device-name")
        section = self.window_.page("about").section("device-name")
        if section is not None:
            section.rename_device()
