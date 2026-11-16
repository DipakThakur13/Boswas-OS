"""System, application and privacy settings that use the system's own tools and files.

Page "system":
  Date & time           the clock, "Set the time automatically" (timedatectl
                        set-ntp), the time zone with a searchable list
                        (set-timezone) and, while automatic time is off,
                        setting the clock by hand (set-time)
  Region & language     the system language and the regional format, from the
                        locales that are generated (localectl set-locale);
                        they apply after signing in again
  Notifications         one line: where Do not disturb is
Page "applications":
  Default applications  per kind of file or link, the installed applications
                        that declare it; written to ~/.config/mimeapps.list
  Start-up applications the autostart entries of this desktop: on/off, add an
                        installed application, remove one the user added
Page "privacy":
  Recent files          empties the shared list of recently used files

timedatectl and localectl are run on a worker thread with a long time-out:
systemd's services ask polkit, and the shell's agent shows the question.
Files are written by userfiles only.
"""

from __future__ import annotations

import os
import zoneinfo
from datetime import datetime, timezone

import boswas_fluent as fluent
from PySide6.QtCore import QDate, QDateTime, QFileSystemWatcher, QLocale, Qt, QTime, QTimer, Signal
from PySide6.QtWidgets import QComboBox, QDateTimeEdit, QListWidget, QListWidgetItem, QWidget

from ... import commands, defaultapps, timedate, userfiles
from ...errors import Failed, user_message
from . import context
from .widgets import RowList, Section, set_checked

CHANGE_TIMEOUT = 180.0         # a change may wait for the person to answer polkit's question
READ_TIMEOUT = 15.0
POLL_MS = 20000
DENIED_WORDS = ("access denied", "not authorized", "authentication is required", "permission denied",
                "interactive authentication required")


def tool_failure(what: str, result: commands.Completed) -> Failed:
    """The error of a timedatectl or localectl call that did not return 0, in words for the user."""
    detail = " ".join((result.stderr or result.stdout or "").split())[:300]
    if any(word in detail.lower() for word in DENIED_WORDS):
        return Failed(f"{what} was not allowed: administrator authorization was not given.")
    return Failed(f"{what} failed" + (f": {detail}" if detail else "."))


class ToolSection(Section):
    """A section whose state comes from blocking calls: they run on a worker thread, one load at a time."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.loading = False
        self.busy = False              # a change is being made
        self._again = False
        self._generation = 0
        self._poll = QTimer(self)
        self._poll.setInterval(POLL_MS)
        self._poll.timeout.connect(self.reload)

    def read(self):
        """Blocking: the state to show."""
        raise NotImplementedError

    def show_state(self, state) -> None:
        raise NotImplementedError

    def load_failed(self, exc: Exception) -> None:
        self.show_error(user_message(exc))

    def activate(self) -> None:
        if self.active:
            return
        self.active = True
        self.reload()
        self._poll.start()

    def deactivate(self) -> None:
        self.active = False
        self._poll.stop()
        self._generation += 1

    def reload(self) -> None:
        if self.loading or self.busy:
            self._again = self.loading
            return
        self.loading, self._again = True, False
        self._generation += 1
        generation = self._generation
        fluent.run_in_thread(self.read, lambda state: self._loaded(generation, state, None),
                             lambda exc: self._loaded(generation, None, exc))

    def _loaded(self, generation: int, state, exc) -> None:
        self.loading = False
        if generation != self._generation or not self.active:
            return
        self.rendering = True
        try:
            if exc is None:
                self.show_state(state)
            else:
                self.load_failed(exc)
        finally:
            self.rendering = False
        if self._again:
            self.reload()

    def change(self, action, done_text: str = "") -> None:
        """Run a blocking change, then read the state again; a failure is said in the error bar."""
        if self.busy:
            return
        self.busy = True
        self.clear_error()
        self.set_enabled(False)

        def finished(exc) -> None:
            self.busy = False
            if not self.active:
                return
            self.set_enabled(True)
            if exc is not None:
                self.show_error(user_message(exc))
            elif done_text:
                self.show_notice(done_text, "success")
            self.reload()              # the controls show what is in use now, changed or not

        fluent.run_in_thread(action, lambda _result: finished(None), finished)

    def set_enabled(self, enabled: bool) -> None:
        """Enable or disable the controls while a change is on its way."""


# --- date and time --------------------------------------------------------------------------------------------

class ZoneDialog(fluent.ContentDialog):
    """A searchable list of the time zones."""

    def __init__(self, parent: QWidget, zones: list[str], current: str):
        super().__init__(parent, "Time zone", "", primary="Set time zone", close="Cancel")
        self.zones = zones
        self.search = fluent.SearchBox(self, placeholder="Search for a city or region")
        self.search.setAccessibleName("Search time zones")
        self.list = QListWidget(self)
        self.list.setAccessibleName("Time zones")
        self.list.setMinimumHeight(320)
        self.list.setMinimumWidth(420)
        self.content_layout.addWidget(self.search)
        self.content_layout.addWidget(self.list)
        self.search.textChanged.connect(self.filter)
        self.list.currentItemChanged.connect(lambda *_a: self.primary_button.setEnabled(self.chosen() is not None))
        self.list.itemActivated.connect(lambda _item: self.accept())
        self._now = datetime.now(timezone.utc)
        self.filter("")
        self.select(current)

    def filter(self, query: str) -> None:
        chosen = self.chosen()
        self.list.clear()
        for zone in timedate.search_timezones(query, self.zones):
            item = QListWidgetItem(timedate.zone_label(zone, self._now))
            item.setData(Qt.ItemDataRole.UserRole, zone)
            self.list.addItem(item)
        if chosen is not None:
            self.select(chosen)
        elif query and self.list.count():
            self.list.setCurrentRow(0)
        self.primary_button.setEnabled(self.chosen() is not None)

    def select(self, zone: str) -> None:
        for row in range(self.list.count()):
            if self.list.item(row).data(Qt.ItemDataRole.UserRole) == zone:
                self.list.setCurrentRow(row)
                self.list.scrollToItem(self.list.item(row), QListWidget.ScrollHint.PositionAtCenter)
                return

    def chosen(self) -> str | None:
        item = self.list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item is not None else None

    def open(self) -> None:
        super().open()
        self.search.setFocus(Qt.FocusReason.PopupFocusReason)

    def keyPressEvent(self, event) -> None:
        if event.key() in (Qt.Key.Key_Down, Qt.Key.Key_Up) and self.search.hasFocus() and self.list.count():
            self.list.setFocus(Qt.FocusReason.OtherFocusReason)     # from the search box into the results
            return
        super().keyPressEvent(event)


class DateTimeSection(ToolSection):
    page = "system"
    title = "Date & time"
    order = 10
    keywords = ("date", "time", "clock", "time zone", "timezone", "ntp", "automatic time", "set time")

    def __init__(self, parent: QWidget | None = None, *, runner=None, zones: list[str] | None = None):
        super().__init__(parent)
        self.runner = runner or context.runner()
        self.zones = zones
        self.state = timedate.TimeState()
        self.clock = self.add(fluent.SettingsCard("", "", fluent.icon("preferences-system-time-symbolic"), None,
                                                  self))
        self.clock.title_label.set_style("subtitle")
        self.auto = fluent.ToggleSwitch()
        self.auto_card = self.add(fluent.SettingsCard("Set the time automatically", "", None, self.auto, self))
        self.auto.toggled.connect(self._auto_toggled)
        self.zone_button = fluent.Button("Change…", self)
        self.zone_button.setAccessibleName("Change the time zone")
        self.zone_card = self.add(fluent.SettingsCard("Time zone", "", None, self.zone_button, self))
        self.zone_button.clicked.connect(self.choose_zone)
        self.manual_button = fluent.Button("Change…", self)
        self.manual_button.setAccessibleName("Set the date and time manually")
        self.manual_card = self.add(fluent.SettingsCard("Set the date and time manually", "", None,
                                                        self.manual_button, self))
        self.manual_button.clicked.connect(self.set_manually)
        self.dialog: fluent.ContentDialog | None = None
        self._tick = QTimer(self)
        self._tick.setInterval(1000)
        self._tick.timeout.connect(self.show_clock)
        self.set_enabled(False)

    # --- reading ---
    def read(self) -> timedate.TimeState:
        result = self.runner.run(commands.timedatectl_argv("show"), READ_TIMEOUT)
        if result.code != 0:
            raise tool_failure("Reading the date and time settings", result)
        return timedate.parse_timedatectl(result.stdout)

    def activate(self) -> None:
        super().activate()
        self._tick.start()
        self.show_clock()

    def deactivate(self) -> None:
        super().deactivate()
        self._tick.stop()
        if self.dialog is not None and self.dialog.is_open():
            self.dialog.reject()

    def now(self) -> datetime:
        """The time in the system's time zone (as set, even before this program's own zone follows)."""
        if self.state.timezone:
            try:
                return datetime.now(zoneinfo.ZoneInfo(self.state.timezone))
            except Exception:
                pass
        return datetime.now().astimezone()

    def show_clock(self) -> None:
        now = self.now()
        locale = QLocale.system()
        pattern = locale.timeFormat(QLocale.FormatType.ShortFormat)
        if "ss" not in pattern:
            pattern = pattern.replace("mm", "mm:ss", 1)
        self.clock.set_title(locale.toString(QTime(now.hour, now.minute, now.second), pattern))
        self.clock.set_description(locale.toString(QDate(now.year, now.month, now.day), QLocale.FormatType.LongFormat))

    def show_state(self, state: timedate.TimeState) -> None:
        self.state = state
        self.show_notice("")
        set_checked(self.auto, state.ntp)
        self.auto_card.set_description(
            "No time synchronisation service is installed on this system." if not state.can_ntp else
            "From time servers on the network" + (" · synchronised" if state.ntp and state.synchronized else
                                                  " · not synchronised yet" if state.ntp else ""))
        self.zone_card.set_description(timedate.zone_label(state.timezone) if state.timezone else "Unknown")
        self.manual_card.set_description("Turn off automatic time to set the clock yourself." if state.ntp else
                                         "The clock of this device is set to the time you give")
        self.set_enabled(True)
        self.show_clock()

    def load_failed(self, exc: Exception) -> None:
        self.set_enabled(False)
        self.show_notice("The date and time settings are not available: " + user_message(exc), "warning")

    def set_enabled(self, enabled: bool) -> None:
        self.auto.setEnabled(enabled and self.state.can_ntp)
        self.zone_button.setEnabled(enabled)
        self.manual_button.setEnabled(enabled and not self.state.ntp)

    # --- changes ---
    def _run(self, what: str, *words: str) -> None:
        result = self.runner.run(commands.timedatectl_argv(*words), CHANGE_TIMEOUT)
        if result.code != 0:
            raise tool_failure(what, result)

    def _auto_toggled(self, checked: bool) -> None:
        if self.rendering:
            return
        self.change(lambda: self._run("Turning automatic time " + ("on" if checked else "off"), "set-ntp",
                                      "true" if checked else "false"))

    def choose_zone(self) -> None:
        zones = self.zones if self.zones is not None else timedate.timezones()
        dialog = ZoneDialog(self, zones, self.state.timezone)
        self.dialog = dialog

        def finished(result) -> None:
            zone = dialog.chosen()
            dialog.deleteLater()
            self.dialog = None
            if result == fluent.ContentDialog.Result.PRIMARY and zone and zone != self.state.timezone:
                self.set_zone(zone)

        dialog.finished.connect(finished)
        dialog.open()

    def set_zone(self, zone: str) -> None:
        if not timedate.valid_timezone(zone):
            self.show_error("This is not a time zone name.")
            return
        self.change(lambda: self._run("Changing the time zone", "set-timezone", zone))

    def set_manually(self) -> None:
        dialog = fluent.ContentDialog(self, "Set the date and time", "", primary="Set", close="Cancel")
        now = self.now()
        edit = QDateTimeEdit(QDateTime(QDate(now.year, now.month, now.day), QTime(now.hour, now.minute, 0)), dialog)
        edit.setCalendarPopup(True)
        edit.setAccessibleName("Date and time")
        edit.setDateRange(QDate(2000, 1, 1), QDate(2099, 12, 31))
        dialog.content_layout.addWidget(edit)
        dialog.edit = edit
        self.dialog = dialog

        def finished(result) -> None:
            text = edit.dateTime().toString("yyyy-MM-dd HH:mm:ss")
            dialog.deleteLater()
            self.dialog = None
            if result == fluent.ContentDialog.Result.PRIMARY:
                self.set_time(text)

        dialog.finished.connect(finished)
        dialog.open()
        edit.setFocus(Qt.FocusReason.PopupFocusReason)

    def set_time(self, text: str) -> None:
        if not timedate.valid_manual_time(text):
            self.show_error("This is not a date and time that can be set.")
            return
        self.change(lambda: self._run("Setting the date and time", "set-time", text))


# --- region and language ----------------------------------------------------------------------------------------

class RegionSection(ToolSection):
    page = "system"
    title = "Region & language"
    order = 20
    keywords = ("language", "region", "locale", "formats", "regional format", "number format", "date format",
                "currency", "24-hour")

    def __init__(self, parent: QWidget | None = None, *, runner=None, root: str | None = None):
        super().__init__(parent)
        self.runner = runner or context.runner()
        self.root = context.root if root is None else root
        self.state = timedate.LocaleState()
        self.language = QComboBox(self)
        self.formats = QComboBox(self)
        for box in (self.language, self.formats):
            box.setMinimumWidth(300)
            box.setMaximumWidth(420)
            box.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        self.language_card = self.add(fluent.SettingsCard(
            "Language", "The language of the system and of applications",
            fluent.icon("preferences-desktop-locale"), self.language, self))
        self.formats_card = self.add(fluent.SettingsCard(
            "Regional format", "Dates, times (12 or 24 hours), numbers, currency, paper size and units", None,
            self.formats, self))
        self.language.activated.connect(lambda _i: self._chosen())
        self.formats.activated.connect(lambda _i: self._chosen())
        self.set_enabled(False)

    def read(self):
        status = self.runner.run(commands.localectl_argv("status"), READ_TIMEOUT)
        if status.code != 0:
            raise tool_failure("Reading the language settings", status)
        listed = self.runner.run(commands.argv("locale", "-a"), READ_TIMEOUT)
        locales = timedate.parse_locale_list(listed.stdout) if listed.code == 0 else []
        return (timedate.locale_state(timedate.parse_localectl(status.stdout)), locales,
                timedate.read_locale_names(locales, self.root))

    def show_state(self, loaded) -> None:
        state, locales, names = loaded
        self.state = state
        for box, current in ((self.language, state.language), (self.formats, state.formats)):
            offered = list(locales)
            if current and current not in offered:
                offered.append(current)            # in use although not listed: still shown as what it is
            if [box.itemData(i) for i in range(box.count())] != offered:
                box.clear()
                for locale in offered:
                    box.addItem(timedate.locale_label(locale, names.get(locale)), locale)
            index = offered.index(current) if current in offered else -1
            if box.currentIndex() != index:
                box.setCurrentIndex(index)
        few = len([name for name in locales if name != "C.UTF-8"]) <= 1
        self.language_card.set_description(
            "Only one language is generated on this system; an administrator adds more with "
            "dpkg-reconfigure locales." if few else "The language of the system and of applications")
        self.formats_card.set_description(
            "Dates, times (12 or 24 hours), numbers, currency, paper size and units"
            + (" · some formats are set separately and are kept until you choose here" if state.mixed else ""))
        self.set_enabled(True)

    def load_failed(self, exc: Exception) -> None:
        self.set_enabled(False)
        self.show_notice("The language settings are not available: " + user_message(exc), "warning")

    def set_enabled(self, enabled: bool) -> None:
        self.language.setEnabled(enabled and self.language.count() > 0)
        self.formats.setEnabled(enabled and self.formats.count() > 0)

    def _chosen(self) -> None:
        language, formats = self.language.currentData(), self.formats.currentData()
        if self.rendering or not language or not formats:
            return
        if (language, formats) == (self.state.language, self.state.formats) and not self.state.mixed:
            return
        try:
            words = timedate.set_locale_words(language, formats)
        except ValueError:
            self.show_error("This language cannot be set.")
            return

        def apply() -> None:
            result = self.runner.run(commands.localectl_argv("set-locale", *words), CHANGE_TIMEOUT)
            if result.code != 0:
                raise tool_failure("Changing the language", result)

        self.change(apply, "Saved. The new language and formats apply after you sign out and in again.")


class NotificationsSection(Section):
    page = "system"
    title = "Notifications"
    order = 30
    keywords = ("notifications", "do not disturb", "quiet")

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.card = self.add(fluent.SettingsCard(
            "Do not disturb", "Do not disturb is switched in the quick settings (Super+A); the notification "
            "centre opens with Super+N.", fluent.icon("notification-symbolic"), None, self))


# --- default applications ---------------------------------------------------------------------------------------

class FilesSection(ToolSection):
    """A ToolSection whose state is read from the user's files: it reads again when they change."""

    def __init__(self, parent: QWidget | None = None, *, environ=None):
        super().__init__(parent)
        self.environ = context.environ() if environ is None else environ
        self._watcher: QFileSystemWatcher | None = None
        self._changed = QTimer(self)
        self._changed.setSingleShot(True)
        self._changed.setInterval(300)
        self._changed.timeout.connect(self.reload)

    def watched(self) -> list[str]:
        return []

    def activate(self) -> None:
        if self.active:
            return
        super().activate()
        paths = [path for path in self.watched() if os.path.isdir(path)]
        if paths:
            self._watcher = QFileSystemWatcher(paths, self)
            self._watcher.directoryChanged.connect(lambda _p: self._changed.start())

    def deactivate(self) -> None:
        super().deactivate()
        self._changed.stop()
        if self._watcher is not None:
            self._watcher.deleteLater()
            self._watcher = None

    def locale(self) -> str | None:
        return self.environ.get("LC_ALL") or self.environ.get("LC_MESSAGES") or self.environ.get("LANG")


class DefaultAppsSection(FilesSection):
    page = "applications"
    title = "Default applications"
    order = 60
    keywords = ("default applications", "default apps", "browser", "mail", "file manager", "text editor",
                "image viewer", "video player", "music player", "pdf", "open with")

    def __init__(self, parent: QWidget | None = None, *, environ=None):
        super().__init__(parent, environ=environ)
        self.apps: list[defaultapps.App] = []
        self.boxes: dict[str, QComboBox] = {}
        self.cards: dict[str, fluent.SettingsCard] = {}
        for kind in defaultapps.KINDS:
            box = QComboBox(self)
            box.setMinimumWidth(240)
            box.setMaximumWidth(300)
            box.activated.connect(lambda _i, k=kind: self._chosen(k))
            self.boxes[kind.key] = box
            self.cards[kind.key] = self.add(fluent.SettingsCard(kind.title, kind.description,
                                                                fluent.icon(kind.icon), box, self))
            box.setEnabled(False)

    def watched(self) -> list[str]:
        try:
            return [userfiles.config_dir(self.environ)]
        except userfiles.UserFileError:
            return []

    def read(self):
        apps = defaultapps.installed_apps(self.environ, self.locale())
        return apps, defaultapps.read_defaults(self.environ)

    def show_state(self, loaded) -> None:
        self.apps, layers = loaded
        for kind in defaultapps.KINDS:
            box, card = self.boxes[kind.key], self.cards[kind.key]
            offered = defaultapps.candidates(kind, self.apps)
            current = defaultapps.current_default(kind, self.apps, layers)
            ids = [app.desktop_id for app in offered]
            if [box.itemData(i) for i in range(box.count())] != ids:
                box.clear()
                for app in offered:
                    box.addItem(app.name, app.desktop_id)
            index = ids.index(current) if current in ids else -1
            if box.currentIndex() != index:
                box.setCurrentIndex(index)
            if index < 0:
                box.setPlaceholderText("Not set" if offered else "No application installed")
            box.setEnabled(bool(offered) and not self.busy)
            card.set_description(kind.description if offered else
                                 f"{kind.description}. No installed application declares this.")

    def set_enabled(self, enabled: bool) -> None:
        for box in self.boxes.values():
            box.setEnabled(enabled and box.count() > 0)

    def _chosen(self, kind: defaultapps.Kind) -> None:
        if self.rendering:
            return
        app = next((a for a in self.apps if a.desktop_id == self.boxes[kind.key].currentData()), None)
        if app is None:
            return
        environ = self.environ

        def apply() -> None:
            path = os.path.join(userfiles.config_dir(environ), "mimeapps.list")
            try:
                with open(path, "rb") as handle:
                    text = handle.read(userfiles.MAX_CONFIG + 1).decode("utf-8", errors="surrogateescape")
            except FileNotFoundError:
                text = ""
            except OSError as exc:
                raise Failed(f"Your default applications could not be read ({exc.strerror or exc}).") from None
            try:
                data = defaultapps.with_default(text, kind, app).encode("utf-8", errors="surrogateescape")
                userfiles.write_config(environ, "mimeapps.list", data)
            except (ValueError, userfiles.UserFileError) as exc:
                raise Failed(f"{app.name} could not be made the default {kind.title.lower()}. {exc}") from None

        self.change(apply)


# --- start-up applications ---------------------------------------------------------------------------------------

class StartupRow(fluent.SettingsCard):
    toggled = Signal(str, bool)
    remove_requested = Signal(str)

    def __init__(self, ident: str, parent: QWidget | None = None):
        super().__init__("", "", None, None, parent)
        self.ident = ident
        self.entry: defaultapps.Startup | None = None
        self.switch = fluent.ToggleSwitch()
        self.set_control(self.switch)
        self.switch.toggled.connect(self._toggled)
        self.remove_button = fluent.Button("Remove", self, subtle=True)
        self.remove_button.clicked.connect(lambda: self.remove_requested.emit(self.ident))
        self.add_control(self.remove_button)
        self._setting = False

    def _toggled(self, checked: bool) -> None:
        if not self._setting:
            self.toggled.emit(self.ident, checked)

    def update_entry(self, entry: defaultapps.Startup, enabled: bool) -> None:
        self.entry = entry
        self.set_title(entry.name)
        where = "Added by you" if entry.removable else "Set up for all users of this device"
        self.set_description(f"{entry.comment} · {where}" if entry.comment else where)
        self.set_icon(fluent.icon(entry.icon, "application-x-executable", "preferences-system")
                      if entry.icon and "/" not in entry.icon else fluent.icon("preferences-system"))
        self._setting = True
        set_checked(self.switch, entry.enabled)
        self._setting = False
        self.switch.setAccessibleName(f"Start {entry.name} when you sign in")
        self.switch.setEnabled(enabled and entry.changeable)
        self.switch.setToolTip("" if entry.changeable else "This entry has a file name Control Center does not "
                               "change")
        self.remove_button.setVisible(entry.removable and entry.changeable)
        self.remove_button.setAccessibleName(f"Remove {entry.name} from the start-up applications")
        self.remove_button.setEnabled(enabled)


class AppDialog(fluent.ContentDialog):
    """Choose one installed application from a searchable list."""

    def __init__(self, parent: QWidget, apps: list[defaultapps.App], title: str, confirm: str):
        super().__init__(parent, title, "", primary=confirm, close="Cancel")
        self.apps = apps
        self.search = fluent.SearchBox(self, placeholder="Search applications")
        self.search.setAccessibleName("Search applications")
        self.list = QListWidget(self)
        self.list.setAccessibleName("Installed applications")
        self.list.setMinimumHeight(300)
        self.list.setMinimumWidth(400)
        self.content_layout.addWidget(self.search)
        self.content_layout.addWidget(self.list)
        self.search.textChanged.connect(self.filter)
        self.list.currentItemChanged.connect(lambda *_a: self.primary_button.setEnabled(self.chosen() is not None))
        self.list.itemActivated.connect(lambda _item: self.accept())
        self.filter("")

    def filter(self, query: str) -> None:
        words = query.casefold().split()
        self.list.clear()
        for app in self.apps:
            if all(word in f"{app.name} {app.desktop_id}".casefold() for word in words):
                item = QListWidgetItem(app.name)
                item.setData(Qt.ItemDataRole.UserRole, app.desktop_id)
                self.list.addItem(item)
        if words and self.list.count():
            self.list.setCurrentRow(0)
        self.primary_button.setEnabled(self.chosen() is not None)

    def chosen(self) -> defaultapps.App | None:
        item = self.list.currentItem()
        ident = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        return next((app for app in self.apps if app.desktop_id == ident), None)

    def open(self) -> None:
        super().open()
        self.search.setFocus(Qt.FocusReason.PopupFocusReason)


class StartupSection(FilesSection):
    page = "applications"
    title = "Start-up applications"
    order = 70
    keywords = ("start-up", "startup", "autostart", "start automatically", "sign in", "login", "log in")

    def __init__(self, parent: QWidget | None = None, *, environ=None):
        super().__init__(parent, environ=environ)
        self.add_button = fluent.Button("Add…", self, icon=fluent.icon("list-add-symbolic"))
        self.add_button.setAccessibleName("Add a start-up application")
        self.add_button.clicked.connect(self.add_app)
        self.header = self.add(fluent.SettingsCard("Applications that start when you sign in", "", None,
                                                   self.add_button, self))
        self.list = RowList(self)
        self.add(self.list)
        self.entries: list[defaultapps.Startup] = []
        self.apps: list[defaultapps.App] = []
        self.dialog: AppDialog | None = None
        self.add_button.setEnabled(False)

    def watched(self) -> list[str]:
        user, _system = defaultapps.autostart_dirs(self.environ)
        paths = [user] if user else []
        try:
            paths.append(userfiles.config_dir(self.environ))        # autostart may not exist yet
        except userfiles.UserFileError:
            pass
        return paths

    def read(self):
        return (defaultapps.startup_entries(self.environ, self.locale()),
                defaultapps.installed_apps(self.environ, self.locale()))

    def show_state(self, loaded) -> None:
        self.entries, self.apps = loaded
        by_id = {entry.ident: entry for entry in self.entries}
        self.list.sync(list(by_id), self._row, lambda ident, row: row.update_entry(by_id[ident], not self.busy))
        count = sum(1 for entry in self.entries if entry.enabled)
        self.header.set_description("None" if not self.entries else
                                    f"{count} of {len(self.entries)} switched on")
        self.add_button.setEnabled(not self.busy)

    def _row(self, ident: str) -> StartupRow:
        row = StartupRow(ident, self.list)
        row.toggled.connect(self.switch)
        row.remove_requested.connect(self.remove)
        return row

    def set_enabled(self, enabled: bool) -> None:
        self.add_button.setEnabled(enabled)
        for ident in self.list.keys():
            row = self.list.row(ident)
            if row.entry is not None:
                row.update_entry(row.entry, enabled)

    def deactivate(self) -> None:
        super().deactivate()
        if self.dialog is not None and self.dialog.is_open():
            self.dialog.reject()

    # --- changes ---
    def switch(self, ident: str, enabled: bool) -> None:
        entry = next((e for e in self.entries if e.ident == ident), None)
        if entry is None or self.rendering:
            return
        environ = self.environ

        def apply() -> None:
            try:
                name = defaultapps.autostart_name(ident)
                user_text, system_text = defaultapps.read_texts(environ, ident)
                text = defaultapps.switched(entry, enabled, user_text, system_text)
                if text is None:
                    userfiles.remove_config(environ, name)
                else:
                    userfiles.write_config(environ, name, text.encode("utf-8", errors="replace"))
            except (ValueError, userfiles.UserFileError) as exc:
                raise Failed(f"{entry.name} could not be switched {'on' if enabled else 'off'}. {exc}") from None

        self.change(apply)

    def remove(self, ident: str) -> None:
        entry = next((e for e in self.entries if e.ident == ident), None)
        if entry is None or not entry.removable:
            return
        if not fluent.ask(self, f"Remove {entry.name}?", "It no longer starts when you sign in. The application "
                          "itself stays installed.", "Remove", "Cancel"):
            return
        environ = self.environ

        def apply() -> None:
            try:
                userfiles.remove_config(environ, defaultapps.autostart_name(ident))
            except (ValueError, userfiles.UserFileError) as exc:
                raise Failed(f"{entry.name} could not be removed. {exc}") from None

        self.change(apply)

    def add_app(self) -> None:
        present = {entry.ident for entry in self.entries}
        offered = [app for app in self.apps if app.desktop_id not in present
                   and defaultapps.ID_RE.fullmatch(app.desktop_id)]
        dialog = AppDialog(self, offered, "Add a start-up application", "Add")
        self.dialog = dialog

        def finished(result) -> None:
            app = dialog.chosen()
            dialog.deleteLater()
            self.dialog = None
            if result == fluent.ContentDialog.Result.PRIMARY and app is not None:
                self.add_chosen(app)

        dialog.finished.connect(finished)
        dialog.open()

    def add_chosen(self, app: defaultapps.App) -> None:
        environ = self.environ

        def apply() -> None:
            try:
                with open(app.path, "rb") as handle:
                    text = handle.read(defaultapps.MAX_FILE + 1).decode("utf-8", errors="replace")
                data = defaultapps.added(text).encode("utf-8", errors="replace")
                userfiles.write_config(environ, defaultapps.autostart_name(app.desktop_id), data)
            except OSError as exc:
                raise Failed(f"{app.name} could not be read ({exc.strerror or exc}).") from None
            except (ValueError, userfiles.UserFileError) as exc:
                raise Failed(f"{app.name} could not be added. {exc}") from None

        self.change(apply)


# --- privacy ---------------------------------------------------------------------------------------------------

class RecentFilesSection(FilesSection):
    page = "privacy"
    title = "Recent files"
    order = 30
    keywords = ("recent files", "recently used", "history", "clear history")

    def __init__(self, parent: QWidget | None = None, *, environ=None):
        super().__init__(parent, environ=environ)
        self.count = 0
        self.clear_button = fluent.Button("Clear", self, icon=fluent.icon("edit-clear-symbolic"))
        self.clear_button.setAccessibleName("Clear the list of recently used files")
        self.clear_button.clicked.connect(self.clear)
        self.card = self.add(fluent.SettingsCard("Recently used files", "", fluent.icon("document-open-symbolic"),
                                                 self.clear_button, self))
        self.clear_button.setEnabled(False)

    def watched(self) -> list[str]:
        try:
            return [userfiles.data_dir(self.environ)]
        except userfiles.UserFileError:
            return []

    def read(self) -> int:
        return userfiles.recent_files_count(self.environ)

    def show_state(self, count: int) -> None:
        self.count = count
        self.card.set_description(
            ("The list applications share is empty." if not count else
             f"The list applications share names {count} file{'s' if count != 1 else ''}.")
            + " Text Editor and Media Player keep their own lists; clear those in their menus.")
        self.clear_button.setEnabled(count > 0 and not self.busy)

    def set_enabled(self, enabled: bool) -> None:
        self.clear_button.setEnabled(enabled and self.count > 0)

    def clear(self) -> None:
        environ = self.environ
        removed = []

        def apply() -> None:
            try:
                removed.append(userfiles.clear_recent_files(environ))
            except userfiles.UserFileError as exc:
                raise Failed(f"The list could not be cleared. {exc}") from None

        self.change(apply, "The list of recently used files was cleared. The files themselves were not touched.")


SECTIONS = (DateTimeSection, RegionSection, NotificationsSection, DefaultAppsSection, StartupSection,
            RecentFilesSection)
